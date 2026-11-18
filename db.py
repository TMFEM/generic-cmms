"""Database: one SQLite file in DATA_DIR.

What lives here is the maintenance group's own record: its people, shifts, hours,
training and non-maintenance work. Work on equipment and the building is *also*
sent to Nameplate when a supervisor closes a shift (see nameplate.py).
"""
import os
import secrets
import sqlite3
from contextlib import closing

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "cmms.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT UNIQUE,                 -- blank for crew who don't sign in (helpers, porters)
  pw_hash TEXT,
  role TEXT NOT NULL CHECK (role IN ('admin','supervisor','technician','manager')),
  trade TEXT,                        -- e.g. "Building engineer", "Porter", "Electrician"
  credential_type TEXT,              -- shown on work sent to Nameplate, e.g. "FDNY COF S-12"
  credential_number TEXT,
  credential_expires TEXT,
  np_technician_id TEXT,             -- the person's own Nameplate technician id, if they have one
  active INTEGER NOT NULL DEFAULT 1,
  created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS buildings (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  bin TEXT,
  np_location_id TEXT,               -- the building in Nameplate
  refreshed TEXT                     -- when its locations and equipment were last read from Nameplate
);
CREATE TABLE IF NOT EXISTS shift_types (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,                -- "Day", "Evening", "Night"
  start_time TEXT NOT NULL,          -- "07:00"
  end_time TEXT NOT NULL,            -- "15:00"; earlier than start means the shift ends the next day
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY,
  building_id INTEGER NOT NULL REFERENCES buildings(id),
  shift_type_id INTEGER REFERENCES shift_types(id),
  name TEXT NOT NULL,
  starts_at TEXT NOT NULL,           -- local time, "2026-11-15T07:00"
  ends_at TEXT NOT NULL,
  supervisor_id INTEGER REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  notes TEXT,                        -- supervisor's summary
  turnover_ack_by INTEGER REFERENCES users(id),   -- incoming supervisor received the open items
  turnover_ack_at TEXT,
  turnover_note TEXT,
  closed_by INTEGER REFERENCES users(id),
  closed_at TEXT,
  created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shifts_building ON shifts(building_id, starts_at);
CREATE TABLE IF NOT EXISTS shift_crew (
  shift_id INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  time_in TEXT,
  time_out TEXT,
  status TEXT NOT NULL DEFAULT 'on_shift'
    CHECK (status IN ('on_shift','late','called_out','holdover','overtime','left_early')),
  note TEXT,
  PRIMARY KEY (shift_id, user_id)
);
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY,
  shift_id INTEGER NOT NULL REFERENCES shifts(id),
  user_id INTEGER NOT NULL REFERENCES users(id),      -- who did the work
  category TEXT NOT NULL,
  np_asset_id TEXT,                  -- equipment in Nameplate, or
  np_location_id TEXT,               -- a place in the building, or
  where_text TEXT,                   -- free text when it's neither
  subject_label TEXT,                -- what the report shows, e.g. "ELEV-1 Otis Gen2 elevator"
  started_at TEXT,                   -- either a start and end time ...
  ended_at TEXT,
  minutes INTEGER,                   -- ... or just a duration (always filled; computed from times when given)
  wo_ref TEXT,
  notes TEXT NOT NULL,
  materials TEXT,
  reading_name TEXT, reading_value REAL, reading_unit TEXT,
  result TEXT,                       -- inspection or test: pass, fail, conditional
  follow_up INTEGER NOT NULL DEFAULT 0,
  follow_up_note TEXT,
  resolves_entry_id INTEGER REFERENCES entries(id),   -- this entry closes an earlier follow-up
  follow_up_closed_at TEXT,
  follow_up_closed_by INTEGER REFERENCES users(id),
  corrects_entry_id INTEGER REFERENCES entries(id),   -- replacement for a signed-off entry
  superseded INTEGER NOT NULL DEFAULT 0,              -- replaced or withdrawn after sign-off
  created_by INTEGER NOT NULL REFERENCES users(id),
  created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_shift ON entries(shift_id);
CREATE INDEX IF NOT EXISTS idx_entries_follow ON entries(follow_up, follow_up_closed_at);

-- Cached copy of the building's places and equipment from Nameplate, for the pickers.
CREATE TABLE IF NOT EXISTS np_places (
  np_id TEXT PRIMARY KEY, building_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT, path TEXT
);
CREATE TABLE IF NOT EXISTS np_equipment (
  np_id TEXT PRIMARY KEY, building_id INTEGER NOT NULL, tag TEXT, label TEXT NOT NULL,
  np_location_id TEXT, place TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

DEFAULT_SHIFT_TYPES = [("Day", "07:00", "15:00", 1), ("Evening", "15:00", "23:00", 2),
                       ("Night", "23:00", "07:00", 3)]


def connect():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def install_id(db):
    return db.execute("SELECT value FROM settings WHERE key='install_id'").fetchone()[0]


def init_db():
    import nameplate  # its outbox tables live in the same file
    os.makedirs(DATA_DIR, exist_ok=True)
    with closing(connect()) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.executescript(nameplate.SCHEMA)
        # A short id for this installation, so our references in Nameplate ("CMMS-3fa9c1-ENTRY-12")
        # never collide with another copy of this app writing to the same building.
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('install_id', ?)", (secrets.token_hex(3),))
        if not db.execute("SELECT 1 FROM shift_types").fetchone():
            db.executemany("INSERT INTO shift_types (name, start_time, end_time, sort) VALUES (?,?,?,?)",
                           DEFAULT_SHIFT_TYPES)
        db.commit()
