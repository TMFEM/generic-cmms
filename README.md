# Generic CMMS (v0.1): shift log

A shift log for building maintenance crews. It is the "generic CMMS" in the Nameplate demo: a third
app, with its own look, writing to the same shared building record as Facility Desk.

- **Shifts:** building, shift (day, evening, night), supervisor, crew with time in and out, late,
  called out, held over, overtime
- **Work and activities:** who, what kind, equipment or place, start and end *or* just a duration,
  work order, notes, materials, readings, inspection results
- **Hand-off:** follow-ups carry to the next shift until someone logs the fix; the incoming
  supervisor records that turnover was received
- **Sign-off:** the supervisor closes and signs the shift. After that, entries aren't edited; a
  correction replaces an entry and the original stays on file
- **Reports:** a printable shift report, and a management summary by date range (crew time,
  logged time by type, by person, preventive share, open follow-ups) with a CSV download
- **Roles:** technician (logs own work on their shift), supervisor (runs shifts, logs for anyone,
  signs off, corrects), manager (reads and reports), admin (people, buildings, shift times)

## What goes to Nameplate, and what stays here

| Goes to the building's record when the shift is signed | Stays with the maintenance group |
|---|---|
| Preventive, corrective, tenant request and emergency work | Crew list, hours, late, call-outs, overtime |
| Inspections and tests (pass, fail, conditional) | Training, safety meetings |
| Readings (steam pressure, run hours) | Vendor escorts, admin time, other |
| Building rounds and building events, as logbook entries | Supervisor notes, turnover notes |

Only entries that name equipment or a place are sent. Each one carries the person, their
credential, their Nameplate technician id if they've linked it, the hours, and the work order.

## Install on businesscore

Nameplate v0.2.1 has to be running first, with `ARP_DEMO_GENERIC_KEY` in its `.env` and the demo
reset once after adding it (see Nameplate's README, "Connect the Generic CMMS").

**1. Copy the zip over.** In PowerShell on your PC:

```powershell
scp $env:USERPROFILE\Documents\Downloads\generic-cmms-v0.1.zip player1@192.168.68.50:~/
```

**2. Unzip and make the settings file.** On businesscore:

```bash
cd ~ && unzip -o generic-cmms-v0.1.zip
cd ~/generic-cmms
cp .env.example .env
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env
KEY=$(grep '^ARP_DEMO_GENERIC_KEY' ~/asset-record-platform/.env | cut -d= -f2)
sed -i "s/^NAMEPLATE_KEY=.*/NAMEPLATE_KEY=$KEY/" .env
unset KEY
grep -c '=.\+' .env
```

The last line should print `6`: six settings with a value.

**3. Start it and build the demo.**

```bash
docker compose up -d --build
docker compose exec generic-cmms python seed_demo.py
```

The seed prints the sign-ins and how many signed entries went to Nameplate.

**4. Sign in.** Open `http://100.90.5.80:8120` (or `http://192.168.68.50:8120` at home).

| Sign-in | Role | What to show |
|---|---|---|
| `sam@demo.cmms` | Supervisor | Today's open shift, turnover, crew, logging for anyone, closing |
| `dana@demo.cmms` | Technician | Phone view: log work on her shift |
| `pat@demo.cmms` | Manager | Reports: hours by type and person, CSV |
| `admin@demo.cmms` | Admin | People, buildings from Nameplate, shift times |

Password for all: `nameplate-demo`.

**5. Commit it to GitHub** as a new repo, the same way as Facility Desk. `.env` stays out (it's
in `.gitignore`).

## A demo script

1. Sign in as **Sam**. Today's day shift is open. Two follow-ups were handed over from earlier
   shifts (the 7th floor detector, elevator 1's door). Click **Turnover received**.
2. **Log work** for Marcus: corrective, ELEV-1, 9:00 to 9:45, "Adjusted the door operator", tick
   *closes an earlier follow-up* and pick the elevator item.
3. Log training for Jamie with just a duration, 1 h. It shows **Stays here**.
4. Close and sign the shift (tick *use the shift end*). The flash says how many entries went to
   Nameplate.
5. Open the **Nameplate demo console** (port 8111) as the owner, then ELEV-1: Marcus's work is
   there, credited to the Generic CMMS, with his license and the hours. The training isn't.
6. Back in the CMMS, **Correct** a reading on yesterday's shift. The original stays on file,
   crossed out; Nameplate shows the withdrawal and the new reading.
7. Sign in as **Pat** and open **Reports**.

## After a Nameplate demo reset

The reset wipes the demo building, so rebuild the CMMS demo too:

```bash
cd ~/asset-record-platform && docker compose exec nameplate-demo python scripts/demo_reset.py
cd ~/facility-desk && docker compose exec facility-desk python sync_nameplate.py --fresh
cd ~/generic-cmms && docker compose exec generic-cmms python seed_demo.py
```

## Tests

```bash
python3 smoke_test.py                       # 68 checks, no Nameplate needed
NAMEPLATE_URL=http://localhost:8111 NAMEPLATE_KEY=... \
NAMEPLATE_OWNER_ID=00000000-0000-4000-8000-00000000d3e0 python3 smoke_nameplate.py   # 21 checks, demo only
```

## Files

| File | What it does |
|---|---|
| `db.py` | The database tables. Read this first. |
| `logic.py` | Work categories (and which go to Nameplate), shift time math, who can do what |
| `views_shifts.py` | Shifts, crew, entries, follow-ups, sign-off, corrections |
| `views_admin.py` | People, buildings, shift times, the Nameplate page, reports, CSV |
| `reports.py` | The numbers behind the shift report and the summary |
| `nameplate.py` | Reading the building's equipment; the outbox that sends signed work |
| `core.py` | The Flask app, sign-in, CSRF, template helpers |
| `seed_demo.py` | The demo crew and a week of shifts |

## Not in v0.1

- Work orders as their own records (an entry carries a work order number for now)
- Scheduling: who is on which shift next week
- Payroll export (crew hours are here; the format depends on the payroll system)
- Offline entry from a phone in a basement with no signal
- A real (non-demo) instance: set `DEMO=0`, add your own people and building, and use the real
  Nameplate address and a real client key
