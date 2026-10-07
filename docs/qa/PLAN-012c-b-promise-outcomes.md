# PLAN-012c-b — Promises: "Still open" as the default outcome, outcome buttons on the promise, one open promise per lease, "Add entry" everywhere

**Date:** 2026-10-07 · **Origin:** owner's finding 5 of Prompt 12 on staging, in writing:
*"(a) When the operator has an open promise, the outcome is mandatory on every new entry, so a
call that did not resolve it cannot be recorded. Add 'Still open (no change)' as the default
outcome; Kept, Rescheduled and Broken stay. (b) Closing a promise must be possible from the
promise itself: on every open promise row (store page, operator page, attention list) show
Kept / Reschedule / Broken buttons. Each opens a small form (date, note; Reschedule also asks the
new date and amount, pre-filled from the old promise) and on save creates the contact-log entry
and closes the promise exactly as the 'Previous promise' path does, so history stays complete.
The 'Previous promise' block in Add contact remains for entries made the other way. (c) An
operator may have at most one open promise per lease: saving a new Promise to pay while one is
open closes the old one as Rescheduled automatically and the form says so. Operator sena
currently has three open at once; the attention list must never show more than one promise per
lease. (d) Name the button the same on both pages: 'Add entry'."*
**Lead/QA:** Fable · **Implementer:** Opus · **Ships:** its own push after the gate.

## 1. Restated objective

Four changes to the promise flow of the contact log, nothing else: the outcome of an open
promise is no longer forced on every new entry (a "Still open" default leaves it untouched);
each open promise carries its own Kept, Reschedule and Broken actions that create the
contact-log entry through the existing outcome path; a new promise on a lease closes the one
already open on that lease as Rescheduled, by rule and by a one-time data migration for the
rows staging already holds, so the attention list shows at most one promise per lease; and the
button that opens the add form reads "Add entry" on the operator page, the operator's contact
log page and the store page alike.

## 2. Affected packages

`backend/ledger` (`services/contacts.py`, `services/attention.py`, `forms.py`, `views/contacts.py`,
`urls.py`, templates `operators/{_contacts_table,contact_form,contacts,detail}.html`,
`_attention.html`, one new small template for the outcome form, one data migration),
`accounts/registry.py` (the new routes), `backend/tests`. Out of scope: the promise matching
and the automatic close by a payment (PLAN-012 D4), the attention list's other items, the
calendar, mail.

## 3. Decisions

- **D1 — "Still open (no change)".** `OUTCOME_CHOICES` gains `("open", "Still open (no change)")`
  first, the form's default; `ContactInput.outcome` accepts `"open"` (and `""`) with `previous`
  set, meaning: record the entry, leave the promise as it is. `outcome` is no longer required
  when a previous promise exists; `kept`, `rescheduled`, `broken` behave as today. The audit
  row of such an entry names the untouched promise (`previous`, outcome `open`) so history says
  the call happened while the promise stood.
- **D2 — outcome actions on the promise.** Routes `ledger:contacts:outcome`
  (`contacts/<pk>/outcome/<kept|rescheduled|broken>/`, GET the small form, POST; billing).
  The form: `happened_on` (default today, New York), `body` (required note), and for
  `rescheduled` `promised_date` and `promised_amount` pre-filled from the old promise and
  required. POST calls `contacts.add_entry(operator, ContactInput(kind=..., previous=<pk>,
  outcome=<outcome>, lease=<the promise's lease>, ...), actor)` and nothing else: the entry's
  kind is `promise` for a reschedule; for kept and broken the form asks **how you learned it**:
  a `kind` select limited to `call`, `email` and `visit`, default `call` (amended 2026-10-07:
  `ContactKind` has no remark kind, and adding one would be a schema change; the log then says
  how the outcome was learned, which is what the kinds mean). The same
  refusals apply (an operator archived, a promise no longer open → the sentence and a redirect).
  Buttons: on every open promise row of `_contacts_table.html` (operator page, store page, the
  operator's log page) for billing and above, three small links **Kept**, **Reschedule**,
  **Broken**; on each broken-promise item of `_attention.html` the same three. Viewers see none.
  Success returns to the page the user came from (`next`, validated as a local path) with the
  entry's anchor.
- **D3 — one open promise per lease.** In `add_entry`, when the new entry is a promise, every
  other open promise of the operator on the same lease (a lease of `NULL` is its own bucket) is
  closed as `rescheduled` with `closed_by` the new entry, each audited (`contact_entry.outcome`
  as the previous-promise path audits, with `"auto": true`), and `AddOutcome.message` says
  "Promise recorded; the earlier promise of $X by YYYY-MM-DD is closed as Rescheduled." The add
  form shows, above the promise fields, "Saving a promise closes the open promise of $X by
  YYYY-MM-DD on this store as Rescheduled." when one exists (per lease; the operator form shows
  it for the lease preselected or, with no lease, for the operator bucket). When `previous`
  names that same promise with outcome `rescheduled`, nothing changes from today. **Data
  migration** `0033_one_open_promise_per_lease`: for every (operator, lease) bucket with more
  than one open, non-deleted promise, the most recently recorded (`created_at`, then id) stays
  open and the others become `rescheduled` with `closed_by` the one kept, each with an audit row
  (actor `None`, the same action, `"migration": "0033"`); reverse is a no-op (the rows keep
  their history). `broken_promises` returns at most one promise per bucket, the open one, and
  **as a defence** orders by `created_at` descending and keeps the first per bucket even if the
  rule were ever bypassed; the attention list therefore never shows two for one lease.
- **D4 — "Add entry".** The operator page, the operator's contact log page and the add form's
  title read **Add entry** (the element ids stay; the store page already says it). Nothing else
  renamed.

## 4. Interface contract

`OUTCOME_CHOICES` with `open` first; `ContactInput.outcome in {"", "open", "kept",
"rescheduled", "broken"}`; `contacts.add_entry` closes same-bucket open promises on a new
promise and reports it; `contacts.broken_promises` at most one per (operator, lease); route
`ledger:contacts:outcome` with `kind` in `kept|rescheduled|broken` (billing, POST; GET for
billing); migration `0033`. No provider or environment change. New registry rules for the route.

## 5. Work breakdown (one batch, Opus)

| Task | Files | Done when |
|---|---|---|
| D1 form and service | `forms.py`, `services/contacts.py`, `views/contacts.py`, `contact_form.html` | a call with "Still open" records and leaves the promise open; the three outcomes unchanged |
| D2 routes, form, buttons | `urls.py`, `accounts/registry.py`, `views/contacts.py`, `forms.py`, new `contacts/outcome.html`, `_contacts_table.html`, `_attention.html` | the three buttons on every open promise row and attention item; each form creates the entry through `add_entry` |
| D3 rule, migration, attention | `services/contacts.py`, `services/attention.py`, `migrations/0033_*.py`, `tests/test_migrations_roundtrip.py` | a new promise closes the bucket's open one as Rescheduled with the sentence; the migration leaves one open per bucket on a seeded database; `broken_promises` one per bucket |
| D4 labels | `operators/detail.html`, `operators/contacts.html`, `contact_form.html` | "Add entry" on all three |
| Tests | `tests/test_contact_log.py`, `tests/test_contacts.py`, `tests/test_attention*.py` as fits | §6 |

## 6. Test plan (before implementation)

Happy: a call recorded with "Still open" while a promise is open → entry saved, promise open,
audit names it; Kept / Reschedule / Broken from the operator page, the store page and the
attention list each create the entry and close the promise exactly as the previous-promise
path (same status, `closed_by`, audit action); Reschedule pre-fills date and amount and takes
new ones; a new promise on a lease with an open one → old `rescheduled`, `closed_by` the new,
the success sentence, the form's warning shown beforehand; a new promise on the other lease
leaves the first lease's promise open; the migration on a seeded database with three open
promises on one lease (sena's case) keeps the newest and closes two, with audit rows, and the
attention list shows one. Boundaries: two open promises in two buckets (lease A, no lease);
the previous-promise block still works with each of the four outcomes; a deleted promise is
never a candidate; the outcome form on a promise that was closed meanwhile → refused with the
sentence. Error paths: viewer 403 on the outcome routes and no buttons; billing of another
operator's promise id → 404/refusal; a reschedule without a new date → refused; a note with a
card number → refused. Adversarial (Fable): the outcome route cannot close a promise of another
operator by a forged `previous`; `next` cannot send the user off-site; the auto-close never
touches a promise of another lease or a deleted one; `broken_promises` never returns two for
one bucket even with two open rows forced in by raw update; the migration is idempotent (run
twice, same result) and writes nothing when every bucket has one.

## 7. Risk register

- Money state: none; promises are workflow rows.
- The data migration changes staging rows (sena's three promises); it keeps history (status
  and `closed_by`) and is audited; reverse is a no-op by design.
- Irreversible: the auto-closed statuses; acceptable, they are what the rule would have done.

## 8. Acceptance criteria

Gate green on both engines; on staging: sena shows one open promise and one attention item; a
call with "Still open" records; the three buttons close a promise from each page; a second
promise on a lease closes the first as Rescheduled with the sentence; "Add entry" on all three
pages.
