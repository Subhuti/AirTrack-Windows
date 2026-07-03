# AirTrack 1.0.0
# Copyright (c) 2025 Trevor (“Subhuti”). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary-NC

# routes/airline_logo_linker_routes.py
from pathlib import Path

from flask import (
    current_app,
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import text

from extensions import db
from utils.settings_utils import get_current_theme
from urllib.parse import urlparse

airline_logo_linker = Blueprint(
    "airline_logo_linker", __name__, url_prefix="/link_airlines"
)



def _safe_redirect(default_endpoint: str) -> str:
    """Return referrer URL only if it's the same host as this request; else default."""
    ref = request.referrer
    if ref:
        try:
            ref_host = urlparse(ref).netloc
            own_host = urlparse(request.host_url).netloc
            if ref_host == own_host or not ref_host:
                return ref
        except Exception:
            pass
    return url_for(default_endpoint)


# ✅ ROUTE DECORATOR ADDED
@airline_logo_linker.route("/", methods=["GET", "POST"])
def link_airline_logos():
    search = request.args.get("search", "").lower()
    logos_path = Path(current_app.root_path) / "static" / "logos"
    all_logos = {f.stem for f in logos_path.glob("*.png")}

    if request.method == "POST":
        icao_code = request.form.get("icao_code", "").strip()
        airline_id = request.form.get("airline_id", "").strip()
        if icao_code and airline_id:
            try:
                db.session.execute(
                    text("UPDATE airlines SET Logo = :logo WHERE AirlineID = :id"),
                    {"logo": f"{icao_code}.png", "id": airline_id},
                )
                db.session.commit()
                flash(f"✅ Logo {icao_code}.png linked successfully.", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"❌ Failed to link logo: {e}", "danger")

    # Load airlines
    airline_rows = db.session.execute(
        text("SELECT AirlineID, AirlineName, Logo FROM airlines ORDER BY AirlineName")
    ).fetchall()
    all_airlines = [dict(row._mapping) for row in airline_rows]

    # Determine linked logos
    linked_logos = [
        {
            "AirlineID": a["AirlineID"],
            "AirlineName": a["AirlineName"],
            "Logo": a["Logo"],
            "ICAO": Path(a["Logo"]).stem if a["Logo"] else "",
        }
        for a in all_airlines
        if a["Logo"]
    ]
    linked_icao_codes = {logo["ICAO"] for logo in linked_logos}

    # Determine unlinked logos
    unlinked_icao_codes = sorted(all_logos - linked_icao_codes)
    filtered_logos = [code for code in unlinked_icao_codes if search in code]
    current_theme = get_current_theme()

    return render_template(
        "link_airline_logos.html",
        all_airlines=all_airlines,
        linked_logos=linked_logos,
        unlinked_icao_codes=unlinked_icao_codes,
        unlinked_count=len(unlinked_icao_codes),
        current_theme=current_theme,
        search=search,
    )


# ✅ FIXED: airline_id now captured safely
@airline_logo_linker.route("/unlink_logo", methods=["POST"])
def unlink_logo():
    airline_id = request.form.get("airline_id")

    if not airline_id:
        flash("No airline specified to unlink.", "warning")
        return redirect(
            _safe_redirect("airline_logo_linker.link_airline_logos")
        )

    try:
        db.session.execute(
            text("UPDATE airlines SET Logo = NULL WHERE AirlineID = :id"),
            {"id": airline_id},
        )
        db.session.commit()
        flash("✅ Logo unlinked successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"❌ Failed to unlink logo: {e}", "danger")

    return redirect(
        _safe_redirect("airline_logo_linker.link_airline_logos")
    )
