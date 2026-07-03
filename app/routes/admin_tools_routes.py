# AirTrack 1.0.0
# Copyright (c) 2025 Trevor ('Subhuti'). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary-NC

import json

import logging

import os

import gzip

import shutil

import time

import signal

import threading

import shlex

import subprocess

import html

from pathlib import Path

from datetime import datetime, timedelta

from flask import (
    Blueprint, flash, jsonify, redirect, render_template,
    request, send_file, url_for, current_app, Response, session
)


from sqlalchemy import text

from extensions import db

from utils.theme_scanner import scan_all

from utils.export_mobile_db import export_mobile_database

from utils.link_checker import append_to_whitelist

from security.guards import require_server

admin_tools_bp = Blueprint('admin_tools', __name__, url_prefix='/admin_tools')

# ====================== Utility Functions ======================

def _is_admin() -> bool:
    try:
        return session.get('is_admin', False) is True
    except Exception:
        return False


def _is_git_dir(p: Path) -> bool:
    g = p / '.git'
    return g.is_dir() or g.is_file()


def _run_cmd(cmd: str, cwd: Path | None = None, extra_env: dict | None = None):
    import os as _os
    env = {**_os.environ, **(extra_env or {})}
    p = subprocess.Popen(
        shlex.split(cmd), cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env,
    )
    out, err = p.communicate()
    return p.returncode, (out or '').strip(), (err or '').strip()


# Git env — suppress interactive prompts, don't touch GIT_SSH_COMMAND
def _git_env() -> dict:
    """Return env for git subprocesses with GIT_SSH_COMMAND removed."""
    import os as _os
    env = {k: v for k, v in _os.environ.items() if k != 'GIT_SSH_COMMAND'}
    env['GIT_TERMINAL_PROMPT'] = '0'
    return env


def _repo_root() -> Path | None:
    env_path = os.getenv('AIRTRACK_REPO_DIR')
    if env_path:
        p = Path(env_path).resolve()
        if _is_git_dir(p):
            return p
    here = Path(__file__).resolve()
    for p in [here.parent] + list(here.parents):
        if _is_git_dir(p):
            return p
    root = Path(current_app.root_path).resolve()
    for p in [root] + list(root.parents):
        if _is_git_dir(p):
            return p
    return None


def _ok(**payload):
    return jsonify(payload), 200


def _err(msg, code=400, **extra):
    d = {"status": "error", "detail": msg}
    d.update(extra)
    return jsonify(d), code


def _ensure_identity(repo: Path):
    rc, name, _ = _run_cmd("git config user.name", cwd=repo)
    if rc != 0 or not name:
        _run_cmd('git config user.name "AirTrack HUD"', cwd=repo)
    rc, email, _ = _run_cmd('git config user.email', cwd=repo)
    if rc != 0 or not email:
        _run_cmd("git config user.email 'hud@example.com'", cwd=repo)

def _shutdown_enabled() -> bool:
    return os.getenv('AIRTRACK_ENABLE_SHUTDOWN', '0').lower() in {
        '1', 'true', 'yes', 'on'
    }


def _days_ago(n: int) -> float:
    return (datetime.now() - timedelta(days=n)).timestamp()


def _fmt_bytes(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _gzip_file(src: Path) -> tuple[int, int]:
    if src.suffix == '.gz' or not src.is_file():
        return (0, 0)
    gz = src.with_suffix(src.suffix + '.gz')
    if gz.exists():
        return (0, 0)
    old = src.stat().st_size
    with src.open('rb') as f_in, gzip.open(gz, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)
    new = gz.stat().st_size
    return (old, new)


def _delete_old_files(root: Path, older_than_ts: float, dry_run: bool) -> dict:
    res = {"files": 0, "bytes": 0, "paths": []}
    if not root.exists():
        return res
    for p in root.rglob('*'):
        try:
            if not p.is_file():
                continue
            st = p.stat()
            if st.st_mtime < older_than_ts:
                res['files'] += 1
                res['bytes'] += st.st_size
                res['paths'].append(str(p))
                if not dry_run:
                    p.unlink(missing_ok=True)
        except Exception:
            continue
    return res


def _rotate_logs(log_dir: Path, older_than_ts: float, dry_run: bool) -> dict:
    res = {"compressed": 0, "deleted": 0, "bytes_saved": 0}
    if not log_dir.exists():
        return res
    for p in log_dir.glob('*.log'):
        try:
            if p.stat().st_mtime < older_than_ts:
                old, new = (0, 0) if dry_run else _gzip_file(p)
                if not dry_run and old and new:
                    p.unlink(missing_ok=True)
                    res['compressed'] += 1
                    res['bytes_saved'] += max(0, old - new)
        except Exception:
            continue
    for gz in log_dir.glob('*.log.gz'):
        try:
            if gz.stat().st_mtime < _days_ago(90):
                if not dry_run:
                    size = gz.stat().st_size
                    gz.unlink(missing_ok=True)
                    res['deleted'] += 1
                    res['bytes_saved'] += size
        except Exception:
            continue
    return res

# ====================== Routes ======================


def test_airport():
    code = (request.args.get('code') or '').upper()
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM airports WHERE ICAO = :c OR IATA = :c LIMIT 1"),
            {"c": code},
        ).mappings().fetchone()
    return jsonify({"code": code, "row_keys": list(row.keys()) if row else []})


@admin_tools_bp.route('/update_airport_link', methods=['POST'])
@require_server
def update_airport_link():
    """Update home_link or wikipedia_link directly in the airports table."""
    icao      = (request.form.get('icao') or '').strip().upper()
    field     = request.form.get('field')   # 'home_link' or 'wikipedia_link'
    new_value = (request.form.get('new_value') or '').strip()

    # Hardcoded map prevents any user-supplied value reaching the SQL string
    _AIRPORT_LINK_COLS = {'home_link': 'home_link', 'wikipedia_link': 'wikipedia_link'}
    safe_col = _AIRPORT_LINK_COLS.get(field)
    if not icao or safe_col is None or not new_value:
        flash("Missing or invalid fields.", 'danger')
        return redirect(url_for('admin_tools.broken_links'))

    try:
        db.session.execute(
            text(f"UPDATE airports SET {safe_col} = :val WHERE ICAO = :icao"),
            {"val": new_value, "icao": icao},
        )
        db.session.commit()
        label = "Website" if field == "home_link" else "Wikipedia"
        flash(f"✅ {label} updated for {icao}.", 'success')
        logging.info(f"Airport link updated: {icao} {field} → {new_value}")
    except Exception as e:
        db.session.rollback()
        logging.error(f"❌ Airport link update failed: {e}")
        flash(f"❌ Failed to update link: {e}", 'danger')
    return redirect(url_for('admin_tools.broken_links'))


@admin_tools_bp.route('/whitelist_link', methods=['POST'])
def whitelist_link():
    icao = (request.form.get('icao') or '').strip().upper()
    label = request.form.get('label')
    url = request.form.get('url')
    if not icao or not label or not url:
        flash("Missing required fields", 'danger')
        return redirect(url_for('admin_tools.broken_links'))
    try:
        whitelist_path = os.path.join('logs', 'link_whitelist.txt')
        append_to_whitelist(whitelist_path, icao, label, url)
        flash(f"✅ Whitelisted link for {icao} ({label})", 'success')
    except Exception as e:
        logging.error(f"❌ Whitelist error: {e}")
        flash("❌ Failed to whitelist link.", 'danger')
    return redirect(url_for('admin_tools.broken_links'))


@admin_tools_bp.route('/git_commit', methods=['POST'])
@require_server

def git_commit():
    repo = _repo_root()
    if not repo:
        return _err("❌ Repo root not found.")
    _ensure_identity(repo)

    data = request.get_json(silent=True) or {}
    msg = (data.get('message') or request.form.get('message') or '').strip()
    if not msg:
        return _err("⚠️ Commit message required.")

    try:
        _run_cmd("git add -A", cwd=repo)
        rc, out, err = _run_cmd(f'git commit -m "{msg}"', cwd=repo)
        if rc != 0:
            if "nothing to commit" in out or "nothing to commit" in err:
                return _ok(status='noop', detail='ℹ️ Nothing to commit.')
            return _err("❌ Git commit failed.", detail=err or out)
        return _ok(status='ok', detail='✅ Commit successful.')
    except Exception as e:
        logging.exception("git commit failed"); return _err("Commit failed — see server logs")


@admin_tools_bp.route('/git_push', methods=['POST'])
@require_server

def git_push():
    """Push committed changes to remote."""
    repo = _repo_root()
    if not repo:
        return _err("❌ Repo root not found.")

    try:
        rc, out, err = _run_cmd("git push", cwd=repo)
        if rc != 0:
            return _err("❌ Git push failed.", detail=err or out)
        return _ok(status="ok", detail="✅ Push successful.")
    except Exception as e:
        logging.exception("git push failed"); return _err("Push failed — see server logs")


@admin_tools_bp.route('/git_pull', methods=['POST'])
@require_server

def git_pull():
    """Pull latest commits from remote into the server repo."""
    repo = _repo_root()
    if not repo:
        return _err("❌ Repo root not found.")

    try:
        rc, out, err = _run_cmd("git fetch origin", cwd=repo, extra_env=_git_env())
        if rc != 0:
            logging.error(f"git fetch failed: {err or out}"); return _err("git fetch failed — see server logs")

        rc2, behind_str, _ = _run_cmd("git rev-list --count HEAD..origin/main", cwd=repo)
        behind = int(behind_str) if rc2 == 0 and behind_str.strip().isdigit() else 0

        if behind == 0:
            return _ok(status="noop", detail="ℹ️ Already up to date.", restart_required=False)

        rc3, out3, err3 = _run_cmd("git reset --hard origin/main", cwd=repo, extra_env=_git_env())
        if rc3 != 0:
            logging.error(f"git reset failed: {err3 or out3}"); return _err("git reset failed — see server logs")

        _run_cmd("git clean -fd", cwd=repo, extra_env=_git_env())
        return _ok(status="ok", detail=out3 or "✅ Pull successful.", restart_required=True)
    except Exception as e:
        logging.exception("git pull failed"); return _err("Pull failed — see server logs")


@admin_tools_bp.get("/themes")

def list_themes():
    themes = scan_all()
    return jsonify(themes)


@admin_tools_bp.post("/rescan_themes")

def rescan_themes():
    """Force a full theme rescan, bypassing the staleness check."""
    try:
        from utils.theme_scanner import scan_and_generate
        themes = scan_and_generate()
        return _ok(status="ok", count=len(themes))
    except Exception as e:
        logging.exception("rescan_themes failed")
        logging.exception("rescan failed"); return _err("Rescan failed — see server logs")


@admin_tools_bp.route("/export_db_mobile")

def export_db_mobile():
    try:
        db_path = export_mobile_database()
        if not db_path or not os.path.isfile(db_path):
            raise ValueError("Mobile export file not found or invalid.")
        return send_file(
            db_path,
            as_attachment=True,
            download_name="airtrack_mobile.db",
            mimetype="application/octet-stream",
        )
    except Exception as e:
        logging.exception("❌ Mobile DB export failed")
        logging.exception("export failed"); return _err("Export failed — see server logs")


@admin_tools_bp.route("/housekeeping", methods=["POST"])

def housekeeping():
    """
    AirTrack Housekeeping — available on both server and client.
    - Accepts JSON { dry_run: bool, retention_days: int }
    - No CSRF required for JSON API use
    - Returns strict JSON (never HTML)
    """

    # Load JSON safely
    data = request.get_json(silent=True) or {}

    # Accept JS-side naming
    dry_run = bool(data.get("dry_run"))
    keep_days = int(data.get("retention_days") or data.get("keep_days") or 30)

    if keep_days < 7:
        return _err("⚠️ Minimum keep period is 7 days.")

    cutoff_ts = _days_ago(keep_days)

    # Resolve paths — respect AIRTRACK_HOME on Windows, fall back to Docker paths
    _home = os.getenv("AIRTRACK_HOME", "").strip()
    if _home:
        logs_path    = (Path(_home) / "logs").resolve()
        backups_path = (Path(_home) / "backups").resolve()
        static_export_path = (Path(_home) / "exports").resolve()
    else:
        logs_path    = Path("/app/logs").resolve()
        backups_path = Path("/app/backups").resolve()
        static_export_path = Path("/app/static/export").resolve()

    # Perform operations
    deleted_backups = _delete_old_files(backups_path, cutoff_ts, dry_run)
    deleted_exports = _delete_old_files(static_export_path, cutoff_ts, dry_run)
    rotated_logs = _rotate_logs(logs_path, cutoff_ts, dry_run)

    # Success JSON
    return _ok(
        dry_run=dry_run,
        retention_days=keep_days,
        cutoff_timestamp=cutoff_ts,
        deleted_backups=deleted_backups,
        deleted_exports=deleted_exports,
        rotated_logs=rotated_logs,
    )


@admin_tools_bp.route("/shutdown", methods=["POST"])
@require_server

def shutdown():
    try:

        def _kill():
            time.sleep(0.5)
            os.kill(os.getppid(), signal.SIGTERM)
        threading.Thread(target=_kill).start()
        return _ok(status="success", detail="Server shutting down…")
    except Exception as e:
        logging.exception("shutdown failed"); return _err("Shutdown failed — see server logs")


# ====================== Log Management Routes ======================

@admin_tools_bp.route("/logs")
@require_server

def logs():
    """List all available log files in /logs directory."""
    logs_dir = Path(current_app.root_path) / "logs"

    logging.warning(
        f"🧩 Checking logs dir: {logs_dir} "
        f"(exists={logs_dir.exists()})"
    )

    logs = []
    try:
        if logs_dir.exists():
            for p in sorted(logs_dir.glob("*")):
                if p.is_file():
                    stat = p.stat()
                    logs.append(
                        {
                            "name": p.name,
                            "size": _fmt_bytes(stat.st_size),
                            "modified": datetime
                            .fromtimestamp(stat.st_mtime)
                            .strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )

                    logging.warning(
                        f"🪶 Found log file: {p.name} "
                        f"({stat.st_size} bytes)"
                    )
        else:
            logging.error(f"❌ Log directory not found: {logs_dir}")

        if not logs:
            logging.warning("⚠️ No log files found in logs_dir")

        return render_template("admin_logs.html", logs=logs)

    except Exception as e:
        logging.exception("❌ Failed to list logs")
        logging.exception("log list failed"); return _err("Failed to list logs — see server logs")


@admin_tools_bp.route("/logs_view/<path:filename>")
@require_server

def logs_view(filename):
    """Display a log file's contents in a browser."""
    logs_dir = Path(current_app.root_path) / "logs"
    # Build allowlist from filesystem — value used in path comes from FS, not request
    valid_paths = {str(p.relative_to(logs_dir)): p
                   for p in logs_dir.glob("**/*") if p.is_file()}
    if filename not in valid_paths:
        return html.escape(f"Log file not found: {filename}"), 404
    file_path = valid_paths[filename]  # filesystem-sourced Path, not derived from request

    try:
        with file_path.open(
            "r",
            encoding="utf-8",
            errors="replace",
        ) as f:
            content = f.read()

        escaped = html.escape(content)

        return Response(
            f"<pre style='font-size:12px'>{escaped}</pre>",
            mimetype="text/html",
        )

    except Exception as e:
        logging.exception(f"❌ Error reading log {filename}")
        logging.exception("log read failed"); return _err("Failed to read log — see server logs")


@admin_tools_bp.route("/logs_download/<path:filename>")
@require_server

def logs_download(filename):
    """Allow downloading a log file."""
    logs_dir = Path(current_app.root_path) / 'logs'
    valid_paths = {str(p.relative_to(logs_dir)): p
                   for p in logs_dir.glob("**/*") if p.is_file()}
    if filename not in valid_paths:
        return html.escape(f"Log file not found: {filename}"), 404
    file_path = valid_paths[filename]

    try:
        return send_file(
            str(file_path),
            as_attachment=True,
            download_name=file_path.name,
            mimetype="text/plain"
        )
    except Exception as e:
        logging.exception(f"❌ Error sending log file {filename}")
        logging.exception("log download failed"); return _err("Failed to download log — see server logs")


@admin_tools_bp.route("/check_whitelist_links", methods=["POST"])
@require_server

def check_whitelist_links():
    """
    Recheck all whitelisted links and generate a report in /logs.
    """
    output_dir = os.path.join(current_app.root_path, 'logs')
    try:
        from utils.link_checker import check_whitelist_links as check_links
        filename = check_links(output_dir)
        flash(f"✅ Whitelist links checked. Report: {filename}", "success")
    except Exception as e:
        logging.exception("❌ Failed to check whitelist links")
        flash(f"❌ Failed to check whitelist links: {e}", "danger")
    return redirect(url_for("admin.admin_dashboard"))


@admin_tools_bp.route("/check_airport_links", methods=["POST"])
@require_server
def check_airport_links():
    """
    Check website and Wikipedia links for all logged airports and show broken ones.
    Only checks airports the operator has actually logged (departure/arrival).
    """
    from utils.link_checker import check_airport_links as run_check
    output_dir = os.path.join(current_app.root_path, 'logs')
    try:
        result = db.session.execute(text("""
            SELECT DISTINCT a.ICAO, a.AirportName, a.home_link, a.wikipedia_link
            FROM airports a
            JOIN (
                SELECT DISTINCT Departure AS ICAO FROM flights
                UNION
                SELECT DISTINCT Arrival AS ICAO FROM flights
            ) f ON a.ICAO = f.ICAO
            WHERE (a.home_link IS NOT NULL AND a.home_link != '')
               OR (a.wikipedia_link IS NOT NULL AND a.wikipedia_link != '')
        """)).fetchall()
        broken = run_check(result, output_dir)

        # Auto-update any redirected links in the database
        redirects = [e for e in broken if e['status'] == 'REDIRECTED' and e.get('redirect_to')]
        if redirects:
            for entry in redirects:
                field = 'home_link' if entry['label'] == 'Website' else 'wikipedia_link'
                try:
                    db.session.execute(
                        text(f"UPDATE airports SET {field} = :val WHERE ICAO = :icao"),
                        {"val": entry['redirect_to'], "icao": entry['icao']},
                    )
                    logging.info(
                        f"Auto-updated redirect: {entry['icao']} {field} "
                        f"{entry['url']} → {entry['redirect_to']}"
                    )
                except Exception as upd_err:
                    logging.warning(f"Auto-update skipped for {entry['icao']}: {upd_err}")
            db.session.commit()
            flash(f"✅ Auto-updated {len(redirects)} redirected link(s).", "success")

        # Only show entries that still need human action
        remaining = [e for e in broken if e['status'] != 'REDIRECTED']

    except Exception as e:
        logging.exception("❌ Airport link check failed")
        flash(f"❌ Airport link check failed: {e}", "danger")
        remaining = []
    return render_template("admin_broken_links.html", broken_links=remaining)


@admin_tools_bp.get("/broken_links")
@require_server
def broken_links():
    """
    Show the broken links page with no results — trigger a check from the Cockpit HUD.
    """
    return render_template("admin_broken_links.html", broken_links=[])


@admin_tools_bp.route("/check_aircraft_images", methods=["POST"])
@require_server

def check_aircraft_images():
    """
    Check aircraft image links (from the Aircraft table) and write a report.
    """
    from utils.link_checker import check_url, normalize_url

    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        output_dir = os.path.join('logs')
        os.makedirs(output_dir, exist_ok=True)

        with db.engine.connect() as conn:
            results = conn.execute(
                text("SELECT Registration, Aircraft_Image FROM aircraft")
            ).fetchall()

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"image_check_report_{timestamp}.txt"
        filepath = os.path.join(output_dir, filename)

        tasks = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            for registration, img in results:
                url = normalize_url(img)
                if url:
                    tasks.append(
                        executor.submit(
                            check_url,
                            registration,
                            registration,
                            url,
                            "Aircraft Image"
                        )
                    )

            with open(filepath, "w", encoding="utf-8") as f:
                f.write("Aircraft Image Link Report\n")
                f.write("=" * 40 + "\n\n")
                for future in as_completed(tasks):
                    icao, name, label, url, status, _redirect_to = future.result()
                    from utils.link_checker import is_ok
                    if is_ok(status):
                        f.write(f"[{icao}] ✅ {status}: {url}\n")
                    else:
                        f.write(f"[{icao}] ❌ {status}: {url}\n")

        flash(f"✅ Aircraft image check complete. Report: {filename}", "success")

    except Exception as e:
        logging.exception("❌ Aircraft image check failed")
        flash(f"❌ Aircraft image check failed: {e}", "danger")

    return redirect(url_for("admin.admin_dashboard"))


@admin_tools_bp.get("/admin/protected/ping")
@require_server

def protected_ping():
    """Simple probe endpoint used by the Admin Cockpit to test server elevation."""
    return jsonify({"protected": True})


@admin_tools_bp.get("/check_updates")
def check_updates():
    """
    Check whether the local repo is behind origin/main.
    Returns: { behind, ahead, files_needing_update[], emergency_update }
    Works on both server (git-push workflow) and client (git-pull workflow).
    """
    repo = _repo_root()
    if not repo:
        return _ok(behind=0, ahead=0, files_needing_update=[], emergency_update=False, status="unavailable", detail="Update check not available on installed client.")

    try:
        # Fetch quietly — don't fail if offline
        _run_cmd("git fetch origin", cwd=repo, extra_env=_git_env())

        # Commits behind and ahead of origin/main
        rc, behind_str, _ = _run_cmd(
            "git rev-list --count HEAD..origin/main", cwd=repo
        )
        behind = int(behind_str) if rc == 0 and behind_str.isdigit() else 0

        rc, ahead_str, _ = _run_cmd(
            "git rev-list --count origin/main..HEAD", cwd=repo
        )
        ahead = int(ahead_str) if rc == 0 and ahead_str.isdigit() else 0

        # Files changed on origin/main that aren't in HEAD
        files_needing_update: list[str] = []
        if behind > 0:
            rc, diff_out, _ = _run_cmd(
                "git diff --name-only HEAD origin/main", cwd=repo
            )
            if rc == 0 and diff_out:
                files_needing_update = [f for f in diff_out.splitlines() if f]

        return _ok(
            behind=behind,
            ahead=ahead,
            files_needing_update=files_needing_update,
            emergency_update=False,
        )
    except Exception as exc:
        logging.exception("check_updates failed")
        logging.exception("update check failed"); return _err("Update check failed — see server logs", code=500)


@admin_tools_bp.post("/run_updater")
def run_updater():
    """
    Pull latest commits from origin/main.
    Returns: { status, detail, restart_required }
    """
    repo = _repo_root()
    if not repo:
        return _ok(behind=0, ahead=0, files_needing_update=[], emergency_update=False, status="unavailable", detail="Update check not available on installed client.")

    try:
        # Fetch latest from origin
        rc, out, err = _run_cmd("git fetch origin", cwd=repo, extra_env=_git_env())
        if rc != 0:
            logging.error(f"git fetch failed: {err or out}"); return _err("git fetch failed — see server logs", code=500)

        # Check if we're already current
        rc2, behind_str, _ = _run_cmd(
            "git rev-list --count HEAD..origin/main", cwd=repo
        )
        behind = int(behind_str) if rc2 == 0 and behind_str.isdigit() else 0

        if behind == 0:
            return _ok(status="success", detail="Already up to date.", restart_required=False)

        # Hard reset to origin/main — wipes local divergence cleanly
        rc3, out3, err3 = _run_cmd("git reset --hard origin/main", cwd=repo, extra_env=_git_env())
        if rc3 != 0:
            logging.error(f"git reset failed: {err3 or out3}"); return _err("git reset failed — see server logs", code=500)

        # Clean up untracked files that conflict with the new state
        _run_cmd("git clean -fd", cwd=repo, extra_env=_git_env())

        return _ok(
            status="success",
            detail=out3 or "Update applied.",
            restart_required=True,
        )
    except Exception as exc:
        logging.exception("run_updater failed")
        logging.exception("update failed"); return _err("Update failed — see server logs", code=500)


@admin_tools_bp.route("/logs_tail")
@require_server
def logs_tail():
    """
    Stream the last N lines of a named log file.
    Query params: file=<filename>, lines=<int>
    """
    logs_dir = Path(current_app.root_path) / "logs"
    filename = (request.args.get("file") or "").strip()
    try:
        n = int(request.args.get("lines", 200))
    except ValueError:
        n = 200

    if not filename:
        return _err("file param required.")

    import html as _html
    valid_paths = {str(p.relative_to(logs_dir)): p for p in logs_dir.glob("**/*") if p.is_file()}
    if filename not in valid_paths:
        return _err(f"Log file not found: {_html.escape(str(filename))}", code=404)
    file_path = valid_paths[filename]  # filesystem-sourced Path

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = "".join(lines[-n:])
        return Response(tail, mimetype="text/plain")
    except Exception as exc:
        logging.exception(f"logs_tail failed for {filename}")
        logging.exception("log tail read failed"); return _err("Failed to read log — see server logs", code=500)


@admin_tools_bp.route("/update_municipality", methods=["POST"])

def update_municipality():
    """Quick-fix endpoint to correct the municipality/city field on an airport record."""
    data = request.get_json(silent=True) or {}
    icao = (data.get("icao") or "").strip().upper()
    city = (data.get("city") or "").strip()
    if not icao or not city:
        return _err("ICAO and city are required.")
    try:
        with db.engine.begin() as conn:
            result = conn.execute(
                text("UPDATE airports SET municipality = :city WHERE icao_code = :icao"),
                {"city": city, "icao": icao},
            )
        if result.rowcount == 0:
            return _err(f"No airport found with ICAO '{icao}'.", code=404)
        return _ok(status="ok", icao=icao, city=city)
    except Exception as e:
        logging.exception("❌ update_municipality failed")
        logging.exception("DB error in admin tools"); return _err("Database error — see server logs")

