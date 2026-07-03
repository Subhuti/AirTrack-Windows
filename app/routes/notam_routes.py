# AirTrack 1.0.0
# Copyright (c) 2025 Trevor ("Subhuti"). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary-NC

# routes/notam_routes.py
#
# NOTAM display page and API endpoints.
#
# GET  /notams                    — main display page
# GET  /api/notams/live           — active NOTAMs, JSON
# GET  /api/notams/live.csv       — active NOTAMs, CSV (disclaimer embedded)
# GET  /api/notams/critical       — CRITICAL severity only, JSON
# GET  /api/notams/statistics     — aggregate counts, JSON
# GET  /api/notams/history        — expired/archived, paginated JSON
# GET  /api/notams/<notam_id>     — single record, JSON
# POST /api/notams/import         — manual import (raw NOTAM text)

import csv
import io
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, make_response, render_template, request
from sqlalchemy import text
from extensions import db

log = logging.getLogger(__name__)

# Humanizer — compute at serve time (not stored in DB)
try:
    from modules.notams.humanizer import make_summary, make_detail_text
    _HUMANIZER_AVAILABLE = True
except ImportError:
    _HUMANIZER_AVAILABLE = False
    log.warning("notam_routes: humanizer not available — summaries will be raw text")

notam_bp = Blueprint("notams", __name__, url_prefix="")

DISCLAIMER_STRIP = (
    "AirTrack NOTAM data is for informational purposes only. "
    "Not certified. Not for operational use. Always consult official sources."
)

_SEVERITY_ORDER = {"CRITICAL": 0, "SIGNIFICANT": 1, "MINOR": 2, "INFORMATIONAL": 3}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _table_exists() -> bool:
    try:
        db.session.execute(text("SELECT 1 FROM notams LIMIT 1"))
        return True
    except Exception:
        return False


def _row_to_dict(row) -> dict:
    keys = row._fields if hasattr(row, "_fields") else row.keys()
    d = dict(zip(keys, row))
    # Serialise datetimes
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    # Compute active_now if not already present
    if "active_now" not in d:
        now = datetime.now(timezone.utc)
        eff_from = d.get("effective_from")
        eff_to = d.get("effective_to")
        is_perm = bool(d.get("is_permanent"))
        try:
            if isinstance(eff_from, str):
                from dateutil.parser import parse as dtparse
                eff_from = dtparse(eff_from)
            if isinstance(eff_to, str):
                from dateutil.parser import parse as dtparse
                eff_to = dtparse(eff_to)
            active = True
            if eff_from and now < eff_from.replace(tzinfo=timezone.utc) if eff_from.tzinfo is None else eff_from:
                active = False
            if not is_perm and eff_to:
                eff_to_aware = eff_to.replace(tzinfo=timezone.utc) if eff_to.tzinfo is None else eff_to
                if now > eff_to_aware:
                    active = False
            d["active_now"] = active
        except Exception:
            d["active_now"] = None

    # Attach human-readable fields (computed at serve time, not stored in DB)
    if _HUMANIZER_AVAILABLE:
        try:
            d["summary"]     = make_summary(d)
            d["detail_text"] = make_detail_text(d)
        except Exception as exc:
            log.debug("humanizer failed for %s: %s", d.get("notam_id"), exc)
            d["summary"]     = d.get("text_raw", "")
            d["detail_text"] = d.get("text_raw", "")
    else:
        d["summary"]     = d.get("text_raw", "")
        d["detail_text"] = d.get("text_raw", "")

    return d


# ---------------------------------------------------------------------------
# Display page
# ---------------------------------------------------------------------------

@notam_bp.route("/notams")
def notams_page():
    import os
    from time import time
    from utils.settings_utils import get_current_theme
    raw = os.getenv("NOTAM_PRIMARY_AIRPORTS", "YSSY,YSBK,YSRI,YWLM")
    airports = [x.strip().upper() for x in raw.split(",") if x.strip()]
    return render_template(
        "notams.html",
        selected_theme=get_current_theme(),
        cache_bust=int(time()),
        notam_airports=airports,
    )


# ---------------------------------------------------------------------------
# API — live
# ---------------------------------------------------------------------------

@notam_bp.route("/api/notams/live")
def api_notams_live():
    if not _table_exists():
        return jsonify({"disclaimer": DISCLAIMER_STRIP, "count": 0, "notams": [], "note": "NOTAM table not yet created."})

    icao     = request.args.get("icao", "").upper().strip()
    fir      = request.args.get("fir", "").upper().strip()
    severity = request.args.get("severity", "").upper().strip()
    category = request.args.get("category", "").strip()
    limit    = min(int(request.args.get("limit", 50)), 200)
    offset   = int(request.args.get("offset", 0))

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    where = [
        "status = 'active'",
        "(effective_from <= :now)",
        "(is_permanent = 1 OR effective_to > :now OR effective_to IS NULL)",
    ]
    params = {"now": now_str, "limit": limit, "offset": offset}

    if icao:
        where.append("primary_icao = :icao")
        params["icao"] = icao
    if fir:
        where.append("fir = :fir")
        params["fir"] = fir
    if severity:
        sevs = [s.strip() for s in severity.split(",")]
        placeholders = ", ".join(f":sev{i}" for i, _ in enumerate(sevs))
        where.append(f"severity IN ({placeholders})")
        for i, s in enumerate(sevs):
            params[f"sev{i}"] = s
    if category:
        where.append("category = :category")
        params["category"] = category

    sql = (
        "SELECT * FROM notams WHERE "
        + " AND ".join(where)
        + " ORDER BY FIELD(severity,'CRITICAL','SIGNIFICANT','MINOR','INFORMATIONAL'), effective_from DESC"
        + " LIMIT :limit OFFSET :offset"
    )

    try:
        rows = db.session.execute(text(sql), params).fetchall()
        notams = [_row_to_dict(r) for r in rows]
        return jsonify({"disclaimer": DISCLAIMER_STRIP, "count": len(notams), "notams": notams})
    except Exception as exc:
        log.error(f"notam_routes live: {exc}")
        log.error(f"notam_routes live: {exc}")
        return jsonify({"error": "query failed"}), 500


# ---------------------------------------------------------------------------
# API — live (CSV export)
# ---------------------------------------------------------------------------

#
# CSV_DISCLAIMER_ROWS are written at the top and bottom of every CSV export.
# They survive copy-paste, email attachments, and spreadsheet imports.
# The redistribution notice is mandatory — this data must never be
# presented as certified, official, or operationally authoritative.
#
_CSV_DISCLAIMER_ROWS = [
    ["# -----------------------------------------------------------------------"],
    ["# AIRTRACK NOTAM EXPORT — SAFETY NOTICE"],
    ["# -----------------------------------------------------------------------"],
    ["# This data is for informational and hobbyist purposes ONLY."],
    ["# NOT certified by CASA, Airservices Australia, ICAO, or any aviation authority."],
    ["# NEVER use for flight planning, navigation, or any operational decision."],
    ["# Data may be incomplete, out of date, or incorrect."],
    ["# For operational use: Australia — NAIPS (airservicesaustralia.com)"],
    ["#                      New Zealand — AIP NZ (aip.net.nz)"],
    ["# Redistribution must retain this notice in full."],
    ["# AirTrack Solutions — airtracksolutions.com.au"],
    ["# -----------------------------------------------------------------------"],
]


@notam_bp.route("/api/notams/live.csv")
def api_notams_live_csv():
    """CSV export of active NOTAMs. Disclaimer embedded as comment rows."""
    if not _table_exists():
        return make_response("# NOTAM table not yet created.\n", 404, {"Content-Type": "text/plain"})

    icao     = request.args.get("icao", "").upper().strip()
    fir      = request.args.get("fir", "").upper().strip()
    severity = request.args.get("severity", "").upper().strip()
    category = request.args.get("category", "").strip()
    limit    = min(int(request.args.get("limit", 500)), 2000)
    offset   = int(request.args.get("offset", 0))

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    where = [
        "status = 'active'",
        "(effective_from <= :now)",
        "(is_permanent = 1 OR effective_to > :now OR effective_to IS NULL)",
    ]
    params = {"now": now_str, "limit": limit, "offset": offset}

    if icao:
        where.append("primary_icao = :icao")
        params["icao"] = icao
    if fir:
        where.append("fir = :fir")
        params["fir"] = fir
    if severity:
        sevs = [s.strip() for s in severity.split(",")]
        placeholders = ", ".join(f":sev{i}" for i, _ in enumerate(sevs))
        where.append(f"severity IN ({placeholders})")
        for i, s in enumerate(sevs):
            params[f"sev{i}"] = s
    if category:
        where.append("category = :category")
        params["category"] = category

    sql = (
        "SELECT notam_id, series, fir, q_code, q_subject, q_condition, "
        "primary_icao, severity, category, effective_from, effective_to, "
        "is_permanent, status, parse_confidence, text_raw, source, created_at "
        "FROM notams WHERE "
        + " AND ".join(where)
        + " ORDER BY FIELD(severity,'CRITICAL','SIGNIFICANT','MINOR','INFORMATIONAL'), effective_from DESC"
        + " LIMIT :limit OFFSET :offset"
    )

    try:
        from sqlalchemy import text
        rows = db.session.execute(text(sql), params).fetchall()
    except Exception as exc:
        log.error(f"notam_routes live.csv: {exc}")
        log.error(f"notam_routes live.csv: {exc}")
        return make_response("# query failed\n", 500, {"Content-Type": "text/plain"})

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")

    # Disclaimer header rows (comment lines)
    for row in _CSV_DISCLAIMER_ROWS:
        writer.writerow(row)

    # Export metadata
    writer.writerow([f"# Exported: {now_str} UTC"])
    if icao:
        writer.writerow([f"# Filter: ICAO={icao}"])
    if severity:
        writer.writerow([f"# Filter: severity={severity}"])
    writer.writerow([f"# Records: {len(rows)}"])
    writer.writerow(["# -----------------------------------------------------------------------"])

    # Column headers
    writer.writerow([
        "notam_id", "series", "fir", "q_code", "q_subject", "q_condition",
        "primary_icao", "severity", "category", "effective_from", "effective_to",
        "is_permanent", "status", "parse_confidence", "text_raw", "source", "created_at",
    ])

    # Data rows
    for row in rows:
        writer.writerow(list(row))

    # Disclaimer footer row (survives if header rows are deleted)
    writer.writerow([])
    for dr in _CSV_DISCLAIMER_ROWS:
        writer.writerow(dr)

    filename = f"airtrack_notams_{now_str[:10]}.csv"
    response = make_response(out.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ---------------------------------------------------------------------------
# API — critical only
# ---------------------------------------------------------------------------

@notam_bp.route("/api/notams/critical")
def api_notams_critical():
    if not _table_exists():
        return jsonify({"disclaimer": DISCLAIMER_STRIP, "count": 0, "notams": []})

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        "SELECT * FROM notams WHERE status = 'active' AND severity = 'CRITICAL' "
        "AND effective_from <= :now "
        "AND (is_permanent = 1 OR effective_to > :now OR effective_to IS NULL) "
        "ORDER BY effective_from ASC"
    )
    try:
        rows = db.session.execute(text(sql), {"now": now_str}).fetchall()
        notams = [_row_to_dict(r) for r in rows]
        return jsonify({"disclaimer": DISCLAIMER_STRIP, "count": len(notams), "notams": notams})
    except Exception as exc:
        log.error(f"notam_routes critical: {exc}")
        return jsonify({"error": "query failed"}), 500


# ---------------------------------------------------------------------------
# API — statistics (Goblin's territory)
# ---------------------------------------------------------------------------

@notam_bp.route("/api/notams/statistics")
def api_notams_statistics():
    if not _table_exists():
        return jsonify({"disclaimer": DISCLAIMER_STRIP, "total": 0, "note": "NOTAM table not yet created."})

    days   = min(int(request.args.get("days", 30)), 365)
    icao   = request.args.get("icao", "").upper().strip()
    fir    = request.args.get("fir", "").upper().strip()
    params = {"days": days}

    base_where = "created_at >= DATE_SUB(NOW(), INTERVAL :days DAY)"
    if icao:
        base_where += " AND primary_icao = :icao"
        params["icao"] = icao
    if fir:
        base_where += " AND fir = :fir"
        params["fir"] = fir

    try:
        total = db.session.execute(
            text(f"SELECT COUNT(*) FROM notams WHERE {base_where}"), params
        ).scalar()

        by_severity = {r[0]: r[1] for r in db.session.execute(
            text(f"SELECT severity, COUNT(*) FROM notams WHERE {base_where} GROUP BY severity"), params
        ).fetchall()}

        by_category = {r[0]: r[1] for r in db.session.execute(
            text(f"SELECT category, COUNT(*) FROM notams WHERE {base_where} GROUP BY category ORDER BY 2 DESC"), params
        ).fetchall()}

        by_icao = {r[0]: r[1] for r in db.session.execute(
            text(f"SELECT primary_icao, COUNT(*) FROM notams WHERE {base_where} AND primary_icao IS NOT NULL GROUP BY primary_icao ORDER BY 2 DESC LIMIT 20"), params
        ).fetchall()}

        peak_row = db.session.execute(
            text(f"SELECT DATE(created_at) AS day, COUNT(*) AS cnt FROM notams WHERE {base_where} GROUP BY day ORDER BY cnt DESC LIMIT 1"), params
        ).fetchone()

        return jsonify({
            "disclaimer": DISCLAIMER_STRIP,
            "period_days": days,
            "total": total,
            "by_severity": by_severity,
            "by_category": by_category,
            "by_icao": by_icao,
            "peak_day": str(peak_row[0]) if peak_row else None,
            "avg_per_day": round(total / days, 1) if days else 0,
        })
    except Exception as exc:
        log.error(f"notam_routes statistics: {exc}")
        return jsonify({"error": "query failed"}), 500


# ---------------------------------------------------------------------------
# API — history (archive)
# ---------------------------------------------------------------------------

@notam_bp.route("/api/notams/history")
def api_notams_history():
    if not _table_exists():
        return jsonify({"disclaimer": DISCLAIMER_STRIP, "count": 0, "notams": []})

    icao     = request.args.get("icao", "").upper().strip()
    category = request.args.get("category", "").strip()
    from_dt  = request.args.get("from", "")
    to_dt    = request.args.get("to", "")
    limit    = min(int(request.args.get("limit", 50)), 500)
    offset   = int(request.args.get("offset", 0))

    where = ["status IN ('expired','cancelled','superseded')"]
    params = {"limit": limit, "offset": offset}

    if icao:
        where.append("primary_icao = :icao")
        params["icao"] = icao
    if category:
        where.append("category = :category")
        params["category"] = category
    if from_dt:
        where.append("effective_from >= :from_dt")
        params["from_dt"] = from_dt
    if to_dt:
        where.append("effective_from <= :to_dt")
        params["to_dt"] = to_dt

    sql = (
        "SELECT * FROM notams WHERE " + " AND ".join(where)
        + " ORDER BY effective_from DESC LIMIT :limit OFFSET :offset"
    )

    try:
        rows = db.session.execute(text(sql), params).fetchall()
        notams = [_row_to_dict(r) for r in rows]
        return jsonify({"disclaimer": DISCLAIMER_STRIP, "count": len(notams), "notams": notams})
    except Exception as exc:
        log.error(f"notam_routes history: {exc}")
        return jsonify({"error": "query failed"}), 500


# ---------------------------------------------------------------------------
# API — single record
# ---------------------------------------------------------------------------

@notam_bp.route("/api/notams/<notam_id>")
def api_notam_detail(notam_id):
    if not _table_exists():
        return jsonify({"error": "not found"}), 404

    try:
        row = db.session.execute(
            text("SELECT * FROM notams WHERE notam_id = :nid LIMIT 1"),
            {"nid": notam_id.upper()}
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify({"disclaimer": DISCLAIMER_STRIP, "notam": _row_to_dict(row)})
    except Exception as exc:
        log.error(f"notam_routes detail: {exc}")
        return jsonify({"error": "query failed"}), 500


# ---------------------------------------------------------------------------
# API — manual fetch (triggers Gus)
# ---------------------------------------------------------------------------

@notam_bp.route("/api/notams/fetch", methods=["POST"])
def api_notams_fetch():
    """
    Trigger a live SkyLink fetch immediately.
    Used by the Admin cockpit "Refresh NOTAMs" button.
    Runs synchronously and returns the fetch summary.
    """
    try:
        from modules.notams.skylink_adapter import run_fetch
    except ImportError as exc:
        log.warning(f"notam_routes: SkyLink adapter unavailable: {exc}")
        return jsonify({"error": "SkyLink adapter not available"}), 503

    try:
        result = run_fetch()
        log.info(f"Manual NOTAM fetch triggered: {result}")
        return jsonify({"status": "ok", "result": result})
    except Exception as exc:
        log.error(f"Manual NOTAM fetch failed: {exc}")
        log.error(f"Manual NOTAM fetch failed: {exc}")
        return jsonify({"error": "fetch failed"}), 500


# ---------------------------------------------------------------------------
# API — import
# ---------------------------------------------------------------------------

@notam_bp.route("/api/notams/import", methods=["POST"])
def api_notams_import():
    if not _table_exists():
        return jsonify({"error": "NOTAM table does not exist yet"}), 503

    try:
        from modules.notams.normalizer import normalize_many
    except ImportError:
        return jsonify({"error": "NOTAM module not available"}), 503

    data   = request.get_json(silent=True) or {}
    raw    = data.get("raw", "")
    source = data.get("source", "manual")

    if not raw.strip():
        return jsonify({"error": "No NOTAM text provided"}), 400

    try:
        records = normalize_many(raw, source=source)
    except Exception as exc:
        log.warning(f"notam_routes import parse failed: {exc}")
        return jsonify({"error": "parse failed"}), 400

    imported = 0
    skipped  = 0
    errors   = 0
    low_conf = 0
    notam_ids = []

    for rec in records:
        if rec.get("parse_confidence") == "LOW":
            low_conf += 1

        try:
            cols = [
                "notam_id","series","number","year","fir","q_code","q_subject","q_condition",
                "q_traffic","q_purpose","q_scope","lower_limit_ft","upper_limit_ft",
                "latitude","longitude","radius_nm","location_raw","effective_from","effective_to",
                "is_permanent","schedule_raw","text_raw","lower_limit_raw","upper_limit_raw",
                "category","severity","parse_confidence","status","primary_icao","source",
                "raw_text","checksum",
            ]
            placeholders = ", ".join(f":{c}" for c in cols)
            col_list = ", ".join(cols)
            sql = f"INSERT IGNORE INTO notams ({col_list}) VALUES ({placeholders})"

            params = {c: rec.get(c) for c in cols}
            # Normalise datetimes
            for dtcol in ("effective_from", "effective_to"):
                v = params.get(dtcol)
                if v and not isinstance(v, str):
                    params[dtcol] = str(v)[:19]
                elif v:
                    params[dtcol] = str(v)[:19]

            result = db.session.execute(text(sql), params)
            if result.rowcount > 0:
                imported += 1
                notam_ids.append(rec.get("notam_id"))
            else:
                skipped += 1
        except Exception as exc:
            log.warning(f"notam import row failed: {exc}")
            errors += 1

    db.session.commit()
    log.info(f"notam_routes import: {imported} imported, {skipped} skipped, {errors} errors from {source}")

    return jsonify({
        "imported": imported,
        "skipped_duplicates": skipped,
        "parse_errors": errors,
        "low_confidence": low_conf,
        "notam_ids": notam_ids,
    })
