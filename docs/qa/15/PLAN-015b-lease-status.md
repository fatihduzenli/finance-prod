# PLAN-015b — lease activation, Pause and Resume collection, the rent history cap

**Owner's decisions (2026-11-07), queued after PR #55 under the same approval; gate class full
(money paths, new routes, the daily job).** Plan by Fable; implementation by Opus; gate and sign-off by
Fable (root `CLAUDE.md` §1). The record is `docs/qa/15/QA-015.md`.

## 1. Objective

Three things the Edit lease redesign (PLAN-015a) left to the owner. (A) A prospective lease becomes
active on its start date by the daily job, and the LEASE card offers "Activate now" (billing role) for
an early start, audited. (B) "Pause collection" on the LEASE card with a required reason: while a
lease is paused, reminders and autopay debits stop and its badge reads Paused; "Resume collection"
restarts them. Both audited and shown in Notes & follow-ups. (C) The Edit lease page's rent history
shows the latest five changes with "Show all".

## 2. Scope

**In:** `backend/ledger/` — `services/leases.py` (the four actions and the latest pause's reading),
`services/reminders.py`, `services/messaging.py`, `services/autopay_plans.py`, `services/nsf.py` (the
stops while paused), `management/commands/collections_tick.py` (the activation step and its sentence),
`services/workspace.py` + `templates/ledger/stores/_header.html` (the LEASE card's status row and
actions), `views/leases.py` + `urls.py` + `forms.py` + two dialog templates, `accounts/registry.py`
(three routes), `services/timeline.py` + `views/contact_log.py` + `templates/ledger/stores/_timeline_list.html`
(the new timeline kind), `templates/ledger/leases/_edit_lease.html` + `views/leases.py _edit_context`
(the history cap), and the tests that pin all of it.

**Out:** no model or migration (the pause reason and the activation live in audit rows); the New lease
page (a future-start lease still saves Active, §8 Q1); the portal; the processor adapter; installment
plans' own pause; rent accrual rules beyond §3 (B).

## 3. Interface contract

**(A) Activation.**
- `ledger.services.leases.activate_due(today: date, *, actor=None) -> ActivationSummary`
  (`activated: int`, `waiting: int`): every PROSPECTIVE lease with `started_on <= today` that meets the
  activation requirements (an operator, a debit day, a known channel — the Active row's database
  checks) becomes ACTIVE under the row lock, with the audit row `lease.activated`
  (`before`/`after` = `_snapshot`, `after["trigger"] = "start_date"`), then `rent_run.catch_up_lease`
  so the same run charges it. A prospective lease past its start that fails a requirement is counted
  in `waiting` and left as it is (no flag, no row).
- `ledger.services.leases.activate(lease_id, *, actor, today) -> Lease` — "Activate now": refused
  (`LeaseRefused`) unless the lease is PROSPECTIVE and meets the requirements; when `started_on` is
  after `today` it becomes `today`, and the lease's only rent row's `effective_from` moves with it
  (a lease with more than one row keeps them; a row starting after today is then an upcoming change);
  ACTIVE, audit `lease.activated` with `after["trigger"] = "staff"`, then `catch_up_lease`.
- `collections_tick`: the step `leases.activate_due(today)` runs **before** `rent_run.create_charges`;
  the summary line gains "Leases activated: N (M waiting on details)." — the tests pinning the line's
  other sentences are re-pinned, not loosened.

**(B) Pause and Resume collection.**
- `ledger.services.leases.pause_collection(lease_id, *, reason: str, actor) -> PauseOutcome`
  (`lease`, `autopay_paused: bool`): refused unless ACTIVE; `reason` must have visible text after
  `visible_text` and pass `validate_no_payment_data`, else `LeaseRefused("reason", …)`; PAUSED under
  the row lock; audit `lease.collection_paused` with `after["reason"]` and
  `after["autopay_paused"]` (true when the lease has a live provider schedule in state active). The
  provider pause itself is a worker job: the **view** queues `JobKind.AUTOPAY_PAUSE` when
  `autopay_paused` is true (as `lease_pause` does today); the service never calls the processor.
- `ledger.services.leases.resume_collection(lease_id, *, actor, today) -> ResumeOutcome`
  (`lease`, `autopay_resume: bool`): refused unless PAUSED; ACTIVE under the row lock; audit
  `lease.collection_resumed`; `autopay_resume` is true when the latest `lease.collection_paused` row
  says `autopay_paused` and the schedules are still paused — the view then queues
  `JobKind.AUTOPAY_RESUME` (resume_autopay needs the lease ACTIVE, which the service has just done).
  Then `rent_run.catch_up_lease(lease, today)`: the months that fell inside the pause are charged on
  resume — collection paused, rent still owed (§8 Q2 asks the owner to confirm).
- `ledger.services.leases.collection_pause(lease) -> CollectionPause | None` — the latest
  `lease.collection_paused` row after the latest `lease.collection_resumed` (or with none): its `at`,
  `reason`, `actor` name; `None` for a lease that is not paused. One query. The LEASE card shows
  "Paused since <date> — <reason>".
- **Stops while PAUSED** (each with a test that the ACTIVE case is unchanged):
  `reminders.plan` (rules B–E on open charges) skips a PAUSED lease's charges;
  `messaging` suppresses a PAUSED lease at send time where it suppresses ENDED today (`:566`, `:1176`);
  `autopay_plans.run_due` / `make_due` make no occurrence for a PAUSED lease's plan (an installment
  plan's own state is untouched); `nsf.create_due_redebits` creates no re-debit for a PAUSED lease
  (the retry waits; it is not stopped). Rent charges are already skipped (`leases_due`).
- **Routes** (FROZEN_ROUTES 320 → 323; the autopay routes `pause`/`resume` keep their names):
  `ledger:leases:pause_collection` `leases/<pk>/pause-collection/` GET (the dialog over the store
  page, `waive.html`'s pattern: `{% dialog %}`, "Are you sure? / What will happen", the reason field
  with `mark=True`, Keep + a primary "Pause collection") and POST; `ledger:leases:resume_collection`
  `leases/<pk>/resume-collection/` POST only (an inline `link-action` form on the card);
  `ledger:leases:activate` `leases/<pk>/activate/` GET dialog ("Activate now": the start date becomes
  today when it is later; the first month is billed) and POST. All three `RouteRule(min_role=Role.BILLING)`
  in `accounts/registry.py`; the views also check the role as `insurance_bill` does; classified in
  `test_plan012f_d_pages` (two `FORM_PAGES`/dialog pages, one `POST_ONLY`); `LeaseRefused` → the
  dialog re-renders with the error (GET+POST pages) or `messages.error` + redirect (resume).
- **Form:** `PauseCollectionForm` with `reason` (Textarea, `max_length=PAYMENT_NOTE_MAX_LENGTH`,
  `validators=[validate_no_payment_data]`, help "Required: why collection is paused."), `clean_reason`
  rejecting blank visible text — `WaiveForm`'s pattern.
- **The LEASE card** (`workspace._header`, `_header.html`): a new first row `header-status` with
  `{% status_badge %}` (Active paid-tone / Paused pending-tone / Prospective neutral / Ended neutral),
  and for `can_bill`: `#pause-collection` (link to the dialog, ACTIVE only), `#resume-collection`
  (inline POST form, PAUSED only), `#activate-lease` (link to the dialog, PROSPECTIVE only); the
  paused line `#pause-reason` "Paused since <date> — <reason>" under the badge. The lease detail
  page's badge keeps reading the status (it already does); the list badge reads "Paused".
- **Notes & follow-ups** (`services/timeline.py`): a new kind `status` sourced from the store's
  leases' audit rows with actions in {`lease.collection_paused`, `lease.collection_resumed`,
  `lease.activated`}: `_KINDS`/`SHOWS`, `entries()`, `Listing.count`, a `TimelineRow` branch in
  `contact_log.timeline_rows`, an `{% elif item.kind == "status" %}` include
  (`_timeline_status.html`: the badge, the words "Collection paused — <reason>", "Collection
  resumed", "Lease activated (start date)" / "(by staff)", the actor and the time). The Charges-tab
  card keeps its three contacts.

**(C) The history cap** (`_edit_lease.html`, `_edit_context`): `rent_history` stays the full list; the
template renders the first five `<li>` and, when there are more, a `<details class="rent-history-more"
id="rent-history-more"><summary>Show all (N more)</summary><ul>…</ul></details>` holding the rest, no
script. `test_the_rent_in_effect_the_upcoming_one_and_the_history_newest_first` is re-pinned
(five visible, the rest inside the details) and a test with seven rows is added.

## 4. Work breakdown (Opus; branch `design-v2/lease-status` from `main` after #55; a commit per task)

1. **Services (A)+(B)** — the four actions, `collection_pause`, the audit kinds, `ActivationSummary` /
   `PauseOutcome` / `ResumeOutcome`; tests in `test_lease_services.py` (activation on/after/before the
   start date, each missing requirement, the early start moving the start date and the only rent row,
   a two-row lease keeping its rows, catch-up charging the first month; pause refused unless active,
   blank/payment-data reasons refused, the audit row's shape, `autopay_paused` true/false; resume
   refused unless paused, `autopay_resume` logic, the catch-up; `collection_pause` with none / one /
   paused-resumed-paused).
2. **The stops while paused** — reminders B–E, messaging send time, autopay plans, re-debits; tests
   beside the existing ones (`test_reminders.py`, `test_plan014_e_reminders.py`, `test_autopay_plans.py`,
   `test_nsf*.py`): paused skipped, active unchanged, resumed collected again.
3. **The daily job** — the step and the sentence; `test_collections_tick`-style test with a pinned day
   (a prospective lease on its start date is activated and charged in the same run; one waiting);
   the tests pinning the summary line re-pinned.
4. **Routes, views, dialogs, the LEASE card** — registry, urls, FROZEN_ROUTES (323, new digest, the
   comment extended), pages classification, `PauseCollectionForm`, the two dialogs, the card row and
   actions; tests: roles (viewer 403, billing allowed), CSRF enforced, the dialog's fields and copy,
   the refusal paths rendered, the queued job kinds (`Job` rows), the card's row and actions per
   status, the list badge "Paused".
5. **Notes & follow-ups** — the `status` kind end to end; tests: the tab, the All entries page, the
   count, ordering with notes and contacts, the kind filter.
6. **The history cap** — template + re-pin + the seven-row test; `--update-baseline -k edit-lease`
   (the visual world's lease has one row, so the baselines should not change; confirm).
7. **Self-review and report** — ruff, format, mypy strict, lock, the targeted tests, e2e once; the
   report the brief asks for.

## 5. Test plan (must pass before acceptance)

Happy paths: §4's. Boundaries: activation exactly on the start date; a start date of today for
"Activate now" (no change to the date); a prospective lease with two rent rows; pause then resume on
the same day (no catch-up charge twice; idempotent charges); a reason of exactly
`PAYMENT_NOTE_MAX_LENGTH`; a paused lease with autopay already paused by staff before
(`autopay_paused` false, resume queues nothing). Error paths: pause on a paused/ended/prospective
lease, resume on an active lease, activate on an active lease — each refused with the sentence, nothing
written; a missing lease 404; the daily job with no prospective lease prints "Leases activated: 0".
Adversarial (Fable writes these too): a reason carrying a card number is refused by the validator; a
reason with markup is escaped on the card and in the timeline; a viewer's POST to each route is 403
and changes nothing; the activation step never touches an ACTIVE, PAUSED or ENDED lease; a PAUSED
lease gets no reminder C/D/E, no plan occurrence and no re-debit on the day they would fall; the
provider is never called from the view or the service (the FakeProvider's call list is empty; only
`Job` rows). Visual: the LEASE card with a Paused badge and reason at both widths in both themes
(a new visual page is not required; the store page's baselines are regenerated if the card changes).

## 6. Risk register

- **Money path:** the catch-up on resume charges the paused months (§3 B). The owner confirms or
  reverses (Q2); the rule is one call, easy to drop.
- **Provider calls:** only through worker jobs; the daily job has no processor access
  (`STAX_CALLER=none`) and activation never touches schedules.
- **Status semantics elsewhere:** PAUSED counts as "current" for the portal, one-off charges and the
  workspace header — unchanged; only reminders, autopay debits, plans and re-debits stop.
- **Routes:** three new names; the autopay `pause`/`resume` routes keep theirs; FROZEN_ROUTES moves.
- **Summary line pins:** the daily job's sentence is appended at the end so existing substrings hold.
- **No migration:** the reason and timing live in audit rows; `collection_pause` reads them.

## 7. Gate class and acceptance criteria

**Full gate.** Accepted when every §5 case exists and is green on sqlite and PostgreSQL; the PR's CI is
green; FROZEN_ROUTES is 323 with the new digest; no model or migration changed; ruff, format, mypy
strict, lock clean; the QA-015 entry names the counts and the verdict.

## 8. Open questions for the owner

1. Should the New lease page save a lease whose start date is in the future as **Prospective** (so the
   daily job activates it on the day, and autopay set-up waits for activation)? Today it saves Active
   with a future start, and nothing in the app creates a prospective lease.
2. On **Resume collection**, the months that fell inside the pause are charged (rent owed, collection
   paused). Confirm, or should paused months be skipped?

## 9. The owner's decisions (2026-11-07), applied on this branch before the merge

1. **Q1 — yes.** The New lease page saves a lease whose start date is after today as **Prospective**
   (`LeaseCreateForm`: `fixed_status` becomes prospective for a future start; Active otherwise); the
   daily job activates it on the start date, or staff "Activate now". Its tests and the page's summary
   words follow; autopay set-up waits for activation (the existing `NOT_ACTIVE` blocker).
2. **Q2 — as built.** The months inside a pause are billed on resume. "Resume collection" becomes a
   dialog (GET + POST, `waive.html`'s pattern) whose first line states it: "Resuming will bill N paused
   months: $X." (N and X from the rent run's catch-up preview for this lease; "Resuming will bill
   nothing." when N is 0). A month to be forgiven is waived afterwards with the existing Waive,
   audited.
3. **Roles.** Pause and Resume collection are **admin only** (`RouteRule(min_role=Role.ADMIN)` and the
   views' own check), consistent with the autopay pause/resume routes. "Activate now" stays billing.
4. **Words.** The LEASE card's actions read "Pause" and "Resume" (ids unchanged) so the Status row fits
   on one line at 1280.
5. **The stale-job race.** A queued `autopay_pause` / `autopay_resume` job carries what it was queued
   for: when queued by Pause or Resume collection, the lease's expected collection status
   (`paused` / `active`). When the job runs, the runner reads the lease under its row lock and, if the
   status no longer matches, does nothing: no provider call, the job completes as skipped and the
   audit row `lease.autopay_pause_stale` / `lease.autopay_resume_stale` records it. A job queued by the
   admin autopay routes carries no expectation and runs as today. Test: pause → resume → the stale
   pause job runs → the schedules stay active at the provider (no call), the audit row is written.
