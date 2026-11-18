"""Shifts, crew, entries, follow-ups and sign-off."""
from datetime import date, timedelta

from flask import abort, flash, redirect, render_template, request, url_for

import logic
import nameplate
import reports
from core import all_, app, current_user, get_db, login_required, one, role_required


# ---------------------------------------------------------------- helpers
def _shift_or_404(sid):
    s = one("SELECT s.*, b.name AS building FROM shifts s JOIN buildings b ON b.id=s.building_id WHERE s.id=?", sid)
    if not s:
        abort(404)
    return s


def _crew_ids(sid):
    return {r["user_id"] for r in all_("SELECT user_id FROM shift_crew WHERE shift_id=?", sid)}


def _entry_or_404(eid):
    e = one("SELECT * FROM entries WHERE id=?", eid)
    if not e:
        abort(404)
    return e


def _pickers(building_id):
    return (all_("SELECT * FROM np_equipment WHERE building_id=? ORDER BY tag, label", building_id),
            all_("SELECT * FROM np_places WHERE building_id=? ORDER BY path", building_id))


# ---------------------------------------------------------------- home
@app.route("/")
@login_required
def home():
    me = current_user()
    open_shifts = all_("""SELECT s.*, b.name AS building, u.name AS supervisor,
                                 (SELECT COUNT(*) FROM shift_crew c WHERE c.shift_id=s.id) AS crew_n,
                                 (SELECT COUNT(*) FROM entries e WHERE e.shift_id=s.id AND e.superseded=0) AS entry_n
                          FROM shifts s JOIN buildings b ON b.id=s.building_id LEFT JOIN users u ON u.id=s.supervisor_id
                          WHERE s.status='open' ORDER BY s.starts_at""")
    recent = all_("""SELECT s.*, b.name AS building, u.name AS supervisor,
                            (SELECT COUNT(*) FROM entries e WHERE e.shift_id=s.id AND e.superseded=0) AS entry_n
                     FROM shifts s JOIN buildings b ON b.id=s.building_id LEFT JOIN users u ON u.id=s.supervisor_id
                     WHERE s.status='closed' ORDER BY s.starts_at DESC LIMIT 8""")
    mine = [s for s in open_shifts if me["id"] in _crew_ids(s["id"])]
    follow = all_("""SELECT COUNT(*) AS n FROM entries WHERE follow_up=1 AND follow_up_closed_at IS NULL
                     AND superseded=0""")[0]["n"]
    buildings = all_("SELECT * FROM buildings ORDER BY name")
    types = all_("SELECT * FROM shift_types ORDER BY sort")
    crew = all_("SELECT * FROM users WHERE active=1 AND role IN ('technician','supervisor') ORDER BY name")
    now = logic.now_local()
    guess = next((t for t in types if _in_window(now, t)), types[0] if types else None)
    return render_template("home.html", open_shifts=open_shifts, recent=recent, mine=mine, follow=follow,
                           buildings=buildings, types=types, crew=crew, today=now.date().isoformat(),
                           guess=guess, supervisors=[c for c in crew if c["role"] == "supervisor"])


def _in_window(now, t):
    for day in (now.date(), now.date() - timedelta(days=1)):
        start, end = logic.shift_window(day.isoformat(), t["start_time"], t["end_time"])
        if start <= now < end:
            return True
    return False


# ---------------------------------------------------------------- starting a shift
@app.post("/shifts/new")
@role_required("admin", "supervisor")
def shift_new():
    db, me, f = get_db(), current_user(), request.form
    b = one("SELECT * FROM buildings WHERE id=?", f.get("building_id", type=int))
    t = one("SELECT * FROM shift_types WHERE id=?", f.get("shift_type_id", type=int))
    day = f.get("day") or date.today().isoformat()
    if not b or not t:
        flash("Pick a building and a shift.", "error")
        return redirect(url_for("home"))
    start, end = logic.shift_window(day, t["start_time"], t["end_time"])
    dup = one("SELECT id FROM shifts WHERE building_id=? AND starts_at=?", b["id"], logic.fmt(start))
    if dup:
        flash("That shift already exists.", "error")
        return redirect(url_for("shift_page", sid=dup["id"]))
    sup = f.get("supervisor_id", type=int) or me["id"]
    cur = db.execute("""INSERT INTO shifts (building_id, shift_type_id, name, starts_at, ends_at, supervisor_id, created)
                        VALUES (?,?,?,?,?,?,?)""",
                     (b["id"], t["id"], t["name"], logic.fmt(start), logic.fmt(end), sup, logic.fmt(logic.now_local())))
    sid = cur.lastrowid
    people = {int(x) for x in f.getlist("crew")} | {sup}
    for uid in people:
        db.execute("INSERT INTO shift_crew (shift_id, user_id, time_in) VALUES (?,?,?)", (sid, uid, logic.fmt(start)))
    db.commit()
    return redirect(url_for("shift_page", sid=sid))


# ---------------------------------------------------------------- the shift page
@app.route("/shifts/<int:sid>")
@login_required
def shift_page(sid):
    me = current_user()
    rep = reports.shift_report(get_db(), sid)
    if not rep:
        abort(404)
    s = rep["shift"]
    crew_ids = {c["user_id"] for c in rep["crew"]}
    others = all_("SELECT * FROM users WHERE active=1 AND role IN ('technician','supervisor') ORDER BY name")
    return render_template("shift.html", r=rep, s=s, crew_ids=crew_ids,
                           can_add=logic.can_add_entry(me, s, crew_ids),
                           can_edit=lambda e: logic.can_edit_entry(me, s, e),
                           not_on_crew=[u for u in others if u["id"] not in crew_ids],
                           missing_out=[c for c in rep["crew"] if c["time_in"] and not c["time_out"]])


@app.post("/shifts/<int:sid>/crew")
@login_required
def shift_crew(sid):
    """Supervisors edit anyone's line; technicians can only add themselves or clock themselves out."""
    db, me, f = get_db(), current_user(), request.form
    s = _shift_or_404(sid)
    if s["status"] != "open":
        abort(400, "This shift is closed.")
    uid = f.get("user_id", type=int) or me["id"]
    if not logic.can_run_shifts(me) and uid != me["id"]:
        abort(403)
    action = f.get("action")
    now = logic.fmt(logic.now_local())
    exists = one("SELECT * FROM shift_crew WHERE shift_id=? AND user_id=?", sid, uid)
    if action == "join" and not exists:
        db.execute("INSERT INTO shift_crew (shift_id, user_id, time_in) VALUES (?,?,?)", (sid, uid, now))
    elif action == "clock_out" and exists:
        db.execute("UPDATE shift_crew SET time_out=? WHERE shift_id=? AND user_id=?", (now, sid, uid))
    elif action == "remove" and exists and logic.can_run_shifts(me):
        if one("SELECT 1 FROM entries WHERE shift_id=? AND user_id=?", sid, uid):
            flash("That person has entries on this shift. Mark them called out instead.", "error")
        else:
            db.execute("DELETE FROM shift_crew WHERE shift_id=? AND user_id=?", (sid, uid))
    elif action == "save" and logic.can_run_shifts(me):
        tin = f.get("time_in") and logic.fmt(logic.on_shift_time(s, f["time_in"]))
        tout = f.get("time_out") and logic.fmt(logic.on_shift_time(s, f["time_out"]))
        if tin and tout and tout <= tin:
            tout = logic.fmt(logic.parse(tout) + timedelta(days=1))
        status = f.get("status") if f.get("status") in logic.CREW_STATUSES else "on_shift"
        if status == "called_out":
            tin = tout = None
        if exists:
            db.execute("UPDATE shift_crew SET time_in=?, time_out=?, status=?, note=? WHERE shift_id=? AND user_id=?",
                       (tin or None, tout or None, status, f.get("note") or None, sid, uid))
        else:
            db.execute("INSERT INTO shift_crew (shift_id, user_id, time_in, time_out, status, note) VALUES (?,?,?,?,?,?)",
                       (sid, uid, tin or None, tout or None, status, f.get("note") or None))
    db.commit()
    return redirect(url_for("shift_page", sid=sid) + "#crew")


@app.post("/shifts/<int:sid>/turnover")
@role_required("admin", "supervisor")
def shift_turnover(sid):
    db = get_db()
    _shift_or_404(sid)
    db.execute("UPDATE shifts SET turnover_ack_by=?, turnover_ack_at=?, turnover_note=? WHERE id=?",
               (current_user()["id"], logic.fmt(logic.now_local()), request.form.get("note") or None, sid))
    db.commit()
    flash("Turnover received.", "ok")
    return redirect(url_for("shift_page", sid=sid))


@app.post("/shifts/<int:sid>/close")
@role_required("admin", "supervisor")
def shift_close(sid):
    db, me, f = get_db(), current_user(), request.form
    s = _shift_or_404(sid)
    if s["status"] != "open":
        abort(400, "This shift is already closed.")
    missing = all_("SELECT * FROM shift_crew WHERE shift_id=? AND time_in IS NOT NULL AND time_out IS NULL", sid)
    if missing:
        if f.get("fill_out") != "1":
            flash("Some of the crew have no time out. Fill them in, or tick the box to use the shift end.", "error")
            return redirect(url_for("shift_page", sid=sid) + "#close")
        db.execute("UPDATE shift_crew SET time_out=? WHERE shift_id=? AND time_in IS NOT NULL AND time_out IS NULL",
                   (s["ends_at"], sid))
    db.execute("UPDATE shifts SET status='closed', closed_by=?, closed_at=?, notes=? WHERE id=?",
               (me["id"], logic.fmt(logic.now_local()), f.get("notes") or None, sid))
    n = nameplate.queue_shift(db, sid)  # same transaction as the close
    db.commit()
    flash(f"Shift closed and signed. {n} entr{'y' if n == 1 else 'ies'} going to Nameplate." if nameplate.enabled()
          else "Shift closed and signed.", "ok")
    return redirect(url_for("shift_page", sid=sid))


# ---------------------------------------------------------------- entries
def _read_entry_form(s, me, existing=None):
    """Check the form and return (values, problems). Times are on the shift's clock."""
    f = request.form
    v, problems = {}, []
    v["user_id"] = f.get("user_id", type=int) if logic.can_run_shifts(me) else (existing["user_id"] if existing else me["id"])
    if not v["user_id"]:
        v["user_id"] = me["id"]
    v["category"] = f.get("category")
    if v["category"] not in logic.CATEGORIES:
        problems.append("Pick what kind of work this was.")
    subj = f.get("subject") or ""
    v["np_asset_id"] = v["np_location_id"] = None
    v["where_text"] = (f.get("where_text") or "").strip() or None
    if subj.startswith("asset:"):
        eq = one("SELECT * FROM np_equipment WHERE np_id=?", subj[6:])
        if eq:
            v["np_asset_id"], v["subject_label"] = eq["np_id"], eq["label"]
    elif subj.startswith("place:"):
        pl = one("SELECT * FROM np_places WHERE np_id=?", subj[6:])
        if pl:
            v["np_location_id"], v["subject_label"] = pl["np_id"], pl["path"]
    if not (v["np_asset_id"] or v["np_location_id"]):
        v["subject_label"] = v["where_text"]

    start = f.get("start") and logic.on_shift_time(s, f["start"])
    end = f.get("end") and logic.on_shift_time(s, f["end"])
    dur = (f.get("dur_h", type=int) or 0) * 60 + (f.get("dur_m", type=int) or 0)
    if start and end:
        if end <= start:
            end += timedelta(days=1)
        v["minutes"] = logic.minutes_between(start, end)
    elif dur:
        v["minutes"] = dur
        if start:
            end = start + timedelta(minutes=dur)
    else:
        problems.append("Give a start and end time, or how long it took.")
        v["minutes"] = None
    if v["minutes"] is not None and v["minutes"] > 16 * 60:
        problems.append("That's more than 16 hours. Check the times.")
    v["started_at"], v["ended_at"] = logic.fmt(start or None), logic.fmt(end or None)

    v["notes"] = (f.get("notes") or "").strip()
    if not v["notes"]:
        problems.append("Say what was done.")
    for k in ("wo_ref", "materials", "reading_name", "reading_unit", "follow_up_note"):
        v[k] = (f.get(k) or "").strip() or None
    v["reading_value"] = None
    v["result"] = f.get("result") if f.get("result") in logic.RESULTS else None
    if v["category"] == "reading":
        try:
            v["reading_value"] = float(f.get("reading_value"))
        except (TypeError, ValueError):
            problems.append("A reading needs a number.")
        if not v["reading_name"]:
            problems.append("Say what was read (for example, steam pressure).")
        if not v["np_asset_id"]:
            problems.append("Pick the equipment the reading was taken on.")
    if v["category"] == "inspection" and not v["result"]:
        problems.append("Pick the inspection or test result.")
    v["follow_up"] = 1 if f.get("follow_up") == "1" else 0
    if v["follow_up"] and not v["follow_up_note"]:
        problems.append("Say what the next shift needs to do.")
    v["resolves_entry_id"] = f.get("resolves_entry_id", type=int) or None
    return v, problems


ENTRY_FIELDS = ("user_id", "category", "np_asset_id", "np_location_id", "where_text", "subject_label",
                "started_at", "ended_at", "minutes", "wo_ref", "notes", "materials", "reading_name",
                "reading_value", "reading_unit", "result", "follow_up", "follow_up_note", "resolves_entry_id")


def _insert_entry(db, sid, v, me, corrects=None):
    cols = ENTRY_FIELDS + ("shift_id", "created_by", "created", "corrects_entry_id")
    vals = [v[k] for k in ENTRY_FIELDS] + [sid, me["id"], logic.fmt(logic.now_local()), corrects]
    cur = db.execute(f"INSERT INTO entries ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    if v["resolves_entry_id"]:
        db.execute("""UPDATE entries SET follow_up_closed_at=?, follow_up_closed_by=?
                      WHERE id=? AND follow_up=1 AND follow_up_closed_at IS NULL""",
                   (logic.fmt(logic.now_local()), me["id"], v["resolves_entry_id"]))
    return cur.lastrowid


def _entry_form(s, entry=None, mode="new", values=None):
    equipment, places = _pickers(s["building_id"])
    people = all_("""SELECT u.* FROM users u JOIN shift_crew c ON c.user_id=u.id WHERE c.shift_id=?
                     ORDER BY u.name""", s["id"])
    follow = reports.open_follow_ups(get_db(), s["building_id"])
    return render_template("entry_form.html", s=s, e=values or entry, mode=mode, equipment=equipment,
                           places=places, people=people, follow=follow)


@app.route("/shifts/<int:sid>/entries/new", methods=["GET", "POST"])
@login_required
def entry_new(sid):
    db, me = get_db(), current_user()
    s = _shift_or_404(sid)
    if not logic.can_add_entry(me, s, _crew_ids(sid)):
        abort(403)
    if request.method == "POST":
        v, problems = _read_entry_form(s, me)
        if v["user_id"] not in _crew_ids(sid):
            problems.append("That person isn't on this shift. Add them to the crew first.")
        if problems:
            for p in problems:
                flash(p, "error")
            return _entry_form(s, values=_form_echo(v))
        _insert_entry(db, sid, v, me)
        db.commit()
        if v["category"] in logic.MAINTENANCE and not (v["np_asset_id"] or v["np_location_id"]):
            flash("Saved. No equipment or place was picked, so this one stays out of the building's record.", "ok")
        else:
            flash("Entry saved.", "ok")
        return redirect(url_for("shift_page", sid=sid) + "#entries")
    prefill = {}
    if request.args.get("resolves"):
        fu = one("SELECT * FROM entries WHERE id=?", request.args.get("resolves", type=int))
        if fu:
            prefill = {"resolves_entry_id": fu["id"], "np_asset_id": fu["np_asset_id"],
                       "np_location_id": fu["np_location_id"], "where_text": fu["where_text"],
                       "category": "corrective"}
    return _entry_form(s, values=prefill or None)


def _form_echo(v):
    """Put what was typed back in the form after a problem."""
    d = dict(v)
    d["start_clock"] = request.form.get("start")
    d["end_clock"] = request.form.get("end")
    return d


@app.route("/entries/<int:eid>/edit", methods=["GET", "POST"])
@login_required
def entry_edit(eid):
    db, me = get_db(), current_user()
    e = _entry_or_404(eid)
    s = _shift_or_404(e["shift_id"])
    if not logic.can_edit_entry(me, s, e):
        abort(403)
    if request.method == "POST":
        if request.form.get("delete") == "1":
            db.execute("UPDATE entries SET follow_up_closed_at=NULL, follow_up_closed_by=NULL WHERE id=?",
                       (e["resolves_entry_id"],))
            db.execute("UPDATE entries SET resolves_entry_id=NULL WHERE resolves_entry_id=?", (eid,))
            db.execute("DELETE FROM entries WHERE id=?", (eid,))
            db.commit()
            flash("Entry deleted.", "ok")
            return redirect(url_for("shift_page", sid=s["id"]) + "#entries")
        v, problems = _read_entry_form(s, me, existing=e)
        if problems:
            for p in problems:
                flash(p, "error")
            return _entry_form(s, mode="edit", values={**_form_echo(v), "id": eid})
        db.execute(f"UPDATE entries SET {', '.join(k + '=?' for k in ENTRY_FIELDS)} WHERE id=?",
                   [v[k] for k in ENTRY_FIELDS] + [eid])
        db.commit()
        flash("Entry updated.", "ok")
        return redirect(url_for("shift_page", sid=s["id"]) + "#entries")
    return _entry_form(s, entry=e, mode="edit")


@app.route("/entries/<int:eid>/correct", methods=["GET", "POST"])
@role_required("admin", "supervisor")
def entry_correct(eid):
    """After sign-off nothing is edited. A correction replaces the entry and the original stays on file."""
    db, me = get_db(), current_user()
    e = _entry_or_404(eid)
    s = _shift_or_404(e["shift_id"])
    if s["status"] != "closed" or e["superseded"]:
        abort(400, "Only signed-off entries are corrected. Edit open-shift entries directly.")
    if request.method == "POST":
        reason = (request.form.get("reason") or "").strip()
        if not reason:
            flash("Give a reason for the correction.", "error")
            return _entry_form(s, entry=e, mode="correct")
        sent_id = nameplate.mapped(db, "entry", eid)
        if request.form.get("withdraw") == "1":
            db.execute("UPDATE entries SET superseded=1 WHERE id=?", (eid,))
            if sent_id:
                nameplate.enqueue(db, "withdraw", eid, {"np_event_id": sent_id, "reason": f"Withdrawn: {reason}"})
            db.commit()
            flash("Entry withdrawn. The original stays on file.", "ok")
            return redirect(url_for("shift_page", sid=s["id"]) + "#entries")
        v, problems = _read_entry_form(s, me, existing=e)
        if problems:
            for p in problems:
                flash(p, "error")
            return _entry_form(s, mode="correct", values={**_form_echo(v), "id": eid})
        v["notes"] = f"{v['notes']} (Correction: {reason})"
        new_id = _insert_entry(db, s["id"], v, me, corrects=eid)
        db.execute("UPDATE entries SET superseded=1 WHERE id=?", (eid,))
        if sent_id:
            nameplate.enqueue(db, "withdraw", eid, {"np_event_id": sent_id, "reason": f"Corrected: {reason}"})
        new = one("SELECT * FROM entries WHERE id=?", new_id)
        if nameplate.shareable(new):
            nameplate.enqueue(db, "entry", new_id)
        db.commit()
        flash("Correction saved. The original stays on file, marked as replaced.", "ok")
        return redirect(url_for("shift_page", sid=s["id"]) + "#entries")
    return _entry_form(s, entry=e, mode="correct")


@app.post("/entries/<int:eid>/close-follow-up")
@role_required("admin", "supervisor")
def follow_up_close(eid):
    db = get_db()
    e = _entry_or_404(eid)
    db.execute("""UPDATE entries SET follow_up_closed_at=?, follow_up_closed_by=?,
                  follow_up_note=COALESCE(follow_up_note,'') || ? WHERE id=? AND follow_up_closed_at IS NULL""",
               (logic.fmt(logic.now_local()), current_user()["id"],
                f" — closed: {request.form.get('note')}" if request.form.get("note") else "", eid))
    db.commit()
    flash("Follow-up closed.", "ok")
    return redirect(request.form.get("back") or url_for("follow_ups"))


@app.route("/follow-ups")
@login_required
def follow_ups():
    rows = all_("""SELECT e.*, u.name AS person, s.starts_at AS shift_start, s.name AS shift_name, b.name AS building
                   FROM entries e JOIN shifts s ON s.id=e.shift_id JOIN users u ON u.id=e.user_id
                   JOIN buildings b ON b.id=s.building_id
                   WHERE e.follow_up=1 AND e.follow_up_closed_at IS NULL AND e.superseded=0
                   ORDER BY s.starts_at""")
    open_shift = {r["building_id"]: r["id"] for r in all_("SELECT id, building_id FROM shifts WHERE status='open'")}
    return render_template("follow_ups.html", rows=rows, open_shift=open_shift,
                           building_of={r["id"]: r["building_id"] for r in all_("SELECT id, building_id FROM shifts")})


@app.route("/shifts")
@login_required
def shift_list():
    b = request.args.get("building", type=int)
    q = """SELECT s.*, b.name AS building, u.name AS supervisor,
                  (SELECT COUNT(*) FROM entries e WHERE e.shift_id=s.id AND e.superseded=0) AS entry_n,
                  (SELECT COALESCE(SUM(minutes),0) FROM entries e WHERE e.shift_id=s.id AND e.superseded=0) AS mins
           FROM shifts s JOIN buildings b ON b.id=s.building_id LEFT JOIN users u ON u.id=s.supervisor_id"""
    args = []
    if b:
        q += " WHERE s.building_id=?"
        args.append(b)
    rows = all_(q + " ORDER BY s.starts_at DESC LIMIT 200", *args)
    return render_template("shifts.html", rows=rows, buildings=all_("SELECT * FROM buildings ORDER BY name"), b=b)


@app.route("/shifts/<int:sid>/report")
@login_required
def shift_report(sid):
    rep = reports.shift_report(get_db(), sid)
    if not rep:
        abort(404)
    return render_template("shift_report.html", r=rep, s=rep["shift"])
