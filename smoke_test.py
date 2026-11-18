"""Checks the main flows without Nameplate. Uses a throwaway database.

    python3 smoke_test.py
"""
import os
import re
import sys
import tempfile

os.environ.update(DATA_DIR=tempfile.mkdtemp(), DISABLE_BACKGROUND="1", NAMEPLATE_URL="", NAMEPLATE_KEY="",
                  NAMEPLATE_OWNER_ID="")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from werkzeug.security import generate_password_hash  # noqa: E402

import app as appmod  # noqa: E402
import db as dbmod  # noqa: E402
import logic  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print("FAIL:", name, extra)


# ---------------------------------------------------------------- setup: people, a building, equipment
c = dbmod.connect()
pw = generate_password_hash("test-password-1")
ids = {}
for name, email, role in [("Sue Super", "sue@x", "supervisor"), ("Tom Tech", "tom@x", "technician"),
                          ("Tia Tech", "tia@x", "technician"), ("Max Manager", "max@x", "manager"),
                          ("Ada Admin", "ada@x", "admin")]:
    ids[name] = c.execute("INSERT INTO users (name, email, pw_hash, role, created) VALUES (?,?,?,?, '2026-01-01')",
                          (name, email, pw, role)).lastrowid
ids["Hal Helper"] = c.execute("INSERT INTO users (name, role, created) VALUES ('Hal Helper','technician','2026-01-01')").lastrowid
bid = c.execute("INSERT INTO buildings (name, np_location_id) VALUES ('Test Tower','loc-b')").lastrowid
c.execute("INSERT INTO np_places VALUES ('loc-b', ?, 'Test Tower', 'facility', 'Whole building')", (bid,))
c.execute("INSERT INTO np_places VALUES ('loc-br', ?, 'Boiler room', 'space', 'Cellar › Boiler room')", (bid,))
c.execute("INSERT INTO np_equipment VALUES ('ast-1', ?, 'BLR-1', 'BLR-1 Test boiler', 'loc-br', 'Cellar › Boiler room')", (bid,))
c.commit()
types = {r["name"]: r["id"] for r in c.execute("SELECT * FROM shift_types")}


def client(email):
    cl = appmod.app.test_client()
    r = cl.post("/login", data={"email": email, "password": "test-password-1"})
    check(f"login {email}", r.status_code == 302, r.status_code)
    cl.get("/")
    return cl


def tok(cl, path):
    html = cl.get(path).get_data(as_text=True)
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


sue, tom, tia, mgr = client("sue@x"), client("tom@x"), client("tia@x"), client("max@x")
check("wrong password refused", appmod.app.test_client().post(
    "/login", data={"email": "sue@x", "password": "nope"}).status_code == 200)

# ---------------------------------------------------------------- start a night shift (crosses midnight)
t = tok(sue, "/")
r = sue.post("/shifts/new", data={"_csrf": t, "building_id": bid, "shift_type_id": types["Night"], "day": "2026-11-10",
                                  "supervisor_id": ids["Sue Super"], "crew": [ids["Tom Tech"], ids["Hal Helper"]]})
check("start shift", r.status_code == 302, r.status_code)
sid = c.execute("SELECT id FROM shifts").fetchone()[0]
s = c.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone()
check("night shift ends next morning", s["starts_at"] == "2026-11-10T23:00" and s["ends_at"] == "2026-11-11T07:00", dict(s))
check("crew added with time in", c.execute("SELECT COUNT(*) FROM shift_crew WHERE shift_id=? AND time_in='2026-11-10T23:00'",
                                           (sid,)).fetchone()[0] == 3)
check("technician can't start shifts", tom.post("/shifts/new", data={"_csrf": tok(tom, "/")}).status_code == 403)
check("manager can't start shifts", mgr.post("/shifts/new", data={"_csrf": tok(mgr, "/")}).status_code == 403)
check("shift page loads", b"Night shift" in sue.get(f"/shifts/{sid}").data)
check("duplicate shift refused", sue.post("/shifts/new", data={"_csrf": t, "building_id": bid, "shift_type_id": types["Night"],
                                                               "day": "2026-11-10"}).status_code == 302
      and c.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 1)

# ---------------------------------------------------------------- entries
t = tok(tom, f"/shifts/{sid}/entries/new")
r = tom.post(f"/shifts/{sid}/entries/new", data={"_csrf": t, "category": "reading", "subject": "asset:ast-1",
                                                  "start": "01:30", "end": "01:40", "notes": "Steam pressure on rounds",
                                                  "reading_name": "Steam pressure", "reading_value": "7.5",
                                                  "reading_unit": "psi"})
check("tech logs a reading", r.status_code == 302, r.get_data(as_text=True)[:300])
e = c.execute("SELECT * FROM entries ORDER BY id DESC").fetchone()
check("after-midnight time lands on the next day", e["started_at"] == "2026-11-11T01:30" and e["minutes"] == 10, dict(e))
check("entry is Tom's", e["user_id"] == ids["Tom Tech"])
check("subject label from the equipment list", e["subject_label"] == "BLR-1 Test boiler")

r = tom.post(f"/shifts/{sid}/entries/new", data={"_csrf": t, "category": "training", "dur_h": "1", "dur_m": "30",
                                                  "notes": "Confined space refresher", "where_text": "Training room",
                                                  "user_id": ids["Hal Helper"]})
e2 = c.execute("SELECT * FROM entries ORDER BY id DESC").fetchone()
check("duration-only entry", e2["minutes"] == 90 and e2["started_at"] is None, dict(e2))
check("technician can't log for someone else", e2["user_id"] == ids["Tom Tech"])

r = tom.post(f"/shifts/{sid}/entries/new", data={"_csrf": t, "category": "pm", "notes": ""})
check("missing time and notes rejected", r.status_code == 200 and b"how long it took" in r.data and b"Say what was done" in r.data)
r = tom.post(f"/shifts/{sid}/entries/new", data={"_csrf": t, "category": "reading", "dur_m": "5", "notes": "x",
                                                  "reading_name": "Temp", "reading_value": "abc"})
check("reading needs a number and equipment", b"needs a number" in r.data and b"Pick the equipment" in r.data)
r = tom.post(f"/shifts/{sid}/entries/new", data={"_csrf": t, "category": "inspection", "dur_m": "5", "notes": "x"})
check("inspection needs a result", b"Pick the inspection" in r.data)

t = tok(sue, f"/shifts/{sid}/entries/new")
r = sue.post(f"/shifts/{sid}/entries/new", data={"_csrf": t, "category": "corrective", "subject": "place:loc-br",
                                                  "user_id": ids["Hal Helper"], "dur_h": "2", "notes": "Cleared the floor drain",
                                                  "follow_up": "1", "follow_up_note": "Drain still slow, call the plumber"})
fu = c.execute("SELECT * FROM entries ORDER BY id DESC").fetchone()
check("supervisor logs for the helper, with a follow-up", fu["user_id"] == ids["Hal Helper"] and fu["follow_up"] == 1)
r = sue.post(f"/shifts/{sid}/entries/new", data={"_csrf": t, "category": "pm", "dur_m": "20", "notes": "x",
                                                  "user_id": ids["Tia Tech"]})
check("can't log for someone not on the crew", b"isn&#39;t on this shift" in r.data or b"isn't on this shift" in r.data)
check("Tia (not on crew) can't add entries", tia.get(f"/shifts/{sid}/entries/new").status_code == 403)
t2 = tok(tia, "/")
tia.post(f"/shifts/{sid}/crew", data={"_csrf": t2, "action": "join"})
check("Tia joins the shift", c.execute("SELECT 1 FROM shift_crew WHERE shift_id=? AND user_id=?", (sid, ids["Tia Tech"])).fetchone())
check("now Tia can add entries", tia.get(f"/shifts/{sid}/entries/new").status_code == 200)

# edit rules
check("Tom edits his own entry", tom.get(f"/entries/{e['id']}/edit").status_code == 200)
check("Tia can't edit Tom's entry", tia.get(f"/entries/{e['id']}/edit").status_code == 403)
t = tok(tom, f"/entries/{e['id']}/edit")
tom.post(f"/entries/{e['id']}/edit", data={"_csrf": t, "category": "reading", "subject": "asset:ast-1", "start": "01:30",
                                           "end": "01:45", "notes": "Steam pressure on rounds", "reading_name": "Steam pressure",
                                           "reading_value": "8", "reading_unit": "psi"})
e = c.execute("SELECT * FROM entries WHERE id=?", (e["id"],)).fetchone()
check("edit saved", e["reading_value"] == 8 and e["minutes"] == 15)

# crew line: late arrival, call-out
t = tok(sue, f"/shifts/{sid}")
sue.post(f"/shifts/{sid}/crew", data={"_csrf": t, "action": "save", "user_id": ids["Tom Tech"], "status": "late",
                                      "time_in": "23:20", "time_out": "07:00", "note": "Car trouble"})
row = c.execute("SELECT * FROM shift_crew WHERE shift_id=? AND user_id=?", (sid, ids["Tom Tech"])).fetchone()
check("late, time out next morning", row["status"] == "late" and row["time_in"] == "2026-11-10T23:20"
      and row["time_out"] == "2026-11-11T07:00", dict(row))
check("tech can't edit others' crew lines", tom.post(f"/shifts/{sid}/crew", data={
    "_csrf": tok(tom, "/"), "action": "save", "user_id": ids["Hal Helper"], "status": "called_out"}).status_code == 403)

# ---------------------------------------------------------------- close
check("technician can't close", tom.post(f"/shifts/{sid}/close", data={"_csrf": tok(tom, "/")}).status_code == 403)
r = sue.post(f"/shifts/{sid}/close", data={"_csrf": t, "notes": "Quiet night"})
check("close blocked while time out is missing", c.execute("SELECT status FROM shifts WHERE id=?", (sid,)).fetchone()[0] == "open")
sue.post(f"/shifts/{sid}/close", data={"_csrf": t, "notes": "Quiet night", "fill_out": "1"})
s = c.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone()
check("closed and signed", s["status"] == "closed" and s["closed_by"] == ids["Sue Super"])
check("missing time out filled with shift end",
      c.execute("SELECT COUNT(*) FROM shift_crew WHERE shift_id=? AND time_out IS NULL AND time_in IS NOT NULL", (sid,)).fetchone()[0] == 0)
check("nothing queued without Nameplate", c.execute("SELECT COUNT(*) FROM np_outbox").fetchone()[0] == 0)
check("closed entries can't be edited", tom.get(f"/entries/{e['id']}/edit").status_code == 403)
check("technician can't correct", tom.get(f"/entries/{e['id']}/correct").status_code == 403)

# ---------------------------------------------------------------- correction after sign-off
t = tok(sue, f"/entries/{e['id']}/correct")
r = sue.post(f"/entries/{e['id']}/correct", data={"_csrf": t, "category": "reading", "subject": "asset:ast-1", "start": "01:30",
                                                  "end": "01:45", "notes": "Steam pressure on rounds", "reading_name": "Steam pressure",
                                                  "reading_value": "6", "reading_unit": "psi", "reason": "Misread the gauge"})
old = c.execute("SELECT * FROM entries WHERE id=?", (e["id"],)).fetchone()
new = c.execute("SELECT * FROM entries WHERE corrects_entry_id=?", (e["id"],)).fetchone()
check("original kept and marked replaced", old["superseded"] == 1 and old["reading_value"] == 8)
check("replacement saved with the reason", new and new["reading_value"] == 6 and "Misread the gauge" in new["notes"])
check("correction needs a reason", b"Give a reason" in sue.post(f"/entries/{new['id']}/correct", data={
    "_csrf": t, "category": "reading", "notes": "x", "dur_m": "5"}).data)
r = sue.post(f"/entries/{e2['id']}/correct", data={"_csrf": t, "withdraw": "1", "reason": "Logged twice"})
check("withdrawn", c.execute("SELECT superseded FROM entries WHERE id=?", (e2["id"],)).fetchone()[0] == 1)

# ---------------------------------------------------------------- next shift: turnover and resolving the follow-up
t = tok(sue, "/")
sue.post("/shifts/new", data={"_csrf": t, "building_id": bid, "shift_type_id": types["Day"], "day": "2026-11-11",
                              "supervisor_id": ids["Sue Super"], "crew": [ids["Tia Tech"]]})
sid2 = c.execute("SELECT max(id) FROM shifts").fetchone()[0]
page = sue.get(f"/shifts/{sid2}").get_data(as_text=True)
check("follow-up carried into the next shift", "Drain still slow" in page)
sue.post(f"/shifts/{sid2}/turnover", data={"_csrf": t, "note": "Walked the cellar with Sue"})
check("turnover received", c.execute("SELECT turnover_ack_by FROM shifts WHERE id=?", (sid2,)).fetchone()[0] == ids["Sue Super"])
check("resolve link prefills", b"Cleared the floor drain" in tia.get(f"/shifts/{sid2}/entries/new?resolves={fu['id']}").data
      or b"loc-br" in tia.get(f"/shifts/{sid2}/entries/new?resolves={fu['id']}").data)
t = tok(tia, f"/shifts/{sid2}/entries/new")
tia.post(f"/shifts/{sid2}/entries/new", data={"_csrf": t, "category": "corrective", "subject": "place:loc-br",
                                              "start": "08:00", "end": "09:15", "notes": "Snaked the drain, runs clear",
                                              "resolves_entry_id": fu["id"]})
check("follow-up closed by the fix", c.execute("SELECT follow_up_closed_at FROM entries WHERE id=?", (fu["id"],)).fetchone()[0])
check("follow-ups page empty now", b"Nothing open" in sue.get("/follow-ups").data)
page = sue.get(f"/shifts/{sid2}/report").get_data(as_text=True)
check("report shows the item carried in as closed", "Drain still slow" in page and "closed" in page)

# ---------------------------------------------------------------- reports
r = sue.get(f"/shifts/{sid}/report")
check("shift report", r.status_code == 200 and b"Quiet night" in r.data and b"Misread the gauge" in r.data)
r = mgr.get(f"/reports?building={bid}&from=2026-11-10&to=2026-11-11")
html = r.get_data(as_text=True)
check("manager sees the summary", r.status_code == 200 and "Tom Tech" in html and "Hal Helper" in html)
check("withdrawn entry not counted", "Confined space" not in html)
r = mgr.get(f"/reports/entries.csv?building={bid}&from=2026-11-10&to=2026-11-11")
csv = r.get_data(as_text=True)
check("CSV export", r.status_code == 200 and "Snaked the drain" in csv and "Misread the gauge" in csv
      and "Logged twice" not in csv and csv.count("\n") == 4, csv)
check("manager can't open people admin", mgr.get("/people/new").status_code == 403)
check("tech can't see reports", tom.get("/reports").status_code == 403)
for path in ["/", "/shifts", "/follow-ups", "/people", "/account", f"/shifts/{sid}", "/reports"]:
    check(f"page {path}", sue.get(path).status_code == 200, path)
ada = client("ada@x")
for path in ["/buildings", "/shift-times", "/nameplate", "/people/new", f"/people/{ids['Tom Tech']}"]:
    check(f"admin page {path}", ada.get(path).status_code == 200, path)
check("CSRF required", sue.post(f"/shifts/{sid2}/close", data={}).status_code == 400)

print(f"{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
