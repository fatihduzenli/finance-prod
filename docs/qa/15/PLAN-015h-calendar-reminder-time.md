# PLAN-015h — the calendar reminder's e-mail at a chosen time (owner, 2026-11-11)

Plan by Fable. Approved by the owner on 2026-11-11 ("confirmed" with the pasted brief). Gate class: **full** (a schema migration; the mail path and the worker change), named here as the owner asked. One PR; the Publish Request after the QA entry (no standing approval for this one).

## 1. Objective

The New calendar entry dialog's switch "E-mail me that morning" with its fixed "09:00 New York" becomes "E-mail me a reminder" with a time the user picks: half hours from 6:00 AM to 9:00 PM New York, 9:00 AM by default, shown only while the switch is on. The time is stored on the entry (existing entries read 9:00 AM), appears on the edit page the same way, and is shown on the entry's page and in the calendar's day panel ("Reminder e-mail at 2:30 PM"). The e-mail goes out at that time: the worker checks every 15 minutes for the day's reminders whose time has come and queues each one once (the existing `calendar_email` job under the existing per-entry-and-day replay key), so a worker that was down sends late the same day and never twice; the daily tick stops queueing entries (it keeps the promises to pay). A reminder set for today at a time already past is refused under the select. Outbox mode, the sandbox allowlist and the settings guard are untouched: the job still sends through `mail.get_sender()`.

## 2. Packages

`backend/` only: `ledger/models.py`, one ledger migration `0079_calendar_entry_email_at.py`, `ledger/services/calendar.py`, `ledger/forms.py`, `ledger/views/calendar.py`, `ledger/templates/ledger/calendar/{_entry_dialog,form,detail,page}.html` and a new `_reminder_row.html`, `ledger/static/ledger/calendar_dialog.js` and a new `ledger/static/ledger/reminder_time.js`, `ledger/management/commands/collections_tick.py`, `payments/management/commands/worker.py`, `finance/dates.py` (one formatter), `finance/static/finance/app.css` (the row only), tests, the four `calendar-entry` visual baselines. **Out of scope:** the ICS feed (entries stay all-day events, no alarm), the promises' morning e-mail (unchanged, still the daily tick), the e-mail's text, the notes' switch (both kinds keep the switch as today), the daily job's cron, the Django admin, any other page or baseline.

## 3. Interface contract

### 3.1 Model and migration
- `CalendarEntry.email_at = models.TimeField(default=time(9, 0))`: the reminder's New York wall-clock time, meaningful when `email_morning` is on (kept as the switch's column; no rename). `ledger/migrations/0079_calendar_entry_email_at.py`: `AddField` with that default (every existing row reads 09:00) and `CheckConstraint` `calendar_entries_email_at_offered`: `email_at` in `REMINDER_TIMES`. No data step.
- `ledger/services/calendar.REMINDER_TIMES: tuple[time, ...]` — the 31 half hours 06:00 … 21:00 inclusive; `REMINDER_TIME_CHOICES: tuple[tuple[str, str], ...]` of `("HH:MM", clock_text(t))`; `DEFAULT_REMINDER_TIME = time(9, 0)`.
- `finance/dates.clock_text(value: time) -> str`: "6:00 AM", "9:00 AM", "12:00 PM", "2:30 PM", "9:00 PM" (no leading zero, upper-case AM/PM, one space).
- `EntryInput.email_at: time = DEFAULT_REMINDER_TIME`; `_write` stores it; `_state` adds `"email_at": "HH:MM"` (audited before/after as the other fields).

### 3.2 Forms
- `_CalendarEntryFields.email_morning`: label **"E-mail me a reminder"**, no help text (both forms). New `email_at = _QuietChoiceField(choices=REMINDER_TIME_CHOICES, initial="09:00", required=False, label="Time", error_messages={"invalid_choice": "Pick a time from the list."})`; `clean_email_at` returns the `time` (the default when empty). A value off the list is refused with that message (never a 500).
- The dialog form (`CalendarEntryDialogForm.__init__`) no longer blanks help text (none left); the switch keeps `class="switch" role="switch"`.
- The past-time rule lives in the service (both views reach it): `save_entry` refuses with `TIME_PASSED = "That time has passed today — pick a later time."` on field `email_at` when `data.email_morning` is on, `data.on == timezone.localdate()` and `data.email_at <= timezone.localtime().time()` — for a new entry always; for an existing entry only when the reminder changed (`email_morning`, `email_at` or `on` differ from the stored row), so a title edit of a reminder already sent today still saves. The views add the error to `email_at` as they do today (`outcome.field in form.fields`).

### 3.3 Templates and script
- New `ledger/templates/ledger/calendar/_reminder_row.html`, used by the dialog and the edit page, with `form` and (for ids) the same `auto_id` the page uses: `div.field.switch-field.entry-morning[.has-error]` > `label.inline` (the switch, then its label text) + `span.entry-time#…-time-wrap` (attribute `hidden` when the switch is off: the bound value, else the initial) > the `email_at` select (`id` from the field, the default pill select, no new class) + `span.muted` **"New York time"** + the `email_at` errors directly under the select (inside the wrap, so the message sits under it); the `email_morning` errors after. The old `#entry-morning-time` span goes.
- The edit page (`form.html`) renders the shared row where the switch stood: the loop over `visible_fields` skips `email_morning` and `email_at` and includes the row after `body`; the three selects follow as today.
- `ledger/static/ledger/reminder_time.js` (loaded by both pages): on the switch's `change`, toggle `hidden` on the wrap; run once on load. `calendar_dialog.js` unchanged except nothing it does conflicts (the dialog's reset on open leaves the switch and the select to the server's state).
- `app.css`: `.entry-time { display: inline-flex; align-items: center; gap: var(--space-2); }` and on the phone the row wraps under the switch (the row is already `flex-wrap`). Nothing else.
- Detail page (`detail.html`): the Details row becomes `<dt>Reminder e-mail</dt><dd>` "At 2:30 PM New York time, to the author" / "No"; `#detail-line` gains ` · Reminder e-mail at 2:30 PM` when the switch is on.
- Day panel (`page.html` `.day-entry-meta`): for a stored entry with the switch on, "Reminder · e-mail at 2:30 PM" (the kind label, a middle dot, the words); `CalendarItem` gains `email_at: time | None` (None for generated items and entries without the switch) set by `entry_item`.

### 3.4 Sending
- `calendar.queue_due_reminders(now: datetime) -> int`: `local = now.astimezone(NEW_YORK)`; every `CalendarEntry` with `on == local.date()`, `email_morning`, an author, and `email_at <= local.time()`, ordered by `email_at, created_at, id`, through the existing `_queue_morning(entry, local.date())` (the same replay key: entity, pk, day — so a job queued by anyone else for that day is never doubled); returns how many it queued. Nothing for other days: a reminder missed yesterday stays unsent.
- `queue_morning_emails(today)` queues **promises only**; `MorningReport` loses `entries`; the tick's line loses "Morning e-mails queued: n." and keeps "Promise reminders queued: n."; the command's docstring and `help` say the calendar reminders are the worker's.
- `send_morning_email` is unchanged (it still checks the entry exists, still asks for the e-mail, and is on that day).
- `payments/management/commands/worker.py`: `REMINDER_CHECK_SECONDS = 900`; `check_reminders(now: datetime) -> bool` runs `calendar.queue_due_reminders(now)` on the first call of the process and whenever 900 s have passed since the last run (state module-level, with `reset_reminder_check()` for tests), logs `"Reminder check: queued=%d"` only when > 0, and returns whether it ran; `tick()` calls it first (before `claim_next`), inside the same try so a failure is logged as the tick's and the loop goes on. `--once` therefore runs one check.
- DST: both sides compare New York wall-clock values; the offered hours (06:00–21:00) exclude the repeated and the missing hour, so a reminder on a change-over day sends at its wall-clock time (14:00 UTC for 9:00 AM on 2026-11-01; 13:00 UTC on 2026-03-08).

### 3.5 Unchanged
Outbox mode, the allowlist, the settings guard, the e-mail's subject and text, the promises' path, the ICS feed, the daily job's schedule, the calendar's filters and feeds.

## 4. Work breakdown (one Opus session; one commit per task)
1. Model, constants, formatter, migration 0079 (`makemigrations --check` clean on both vendors; `tz_audit` untouched). Done: tests of §5 (a).
2. Service: `EntryInput.email_at`, `_write`, `_state`, `TIME_PASSED` rule, `CalendarItem.email_at`, `queue_due_reminders`, promises-only `queue_morning_emails`, the tick's line. Done: §5 (b)–(d).
3. Forms and views (`_input` passes `email_at`; the edit page's initial). Done: §5 (e).
4. Templates, script, CSS; detail and day panel. Done: §5 (f); the dialog's four baselines regenerated in the Playwright image against PostgreSQL (and sqlite byte-identical), nothing else regenerated.
5. Worker check. Done: §5 (g).
6. Re-pin the existing tests that name the old words (`test_calendar_dialog.py` lines 4/117/120, `tests/e2e/test_calendar_dialog.py:130`, `test_collections.py` "Morning e-mails queued"), each named in the commit; no test weakened.

## 5. Test plan (Fable verifies; Opus writes (a)–(g), Fable adds the adversarial ones)
(a) The field: default 09:00 on an existing row after 0079; the constraint refuses 06:15 and 22:00; `clock_text` for 6:00 AM, 12:00 PM, 12:30 PM, 9:00 PM.
(b) Saved and shown: the dialog saves 14:30; the audit row's after carries "14:30"; the detail page shows "At 2:30 PM New York time, to the author" and the line "Reminder e-mail at 2:30 PM"; the day panel's meta; the edit page shows the switch on and 14:30 selected; off → wrap `hidden`, detail "No".
(c) Sent once at the time: entry today 14:30, author with an address; `check_reminders` at 14:15 NY queues nothing; at 14:30 queues one job, `run_queued_jobs` sends one; a second check at 14:45 and the daily tick at any time queue nothing more (one `calendar_email` job in all).
(d) Late after downtime: the first check of a process at 17:50 queues the 14:30 reminder (sent, late); a reminder of yesterday is never queued. DST: 9:00 AM on 2026-11-01 queues when `now` is 14:00 UTC and not at 13:00 UTC; on 2026-03-08 at 13:00 UTC and not at 12:00 UTC.
(e) Past-time: a new entry for today at a time just past is refused with the words under the select and nothing saved; the same time tomorrow saves; an edit of only the title of today's already-passed reminder saves; an edit that moves the time to a past one is refused; a value off the list is refused with "Pick a time from the list."
(f) Markup: the dialog's row (label words, select with 31 options and 09:00 selected, "New York time", wrap hidden when off, shown with errors when the server refused); the old span gone; the edit page's row; the script toggles (e2e: switching on shows the select, off hides it, on the laptop and the phone).
(g) Worker: `check_reminders` runs on the first tick, not again within 900 s, again after; a failure inside it is logged and the tick goes on; `--once` runs it.
Fable's adversarial: a reminder whose author is inactive or has no address (no send, audited as today); the switch on for a note (sends as a reminder does — unchanged); 31 options exactly and none outside 06:00–21:00; an entry moved to another day after it was queued today (the job says not due); two workers' checks at once (the replay key holds, one job); the sandbox allowlist holds the reminder off the list.

## 6. Risks
- Double sending between the daily tick and the worker during the switch-over: the shared replay key makes either path a no-op after the other; tested (c).
- A reminder set between checks is late by up to 15 minutes: accepted by the owner.
- The `email_at` default on the migration (31 values, a CHECK): PostgreSQL rewrites nothing large (the table is small). The constraint names every value; a change to the list is a migration.
- The worker's check adds one query per 15 minutes; none on the request path.
- No money path; no secret; no new dependency; mail goes through the same sender and guard.

## 7. Acceptance criteria
1. The dialog and the edit page show "E-mail me a reminder" with the select (31 half hours, 9:00 AM selected by default, "New York time" after it) only while the switch is on; the old "09:00 New York" words are gone.
2. The time is stored, audited and shown on the entry's page and in the day panel in the words of §3.3.
3. The e-mail for a reminder goes out at the chosen New York time (within 15 minutes), once per entry and day, late the same day after downtime, never for a past day; the daily tick queues no entry.
4. Today's past time is refused with the exact words under the select; an unrelated edit still saves.
5. 0079 applies and reverses on both vendors; `makemigrations --check` is clean; the four `calendar-entry` baselines are the only ones that change.
6. Full gate green; Outbox, allowlist and guard tests unchanged and green.

## 8. Assumptions
- "Edit dialog" means the edit page (`ledger:calendar:edit`); it keeps its page form with the shared row.
- "Popover" means the calendar's day panel (the page has no popover).
- The switch stays available for notes as today; the owner's "Kind = Reminder" describes the dialog, not a new rule.
