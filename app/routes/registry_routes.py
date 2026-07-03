# AirTrack 1.0.0
# Copyright (c) 2025 Trevor ("Subhuti"). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary-NC

import os
import re
import subprocess
import logging
import shutil

from datetime import date
from pathlib import Path
from flask import Blueprint, render_template, jsonify, request, current_app
from sqlalchemy import text
from extensions import db
from routes.country_importer import ensure_country_table

registry_bp = Blueprint('registry', __name__, url_prefix='/admin/registries')

# Top-level dirs to skip — not country registries
_SKIP_DIRS = {
    'zoo of registers', 'packs', 'failed', 'rejected',
    'processed', 'logs', 'inbox', 'holding',
}

DAILY_IMPORT_LIMIT = 5

_SAFE_IDENT_RE = re.compile(r'^[a-z][a-z0-9_]*$')


def _safe_ident(name: str) -> str:
    """Sanitise a string for safe use as a backtick-quoted SQL identifier.

    Only lowercase letters, digits, and underscores are permitted.
    Raises ValueError if the result would be empty or invalid.
    """
    clean = re.sub(r'[^a-z0-9_]', '',
                   (name or '').strip().lower().replace(' ', '_').replace('-', '_'))
    if not clean or not _SAFE_IDENT_RE.match(clean):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return clean


# ── Helpers ──────────────────────────────────────────────────────────────────

def _registries_dir() -> Path:
    return Path(current_app.root_path) / 'registries'


def _find_sql_file(country_dir: Path) -> Path | None:
    """Return the canonical .sql file directly inside a country directory."""
    try:
        sql_files = [
            f for f in country_dir.iterdir()
            if f.is_file() and f.suffix.lower() == '.sql'
        ]
    except PermissionError:
        return None

    if not sql_files:
        return None

    # Prefer a file whose stem matches the directory name (case-insensitive)
    dirname_lower = country_dir.name.lower().replace(' ', '_')
    for f in sql_files:
        if f.stem.lower() == dirname_lower:
            return f

    # Accept any single .sql file
    return sql_files[0]


def _extract_table_name(sql_file: Path) -> str | None:
    """Read the table name from the first INSERT statement in a SQL file."""
    try:
        with sql_file.open('r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                m = re.search(r'INSERT(?:\s+IGNORE)?\s+INTO\s+(?:`([^`]+)`|([a-z_]+))\s*\(', line)
                if m:
                    return m.group(1) or m.group(2)
    except Exception:
        pass
    return None


def _record_count_from_comment(sql_file: Path) -> int:
    """Try to read the record count from the header comment (-- Records: N)."""
    try:
        with sql_file.open('r', encoding='utf-8', errors='replace') as fh:
            for i, line in enumerate(fh):
                if i >= 10:
                    break
                m = re.search(r'Records:\s*([\d,]+)', line)
                if m:
                    return int(m.group(1).replace(',', ''))
    except Exception:
        pass
    return 0


def _table_exists(table_name: str) -> bool:
    try:
        n = db.session.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :n"
            ),
            {"n": table_name},
        ).scalar()
        return bool(n)
    except Exception:
        return False


def _row_count(table_name: str) -> int:
    try:
        return db.session.execute(
            text(f"SELECT COUNT(*) FROM `{_safe_ident(table_name)}`")
        ).scalar() or 0
    except Exception:
        return 0


# ── Daily import quota ────────────────────────────────────────────────────────

def _ensure_quota_table():
    """Create the quota tracking table if it doesn't exist."""
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS `registry_quota` (
            `quota_date`   DATE NOT NULL,
            `imports_done` INT  NOT NULL DEFAULT 0,
            PRIMARY KEY (`quota_date`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))
    db.session.commit()


def _imports_today() -> int:
    """Return how many successful imports have been recorded today."""
    try:
        _ensure_quota_table()
        result = db.session.execute(
            text("SELECT imports_done FROM registry_quota WHERE quota_date = CURDATE()")
        ).scalar()
        return int(result or 0)
    except Exception:
        return 0


def _record_import():
    """Increment today's import counter (upsert)."""
    try:
        db.session.execute(text("""
            INSERT INTO registry_quota (quota_date, imports_done)
            VALUES (CURDATE(), 1)
            ON DUPLICATE KEY UPDATE imports_done = imports_done + 1
        """))
        db.session.commit()
    except Exception:
        logging.exception("Failed to record import quota")


def scan_registries() -> list[dict]:
    """
    Walk the registries directory and return one dict per importable registry.
    Only Title_Case directories with a matching .sql file are included.
    """
    reg_dir = _registries_dir()
    results = []
    seen_tables: set[str] = set()

    try:
        entries = sorted(reg_dir.iterdir(), key=lambda p: p.name.lower())
    except Exception:
        return results

    for item in entries:
        if not item.is_dir():
            continue
        if item.name.lower() in _SKIP_DIRS:
            continue
        if not item.name[0].isupper():
            continue  # Skip lowercase duplicates (armenia/, malta/, etc.)

        sql_file = _find_sql_file(item)
        if not sql_file:
            continue

        table_name = _extract_table_name(sql_file)
        if not table_name:
            continue

        if table_name in seen_tables:
            continue
        seen_tables.add(table_name)

        imported = _table_exists(table_name)
        results.append({
            'display_name': item.name.replace('_', ' '),
            'dir':          item.name,
            'table':        table_name,
            'sql_path':     str(sql_file),
            'file_records': _record_count_from_comment(sql_file),
            'imported':     imported,
            'row_count':    _row_count(table_name) if imported else 0,
        })

    return results


# ── Routes ───────────────────────────────────────────────────────────────────

def _server_url() -> str | None:
    """Return the configured server base URL, or None if not set."""
    url = os.getenv('AIRTRACK_SERVER_URL', '').rstrip('/')
    return url or None


def _fetch_manifest(server_url: str) -> list[dict]:
    """Fetch available registries from the server manifest endpoint."""
    import urllib.request, json as _json
    try:
        with urllib.request.urlopen(f'{server_url}/admin/registries/manifest', timeout=10) as resp:
            return _json.loads(resp.read().decode('utf-8'))
    except Exception as exc:
        logging.warning(f'Failed to fetch registry manifest from {server_url}: {exc}')
        return []


@registry_bp.route('/')
def registry_list():
    is_client = os.getenv('AIRTRACK_ROLE', 'server').lower() == 'client'
    server_url = _server_url()

    if is_client:
        # Client mode: Wombat entitlements + Marmot-installed registries
        from woodland.mangy_marmot import (
            WOMBAT_URL as _WOMBAT_URL, CUSTOMER_ID as _CUSTOMER_ID,
            BILLING_URL as _BILLING_URL,
            REGISTRIES_INSTALLED as _REG_INSTALLED,
            _get, _scan_installed_registries,
            _read_pending_registries, _get_registry_pref,
            _read_entitlements_cache,
        )

        wombat_offline = False
        entitled = []
        available_wombat = []   # all registries Wombat offers
        update_window_hours = 24
        try:
            if _WOMBAT_URL and _CUSTOMER_ID:
                wh = _get(f"/api/wombat/manifest/{_CUSTOMER_ID}")
                # Wombat manifest returns deliveries[*].capability (not required_registries)
                entitled = [
                    d["capability"] for d in wh.get("deliveries", [])
                    if d.get("capability")
                ]
            else:
                import logging as _lg
                _lg.warning(f"registry_routes: Wombat offline — WOMBAT_URL={_WOMBAT_URL!r} CUSTOMER_ID={_CUSTOMER_ID!r}")
                wombat_offline = True
        except Exception as _wombat_exc:
            import logging as _lg
            _lg.warning(f"registry_routes: Wombat exception — WOMBAT_URL={_WOMBAT_URL!r} CUSTOMER_ID={_CUSTOMER_ID!r} error={_wombat_exc!r}")
            wombat_offline = True

        # Merge entitlement cache with Wombat deliveries.
        # Marmot writes country-auto-entitled registries (e.g. australia for AU
        # customers) to the cache; Wombat deliveries only covers paid entitlements.
        # We always merge the cache so both sources contribute to the entitled list.
        wombat_cache_at: str | None = None
        _cache = _read_entitlements_cache()
        if wombat_offline:
            # Wombat unreachable — use cache exclusively
            if _cache:
                entitled        = _cache.get("entitled", [])
                wombat_cache_at = _cache.get("cached_at")
        elif _cache:
            # Wombat online — merge cache into deliveries (cache wins on overlap)
            _cache_entitled = set(_cache.get("entitled", []))
            _delivery_set   = set(entitled)
            for _slug in _cache_entitled:
                if _slug not in _delivery_set:
                    entitled.append(_slug)

        # Supplement entitled list with direct Wombat registry entitlements.
        # This is the reinstall-safe source: survives a wiped AIRTRACK_HOME because
        # it reads from Wombat's entitlements.json, not the local cache.
        if not wombat_offline and _WOMBAT_URL and _CUSTOMER_ID:
            try:
                _ent_resp = _get(f"/api/wombat/entitled-registries/{_CUSTOMER_ID}")
                _ent_set  = set(entitled)
                for _reg in _ent_resp.get("registries", []):
                    if _reg and _reg not in _ent_set:
                        entitled.append(_reg)
                        _ent_set.add(_reg)
            except Exception:
                pass  # non-fatal; cache and deliveries already merged above

        # Fetch what Wombat has available (to show marketplace)
        _wombat_registry_meta: dict[str, dict] = {}  # slug -> {version, records}
        # Seed registry meta from cache when offline
        if wombat_offline and wombat_cache_at:
            _offline_cache = _read_entitlements_cache() or {}
            for _cr in _offline_cache.get("available_registries", []):
                if isinstance(_cr, dict) and _cr.get("slug"):
                    _wombat_registry_meta[_cr["slug"]] = _cr
            available_wombat = list(_wombat_registry_meta.keys())
        if not wombat_offline and _WOMBAT_URL:
            try:
                av_resp = _get("/api/wombat/available-registries")
                for r in av_resp.get("registries", []):
                    if isinstance(r, dict):
                        _wombat_registry_meta[r["slug"]] = r
                    else:
                        _wombat_registry_meta[r] = {"slug": r}
                available_wombat = list(_wombat_registry_meta.keys())
                update_window_hours = av_resp.get("update_window_hours", 24)
            except Exception:
                available_wombat = []

        # Free registries = warehouse required_registries (airports_ref etc.)
        # These are auto-delivered to every customer and must never appear in the marketplace.
        free_registries: set[str] = set()
        try:
            wh_manifest  = _get("/api/wombat/manifest")
            free_registries = set(wh_manifest.get("required_registries", []))
        except Exception:
            pass

        # Purchasable = in Wombat, not yet entitled, and not free/auto-delivered
        # Each entry is a dict {slug, records} so the template can show record counts.
        entitled_set = set(entitled)
        purchasable = [
            {"slug": slug, "records": _wombat_registry_meta.get(slug, {}).get("records", 0)}
            for slug in available_wombat
            if slug not in entitled_set and slug not in free_registries
        ]

        # Display name lookup from ICAO_COUNTRIES
        def _n(s):
            import re as _re
            return _re.sub(r"[_\s\-&'.]+", "", s).lower()
        name_map = {_n(d): name for _, name, d in ICAO_COUNTRIES}

        installed_list = _scan_installed_registries()
        installed_map  = {r['name']: r for r in installed_list}
        pending        = set(_read_pending_registries())
        registry_pref  = _get_registry_pref()

        registries = []
        for reg_name in entitled:
            display = name_map.get(_n(reg_name), reg_name.replace('_', ' ').title())
            if reg_name in installed_map:
                table_name = installed_map[reg_name].get('table_name', reg_name)
                registries.append({
                    'display_name': display, 'dir': reg_name,
                    'table': table_name, 'file_records': 0,
                    'imported': True,
                    'state': 'installed',
                    'has_update': reg_name in pending,
                    'row_count': _row_count(table_name),
                })
            elif reg_name in pending:
                registries.append({
                    'display_name': display, 'dir': reg_name,
                    'table': '', 'file_records': 0,
                    'imported': False, 'state': 'pending',
                    'has_update': False,
                    'row_count': 0,
                })
            else:
                registries.append({
                    'display_name': display, 'dir': reg_name,
                    'table': '', 'file_records': 0,
                    'imported': False, 'state': 'available',
                    'has_update': False,
                    'row_count': 0,
                })

        imported_count = sum(1 for r in registries if r['state'] == 'installed')
        pending_count  = sum(1 for r in registries if r['state'] == 'pending')
        total_records  = sum(r['row_count'] for r in registries)

        # Purchase status from billing-api (non-fatal if unreachable)
        purchases_today = 0
        purchase_limit  = 5
        if not wombat_offline and _BILLING_URL and _CUSTOMER_ID:
            try:
                import urllib.request as _ureq, json as _jmod
                _preq = _ureq.Request(
                    f"{_BILLING_URL}/billing/registry-purchase-status/{_CUSTOMER_ID}",
                    headers={"User-Agent": "AirTrack-RegistryRoutes/0.1"},
                )
                with _ureq.urlopen(_preq, timeout=3) as _presp:
                    _pdata = _jmod.loads(_presp.read())
                purchases_today = int(_pdata.get("used_today", 0))
                purchase_limit  = int(_pdata.get("limit", 5))
            except Exception:
                pass

        return render_template(
            'admin_registries.html',
            registries=registries,
            imported_count=imported_count,
            pending_count=pending_count,
            total_count=len(registries),
            total_records=total_records,
            imports_today=0,
            daily_limit=0,
            is_client=True,
            wombat_offline=wombat_offline,
            wombat_cache_at=wombat_cache_at,
            registry_pref=registry_pref,
            server_url=None,
            server_reachable=True,
            purchasable=purchasable,
            purchases_today=purchases_today,
            purchase_limit=purchase_limit,
            billing_url=_BILLING_URL,
            customer_id=_CUSTOMER_ID,
        )

    # Server mode: scan local files as before
    registries = scan_registries()
    imported_count = sum(1 for r in registries if r['imported'])
    total_records  = sum(r['row_count'] for r in registries if r['imported'])
    today_count = _imports_today()
    return render_template(
        'admin_registries.html',
        registries=registries,
        imported_count=imported_count,
        total_count=len(registries),
        total_records=total_records,
        imports_today=today_count,
        daily_limit=DAILY_IMPORT_LIMIT,
        is_client=False,
        server_url=None,
        server_reachable=True,
    )


@registry_bp.route('/import/<country>', methods=['POST'])
def import_registry(country):
    is_client = os.getenv('AIRTRACK_ROLE', 'server').lower() == 'client'

    # ── Daily limit check ────────────────────────────────────────────────────
    today_count = _imports_today()
    if today_count >= DAILY_IMPORT_LIMIT:
        return jsonify({
            'status': 'error',
            'detail': f'Daily limit of {DAILY_IMPORT_LIMIT} imports reached — come back tomorrow!',
            'quota':  {'used': today_count, 'limit': DAILY_IMPORT_LIMIT, 'remaining': 0},
        }), 429

    db_host     = os.getenv('DB_HOST', 'airtrack-db')
    db_user     = os.getenv('DB_USER', 'airtrack')
    db_password = os.getenv('DB_PASSWORD', '')
    db_name     = os.getenv('DB_NAME', 'airtrack')

    if is_client:
        # Client mode: deliver via Marmot/Wombat pipeline
        from woodland.mangy_marmot import (
            _deliver_registry, _remove_from_pending,
            REGISTRIES_INSTALLED as _REG_INSTALLED,
        )
        import json as _json
        try:
            success = _deliver_registry(country)
        except Exception as exc:
            logging.exception(f"Client registry delivery error for {country}")
            return jsonify({'status': 'error', 'detail': str(exc)}), 500

        if not success:
            return jsonify({'status': 'error', 'detail': 'Delivery failed — check Marmot logs'}), 500

        _remove_from_pending(country)
        table_name = country
        try:
            manifest_path = _REG_INSTALLED / country / "installed.json"
            if manifest_path.exists():
                table_name = _json.loads(manifest_path.read_text(encoding="utf-8")).get("table_name", country)
        except Exception:
            pass
        rows = _row_count(table_name)
        return jsonify({'status': 'ok', 'detail': f'Installed {rows:,} records', 'row_count': rows})

    # ── Server mode: import from local SQL file ───────────────────────────────
    reg_dir = _registries_dir()
    country_dir = (reg_dir / country).resolve()
    try:
        country_dir.relative_to(reg_dir.resolve())
    except ValueError:
        return jsonify({'status': 'error', 'detail': 'Invalid country'}), 400

    if not country_dir.is_dir():
        return jsonify({'status': 'error', 'detail': f'Not found: {country}'}), 404

    sql_file = _find_sql_file(country_dir)
    if not sql_file:
        return jsonify({'status': 'error', 'detail': 'No SQL file found'}), 404

    table_name = _extract_table_name(sql_file)
    if not table_name:
        return jsonify({'status': 'error', 'detail': 'Could not determine table name'}), 400

    try:
        ensure_country_table(db, table_name)

        with sql_file.open('r', encoding='utf-8', errors='replace') as fh:
            result = subprocess.run(
                ['mysql', '-h', db_host, f'-u{db_user}', f'-p{db_password}', db_name],
                stdin=fh,
                capture_output=True,
                text=True,
                timeout=600,
            )

        if result.returncode != 0:
            logging.error(f"Registry import failed for {country}: {result.stderr}")
            return jsonify({'status': 'error', 'detail': result.stderr or 'Import failed'}), 500

        rows = _row_count(table_name)
        _record_import()
        new_count = today_count + 1
        remaining = max(0, DAILY_IMPORT_LIMIT - new_count)
        return jsonify({
            'status':    'ok',
            'detail':    f'Imported {rows:,} records',
            'row_count': rows,
            'quota':     {'used': new_count, 'limit': DAILY_IMPORT_LIMIT, 'remaining': remaining},
        })

    except subprocess.TimeoutExpired:
        return jsonify({'status': 'error', 'detail': 'Import timed out (>10 min)'}), 500
    except Exception as e:
        logging.exception(f"Registry import error for {country}")
        return jsonify({'status': 'error', 'detail': str(e)}), 500


@registry_bp.route('/remove/<country>', methods=['POST'])
def remove_registry(country):
    is_client = os.getenv('AIRTRACK_ROLE', 'server').lower() == 'client'

    if is_client:
        from woodland.mangy_marmot import REGISTRIES_INSTALLED as _REG_INSTALLED
        import json as _json
        table_name = country
        try:
            manifest_path = _REG_INSTALLED / country / "installed.json"
            if manifest_path.exists():
                table_name = _json.loads(manifest_path.read_text(encoding="utf-8")).get("table_name", country)
        except Exception:
            pass
        try:
            db.session.execute(text(f"DROP TABLE IF EXISTS `{_safe_ident(table_name)}`"))
            db.session.commit()
        except Exception as e:
            return jsonify({'status': 'error', 'detail': str(e)}), 500
        try:
            installed_dir = _REG_INSTALLED / country
            if installed_dir.exists():
                shutil.rmtree(installed_dir)
        except Exception:
            pass
        return jsonify({'status': 'ok', 'detail': f'Removed {country}'})

    # Server mode
    reg_dir = _registries_dir()
    country_dir = (reg_dir / country).resolve()
    try:
        country_dir.relative_to(reg_dir.resolve())
    except ValueError:
        return jsonify({'status': 'error', 'detail': 'Invalid country'}), 400
    table_name = None
    if country_dir.is_dir():
        sql_file = _find_sql_file(country_dir)
        if sql_file:
            table_name = _extract_table_name(sql_file)
    if not table_name:
        table_name = country.lower().replace(' ', '_')
    try:
        db.session.execute(text(f"DROP TABLE IF EXISTS `{_safe_ident(table_name)}`"))
        db.session.commit()
        return jsonify({'status': 'ok', 'detail': f'Removed {country}'})
    except Exception as e:
        logging.exception(f"Registry remove error for {country}")
        return jsonify({'status': 'error', 'detail': str(e)}), 500


@registry_bp.route('/purchase-checkout', methods=['POST'])
def purchase_checkout():
    """
    Client-only. Calls billing-api to create a Stripe Checkout session for
    registry purchases. Returns {ok, checkout_url} or {error}.
    """
    is_client = os.getenv('AIRTRACK_ROLE', 'server').lower() == 'client'
    if not is_client:
        return jsonify({'status': 'error', 'detail': 'Not available in server mode'}), 403

    data       = request.get_json(silent=True) or {}
    registries = data.get('registries', [])
    if not registries:
        return jsonify({'status': 'error', 'detail': 'No registries selected'}), 400

    from woodland.mangy_marmot import (
        BILLING_URL as _BILLING_URL, CUSTOMER_ID as _CUSTOMER_ID,
    )
    if not _BILLING_URL or not _CUSTOMER_ID:
        return jsonify({'status': 'error', 'detail': 'Billing service not configured'}), 503

    import urllib.request as _ureq, json as _jmod
    try:
        payload = _jmod.dumps({
            'customer_id': _CUSTOMER_ID,
            'registries':  registries,
        }).encode('utf-8')
        req = _ureq.Request(
            f"{_BILLING_URL}/billing/create-registry-checkout",
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'User-Agent':   'AirTrack-RegistryRoutes/0.1',
            },
            method='POST',
        )
        with _ureq.urlopen(req, timeout=15) as resp:
            result = _jmod.loads(resp.read())
        if result.get('ok') and result.get('checkout_url'):
            return jsonify({'status': 'ok', 'checkout_url': result['checkout_url']})
        return jsonify({'status': 'error', 'detail': result.get('error', 'Checkout failed')}), 500
    except Exception as exc:
        logging.exception("Registry purchase checkout error")
        return jsonify({'status': 'error', 'detail': 'Could not reach billing service'}), 503


# ── Registry Tracker ──────────────────────────────────────────────────────────

# Canonical ICAO registration prefix list.
# (prefix, display_name, dir_name)
# dir_name must match the Title_Case folder name used in the registries directory.
ICAO_COUNTRIES = [
    ('A2',        'Botswana',                       'Botswana'),
    ('A3',        'Tonga',                          'Tonga'),
    ('A40',       'Oman',                           'Oman'),
    ('A5',        'Bhutan',                         'Bhutan'),
    ('A6',        'United Arab Emirates',            'United_Arab_Emirates'),
    ('A7',        'Qatar',                          'Qatar'),
    ('A9C',       'Bahrain',                        'Bahrain'),
    ('AP',        'Pakistan',                       'Pakistan'),
    ('B',         'China',                          'China'),
    ('B-H',       'Hong Kong',                      'Hong_Kong'),
    ('B-M',       'Macau',                          'Macau'),
    ('C',         'Canada',                         'Canada'),
    ('C6',        'Bahamas',                        'Bahamas'),
    ('CC',        'Chile',                          'Chile'),
    ('CN',        'Morocco',                        'Morocco'),
    ('CP',        'Bolivia',                        'Bolivia'),
    ('CS',        'Portugal',                       'Portugal'),
    ('CU',        'Cuba',                           'Cuba'),
    ('CX',        'Uruguay',                        'Uruguay'),
    ('D',         'Germany',                        'Germany'),
    ('D2',        'Angola',                         'Angola'),
    ('D4',        'Cape Verde',                     'Cape_Verde'),
    ('D6',        'Comoros',                        'Comoros'),
    ('DQ',        'Fiji',                           'Fiji'),
    ('EC',        'Spain',                          'Spain'),
    ('EI',        'Ireland',                        'Ireland'),
    ('EK',        'Armenia',                        'Armenia'),
    ('EL',        'Liberia',                        'Liberia'),
    ('EP',        'Iran',                           'Iran'),
    ('ER',        'Moldova',                        'Moldova'),
    ('ES',        'Estonia',                        'Estonia'),
    ('ET',        'Ethiopia',                       'Ethiopia'),
    ('EW',        'Belarus',                        'Belarus'),
    ('EX',        'Kyrgyzstan',                     'Kyrgyzstan'),
    ('EY',        'Tajikistan',                     'Tajikistan'),
    ('EZ',        'Turkmenistan',                   'Turkmenistan'),
    ('F',         'France',                         'France'),
    ('G',         'United Kingdom',                 'United_Kingdom'),
    ('HA',        'Hungary',                        'Hungary'),
    ('HB',        'Switzerland',                    'Switzerland'),
    ('HC',        'Ecuador',                        'Ecuador'),
    ('HH',        'Haiti',                          'Haiti'),
    ('HI',        'Dominican Republic',             'Dominican_Republic'),
    ('HK',        'Colombia',                       'Colombia'),
    ('HL',        'South Korea',                    'South_Korea'),
    ('HP',        'Panama',                         'Panama'),
    ('HR',        'Honduras',                       'Honduras'),
    ('HS',        'Thailand',                       'Thailand'),
    ('HV',        'Vatican City',                   'Vatican_City'),
    ('HZ',        'Saudi Arabia',                   'Saudi_Arabia'),
    ('I',         'Italy',                          'Italy'),
    ('J2',        'Djibouti',                       'Djibouti'),
    ('J3',        'Grenada',                        'Grenada'),
    ('J5',        'Guinea-Bissau',                  'Guinea-Bissau'),
    ('J6',        'Saint Lucia',                    'Saint_Lucia'),
    ('J7',        'Dominica',                       'Dominica'),
    ('J8',        'Saint Vincent and the Grenadines', 'Saint_Vincent_And_The_Grenadines'),
    ('JA',        'Japan',                          'Japan'),
    ('JY',        'Jordan',                         'Jordan'),
    ('LN',        'Norway',                         'Norway'),
    ('LV',        'Argentina',                      'Argentina'),
    ('LX',        'Luxembourg',                     'Luxembourg'),
    ('LY',        'Lithuania',                      'Lithuania'),
    ('LZ',        'Bulgaria',                       'Bulgaria'),
    ('M-',        'Isle of Man',                    'Isleofman'),
    ('N',         'United States',                  'United_States'),
    ('OB',        'Peru',                           'Peru'),
    ('OD',        'Lebanon',                        'Lebanon'),
    ('OE',        'Austria',                        'Austria'),
    ('OH',        'Finland',                        'Finland'),
    ('OK',        'Czech Republic',                 'Czech_Republic'),
    ('OM',        'Slovakia',                       'Slovakia'),
    ('OO',        'Belgium',                        'Belgium'),
    ('OY',        'Denmark',                        'Denmark'),
    ('P2',        'Papua New Guinea',               'Papua_New_Guinea'),
    ('P4',        'Aruba',                          'Aruba'),
    ('PH',        'Netherlands',                    'Netherlands'),
    ('PJ',        'Curaçao',                        'Curacao'),
    ('PK',        'Indonesia',                      'Indonesia'),
    ('PP',        'Brazil',                         'Brazil'),
    ('PZ',        'Suriname',                       'Suriname'),
    ('RA',        'Russia',                         'Russia'),
    ('RP',        'Philippines',                    'Philippines'),
    ('S2',        'Bangladesh',                     'Bangladesh'),
    ('S5',        'Slovenia',                       'Slovenia'),
    ('S7',        'Seychelles',                     'Seychelles'),
    ('S9',        'São Tomé and Príncipe',           'Sao_Tome_And_Principe'),
    ('SE',        'Sweden',                         'Sweden'),
    ('SP',        'Poland',                         'Poland'),
    ('ST',        'Sudan',                          'Sudan'),
    ('SU',        'Egypt',                          'Egypt'),
    ('SX',        'Greece',                         'Greece'),
    ('T2',        'Tuvalu',                         'Tuvalu'),
    ('T3',        'Kiribati',                       'Kiribati'),
    ('T7',        'San Marino',                     'San_Marino'),
    ('T9',        'Bosnia and Herzegovina',          'Bosnia_And_Herzegovina'),
    ('TC',        'Turkey',                         'Turkey'),
    ('TF',        'Iceland',                        'Iceland'),
    ('TG',        'Guatemala',                      'Guatemala'),
    ('TI',        'Costa Rica',                     'Costa_Rica'),
    ('TJ',        'Cameroon',                       'Cameroon'),
    ('TL',        'Central African Republic',        'Central_African_Republic'),
    ('TN',        'Republic of Congo',              'Republic_Of_Congo'),
    ('TR',        'Gabon',                          'Gabon'),
    ('TS',        'Tunisia',                        'Tunisia'),
    ('TT',        'Chad',                           'Chad'),
    ('TU',        "Côte d'Ivoire",                  'Cote_DIvoire'),
    ('TY',        'Benin',                          'Benin'),
    ('TZ',        'Mali',                           'Mali'),
    ('UK',        'Uzbekistan',                     'Uzbekistan'),
    ('UN',        'Kazakhstan',                     'Kazakhstan'),
    ('UR',        'Ukraine',                        'Ukraine'),
    ('V2',        'Antigua and Barbuda',             'Antigua_And_Barbuda'),
    ('V3',        'Belize',                         'Belize'),
    ('V4',        'Saint Kitts and Nevis',           'Saint_Kitts_And_Nevis'),
    ('V5',        'Namibia',                        'Namibia'),
    ('V6',        'Micronesia',                     'Micronesia'),
    ('V7',        'Marshall Islands',               'Marshall_Islands'),
    ('V8',        'Brunei',                         'Brunei'),
    ('VH',        'Australia',                      'Australia'),
    ('VN',        'Vietnam',                        'Vietnam'),
    ('VP-A',      'Anguilla',                       'Anguilla'),
    ('VP-B',      'Bermuda',                        'Bermuda'),
    ('VP-C',      'Cayman Islands',                 'Cayman_Islands'),
    ('VP-F',      'Falkland Islands',               'Falkland_Islands'),
    ('VP-G',      'Gibraltar',                      'Gibraltar'),
    ('VP-L',      'British Virgin Islands',          'British_Virgin_Islands'),
    ('VP-M',      'Montserrat',                     'Montserrat'),
    ('VQ-H',      'Saint Helena',                   'Saint_Helena'),
    ('VQ-T',      'Turks and Caicos Islands',        'Turks_And_Caicos_Islands'),
    ('VT',        'India',                          'India'),
    ('XA',        'Mexico',                         'Mexico'),
    ('XT',        'Burkina Faso',                   'Burkina_Faso'),
    ('XU',        'Cambodia',                       'Cambodia'),
    ('XY',        'Myanmar',                        'Myanmar'),
    ('YA',        'Afghanistan',                    'Afghanistan'),
    ('YI',        'Iraq',                           'Iraq'),
    ('YJ',        'Vanuatu',                        'Vanuatu'),
    ('YK',        'Syria',                          'Syria'),
    ('YL',        'Latvia',                         'Latvia'),
    ('YN',        'Nicaragua',                      'Nicaragua'),
    ('YR',        'Romania',                        'Romania'),
    ('YS',        'El Salvador',                    'El_Salvador'),
    ('YU',        'Serbia',                         'Serbia'),
    ('YV',        'Venezuela',                      'Venezuela'),
    ('Z',         'Zimbabwe',                       'Zimbabwe'),
    ('Z3',        'North Macedonia',                'North_Macedonia'),
    ('ZA',        'Albania',                        'Albania'),
    ('ZK',        'New Zealand',                    'New_Zealand'),
    ('ZP',        'Paraguay',                       'Paraguay'),
    ('ZS',        'South Africa',                   'South_Africa'),
    ('2-',        'Guernsey',                       'Guernsey'),
    ('3A',        'Monaco',                         'Monaco'),
    ('3B',        'Mauritius',                      'Mauritius'),
    ('3C',        'Equatorial Guinea',              'Equatorial_Guinea'),
    ('3D',        'Eswatini',                       'Eswatini'),
    ('3X',        'Guinea',                         'Guinea'),
    ('4K',        'Azerbaijan',                     'Azerbaijan'),
    ('4L',        'Georgia',                        'Georgia'),
    ('4O',        'Montenegro',                     'Montenegro'),
    ('4R',        'Sri Lanka',                      'Sri_Lanka'),
    ('4W',        'Timor-Leste',                    'Timor_Leste'),
    ('4X',        'Israel',                         'Israel'),
    ('5A',        'Libya',                          'Libya'),
    ('5B',        'Cyprus',                         'Cyprus'),
    ('5H',        'Tanzania',                       'Tanzania'),
    ('5N',        'Nigeria',                        'Nigeria'),
    ('5R',        'Madagascar',                     'Madagascar'),
    ('5T',        'Mauritania',                     'Mauritania'),
    ('5U',        'Niger',                          'Niger'),
    ('5V',        'Togo',                           'Togo'),
    ('5W',        'Samoa',                          'Samoa'),
    ('5X',        'Uganda',                         'Uganda'),
    ('5Y',        'Kenya',                          'Kenya'),
    ('6O',        'Somalia',                        'Somalia'),
    ('6V',        'Senegal',                        'Senegal'),
    ('6Y',        'Jamaica',                        'Jamaica'),
    ('7O',        'Yemen',                          'Yemen'),
    ('7P',        'Lesotho',                        'Lesotho'),
    ('7Q',        'Malawi',                         'Malawi'),
    ('7T',        'Algeria',                        'Algeria'),
    ('8P',        'Barbados',                       'Barbados'),
    ('8Q',        'Maldives',                       'Maldives'),
    ('8R',        'Guyana',                         'Guyana'),
    ('9A',        'Croatia',                        'Croatia'),
    ('9G',        'Ghana',                          'Ghana'),
    ('9H',        'Malta',                          'Malta'),
    ('9J',        'Zambia',                         'Zambia'),
    ('9K',        'Kuwait',                         'Kuwait'),
    ('9L',        'Sierra Leone',                   'Sierra_Leone'),
    ('9M',        'Malaysia',                       'Malaysia'),
    ('9N',        'Nepal',                          'Nepal'),
    ('9Q',        'DR Congo',                       'DR_Congo'),
    ('9U',        'Burundi',                        'Burundi'),
    ('9V',        'Singapore',                      'Singapore'),
    ('9XR',       'Rwanda',                         'Rwanda'),
    ('9Y',        'Trinidad and Tobago',             'Trinidad_And_Tobago'),
]


def _packs_countries() -> set[str]:
    """
    Return a set of normalised country names found in the packs folder.
    Normalise = lowercase, remove underscores, spaces, hyphens, ampersands, apostrophes.
    Picks up any file with a recognised data extension.
    """
    packs_dir = _registries_dir() / 'packs'
    found: set[str] = set()
    if not packs_dir.is_dir():
        return found
    DATA_EXTS = {'.xlsx', '.pdf', '.csv', '.sql', '.txt', '.xls', '.ods'}
    for f in packs_dir.iterdir():
        if f.is_file() and f.suffix.lower() in DATA_EXTS:
            stem = f.stem.replace('_', '').replace(' ', '').replace('-', '').replace('&', '').replace("'", '').lower()
            found.add(stem)
    return found


def _norm(s: str) -> str:
    """Normalise a country name or dir name for fuzzy matching."""
    return s.replace('_', '').replace(' ', '').replace('-', '').replace('&', '').replace("'", '').replace('.', '').lower()


@registry_bp.route('/manifest')
def registry_manifest():
    """Return JSON list of available registries — consumed by client installs."""
    registries = scan_registries()
    return jsonify([
        {
            'name':         r['display_name'],
            'dir':          r['dir'],
            'table':        r['table'],
            'file_records': r['file_records'],
        }
        for r in registries
    ])


@registry_bp.route('/sql/<country>')
def registry_sql(country):
    """Stream the SQL file for a registry — consumed by client installs."""
    from flask import send_file, abort
    reg_dir = _registries_dir()
    # Sanitise: only allow Title_Case dir names that exist, and must be within reg_dir
    country_dir = (reg_dir / country).resolve()
    try:
        country_dir.relative_to(reg_dir.resolve())
    except ValueError:
        abort(404)
    if not country_dir.is_dir() or country.lower() in _SKIP_DIRS or not country[0].isupper():
        abort(404)
    sql_file = _find_sql_file(country_dir)
    if not sql_file:
        abort(404)
    return send_file(str(sql_file), mimetype='application/sql', as_attachment=True,
                     download_name=sql_file.name)


@registry_bp.route('/tracker')
def registry_tracker():
    """Full ICAO country registry progress tracker."""
    reg_dir = _registries_dir()
    packs_names = _packs_countries()

    # Build a map of dir_name (normalised) → (has_sql, is_imported, row_count, table_name)
    sql_ready: dict[str, tuple[bool, int, str]] = {}
    try:
        for item in reg_dir.iterdir():
            if not item.is_dir():
                continue
            if item.name.lower() in _SKIP_DIRS:
                continue
            if not item.name[0].isupper():
                continue
            sql_file = _find_sql_file(item)
            if not sql_file:
                continue
            table_name = _extract_table_name(sql_file)
            if not table_name:
                continue
            imported = _table_exists(table_name)
            rows = _row_count(table_name) if imported else _record_count_from_comment(sql_file)
            sql_ready[_norm(item.name)] = (imported, rows, table_name)
    except Exception:
        pass

    countries = []
    counts = {'imported': 0, 'sql_ready': 0, 'in_packs': 0, 'missing': 0}

    for prefix, display_name, dir_name in ICAO_COUNTRIES:
        key = _norm(dir_name)
        name_key = _norm(display_name)

        if key in sql_ready:
            imported, rows, table_name = sql_ready[key]
            if imported:
                status = 'imported'
                status_label = '✓ Imported'
            else:
                status = 'sql_ready'
                status_label = 'SQL Ready'
        elif key in packs_names or name_key in packs_names:
            status = 'in_packs'
            status_label = 'In Packs'
            rows = 0
        else:
            status = 'missing'
            status_label = 'Missing'
            rows = 0

        counts[status] += 1
        countries.append({
            'prefix':       prefix,
            'name':         display_name,
            'dir':          dir_name,
            'status':       status,
            'status_label': status_label,
            'rows':         rows,
        })

    total = len(countries)
    return render_template(
        'admin_registry_tracker.html',
        countries=countries,
        counts=counts,
        total=total,
    )
