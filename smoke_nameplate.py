"""End-to-end check against a running Nameplate (use the demo instance, never the real record).

    NAMEPLATE_URL=http://localhost:8111 NAMEPLATE_KEY=<Generic CMMS key> \\
    NAMEPLATE_OWNER_ID=00000000-0000-4000-8000-00000000d3e0 python3 smoke_nameplate.py

Needs the demo building (BIN 9999901). Adds a few entries to it, which the next demo reset clears.
"""
import os
import re
import sys
import tempfile

if not (os.environ.get("NAMEPLATE_URL") and os.environ.get("NAMEPLATE_KEY") and os.environ.get("NAMEPLATE_OWNER_ID")):
    print("skipped: set NAMEPLATE_URL, NAMEPLATE_KEY and NAMEPLATE_OWNER_ID to run")
    sys.exit(0)
os.environ.update(DATA_DIR=tempfile.mkdtemp(), DISABLE_BACKGROUND="1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from werkzeug.security import generate_password_hash  # noqa: E402

import app as appmod  # noqa: E402
import db as dbmod  # noqa: E402
import nameplate  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print("FAIL:", name, extra)


def events_for(source_ref):
    """Nameplate entries this test wrote, found by our reference in the payload."""
    out = []
    for e in nameplate._call("GET", nameplate._owner("/events?limit=500")) or []:
        if (e.get("payload") or {}).get("source_ref") == source_ref:
            out.append(e)
    return out


ok_ping, msg = nameplate.ping()
check("connected", ok_ping, msg)
c = dbmod.connect()
pw = generate_password_hash("test-password-1")
sup = c.execute("INSERT INTO users (name, email, pw_hash, role, created) VALUES ('Sue Super','sue@x',?,'supervisor','x')",
                (pw,)).lastrowid
tech = c.execute("""INSERT INTO users (name, email, pw_hash, role, credential_type, credential_number, created)
                    VALUES ('Tom Tech','tom@x',?,'technician','EPA 608 Universal','T-1','x')""", (pw,)).lastrowid
hit = nameplate.find_building("9999901")
check("demo building found by BIN", hit, "reset the Nameplate demo first")
bid = c.execute("INSERT INTO buildings (name, bin, np_location_id) VALUES ('Harbor Point Tower','9999901',?)",
                (hit["id"],)).lastrowid
c.commit()
n = nameplate.refresh_building(c, bid)
check("equipment read from Nameplate", n >= 5, n)
boiler = c.execute("SELECT * FROM np_equipment WHERE tag='BLR-1'").fetchone()
room = c.execute("SELECT * FROM np_places WHERE name='Boiler room'").fetchone()
check("places have paths", room and room["path"] == "Cellar › Boiler room", room and dict(room))

cl = appmod.app.test_client()
cl.post("/login", data={"email": "sue@x", "password": "test-password-1"})
t = re.search(r'name="_csrf" value="([^"]+)"', cl.get("/").get_data(as_text=True)).group(1)
day_type = c.execute("SELECT id FROM shift_types WHERE name='Day'").fetchone()[0]
cl.post("/shifts/new", data={"_csrf": t, "building_id": bid, "shift_type_id": day_type, "day": "2026-01-05",
                             "supervisor_id": sup, "crew": [tech]})
sid = c.execute("SELECT max(id) FROM shifts").fetchone()[0]


def add(**form):
    r = cl.post(f"/shifts/{sid}/entries/new", data={"_csrf": t, "user_id": tech, **form})
    check(f"entry {form.get('category')}", r.status_code == 302, r.get_data(as_text=True)[-400:])
    return c.execute("SELECT max(id) FROM entries").fetchone()[0]


reading = add(category="reading", subject=f"asset:{boiler['np_id']}", start="08:00", end="08:05",
              notes="Steam pressure", reading_name="Steam pressure", reading_value="9", reading_unit="psi")
repair = add(category="corrective", subject=f"place:{room['np_id']}", dur_h="1", dur_m="30",
             notes="Replaced a leaking union on the condensate return", wo_ref="WO-T1", materials="1 union")
training = add(category="training", dur_h="1", notes="Lockout-tagout refresher", where_text="Training room")
loose = add(category="pm", dur_m="20", notes="Checked the roof drains", where_text="Roof")

cl.post(f"/shifts/{sid}/close", data={"_csrf": t, "fill_out": "1"})
queued = c.execute("SELECT COUNT(*) FROM np_outbox WHERE sent_at IS NULL").fetchone()[0]
check("only linked maintenance entries queued", queued == 2, queued)
sent, failed, waiting = nameplate.process_outbox(c)
check("sent", (sent, failed, waiting) == (2, 0, 0), (sent, failed, waiting))

ev = events_for(f"CMMS-{dbmod.install_id(c)}-ENTRY-{reading}")
check("reading arrived as a measurement", len(ev) == 1 and ev[0]["event_type"] == "measurement.recorded"
      and ev[0]["payload"]["value"] == 9 and ev[0]["payload"]["unit"] == "psi", ev)
check("performer and credential", ev and ev[0]["payload"]["performer"]["credential_type"] == "EPA 608 Universal")
check("labor hours", ev and ev[0]["payload"]["labor_hours"] == 0.08)
rp = events_for(f"CMMS-{dbmod.install_id(c)}-ENTRY-{repair}")
check("repair arrived as corrective work on the place", len(rp) == 1 and rp[0]["event_type"] == "work.completed"
      and rp[0]["payload"]["work_type"] == "corrective" and rp[0]["subject_type"] == "location"
      and rp[0]["payload"]["work_order"] == "WO-T1" and rp[0]["payload"]["labor_hours"] == 1.5, rp)
check("training stayed here", not events_for(f"CMMS-{dbmod.install_id(c)}-ENTRY-{training}"))
check("unlinked work stayed here", not events_for(f"CMMS-{dbmod.install_id(c)}-ENTRY-{loose}"))

# correction after sign-off: the old Nameplate entry is withdrawn, the new one added
cl.post(f"/entries/{reading}/correct", data={"_csrf": t, "user_id": tech, "category": "reading",
                                             "subject": f"asset:{boiler['np_id']}", "start": "08:00", "end": "08:05",
                                             "notes": "Steam pressure", "reading_name": "Steam pressure",
                                             "reading_value": "6", "reading_unit": "psi", "reason": "Misread the gauge"})
new_id = c.execute("SELECT id FROM entries WHERE corrects_entry_id=?", (reading,)).fetchone()[0]
sent, failed, waiting = nameplate.process_outbox(c)
check("correction sent", (sent, failed, waiting) == (2, 0, 0), (sent, failed, waiting))
hist = nameplate._call("GET", nameplate._owner(f"/assets/{boiler['np_id']}/history"))
old_np = nameplate.mapped(c, "entry", reading)
check("withdrawal recorded against the original",
      any(h["event_type"] == "record.corrected" and h.get("corrects_event_id") == old_np for h in hist), old_np)
nw = events_for(f"CMMS-{dbmod.install_id(c)}-ENTRY-{new_id}")
check("replacement reading in Nameplate", len(nw) == 1 and nw[0]["payload"]["value"] == 6, nw)
v = nameplate._call("GET", nameplate._owner("/verify"))
check("chain still valid", v["valid"], v)
check("shift report marks entries in Nameplate", b"In Nameplate" in cl.get(f"/shifts/{sid}/report").data)

print(f"{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
