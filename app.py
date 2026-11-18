"""Generic CMMS: shift reports for building maintenance crews, on top of Nameplate.

Run locally:   DATA_DIR=./data python3 app.py
In Docker:     gunicorn app:app (see Dockerfile)
"""
import os

import db
import nameplate
import views_admin  # noqa: F401  (registers pages)
import views_shifts  # noqa: F401
from core import app

db.init_db()
nameplate.start_worker()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8120")), debug=False)
