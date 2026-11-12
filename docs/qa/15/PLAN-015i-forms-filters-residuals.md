# PLAN-015i — the single-card forms, the two admin filter forms and two cosmetic residuals (owner, 2026-11-12)

Fable's plan for the owner's "items 4 and 5" of the Design v2 closing report (2026-11-11), called on 2026-11-12: "go ahead with items 4 and 5 in one PR, small-change gate". Gate class: **small change** (the owner's choice; no migration, no money path, no access change) with the full visual comparison and Fable's by-eye check; the full suites run on the PR's CI.

## 1. Objective

Four small things finish the Design v2 round. (A) The plain single-card form pages open with the step-card head the New lease and Edit lease pages use, so every form page reads the same way. (B) The Activity log and the Messages from the processor pages replace their hand-laid filter forms with the list pages' filter component (the Filters button and panel, Clear and Apply, the chips row). (C) The visual data set's dashboard attention count stops drifting with the wall clock. (D) The status chip in the Message templates table sits on its row's text line. No model, migration, route or query changes; the filters keep their parameter names, so every existing link and the two exports keep working.

## 2. Packages and scope

`backend/` only.
- A: `ledger/templates/ledger/stores/{cost_form,cost_correct}.html`, `contacts/{edit,correct,outcome}.html`, `leases/enrol.html`, `charges/link_invoice.html`, `one_off_charges/link_invoice.html`, `installments/reschedule.html`, `deposits/resolve.html`, and the two other plain single-card form pages found in the tree, `calendar/form.html` and `one_off_charges/form.html`; `ledger/templates/ledger/leases/_step_head.html` (only to omit the note line when no note is given); `finance/static/finance/app.css` if a rule is needed.
- B: `ledger/templates/ledger/audit/page.html`, `ledger/templates/ledger/webhooks/list.html`, `ledger/views/audit.py`, `ledger/views/webhooks.py` (the `active_filters` context only), CSS if needed.
- C: `tests/visual/world.py` / `conftest.py` / `test_pages.py` (tests only).
- D: `finance/static/finance/app.css` (or the templates table's cell), nothing else.
- Tests and visual baselines for the pages named in §4.5.

**Out of scope:** the multi-card pages (New lease, Edit lease, the builder, Add payment, Split, `_billing_step.html`, `_how.html`, `record_payment.html`); every dialog; the audit and webhook services and their queries; the exports; the pagers; as-you-type search on these two pages (they have no search field; the Activity log's budgeted query must run only on Apply).

## 3. Interface contract

### 3.1 A — the step head on the single-card forms
Each page's `section.card.form-card` gains the class `step-card` and opens with `{% include "ledger/leases/_step_head.html" with heading_id=… title=… note=… %}` and **no number** (one card needs no "1"; the Edit lease page's form). `title` is a short name for the card's content, not a repeat of the `h1` (Opus proposes one per page in the report, e.g. "Landlord rent", "Contact", "Autopay", "Invoice", "New dates", "Deposit", "Entry", "Charge"); `heading_id` is `<page>-card-title` and the section takes `aria-labelledby` of it. `note` is the page's existing one help or intro sentence moved into the head (moved, never duplicated: `test_one_line_help.py` must stay green); where the page has none, no note line is drawn. `_step_head.html` omits `<p class="muted step-note">` when `note` is empty; with a note its output is byte-identical to today's. Forms, fields, ids, buttons, "Learn more" links and error rendering are unchanged.

### 3.2 B — the filter component on the two admin pages
Both pages render `<div class="card filter-card">{% filters active=active_filters %} … the page's fields, with their names, ids, labels, maxlengths and values unchanged … {% endfilters %}</div>`; the hand-laid `<form>` and its Show / Filter button go (the panel's Apply submits); the hidden `documents_of` / `store_of` inputs stay inside the block. "Download spreadsheet" (`#audit-export`, `#export-csv`) moves to the header's `div.page-actions`, href unchanged. The results are wrapped in `#results` as the component's `data-results` expects, if the component needs it without a search field (Opus checks `filters.js`; no typing-triggered request may exist on these pages).
- `audit_page` passes `active_filters = list_filters.active(request, fields, accepted=…, keep=("documents_of", "store_of"))` with chips Action, User, Entity type, Entity id, From, To from the parsed query's accepted values (the values the service accepted, not the raw input); the scoped links' `documents_of` / `store_of` survive Clear.
- `webhook list` passes chips Outcome (the label words) and Type; an outcome or type the view does not offer yields no chip and no filter, as today.
- Chips print through the component (autoescaped); a chip's remove link drops that parameter and the page number.
- The too-slow notice, the muted sentence, the messages list, the tables and the pagers are unchanged. Viewer/billing access to both pages is unchanged (the existing decorators).

### 3.3 C — the attention count in the visual data set
Opus finds why the dashboard's attention count differs between runs (a wall-clock read outside `FrozenClock`, e.g. `date.today()`, `datetime.now()`, a DB `now()` default or an `auto_now` field compared to the frozen noon) and pins it in the visual test world only. No production change; if the cause turns out to be production code reading the wall clock where it should read `timezone.now`, stop and report instead of changing it.

### 3.4 D — the template status chip
The chip in `td[data-role=status]` of the Message templates table aligns with the row's text baseline, by the smallest rule that does it, without moving chips on any other page (the other chip baselines must be byte-identical).

## 4. Work breakdown (one Opus session, one commit per task)
1. **A** (§3.1). Re-pin the tests that pin these pages' markup (grep the tests for each touched id, `form-card`, and any slicing helper before running); new tests: each of the twelve pages opens its card with the head, the title's id, `aria-labelledby`, no `.step-number`; the note present where a sentence moved and absent otherwise; `_step_head.html` without a note draws no `step-note`.
2. **B** (§3.2). Re-pin `test_audit_page.py`, `test_webhooks.py`, `test_plan015_batch6*.py` and any e2e that clicks Show / Filter; new tests: each chip per filter; Clear keeps `documents_of` / `store_of`; the export links in the header with the same query; the old submit button gone; no inline style.
3. **C** (§3.3), tests only.
4. **D** (§3.4).
5. **Visual**: regenerate only what changes: `activity-log`, `webhooks`, `message-templates`, `dashboard` (only if C moves it; then it must be stable across two runs). Add no new visual page unless one of the twelve forms already has one (`calendar-entry` is the dialog, not the page). Every other baseline byte-identical, in particular `new-lease`, `edit-lease`, `settings`, `outbox`. `visual.sh "activity-log or webhooks or message-templates or dashboard or new-lease or edit-lease or outbox or settings"`; list every changed PNG.
6. **Self-review and checks**: ruff, format, mypy strict, the targeted tests, `test_one_line_help.py`, the stylesheet tests (one phone `@media` block at the end of `app.css`), Rule 8 (no processor literal outside `backend/payments/`).

## 5. Test plan (Fable verifies and extends)
- Happy: each form page renders its head; each filter page filters by each field through the panel; exports carry the filters.
- Boundaries: a form page with no help sentence (no empty note line); the Activity log with no filter (no chips, "Filters" without a count); with all six filters; the too-slow notice with filters set (chips still shown); Messages with no events.
- Errors: an invalid date or an over-long value in the Activity log (the existing message, no chip for the refused value); an unknown outcome or type on Messages.
- Adversarial: `action=<script>alert(1)</script>` and `user="><img src=x onerror=1>` appear escaped in the chip and in the field; a chip's remove link is a local path that keeps no foreign host; billing/viewer roles get the same 403/200 as today on both pages; `documents_of` with a malformed value does not survive into Clear.
- Accessibility: one `h2` per form card with its id; the filter panel's labels kept; headings order.
- Visual: §4.5, both themes and widths, by eye.

## 6. Risks
- Many tests pin exact markup on these pages: re-pin, never loosen.
- The filter component may assume a search field or `#results`; the Activity log's query has a time budget, so nothing may fire it on typing.
- `_step_head.html` is shared with New lease and Edit lease: their baselines are the proof it did not move.
- Moving the export links changes their place, not their address; e2e selectors by id keep working.
- Nothing security-sensitive is added; no secret, no new dependency, nothing irreversible.

## 7. Acceptance criteria
1. The twelve single-card form pages open with an unnumbered step head; no page shows a sentence twice; `test_one_line_help.py` green.
2. The Activity log and Messages pages render `{% filters %}` with chips for every active filter; parameter names unchanged; both export links in the header with unchanged hrefs; no hand-laid filter form remains.
3. The dashboard baselines are identical across two consecutive visual runs.
4. The template status chip aligns with its row; no other chip baseline changes.
5. Only the baselines named in §4.5 change; all others byte-identical; Fable's by-eye check recorded.
6. Small gate green locally; the full suites green on the PR's CI.

## 8. Assumptions told to the owner
- "Items 4 and 5" are read as the closing report's numbering: 4 = the single-card forms and the two filter forms; 5 = the two cosmetic residuals. All four parts are in this PR.
- `calendar/form.html` (the entry's edit page) and `one_off_charges/form.html` are the same kind of page and are included so no plain form is left over.
- The two admin pages get the Filters panel with Apply, not as-you-type filtering.
