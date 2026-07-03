# AirTrack 1.0.0
# Copyright (c) 2025 Trevor (“Subhuti”). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary-NC



# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from version import FULL_VERSION, SHORT_VERSION, DISPLAY_VERSION

# ⚠️ DO NOT IMPORT MODELS YET — db must be initialised first
# from models.billing_models import Customer, License, LicenseActivity  # noqa: F401

from utils.airport_utils import format_airport_display

from security.server_webauthn import server_webauthn

from utils.stats_utils import get_airtrack_stats, get_all_airlines

from utils.logging_utils import log_admin_action

from utils.settings_utils import convert_to_local, get_current_theme

from utils.country_flags import get_country_flag

from utils import theme_scanner, jinja_filters

from extensions import db

import logging

import os

import subprocess

import sys

from datetime import datetime, timezone

from logging.handlers import RotatingFileHandler

from pathlib import Path

from time import time

import pytz

from dotenv import load_dotenv

from flask import (
    Flask,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
    has_app_context,
)

from flask_wtf.csrf import CSRFProtect, generate_csrf

from sqlalchemy import text

# Ensure this directory is importable as 'app'
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# ---------------------------------------------------------------------------
# Bootstrap / Flask app setup
# ---------------------------------------------------------------------------
load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=str(Path(__file__).with_name('templates')),
    static_folder=str(Path(__file__).with_name('static')),
)

# ---------------------------------------------------------
# License load (client + server safe)
# ---------------------------------------------------------

try:
    if os.getenv('AIRTRACK_ROLE') == "client":
        from config.license import load_license

        license_data = load_license()

        if license_data:
            print("✔ License loaded")
            print(f"Customer: {license_data.name}")
            print(f"Edition: {license_data.edition}")
            print(f"License ID: {license_data.license_id}")
            print(f"Issued: {license_data.issued}")
        else:
            print("⚠ No license detected")

except Exception as e:
    print(f"⚠ License system not available: {e}")


# (Your config stays where it already is, example shown)
# app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URI')
# app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# etc...

# ---------------------------------------------------------------------------
# Database configuration must be set BEFORE db.init_app(app)
# ---------------------------------------------------------------------------
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URI') or \
    f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@" \
    f"{os.getenv('DB_HOST')}/{os.getenv('DB_NAME')}"

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


# ---------------------------------------------------------------------------
# 🔥 Initialise DB before importing models
# ---------------------------------------------------------------------------
db.init_app(app)

# ---------------------------------------------------------------------------
# 🔄 Run database migrations
# ---------------------------------------------------------------------------
from utils.migration_runner import run_migrations
with app.app_context():
    run_migrations(db)

# ---------------------------------------------------------------------------
# 📌 Now import billing models safely
# ---------------------------------------------------------------------------
try:
    from models.billing_models import Customer, License, LicenseActivity
except ImportError:
    Customer = None
    License = None
    LicenseActivity = None
# ... rest of your app remains unchanged below ...


@app.context_processor

def inject_airtrack_version():
    return {
        "AIRTRACK_VERSION": FULL_VERSION,
        "AIRTRACK_DISPLAY_VERSION": DISPLAY_VERSION,
    }

# Register custom Jinja filters
jinja_filters.register_filters(app)

# CSRF setup
csrf = CSRFProtect()
app.config.update(
    WTF_CSRF_TIME_LIMIT=None,
    WTF_CSRF_SSL_STRICT=False,
    WTF_CSRF_HEADERS=['X-CSRFToken'],
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False,
)

csrf.init_app(app)

# ---------------------------------------------------------------------------
# Context processors
# ---------------------------------------------------------------------------

@app.context_processor

def inject_app_context():
    return {"current_app": current_app, "config": current_app.config}


@app.context_processor

def inject_time():
    from datetime import datetime
    return {"time": time, "current_year": datetime.utcnow().year}


@app.context_processor

def inject_env_vars():
    try:
        return {
            "AIRTRACK_UPDATE_MODE": os.getenv("AIRTRACK_UPDATE_MODE", ""),
            "AIRTRACK_SYNC_USER":   os.getenv("AIRTRACK_SYNC_USER", ""),
            "aria_enabled":         os.getenv("ARIA_ENABLED", "0").lower() in ("1", "true", "yes"),
        }
    except Exception:
        return {}


@app.context_processor

def inject_settings():
    """Expose app_settings rows as `settings` in templates."""
    try:
        with db.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT SettingKey, SettingValue FROM app_settings")
            ).fetchall()
            return {"settings": {row[0]: row[1] for row in rows}}
    except Exception:
        return {"settings": {}}


# Make csrf_token() available in templates
app.jinja_env.globals['csrf_token'] = generate_csrf

# Global flag helper
app.jinja_env.globals["get_country_flag"] = get_country_flag

# Register airport display formatter as a Jinja filter
app.jinja_env.filters["airport"] = format_airport_display

# Also make time() available globally
app.jinja_env.globals["time"] = time

# Register WebAuthn blueprint
app.register_blueprint(server_webauthn)

# ---------------------------------------------------------------------------
# Configuration (DB / debug / secrets)
# ---------------------------------------------------------------------------
app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}/{os.getenv('DB_NAME')}?charset=utf8mb4"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["DEBUG"] = True
app.config["TESTING"] = False

# Fallback secret key
app.secret_key = os.getenv("SECRET_KEY", "fallback-hardcoded-key")


# ---------------------------------------------------------------------------
# Theme refresh hook
# ---------------------------------------------------------------------------
@app.before_request

def _themes_auto_refresh():
    pass


# ---------------------------------------------------------------------------
# Timezone helpers + logging formatter
# ---------------------------------------------------------------------------

def get_backup_dir() -> Path:
    """
    Returns the *correct* backup directory inside the container.

    Always returns a Path, never None.
    """
    # Inside Docker, backups MUST go here
    base = Path("/app/backups")

    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.error(f"❌ Failed to create backup directory {base}: {e}")

    return base


def _safe_tz(name: str | None):
    try:
        return pytz.timezone(name) if name else pytz.utc
    except Exception:
        logging.warning("Invalid timezone '%s', defaulting to UTC", name)
        return pytz.utc


def get_app_timezone():
    try:
        if not has_app_context():
            return pytz.utc
        with db.engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT SettingValue FROM app_settings "
                    "WHERE SettingKey='timezone' LIMIT 1"
                )
            ).scalar()
            return _safe_tz(result)
    except Exception:
        return pytz.utc


class DBTimezoneFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None):
        super().__init__(fmt, datefmt)
        self.tz = None

    def get_timezone(self):
        if self.tz is not None:
            return self.tz
        if not has_app_context():
            return pytz.utc
        try:
            with db.engine.connect() as conn:
                result = conn.execute(
                    text(
                        "SELECT SettingValue FROM app_settings "
                        "WHERE SettingKey='timezone' LIMIT 1"
                    )
                ).scalar()
                self.tz = _safe_tz(result)
        except Exception:
            self.tz = pytz.utc
        return self.tz

    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=pytz.utc).astimezone(
            self.get_timezone()
        )

    def formatTime(self, record, datefmt=None):
        dt = self.converter(record.created)
        return dt.strftime(datefmt) if datefmt else dt.isoformat()


formatter = DBTimezoneFormatter(
    "%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
else:
    for h in root_logger.handlers:
        try:
            h.setFormatter(formatter)
        except Exception:
            pass

# File logging
_default_logs = Path(BASE_DIR) / "logs"
LOGS_DIR = Path(os.getenv("AIRTRACK_LOG_DIR", str(_default_logs))).resolve()
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_file_handler(logger, filename):
    target = (LOGS_DIR / filename).resolve()
    for h in logger.handlers:
        if isinstance(h, RotatingFileHandler) and getattr(
            h, "baseFilename", None
        ) == str(target):
            return
    handler = RotatingFileHandler(
        str(target),
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)


_ensure_file_handler(logging.getLogger(), "airtrack.log")
admin_logger = logging.getLogger("airtrack.admin")
admin_logger.setLevel(logging.INFO)
_ensure_file_handler(admin_logger, "admin_activity.log")

logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("werkzeug").propagate = False

# ---------------------------------------------------------------------------
# Default timezone seed
# ---------------------------------------------------------------------------
with app.app_context():
    try:
        with db.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO app_settings (SettingKey, SettingValue)
                    VALUES ('timezone', 'Australia/Sydney')
                    ON DUPLICATE KEY UPDATE SettingValue = SettingValue
                """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT IGNORE INTO settings (id, show_disclaimer)
                    VALUES (1, 1)
                """
                )
            )
    except Exception as e:
        logging.warning("Could not seed defaults: %s", e)

# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def inject_globals():
    return {"max": max, "min": min}


@app.context_processor
def inject_endpoint_check():
    """Expose endpoint_exists() to all templates for graceful nav degradation."""
    from flask import current_app
    def endpoint_exists(name):
        return name in current_app.url_map._rules_by_endpoint
    return dict(endpoint_exists=endpoint_exists)


@app.template_filter("format_datetime")

def format_datetime(value):
    if value is None:
        return "N/A"
    if isinstance(value, datetime):
        sydney = pytz.timezone("Australia/Sydney")
        return value.astimezone(sydney).strftime("%d %B %Y - %H:%M:%S")
    return value


# ---------------------------------------------------------------------------
# Routes: splash + index
# ---------------------------------------------------------------------------

@app.route("/")

def root():
    return redirect(url_for("splash"))


@app.route("/splash")

def splash():
    return render_template("splash.html", current_time=time())


@app.route("/reports")

def reports():
    try:
        with db.engine.connect() as conn:
            result = conn.execute(
                text("SELECT show_disclaimer FROM settings WHERE id=1")
            ).fetchone()
            show_disclaimer = result[0] if result else True
        now = convert_to_local(datetime.utcnow())
        # NOTE: original code didn’t return; leaving behaviour minimal
        # so we don’t change functional behaviour unexpectedly.
        return render_template(
            "reports.html",
            show_disclaimer=show_disclaimer,
            now=now,
            is_server=os.getenv("AIRTRACK_ROLE", "").lower() == "server",
            has_api_key=bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
            license=license_data if 'license_data' in globals() else None,
            selected_theme=get_current_theme(),
            cache_bust=int(time()),
        )
    except Exception as e:
        logging.error("❌ Error checking disclaimer: %s", e)
        return "Internal Server Error", 500


@app.route("/index")

def index():
    now = convert_to_local(datetime.utcnow())
    current_year = now.year if now else datetime.utcnow().year

    try:
        page = 1
        per_page = 10
        filtered_aircraft = []
        selected_airline_id = request.form.get("airline_id") if request.method=="POST" else request.args.get("airline_id")

        with db.engine.connect() as conn:
            total_airlines = conn.execute(text("SELECT COUNT(*) FROM airlines")).scalar()
            total_aircraft = conn.execute(text("SELECT COUNT(*) FROM aircraft")).scalar()
            airlines_list = get_all_airlines()

            settings = {row[0]: row[1] for row in conn.execute(text("SELECT SettingKey, SettingValue FROM app_settings")).fetchall()}

            if selected_airline_id:
                res = conn.execute(text("SELECT AirlineName FROM airlines WHERE AirlineID=:id"), {"id": selected_airline_id}).fetchone()
                selected_airline_name = res[0] if res else None
            else:
                selected_airline_name = None

        return render_template(
            "index.html",
            total_airlines=total_airlines,
            total_aircraft=total_aircraft,
            airlines=airlines_list,
            filtered_aircraft=filtered_aircraft,
            selected_airline_name=selected_airline_name,
            filtered_total_aircraft=0,
            no_aircraft_message="",
            current_page=page,
            total_pages=1,
            current_year=current_year,
            settings=settings,
        )

    except Exception as e:
        logging.exception("❌ Index error: %s", e)
        flash("An error occurred.", "danger")
        return render_template(
            "index.html",
            airlines=[], total_airlines=0, total_aircraft=0,
            filtered_aircraft=[], filtered_total_aircraft=0,
            current_page=1, total_pages=1,
            selected_airline_name="",
            no_aircraft_message="", current_year=current_year,
            settings={},
        )


# ---------------------------------------------------------------------------
# Aircraft helper routes
# ---------------------------------------------------------------------------

@app.route("/get_registrations/<int:airline_id>")

def get_registrations(airline_id):
    try:
        with db.engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT Registration
                    FROM aircraft
                    WHERE AirlineID=:airline_id
                    ORDER BY Registration ASC
                """
                ),
                {"airline_id": airline_id},
            ).fetchall()
        return jsonify([r[0] for r in rows])
    except Exception as e:
        app.logger.error("❌ Registration error: %s", e)
        return jsonify([]), 500


@app.route("/delete_flight/<int:flight_id>", methods=["POST"])
@csrf.exempt

def delete_flight(flight_id):
    try:
        with db.engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT Registration "
                    "FROM flights WHERE FlightID=:id"
                ),
                {"id": flight_id},
            ).fetchone()
            if not row:
                flash("Flight not found.", "warning")
                return redirect(url_for("index"))

            reg = row[0]
            conn.execute(
                text(
                    "DELETE FROM flights "
                    "WHERE FlightID=:id"
                ),
                {"id": flight_id},
            )

            ac = conn.execute(
                text(
                    "SELECT AircraftID "
                    "FROM aircraft WHERE Registration=:r LIMIT 1"
                ),
                {"r": reg},
            ).fetchone()
            ac_id = ac[0] if ac else None

        flash("Flight deleted.", "success")
        return redirect(
            url_for("aircraft.aircraft_info", aircraft_id=ac_id)
            if ac_id
            else url_for("aircraft.aircraft_table")
        )
    except Exception as e:
        flash(f"Error deleting flight: {e}", "danger")
        return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Edit Flight – /edit_flight/<flight_id>
# ---------------------------------------------------------------------------
@app.route("/edit_flight/<int:flight_id>", methods=["GET", "POST"])
@csrf.exempt
def edit_flight(flight_id):
    try:
        row = db.session.execute(
            text(
                """
                SELECT FlightID, AircraftID, FlightNumber, Departure, Arrival,
                       Timestamp, Spotted_At, Notes, Registration
                FROM flights
                WHERE FlightID = :id
                """
            ),
            {"id": flight_id},
        ).fetchone()

        if not row:
            flash("Flight not found.", "warning")
            return redirect(url_for("index"))

        flight = dict(row._mapping)

        # Look up aircraft_id for redirect
        ac_row = db.session.execute(
            text("SELECT AircraftID FROM aircraft WHERE Registration = :reg LIMIT 1"),
            {"reg": flight["Registration"]},
        ).fetchone()
        aircraft_id = ac_row[0] if ac_row else None

        if request.method == "POST":
            flight_number = (request.form.get("FlightNumber") or "").strip().upper()
            departure     = (request.form.get("Departure") or "").strip().upper()
            arrival       = (request.form.get("Arrival") or "").strip().upper()
            spotted_at    = (request.form.get("Spotted_At") or "").strip()
            notes         = (request.form.get("Notes") or "").strip()
            timestamp_raw = (request.form.get("Timestamp") or "").strip()

            try:
                timestamp = datetime.strptime(timestamp_raw, "%Y-%m-%dT%H:%M") if timestamp_raw else flight["Timestamp"]
            except ValueError:
                timestamp = flight["Timestamp"]

            db.session.execute(
                text(
                    """
                    UPDATE flights SET
                        FlightNumber = :FlightNumber,
                        Departure    = :Departure,
                        Arrival      = :Arrival,
                        Spotted_At   = :Spotted_At,
                        Notes        = :Notes,
                        Timestamp    = :Timestamp
                    WHERE FlightID = :FlightID
                    """
                ),
                {
                    "FlightNumber": flight_number or None,
                    "Departure":    departure or None,
                    "Arrival":      arrival or None,
                    "Spotted_At":   spotted_at or None,
                    "Notes":        notes or None,
                    "Timestamp":    timestamp,
                    "FlightID":     flight_id,
                },
            )
            db.session.commit()
            flash("Flight updated successfully.", "success")
            return redirect(
                url_for("aircraft.aircraft_info", aircraft_id=aircraft_id)
                if aircraft_id
                else url_for("aircraft.aircraft_table")
            )

    except Exception as e:
        db.session.rollback()
        flash(f"Error loading flight: {e}", "danger")
        return redirect(url_for("index"))

    from utils.settings_utils import get_current_theme
    return render_template(
        "edit_flight.html",
        flight=flight,
        aircraft_id=aircraft_id,
        selected_theme=get_current_theme(),
    )


@app.route("/delete_aircraft/<int:aircraft_id>", methods=["POST"])
@csrf.exempt

def delete_aircraft(aircraft_id):
    try:
        with db.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM aircraft WHERE AircraftID=:id"),
                {"id": aircraft_id},
            )
        flash("Aircraft deleted.", "success")
    except Exception as e:
        logging.error("❌ Delete aircraft error: %s", e)
        flash("Failed to delete aircraft.", "danger")
    return redirect(url_for("aircraft.aircraft_table"))


@app.route("/orphaned_aircraft")

def orphaned_aircraft():
    try:
        with db.engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM aircraft "
                    "WHERE AirlineID IS NULL"
                )
            ).scalar()
            rows = conn.execute(
                text(
                    """
                    SELECT a.*, al.AirlineName
                    FROM aircraft a
                    LEFT JOIN airlines al
                        ON a.AirlineID = al.AirlineID
                    ORDER BY a.Aircraft_Updated DESC
                """
                )
            ).fetchall()
            orphaned_list = [dict(r._mapping) for r in rows]
    except Exception as e:
        logging.error("❌ Orphaned error: %s", e)
        flash("Failed to load orphaned aircraft.", "danger")
        return redirect(url_for("index"))

    return render_template(
        "orphaned_aircraft.html",
        orphaned_aircraft=orphaned_list,
        orphaned_aircraft_count=count,
    )


@app.route("/flight_history/<int:aircraft_id>")

def flight_history(aircraft_id):
    try:
        with db.engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT *
                    FROM flights
                    WHERE AircraftID=:aircraft_id
                    ORDER BY Timestamp DESC
                """
                ),
                {"aircraft_id": aircraft_id},
            )
            cols = result.keys()
            history = [dict(zip(cols, r)) for r in result.fetchall()]

            reg = conn.execute(
                text(
                    "SELECT Registration FROM aircraft "
                    "WHERE AircraftID=:id"
                ),
                {"id": aircraft_id},
            ).fetchone()

            aircraft = {"Registration": reg[0]} if reg else {}

            for f in history:
                ts = f.get("Timestamp") or f.get("timestamp")
                f["Spotted_At"] = f.get("Spotted_At") or "Unknown"
                f["local_timestamp"] = (
                    convert_to_local(ts).strftime("%d-%m-%Y %H:%M")
                    if isinstance(ts, datetime)
                    else "N/A"
                )

        return render_template(
            "flight_history.html",
            history=history,
            aircraft=aircraft,
        )

    except Exception as e:
        logging.exception("❌ Flight history error: %s", e)
        flash("Failed to load flight history.", "danger")
        return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Airline admin routes
# ---------------------------------------------------------------------------
@app.route("/edit_airline/<int:airline_id>", methods=["GET", "POST"])

def edit_airline(airline_id):
    try:
        with db.engine.begin() as conn:
            airline = (
                conn.execute(
                    text(
                        "SELECT * FROM airlines "
                        "WHERE AirlineID=:a"
                    ),
                    {"a": airline_id},
                )
                .mappings()
                .fetchone()
            )

            if not airline:
                flash("Airline not found.", "danger")
                return redirect(url_for("airlines.airlines_table"))

            if request.method == "POST":
                name = request.form.get("airlineName", "").strip()
                if not name:
                    return redirect(
                        url_for("edit_airline", airline_id=airline_id)
                    )

                conn.execute(
                    text(
                        "UPDATE airlines "
                        "SET AirlineName=:n "
                        "WHERE AirlineID=:i"
                    ),
                    {"n": name, "i": airline_id},
                )
                flash("Airline updated.", "success")
                return redirect(url_for("airlines.airlines_table"))

        return render_template("edit_airline.html", airline=airline)

    except Exception as e:
        flash("Error editing airline.", "danger")
        return redirect(url_for("airlines.airlines_table"))


@app.route("/update_airline", methods=["POST"])

def update_airline():
    try:
        airline_id = request.form.get("airline_id")
        airline_name = request.form.get("airline_name", "").strip()

        if not airline_id or not airline_name:
            flash("Missing airline details.", "danger")
            return redirect(url_for("airlines.airlines_table"))

        with db.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE airlines "
                    "SET AirlineName=:n "
                    "WHERE AirlineID=:i"
                ),
                {"n": airline_name, "i": airline_id},
            )

        flash("Airline updated.", "success")
        return redirect(url_for("airlines.airlines_table"))

    except Exception as e:
        logging.error("❌ Airline update error: %s", e)
        flash("Error updating airline.", "danger")
        return redirect(url_for("airlines.airlines_table"))


@app.route("/delete_airline/<int:airline_id>", methods=["POST"])
@csrf.exempt

def delete_airline(airline_id):
    try:
        delete_option = request.form.get("delete_option")

        with db.engine.begin() as conn:
            if delete_option == "full":
                conn.execute(
                    text(
                        "DELETE FROM airlines WHERE AirlineID=:i"
                    ),
                    {"i": airline_id},
                )
                conn.execute(
                    text(
                        "DELETE FROM aircraft WHERE AirlineID=:i"
                    ),
                    {"i": airline_id},
                )
                flash("Airline and aircraft deleted.", "success")
            elif delete_option == "orphan":
                conn.execute(
                    text(
                        "UPDATE aircraft "
                        "SET AirlineID=NULL "
                        "WHERE AirlineID=:i"
                    ),
                    {"i": airline_id},
                )
                conn.execute(
                    text(
                        "DELETE FROM airlines WHERE AirlineID=:i"
                    ),
                    {"i": airline_id},
                )
                flash("Airline deleted; aircraft orphaned.", "success")
            else:
                flash("Invalid option.", "danger")

        return redirect(url_for("airlines.airlines_table"))

    except Exception as e:
        logging.error("❌ Airline delete error: %s", e)
        flash("Error deleting airline.", "danger")
        return redirect(url_for("airlines.airlines_table"))


# Disclaimer routes
# ---------------------------------------------------------------------------
@app.route("/get_disclaimer_status")

def get_disclaimer_status():
    try:
        with db.engine.connect() as conn:
            r = conn.execute(
                text(
                    "SELECT show_disclaimer FROM settings "
                    "WHERE id=1"
                )
            ).fetchone()
            return jsonify({"show_disclaimer": bool(r[0]) if r else True})
    except Exception as e:
        logging.error("❌ Disclaimer error: %s", e)
        return jsonify({"error": "Database error"}), 500


@app.route("/hide_disclaimer", methods=["POST"])
@csrf.exempt
def hide_disclaimer():
    try:
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE settings SET show_disclaimer=FALSE WHERE id=1")
            )
        return jsonify({"status": "ok"})
    except Exception as e:
        logging.error("❌ Disclaimer update error: %s", e)
        return jsonify({"error": "Database error"}), 500


# ---------------------------------------------------------------------------
# Admin: backup / restore / flush
# ---------------------------------------------------------------------------
@app.route("/admin/backup", methods=["POST"])

def backup_database():
    try:
        backup_dir = get_backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)

        tz = get_app_timezone()
        now = datetime.now(tz).strftime("%Y%m%d_%H%M%S")
        filename = f"airtrack_backup_{now}.sql"
        filepath = backup_dir / filename

        cmd = [
            "mysqldump",
            "-h",
            os.getenv("DB_HOST", "airtrack-db"),
            "-P",
            os.getenv("DB_PORT", "3306"),
            "-u",
            os.getenv("DB_USER", ""),
            f"-p{os.getenv('DB_PASSWORD', '')}",
            os.getenv("DB_NAME", "airtrack"),
        ]

        with open(filepath, "wb") as out:
            proc = subprocess.run(cmd, stdout=out)

        if proc.returncode != 0:
            filepath.unlink(missing_ok=True)
            raise RuntimeError("mysqldump failed")

        log_admin_action(f"Database backup saved to {filepath}")

    except Exception as e:
        logging.error("❌ Backup error: %s", e)
        flash("Backup failed.", "danger")

    return redirect(url_for("admin_panel"))


@app.route("/admin/restore", methods=["POST"])

def restore_database():
    try:
        backup_dir = get_backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        backups = sorted(
            backup_dir.glob("*.sql"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not backups:
            flash("No backups found.", "danger")
            return redirect(url_for("admin_panel"))

        latest = backups[0]

        log_admin_action("Database restored.")
        cmd = [
            "mysql",
            f"-h{os.getenv('DB_HOST', 'airtrack-db')}",
            f"-u{os.getenv('DB_USER', '')}",
            f"-p{os.getenv('DB_PASSWORD', '')}",
            os.getenv("DB_NAME", "airtrack"),
        ]
        with open(latest, "rb") as f:
            proc = subprocess.run(cmd, stdin=f)

        if proc.returncode == 0:
            flash(f"Restored from {latest.name}", "success")
        else:
            raise RuntimeError("mysql restore failed")
    except Exception as e:
        logging.error("❌ Restore error: %s", e)
        flash("Restore failed.", "danger")

    return redirect(url_for("admin_panel"))


@app.route("/admin/flush_backups", methods=["POST"])

def flush_backups():
    try:
        backup_dir = os.path.join(os.path.dirname(__file__), "backups")
        files = [f for f in os.listdir(backup_dir) if f.endswith(".sql")]

        if not files:
            flash("No backups found.", "info")
            return redirect(url_for("admin_panel"))

        for f in files:
            os.remove(os.path.join(backup_dir, f))

        log_admin_action("All backups deleted.")
        flash("Backups deleted.", "warning")

    except Exception as e:
        logging.error("❌ Flush error: %s", e)
        flash("Failed to delete backups.", "danger")

    return redirect(url_for("admin_panel"))


@app.route("/admin/flush_logs", methods=["POST"])

def flush_logs():
    try:
        open("logs/airtrack.log", "w").close()
        log_admin_action("Logs flushed.")
        flash("Logs flushed.", "success")
    except Exception:
        pass
    return redirect(url_for("admin_panel"))


@app.route("/admin/flush", methods=["POST"])

def flush_database():
    try:
        if request.form.get("confirm_flush") != "CONFIRM":
            flash("Flush aborted.", "danger")
            return redirect(url_for("admin_panel"))

        with db.engine.begin() as conn:
            conn.execute(text("DELETE FROM flights"))
            conn.execute(text("DELETE FROM aircraft"))
            conn.execute(text("DELETE FROM airlines"))

        log_admin_action("Database flushed.")
        flash("Database flushed.", "success")

    except Exception:
        flash("Failed to flush DB.", "danger")

    return redirect(url_for("admin_panel"))


# Admin dashboard
# ---------------------------------------------------------------------------
@app.route("/admin", methods=["GET", "POST"])

def admin_panel():
    try:
        stats = get_airtrack_stats()
    except Exception:
        stats = {}

    def _backup_dir():
        env = os.getenv("AIRTRACK_BACKUP_DIR")
        return Path(env).resolve() if env else Path("/app/logs/backups").resolve()

    backup_dir = _backup_dir()
    backup_files = []
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        for p in backup_dir.glob("*.sql"):
            st = p.stat()
            backup_files.append(
                {
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "modified": datetime.fromtimestamp(
                        st.st_mtime
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        backup_files.sort(key=lambda x: x["mtime"], reverse=True)
    except Exception as e:
        logging.error("❌ Backup listing error: %s", e)

    AIRTRACK_SYNC_USER = os.getenv("AIRTRACK_SYNC_USER", "").lower()
    update_mode = os.getenv("AIRTRACK_UPDATE_MODE", "remote").lower()
    show_update_button = False

    sync_route_name = (
        "sync_route"  # Placeholder, as original code references this variable but it's undefined
    )
    show_sync_button = (
        AIRTRACK_SYNC_USER == "trevor"
        and sync_route_name in current_app.view_functions
    )

    def _safe_url(name, **kwargs):
        try:
            if name in current_app.view_functions:
                return url_for(name, **kwargs)
        except Exception:
            pass
        return None

    airtrack_urls = {
        "check_updates": _safe_url("admin_tools.check_updates"),
        "run_updater": _safe_url("admin_tools.run_updater"),
        "git_commit": _safe_url("admin_tools.git_commit"),
        "git_push": _safe_url("admin_tools.git_push"),
        "housekeeping": _safe_url("admin_tools.housekeeping"),
        "shutdown": _safe_url("admin_tools.admin_shutdown"),
        "logs": _safe_url("admin_tools.logs"),
        "logs_view": _safe_url("admin_tools.logs_view"),
        "logs_download": _safe_url("admin_tools.logs_download", filename=""),
        "logs_tail": _safe_url("admin_tools.logs_tail"),
        "backup": _safe_url("backup_database"),
        "restore": _safe_url("restore_database"),
    }
    airtrack_urls = {k: v for k, v in airtrack_urls.items() if v}

    logging.warning("🧪 AIRTRACK_SYNC_USER = %s", AIRTRACK_SYNC_USER)
    logging.warning("🧪 AIRTRACK_UPDATE_MODE = %s", update_mode)

    return render_template(
        "admin.html",
        is_server=os.getenv("AIRTRACK_ROLE") == "server",
        stats=stats,
        backup_files=backup_files,
        show_update_button=show_update_button,
        show_sync_button=show_sync_button,
        AIRTRACK_SYNC_USER=AIRTRACK_SYNC_USER,
        airtrack_urls=airtrack_urls,
    )


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(404)

def page_not_found(e):  # noqa: ARG001
    return render_template("404.html"), 404


app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.route("/test_direct")

def test_direct():
    return "✅ Direct route from app.py is working"


# ---------------------------------------------------------------------------
# Blueprints
# ---------------------------------------------------------------------------
from routes.search_routes import search_unified_bp

from routes.add_aircraft_routes import add_aircraft_bp

from routes.admin_routes import admin_bp

from routes.admin_util_routes import admin_util_bp

from routes.aircraft_routes import aircraft_bp

from routes.airline_logo_linker_routes import airline_logo_linker

from routes.airlines_routes import airlines_bp

from routes.airports_routes import airports_bp

from routes.edit_aircraft_routes import edit_aircraft_bp

from routes.flight_history_routes import flight_history_bp

from routes.reports_routes import reports_bp

from routes import admin_tools_routes

from routes.airports_api import bp as airports_api_bp

from routes.manual_entry_routes import manual_entry_bp
from routes.registry_routes import registry_bp
_ARIA_ENABLED = os.getenv("ARIA_ENABLED", "0").lower() in ("1", "true", "yes")
if _ARIA_ENABLED:
    from routes.aria_routes import aria_bp
from modules.module_loader import register_optional_modules

try:
    from routes.billing_routes import billing_bp
    
    from routes.billing_webhook_routes import billing_webhook_bp

except ImportError:
    billing_bp = None
    billing_webhook_bp = None

app.register_blueprint(search_unified_bp)
logging.info("✅ Registered blueprint: search_unified_bp (/search_unified)")
app.register_blueprint(admin_bp)
app.register_blueprint(airlines_bp)
app.register_blueprint(reports_bp)
app.register_blueprint(airports_bp)
app.register_blueprint(aircraft_bp)
app.register_blueprint(admin_util_bp)
app.register_blueprint(add_aircraft_bp)
app.register_blueprint(edit_aircraft_bp)
app.register_blueprint(airline_logo_linker)
app.register_blueprint(admin_tools_routes.admin_tools_bp)
app.register_blueprint(manual_entry_bp)
app.register_blueprint(registry_bp)
if _ARIA_ENABLED:
    app.register_blueprint(aria_bp)
    csrf.exempt(aria_bp)
csrf.exempt(manual_entry_bp)
csrf.exempt(admin_tools_routes.admin_tools_bp)
csrf.exempt(admin_bp)
csrf.exempt(aircraft_bp)
app.register_blueprint(airports_api_bp)
if billing_bp:
    app.register_blueprint(billing_bp)

if billing_webhook_bp:
    app.register_blueprint(billing_webhook_bp)


# ---------------------------------------------------------------------------
# Optional modules
# ---------------------------------------------------------------------------
try:
    module_summary = register_optional_modules(app)
    logging.info(f"✅ Optional module loader completed: {module_summary}")
except Exception as e:
    logging.error(f"❌ Optional module loader failed: {e}", exc_info=True)

# Optional
try:
    from server.utils.private_cockpit import private_cockpit_bp
    app.register_blueprint(private_cockpit_bp)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        print("\n--- Flask URL Map ---")
        print(app.url_map)
        print("----------------------\n")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        use_reloader=False,
        threaded=True,
    )

# ---------------------------------------------------------------------------
# Woodland Scheduler — capability delivery
# ---------------------------------------------------------------------------
try:
    import os as _os
    if _os.getenv("WOMBAT_URL") and _os.getenv("AIRTRACK_CUSTOMER_ID"):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        def _run_mangy_marmot():
            try:
                from woodland.mangy_marmot import main
                main()
            except Exception as _exc:
                logging.error("Mangy Marmot crashed: %s", _exc, exc_info=True)

        _scheduler = BackgroundScheduler(timezone="UTC")
        _scheduler.add_job(
            _run_mangy_marmot,
            IntervalTrigger(minutes=5),
            id="mangy_marmot",
            name="Mangy Marmot",
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
        logging.info("Woodland Scheduler started — Mangy Marmot every 5 minutes")
        _run_mangy_marmot()  # run immediately on startup
except Exception as _sched_exc:
    logging.error("Woodland Scheduler failed to start: %s", _sched_exc, exc_info=True)
