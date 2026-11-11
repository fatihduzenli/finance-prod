# PLAN-015 Batch 5 — forms, builders, wizards and detail pages aligned to the New lease page (owner, 2026-11-10)

Fable's plan for Batch 5 of `docs/qa/PLAN-015-design-v2.md` (§2: "the New lease page already follows the step-card style; invoice builder, add payment, transfers, charge/invoice/payment detail aligned to it"). Style reference: the New lease page (`docs/design/new-lease-dark-laptop.png`, 2026-11-03, and the built `new-lease-1280-dark.png`): numbered step cards with a muted note, the sticky SUMMARY card (uppercase label, label/value rows, a rule, the bold total row, the primary full width, Cancel under it, a muted help line). The dashboard (`dashboard-v2-dark-laptop.png`) for KPI cards v2 and card heads. The older light mockups (`create-invoice-laptop.png`, `add-payment-laptop.png`, `transfer-wizard-laptop.png`, `charge-detail-laptop.png`, 2026-10-23/24) for what stays. Standing approval of 2026-11-10. Gate class: **small change + the full visual comparison** and Fable's by-eye check in both themes and widths; the full suites on the PR's CI.

## 1. Objective

The form/builder pages take the New lease page's step cards and summary card; the wizards' and the detail pages' figure tiles become KPI cards v2 and their side cards take the card head; the old `{% kpi %}` tag goes when nothing uses it. Dialogs (New store / operator, Log contact, the confirmations, Change start / Pause / Resume / Activate) and the wizard frame itself stay as the approved mockups show them. Templates and CSS only; no new query, no route, no model.

## 2. Packages and scope

`backend/` only: `ledger/templates/ledger/invoices/{new,draft,_builder,_draft_form,_collect,_summary}.html`, `ledger/templates/ledger/payments/{add_payment,_how,_add_payment_summary,detail}.html`, `ledger/templates/ledger/stores/record_payment.html`, `ledger/templates/ledger/installments/split.html`, `ledger/templates/ledger/lease_ends/{_tiles,_wizard}.html` (tiles only), `ledger/templates/ledger/charges/detail.html`, `ledger/templates/ledger/one_off_charges/detail.html`, `ledger/templates/ledger/leases/detail.html`, `ledger/templates/ledger/leases/_step_head.html` (shared as is), `finance/templatetags/components.py` (the old `kpi` tag's removal when unused), `finance/static/finance/app.css`, tests, visual baselines (+ one new visual page). **Out of scope:** New lease and Edit lease (the reference), every dialog, the wizard frame (`_wizard.html`'s steps card, footer, title), `start.html`/`confirmed.html`, invoice preview/print (documents), the portal, the calendar entry form, the plain `card form-card` pages of the survey's list (cost form, contact edit/correct/outcome, enrol, link-invoice, reschedule, deposits/resolve — candidates, §8).

## 3. Interface contract

### 3.1 Step cards on the form/builder pages
Each main-column card takes `{% include "ledger/leases/_step_head.html" with number=N heading_id=… title=… note=… action=… %}` as its first child, replacing its plain `<h2>` / `<legend>`: a `fieldset`'s `<legend>` is kept for accessibility and rendered as the step title inside `.step-title` (the builder's headings regex accepts `legend`). The cards keep their ids and classes and gain `new-lease-step`'s head styling through a shared class (`.step-card`, applied by the `.new-lease-step` / `.edit-lease-card` rule becoming `.step-card`, with the two old classes kept on the lease pages).
- **Invoice builder** (`_draft_form.html`, `_collect.html`): 1 "Lines" (note: the one help sentence the add-line row carries today, moved into the note — one line of help per block), 2 "How to collect" (the legend; note "One choice; the invoice and its charges are written the same way whichever you pick." as the mockup).
- **Add payment** (`add_payment.html`, `_how.html`): 1 "How is this being paid?" (legend; note = today's `#how-help` sentence), 2 "Note" (note "What was received or why it is being charged now."). The summary tiles (four `{% kpi %}`) become `dl.lease-summary-rows` label/value rows Charge / Paid / Balance due and a `lease-summary-total` row "This payment"; `p.after-payment` stays under the rows as the muted line.
- **Store Add payment with allocation** (`record_payment.html`): 1 "Payment", 2 "Allocation" (the lines style stays).
- **Split into installments** (`split.html`): its cards numbered in order (Opus lists them).
Every summary aside (`.form-summary`) takes the New lease summary's shape: `h2.summary-label` (uppercase muted) replacing `h2.section-label`, the rows, the primary full width in `.form-actions`, then `p.summary-cancel > a.btn.btn-quiet` (the builder's "Discard" and Add payment's "Cancel" keep their ids), then `p.muted.summary-after` with the page's one help sentence (the builder's separate `.builder-help` card's sentence moves here and that card goes; Add payment's "Label follows the choice…" likewise if it exists as a help line). The `.lease-summary-*` rules become `.summary-*` shared rules (the lease pages keep working through the same classes).

### 3.2 Figure tiles as KPI cards v2
- **Detail pages** (charge, one-off charge, payment, sent invoice, lease): `section.kpi-row.detail-tiles#detail-tiles` keeps its id and class and holds `{% kpi2 %}` cards: Amount (`banknote`), Paid (`check`, `paid` tone when > 0), Balance (`alert`, `unpaid` when > 0, `muted` at zero); the payment page's Amount / Applied to / Refunded / Remaining and the lease page's Rent / Open balance / Deposit the same way (icons from the pinned set: `banknote`, `check`, `alert`, `lock` for a deposit, `calendar`). Values keep their ids where tests read them (`value_id`). No links.
- **Wizard statement step** (`lease_ends/_tiles.html`): the three tiles → `{% kpi2 %}` (Open balance, Deposit held, Deductions in the `unpaid` tone when > 0); the `js-total-out` class and ids stay on the values (the e2e statement test reads them).
- **The old `{% kpi %}` tag, its template rules (`.kpi`, `.kpi-label`, `.kpi-value`, `.kpi-note`) and its component tests go** once no template uses them (Opus greps after the above; if a user remains, it is listed and the tag stays).

### 3.3 Card heads on the detail pages
The detail pages' cards open with `div.card-head` > `h2` (the section's title) and the card's link at the right where one exists (the Invoices card's "Attach … invoice", the Payments card's "Add a payment" when it is a header link today); the side "Details" card and `#more-card` open with `div.card-head > h2.section-label` (the `frame()` test's string `<h2 class="section-label">Details</h2>` still matches). The lease detail page's header badge becomes `{% status_badge %}` with today's words (a plain `span.badge` today — the only detail header without the status tone).

### 3.4 CSS
Additions and renames only as named: `.step-card` (the shared step-head rules), `.summary-*` (the shared summary rules), `.detail-tiles` keeping the `kpi2-row` grid; the old `.kpi*` rules removed with the tag; the phone rules pinned by tests (`.form-actions { position: sticky …`, `.form-summary .form-actions { position: fixed;`, `.form-layout …`) untouched.

## 4. Work breakdown (one Opus session; one commit per task)
1. **Step cards and summaries** (§3.1) on the builder, Add payment ×2 and Split; re-pin `test_plan012fa_c_builder.py` (the header test, the lines card string, the summary strings, the headings list of each page), `test_plan012fa_c_adversarial.py:94`, `test_plan012f_d_pages.py:888` (the exact-class regex — extend it to the new class, do not loosen), `test_add_payment_page.py` (:73, :79, :477, :541 — the tiles become rows), `test_split_page.py:483`, `test_one_line_help.py` (the one-line rule must still hold on every changed template), `test_copy_pass.py` strings; new tests: each page's step heads in order with numbers and notes; each summary's label, primary, cancel and help line.
2. **KPI cards v2 on the detail pages and the wizard tiles** (§3.2) + the old tag's removal; re-pin `test_plan012fa_b_detail.py` (tile labels from `kpi2-title`; the lease page's value), `test_plan012fc_b.py:633/660`, `test_plan012fc_e.py:112`, `test_lease_end.py:1339`, e2e `test_statement_tile_13a.py` (`.js-total-out` on the kpi2 value), `test_design_components.py` (the `kpi` cases → removed with the tag); new tests: the tones per page (zero muted, paid, unpaid), the ids.
3. **Card heads and the lease badge** (§3.3); re-pin `test_plan012fa_b_detail.py` strings as needed; new test: each detail page's cards open with a head.
4. **Visual**: regenerate `invoice-builder`, `record-payment`, `add-payment`, `charge-detail`, `payment-detail`, `void-payment`, `lease-end-statement` (+ `new-lease`, `edit-lease`, `search-new-invoice` must be byte-identical — the shared class renames must not move them); add a `lease-detail` page (`/leases/<pk>/`, billing) to `test_pages.py` with its four new baselines; `visual.sh "invoice-builder or record-payment or add-payment or charge-detail or payment-detail or void-payment or lease-end-statement or new-lease or edit-lease or search-new-invoice or lease-detail"`; list every changed PNG.
5. **Self-review and checks** (the brief).

## 5. Test plan (Fable verifies)
Happy paths per §4. Boundaries: a builder draft with no lines (the Lines card's empty row, the summary total $0.00); Add payment with no bank on file (the greyed ACH tile, the second card chosen); a detail page at zero balance (muted); a lease detail without a deposit; the wizard statement with no deductions. Adversarial: a charge name with `<script>` in a tile title; the viewer's detail pages render the tiles without actions; no provider literal (the builder note reads `company.provider_name`). Accessibility: every legend kept; the step numbers `aria-hidden`; the summary's heading an `h2`; kpi2 AA pairs. The one-line-help test green on every changed template. Query budgets unchanged.

## 6. Risks
- The exact-class regexes and heading lists are pinned by many tests: re-pin, never loosen.
- Shared class renames touch the New lease and Edit lease pages: their baselines must be byte-identical (the proof).
- Removing the old `kpi` tag reaches every remaining user: grep first; if any page outside this batch still uses it, keep the tag and list the page.
- The one-line-help rule: a step note plus a surviving help line in the same card breaks it — move, do not add.

## 7. Acceptance criteria
1. The builder, Add payment (both) and Split open each card with a numbered step head and close with the New lease summary shape.
2. The five detail pages and the wizard statement show `kpi2` tiles with the tones of §3.2; the old `kpi` tag is gone or its remaining user named.
3. The detail pages' cards open with card heads; the lease header has the status badge.
4. New lease, Edit lease and search-new-invoice baselines byte-identical; the named pages' baselines changed; `lease-detail` added; Fable's by-eye check recorded.
5. Small gate green locally; the full suites green in CI.

## 8. Told to the owner / candidates
- The plain `card form-card` pages (store cost, contact edit/correct/outcome, enrol, link invoice, reschedule, resolve a deposit) keep their single-card frame; they can take the step head later if wanted.
- Invoice preview/print are documents and are not restyled.
- The dialogs stay as the approved modals.
## 9. Addendum (owner, 2026-11-11, folded into Batch 5's PR)

### 9.1 The LEASE card loses the rows the figure row shows
- `stores/_header.html`: the Rent (`#header-rent`), Autopay (`#header-autopay`, with its "Set up" link `#autopay-set-up` and the `autopay_moved` note) and Open balance (`#header-open-balance`) rows go from the LEASE card on both the store page and the operator page's selected store. The Status, Operator/Store, Lease, Free rent, Debit day, Deposit, Sign fee, Insurance, Credit, Company, Billing and Bank rows stay as they are. `header.autopay_moved` (the "moved" note) moves to the Autopay figure card's sub-line when set (after "Next debit …", joined with " · "); nothing else is lost.
- `stores/_figures.html`: the Autopay card's footer link reads **"Set up"** and points to `side_autopay_url` while `header.autopay == "Not set up"` and the lease exists (what the card's row link did), and **"Manage"** to the Autopay tab otherwise; the link keeps `id="autopay-set-up"` in the first case so the e2e and unit tests that click it still find it (re-pin their location, not their words).
- The operator page's selected-store figure row (§3.2 of Batch 3) is unchanged; its LEASE card follows the store page's.
- Re-pin: `test_plan012f_pages.py` (two hits), `test_operator_workspace.py` (two), `test_store_workspace.py`, `test_plan014_b_store_page.py`, `test_copy_pass.py` (the row words), `test_plan015_batch3_workspaces.py` (the Autopay card's link); grep every test for the three ids and `autopay-set-up`; new tests: the three rows absent on both pages; the Autopay card's "Set up" link with its id when not set up and "Manage" when set up; the moved note on the card.
- Visual: the store tabs, store-log-contact, operator, operator-form, operator-log-contact baselines change (the card shorter); regenerate with `"store or operator"` in the batch's visual run and list them.

### 9.2 The move-in checklist's "Trash removal"
No code change: `FeeType.show_in_setup` is the per-type "show in the move-in checklist" switch (`checklist_types()` lists the active fee types with it on), editable in the Django admin's Fee types; its default for a new type is off. "Trash removal" is not in the code or any seed — it is a fee type on staging with the box ticked, which the owner unticks himself (told in the report). Nothing for Opus.
