#!/usr/bin/env python3
# AirTrack Server Utility
# photo_ferret.py
#
# Finds missing aircraft photos, saves them locally, and updates the AirTrack
# server/logbook database.
#
# SERVER ONLY.
# DO NOT DISTRIBUTE TO CLIENT INSTALLS.
#
# Add this file to file_sync exclusions:
#   app/utils/photo_ferret.py

import json
import os
import random
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pymysql
from bs4 import BeautifulSoup


# =========================
# CONFIG
# =========================

TIMEZONE = ZoneInfo("Australia/Sydney")

PHOTO_DIR = Path("/home/trevor/docker/AirTrack/pics")
LOG_DIR = Path("/home/trevor/docker/AirTrack/AirTrack1/app/logs")
CACHE_DIR = Path("/home/trevor/cache/photo_ferret")

ENABLE_JETPHOTOS_SEARCH = False

LOG_FILE = LOG_DIR / "photo_ferret.log"
NOT_FOUND_CACHE = CACHE_DIR / "not_found.json"

MIN_DELAY_SECONDS = 15 * 60
MAX_DELAY_SECONDS = 30 * 60

ACTIVE_START_HOUR = 9
ACTIVE_END_HOUR = 22

STATIC_PHOTO_DIR = Path("/home/trevor/docker/AirTrack/AirTrack1/app/static/uploads/aircraft")
MISSING_IMAGE_REPORT = LOG_DIR / "missing_aircraft_images.txt"

MIN_DAILY_DOWNLOADS = 10
MAX_DAILY_DOWNLOADS = 20

REQUEST_TIMEOUT = 30

# JETPHOTOS_SEARCH_URL = "https://www.jetphotos.com/keyword/{query}"

USER_AGENT = "AirTrack PhotoFerret/1.0 (contact: trevor@airtracksolutions.com)"

IMAGE_COLUMN = "Aircraft_Image"

STOP_STATUS_CODES = {403, 429}

DB_CONFIG = {
    "host": "192.168.0.200",
    "port": 3306,
    "user": "SirBob",
    "password": "ofAirTrack",
    "database": "airtrack",
}


# =========================
# IMAGE AUDIT
# =========================

def audit_database_image_links(cursor):
    sql = """
        SELECT Registration, Aircraft_Image
        FROM aircraft
        WHERE Aircraft_Image IS NOT NULL
          AND TRIM(Aircraft_Image) != ''
        ORDER BY Registration
    """
    cursor.execute(sql)

    missing = []

    for reg, filename in cursor.fetchall():
        reg = str(reg or "").strip().upper()
        filename = str(filename or "").strip()

        if not filename:
            continue

        expected_path = STATIC_PHOTO_DIR / filename

        if not expected_path.exists():
            missing.append((reg, filename, str(expected_path)))

    if not missing:
        log("Image audit: all database image links have matching files.")
        return []

    log(f"Image audit: {len(missing)} database image links point to missing files.")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with MISSING_IMAGE_REPORT.open("w", encoding="utf-8") as handle:
        handle.write(f"Missing aircraft image report - {timestamp()}\n")
        handle.write("=" * 70 + "\n\n")

        for reg, filename, path in missing:
            line = f"{reg}: {filename} missing at {path}"
            log(f"WARNING: {line}")
            handle.write(line + "\n")

    log(f"Missing image report written to: {MISSING_IMAGE_REPORT}")
    return missing


# =========================
# LOGGING
# =========================

def now_local():
    return datetime.now(TIMEZONE)


def timestamp():
    return now_local().strftime("%Y-%m-%d %H:%M:%S %Z")


def log(message):
    line = f"[{timestamp()}] {message}"
    print(line)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


# =========================
# CACHE
# =========================

def load_not_found_cache():
    if not NOT_FOUND_CACHE.exists():
        return {}

    try:
        with NOT_FOUND_CACHE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_not_found_cache(cache):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with NOT_FOUND_CACHE.open("w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2, sort_keys=True)
    except Exception as exc:
        log(f"WARNING: Could not save not-found cache: {exc}")


def mark_not_found(cache, reg):
    cache[reg] = timestamp()
    save_not_found_cache(cache)


# =========================
# DATABASE
# =========================

def get_db_connection():
    return pymysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        cursorclass=pymysql.cursors.Cursor,
        autocommit=True,
    )


def get_aircraft_missing_images(cursor):
    sql = f"""
        SELECT Registration
        FROM aircraft
        WHERE Registration IS NOT NULL
          AND TRIM(Registration) != ''
          AND ({IMAGE_COLUMN} IS NULL OR TRIM({IMAGE_COLUMN}) = '')
        ORDER BY Registration
    """
    cursor.execute(sql)

    registrations = []
    for row in cursor.fetchall():
        reg = str(row[0]).strip().upper()
        if reg:
            registrations.append(reg)

    return registrations


def update_aircraft_image(cursor, reg, filename):
    sql = f"""
        UPDATE aircraft
        SET {IMAGE_COLUMN} = %s
        WHERE UPPER(TRIM(Registration)) = %s
    """
    cursor.execute(sql, (filename, reg.upper().strip()))


# =========================
# TIME / THROTTLING
# =========================

def within_active_hours():
    hour = now_local().hour
    return ACTIVE_START_HOUR <= hour < ACTIVE_END_HOUR


def wait_until_active_hours():
    while not within_active_hours():
        log("Outside active hours. Sleeping 10 minutes.")
        time.sleep(10 * 60)


def sleep_random_delay():
    delay = random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
    minutes = delay // 60
    seconds = delay % 60

    log(f"Sleeping for {minutes}m {seconds}s.")
    time.sleep(delay)


# =========================
# HTTP
# =========================

def build_request(url):
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        },
    )


def fetch_url(url):
    try:
        req = build_request(url)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read()
            return response.status, content_type, data

    except urllib.error.HTTPError as exc:
        log(f"HTTP error {exc.code} for {url}")

        if exc.code in STOP_STATUS_CODES:
            log("Stop status received. Exiting cleanly.")
            sys.exit(1)

        return exc.code, "", b""

    except Exception as exc:
        log(f"Fetch error for {url}: {exc}")
        return None, "", b""


def fetch_html(url):
    status, content_type, data = fetch_url(url)

    if status != 200 or not data:
        return None

    html = data.decode("utf-8", errors="ignore")

    lower_html = html.lower()
    if "captcha" in lower_html or "rate limit" in lower_html or "too many requests" in lower_html:
        log("Possible CAPTCHA or rate limit page detected. Exiting cleanly.")
        sys.exit(1)

    return html


# =========================
# IMAGE HELPERS
# =========================

def local_photo_path(reg):
    return PHOTO_DIR / f"{reg.upper().strip()}.jpg"


def photo_exists_locally(reg):
    return local_photo_path(reg).exists()


def copy_to_static(reg):
    """Move image from PHOTO_DIR to STATIC_PHOTO_DIR, deleting the original."""
    src = local_photo_path(reg)
    dest = STATIC_PHOTO_DIR / f"{reg.upper().strip()}.jpg"
    STATIC_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    log(f"{reg}: moved to static: {dest}")
    return dest


def save_image(reg, image_data):
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)

    path = local_photo_path(reg)

    with path.open("wb") as handle:
        handle.write(image_data)

    return path


def download_image(reg, image_url):
    log(f"{reg}: downloading image.")

    status, content_type, data = fetch_url(image_url)

    if status != 200 or not data:
        log(f"{reg}: image download failed.")
        return None

    if content_type and "image" not in content_type.lower():
        log(f"{reg}: downloaded content was not an image: {content_type}")
        return None

    path = save_image(reg, data)
    log(f"{reg}: saved {path}")
    return path


# =========================
# JETPHOTOS
# =========================

def build_search_url(reg):
    query = urllib.parse.quote(reg.upper().strip())
    return JETPHOTOS_SEARCH_URL.format(query=query)


_JETPHOTOS_HOSTS = frozenset({
    "cdn.jetphotos.com", "www.jetphotos.com", "jetphotos.com",
})


def absolutise_jetphotos_url(href):
    if href.startswith("http://") or href.startswith("https://"):
        # Only return absolute URLs from known jetphotos domains
        try:
            if urlparse(href).netloc.lower() in _JETPHOTOS_HOSTS:
                return href
        except Exception:
            pass
        return None  # Non-jetphotos absolute URL — discard

    if href.startswith("/"):
        return f"https://www.jetphotos.com{href}"

    return f"https://www.jetphotos.com/{href}"


def find_first_photo_page(reg):
    search_url = build_search_url(reg)
    log(f"{reg}: searching JetPhotos.")

    html = fetch_html(search_url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    for link in soup.find_all("a", href=True):
        href = link.get("href", "")

        if "/photo/" not in href:
            continue

        return absolutise_jetphotos_url(href)

    return None


def extract_image_url(photo_page_url):
    html = fetch_html(photo_page_url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    selectors = [
        ("img", {"class": "large-photo__img"}),
        ("img", {"id": "large-photo"}),
    ]

    for tag_name, attrs in selectors:
        img = soup.find(tag_name, attrs)
        if img:
            src = img.get("src") or img.get("data-src")
            if src:
                return absolutise_jetphotos_url(src)

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        try:
            _parsed = urlparse(src)
            _host = _parsed.netloc.lower()
        except Exception:
            _host = ""
        if _host in ("cdn.jetphotos.com", "www.jetphotos.com", "jetphotos.com") or \
                (not _parsed.scheme and "jetphotos" in src):
            return absolutise_jetphotos_url(src)

    return None


def process_registration(cursor, reg, not_found_cache):
    reg = reg.upper().strip()

    if reg in not_found_cache:
        log(f"{reg}: previously marked not found. Skipping.")
        return False

    # Local image found — copy to static and update database
    if photo_exists_locally(reg):
        filename = f"{reg}.jpg"
        log(f"{reg}: local image exists. Copying to static dir and updating database.")
        try:
            copy_to_static(reg)
        except Exception as exc:
            log(f"{reg}: failed to copy to static dir: {exc}")
            return False
        update_aircraft_image(cursor, reg, filename)
        log(f"{reg}: database updated.")
        return True

    if not ENABLE_JETPHOTOS_SEARCH:
        log(f"{reg}: no local image found, external search disabled. Skipping.")
        return False

    # --- JetPhotos logic below ---
    photo_page = find_first_photo_page(reg)

    if not photo_page:
        log(f"{reg}: no JetPhotos result found.")
        mark_not_found(not_found_cache, reg)
        return False

    log(f"{reg}: photo page found: {photo_page}")

    image_url = extract_image_url(photo_page)

    if not image_url:
        log(f"{reg}: no usable image found on photo page.")
        mark_not_found(not_found_cache, reg)
        return False

    saved_path = download_image(reg, image_url)

    if not saved_path:
        return False

    filename = f"{reg}.jpg"

    try:
        copy_to_static(reg)
    except Exception as exc:
        log(f"{reg}: failed to copy downloaded image to static dir: {exc}")
        return False

    update_aircraft_image(cursor, reg, filename)
    log(f"{reg}: database updated: {IMAGE_COLUMN} = {filename}")

    return True


# =========================
# MAIN
# =========================

def main():
    log("Photo Ferret starting. Server-only mode.")

    # In local-only mode, no throttling needed — process everything immediately
    local_only = not ENABLE_JETPHOTOS_SEARCH

    if local_only:
        log("Local import mode — no delays, no daily limit.")
    else:
        daily_target = random.randint(MIN_DAILY_DOWNLOADS, MAX_DAILY_DOWNLOADS)
        log(f"Daily target selected: {daily_target} images.")
        log(f"Active hours: {ACTIVE_START_HOUR}:00 to {ACTIVE_END_HOUR}:00 Australia/Sydney.")
        wait_until_active_hours()

    not_found_cache = load_not_found_cache()

    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Run image audit first
        audit_database_image_links(cursor)

        registrations = get_aircraft_missing_images(cursor)

        if not local_only:
            random.shuffle(registrations)

        log(f"Aircraft missing images in database: {len(registrations)}")

        imported = 0
        skipped = 0

        for reg in registrations:
            if not local_only:
                wait_until_active_hours()

            try:
                did_import = process_registration(cursor, reg, not_found_cache)

                if did_import:
                    imported += 1
                else:
                    skipped += 1

                if not local_only:
                    if imported >= daily_target:
                        log("Daily target reached. Exiting.")
                        break
                    sleep_random_delay()

            except KeyboardInterrupt:
                log("Interrupted by user. Exiting.")
                break

            except Exception as exc:
                log(f"{reg}: unexpected error: {exc}")

        log(f"Run complete. Imported: {imported}, Skipped: {skipped}.")

    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass

        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

    log("Photo Ferret finished.")


if __name__ == "__main__":
    main()
