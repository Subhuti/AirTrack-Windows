# AirTrack 1.0.0
# Copyright (c) 2025 Trevor ("Subhuti"). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary-NC

import logging

from datetime import date, datetime

from extensions import db

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from sqlalchemy import text

from utils.settings_utils import get_current_theme

# Optional local-time converter (safe fallback)
from utils.settings_utils import convert_to_local, format_display_dt

airlines_bp = Blueprint("airlines", __name__, url_prefix="/airlines")


def load_settings():
    settings = {}
    try:
        rows = db.session.execute(
            text("SELECT SettingKey, SettingValue FROM app_settings")
        ).fetchall()
        settings = {row[0]: row[1] for row in rows}
    except Exception as e:
        logging.warning(f"⚠️ Failed to load settings: {e}")
    return settings


@airlines_bp.route("/", methods=["GET"], endpoint="airlines_table")
def airlines_table():
    """Paginated Airlines table."""
    try:
        airline_filter = request.args.get("airline", "").strip()
        search_query = request.args.get("search", "").strip()
        page = request.args.get("page", default=1, type=int)
        per_page = 10
        offset = (page - 1) * per_page

        params = {
            "search": f"%{search_query}%",
            "limit": per_page,
            "offset": offset,
        }

        count_result = db.session.execute(
            text(
                """
                SELECT COUNT(DISTINCT a.AirlineID) AS total
                FROM airlines a
                LEFT JOIN aircraft ac ON a.AirlineID = ac.AirlineID
                WHERE a.AirlineName LIKE :search
                """
            ),
            params,
        ).fetchone()
        total_records = count_result[0] if count_result else 0
        total_pages = max(1, (total_records + per_page - 1) // per_page)

        # Block pagination
        block = 10
        start_page = ((page - 1) // block) * block + 1
        end_page = min(start_page + block - 1, total_pages)
        prev_10_page = start_page - block if start_page > 1 else None
        next_10_page = end_page + 1 if end_page < total_pages else None
        prev_page = page - 1 if page > 1 else None
        next_page = page + 1 if page < total_pages else None

        result = db.session.execute(
            text(
                """
                SELECT a.AirlineID, a.AirlineName, a.Logo,
                       a.Ceased_Operations,
                       COUNT(ac.AircraftID) AS TotalAircraft
                FROM airlines a
                LEFT JOIN aircraft ac ON a.AirlineID = ac.AirlineID
                WHERE a.AirlineName LIKE :search
                GROUP BY a.AirlineID, a.AirlineName, a.Logo, a.Ceased_Operations
                ORDER BY a.AirlineName
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        airlines = [dict(row._mapping) for row in result.fetchall()]

        settings = load_settings()
        current_theme = get_current_theme()
        return render_template(
            "airlines_table.html",
            airlines=airlines,
            current_page=page,
            total_pages=total_pages,
            start_page=start_page,
            end_page=end_page,
            prev_10_page=prev_10_page,
            next_10_page=next_10_page,
            prev_page=prev_page,
            next_page=next_page,
            per_page=per_page,
            search_query=search_query,
            airline_filter=airline_filter,
            settings=settings,
            current_theme=current_theme,
        )
    except Exception as e:
        logging.error(f"❌ Error fetching airlines: {e}")
        flash("An error occurred loading airlines.", "danger")
        settings = load_settings()
        current_theme = get_current_theme()
        # Keep the template shape stable on error
        return render_template(
            "airlines_table.html",
            airlines=[],
            current_page=1,
            total_pages=1,
            start_page=1,
            end_page=1,
            per_page=10,
            search_query="",
            airline_filter="",
            prev_10_page=None,
            next_10_page=None,
            prev_page=None,
            next_page=None,
            settings=settings,
            current_theme=current_theme,
        )


@airlines_bp.route("/info/<int:airline_id>", methods=["GET"], endpoint="airline_info")
def airline_info(airline_id: int):
    """
    Airline detail page.
    URL: /airlines/info/<airline_id>
    Templates should link via: url_for('airlines.airline_info', airline_id=ID)
    """
    try:
        # --- Airline row
        airline_row = db.session.execute(
            text(
                """
                SELECT AirlineID, AirlineName, Logo, Country, IATA, ICAO, Callsign,
                       Ceased_Operations, Ceased_Date, Last_Updated
                FROM airlines
                WHERE AirlineID = :id
                """
            ),
            {"id": airline_id},
        ).fetchone()

        if not airline_row:
            flash("Airline not found.", "warning")
            return redirect(url_for("airlines.airlines_table"))

        airline = dict(airline_row._mapping)

        # Normalize and present-friendly values
        def norm(s):
            return (s or "").strip() or None

        airline["Country"] = norm(airline.get("Country"))
        airline["IATA"] = norm(airline.get("IATA"))
        airline["ICAO"] = norm(airline.get("ICAO"))
        airline["Callsign"] = norm(airline.get("Callsign"))

        # Format Last_Updated to local display
        lu = airline.get("Last_Updated")

        if isinstance(lu, date) and not isinstance(lu, datetime):
            lu = datetime.combine(lu, datetime.min.time())

        airline["Last_Updated_Display"] = format_display_dt(lu, default="—")

        # --- Aircraft operated by this airline ---
        rows = db.session.execute(
            text(
                """
                SELECT
                    ac.AircraftID,
                    ac.Registration,
                    ac.Aircraft_Type,
                    ac.FlightNumber,
                    ac.Departure,
                    ac.Arrival,
                    ac.First_Sighted,
                    ac.Aircraft_Updated
                FROM aircraft ac
                WHERE ac.AirlineID = :id
                ORDER BY ac.Aircraft_Updated DESC
                """
            ),
            {"id": airline_id},
        ).fetchall()

        aircraft_list = []
        for r in rows:
            d = dict(r._mapping)

            # Time displays
            fs = d.get("First_Sighted")
            if isinstance(fs, date) and not isinstance(fs, datetime):
                fs = datetime.combine(fs, datetime.min.time())

            up = d.get("Aircraft_Updated")
            if isinstance(up, date) and not isinstance(up, datetime):
                up = datetime.combine(up, datetime.min.time())

            # Format to display strings
            d["First_Sighted_Display"] = format_display_dt(fs, default="—")
            d["Aircraft_Updated_Display"] = format_display_dt(up, default="—")

            aircraft_list.append(d)

        # --- Former aircraft (historical operators via aircraft_owners) ---
        former_rows = db.session.execute(
            text(
                """
                SELECT ac.AircraftID, ac.Registration, ac.Aircraft_Type,
                       ao.From_Date, ao.To_Date, ao.Notes
                FROM aircraft_owners ao
                JOIN aircraft ac ON ao.AircraftID = ac.AircraftID
                WHERE ao.AirlineID = :id
                  AND ao.To_Date IS NOT NULL
                ORDER BY ao.To_Date DESC
                """
            ),
            {"id": airline_id},
        ).fetchall()
        former_list = [dict(r._mapping) for r in former_rows]

        current_theme = get_current_theme()
        return render_template(
            "airline_info.html",
            airline=airline,
            aircraft_list=aircraft_list,
            former_list=former_list,
            selected_theme=current_theme,
            cache_bust=datetime.utcnow().strftime("%Y%m%d%H%M%S"),
        )
    except Exception as e:
        logging.error("❌ Failed to load airline info", exc_info=True)
        flash("Could not load airline info.", "danger")
        return redirect(url_for("airlines.airlines_table"))


@airlines_bp.route("/lookup", methods=["GET"], endpoint="airline_lookup")
def airline_lookup():
    """
    JSON endpoint: search airlines table for form pre-fill suggestions.
    JSON endpoint: search airline_codes reference table for form pre-fill suggestions.
    GET /airlines/lookup?q=<name>
    Returns: { results: [ {name, iata, icao, callsign, country}, ... ] }

    Searches active airlines (Ceased_Operations = 0) only.
    Results are suggestions only — saving always writes to airlines via the form.
    Ranking: exact name/code match first, then starts-with, then contains.
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    try:
        q_upper = q.upper()
        rows = db.session.execute(
            text("""
                SELECT AirlineName, IATA, ICAO, Callsign, Country
                FROM airlines
                WHERE Ceased_Operations = 0
                  AND (
                      AirlineName LIKE :contains
                      OR UPPER(IATA) = :exact_upper
                      OR UPPER(ICAO) = :exact_upper
                  )
                ORDER BY
                    CASE
                        WHEN UPPER(AirlineName) = :exact_upper THEN 0
                        WHEN UPPER(IATA)        = :exact_upper THEN 0
                        WHEN UPPER(ICAO)        = :exact_upper THEN 0
                        WHEN AirlineName LIKE :starts          THEN 1
                        ELSE 2
                    END,
                    AirlineName
                LIMIT 5
            """),
            {
                "contains":    f"%{q}%",
                "starts":      f"{q}%",
                "exact_upper": q_upper,
            },
        ).fetchall()

        results = [
            {
                "name":     r[0],
                "iata":     r[1] or "",
                "icao":     r[2] or "",
                "callsign": r[3] or "",
                "country":  r[4] or "",
            }
            for r in rows
        ]
        return jsonify({"results": results})
    except Exception as exc:
        logging.warning("airline_lookup: DB error: %s", exc)
        current_app.logger.exception("airline search failed")
        return jsonify({"results": [], "error": "Search failed"})


@airlines_bp.route("/add", methods=["GET", "POST"], endpoint="add_airline")
def add_airline():
    settings = load_settings() or {}
    current_theme = get_current_theme()

    if request.method == "POST":
        airline_name = request.form.get("AirlineName", "").strip()
        if not airline_name:
            flash("Airline name is required.", "danger")
            return redirect(url_for("airlines.add_airline"))
        try:
            iata     = request.form.get("IATA", "").strip() or None
            icao     = request.form.get("ICAO", "").strip() or None
            callsign = request.form.get("Callsign", "").strip() or None
            country  = request.form.get("Country", "").strip() or None
            ceased      = 1 if request.form.get("Ceased_Operations") else 0
            ceased_date = request.form.get("Ceased_Date", "").strip() or None

            db.session.execute(
                text(
                    "INSERT INTO airlines (AirlineName, IATA, ICAO, Callsign, Country, Ceased_Operations, Ceased_Date) "
                    "VALUES (:name, :iata, :icao, :callsign, :country, :ceased, :ceased_date)"
                ),
                {
                    "name":        airline_name,
                    "iata":        iata,
                    "icao":        icao,
                    "callsign":    callsign,
                    "country":     country,
                    "ceased":      ceased,
                    "ceased_date": ceased_date,
                },
            )
            db.session.commit()
            flash("Airline added successfully!", "success")
            return redirect(url_for("airlines.airlines_table"))
        except Exception as e:
            logging.error(f"❌ Error adding airline: {e}", exc_info=True)
            flash("Failed to add airline.", "danger")
            return redirect(url_for("airlines.add_airline"))

    return render_template(
        "add_airline.html",
        settings=settings,
        selected_theme=current_theme,
        cache_bust=datetime.utcnow().strftime("%Y%m%d%H%M%S"),
        AirlineName="",
        IATA="", ICAO="", Callsign="", Country="",
        Ceased_Operations=0, Ceased_Date="",
    )
