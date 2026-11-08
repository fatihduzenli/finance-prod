# PLAN-015a — the Edit lease page, redesigned in the New lease family

**Owner's request (2026-11-06), queued after the calendar dialog under the same approval.** Style:
`docs/design/new-lease-dark-laptop.png`. Plan by Fable; implementation by Opus; gate and sign-off by
Fable (root `CLAUDE.md` §1). The record is `docs/qa/15/QA-015.md`.

## 1. Objective

The Edit lease page stops being a flat form of every lease field and becomes four cards in the New
lease style plus a sticky "Changes" card. The lease's identity (store, operator, start date, status)
is read-only here: operator changes go through the transfer wizard and lease ends through the lease-end
wizard, both linked from the card. Rent is changed through a "Change rent" control with an effective
date that defaults to the next rent due date, with the rent history shown. Collection (debit day 1–28,
twice a month, the three payment-channel cards) and the proration switch stay editable, the switch only
until the first month has been billed. Billing details are a read-only summary with a link to the
existing billing dialog. The Changes card lists each pending change as "old → new", enables Save only
when something changed, and warns in one line when an autopay would be marked Needs review.

## 2. Scope

**In:** `backend/ledger/` — the edit view (`views/leases.py: lease_edit`, `_edit_form`, `_initial`,
`_lock_known`), a new `LeaseEditForm` in `forms.py` (and `LeaseForm` trimmed of its edit-only
branches), two public service helpers in `services/leases.py`, the templates under
`templates/ledger/leases/` (a new `_edit_lease.html` reached from `form.html`'s `{% if lease %}`
branch, with partials), one small JS file `static/ledger/edit_lease.js`, CSS in
`backend/finance/static/finance/app.css` next to the New lease block, and the tests that pin the page.

**Out:** the wizards, the billing dialog, `update_lease`'s history and schedule rules, the rent run,
the New lease page's behaviour, any model or migration, any route (FROZEN_ROUTES stays at 320), the
operator portal.

## 3. Interface contract

- `ledger.services.leases.next_rent_due(lease, today: date) -> date` — the next date a rent debit
  falls on, strictly after `today`: the lease's debit day (and second debit day when twice a month;
  the nearer one), a day above the month's length clamped to its last day; a lease with no debit day
  gives the first day of the next month. Pure, no query beyond the lease row.
- `ledger.services.leases.first_month_billed(lease) -> bool` — true when a `RentCharge` exists for
  the lease's start month (`period_month = started_on.replace(day=1)`), whatever its proration or
  invoice state. One query.
- `ledger.forms.LeaseEditForm(forms.ModelForm)` on `Lease`, `Meta.fields = ("debit_day",
  "second_debit_day", "payment_channel", "prorate_first_month")`, plus `rent` (Decimal, optional —
  empty means unchanged) and `rent_effective_from` (date, optional — empty means `next_rent_due`).
  `debit_day` and `second_debit_day` are choice fields 1st–28th; when the lease's current value is
  29–31 that value is one more choice, labelled "<n>th (current)", so an unchanged lease stays valid.
  `payment_channel` offers the three cards of `CHANNEL_CARDS` (shared with `LeaseCreateForm`); when
  the lease's current channel is outside them it is one more card, "<name> (current)". The `clean()`
  rules of today's `LeaseForm` (second day needs a first, differs from it; an active lease needs a
  known channel and a debit day) stay. When `first_month_billed`, `prorate_first_month` is removed
  from the form, so a posted value is ignored. `store`, `operator`, `started_on` and
  `collection_status` are not fields: a posted value is ignored, never saved.
- The view's context for the template: `rent_now` (amount, since) from the open or current row,
  `rent_next` (amount, from) when an upcoming row exists, `rent_history` (newest first: amount, from,
  to or open), `rent_default_date` (= `next_rent_due`), `billing_summary` (company · e-mail ·
  "<bank> ····last4" or "no bank account on file", through the existing `detail_cards` helpers; Rule 1:
  the cached last4 only), `has_live_schedule` (a live schedule with a provider reference),
  `first_month_billed`, `first_month_billed_on` (the charge's date for the note), the wizard links
  (`ledger:stores:transfer`, `ledger:stores:end_lease`) and the billing link (`ledger:leases:billing`).
- The POST path still calls `service.update_lease(...)`: with the unchanged fields passed as they are
  and `rent`/`rent_effective_from` only when a rent was entered. The audit row, the history rule (the
  open row closes the day before; a too-early date is refused on the date field) and
  `_flag_stale_schedules` are untouched.
- Markup contract (what the tests and the Changes card rely on): the four cards are
  `section.card.form-card` with ids `lease-card` ("Lease"), `rent-card` ("Rent"),
  `collection-card` ("Collection"), `billing-card` ("Billing details"); the right column is
  `aside.form-summary#lease-changes` > `section.card.summary-card` with `.changes-list` (one
  `li` per pending change, `data-field`), `#autopay-warning` (hidden unless it applies), and
  `.form-actions` with exactly one `button.btn-primary#save-lease` ("Save changes") and the Cancel
  link `#cancel-lease` to the lease detail. Every editable input carries `data-initial` (its value
  on load) and `data-label` (the words the Changes card uses). "Change rent" is
  `button#change-rent` revealing `#rent-change-fields` (`rent`, `rent_effective_from`). Store,
  Operator, Start date and the status badge are plain text with `{% status_badge %}`; the two links
  are `a#transfer-lease` and `a#end-lease`.
- Without JS: Save is enabled, the Changes card says "Change a field above to see it here.", the
  rent fields are visible. Everything else is server-side.

## 4. Work breakdown (Opus; one branch, `design-v2/edit-lease`; a commit per task)

1. **Services** — `next_rent_due`, `first_month_billed` in `services/leases.py`, with tests in
   `backend/tests/test_lease_services.py` (month ends, 29–31 clamped, twice a month, no debit day;
   billed/not billed, a prorated charge, an invoice-billed charge). Done when both are green on
   sqlite.
2. **Form** — `LeaseEditForm`; `CHANNEL_CARDS`/`channel_cards()` shared; `LeaseForm` loses its
   edit-only branches (`rent_effective_from`, `collection_status`), so `LeaseCreateForm` is
   unchanged in behaviour. Tests in `backend/tests/test_lease_views.py` and `test_new_lease_page.py`
   re-pinned (`test_no_status_and_no_rent_date_on_create_both_on_edit`,
   `test_another_channel_is_refused_on_create_and_accepted_on_edit` become: no status on either;
   a current outside channel is kept, a new one is refused). Done when the form's tests are green.
3. **View and templates** — the context above; `_edit_lease.html` with the four cards and the Changes
   card; `_store_context.html` and the breadcrumb unchanged; `edit_lease.js`; CSS. The store page's
   `?store=&lease=` opener keeps working (there is nothing left to lock). Done when the page renders
   for the world's lease in both themes and widths and `test_plan012fa_c_builder` /
   `test_plan012f_d_pages` are re-pinned (cards `["Lease", "Rent", "Collection", "Billing details"]`;
   the primary in the summary; the fields' placement).
4. **Tests of the behaviour** — in `backend/tests/test_edit_lease_page.py` (new): the POST that
   cannot change store, operator, status or start date (the owner's test); prorate ignored once
   billed; debit day 29 refused, a current 30 kept; a current wire channel kept, zelle not offered;
   the rent date default; the rent history rows; the billing summary without bank numbers; the
   autopay warning flag only with a live schedule. e2e in `backend/tests/e2e/test_edit_lease.py`:
   a rent change shows its "old → new" line and enables Save, reverting disables it; a debit-day
   change shows "Debit day 1st → 5th"; the phone width. Visual: `"edit-lease"` added to `PAGES`
   and `AS_ADMIN` in `backend/tests/visual/test_pages.py` (Fable regenerates the baselines in the
   gate; Opus runs `--update-baseline` locally to look, and commits the four files).
5. **Self-review and report** — ruff, format, mypy strict, the targeted tests, e2e once; report
   what was built, skipped, uncertain, untested.

## 5. Test plan (must pass before acceptance)

Happy path: the page renders for an active lease with history; a rent change with the default date
creates the history row from the next due date; a debit-day change saves; a channel change saves; the
proration switch saves while unbilled. Boundaries: debit day 28 accepted, 29 refused, a current 31
kept unchanged; twice a month on/off reveals and clears the second day; a rent effective date equal to
the open row's start refused with the field error; a lease without a debit day defaults to the first of
next month; `first_month_billed` false before the first run and true after. Error paths: an invalid
POST re-renders the four cards with the error on its field, nothing saved; a missing lease is 404;
billing and read-only roles get the admin refusal. Adversarial: a POST carrying `store`, `operator`,
`collection_status`, `started_on` and `prorate_first_month` (billed) changes none of them (Fable also
writes this one); a POST with `payment_channel=zelle` on a stax lease is refused; a POST with
`debit_day=31` on a lease whose current day is 5 is refused; the page body and the Changes card
contain no routing or account number (`test_no_bank_or_routing_data_reaches_any_lease_page` stays
green); the autopay warning never appears without a live provider schedule. Visual: four baselines,
both themes and widths, read by eye against the New lease reference.

## 6. Risk register

- **Status is no longer editable here.** Today's form could set paused / prospective / ended. Ends
  go through the wizard; "paused" and "prospective → active" have no other staff page that Fable
  found. Recorded as an open question for the owner (§8); the page follows the instruction.
- **Rent is a money path.** The default effective date changes what a staff user saves when they do
  not touch the date. `update_lease` is unchanged; the default is tested. Gate class: full (§7).
- **Leases outside the three channels or above day 28** must stay saveable without a change; the
  "(current)" choice handles it; tested.
- **The builder/pages pins** (`test_plan012fa_c_builder`, `test_plan012f_d_pages`) are re-pinned,
  not loosened; `test_every_form_card_ends_in_one_primary_button` must still find the summary's one
  primary.
- **JS off**: Save stays enabled; nothing depends on the Changes card for correctness.
- **Shared code**: `CHANNEL_CARDS` moves to be shared by both forms — the New lease page's tests must
  stay green unchanged.

## 7. Gate class and acceptance criteria

**Full gate**: the form feeds the rent-history and proration paths with a new default and a new lock,
so Fable runs the full class (root `CLAUDE.md` §1.3). The owner may move it to the small class in
writing. Accepted when: every test in §5 exists and is green on sqlite and PostgreSQL; the PR's CI is
green; FROZEN_ROUTES is unchanged; no model, migration or route changed; `uv lock --check`, ruff,
format and mypy strict are clean; the four baselines are committed and match the reference by eye;
the QA-015 entry names the counts and the verdict.

## 8. Open questions for the owner (answered or deferred at sign-off)

1. Pausing collection (status "paused") and moving a prospective lease to active: with Status gone
   from this form, is a dedicated action wanted, or do these stay out of the staff UI for now?
2. The rent history list shows every row; cap at the last twelve with "and N earlier"?
