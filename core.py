"""The Flask app, sign-in, and helpers shared by the pages."""
import os
import secrets
from contextlib import closing
from functools import wraps

from flask import Flask, abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash

import logic
from db import DATA_DIR, connect

APP_NAME = os.environ.get("APP_NAME", "Generic CMMS")
DEMO = os.environ.get("DEMO") == "1"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or None,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
)
if not app.config["SECRET_KEY"]:
    # No secret set: keep one in the data folder so sign-ins survive a restart.
    path = os.path.join(DATA_DIR, "secret_key")
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        app.config["SECRET_KEY"] = fh.read().strip()


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def _close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------- sign-in
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    if "user" not in g:
        g.user = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def role_required(*roles):
    def deco(view):
        @wraps(view)
        def wrapped(*a, **kw):
            u = current_user()
            if not u:
                return redirect(url_for("login", next=request.path))
            if u["role"] not in roles:
                abort(403)
            return view(*a, **kw)
        return login_required(wrapped)
    return deco


_attempts = {}


def check_login(email, password):
    """Returns the user, or None. Slows down repeated wrong passwords from one address."""
    key = (request.remote_addr, email)
    recent = [t for t in _attempts.get(key, []) if t > logic.now_local().timestamp() - 300]
    if len(recent) >= 8:
        return None
    row = get_db().execute("SELECT * FROM users WHERE email=? AND active=1 AND pw_hash IS NOT NULL",
                           (email,)).fetchone()
    if row and check_password_hash(row["pw_hash"], password):
        _attempts.pop(key, None)
        return row
    _attempts[key] = recent + [logic.now_local().timestamp()]
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    from flask import render_template
    if request.method == "POST":
        u = check_login((request.form.get("email") or "").strip().lower(), request.form.get("password") or "")
        if u:
            session.clear()
            session["uid"] = u["id"]
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("home"))
        flash("That email and password don't match.", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- forms
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def _csrf_check():
    if request.method == "POST" and request.endpoint not in ("login",):
        if not secrets.compare_digest(request.form.get("_csrf", ""), session.get("_csrf", "")):
            abort(400, "The form expired. Go back, reload the page and try again.")


app.jinja_env.globals["csrf_token"] = csrf_token  # globals also reach imported macros


@app.context_processor
def _globals():
    return {"me": current_user(), "APP_NAME": APP_NAME, "DEMO": DEMO,
            "label": logic.label, "short": lambda c: logic.SHORT.get(c, c), "CATEGORIES": logic.CATEGORIES, "CREW_STATUSES": logic.CREW_STATUSES,
            "RESULTS": logic.RESULTS, "hours": logic.hours, "can_run": logic.can_run_shifts}


@app.template_filter("t")
def _time(ts):
    """"2026-11-15T07:30" -> "7:30 AM"."""
    d = logic.parse(ts)
    return d.strftime("%-I:%M %p") if d else ""


@app.template_filter("day")
def _day(ts):
    d = logic.parse(ts)
    return d.strftime("%a %b %-d, %Y") if d else ""


@app.template_filter("hm")
def _hm(minutes):
    if minutes is None:
        return ""
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


@app.errorhandler(403)
def _forbidden(e):
    from flask import render_template
    return render_template("error.html", message="Your role can't do that."), 403


@app.errorhandler(404)
def _missing(e):
    from flask import render_template
    return render_template("error.html", message="That page or record doesn't exist."), 404


@app.errorhandler(400)
def _bad(e):
    from flask import render_template
    return render_template("error.html", message=getattr(e, "description", "Bad request")), 400


def one(sql, *args):
    return get_db().execute(sql, args).fetchone()


def all_(sql, *args):
    return get_db().execute(sql, args).fetchall()


def with_db(fn):
    """For scripts: run fn(db) with a connection that is always closed."""
    with closing(connect()) as db:
        return fn(db)
