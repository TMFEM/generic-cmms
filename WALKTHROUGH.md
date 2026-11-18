# Reading the Generic CMMS code

The app is small: about 1,500 lines of Python (plus two test files) and 470 lines of templates. It uses the same tools as
Facility Desk (Flask, SQLite, Jinja), so most patterns will look familiar. Read in this order.

## 1. `db.py`: what the app remembers

Start with the tables, because everything else is moving data in and out of them.

- `users` is everyone, including crew who never sign in (`email` blank). `np_technician_id` links
  a person to *their own* record in Nameplate.
- `shifts` + `shift_crew`: one row per shift, one row per person on it. Times are stored as local
  text like `2026-11-15T07:00`, which sorts correctly as text.
- `entries` is the heart of it. Notice the three ways to say *where*: `np_asset_id`,
  `np_location_id`, or `where_text`. Only the first two can go to Nameplate.
- `superseded` and `corrects_entry_id` are how corrections work without deleting anything.
- `np_places` and `np_equipment` are a **cache**: a copy of Nameplate's list so the form works fast
  and still works if Nameplate is down.

**Question to answer for yourself:** why is `minutes` always filled, even when there's a start and
an end? (Hint: look at what reports.py adds up.)

## 2. `logic.py`: the rules

- `CATEGORIES` is the most important dictionary in the app. Each kind of work says what it becomes
  in Nameplate, or `None` for "stays here." Change this and you change what the building's record
  receives.
- `shift_window` and `on_shift_time` handle the night shift. A 23:00 to 07:00 shift means "02:00"
  is tomorrow. Read these slowly; time math is where bugs hide.
- The `can_...` functions are every permission rule in one place. The pages call them instead of
  repeating the rules.

## 3. `views_shifts.py`: the pages people use

Follow one entry from the form to the database:

1. `entry_new` checks permission with `logic.can_add_entry`.
2. `_read_entry_form` turns the form into clean values and a list of problems. It never saves.
3. If there are problems, the form is shown again with what was typed (`_form_echo`).
4. `_insert_entry` saves it, and if it closes a follow-up, marks that one closed in the same
   transaction.

Then read `shift_close`. One line matters most:

```python
n = nameplate.queue_shift(db, sid)  # same transaction as the close
```

The close and the queue are saved together or not at all. That's the **outbox pattern** again, the
same idea as in Facility Desk: the shift never waits on Nameplate, and nothing is lost if
Nameplate is down.

Finally read `entry_correct`. After sign-off, nothing is edited: the old entry is marked
`superseded`, a new one is added, and if the old one already reached Nameplate, a `withdraw` is
queued. Nameplate works the same way, so both records tell the same story.

## 4. `nameplate.py`: talking to the shared record

- `refresh_building` reads locations, positions and equipment, then works out which belong to
  *this* building by walking up the parent chain (`path()`).
- `event_body` translates one entry into a Nameplate entry. Compare it with `CATEGORIES`.
- `process_outbox` is nearly the same as Facility Desk's, with one addition you should notice:
  `_sender_lock()`. It makes sure only one sender runs at a time, even if the app and a command
  both try. That's the fix for the "two senders" worry from the first Facility Desk sync.
- `install_id` in the references (`CMMS-3fa9c1-ENTRY-12`): the end-to-end test caught that two
  copies of the app would both write `ENTRY-2`. A short random id per installation fixes it.

## 5. `reports.py`

Plain Python adding up minutes. No Flask in it, so you can test it with a database and nothing else.

## Try these yourself

1. **Add a category.** Add `"cleaning": ("Cleaning", None)` to `CATEGORIES`. Restart, and check
   that it appears under "Stays with the crew's records" in the form, and in the reports.
2. **Change a rule.** Make 12 hours the longest entry instead of 16 (`_read_entry_form`). Then
   run `python3 smoke_test.py` and see whether any check notices. If none does, add one.
3. **Show more on the home page.** On `home.html`, show each open shift's open follow-ups count.
   You'll need a small query in `home()`; copy the style of `entry_n`.
4. **Read a test.** In `smoke_test.py`, find the check named "after-midnight time lands on the
   next day" and explain to yourself why 01:30 becomes `2026-11-11T01:30`.
