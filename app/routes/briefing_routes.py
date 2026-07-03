"""
briefing_routes.py
==================
API routes for the AirTrack Daily Operational Briefing.

GET  /api/briefing          — Returns the current briefing if unread, else {ready: false}
POST /api/briefing/mark-read — Marks the briefing as read
POST /api/briefing/generate  — Manually trigger a briefing generation (admin use)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import Blueprint, jsonify

log = logging.getLogger(__name__)

briefing_bp = Blueprint("briefing", __name__)

BRIEFING_FILE = Path(__file__).resolve().parent.parent / "woodland" / "briefing" / "daily_briefing.json"


def _load_briefing() -> dict | None:
    if not BRIEFING_FILE.exists():
        return None
    try:
        return json.loads(BRIEFING_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read briefing file: %s", exc)
        return None


def _save_briefing(data: dict) -> None:
    tmp = BRIEFING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(BRIEFING_FILE)


@briefing_bp.route("/api/briefing", methods=["GET"])
def get_briefing():
    """Return the current briefing if it exists and is unread."""
    briefing = _load_briefing()
    if briefing is None or briefing.get("read", True):
        return jsonify({"ready": False})
    return jsonify({"ready": True, "briefing": briefing})


@briefing_bp.route("/api/briefing/mark-read", methods=["POST"])
def mark_read():
    """Mark the current briefing as read."""
    briefing = _load_briefing()
    if briefing is None:
        return jsonify({"ok": False, "error": "No briefing found"})
    briefing["read"] = True
    _save_briefing(briefing)
    return jsonify({"ok": True})


@briefing_bp.route("/api/briefing/generate", methods=["POST"])
def generate_briefing():
    """Manually trigger briefing generation (admin use — no auth, localhost-trust only)."""
    try:
        from woodland.daily_briefing import generate
        briefing = generate()
        return jsonify({"ok": True, "generated_at": briefing.get("generated_at")})
    except Exception as exc:
        log.error("Manual briefing generation failed: %s", exc, exc_info=True)
        current_app.logger.exception("briefing error")
        return jsonify({"ok": False, "error": "Internal server error"}), 500
