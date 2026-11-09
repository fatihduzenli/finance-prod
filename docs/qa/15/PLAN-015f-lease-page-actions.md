# PLAN-015f — Store page: entry action menus, the Lease ▾ menu, insurance Bill now, Switch to own account (owner, 2026-11-08)

Plan by Fable. Approved as a queued item: "After PLAN-015e, one small change (same approval: push, PR, merge on green, verify staging)." Gate class: **full** — the insurance Bill now rule is a money path (see §9).

## 1. Objective

On the store page (`ledger:stores:detail`), each Notes & follow-ups entry gets at most two controls — an "Outcome ▾" menu on an open promise and a "⋯" menu for the rest — instead of a row of buttons; a closed promise reads "Kept on Nov 28", "Broken" or "Rescheduled to Dec 5"; a promise whose note is only the auto-sentence shows no second line; the author stays the staff name with the e-mail as the hover title. On the LEASE card the Status row shows the badge alone, and Pause / Resume collection, Activate now and Change start date move into the page's "Lease ▾" menu beside Edit lease, Transfer and End lease, each item shown only when applicable and role-allowed. The insurance row's "not billed" gets a "Bill now" for billing roles like the Sign fee row, and "Switch to own account" appears only when the lease uses another store's bank account. The visual world gains a lease active in the previous insurance year so the yellow note and the insurance row are in the baselines.

## 2. Packages

`backend/` only (ledger templates, `ledger/views/contact_log.py`, `ledger/services/workspace.py`, `ledger/services/detail_cards.py`, `ledger/services/insurance.py`, `finance/static/finance/app.css`, `ledger/templates/ledger/_menu_script.html`, `backend/tests/visual/world.py` and baselines). Out of scope: `/leases/<pk>/` page and its "More ▾" menu, the operator page, the Contacts tab's note and e-mail entries (unchanged), the daily insurance generator's rule, any migration, portal.

## 3. Interface contract

### 3.1 Notes & follow-ups entry (`contacts/_log_entry.html`, `LogRow`)

- `LogRow` gains `note: str` — the entry's body, or `""` when the entry is a promise and the body equals the auto-sentence `forms._sentence(amount, day)` for its amount and date (whitespace-trimmed comparison). The template renders the `contact-text` line only when `note` is non-empty. Deleted entries keep rendering the struck body as today.
- `LogRow` gains `outcome_text: str` for a closed promise: `"Kept on {day}"` (the day = `kept_by_payment.settled_on` when set, else `closed_by.happened_on`), `"Rescheduled to {day}"` (the new promise's `promised_date` — the `closed_by` entry of a rescheduled promise; Opus confirms that linkage in `services/contacts.py` before relying on it and reports if it is not so), `"Broken"`; `""` for an open promise. The badge shows `outcome_text` for a closed promise and "Open" for an open one; the "payment settled D" / "recorded D" muted fragment goes away (its information is now in the badge).
- Controls, right-aligned in `.contact-actions`, at most two:
  - **Outcome ▾** (`<details class="menu menu-sm" id="outcome-{pk}">`, summary text "Outcome ▾", `aria-haspopup="menu"`): Kept / Reschedule / Broken as today's links to `ledger:contacts:outcome` with `?next=`. Shown only when `row.can_record_outcome`.
  - **⋯** (`<details class="menu menu-sm" id="more-{pk}">`, summary text "⋯" with `aria-label="More actions"`, `aria-haspopup="menu"`): items in this order, each only when allowed — "Edit" (with "until HH:MM" as muted text inside the item) when `can_edit`; "Add correction" when `can_correct`; "Delete" as a `menu-danger` item when `can_delete`, leading to the existing confirmation page (`contacts/delete.html`, reason required) — no inline deletion. Not rendered when the role has no item.
  - The existing `_outcome_links.html` is deleted if nothing else includes it (Opus checks).
- Author: unchanged — `author|staff` renders the name with `title="{email}"`. Confirmed, not changed.
- Phone (390): the two summaries stay on one line at the right of the meta line; no wrapping of the controls.
- Menus: native `<details>`/`<summary>` (keyboard: Enter/Space toggle, Escape closes and returns focus — existing `_menu_script.html`); add to that script: ArrowDown/ArrowUp move focus between the open menu's items, Home/End to first/last; summary carries `aria-haspopup="menu"`, `<ul role="menu">`, items `role="menuitem"`. Applies to every `details.menu` on the site (the script and markup pattern are shared), so the Lease ▾ menu gets it too.

### 3.2 LEASE card and the Lease ▾ menu (`stores/_header.html`, `services/workspace.py::_actions`)

- Status row: `{% status_badge %}` only, plus the existing muted "Paused since … — reason" line under it when paused (information, not a control; §8 Q1). No Pause / Resume / Activate now links in the card. The Lease row loses the "Change start date" link.
- `_actions` builds the Lease menu with these items, in this order, each present only when its condition holds (`space` already knows the header/lease; the role flags come from the same place as today's `can_bill`/`can_pause_collection`):
  1. "Edit lease" — admin, current lease.
  2. "Change start date" — `header.start_changeable` and (prospective and billing) or (active and admin). Same rule as today's card link.
  3. "Activate now" — prospective and billing.
  4. "Pause collection" — active and admin. "Resume collection" — paused and admin. Same URLs/ids (`pause-collection`, `resume-collection`, `activate-lease`, `change-start` keep their element ids so the e2e/visual selectors and the Playwright tests that click them keep working — Opus greps `tests/e2e` and `tests/visual` for these ids and keeps them).
  5. "Transfer to another operator" — admin, operator not archived (as today).
  6. "End lease" — admin, danger (as today).
- The menu renders for any role that has at least one item (billing sees it on a prospective lease; a viewer never sees it). No current lease: "New lease" (admin) as today.

### 3.3 Insurance Bill now (`detail_cards.py`, `insurance.py::bill_now`)

- `insurance_billable` becomes: current year's charge is `None`, the store has an open date, and `insurance.can_bill_now(lease, year, today)` (new, pure): the store is not closed on or before the anniversary and the lease holds the store on the anniversary by its dates (`holds_on`), whether or not the anniversary has passed and whether or not the lease is prospective. The button's form, URL, view and audit are unchanged.
- `bill_now` uses the same `can_bill_now` rule in place of `in_scope`+`owes`, so a press before the anniversary creates the year's charge with `due_on` = the anniversary (never earlier) and the audited `insurance_charge.created`. The refusal messages stay; `BILL_NOT_OWED` is kept for "does not hold the store on the anniversary / store closed". The daily generator (`generate_insurance_charges`) keeps `owes` — no change to automatic billing.

### 3.4 Switch to own account (`detail_cards.py::LeaseAccount.switchable`)

- `switchable` additionally requires `self.shares.store_id != self.lease.store_id`: a lease sharing a lease of its own store (a renewal) does not offer the switch. Query count unchanged (`shares.store_id` is a column on the already-loaded row).

### 3.5 Visual world (`backend/tests/visual/world.py`)

- The Maple store gets `open_date = date(2024, 6, 15)`. With TODAY = 2026-10-20 and the Maple lease started 2025-01-01 this shows "Insurance 2026–27 · not billed · Bill now" and the yellow note "Insurance 2025–26 never billed" on the Maple store page. Opus regenerates baselines in the Playwright image against PostgreSQL and checks sqlite is pixel-identical (docs/qa/tools; memory rule). Fable reviews the phone and laptop shots in both themes.

## 4. Work breakdown (one Opus session; each task its own commit)

1. **Entry menus** — `_log_entry.html`, `contact_log.py` (`note`, `outcome_text`), `_menu_script.html` (arrow keys, roles), `app.css` (`.menu-sm`, `.contact-actions` one line at 390); delete `_outcome_links.html` if orphaned; re-pin the tests that assert the old buttons (`tests/test_plan012c*`, `test_contact*`, `test_store_workspace`, e2e that click `.promise-kept` etc. — keep working by class or id). Done when: an open promise shows exactly two summaries for billing; a closed promise shows the badge text per §3.1; a viewer sees none; tests green.
2. **Lease ▾ menu and the Status row** — `_header.html`, `workspace.py::_actions`, `workspace_cards.py` if the flags must reach `_actions`; re-pin `test_store_workspace.py` menu HTML and `test_copy_pass.py`; e2e tests that clicked the card links now open the menu first. Done when: the six items appear per §3.2 for admin/billing/viewer × prospective/active/paused/ended.
3. **Insurance Bill now and Switch to own account** — `insurance.py::can_bill_now`, `bill_now`, `detail_cards.py`; tests in `test_insurance_charges.py`, `test_plan014_b_store_page.py`, `test_lease_views.py`. Done when: Bill now shows for billing on every "not billed" row whose lease holds the store on the anniversary; a press before the anniversary bills with `due_on` = anniversary; a same-store sharer has no Switch.
4. **Visual world and baselines** — `world.py` open date; regenerate; report which baselines changed and why (expected: all `store-*` shots for Maple's tabs, `store-log-contact`; nothing else).
5. **Self-review** against §6 and `docs/CLAUDE.md`; run ruff, ruff format --check, mypy, the full sqlite suite, `make check-no-secrets`; report honestly.

## 5. Test plan (Fable verifies; Opus writes the per-task tests)

Happy: open promise → Outcome ▾ with three items + ⋯ with Add correction (billing) / + Delete (admin); Kept → "Kept on <settled day>"; auto-Kept by payment → settled day; Rescheduled → "Rescheduled to <new date>"; Broken → "Broken"; promise with auto-sentence body → one line; promise with a typed note → two lines; call entry (billing, inside the edit window) → ⋯ with Edit "until HH:MM" only. Lease menu for admin on an active lease: Edit lease, Change start date (when changeable), Pause collection, Transfer, End lease; paused: Resume; prospective billing: Change start date, Activate now only; viewer: no menu, no Pause anywhere in the HTML; ended lease: no Pause/Resume. Insurance: anniversary not yet reached, active lease → Bill now; press → charge `due_on` = anniversary, audit row, message BILL_CREATED; prospective lease starting before the anniversary → Bill now; lease ending before the anniversary → "not billed", no button, POST refused BILL_NOT_OWED; already billed → no button, POST refused BILL_ALREADY; viewer POST → 403. Switch: sharer of another store's lease with its own customer → shown; sharer of the same store's lease → hidden and the POST refused by `switch_refusal` (Opus: refusal rule gains the same store check so the URL cannot bypass the card). Boundary: 29 Feb open date in a common year; unicode and 500-char note; promise with `promised_date` None (status rows) never crash `outcome_text`. Adversarial: a billing user POSTs `pause_collection` → 403 unchanged; a hand-built `?next=` on the outcome link stays same-origin (existing check); the ⋯ Delete item for billing is absent and the view still refuses billing; the arrow-key script does not trap focus (Tab leaves the menu). Budgets: the store page query budgets unchanged (`switchable` and `outcome_text` read loaded rows; `closed_by`/`kept_by_payment` are already selected in `store_entries` — Opus verifies with the existing one-query tests).

## 6. Risks

- Element ids used by e2e/visual tests move into menus: keep ids, open the menu in tests.
- The shared menu script changes behaviour on every `details.menu` on the site: arrow keys must not break the Sort menu's forms (`finance/_filters.html`).
- `bill_now` relaxation is a money path: it never changes `due_on`, never bills a year twice (unique constraint on lease+year stands), never changes the daily generator.
- Baselines: regenerate against PostgreSQL in the Playwright image; sqlite pixel-identical check; Fable compares phone and laptop in both themes.
- No migration expected. If Opus finds one is needed (e.g. the reschedule linkage), stop and report before writing it.

## 7. Acceptance criteria

1. Every Notes & follow-ups entry renders ≤ 2 controls, as `details.menu` summaries, per role rules in §3.1; a role with no permitted item sees none.
2. A closed promise's badge reads exactly "Kept on {Mon D}", "Rescheduled to {Mon D}" or "Broken".
3. A promise whose body equals the auto-sentence renders no `contact-text` line.
4. The LEASE card Status row contains the badge (and the paused reason line) and no link; the Lease ▾ menu contains exactly the items of §3.2 for each role/state.
5. Insurance "not billed" rows show "Bill now" for billing whenever `can_bill_now` holds; a press bills the year with `due_on` = anniversary.
6. "Switch to own account" is absent for a same-store sharer; the POST is refused too.
7. Visual baselines show the insurance row, Bill now and the yellow previous-year note on Maple's store page; Fable confirms both widths and themes.
8. Full gate green locally (full class) and in CI; no new ruff/mypy warnings; secret scan clean; query budgets unchanged.

## 8. Owner questions (assumptions taken; the work proceeds under them)

- Q1. The "Paused since … — reason" line stays under the badge (information, not a control). Say if the badge alone is wanted with the reason as its hover title instead.
- Q2. "Edit (until HH:MM)" within the 15-minute window stays, inside the ⋯ menu. Say if it should go.
- Q3. Insurance Bill now before the anniversary bills the year early with the normal due date (the anniversary). Say if early billing should instead be refused with a message.
- Q4. The Contacts tab's note entries (Pin / Edit / Delete) are left as they are; only contact entries (calls, e-mails, visits, promises) get the menus. Say if notes should get a ⋯ too.

## 9. Size and gate class

Medium-small: 4 templates, 4 services/views, CSS + one script, the visual world, baselines. Gate: full (money path in §3.3). Branch `design-v2/lease-page-actions` from `2d79ff1` (fix/credit-return = main + PLAN-015e); rebased onto main once #015e merges.
