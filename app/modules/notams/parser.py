"""
NOTAM parser and normalizer.

Purpose:
    raw NOTAM text -> canonical database-ready dict

This is a first-pass parser scaffold. It handles standard ICAO-style NOTAMs
and preserves raw source text for audit/history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
from typing import Optional


HEADER_RE = re.compile(
    r"(?P<id>[A-Z]\d{4}/\d{2})\s+NOTAM(?P<type>[NRC])",
    re.IGNORECASE,
)

# FIELD_RE: matches a field label (e.g. 'A) YSSY') and its value, which
# continues until the next field label line or end of string.
# Written without re.DOTALL and using [^\n]* to avoid catastrophic backtracking.
FIELD_RE = re.compile(
    r"(?m)^(?P<label>[QABCDEFG])\)\s*(?P<value>[^\n]*(?:\n(?![A-Z]\))[^\n]*)*)"
)

NOTAM_ID_RE = re.compile(r"^(?P<series>[A-Z])(?P<number>\d{4})/(?P<year>\d{2})$")


@dataclass
class ParsedNotam:
    record: dict
    errors: list[str]


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return " ".join(value.strip().split())


def _extract_fields(raw_text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in FIELD_RE.finditer(raw_text):
        label = match.group("label").upper()
        value = match.group("value").strip()
        fields[label] = value
    return fields


def _parse_notam_id(notam_id: str) -> tuple[Optional[str], Optional[int], Optional[int]]:
    match = NOTAM_ID_RE.match(notam_id.upper())
    if not match:
        return None, None, None
    return (
        match.group("series"),
        int(match.group("number")),
        int(match.group("year")),
    )


def _parse_datetime_utc(value: Optional[str]) -> tuple[Optional[datetime], bool]:
    """
    Parse YYMMDDHHMM into timezone-aware UTC datetime.

    Returns:
        (datetime_or_none, permanent_flag)
    """
    if not value:
        return None, True

    cleaned = value.strip().upper()

    if "PERM" in cleaned or "UFN" in cleaned:
        return None, True

    # Strip common trailing qualifiers such as EST.
    token = cleaned.split()[0]

    try:
        dt = datetime.strptime(token, "%y%m%d%H%M").replace(tzinfo=timezone.utc)
        return dt, False
    except ValueError:
        return None, True


def _parse_altitude(value: Optional[str]) -> Optional[int]:
    if not value:
        return None

    text = value.strip().upper()
    if text in {"SFC", "GND"}:
        return 0
    if text in {"UNL", "UNLIMITED"}:
        return 99999
    if text.startswith("FL"):
        digits = re.sub(r"\D", "", text)
        return int(digits) * 100 if digits else None
    if text.endswith("FT"):
        digits = re.sub(r"\D", "", text)
        return int(digits) if digits else None
    if text.isdigit():
        return int(text)

    return None


def _parse_q_line(value: Optional[str], errors: list[str]) -> dict:
    result = {
        "fir": None,
        "q_code": None,
        "q_subject": None,
        "q_condition": None,
        "q_traffic": None,
        "q_purpose": None,
        "q_scope": None,
        "lower_limit_ft": None,
        "upper_limit_ft": None,
        "latitude": None,
        "longitude": None,
        "radius_nm": None,
    }

    if not value:
        errors.append("Missing Q-line")
        return result

    parts = [p.strip() for p in value.split("/")]
    if len(parts) < 8:
        errors.append(f"Q-line has {len(parts)} segments, expected 8")

    while len(parts) < 8:
        parts.append("")

    result["fir"] = parts[0] or None

    q_code_raw = parts[1].upper()
    if q_code_raw.startswith("Q"):
        q_code = q_code_raw[1:]
    else:
        q_code = q_code_raw

    result["q_code"] = q_code or None
    if len(q_code) >= 4:
        result["q_subject"] = q_code[:2]
        result["q_condition"] = q_code[2:4]

    result["q_traffic"] = parts[2] or None
    result["q_purpose"] = parts[3] or None
    result["q_scope"] = parts[4] or None

    result["lower_limit_ft"] = _parse_q_limit(parts[5])
    result["upper_limit_ft"] = _parse_q_limit(parts[6])

    lat, lon, radius = _parse_q_coord_radius(parts[7], errors)
    result["latitude"] = lat
    result["longitude"] = lon
    result["radius_nm"] = radius

    return result


def _parse_q_limit(value: str) -> Optional[int]:
    if not value:
        return None
    if value == "000":
        return 0
    if value == "999":
        return 99999
    if value.isdigit():
        return int(value) * 100
    return None


def _parse_q_coord_radius(value: str, errors: list[str]) -> tuple[Optional[float], Optional[float], Optional[int]]:
    """
    Parse compact Q-line coord/radius such as 3351S15112E005.
    """
    if not value:
        return None, None, None

    match = re.match(
        r"(?P<lat_deg>\d{2})(?P<lat_min>\d{2})(?P<lat_hemi>[NS])"
        r"(?P<lon_deg>\d{3})(?P<lon_min>\d{2})(?P<lon_hemi>[EW])"
        r"(?P<radius>\d{3})",
        value.strip().upper(),
    )
    if not match:
        errors.append(f"Could not parse Q-line coordinate/radius: {value}")
        return None, None, None

    lat = int(match.group("lat_deg")) + int(match.group("lat_min")) / 60
    lon = int(match.group("lon_deg")) + int(match.group("lon_min")) / 60

    if match.group("lat_hemi") == "S":
        lat = -lat
    if match.group("lon_hemi") == "W":
        lon = -lon

    return round(lat, 5), round(lon, 5), int(match.group("radius"))


def _parse_confidence(errors: list[str], record: dict) -> str:
    if not errors and record.get("primary_icao") and record.get("effective_from"):
        return "HIGH"
    if record.get("notam_id") and record.get("text_raw"):
        return "MEDIUM"
    return "LOW"


def split_notam_blocks(raw: str) -> list[str]:
    """
    Split a text blob into probable NOTAM entries.
    """
    raw = raw.strip()
    if not raw:
        return []

    starts = list(re.finditer(r"(?m)^[A-Z]\d{4}/\d{2}\s+NOTAM[NRC]", raw, re.IGNORECASE))
    if not starts:
        return [raw]

    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(raw)
        blocks.append(raw[start.start():end].strip())

    return blocks


def parse_notam(raw_text: str, source: str = "manual") -> ParsedNotam:
    errors: list[str] = []
    raw_text = raw_text.strip()

    header = HEADER_RE.search(raw_text)
    if not header:
        errors.append("Missing or invalid NOTAM header")
        notam_id = "UNKNOWN"
        notam_type = "N"
    else:
        notam_id = header.group("id").upper()
        notam_type = header.group("type").upper()

    series, number, year = _parse_notam_id(notam_id)
    if not series:
        errors.append(f"Could not parse NOTAM ID: {notam_id}")

    fields = _extract_fields(raw_text)
    q = _parse_q_line(fields.get("Q"), errors)

    location_raw = _clean(fields.get("A"))
    locations = [p.strip().upper() for p in re.split(r"[\s,]+", location_raw or "") if p.strip()]
    primary_icao = locations[0] if locations else None

    effective_from, from_perm = _parse_datetime_utc(fields.get("B"))
    effective_to, to_perm = _parse_datetime_utc(fields.get("C"))
    is_permanent = bool(to_perm)

    if not effective_from:
        errors.append("Missing or unparseable B field effective_from")
        effective_from = datetime.now(timezone.utc)

    lower_raw = _clean(fields.get("F"))
    upper_raw = _clean(fields.get("G"))

    record = {
        "notam_id": notam_id,
        "series": series,
        "number": number,
        "year": year,
        "notam_type": notam_type,

        **q,

        "location_raw": location_raw,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "is_permanent": 1 if is_permanent else 0,
        "schedule_raw": fields.get("D"),
        "text_raw": fields.get("E"),
        "lower_limit_raw": lower_raw,
        "upper_limit_raw": upper_raw,
        "lower_limit_ft": _parse_altitude(lower_raw) if lower_raw else q.get("lower_limit_ft"),
        "upper_limit_ft": _parse_altitude(upper_raw) if upper_raw else q.get("upper_limit_ft"),

        "category": "other",
        "severity": "MINOR",
        "parse_confidence": "LOW",

        "status": "active",
        "superseded_by": None,
        "primary_icao": primary_icao,

        "source": source,
        "raw_text": raw_text,
        "checksum": "",
    }

    record["checksum"] = hashlib.md5(
        f"{record['notam_id']}|{record['effective_from'].isoformat()}".encode("utf-8")
    ).hexdigest()

    record["parse_confidence"] = _parse_confidence(errors, record)

    return ParsedNotam(record=record, errors=errors)
