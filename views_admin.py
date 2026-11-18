"""People, buildings, shift times, the Nameplate page, and management reports."""
import csv
import io
from datetime import date, timedelta

from flask import Response, abort, flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

import logic
import nameplate
import reports
from core import all_, app, current_user, get_db, login_required, one, role_required


# ---------------------------------------------------------------- people
@app.route("/people")
@role_required("admin", "supervisor", "manager")
def people():
    rows = all_("SELECT * FROM users ORDER BY active DESC, role, name")
    return render_template("people.html", rows=rows)


@app.route("/people/new", methods=["GET", "POST"])
@app.route("/people/<int:uid>", methods=["GET", "POST"])
@role_required("admin")
def person_edit(uid=None):
    db = get_db()
    u = one("SELECT * FROM users WHERE id=?", uid) if uid else None
    if uid and not u:
        abort(404)
    if request.method == "POST":
        f = request.form
        name = (f.get("name") or "").strip()
        email = (f.get("email") or "").strip().lower() or None
        role = f.get("role")
        if not name or role not in ("admin", "supervisor", "technician", "manager"):
            flash("A name and a role are needed.", "error")
            return render_template("person.html", u=f)
        if email and one("SELECT 1 FROM users WHERE email=? AND id IS NOT ?", email, uid):
            flash("Someone already uses that email.", "error")
            return render_template("person.html", u=f)
        vals = (name, email, role, f.get("trade") or None, f.get("credential_type") or None,
                f.get("credential_number") or None, f.get("credential_expires") or None,
                (f.get("np_technician_id") or "").strip() or None, 1 if f.get("active", "1") == "1" else 0)
        if uid:
            db.execute("""UPDATE users SET name=?, email=?, role=?, trade=?, credential_type=?, credential_number=?,
                          credential_expires=?, np_technician_id=?, active=? WHERE id=?""", vals + (uid,))
        else:
            uid = db.execute("""INSERT INTO users (name, email, role, trade, credential_type, credential_number,
                                credential_expires, np_technician_id, active, created) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                             vals + (logic.fmt(logic.now_local()),)).lastrowid
        pw = f.get("password") or ""
        if pw:
            if len(pw) < 10:
                flash("Passwords need at least 10 characters. The rest was saved.", "error")
            else:
                db.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(pw), uid))
        if not email:
            db.execute("UPDATE users SET pw_hash=NULL WHERE id=?", (uid,))  # no email, no sign-in
        db.commit()
        flash("Saved.", "ok")
        return redirect(url_for("people"))
    return render_template("person.html", u=u)


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    me = current_user()
    if request.method == "POST":
        from werkzeug.security import check_password_hash
        f = request.form
        if not check_password_hash(me["pw_hash"], f.get("current") or ""):
            flash("Your current password doesn't match.", "error")
        elif len(f.get("new") or "") < 10:
            flash("Use at least 10 characters.", "error")
        else:
            get_db().execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(f["new"]), me["id"]))
            get_db().commit()
            flash("Password changed.", "ok")
            return redirect(url_for("home"))
    return render_template("account.html")


# ---------------------------------------------------------------- buildings
@app.route("/buildings", methods=["GET", "POST"])
@role_required("admin")
def buildings():
    db = get_db()
    found = []
    if request.method == "POST":
        f = request.form
        try:
            if f.get("action") == "add":
                loc_id, name, bin_number = f.get("np_location_id"), (f.get("name") or "").strip(), f.get("bin") or None
                if not loc_id and bin_number:
                    hit = nameplate.find_building(bin_number)
                    if not hit:
                        flash(f"No building with BIN {bin_number} in Nameplate, or no access to it.", "error")
                        return redirect(url_for("buildings"))
                    loc_id, name = hit["id"], name or hit["name"]
                if not loc_id or one("SELECT 1 FROM buildings WHERE np_location_id=?", loc_id):
                    flash("That building is already here, or none was picked.", "error")
                    return redirect(url_for("buildings"))
                bid = db.execute("INSERT INTO buildings (name, bin, np_location_id) VALUES (?,?,?)",
                                 (name or "Building", bin_number, loc_id)).lastrowid
                db.commit()
                n = nameplate.refresh_building(db, bid)
                flash(f"Added {name}. {n} pieces of equipment read from Nameplate.", "ok")
            elif f.get("action") == "refresh":
                n = nameplate.refresh_building(db, f.get("building_id", type=int))
                flash(f"Refreshed: {n} pieces of equipment.", "ok")
        except (nameplate.NameplateDown, nameplate.NameplateRefused) as exc:
            flash(f"Nameplate: {exc}", "error")
        return redirect(url_for("buildings"))
    rows = all_("""SELECT b.*, (SELECT COUNT(*) FROM np_equipment e WHERE e.building_id=b.id) AS eq_n,
                          (SELECT COUNT(*) FROM np_places p WHERE p.building_id=b.id) AS place_n
                   FROM buildings b ORDER BY name""")
    if nameplate.enabled():
        try:
            have = {r["np_location_id"] for r in rows}
            found = [l for l in nameplate.list_buildings() if l["id"] not in have]
        except (nameplate.NameplateDown, nameplate.NameplateRefused):
            found = []
    return render_template("buildings.html", rows=rows, found=found)


@app.route("/shift-times", methods=["GET", "POST"])
@role_required("admin")
def shift_times():
    db = get_db()
    if request.method == "POST":
        f = request.form
        for t in all_("SELECT * FROM shift_types"):
            name = (f.get(f"name_{t['id']}") or "").strip()
            st, en = f.get(f"start_{t['id']}"), f.get(f"end_{t['id']}")
            if name and st and en:
                db.execute("UPDATE shift_types SET name=?, start_time=?, end_time=? WHERE id=?", (name, st, en, t["id"]))
        if (f.get("new_name") or "").strip() and f.get("new_start") and f.get("new_end"):
            db.execute("INSERT INTO shift_types (name, start_time, end_time, sort) VALUES (?,?,?,?)",
                       (f["new_name"].strip(), f["new_start"], f["new_end"], 99))
        db.commit()
        flash("Shift times saved. Shifts already started keep their times.", "ok")
        return redirect(url_for("shift_times"))
    return render_template("shift_times.html", rows=all_("SELECT * FROM shift_types ORDER BY sort, id"))


# ---------------------------------------------------------------- Nameplate
@app.route("/nameplate", methods=["GET", "POST"])
@role_required("admin", "supervisor")
def nameplate_page():
    db = get_db()
    if request.method == "POST" and request.form.get("action") == "send":
        sent, failed, waiting = nameplate.process_outbox(db)
        flash(f"Sent {sent}, could not send {failed}, still waiting {waiting}.", "ok" if not failed else "error")
        return redirect(url_for("nameplate_page"))
    ok, msg = nameplate.ping()
    return render_template("nameplate.html", ok=ok, msg=msg, st=nameplate.status(db), url=nameplate.URL,
                           owner=nameplate.OWNER)


# ---------------------------------------------------------------- management reports
@app.route("/reports")
@role_required("admin", "supervisor", "manager")
def report_summary():
    blds = all_("SELECT * FROM buildings ORDER BY name")
    if not blds:
        return render_template("reports.html", blds=blds, r=None)
    bid = request.args.get("building", type=int) or blds[0]["id"]
    end = request.args.get("to") or date.today().isoformat()
    start = request.args.get("from") or (date.fromisoformat(end) - timedelta(days=6)).isoformat()
    r = reports.summary(get_db(), bid, start, end)
    return render_template("reports.html", blds=blds, r=r, bid=bid, start=start, end=end,
                           b=next(x for x in blds if x["id"] == bid))


@app.route("/reports/entries.csv")
@role_required("admin", "supervisor", "manager")
def report_csv():
    bid = request.args.get("building", type=int)
    start, end = request.args.get("from"), request.args.get("to")
    if not (bid and start and end):
        abort(400, "Pick a building and dates first.")
    r = reports.summary(get_db(), bid, start, end)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "shift", "person", "category", "equipment or place", "start", "end", "minutes", "hours",
                "work order", "notes", "materials", "reading", "result", "follow-up", "nameplate"])
    for e in r["entries"]:
        reading = f"{e['reading_name']} {e['reading_value']:g} {e['reading_unit'] or ''}".strip() \
            if e["reading_value"] is not None else ""
        w.writerow([e["shift_start"][:10], e["shift_name"], e["person"], logic.label(e["category"]),
                    e["subject_label"] or "", e["started_at"] or "", e["ended_at"] or "", e["minutes"],
                    logic.hours(e["minutes"]), e["wo_ref"] or "", e["notes"], e["materials"] or "", reading,
                    e["result"] or "", e["follow_up_note"] if e["follow_up"] else "", e["np_status"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=shift-entries-{start}-to-{end}.csv"})
