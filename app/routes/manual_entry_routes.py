 # AirTrack 1.0.0
# Copyright (c) 2025 Trevor (“Subhuti”). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary-NC

import re

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import text

from extensions import db
from utils.settings_utils import get_current_theme

manual_entry_bp = Blueprint(
    "manual_entry",
    __name__,
    url_prefix="/admin/manual-aircraft",
)

AIRTRACK_COLUMNS = [
    "registration",
    "hexcode",
    "aircraftmanufacturer",
    "aircraftmodel",
    "msn",
    "maxtakeoffweight",
    "enginecount",
    "enginemanufacturer",
    "enginetype",
    "enginemodel",
    "fueltype",
    "registrationtype",
    "registeredowner",
    "registeredownercountry",
    "operatorname",
    "operatorcountry",
    "firstregistrationdate",
    "airframe",
    "propmanu",
    "propmodel",
    "typecert",
    "countrymanu",
    "yearmanu",
    "monthmanu",
    "icaotypedesig",
]

PREFIX_TABLES = {
    "VH": "australia",
    "P2": "papua_new_guinea",
    "ZK": "new_zealand",
    "G": "uk",
    "C": "canada",
    "N": "usa",
    "4R": "sri_lanka",
}


def normalize_registration(registration):
    reg = (registration or "").strip().upper()
    if reg.startswith("N-"):
        reg = reg.replace("-", "", 1)
    return reg


def clean_table_name(name):
    table = (name or "").strip().lower()
    table = table.replace(" ", "_").replace("-", "_")
    table = re.sub(r"[^a-z0-9_]", "", table)
    # Explicit guard: return empty string if result isn't a safe identifier.
    # The re.sub above already removes all unsafe chars; this assertion makes
    # the sanitization explicit for static analysis tools.
    return table if re.fullmatch(r'[a-z0-9_]+', table) else ""


def detect_country_table(registration):
    reg = normalize_registration(registration)

    for prefix, table in sorted(PREFIX_TABLES.items(), key=lambda x: len(x[0]), reverse=True):
        if reg.startswith(prefix):
            return table

    return "unknown"


def ensure_country_table(table_name):
    table = clean_table_name(table_name)
    if not table:
        table = "unknown"

    db.session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS `{table}` (
                `registration` varchar(10) NOT NULL,
                `aircraftmanufacturer` varchar(100) DEFAULT NULL,
                `aircraftmodel` varchar(50) DEFAULT NULL,
                `msn` varchar(50) DEFAULT NULL,
                `maxtakeoffweight` int(11) DEFAULT NULL,
                `enginecount` int(11) DEFAULT NULL,
                `enginemanufacturer` varchar(100) DEFAULT NULL,
                `enginetype` varchar(50) DEFAULT NULL,
                `enginemodel` varchar(50) DEFAULT NULL,
                `fueltype` varchar(50) DEFAULT NULL,
                `registrationtype` varchar(50) DEFAULT NULL,
                `registeredowner` varchar(150) DEFAULT NULL,
                `registeredownercountry` varchar(50) DEFAULT NULL,
                `operatorname` varchar(150) DEFAULT NULL,
                `operatorcountry` varchar(50) DEFAULT NULL,
                `firstregistrationdate` date DEFAULT NULL,
                `airframe` varchar(100) DEFAULT NULL,
                `propmanu` varchar(100) DEFAULT NULL,
                `propmodel` varchar(50) DEFAULT NULL,
                `typecert` varchar(50) DEFAULT NULL,
                `countrymanu` varchar(50) DEFAULT NULL,
                `yearmanu` int(11) DEFAULT NULL,
                `icaotypedesig` varchar(10) DEFAULT NULL,
                PRIMARY KEY (`registration`)
            )
            """
        )
    )
    db.session.commit()
    return table


def table_exists(table_name):
    table = clean_table_name(table_name)
    if not table:
        return False

    row = db.session.execute(
        text(
            """
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
            AND table_name = :table
            """
        ),
        {"table": table},
    ).scalar()

    return bool(row)


def fetch_aircraft(table_name, registration):
    table = clean_table_name(table_name)
    reg = normalize_registration(registration)

    if not table or not reg or not table_exists(table):
        return None

    # Validate table against information_schema to get a DB-sourced name
    _trow = db.session.execute(
        text("SELECT TABLE_NAME FROM information_schema.TABLES "
             "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :tn"),
        {"tn": table},
    ).fetchone()
    if not _trow:
        return None
    db_table = _trow[0]  # DB-sourced, not request-tainted
    row = db.session.execute(
        text(f"SELECT * FROM `{db_table}` WHERE registration = :registration LIMIT 1"),
        {"registration": reg},
    ).mappings().fetchone()

    return dict(row) if row else None


def upsert_aircraft(table_name, form_data):
    table = ensure_country_table(table_name)

    cleaned = {}
    for col in AIRTRACK_COLUMNS:
        value = (form_data.get(col) or "").strip()
        cleaned[col] = value or None

    cleaned["registration"] = normalize_registration(cleaned.get("registration"))

    if not cleaned["registration"]:
        raise ValueError("Registration is required.")

    int_fields = ["maxtakeoffweight", "enginecount", "yearmanu"]
    for field in int_fields:
        if cleaned.get(field):
            try:
                cleaned[field] = int(cleaned[field])
            except ValueError:
                cleaned[field] = None

    columns = ", ".join(f"`{col}`" for col in AIRTRACK_COLUMNS)
    values = ", ".join(f":{col}" for col in AIRTRACK_COLUMNS)
    updates = ", ".join(
        f"`{col}` = VALUES(`{col}`)"
        for col in AIRTRACK_COLUMNS
        if col != "registration"
    )

    db.session.execute(
        text(
            f"""
            INSERT INTO `{table}` ({columns})
            VALUES ({values})
            ON DUPLICATE KEY UPDATE
                {updates}
            """
        ),
        cleaned,
    )
    db.session.commit()

    return table, cleaned["registration"]


@manual_entry_bp.route("/", methods=["GET", "POST"])
def manual_aircraft_entry():
    if request.method == "POST":
        try:
            form_data = request.form.to_dict()
            registration = normalize_registration(form_data.get("registration"))

            override_table = clean_table_name(form_data.get("override_table"))
            detected_table = detect_country_table(registration)
            target_table = override_table or detected_table

            table, reg = upsert_aircraft(target_table, form_data)

            flash(f"✅ {reg} saved to `{table}`.", "success")
            return redirect(url_for("manual_entry.manual_aircraft_entry"))

        except Exception as exc:
            db.session.rollback()
            flash(f"❌ Manual aircraft save failed: {exc}", "danger")
            return redirect(url_for("manual_entry.manual_aircraft_entry"))

    return render_template(
        "manual_aircraft_entry.html",
        columns=AIRTRACK_COLUMNS,
        selected_theme=get_current_theme(),
    )


@manual_entry_bp.route("/lookup")
def lookup_aircraft():
    registration = normalize_registration(request.args.get("registration"))
    override_table = clean_table_name(request.args.get("table"))

    if not registration:
        return jsonify(
            {
                "ok": False,
                "found": False,
                "error": "Registration is required.",
            }
        ), 400

    detected_table = detect_country_table(registration)
    target_table = override_table or detected_table

    row = fetch_aircraft(target_table, registration)

    return jsonify(
        {
            "ok": True,
            "found": bool(row),
            "registration": registration,
            "detected_table": detected_table,
            "target_table": target_table,
            "data": row or {},
        }
    )
