"""Talking to Nameplate, the shared equipment and building record.

Two directions:

1. Reading: the building's places and equipment are copied into np_places and
   np_equipment so the entry form can offer them in a list. refresh_building() does it.

2. Writing: when a supervisor closes a shift, each entry about equipment or the building
   is queued in np_outbox (same transaction as the close) and sent in the background.
   If Nameplate is down, the queue waits; the shift is still closed here.

What is never sent: crew hours, call-outs, training, safety meetings, vendor escorts,
admin time, and anything else that's the employer's business rather than the building's.
"""
import fcntl
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing, contextmanager

import logic
from db import DATA_DIR, connect, install_id

URL = os.environ.get("NAMEPLATE_URL", "").rstrip("/")
KEY = os.environ.get("NAMEPLATE_KEY", "")
OWNER = os.environ.get("NAMEPLATE_OWNER_ID", "")
TIMEOUT = int(os.environ.get("NAMEPLATE_TIMEOUT", "10"))
COMPANY = os.environ.get("COMPANY_NAME", "")
MAX_ATTEMPTS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS np_outbox (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, local_id INTEGER NOT NULL, payload TEXT,
  created TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_np_outbox_pending ON np_outbox(sent_at, id);
CREATE TABLE IF NOT EXISTS np_map (
  kind TEXT NOT NULL, local_id INTEGER NOT NULL, np_id TEXT NOT NULL, synced TEXT NOT NULL,
  PRIMARY KEY (kind, local_id)
);
"""

_wake = threading.Event()


class NameplateDown(Exception):
    """Couldn't reach Nameplate. Keep the queue in order and try again later."""


class NameplateRefused(Exception):
    """Nameplate answered and said no (bad data, no access)."""

    def __init__(self, status, detail):
        super().__init__(f"{status}: {detail}")
        self.status = status


def enabled():
    return bool(URL and KEY and OWNER)


# ---------------------------------------------------------------- HTTP
def _call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method,
                                 headers={"X-API-Key": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail")
        except Exception:
            detail = exc.reason
        if exc.code >= 500:
            raise NameplateDown(f"Nameplate error {exc.code}") from exc
        raise NameplateRefused(exc.code, detail) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise NameplateDown(str(exc)) from exc


def _owner(path):
    return f"/v1/owners/{OWNER}{path}"


def ping():
    if not enabled():
        return False, "Not connected. Set NAMEPLATE_URL, NAMEPLATE_KEY and NAMEPLATE_OWNER_ID."
    try:
        h = _call("GET", "/health")
        _call("GET", _owner("/locations?kind=facility"))
        return True, f"Connected to Nameplate {h.get('version', '')}"
    except NameplateDown as exc:
        return False, f"Can't reach Nameplate: {exc}"
    except NameplateRefused as exc:
        if exc.status == 403:
            return False, "Nameplate is reachable, but the owner hasn't given this app access."
        return False, f"Nameplate refused the request: {exc}"


# ---------------------------------------------------------------- reading
def find_building(bin_number):
    """The Nameplate building with this NYC BIN, or None."""
    q = urllib.parse.urlencode({"scheme": "NYC-BIN", "value": bin_number})
    hits = _call("GET", _owner(f"/identifiers/lookup?{q}"))
    hit = next((h for h in hits if h["subject_type"] == "location"), None)
    if not hit:
        return None
    return next((l for l in _call("GET", _owner("/locations")) if l["id"] == hit["subject_id"]), None)


def list_buildings():
    return _call("GET", _owner("/locations?kind=facility"))


def refresh_building(db, building_id):
    """Copy this building's places and equipment from Nameplate into the local pickers."""
    b = db.execute("SELECT * FROM buildings WHERE id=?", (building_id,)).fetchone()
    locs = {l["id"]: l for l in _call("GET", _owner("/locations"))}
    segs = {s["id"]: s for s in _call("GET", _owner("/segments"))}
    assets = _call("GET", _owner("/assets?status=in_service"))

    # Every place inside the building, with a readable path ("Cellar › Boiler room").
    def path(lid):
        names, cur = [], locs.get(lid)
        while cur and cur["id"] != b["np_location_id"]:
            names.append(cur["name"])
            cur = locs.get(cur["parent_id"])
        return " › ".join(reversed(names)) if cur else None  # None: not in this building

    places = {lid: path(lid) for lid in locs}
    places[b["np_location_id"]] = "Whole building"
    db.execute("DELETE FROM np_places WHERE building_id=?", (building_id,))
    db.execute("DELETE FROM np_equipment WHERE building_id=?", (building_id,))
    for lid, p in places.items():
        if p is not None:
            db.execute("INSERT OR REPLACE INTO np_places (np_id, building_id, name, kind, path) VALUES (?,?,?,?,?)",
                       (lid, building_id, locs[lid]["name"], locs[lid]["kind"], p))
    for a in assets:
        seg = segs.get(a["current_segment_id"]) or {}
        lid = seg.get("location_id")
        if lid not in places or places[lid] is None:
            continue
        desc = a.get("name") or a.get("type_name") or ""
        tag = a.get("segment_tag") or ""
        db.execute("INSERT OR REPLACE INTO np_equipment (np_id, building_id, tag, label, np_location_id, place) "
                   "VALUES (?,?,?,?,?,?)",
                   (a["id"], building_id, tag, f"{tag} {desc}".strip() or a["id"][:8], lid, places[lid]))
    db.execute("UPDATE buildings SET refreshed=? WHERE id=?", (logic.fmt(logic.now_local()), building_id))
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM np_equipment WHERE building_id=?", (building_id,)).fetchone()[0]
    return n


# ---------------------------------------------------------------- queue
def enqueue(db, kind, local_id, payload=None):
    """Queue something to send. Call inside the same transaction as the local save."""
    if not enabled():
        return
    db.execute("INSERT INTO np_outbox (kind, local_id, payload, created) VALUES (?,?,?,?)",
               (kind, local_id, json.dumps(payload) if payload else None, time.strftime("%Y-%m-%dT%H:%M:%S")))
    _wake.set()


def mapped(db, kind, local_id):
    row = db.execute("SELECT np_id FROM np_map WHERE kind=? AND local_id=?", (kind, local_id)).fetchone()
    return row[0] if row else None


def _remember(db, kind, local_id, np_id):
    db.execute("INSERT OR REPLACE INTO np_map (kind, local_id, np_id, synced) VALUES (?,?,?,?)",
               (kind, local_id, str(np_id), time.strftime("%Y-%m-%dT%H:%M:%S")))


def shareable(entry):
    """Is this entry about equipment or the building, with something to attach it to?"""
    return entry["category"] in logic.MAINTENANCE and bool(entry["np_asset_id"] or entry["np_location_id"])


def queue_shift(db, shift_id):
    """Called when a shift is closed: queue every entry that belongs in the shared record."""
    rows = db.execute("SELECT * FROM entries WHERE shift_id=? AND superseded=0 ORDER BY id", (shift_id,)).fetchall()
    n = 0
    for e in rows:
        if shareable(e) and not mapped(db, "entry", e["id"]):
            enqueue(db, "entry", e["id"])
            n += 1
    return n


# ---------------------------------------------------------------- building the Nameplate entry
def event_body(db, e):
    user = db.execute("SELECT * FROM users WHERE id=?", (e["user_id"],)).fetchone()
    shift = db.execute("SELECT * FROM shifts WHERE id=?", (e["shift_id"],)).fetchone()
    when = e["ended_at"] or e["started_at"] or shift["ends_at"]
    body = {"occurred_at": logic.iso_offset(when)}
    if e["np_asset_id"]:
        body["asset_id"] = e["np_asset_id"]
    else:
        body["location_id"] = e["np_location_id"]
    if user["np_technician_id"]:
        body["technician_id"] = user["np_technician_id"]
    performer = {"name": user["name"]}
    if COMPANY:
        performer["company"] = COMPANY
    if user["credential_type"]:
        performer["credential_type"] = user["credential_type"]
        if user["credential_number"]:
            performer["credential_number"] = user["credential_number"]
        if user["credential_expires"]:
            performer["credential_expires"] = user["credential_expires"]
    body["performer"] = performer

    ref = f"CMMS-{install_id(db)}"
    extra = {"source_ref": f"{ref}-ENTRY-{e['id']}", "shift_ref": f"{ref}-SHIFT-{e['shift_id']}",
             "labor_hours": logic.hours(e["minutes"])}
    if e["wo_ref"]:
        extra["work_order"] = e["wo_ref"]
    if e["materials"]:
        extra["materials"] = e["materials"]
    kind = logic.CATEGORIES[e["category"]][1]
    if kind.startswith("work:"):
        body.update(event_type="work.completed",
                    payload={"work_type": kind.split(":")[1], "description": e["notes"], **extra})
    elif kind == "inspection":
        body.update(event_type="inspection.recorded",
                    payload={"result": e["result"] or "pass", "notes": e["notes"], **extra})
    elif kind == "reading":
        body.update(event_type="measurement.recorded",
                    payload={"name": e["reading_name"], "value": e["reading_value"], "unit": e["reading_unit"] or "",
                             "notes": e["notes"], **extra})
    elif kind.startswith("logbook:"):
        body.update(event_type="logbook.entry",
                    payload={"logbook": kind.split(":")[1], "entry": e["notes"], **extra})
    return body


def send_entry(db, eid):
    if mapped(db, "entry", eid):
        return  # already sent (a retry after the answer was lost)
    e = db.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    if not e or e["superseded"] or not shareable(e):
        return  # replaced or no longer about equipment: nothing to send
    ev = _call("POST", _owner("/events"), event_body(db, e))
    _remember(db, "entry", eid, ev["id"])


def _dispatch(db, row):
    payload = json.loads(row["payload"]) if row["payload"] else {}
    if row["kind"] == "entry":
        send_entry(db, row["local_id"])
    elif row["kind"] == "withdraw":
        np_id = payload.get("np_event_id") or mapped(db, "entry", row["local_id"])
        if np_id:
            _call("POST", _owner(f"/events/{np_id}/corrections"),
                  {"reason": payload.get("reason") or "Withdrawn in the shift log",
                   "corrected_payload": {"withdrawn": True}})


@contextmanager
def _sender_lock():
    """Only one sender at a time, even across processes (the app and a sync command)."""
    with open(os.path.join(DATA_DIR, "nameplate-send.lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def process_outbox(db=None, limit=200):
    """Send queued items in order. Returns (sent, failed, waiting)."""
    if not enabled():
        return 0, 0, 0
    own = db is None
    db = db or connect()
    sent = failed = 0
    try:
        with _sender_lock():
            rows = db.execute("SELECT * FROM np_outbox WHERE sent_at IS NULL AND attempts < ? ORDER BY id LIMIT ?",
                              (MAX_ATTEMPTS, limit)).fetchall()
            for row in rows:
                try:
                    _dispatch(db, row)
                    # keep last_error: it says why an earlier try failed
                    db.execute("UPDATE np_outbox SET sent_at=? WHERE id=?",
                               (time.strftime("%Y-%m-%dT%H:%M:%S"), row["id"]))
                    sent += 1
                except NameplateDown as exc:
                    db.execute("UPDATE np_outbox SET last_error=? WHERE id=?", (str(exc)[:300], row["id"]))
                    db.commit()
                    break
                except NameplateRefused as exc:
                    db.execute("UPDATE np_outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                               (str(exc)[:300], row["id"]))
                    failed += 1
                db.commit()
        waiting = db.execute("SELECT COUNT(*) FROM np_outbox WHERE sent_at IS NULL AND attempts < ?",
                             (MAX_ATTEMPTS,)).fetchone()[0]
        return sent, failed, waiting
    finally:
        if own:
            db.close()


def status(db):
    one = lambda sql, *a: db.execute(sql, a).fetchone()[0]  # noqa: E731
    return {
        "waiting": one("SELECT COUNT(*) FROM np_outbox WHERE sent_at IS NULL AND attempts < ?", MAX_ATTEMPTS),
        "gave_up": one("SELECT COUNT(*) FROM np_outbox WHERE sent_at IS NULL AND attempts >= ?", MAX_ATTEMPTS),
        "sent": one("SELECT COUNT(*) FROM np_outbox WHERE sent_at IS NOT NULL"),
        "problems": [dict(r) for r in db.execute(
            "SELECT * FROM np_outbox WHERE last_error IS NOT NULL ORDER BY id DESC LIMIT 10")],
    }


def forget_all(db):
    """After the Nameplate side was wiped (demo reset): forget every link and resend."""
    db.execute("DELETE FROM np_map")
    db.execute("DELETE FROM np_outbox")
    db.commit()


# ---------------------------------------------------------------- background
def _loop():
    while True:
        _wake.wait(timeout=60)
        _wake.clear()
        time.sleep(1)  # let the request that queued the item finish committing
        try:
            with closing(connect()) as db:
                process_outbox(db)
        except Exception as exc:  # never let the sender take the app down
            print("nameplate sender error:", exc, flush=True)


def start_worker():
    if enabled() and os.environ.get("DISABLE_BACKGROUND") != "1":
        threading.Thread(target=_loop, daemon=True, name="nameplate").start()
        _wake.set()
