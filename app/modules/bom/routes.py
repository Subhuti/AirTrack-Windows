#!/usr/bin/env python3
"""
AirTrack Logbook BOM Module Routes

This module is optional and additive.

If app/modules/BOM is removed, Logbook should continue to run normally.
The module loader discovers this file through module.json and registers
`bom_bp` with the configured url_prefix.

Routes provided:
    /modules/BOM/
    /modules/BOM/status
    /modules/BOM/status.json
    /modules/BOM/kiosk.json
    /modules/BOM/weather_status.json
    /modules/BOM/warnings
    /modules/BOM/warnings.json
    /modules/BOM/weather_warnings.json
    /modules/BOM/refresh
    /modules/BOM/health

No user-specific paths are hardwired.
No hardwiring.
No discombobulation.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from flask import Blueprint, Response, jsonify, redirect, request, url_for
from extensions import csrf


MODULE_DIR = Path(__file__).resolve().parent
STATUS_FILE = MODULE_DIR / "weather_status.json"
WARNINGS_FILE = MODULE_DIR / "weather_warnings.json"
CONFIG_FILE = MODULE_DIR / "bom_config.json"
MODULE_FILE = MODULE_DIR / "module.json"
FETCHER_FILE = MODULE_DIR / "bom_fetcher.py"
CACHE_DIR = MODULE_DIR / "cache"
RAW_FEED_FILE = CACHE_DIR / "latest_feed.xml"

AUTO_REFRESH_SECONDS = 300

bom_bp = Blueprint("bom", __name__)


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _json_response(payload: Any, status_code: int = 200) -> Response:
    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


def _module_info() -> Dict[str, Any]:
    default = {
        "name": "BOM",
        "title": "BOM Weather",
        "description": "Optional Bureau of Meteorology weather awareness module for AirTrack Logbook.",
        "version": "0.1.0",
        "enabled": True,
        "provider": "Bureau of Meteorology",
        "country": "AU",
    }

    payload = _read_json(MODULE_FILE, default)
    if isinstance(payload, dict):
        default.update(payload)

    return default


def _config() -> Dict[str, Any]:
    default = {
        "region_label": "NSW / ACT",
        "source_name": "Bureau of Meteorology",
        "source_url": "https://www.bom.gov.au/rss/",
        "feed_url": "",
        "refresh_seconds": AUTO_REFRESH_SECONDS,
    }

    payload = _read_json(CONFIG_FILE, default)
    if isinstance(payload, dict):
        default.update(payload)

    return default


def _status() -> Dict[str, Any]:
    return _read_json(
        STATUS_FILE,
        {
            "ok": False,
            "status": "missing",
            "level": "amber",
            "label": "WX",
            "title": "WX UNAVAILABLE",
            "summary": "BOM weather_status.json has not been generated yet.",
            "region": "",
            "updated": "",
            "updated_utc": "",
            "count": 0,
            "source": "Bureau of Meteorology",
            "source_url": "https://www.bom.gov.au/rss/",
            "top_warning": None,
            "stale": False,
        },
    )


def _warnings() -> Dict[str, Any]:
    return _read_json(
        WARNINGS_FILE,
        {
            "ok": False,
            "region": "",
            "updated": "",
            "updated_utc": "",
            "count": 0,
            "items": [],
        },
    )


def _escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _class_for_level(level: str) -> str:
    level = str(level or "").lower()

    if level in {"red", "danger", "error", "critical", "emergency"}:
        return "danger"

    if level in {"amber", "yellow", "warning", "stale", "advisory"}:
        return "warning"

    if level in {"green", "ok", "normal"}:
        return "ok"

    return "muted"


def _operational_class(status_payload: Dict[str, Any]) -> str:
    if not status_payload.get("ok"):
        return "danger"

    if status_payload.get("stale"):
        return "warning"

    return _class_for_level(str(status_payload.get("level", "")))


def _operational_title(status_payload: Dict[str, Any]) -> str:
    if not status_payload.get("ok"):
        return "MODULE DEGRADED"

    if status_payload.get("stale"):
        return "WX CACHED"

    return "MODULE ONLINE"


def _feed_state(status_payload: Dict[str, Any]) -> str:
    if not status_payload.get("ok"):
        return "ERROR"

    if status_payload.get("stale"):
        return "CACHED"

    return "LIVE"


def _cache_state() -> str:
    if RAW_FEED_FILE.exists():
        return "OK"
    return "MISSING"


def _warning_items() -> List[Dict[str, Any]]:
    warnings = _warnings()
    items = warnings.get("items", [])
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def _load_fetcher_module():
    if not FETCHER_FILE.exists():
        raise ImportError(f"Fetcher file not found: {FETCHER_FILE}")

    spec = importlib.util.spec_from_file_location("airtrack_bom_fetcher", str(FETCHER_FILE))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create import spec for {FETCHER_FILE}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_warning_cards(items: List[Dict[str, Any]]) -> str:
    warning_cards: List[str] = []

    for item in items:
        item_level_class = _class_for_level(str(item.get("level", "")))
        title = _escape(item.get("title") or "Untitled warning")
        summary = _escape(item.get("summary") or "")
        published = _escape(item.get("published") or item.get("published_utc") or "")
        source = _escape(item.get("source") or "Bureau of Meteorology")
        status = _escape(item.get("status") or "warning")
        link = _escape(item.get("link") or "")

        link_html = ""
        if link:
            link_html = (
                f'<a class="inline-link" href="{link}" '
                f'target="_blank" rel="noopener noreferrer">Open BOM warning</a>'
            )

        warning_cards.append(
            f"""
            <section class="warning-card {item_level_class}">
                <div class="warning-card-head">
                    <div>
                        <div class="warning-title">{title}</div>
                        <div class="warning-meta">{published}</div>
                    </div>
                    <span class="pill {item_level_class}">{status}</span>
                </div>
                <p>{summary}</p>
                <div class="warning-footer">
                    <span>{source}</span>
                    {link_html}
                </div>
            </section>
            """
        )

    if not warning_cards:
        warning_cards.append(
            """
            <section class="warning-card ok">
                <div class="warning-card-head">
                    <div>
                        <div class="warning-title">No active warnings</div>
                        <div class="warning-meta">Current feed reports normal conditions.</div>
                    </div>
                    <span class="pill ok">normal</span>
                </div>
                <p>No active BOM warnings were found in the current feed.</p>
            </section>
            """
        )

    return "\n".join(warning_cards)


def _render_dashboard() -> str:
    module = _module_info()
    config = _config()
    status = _status()
    warnings = _warnings()
    items = _warning_items()

    level_class = _operational_class(status)
    operational_title = _operational_title(status)
    feed_state = _feed_state(status)
    cache_state = _cache_state()

    updated = status.get("updated") or warnings.get("updated") or "Never"
    region = (
        status.get("region")
        or config.get("region_label")
        or warnings.get("region")
        or "Configured region"
    )
    source = status.get("source") or config.get("source_name") or "Bureau of Meteorology"
    source_url = status.get("source_url") or config.get("source_url") or "https://www.bom.gov.au/rss/"
    feed_url = config.get("feed_url") or status.get("feed_url") or ""
    refresh_seconds = int(config.get("refresh_seconds") or AUTO_REFRESH_SECONDS)

    if refresh_seconds < 60:
        refresh_seconds = AUTO_REFRESH_SECONDS

    stale_badge = ""
    if status.get("stale"):
        stale_badge = '<span class="pill warning">cached data</span>'

    last_error = status.get("last_error") or ""
    last_error_html = ""
    if last_error:
        last_error_html = f"""
        <div class="alert-line">
            <strong>Last feed error:</strong> {_escape(last_error)}
        </div>
        """

    warning_cards = _render_warning_cards(items)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{_escape(module.get("title", "BOM Weather"))} - AirTrack Logbook</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="{refresh_seconds}">
<style>
    :root {{
        --bg: #050705;
        --panel: #0d1710;
        --panel2: #111f15;
        --fg: #d7ffe2;
        --muted: #8dcfa5;
        --line: #1b7f4b;
        --bright: #1cff8a;
        --warn: #ffe45c;
        --danger: #ff6060;
        --blue: #7ec8ff;
        --shadow: rgba(0, 0, 0, 0.38);
    }}

    * {{
        box-sizing: border-box;
    }}

    body {{
        margin: 0;
        min-height: 100vh;
        background:
            radial-gradient(circle at top left, rgba(28, 255, 138, 0.12), transparent 34%),
            radial-gradient(circle at bottom right, rgba(126, 200, 255, 0.08), transparent 30%),
            linear-gradient(135deg, #030503, #071208 55%, #020302);
        color: var(--fg);
        font-family: "Courier New", monospace;
    }}

    .shell {{
        width: min(1220px, calc(100vw - 32px));
        margin: 0 auto;
        padding: 28px 0 48px;
    }}

    .top {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 20px;
        margin-bottom: 18px;
    }}

    h1 {{
        margin: 0;
        color: var(--bright);
        font-size: clamp(32px, 5vw, 58px);
        letter-spacing: 0.04em;
        text-shadow: 0 0 18px rgba(28, 255, 138, 0.25);
    }}

    .subtitle {{
        margin-top: 8px;
        color: var(--muted);
        font-size: 15px;
        line-height: 1.5;
        max-width: 760px;
    }}

    .actions {{
        display: flex;
        flex-wrap: wrap;
        justify-content: flex-end;
        gap: 10px;
    }}

    .button {{
        border: 1px solid var(--line);
        background: linear-gradient(to bottom, #173823, #091d10);
        color: var(--fg);
        border-radius: 12px;
        padding: 10px 14px;
        text-decoration: none;
        font-weight: 700;
        box-shadow: 0 6px 18px var(--shadow);
        cursor: pointer;
        font-family: inherit;
        font-size: 14px;
    }}

    .button:hover {{
        color: var(--bright);
        border-color: var(--bright);
    }}

    .ops-strip {{
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: 10px;
        margin-bottom: 16px;
    }}

    .ops-tile {{
        border: 1px solid rgba(28, 255, 138, 0.22);
        background: rgba(0, 0, 0, 0.22);
        border-radius: 14px;
        padding: 12px;
        box-shadow: 0 8px 20px var(--shadow);
    }}

    .ops-tile.ok {{
        border-color: rgba(28, 255, 138, 0.55);
    }}

    .ops-tile.warning {{
        border-color: rgba(255, 228, 92, 0.72);
    }}

    .ops-tile.danger {{
        border-color: rgba(255, 96, 96, 0.85);
    }}

    .ops-label {{
        color: var(--muted);
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.14em;
        margin-bottom: 6px;
    }}

    .ops-value {{
        font-size: 17px;
        font-weight: 800;
        color: var(--bright);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }}

    .ops-tile.warning .ops-value {{
        color: var(--warn);
    }}

    .ops-tile.danger .ops-value {{
        color: var(--danger);
    }}

    .status-grid {{
        display: grid;
        grid-template-columns: 1.35fr 0.75fr 0.65fr;
        gap: 14px;
        margin-bottom: 18px;
    }}

    .panel {{
        background: linear-gradient(to bottom, rgba(13, 23, 16, 0.96), rgba(5, 11, 7, 0.96));
        border: 1px solid rgba(28, 255, 138, 0.24);
        border-radius: 18px;
        padding: 18px;
        box-shadow: 0 12px 28px var(--shadow);
    }}

    .big-status {{
        border-width: 2px;
    }}

    .big-status.ok {{
        border-color: rgba(28, 255, 138, 0.55);
    }}

    .big-status.warning {{
        border-color: rgba(255, 228, 92, 0.75);
    }}

    .big-status.danger {{
        border-color: rgba(255, 96, 96, 0.85);
    }}

    .label {{
        color: var(--muted);
        font-size: 13px;
        text-transform: uppercase;
        letter-spacing: 0.16em;
        margin-bottom: 8px;
    }}

    .value {{
        font-size: 28px;
        font-weight: 800;
        color: var(--bright);
    }}

    .big-status.warning .value {{
        color: var(--warn);
    }}

    .big-status.danger .value {{
        color: var(--danger);
    }}

    .summary {{
        margin-top: 10px;
        color: var(--fg);
        line-height: 1.5;
        font-size: 16px;
    }}

    .pill-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: 12px;
    }}

    .pill {{
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        border: 1px solid rgba(28, 255, 138, 0.38);
        color: var(--bright);
        padding: 4px 9px;
        font-size: 12px;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        background: rgba(28, 255, 138, 0.08);
    }}

    .pill.warning {{
        border-color: rgba(255, 228, 92, 0.76);
        color: var(--warn);
        background: rgba(255, 228, 92, 0.08);
    }}

    .pill.danger {{
        border-color: rgba(255, 96, 96, 0.85);
        color: var(--danger);
        background: rgba(255, 96, 96, 0.08);
    }}

    .pill.ok {{
        border-color: rgba(28, 255, 138, 0.55);
        color: var(--bright);
        background: rgba(28, 255, 138, 0.08);
    }}

    .meta-list {{
        display: grid;
        gap: 8px;
        color: var(--muted);
        font-size: 14px;
    }}

    .meta-list strong {{
        color: var(--fg);
    }}

    .alert-line {{
        margin-top: 12px;
        border: 1px solid rgba(255, 228, 92, 0.6);
        border-radius: 12px;
        padding: 10px;
        color: var(--warn);
        background: rgba(255, 228, 92, 0.06);
        line-height: 1.45;
    }}

    .warning-list {{
        display: grid;
        gap: 12px;
        margin-top: 14px;
    }}

    .warning-card {{
        background: rgba(0, 0, 0, 0.22);
        border: 1px solid rgba(28, 255, 138, 0.24);
        border-radius: 16px;
        padding: 16px;
    }}

    .warning-card.warning {{
        border-color: rgba(255, 228, 92, 0.75);
    }}

    .warning-card.danger {{
        border-color: rgba(255, 96, 96, 0.85);
    }}

    .warning-card-head {{
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: flex-start;
        margin-bottom: 6px;
    }}

    .warning-title {{
        font-size: 20px;
        font-weight: 800;
        color: var(--bright);
        margin-bottom: 6px;
    }}

    .warning-card.warning .warning-title {{
        color: var(--warn);
    }}

    .warning-card.danger .warning-title {{
        color: var(--danger);
    }}

    .warning-meta {{
        color: var(--muted);
        font-size: 13px;
    }}

    .warning-footer {{
        display: flex;
        justify-content: space-between;
        gap: 12px;
        color: var(--muted);
        font-size: 13px;
        margin-top: 12px;
    }}

    p {{
        margin: 8px 0;
        line-height: 1.55;
    }}

    a,
    .inline-link {{
        color: var(--bright);
    }}

    .footer {{
        margin-top: 20px;
        color: var(--muted);
        font-size: 13px;
        line-height: 1.5;
    }}

    @media (max-width: 980px) {{
        .ops-strip {{
            grid-template-columns: repeat(2, 1fr);
        }}

        .status-grid {{
            grid-template-columns: 1fr;
        }}
    }}

    @media (max-width: 850px) {{
        .top {{
            display: block;
        }}

        .actions {{
            justify-content: flex-start;
            margin-top: 16px;
        }}
    }}

    @media (max-width: 520px) {{
        .ops-strip {{
            grid-template-columns: 1fr;
        }}

        .warning-card-head,
        .warning-footer {{
            display: block;
        }}

        .pill {{
            margin-top: 8px;
        }}
    }}
</style>
</head>
<body>
    <main class="shell">
        <section class="top">
            <div>
                <h1>{_escape(module.get("title", "BOM Weather"))}</h1>
                <div class="subtitle">
                    Optional AirTrack weather-awareness module. This page is powered by module-local JSON,
                    cached feed data, and the drop-in module loader.
                </div>
            </div>

            <div class="actions">
                <a class="button" href="/index">Back to Logbook</a>
                <a class="button" href="{url_for("bom.health")}">Health</a>
                <a class="button" href="{url_for("bom.status_json")}">Status JSON</a>
                <a class="button" href="{url_for("bom.warnings_json")}">Warnings JSON</a>
                <form method="post" action="{url_for("bom.refresh")}" style="margin:0;">
                    <button class="button" type="submit">Refresh Feed</button>
                </form>
            </div>
        </section>

        <section class="ops-strip">
            <article class="ops-tile {level_class}">
                <div class="ops-label">Module</div>
                <div class="ops-value">{_escape(operational_title)}</div>
            </article>

            <article class="ops-tile {level_class}">
                <div class="ops-label">Feed</div>
                <div class="ops-value">{_escape(feed_state)}</div>
            </article>

            <article class="ops-tile ok">
                <div class="ops-label">Cache</div>
                <div class="ops-value">{_escape(cache_state)}</div>
            </article>

            <article class="ops-tile {_class_for_level(str(status.get("level", "")))}">
                <div class="ops-label">Warnings</div>
                <div class="ops-value">{_escape(status.get("count", 0))}</div>
            </article>

            <article class="ops-tile ok">
                <div class="ops-label">Refresh</div>
                <div class="ops-value">{refresh_seconds}s</div>
            </article>
        </section>

        <section class="status-grid">
            <article class="panel big-status {level_class}">
                <div class="label">Weather Status</div>
                <div class="value">{_escape(status.get("title") or "WX UNKNOWN")}</div>
                <div class="summary">{_escape(status.get("summary") or "No summary available.")}</div>
                <div class="pill-row">
                    <span class="pill {level_class}">{_escape(status.get("status") or "unknown")}</span>
                    <span class="pill ok">{_escape(status.get("label") or "WX")}</span>
                    {stale_badge}
                </div>
                {last_error_html}
            </article>

            <article class="panel">
                <div class="label">Region</div>
                <div class="value" style="font-size:22px;">{_escape(region)}</div>
                <div class="summary">Configured BOM warning region.</div>
            </article>

            <article class="panel">
                <div class="label">Active Warnings</div>
                <div class="value">{_escape(status.get("count", 0))}</div>
                <div class="summary">Sorted by operational severity.</div>
            </article>
        </section>

        <section class="panel">
            <div class="label">Module Information</div>
            <div class="meta-list">
                <div><strong>Updated:</strong> {_escape(updated)}</div>
                <div><strong>Provider:</strong> {_escape(source)}</div>
                <div><strong>Module:</strong> {_escape(module.get("name", "BOM"))} v{_escape(module.get("version", "0.1.0"))}</div>
                <div><strong>Enabled:</strong> {_escape(module.get("enabled", True))}</div>
                <div><strong>Feed URL:</strong> {_escape(feed_url or "Not configured")}</div>
                <div><strong>Source:</strong> <a href="{_escape(source_url)}" target="_blank" rel="noopener noreferrer">{_escape(source_url)}</a></div>
            </div>
        </section>

        <section class="panel" style="margin-top:14px;">
            <div class="label">Active Warnings</div>
            <div class="warning-list">
                {warning_cards}
            </div>
        </section>

        <div class="footer">
            This module uses public Bureau of Meteorology feed data where available.
            Always consult official BOM sources for critical weather decisions.
            Auto-refresh is enabled every {refresh_seconds} seconds.
        </div>
    </main>
</body>
</html>
"""


@bom_bp.route("/", methods=["GET"])
def dashboard() -> Response:
    return Response(_render_dashboard(), mimetype="text/html")


@bom_bp.route("/status", methods=["GET"])
def status() -> Response:
    return redirect(url_for("bom.dashboard"))


@bom_bp.route("/status.json", methods=["GET"])
def status_json() -> Response:
    return _json_response(_status())


@bom_bp.route("/kiosk.json", methods=["GET"])
def kiosk_json() -> Response:
    return _json_response(_status())


@bom_bp.route("/weather_status.json", methods=["GET"])
def weather_status_json() -> Response:
    return _json_response(_status())


@bom_bp.route("/warnings", methods=["GET"])
def warnings() -> Response:
    return redirect(url_for("bom.dashboard"))


@bom_bp.route("/warnings.json", methods=["GET"])
def warnings_json() -> Response:
    return _json_response(_warnings())


@bom_bp.route("/weather_warnings.json", methods=["GET"])
def weather_warnings_json() -> Response:
    return _json_response(_warnings())


@bom_bp.route("/refresh", methods=["POST"])
@csrf.exempt
def refresh() -> Response:
    try:
        bom_fetcher = _load_fetcher_module()
    except Exception as exc:
        import logging as _log
        _log.exception("BOM route error: could not import bom_fetcher")
        return _json_response(
            {
                "ok": False,
                "error": "BOM module unavailable — see server logs",
            },
            500,
        )

    try:
        ok = bool(bom_fetcher.run_once())
        if request.headers.get("Accept", "").lower().find("application/json") >= 0:
            return _json_response({"ok": ok, "status": _status()})
        return redirect(url_for("bom.dashboard"))
    except Exception as exc:
        import logging as _log
        _log.exception("BOM route error")
        return _json_response(
            {
                "ok": False,
                "error": "Internal server error",
            },
            500,
        )


@bom_bp.route("/health", methods=["GET"])
def health() -> Response:
    module = _module_info()
    status_payload = _status()
    warnings_payload = _warnings()

    return _json_response(
        {
            "ok": True,
            "module": module.get("name", "BOM"),
            "title": module.get("title", "BOM Weather"),
            "version": module.get("version", "0.1.0"),
            "enabled": module.get("enabled", True),
            "status_file_exists": STATUS_FILE.exists(),
            "warnings_file_exists": WARNINGS_FILE.exists(),
            "cache_file_exists": RAW_FEED_FILE.exists(),
            "weather_status": status_payload.get("status"),
            "weather_level": status_payload.get("level"),
            "stale": bool(status_payload.get("stale")),
            "warning_count": status_payload.get("count", warnings_payload.get("count", 0)),
            "updated": status_payload.get("updated"),
            "updated_utc": status_payload.get("updated_utc"),
            "checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
