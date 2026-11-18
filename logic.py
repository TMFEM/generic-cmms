"""Rules that don't depend on the web pages: categories, time math, permissions."""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TZ", "America/New_York"))

# key: (label, what Nameplate gets, or None if the entry stays with the maintenance group)
CATEGORIES = {
    "pm":             ("Preventive maintenance", "work:preventive"),
    "corrective":     ("Corrective repair", "work:corrective"),
    "tenant_request": ("Tenant request", "work:corrective"),
    "emergency":      ("Emergency response", "work:emergency"),
    "inspection":     ("Inspection or test", "inspection"),
    "reading":        ("Reading", "reading"),
    "rounds":         ("Building rounds", "logbook:engineer_rounds"),
    "event":          ("Building event (alarm, leak, entrapment, outage)", "logbook:building_events"),
    "vendor":         ("Vendor or contractor escort", None),
    "training":       ("Training", None),
    "safety":         ("Safety meeting or toolbox talk", None),
    "admin":          ("Admin, ordering, paperwork", None),
    "other":          ("Other", None),
}
SHORT = {"pm": "PM", "corrective": "Repair", "tenant_request": "Tenant", "emergency": "Emerg.",
         "inspection": "Insp.", "reading": "Readings", "rounds": "Rounds", "event": "Events", "vendor": "Vendor",
         "training": "Training", "safety": "Safety", "admin": "Admin", "other": "Other"}
# Work on equipment or the building, as opposed to time that's only the employer's business.
MAINTENANCE = {k for k, (_, np) in CATEGORIES.items() if np}
CREW_STATUSES = {"on_shift": "On shift", "late": "Late", "called_out": "Called out",
                 "holdover": "Held over", "overtime": "Overtime", "left_early": "Left early"}
RESULTS = {"pass": "Pass", "fail": "Fail", "conditional": "Conditional"}


def label(cat):
    return CATEGORIES.get(cat, (cat,))[0]


def now_local():
    return datetime.now(TZ).replace(second=0, microsecond=0, tzinfo=None)


def parse(ts):
    return datetime.fromisoformat(ts) if ts else None


def fmt(ts):
    return ts.strftime("%Y-%m-%dT%H:%M") if ts else None


def shift_window(day, start_time, end_time):
    """A shift's start and end. An end time earlier than the start means the next day (night shift)."""
    start = datetime.fromisoformat(f"{day}T{start_time}")
    end = datetime.fromisoformat(f"{day}T{end_time}")
    if end <= start:
        end += timedelta(days=1)
    return start, end


def on_shift_time(shift, clock):
    """Turn "14:30" into a full timestamp inside this shift (night shifts cross midnight)."""
    start, end = parse(shift["starts_at"]), parse(shift["ends_at"])
    t = datetime.fromisoformat(f"{start.date()}T{clock}")
    if t < start - timedelta(hours=4):  # e.g. 02:00 on a 23:00 shift is tomorrow
        t += timedelta(days=1)
    return t


def minutes_between(a, b):
    return int((b - a).total_seconds() // 60)


def hours(minutes):
    return round((minutes or 0) / 60, 2)


def iso_offset(local_ts):
    """Local "2026-11-15T07:30" to a timestamp with the UTC offset, for Nameplate."""
    return parse(local_ts).replace(tzinfo=TZ).isoformat()


# ---------------------------------------------------------------- permissions
def can_run_shifts(user):
    return user["role"] in ("admin", "supervisor")


def can_add_entry(user, shift, crew_ids):
    if shift["status"] != "open":
        return False
    return can_run_shifts(user) or (user["role"] == "technician" and user["id"] in crew_ids)


def can_edit_entry(user, shift, entry):
    """Open shifts only. After sign-off, changes are corrections (see correct_entry)."""
    if shift["status"] != "open" or entry["superseded"]:
        return False
    return can_run_shifts(user) or (user["role"] == "technician" and entry["user_id"] == user["id"])
