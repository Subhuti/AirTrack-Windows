# AirTrack 1.0.0
# Copyright (c) 2025 AirTrack Solutions (ABN 70 472 536 433). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary

import os
import json
import urllib.request
import urllib.error
from datetime import date, datetime
from pathlib import Path

import pytz
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import text

from extensions import db
from security.guards import require_server
from utils.settings_utils import format_display_dt

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")

SITEMAP_PATH = Path(__file__).resolve().parents[1] / "static" / "sitemap.json"

# -------------------------------------------------
# Database schema description sent to the API
# -------------------------------------------------
DB_SCHEMA = """
You are an expert assistant for AirTrack, a hobbyist aircraft spotting logbook application.
The MariaDB database has these tables and columns:

aircraft (main spotting logbook):
  AircraftID (int PK), AirlineID (int FK->airlines), Registration (varchar),
  FlightNumber (varchar), Aircraft_Type (varchar), MSN (varchar), Category (varchar),
  Country_of_Reg (varchar), Country_Flag (varchar), Times_Seen (int),
  Manufacture_Year (year), Manufacture_Month (tinyint), ICAO_Address (varchar),
  Spotted_At (varchar), Notes (text), Departure (varchar ICAO), Arrival (varchar ICAO),
  Age (int), Sightings (int default 1), Engine_Type (varchar), Orphaned (tinyint default 0),
  Aircraft_Image (varchar, NULL=no image, NOT NULL=has primary image),
  First_Sighted (datetime), Aircraft_Updated (timestamp), Timestamp (datetime)

airlines:
  AirlineID (int PK), AirlineName (varchar), Country (varchar),
  IATA (varchar), ICAO (varchar), Callsign (varchar), Logo (varchar),
  Last_Updated (timestamp)

flights (every sighting logged):
  FlightID (int PK), AircraftID (int), AirlineID (int FK->airlines),
  FlightNumber (varchar), Registration (varchar), MSN (varchar),
  Aircraft_Type (varchar), Times_Seen (int), Departure (varchar ICAO),
  Arrival (varchar ICAO), Country_of_Reg (varchar), Country_Flag (varchar),
  Flight_Image (varchar), Notes (text), Spotted_At (varchar),
  Timestamp (datetime), Flight_Updated (timestamp)

airports:
  id (int PK), ICAO (varchar), IATA (varchar), AirportName (varchar),
  Country (varchar), municipality (varchar), latitude_deg (decimal),
  longitude_deg (decimal), elevation_ft (int), type (varchar)

aircraft_images (additional images 2-5 per aircraft):
  ImageID (int PK), AircraftID (int FK->aircraft), Registration (varchar),
  Filename (varchar), Image_Number (tinyint default 2), Uploaded_At (datetime)

aircraft_owners (ownership history):
  OwnerID (int PK), AircraftID (int FK->aircraft), AirlineID (int FK->airlines),
  From_Date (date), To_Date (date nullable), Notes (varchar)

app_settings:
  SettingKey (varchar PK), SettingValue (varchar)

registration_prefixes (ICAO prefix to country mapping):
  Reg_Prefix (varchar PK), Country_of_Reg (varchar)

Country registry tables (real-world aircraft registries for cross-referencing):
  australia: registration, aircraftmanufacturer, aircraftmodel, msn, yearmanu,
             registeredowner, operatorname, icaotypedesig
  united_kingdom, united_states (n_number), new_zealand, canada, germany, france,
  japan, china, south_korea, singapore, thailand, indonesia, india (not present),
  and many others — all have: registration, model, operator, serial, icao_address
  united_states uses n_number instead of registration

IMPORTANT QUERY GUIDANCE:
- Total aircraft count: SELECT COUNT(*) FROM aircraft
- Total flights logged: SELECT COUNT(*) FROM flights
- Total airlines: SELECT COUNT(*) FROM airlines
- Aircraft WITH a primary image: SELECT COUNT(*) FROM aircraft WHERE Aircraft_Image IS NOT NULL AND Aircraft_Image != ''
- Aircraft WITHOUT any image: SELECT COUNT(*) FROM aircraft WHERE (Aircraft_Image IS NULL OR Aircraft_Image = '') AND AircraftID NOT IN (SELECT DISTINCT AircraftID FROM aircraft_images)
- Total images: SELECT (SELECT COUNT(*) FROM aircraft WHERE Aircraft_Image IS NOT NULL AND Aircraft_Image != '') + (SELECT COUNT(*) FROM aircraft_images) AS TotalImages
- Primary image is Aircraft_Image on aircraft table; extra images (2-5) are in aircraft_images
- Never invent column names — only use columns listed above
- For counting, prefer simple COUNT(*) over complex JOINs
- Cross-reference with country registry tables to enrich answers (e.g. JOIN australia ON aircraft.Registration = australia.registration)
- When asked about a specific registration, check both aircraft table and relevant country registry

The user asks natural language questions about their aircraft spotting data.
You will also receive a SITEMAP listing every page in the application with its URL and description.
Use the sitemap to answer navigation questions like "where do I add an aircraft" or "how do I see reports".

You must respond with a JSON object in one of three formats:

Format 1 - for questions that need a database query:
{
  "type": "query",
  "sql": "SELECT ... FROM ... WHERE ...",
  "explanation": "Plain English explanation of what this query does"
}

Format 2 - for questions you can answer without a query (general knowledge about aircraft types, airlines, etc):
{
  "type": "answer",
  "text": "Your plain English answer here"
}

Format 3 - for navigation questions about where to find a feature or page:
{
  "type": "navigate",
  "text": "Plain English answer describing where to go",
  "url": "/the/url/to/link/to"
}

Rules:
- Only use SELECT statements, never INSERT/UPDATE/DELETE/DROP
- Always use table aliases for clarity
- Limit results to 50 rows maximum unless the user asks for more
- For date questions, use MySQL date functions
- If unsure, return a query that gets relevant data and explain what it shows
- Respond ONLY with the JSON object, no markdown, no explanation outside the JSON
- For navigation questions, always use Format 3 with the exact URL from the sitemap
"""


# -------------------------------------------------
# Helpers
# -------------------------------------------------



def render_report(title, columns, data):
    return render_template(
        "partials/_report_table.html", title=title, columns=columns, data=data
    )


def safe_query(sql, params=None, title="", columns=None):
    """Execute a safe DB query with optional parameters and render a report."""
    try:
        result = db.session.execute(text(sql), params or {})
        rows = [dict(row._mapping) for row in result.fetchall()]

        for row in rows:
            if "First_Sighted" in row:
                row["First_Sighted"] = format_display_dt(row["First_Sighted"])

        return render_report(title, columns or [], rows)

    except Exception as e:
        print(f"❌ Error in report '{title}': {e}")
        flash("Report failed to generate.", "danger")
        return render_report(title, columns or [], [])


# -------------------------------------------------
# Smart Ask route
# -------------------------------------------------
@reports_bp.route("/ask", methods=["POST"])
@require_server
def ask():
    """Natural language question → Anthropic API → SQL, plain answer, or navigation."""
    question = (request.json or {}).get("question", "").strip()
    if not question:
        return jsonify({"type": "error", "text": "No question provided."}), 400

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"type": "error", "text": "Anthropic API key not configured."}), 500

    # Load sitemap fresh from disk on every request — no caching
    sitemap_text = ""
    try:
        sitemap_data = json.loads(SITEMAP_PATH.read_text(encoding="utf-8"))
        sitemap_text = "\n\nSITEMAP:\n" + json.dumps(sitemap_data, indent=2)
    except Exception:
        pass  # Sitemap missing or unreadable — degrade gracefully

    system_prompt = DB_SCHEMA + sitemap_text

    # Call Anthropic API
    try:
        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [{"role": "user", "content": question}]
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01"
            },
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))

        raw = response_data["content"][0]["text"].strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        parsed = json.loads(raw)

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        current_app.logger.error(f"reports: API error {e.code}")
        return jsonify({"type": "error", "text": f"API error {e.code}"}), 500
    except Exception as e:
        current_app.logger.exception("reports: Anthropic API unreachable")
        return jsonify({"type": "error", "text": "Failed to contact AI API"}), 500

    # Handle plain answer
    if parsed.get("type") == "answer":
        return jsonify({"type": "answer", "text": parsed.get("text", "No answer returned.")})

    # Handle navigation
    if parsed.get("type") == "navigate":
        return jsonify({
            "type": "navigate",
            "text": parsed.get("text", ""),
            "url":  parsed.get("url", "")
        })

    # Handle SQL query
    if parsed.get("type") == "query":
        sql = parsed.get("sql", "").strip()
        explanation = parsed.get("explanation", "")

        # Safety check — only allow SELECT
        if not sql.upper().startswith("SELECT"):
            return jsonify({"type": "error", "text": "Only SELECT queries are permitted."})

        try:
            result = db.session.execute(text(sql))
            rows = [dict(row._mapping) for row in result.fetchall()]

            # Normalise datetimes
            for row in rows:
                for key, val in row.items():
                    if isinstance(val, datetime):
                        row[key] = format_display_dt(val)
                    elif isinstance(val, date):
                        row[key] = val.strftime("%d-%m-%Y")

            columns = list(rows[0].keys()) if rows else []

            return jsonify({
                "type": "query",
                "explanation": explanation,
                "columns": columns,
                "rows": rows
            })

        except Exception as e:
            current_app.logger.exception("reports: query failed")
            return jsonify({"type": "error", "text": "Query failed"}), 500

    return jsonify({"type": "error", "text": "Unexpected response from AI."}), 500


# -------------------------------------------------
# Routes
# -------------------------------------------------
@reports_bp.route("/")
def reports_index():
    """Alias/index that accepts ?report=... or ?report_name=... and redirects."""
    name = (
        request.args.get("report")
        or request.args.get("report_name")
        or "logged_airports"
    )
    routes = {
        "logged_airports": "reports.report_logged_airports",
        "most_seen_aircraft": "reports.most_seen_aircraft",
        "top_airlines": "reports.top_airlines",
        "most_frequent_routes": "reports.most_frequent_routes",
        "top_10_busiest_airports": "reports.top_10_busiest_airports",
        "first_time_sightings": "reports.first_time_sightings",
        "rare_airlines": "reports.rare_airlines",
        "top_countries": "reports.top_countries",
        "most_common_models": "reports.most_common_models",
        "oldest_aircraft": "reports.oldest_aircraft",
        "different_aircraft": "reports.different_aircraft_report",
        "orphaned_aircraft": "reports.orphaned_aircraft",
    }
    endpoint = routes.get(name, "reports.report_logged_airports")
    return redirect(url_for(endpoint))


@reports_bp.route("/logged_airports")
def report_logged_airports():
    try:
        flight_count = db.session.execute(text("SELECT COUNT(*) FROM flights")).scalar()
        if not flight_count:
            flash("No flights found to generate the report.", "info")
            return render_report("Logged Airports", ["Airport", "AirportName", "Country"], [])

        query = text("""
            SELECT DISTINCT a.ICAO AS Airport, a.AirportName, a.Country
            FROM airports a
            JOIN (
                SELECT DISTINCT Departure AS ICAO FROM flights
                UNION
                SELECT DISTINCT Arrival AS ICAO FROM flights
            ) f ON a.ICAO = f.ICAO
            WHERE a.AirportName IS NOT NULL AND a.AirportName != ''
            ORDER BY a.Country, a.AirportName
        """)

        with db.engine.connect() as conn:
            result = conn.execute(query)
            columns = result.keys()
            airports = [dict(zip(columns, row)) for row in result]

    except Exception as e:
        import logging
        logging.error(f"❌ Error generating Logged Airports report: {e}")
        flash("Failed to load Logged Airports report.", "danger")
        airports = []

    return render_report("Logged Airports", ["Airport", "AirportName", "Country"], airports)


@reports_bp.route("/most_seen_aircraft")
def most_seen_aircraft():
    return safe_query(
        """
        SELECT Registration, COUNT(*) AS TimesSeen
        FROM flights
        GROUP BY Registration
        ORDER BY TimesSeen DESC
        LIMIT 20
        """,
        title="Most Seen Aircraft",
        columns=["Registration", "TimesSeen"],
    )


@reports_bp.route("/top_airlines")
def top_airlines():
    return safe_query(
        """
        SELECT a.AirlineName, COUNT(*) AS Flights
        FROM flights f
        LEFT JOIN airlines a ON f.AirlineID = a.AirlineID
        WHERE a.AirlineName IS NOT NULL
        GROUP BY a.AirlineName
        ORDER BY Flights DESC
        LIMIT 20
        """,
        title="Top Airlines",
        columns=["AirlineName", "Flights"],
    )


@reports_bp.route("/most_frequent_routes")
def most_frequent_routes():
    return safe_query(
        """
        SELECT a1.AirportName AS Departure,
               a2.AirportName AS Arrival,
               COUNT(*) AS Flights
        FROM flights f
        INNER JOIN airports a1 ON f.Departure = a1.ICAO
        INNER JOIN airports a2 ON f.Arrival = a2.ICAO
        WHERE a1.AirportName IS NOT NULL AND a1.AirportName != ''
          AND a2.AirportName IS NOT NULL AND a2.AirportName != ''
        GROUP BY f.Departure, f.Arrival
        ORDER BY Flights DESC
        LIMIT 20
        """,
        title="Most Frequent Routes",
        columns=["Departure", "Arrival", "Flights"],
    )


@reports_bp.route("/top_10_busiest_airports")
def top_10_busiest_airports():
    return safe_query(
        """
        SELECT a.AirportName AS Airport, airport_stats.Count
        FROM (
            SELECT Airport, COUNT(*) AS Count
            FROM (
                SELECT Departure AS Airport FROM flights WHERE Departure IS NOT NULL
                UNION ALL
                SELECT Arrival AS Airport FROM flights WHERE Arrival IS NOT NULL
            ) AS Combined
            GROUP BY Airport
        ) AS airport_stats
        INNER JOIN airports a ON airport_stats.Airport = a.ICAO
        WHERE a.AirportName IS NOT NULL AND a.AirportName != ''
        ORDER BY airport_stats.Count DESC
        LIMIT 10
        """,
        title="Top 10 Busiest Airports",
        columns=["Airport", "Count"],
    )


@reports_bp.route("/first_time_sightings")
def first_time_sightings():
    return safe_query(
        """
        SELECT Registration, First_Sighted
        FROM aircraft
        LIMIT 20
        """,
        title="First-Time Sightings",
        columns=["Registration", "First_Sighted"],
    )


@reports_bp.route("/rare_airlines")
def rare_airlines():
    return safe_query(
        """
        SELECT a.AirlineName, COUNT(*) AS Sightings
        FROM flights f
        LEFT JOIN airlines a ON f.AirlineID = a.AirlineID
        GROUP BY a.AirlineName
        HAVING Sightings <= 2
        ORDER BY Sightings ASC
        LIMIT 20
        """,
        title="Rare Airline Sightings",
        columns=["AirlineName", "Sightings"],
    )


@reports_bp.route("/top_countries")
def top_countries():
    return safe_query(
        """
        SELECT Country_of_Reg, COUNT(*) AS Count
        FROM aircraft
        WHERE Country_of_Reg IS NOT NULL AND Country_of_Reg != ''
        GROUP BY Country_of_Reg
        """,
        title="Top Countries of Registration",
        columns=["Country_of_Reg", "Count"],
    )


@reports_bp.route("/most_common_models")
def most_common_models():
    return safe_query(
        """
        SELECT Aircraft_Type, Country_of_Reg, COUNT(*) AS Count
        FROM aircraft
        WHERE Aircraft_Type IS NOT NULL AND Country_of_Reg IS NOT NULL
        GROUP BY Aircraft_Type, Country_of_Reg
        ORDER BY Count DESC
        """,
        title="Most Common Models Per Country",
        columns=["Aircraft_Type", "Country_of_Reg", "Count"],
    )


@reports_bp.route("/oldest_aircraft")
def oldest_aircraft():
    return safe_query(
        """
        SELECT Registration, Aircraft_Type, Manufacture_Year,
               YEAR(CURDATE()) - Manufacture_Year AS Age
        FROM aircraft
        WHERE Manufacture_Year IS NOT NULL AND Manufacture_Year != ''
        ORDER BY Manufacture_Year ASC
        LIMIT 20
        """,
        title="Oldest Aircraft Still in Service",
        columns=["Registration", "Aircraft_Type", "Manufacture_Year", "Age"],
    )


@reports_bp.route("/different_aircraft", methods=["GET"])
def different_aircraft_report():
    return safe_query(
        """
        SELECT Aircraft_Type, COUNT(*) AS Total
        FROM aircraft
        WHERE Aircraft_Type IS NOT NULL AND Aircraft_Type != ''
        GROUP BY Aircraft_Type
        ORDER BY Total DESC
        """,
        title="Different Aircraft Types",
        columns=["Aircraft_Type", "Total"],
    )


@reports_bp.route("/orphaned_aircraft", methods=["GET"])
def orphaned_aircraft():
    return safe_query(
        """
        SELECT ac.Registration, ac.Aircraft_Type, ac.Country_of_Reg, ac.First_Sighted
        FROM aircraft ac
        LEFT JOIN airlines al ON ac.AirlineID = al.AirlineID
        WHERE ac.AirlineID IS NULL OR al.AirlineID IS NULL
        ORDER BY ac.Registration ASC
        """,
        title="Orphaned Aircraft (No Airline Assigned)",
        columns=["Registration", "Aircraft_Type", "Country_of_Reg", "First_Sighted"],
    )


@reports_bp.route("/support")
def support():
    return render_template("support.html")
