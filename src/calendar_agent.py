"""Calendar agent: check free appointment slots and book them.

Two backends, same public functions:
  * Google Calendar  (MOCK_CALENDAR=false)  - real events on your calendar
  * Demo / mock mode (MOCK_CALENDAR=true)   - bookings saved in a local JSON file,
    so you can test the whole chat flow without any Google setup.

Public functions (these are exposed to the LLM as tools):
  * check_available_slots(date)
  * book_appointment(patient_name, date, time)

Dates are "YYYY-MM-DD" and times are 24-hour "HH:MM".
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
ROOT_DIR = Path(__file__).resolve().parent.parent

# Stops two people from grabbing the same slot at the same moment.
_booking_lock = threading.Lock()


# --------------------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    """All calendar settings, read from environment variables (.env)."""

    tz: ZoneInfo
    open_time: dt.time
    close_time: dt.time
    slot_minutes: int
    working_days: frozenset[int]  # Monday=0 ... Sunday=6
    max_days_ahead: int
    calendar_id: str
    credentials_path: Path
    token_path: Path
    mock: bool
    mock_path: Path


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT_DIR / p


def _parse_time(value: str) -> dt.time:
    """Accept '14:30', '2:30 PM', '2 PM' and return a time object."""
    value = value.strip().upper()
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            return dt.datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Time '{value}' is not valid. Use 24-hour HH:MM, for example 14:30.")


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise ValueError(f"Date '{value}' is not valid. Use the format YYYY-MM-DD.") from None


def get_settings() -> Settings:
    """Read settings fresh each time so .env changes are picked up."""
    days = os.getenv("WORKING_DAYS", "0,1,2,3,4,5")
    return Settings(
        tz=ZoneInfo(os.getenv("CLINIC_TIMEZONE", "UTC")),
        open_time=_parse_time(os.getenv("CLINIC_OPEN", "09:00")),
        close_time=_parse_time(os.getenv("CLINIC_CLOSE", "17:00")),
        slot_minutes=int(os.getenv("SLOT_MINUTES", "30")),
        working_days=frozenset(int(d) for d in days.split(",") if d.strip() != ""),
        max_days_ahead=int(os.getenv("MAX_DAYS_AHEAD", "60")),
        calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
        credentials_path=_path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")),
        token_path=_path(os.getenv("GOOGLE_TOKEN_FILE", "token.json")),
        mock=os.getenv("MOCK_CALENDAR", "false").strip().lower() in {"1", "true", "yes", "on"},
        mock_path=_path(os.getenv("MOCK_BOOKINGS_FILE", "data/mock_bookings.json")),
    )


def is_mock() -> bool:
    return get_settings().mock


def now_in_clinic_tz() -> dt.datetime:
    return dt.datetime.now(get_settings().tz)


def describe_schedule() -> str:
    """One-line schedule summary that is put into the LLM's system prompt."""
    cfg = get_settings()
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    days = ", ".join(names[d] for d in sorted(cfg.working_days))
    return (
        f"Bookable days: {days}. Hours: {cfg.open_time:%H:%M}-{cfg.close_time:%H:%M} "
        f"({cfg.tz.key}). Each appointment is {cfg.slot_minutes} minutes. "
        f"Bookings are allowed up to {cfg.max_days_ahead} days ahead."
    )


# --------------------------------------------------------------------------- slot maths
def _day_slots(day: dt.date, cfg: Settings) -> list[dt.datetime]:
    """Every possible slot start on a day, e.g. 09:00, 09:30, ... 16:30."""
    start = dt.datetime.combine(day, cfg.open_time, tzinfo=cfg.tz)
    end = dt.datetime.combine(day, cfg.close_time, tzinfo=cfg.tz)
    step = dt.timedelta(minutes=cfg.slot_minutes)
    slots: list[dt.datetime] = []
    current = start
    while current + step <= end:
        slots.append(current)
        current += step
    return slots


def _overlaps(start: dt.datetime, end: dt.datetime, busy: list[tuple[dt.datetime, dt.datetime]]) -> bool:
    """True if [start, end) touches any busy interval."""
    return any(start < b_end and end > b_start for b_start, b_end in busy)


def _validate_day(day: dt.date, cfg: Settings) -> str | None:
    """Return an error message if the clinic cannot take bookings on this day."""
    today = dt.datetime.now(cfg.tz).date()
    if day < today:
        return "That date is in the past. Please choose today or a future date."
    if (day - today).days > cfg.max_days_ahead:
        return f"We can only book up to {cfg.max_days_ahead} days ahead."
    if day.weekday() not in cfg.working_days:
        return f"The clinic is closed on {day:%A}s. Please choose another day."
    return None


# --------------------------------------------------------------------------- backends
def _read_mock(cfg: Settings) -> list[dict[str, Any]]:
    if not cfg.mock_path.exists():
        return []
    try:
        return json.loads(cfg.mock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Mock bookings file is corrupted; starting fresh.")
        return []


@lru_cache(maxsize=1)
def _google_service() -> Any:
    """Log in to Google once (opens a browser the first time) and reuse the connection."""
    # Imported here so demo mode works even if the Google libraries are missing.
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    cfg = get_settings()
    creds = None
    if cfg.token_path.exists():
        creds = Credentials.from_authorized_user_file(str(cfg.token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # silently renew an expired token
        else:
            if not cfg.credentials_path.exists():
                raise FileNotFoundError(
                    f"Google credentials not found at {cfg.credentials_path}. "
                    "Download credentials.json from Google Cloud Console, or set MOCK_CALENDAR=true."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(cfg.credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)  # one-time browser sign-in
        cfg.token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _busy_intervals(start: dt.datetime, end: dt.datetime, cfg: Settings) -> list[tuple[dt.datetime, dt.datetime]]:
    """All busy time ranges between `start` and `end`."""
    if cfg.mock:
        return [
            (dt.datetime.fromisoformat(b["start"]), dt.datetime.fromisoformat(b["end"]))
            for b in _read_mock(cfg)
        ]

    # Google's free/busy API answers "when is this calendar occupied?" in one call.
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": cfg.tz.key,
        "items": [{"id": cfg.calendar_id}],
    }
    result = _google_service().freebusy().query(body=body).execute()
    calendars = result.get("calendars", {})
    busy_list = (calendars.get(cfg.calendar_id) or next(iter(calendars.values()), {})).get("busy", [])
    return [
        (
            dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(cfg.tz),
            dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(cfg.tz),
        )
        for b in busy_list
    ]


def _create_event(patient_name: str, start: dt.datetime, end: dt.datetime, cfg: Settings) -> dict[str, Any]:
    """Write the appointment to the backend and return details about it."""
    if cfg.mock:
        bookings = _read_mock(cfg)
        bookings.append({"patient": patient_name, "start": start.isoformat(), "end": end.isoformat()})
        cfg.mock_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.mock_path.write_text(json.dumps(bookings, indent=2), encoding="utf-8")
        return {"event_id": f"demo-{len(bookings)}", "event_link": None}

    event = {
        "summary": f"Appointment: {patient_name}",
        "description": f"Booked by the Clinic AI Receptionist for {patient_name}.",
        "start": {"dateTime": start.isoformat(), "timeZone": cfg.tz.key},
        "end": {"dateTime": end.isoformat(), "timeZone": cfg.tz.key},
    }
    created = _google_service().events().insert(calendarId=cfg.calendar_id, body=event).execute()
    return {"event_id": created.get("id"), "event_link": created.get("htmlLink")}


def _free_slots(day: dt.date, cfg: Settings) -> list[dt.datetime]:
    """Slots on `day` that are not busy and not already in the past."""
    slots = _day_slots(day, cfg)
    if not slots:
        return []
    step = dt.timedelta(minutes=cfg.slot_minutes)
    busy = _busy_intervals(slots[0], slots[-1] + step, cfg)
    now = dt.datetime.now(cfg.tz)
    return [s for s in slots if s > now and not _overlaps(s, s + step, busy)]


# --------------------------------------------------------------------------- public tools
def check_available_slots(date: str) -> dict[str, Any]:
    """List the free appointment times on `date` (YYYY-MM-DD)."""
    cfg = get_settings()
    try:
        day = _parse_date(date)
    except ValueError as exc:
        return {"error": str(exc)}

    problem = _validate_day(day, cfg)
    if problem:
        return {"date": day.isoformat(), "available_slots": [], "message": problem}

    free = [s.strftime("%H:%M") for s in _free_slots(day, cfg)]
    return {
        "date": day.isoformat(),
        "weekday": f"{day:%A}",
        "available_slots": free,
        "message": f"{len(free)} slot(s) free." if free else "No free slots on this day.",
    }


def book_appointment(patient_name: str, date: str, time: str) -> dict[str, Any]:
    """Book a slot for `patient_name` on `date` (YYYY-MM-DD) at `time` (HH:MM)."""
    cfg = get_settings()
    patient_name = (patient_name or "").strip()
    if len(patient_name) < 2:
        return {"success": False, "error": "A valid patient name is required."}

    try:
        day = _parse_date(date)
        start_time = _parse_time(time)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    problem = _validate_day(day, cfg)
    if problem:
        return {"success": False, "error": problem}

    start = dt.datetime.combine(day, start_time, tzinfo=cfg.tz)
    end = start + dt.timedelta(minutes=cfg.slot_minutes)

    # Lock + re-check so the slot cannot be double-booked.
    with _booking_lock:
        free = _free_slots(day, cfg)
        if start not in free:
            options = [s.strftime("%H:%M") for s in free]
            return {
                "success": False,
                "error": f"{start:%H:%M} on {day.isoformat()} is not available.",
                "available_slots": options,
            }
        details = _create_event(patient_name, start, end, cfg)

    return {
        "success": True,
        "patient_name": patient_name,
        "date": day.isoformat(),
        "weekday": f"{day:%A}",
        "time": start.strftime("%H:%M"),
        "duration_minutes": cfg.slot_minutes,
        **details,
    }
