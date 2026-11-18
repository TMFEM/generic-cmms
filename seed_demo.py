"""Rebuild the demo: made-up crew and a week of shifts at Harbor Point Tower.

    docker compose exec generic-cmms python seed_demo.py

Only runs when DEMO=1. Wipes this app's database first. Run it after resetting the
Nameplate demo building, so the signed shifts land in the fresh demo record.
"""
import os
import sys
from contextlib import closing
from datetime import date, timedelta

if os.environ.get("DEMO") != "1":
    sys.exit("Refusing to run: this wipes the database. Set DEMO=1 (demo instance only).")

import db  # noqa: E402
import logic  # noqa: E402
import nameplate  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

PASSWORD = os.environ.get("DEMO_PASSWORD", "nameplate-demo")
DEMO_BIN = "9999901"
# Fixed ids from the Nameplate demo (scripts/demo_reset.py), the same after every reset.
NP_TECH = {"Dana Ruiz": "00000000-0000-4000-8000-0000000d0001",
           "Marcus Lee": "00000000-0000-4000-8000-0000000d0002"}
TODAY = date.today()


def main():
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db.DB_PATH + suffix):
            os.remove(db.DB_PATH + suffix)
    db.init_db()
    with closing(db.connect()) as c:
        now = logic.fmt(logic.now_local())
        pw = generate_password_hash(PASSWORD)

        def person(name, email, role, trade=None, cred=None, num=None, exp=None):
            return c.execute("""INSERT INTO users (name, email, pw_hash, role, trade, credential_type, credential_number,
                                credential_expires, np_technician_id, created) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                             (name, email, pw if email else None, role, trade, cred, num, exp, NP_TECH.get(name), now)
                             ).lastrowid

        admin = person("Alex Admin", "admin@demo.cmms", "admin", "Office")
        sam = person("Sam Patel", "sam@demo.cmms", "supervisor", "Chief engineer", "FDNY COF F-89", "DEMO-F89-3")
        rosa = person("Rosa Kim", "rosa@demo.cmms", "supervisor", "Assistant chief engineer")
        dana = person("Dana Ruiz", "dana@demo.cmms", "technician", "Building engineer", "EPA 608 Universal",
                      "DEMO-608-1")
        marcus = person("Marcus Lee", "marcus@demo.cmms", "technician", "Plumber", "NYC Licensed Master Plumber",
                        "DEMO-LMP-9", (TODAY + timedelta(days=200)).isoformat())
        jamie = person("Jamie Ortiz", None, "technician", "Porter")
        person("Pat Quinn", "pat@demo.cmms", "manager", "Property manager")
        del admin

        # ------------------------------------------------ the building, from Nameplate
        if not nameplate.enabled():
            sys.exit("Set NAMEPLATE_URL, NAMEPLATE_KEY and NAMEPLATE_OWNER_ID: the demo building comes from Nameplate.")
        hit = nameplate.find_building(DEMO_BIN)
        if not hit:
            sys.exit(f"No building with BIN {DEMO_BIN}. Reset the Nameplate demo first.")
        bid = c.execute("INSERT INTO buildings (name, bin, np_location_id) VALUES (?,?,?)",
                        ("Harbor Point Tower", DEMO_BIN, hit["id"])).lastrowid
        c.commit()
        nameplate.refresh_building(c, bid)
        eq = {r["tag"]: r for r in c.execute("SELECT * FROM np_equipment WHERE building_id=?", (bid,))}
        place = {r["name"]: r for r in c.execute("SELECT * FROM np_places WHERE building_id=?", (bid,))}
        types = {r["name"]: r for r in c.execute("SELECT * FROM shift_types")}

        def shift(days_ago, kind, sup, crew, late=None, called_out=None):
            day = (TODAY - timedelta(days=days_ago)).isoformat()
            t = types[kind]
            start, end = logic.shift_window(day, t["start_time"], t["end_time"])
            sid = c.execute("""INSERT INTO shifts (building_id, shift_type_id, name, starts_at, ends_at, supervisor_id,
                               created) VALUES (?,?,?,?,?,?,?)""",
                            (bid, t["id"], kind, logic.fmt(start), logic.fmt(end), sup, now)).lastrowid
            for uid in {sup, *crew}:
                tin = start + timedelta(minutes=25) if uid == late else start
                status = "late" if uid == late else "on_shift"
                c.execute("INSERT INTO shift_crew (shift_id, user_id, time_in, status, note) VALUES (?,?,?,?,?)",
                          (sid, uid, logic.fmt(tin), status, "Train delay" if uid == late else None))
            if called_out:
                c.execute("INSERT INTO shift_crew (shift_id, user_id, status, note) VALUES (?,?,?,?)",
                          (sid, called_out, "called_out", "Called out sick"))
            return sid, start

        def entry(sid, start, who, cat, at, mins, notes, asset=None, where=None, **kw):
            s_at = start + timedelta(minutes=at)
            subj = eq.get(asset) if asset else None
            pl = place.get(where) if where else None
            v = {"np_asset_id": subj["np_id"] if subj else None, "np_location_id": pl["np_id"] if pl else None,
                 "subject_label": subj["label"] if subj else (pl["path"] if pl else kw.pop("where_text", None))}
            return c.execute("""INSERT INTO entries (shift_id, user_id, category, np_asset_id, np_location_id,
                    subject_label, where_text, started_at, ended_at, minutes, notes, wo_ref, materials, reading_name,
                    reading_value, reading_unit, result, follow_up, follow_up_note, resolves_entry_id, created_by,
                    created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (sid, who, cat, v["np_asset_id"], v["np_location_id"], v["subject_label"],
                              None if subj or pl else v["subject_label"], logic.fmt(s_at),
                              logic.fmt(s_at + timedelta(minutes=mins)), mins, notes, kw.get("wo"), kw.get("materials"),
                              kw.get("rname"), kw.get("rvalue"), kw.get("runit"), kw.get("result"),
                              1 if kw.get("follow") else 0, kw.get("follow"), kw.get("resolves"), sup_of[sid], now)
                             ).lastrowid

        def close(sid, notes=None):
            s = c.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone()
            c.execute("UPDATE shift_crew SET time_out=? WHERE shift_id=? AND time_in IS NOT NULL",
                      (s["ends_at"], sid))
            c.execute("UPDATE shifts SET status='closed', closed_by=supervisor_id, closed_at=?, notes=?, "
                      "turnover_ack_by=supervisor_id, turnover_ack_at=starts_at WHERE id=?",
                      (logic.fmt(logic.parse(s["ends_at"]) + timedelta(minutes=10)), notes, sid))
            nameplate.queue_shift(c, sid)

        sup_of = {}
        follow_open = follow_fixed = None
        for d in range(6, 0, -1):
            out = jamie if d == 3 else None
            sid, st = shift(d, "Day", sam, [dana, marcus] + ([] if out else [jamie]),
                            late=marcus if d == 4 else None, called_out=out)
            sup_of[sid] = sam
            entry(sid, st, sam, "safety", 0, 15, "Morning toolbox talk: ladder safety and the elevator machine room key.")
            entry(sid, st, dana, "rounds", 20, 45, "Morning rounds, cellar to penthouse. Nothing unusual.",
                  where="Mechanical penthouse")
            entry(sid, st, dana, "reading", 70, 10, "Steam pressure at the boiler, reading taken on rounds.",
                  asset="BLR-1", rname="Steam pressure", rvalue=[6, 7, 6.5, 7, 6, 8][d - 1], runit="psi")
            if not out:
                entry(sid, st, jamie, "other", 30, 240, "Lobby and garage sweep, trash out to the curb.",
                      where_text="Lobby and garage")
            if d == 6:
                entry(sid, st, dana, "pm", 120, 90, "Changed filters (4 x 20x25x2), checked belt tension.",
                      asset="RTU-1", wo="WO-2231", materials="4 pleated filters 20x25x2")
                entry(sid, st, marcus, "tenant_request", 60, 50, "Suite 7A: running toilet, replaced the flapper.",
                      where="Suite 7A (tenant)", wo="WO-2234", materials="1 flapper")
            if d == 5:
                entry(sid, st, sam, "vendor", 180, 120, "Escorted the elevator contractor for the monthly maintenance.",
                      where_text="Elevator machine room")
                entry(sid, st, dana, "inspection", 200, 30, "Weekly generator test run, 30 minutes under no load.",
                      asset="GEN-1", result="pass")
                entry(sid, st, dana, "reading", 232, 5, "Generator run hours after the test.", asset="GEN-1",
                      rname="Run hours", rvalue=412.6, runit="hours")
            if d == 4:
                entry(sid, st, marcus, "corrective", 90, 75, "Pinhole leak on the cold water line by the meter, "
                      "temporary clamp installed.", where="Water meter room", wo="WO-2240",
                      materials="1 repair clamp", follow="Replace the section of pipe; parts ordered.")
                entry(sid, st, dana, "training", 300, 60, "Lockout-tagout refresher (in-house).",
                      where_text="Training room")
                entry(sid, st, marcus, "training", 300, 60, "Lockout-tagout refresher (in-house).",
                      where_text="Training room")
            if d == 3:
                entry(sid, st, dana, "event", 150, 35, "Fire alarm: smoke detector in the 7th floor corridor, "
                      "caused by construction dust. FDNY responded, all clear. Panel reset.", asset="FACP-1",
                      follow="Ask the 7th floor contractor to cover detectors while sanding.")
                entry(sid, st, sam, "admin", 200, 45, "Ordered pipe and fittings for the meter room repair.")
            if d == 2:
                follow_open = entry(sid, st, dana, "tenant_request", 100, 40,
                                    "Elevator 1 door slow to close on the lobby level. Cleaned the sill track.",
                                    asset="ELEV-1", wo="WO-2251",
                                    follow="Still slow at times. Call the elevator contractor if it keeps up.")
                entry(sid, st, marcus, "pm", 180, 60, "Annual exercise of the RPZ test cocks before the tester visit.",
                      asset="RPZ-1")
            if d == 1:
                pipe = c.execute("SELECT id FROM entries WHERE follow_up_note LIKE 'Replace the section%'").fetchone()[0]
                follow_fixed = entry(sid, st, marcus, "corrective", 60, 150,
                                     "Replaced 3 ft of 1-inch copper at the meter room, removed the clamp. No leaks.",
                                     where="Water meter room", wo="WO-2240", materials="3 ft 1\" type L copper, 2 couplings",
                                     resolves=pipe)
                c.execute("UPDATE entries SET follow_up_closed_at=?, follow_up_closed_by=? WHERE id=?",
                          (logic.fmt(st + timedelta(minutes=210)), sam, pipe))
                entry(sid, st, dana, "pm", 240, 45, "Greased the supply fan bearings, checked the drain pan.",
                      asset="AHU-1", wo="WO-2255")
            close(sid, notes={4: "Marcus 25 min late (train). Meter room leak clamped; pipe on order.",
                              3: "Fire alarm at 9:30, construction dust. FDNY all clear. Jamie called out sick."}.get(d))

            if d in (5, 2):  # an evening shift now and then
                esid, est = shift(d, "Evening", rosa, [])
                sup_of[esid] = rosa
                entry(esid, est, rosa, "rounds", 30, 40, "Evening rounds. Roof doors locked.", where="Roof")
                entry(esid, est, rosa, "reading", 75, 5, "Steam pressure at the boiler.", asset="BLR-1",
                      rname="Steam pressure", rvalue=6.5, runit="psi")
                close(esid)

        # ------------------------------------------------ today: an open day shift
        sid, st = shift(0, "Day", sam, [dana, marcus, jamie])
        sup_of[sid] = sam
        entry(sid, st, sam, "safety", 0, 15, "Toolbox talk: working near the roof edge.")
        entry(sid, st, dana, "rounds", 20, 40, "Morning rounds. Penthouse fan noisy, see below.",
              where="Mechanical penthouse")
        entry(sid, st, dana, "reading", 65, 5, "Steam pressure at the boiler.", asset="BLR-1",
              rname="Steam pressure", rvalue=7, runit="psi")
        del follow_open, follow_fixed
        c.commit()

        n = c.execute("SELECT COUNT(*) FROM np_outbox WHERE sent_at IS NULL").fetchone()[0]
        total_sent = 0
        while True:
            sent, failed, waiting = nameplate.process_outbox(c)
            total_sent += sent
            if not waiting or not (sent or failed):
                break
        print("\nDemo rebuilt: Harbor Point Tower crew log")
        print(f"  {c.execute('SELECT COUNT(*) FROM shifts').fetchone()[0]} shifts (one open today), "
              f"{c.execute('SELECT COUNT(*) FROM entries').fetchone()[0]} entries")
        print(f"  {total_sent} of {n} signed entries sent to Nameplate"
              + ("" if total_sent == n else "  <- check the Nameplate page"))
        print(f"\nSign-ins (password for all: {PASSWORD})")
        for u in c.execute("SELECT name, email, role FROM users WHERE email IS NOT NULL ORDER BY role, name"):
            print(f"  {u['role']:<11} {u['email']:<20} {u['name']}")


if __name__ == "__main__":
    main()
