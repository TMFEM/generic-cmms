"""Numbers for the shift report and the management summary. No Flask in here."""
from collections import defaultdict

import logic
import nameplate


def crew_rows(db, shift_id, until=None):
    """Crew with minutes on shift. `until` (for an open shift) counts time so far for anyone still on."""
    rows = db.execute("""SELECT c.*, u.name, u.trade, u.role FROM shift_crew c JOIN users u ON u.id=c.user_id
                         WHERE c.shift_id=? ORDER BY u.name""", (shift_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        tin, tout = logic.parse(r["time_in"]), logic.parse(r["time_out"])
        d["so_far"] = bool(tin and not tout and until)
        if d["so_far"]:
            tout = max(tin, until)
        d["minutes"] = logic.minutes_between(tin, tout) if tin and tout else None
        out.append(d)
    return out


def entry_rows(db, where, args):
    rows = db.execute(f"""SELECT e.*, u.name AS person, s.status AS shift_status, s.name AS shift_name,
                                 s.starts_at AS shift_start, b.name AS building
                          FROM entries e JOIN users u ON u.id=e.user_id JOIN shifts s ON s.id=e.shift_id
                          JOIN buildings b ON b.id=s.building_id
                          WHERE {where}
                          ORDER BY COALESCE(e.started_at, e.ended_at, s.starts_at), e.id""", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["np_event_id"] = nameplate.mapped(db, "entry", r["id"])
        if not nameplate.shareable(r):
            d["np_status"] = "stays here" if r["category"] not in logic.MAINTENANCE else "no equipment or place chosen"
        elif d["np_event_id"]:
            d["np_status"] = "in Nameplate"
        elif r["shift_status"] == "open":
            d["np_status"] = "sent when the shift is closed"
        else:
            d["np_status"] = "waiting to send"
        out.append(d)
    return out


def open_follow_ups(db, building_id, before=None):
    """Follow-ups not yet closed. With `before`, those raised before that time and still open then."""
    q = """SELECT e.*, u.name AS person, s.starts_at AS shift_start, s.name AS shift_name
           FROM entries e JOIN shifts s ON s.id=e.shift_id JOIN users u ON u.id=e.user_id
           WHERE s.building_id=? AND e.follow_up=1 AND e.superseded=0"""
    args = [building_id]
    if before:
        q += " AND s.starts_at < ? AND (e.follow_up_closed_at IS NULL OR e.follow_up_closed_at > ?)"
        args += [before, before]
    else:
        q += " AND e.follow_up_closed_at IS NULL"
    return db.execute(q + " ORDER BY s.starts_at, e.id", args).fetchall()


def shift_report(db, shift_id):
    s = db.execute("""SELECT s.*, b.name AS building, u.name AS supervisor, c.name AS closed_by_name,
                             t.name AS turnover_by_name
                      FROM shifts s JOIN buildings b ON b.id=s.building_id
                      LEFT JOIN users u ON u.id=s.supervisor_id LEFT JOIN users c ON c.id=s.closed_by
                      LEFT JOIN users t ON t.id=s.turnover_ack_by WHERE s.id=?""", (shift_id,)).fetchone()
    if not s:
        return None
    until = min(logic.now_local(), logic.parse(s["ends_at"])) if s["status"] == "open" else None
    crew = crew_rows(db, shift_id, until)
    entries = entry_rows(db, "e.shift_id=?", (shift_id,))
    active = [e for e in entries if not e["superseded"]]
    by_cat = defaultdict(int)
    by_person = defaultdict(int)
    for e in active:
        by_cat[e["category"]] += e["minutes"] or 0
        by_person[e["user_id"]] += e["minutes"] or 0
    for c in crew:
        c["logged"] = by_person.get(c["user_id"], 0)
    return {
        "shift": s, "crew": crew, "entries": active,
        "corrected": [e for e in entries if e["superseded"]],
        "by_category": sorted(by_cat.items(), key=lambda kv: -kv[1]),
        "total_logged": sum(by_cat.values()),
        "crew_minutes": sum(c["minutes"] or 0 for c in crew),
        "so_far": any(c["so_far"] for c in crew),
        "readings": [e for e in active if e["category"] == "reading"],
        "events": [e for e in active if e["category"] in ("event", "emergency")],
        "raised": [e for e in active if e["follow_up"]],
        "resolved": [e for e in active if e["resolves_entry_id"]],
        "carried_in": open_follow_ups(db, s["building_id"], before=s["starts_at"]),
    }


def summary(db, building_id, start, end):
    """Management view for a date range (end inclusive)."""
    where = "s.building_id=? AND s.starts_at >= ? AND s.starts_at < date(?, '+1 day') AND e.superseded=0"
    entries = entry_rows(db, where, (building_id, start, end))
    shifts = db.execute("""SELECT * FROM shifts WHERE building_id=? AND starts_at >= ? AND starts_at < date(?, '+1 day')
                           ORDER BY starts_at""", (building_id, start, end)).fetchall()
    crew_minutes = defaultdict(int)
    crew_shifts = defaultdict(int)
    call_outs = defaultdict(int)
    names = {}
    for s in shifts:
        for c in crew_rows(db, s["id"]):
            names[c["user_id"]] = c["name"]
            if c["status"] == "called_out":
                call_outs[c["user_id"]] += 1
                continue
            crew_shifts[c["user_id"]] += 1
            crew_minutes[c["user_id"]] += c["minutes"] or 0
    cats = [k for k in logic.CATEGORIES]
    by_cat = defaultdict(int)
    grid = defaultdict(lambda: defaultdict(int))  # person -> category -> minutes
    for e in entries:
        names.setdefault(e["user_id"], e["person"])
        by_cat[e["category"]] += e["minutes"] or 0
        grid[e["user_id"]][e["category"]] += e["minutes"] or 0
    people = []
    for uid, name in sorted(names.items(), key=lambda kv: kv[1]):
        logged = sum(grid[uid].values())
        people.append({"name": name, "shifts": crew_shifts[uid], "on_shift": crew_minutes[uid], "logged": logged,
                       "by_cat": grid[uid], "call_outs": call_outs[uid]})
    pm, corrective = by_cat["pm"], by_cat["corrective"] + by_cat["tenant_request"] + by_cat["emergency"]
    return {
        "shifts": shifts, "entries": entries, "people": people,
        "cats": [c for c in cats if by_cat[c]], "by_cat": by_cat,
        "total": sum(by_cat.values()), "on_shift": sum(crew_minutes.values()),
        "maintenance": sum(v for k, v in by_cat.items() if k in logic.MAINTENANCE),
        "pm_share": round(100 * pm / (pm + corrective)) if pm + corrective else None,
        "open_shifts": sum(1 for s in shifts if s["status"] == "open"),
        "follow_ups": open_follow_ups(db, building_id),
        "in_nameplate": sum(1 for e in entries if e["np_event_id"]),
    }
