# AirTrack 1.0.0
# Copyright (c) 2025 Trevor ("Subhuti"). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary-NC

# routes/manifest_routes.py
#
# Exposes /api/registry-manifest — a live JSON snapshot of every country
# table in the airtrack DB with its current record count.
#
# The website's data-downloads page fetches this endpoint so it never needs
# manual updates when a new country registry is imported.
#
# Cache TTL: 1 hour (module-level dict — resets on container restart).

import re
import time
import logging

from flask import Blueprint, jsonify, request
from sqlalchemy import text
from extensions import db

manifest_bp = Blueprint("manifest", __name__, url_prefix="/api")

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tables that are NOT country registries — skip these in the manifest.
# Any table whose name starts with '_' (staging/temp) is also skipped.
# ---------------------------------------------------------------------------

_SYSTEM_TABLES = {
    "aircraft",
    "aircraft_images",
    "aircraft_manual_registry",
    "aircraft_owners",
    "airlines",
    "airports",
    "app_settings",
    "audit_country_updates",
    "customers",
    "flights",
    "license_activity",
    "licenses",
    "migrations",
    "prefixes",
    "registration_prefixes",
    "registry_quota",
    "settings",
}

# ---------------------------------------------------------------------------
# Simple in-process cache — avoids COUNT(*) hammering on every page load.
# ---------------------------------------------------------------------------

_CACHE: dict = {"data": None, "ts": 0.0}
_CACHE_TTL   = 3600  # seconds


def _build_manifest() -> dict:
    """Query every country table for its exact record count and return as dict."""
    result = {}

    rows = db.session.execute(
        text(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
        )
    ).fetchall()

    for (table_name,) in rows:
        if table_name.startswith("_") or table_name in _SYSTEM_TABLES:
            continue

        try:
            count_row = db.session.execute(
                text(f"SELECT COUNT(*) FROM `{table_name}`")
            ).fetchone()
            result[table_name] = count_row[0] if count_row else 0
        except Exception as exc:
            log.warning(f"manifest_routes: could not count {table_name}: {exc}")
            result[table_name] = 0

    return result


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@manifest_bp.route("/registry-manifest", methods=["GET"])
def registry_manifest():
    """
    Returns a JSON object mapping table_name -> record_count for every
    country registry table in the airtrack DB.

    Example response:
        {
            "australia": 16609,
            "canada": 34911,
            "france": 40486,
            ...
        }

    Result is cached for 1 hour. Pass ?refresh=1 to force a rebuild.
    """
    force = bool(int(request.args.get("refresh", 0)))
    now   = time.time()

    if force or _CACHE["data"] is None or (now - _CACHE["ts"]) > _CACHE_TTL:
        try:
            _CACHE["data"] = _build_manifest()
            _CACHE["ts"]   = now
            log.info(f"manifest_routes: rebuilt — {len(_CACHE['data'])} tables")
        except Exception as exc:
            log.error(f"manifest_routes: build failed: {exc}")
            if _CACHE["data"] is None:
                return jsonify({"error": "manifest unavailable"}), 503

    return jsonify(_CACHE["data"])


# ---------------------------------------------------------------------------
# Wombat manifest endpoint — consumed by Mangy Marmot clients
# Serves the JSON file written by waddling_wombat.py
# ---------------------------------------------------------------------------

from pathlib import Path as _Path
import json as _json
import gzip as _gzip
import base64 as _base64
import hashlib as _hashlib
import time as _time
from datetime import date as _date

_WOMBAT_MANIFEST = _Path(__file__).resolve().parent.parent / "woodland" / "wombat" / "manifest.json"
_QUOTA_DIR       = _Path(__file__).resolve().parent.parent / "woodland" / "wombat" / "quota"
_REPORTS_DIR     = _Path(__file__).resolve().parent.parent / "woodland" / "wombat" / "reports"
_MAX_DAILY_DL    = 5


# ---------------------------------------------------------------------------
# Quota helpers (per license key, per calendar day, file-backed)
# ---------------------------------------------------------------------------

def _quota_file(license_key: str) -> _Path:
    kh = _hashlib.sha256(license_key.encode()).hexdigest()[:16]
    return _QUOTA_DIR / f"{_date.today().isoformat()}_{kh}.json"


def _get_quota_used(license_key: str) -> int:
    qf = _quota_file(license_key)
    if not qf.exists():
        return 0
    try:
        return _json.loads(qf.read_text()).get("count", 0)
    except Exception:
        return 0


def _record_quota_use(license_key: str, count: int) -> int:
    _QUOTA_DIR.mkdir(parents=True, exist_ok=True)
    new_count = _get_quota_used(license_key) + count
    _quota_file(license_key).write_text(
        _json.dumps({"count": new_count, "date": _date.today().isoformat()})
    )
    return new_count


# ---------------------------------------------------------------------------
# Registry SQL packager
# ---------------------------------------------------------------------------

_SAFE_IDENT_RE = re.compile(r'^[a-z][a-z0-9_]*$')


def _safe_ident(name: str) -> str:
    """Sanitise a string for safe use as a backtick-quoted SQL identifier."""
    clean = re.sub(r'[^a-z0-9_]', '',
                   (name or '').strip().lower().replace(' ', '_').replace('-', '_'))
    if not clean or not _SAFE_IDENT_RE.match(clean):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return clean


def _package_registry_sql(table_name: str) -> dict:
    """
    Dump a registry table as gzipped SQL (DROP + CREATE + batched INSERTs).
    Returns {"sha256": str, "record_count": int, "sql_gz_b64": str}.
    """
    import os as _os
    import pymysql as _pymysql

    db_cfg = dict(
        host     = _os.getenv("DB_HOST", "127.0.0.1"),
        port     = int(_os.getenv("DB_PORT", "3306")),
        user     = _os.getenv("DB_USER"),
        password = _os.getenv("DB_PASSWORD", _os.getenv("DB_PASS", "")),
        database = _os.getenv("DB_NAME", "airtrack"),
        charset  = "utf8mb4",
    )

    conn = _pymysql.connect(**db_cfg, cursorclass=_pymysql.cursors.Cursor)
    try:
        with conn.cursor() as cur:
            # Validate table_name against information_schema — get DB-sourced name
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (table_name,)
            )
            _trow = cur.fetchone()
            if not _trow:
                raise ValueError(f"Unknown table: {table_name!r}")
            safe_table = _trow[0]  # DB-sourced name — not derived from request
            cur.execute(f"SHOW CREATE TABLE `{safe_table}`")
            schema_row = cur.fetchone()
            create_sql = schema_row[1] if schema_row else ""

            cur.execute(f"SELECT * FROM `{safe_table}` LIMIT 0")
            columns  = [d[0] for d in cur.description]
            col_list = ", ".join(f"`{c}`" for c in columns)

            cur.execute(f"SELECT COUNT(*) FROM `{safe_table}`")
            count = cur.fetchone()[0]

            lines = [
                "-- Wombat Registry Export",
                f"-- Registry: {safe_table}",
                f"-- Records: {count}",
                "",
                f"DROP TABLE IF EXISTS `{safe_table}`;",
                create_sql + ";",
                "",
            ]

            cur.execute(f"SELECT * FROM `{safe_table}`")
            BATCH = 500
            while True:
                rows = cur.fetchmany(BATCH)
                if not rows:
                    break
                vgroups = []
                for row in rows:
                    vals = []
                    for v in row:
                        if v is None:
                            vals.append("NULL")
                        elif isinstance(v, (int, float)):
                            vals.append(str(v))
                        elif isinstance(v, (bytes, bytearray)):
                            vals.append(f"X'{v.hex()}'")
                        else:
                            vals.append(f"'{conn.escape_string(str(v))}'")
                    vgroups.append(f"({','.join(vals)})")
                lines.append(f"INSERT INTO `{safe_table}` ({col_list}) VALUES")
                lines.append(",\n".join(vgroups) + ";")
                lines.append("")
    finally:
        conn.close()

    sql_bytes = "\n".join(lines).encode("utf-8")
    gz_data   = _gzip.compress(sql_bytes, compresslevel=6)
    sha256    = _hashlib.sha256(gz_data).hexdigest()
    b64       = _base64.b64encode(gz_data).decode("ascii")

    return {"sha256": sha256, "record_count": count, "sql_gz_b64": b64}


def _check_license() -> bool:
    """
    Validate the X-AirTrack-License header against airtrack_admin.licenses.
    Returns True if the activation_key is present, status='active', and not expired.
    No details are returned to the caller on failure — bye bye.
    """
    key = request.headers.get("X-AirTrack-License", "").strip()
    if not key:
        return False
    try:
        row = db.session.execute(
            text(
                "SELECT id FROM airtrack_admin.licenses "
                "WHERE activation_key = :key "
                "AND status = 'active' "
                "AND (expires_at IS NULL OR expires_at > NOW()) "
                "LIMIT 1"
            ),
            {"key": key},
        ).fetchone()
        return row is not None
    except Exception as exc:
        log.error(f"wombat license check failed: {exc}")
        return False


@manifest_bp.route("/wombat/manifest", methods=["GET"])
def wombat_manifest():
    """
    Serves the Waddling Wombat manifest JSON for Mangy Marmot clients.
    Written by woodland/waddling_wombat.py on its scheduled run.
    Requires a valid X-AirTrack-License header. No valid key — no entry.
    Returns 503 if the manifest file doesn't exist yet.
    """
    if not _check_license():
        log.warning(f"wombat_manifest: rejected unlicensed request from {request.remote_addr}")
        return "", 403

    if not _WOMBAT_MANIFEST.exists():
        return jsonify({"error": "Wombat manifest not yet generated"}), 503
    try:
        data = _json.loads(_WOMBAT_MANIFEST.read_text(encoding="utf-8"))
        return jsonify(data)
    except Exception as exc:
        log.error(f"wombat_manifest: could not read manifest: {exc}")
        return jsonify({"error": "manifest read error"}), 503


# ---------------------------------------------------------------------------
# Embargo status — checked by Marmot before any patch/install activity
# ---------------------------------------------------------------------------

@manifest_bp.route("/wombat/embargo", methods=["GET"])
def wombat_embargo():
    """Returns current embargo status. Phase 3 will add cryptographic signing."""
    return jsonify({"embargo_active": False, "reason": None})


# ---------------------------------------------------------------------------
# Outcome reports from Marmot — stored for Badger to analyse
# ---------------------------------------------------------------------------

@manifest_bp.route("/wombat/report", methods=["POST"])
def wombat_report():
    """
    Receives install/patch outcome reports from Marmot clients.
    Requires a valid license key. Stored in woodland/wombat/reports/ for Badger.
    """
    if not _check_license():
        log.warning(f"wombat_report: rejected unlicensed request from {request.remote_addr}")
        return "", 403

    try:
        data = request.get_json(silent=True) or {}
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        import uuid as _uuid
        fname = f"{int(_time.time())}_{_uuid.uuid4().hex[:12]}.json"  # not derived from request
        data["reported_from"] = request.remote_addr
        data["received_at"]   = _date.today().isoformat()
        report_path = _REPORTS_DIR / fname  # filesystem-generated name
        report_path.write_text(_json.dumps(data, indent=2))
        log.info(f"wombat_report: stored {fname} — registry={data.get('registry')} outcome={data.get('outcome')}")
        return jsonify({"ok": True})
    except Exception as exc:
        log.error(f"wombat_report: {exc}")
        return jsonify({"ok": False}), 500


# ---------------------------------------------------------------------------
# Registry request — core of Phase 2 delivery
# ---------------------------------------------------------------------------

@manifest_bp.route("/wombat/registry-request", methods=["POST"])
def wombat_registry_request():
    """
    Packages and delivers requested registry tables to authenticated Marmot clients.

    Request body (JSON):
        {"registries": ["australia", "france", ...]}

    Response (JSON):
        {
            "registries": {
                "australia": {"sha256": "...", "record_count": N, "sql_gz_b64": "..."},
                ...
            },
            "quota_used": N,
            "quota_remaining": N,
            "quota_limit": 5,
            "failed": []
        }

    Quota: 5 registry downloads per license key per calendar day.
    """
    if not _check_license():
        log.warning(f"registry_request: rejected unlicensed from {request.remote_addr}")
        return "", 403

    key      = request.headers.get("X-AirTrack-License", "").strip()
    body     = request.get_json(silent=True) or {}
    requested = [str(r).lower().strip() for r in body.get("registries", []) if r]

    if not requested:
        return jsonify({"error": "no_registries_requested"}), 400

    if len(requested) > _MAX_DAILY_DL:
        return jsonify({"error": "too_many_requested", "max": _MAX_DAILY_DL}), 400

    used      = _get_quota_used(key)
    remaining = _MAX_DAILY_DL - used

    if len(requested) > remaining:
        return jsonify({
            "error":           "daily_quota_exceeded",
            "quota_used":      used,
            "quota_remaining": remaining,
            "quota_limit":     _MAX_DAILY_DL,
        }), 429

    # Verify all requested tables are known registry tables
    try:
        rows = db.session.execute(
            text(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE()"
            )
        ).fetchall()
        all_tables   = {r[0].lower() for r in rows}
        system_lower = {t.lower() for t in _SYSTEM_TABLES}
        valid_tables = all_tables - system_lower
    except Exception as exc:
        log.error(f"registry_request: table lookup failed: {exc}")
        return jsonify({"error": "server_error"}), 503

    unknown = [r for r in requested if r not in valid_tables]
    if unknown:
        return jsonify({"error": "unknown_registries", "unknown": unknown}), 400

    # Package each registry
    packaged = {}
    failed   = []
    for table in requested:
        try:
            pkg = _package_registry_sql(table)
            packaged[table] = pkg
            log.info(
                f"registry_request: packaged {table} "
                f"({pkg['record_count']:,} records, {len(pkg['sql_gz_b64'])} b64 chars)"
            )
        except Exception as exc:
            log.error(f"registry_request: failed to package {table}: {exc}")
            failed.append(table)

    if not packaged:
        log.error(f"registry_request: all {len(requested)} registries failed to package")
        return jsonify({"error": "packaging_failed", "failed": failed}), 503

    # Record quota — only charge for what was successfully packaged
    new_used = _record_quota_use(key, len(packaged))
    quota_remaining = _MAX_DAILY_DL - new_used

    log.info(
        f"registry_request: delivered {len(packaged)} registries to {request.remote_addr} "
        f"— quota now {new_used}/{_MAX_DAILY_DL}"
    )

    return jsonify({
        "registries":      packaged,
        "quota_used":      new_used,
        "quota_remaining": quota_remaining,
        "quota_limit":     _MAX_DAILY_DL,
        "failed":          failed,
    })
