# PLAN-015 Batch 2 — the list pages in the dashboard's language (owner, 2026-11-10)

Fable's plan for Batch 2 of `docs/qa/PLAN-015-design-v2.md` (§2: "list pages … the header with period/segmented controls where a list has a range, the table style, chips, the paging bar"). References: `docs/design/dashboard-v2-dark-laptop.png` and the built dashboard (`backend/tests/visual/baseline/dashboard-1280-dark.png`, `-390-dark.png`). Approved by the owner on 2026-11-10 with the push / PR / merge-on-green / verify-staging approval for each batch. Gate class: **small change + the full visual comparison**, Fable's by-eye check of every changed page in both themes and both widths; the full suites on the PR's CI.

## 1. Objective

The seven list pages read like the dashboard: the header is the dashboard's (title, a muted sub-line with the counts, the controls and buttons on the right, a segmented period control where the list has a date range); the Payments totals are KPI cards v2; every status that is still plain text or a hand-written badge is a status chip; the Operators list shows the avatar tile; Bank deposits' one filter is a segmented control. The tables, the filter card, the pager and the empty states already follow the dashboard (the table rules are shared, the pager is the dashboard's) and are not changed. There is no staff `/invoices/` page: the invoices list is the store and operator pages' Invoices tab, which is in scope for its status chips; a separate page is not built (told to the owner).

## 2. Packages and scope

`backend/` only: `ledger/templates/ledger/{stores,operators,leases,payments,outbox}/list|page.html`, `ledger/templates/ledger/stores/_tab_invoices.html`, `banking/templates/banking/page.html`, `finance/templates/finance/components/kpi2.html` + `finance/templatetags/components.py` (two value tones), `ledger/views/payments.py` (the period links, read-only), `ledger/views/outbox.py` (a status tone per row, read-only), `finance/static/finance/app.css`, tests, visual baselines. **Out of scope:** any model or route change; the filter card and `filters.js`; the table rules (`th, td` padding, header style); `_list_footer.html`; `responsive.py`; the portal; the badge → chip conversion of statuses that already render with `{% status_badge %}` (same look, pinned by many tests; listed under §8 as a candidate for the owner).

## 3. Interface contract

### 3.1 The header (every list page)
Each page's `{% block header %}` becomes the dashboard's shape: `div.page-header` > `div.page-title-block` (`h1`, then `p.page-sub#list-counts` with the counts sentence that `span.list-counts` carried: "3 stores · 2 with a lease · 1 vacant", "1 operator", "2 leases", "18 payments", "2 deposits", "1 message") and `div.page-actions` with, in this order: the segmented control when the page has one (§3.3, §3.6), "Download spreadsheet" with the icon the dashboard's button uses, the page's other buttons as today (Fetch now, Preview next 30 days, the held-for-settings form), the primary "+ New …" last. The header search of the Stores page stays. `.list-counts` goes from the CSS when nothing uses it.

### 3.2 Payments totals as KPI cards v2
`#payment-totals-card` becomes a `div.card-head` (`p.section-label` "Totals of the 18 payments shown" + the existing ⓘ `button.info` and its `#totals-help` tooltip) followed by `section.kpi-row.kpi2-row#payments-totals` of five `{% kpi2 %}` cards, no wrapping card: Settled (icon `banknote`, value tone `paid`), Pending (`circle-half`, neutral), Returned (`alert`, `unpaid` when above zero), Refunded (`dash`, neutral), Deposits held (`lock`, neutral); each with its sub-line as today's tile note when it has one, else "" ; a zero figure takes value tone `muted`. The figure keeps its id (`#total-settled` …) on the `kpi2-value` span (pass `value_attrs` or a wrapper — Opus picks the smaller change and names it). `kpi2` gains value tones `paid` (`--paid-text` pair) and `muted` (`--muted`) beside `unpaid`; the components test pins the three and the contrast pairs test covers them. Phone: two cards a row (the existing `.kpi-row` rule), the figure 22 px as on the dashboard.

### 3.3 Payments period control
`views/payments.py` passes `period_links`: four `(label, href, active)` — "All time", "This month", "Quarter", "Year" — built from `dashboard_v2.window()` for today's month / quarter / year; each href is the current query (`PaymentFilter.as_query`) with `date_from` / `date_to` replaced by the window (and `date_by` kept), "All time" with both removed; `active` when the filter's dates equal that window exactly (All time when both are empty); none active for any other range. The template renders them as `div.segmented.period-control[role=group][aria-label=Period]` of `<a>` links with `aria-current="page"` on the active one, in `page-actions` before Download (the dashboard's Collections switch is the precedent). No `{% period_control %}` (it drops the other filters). Query count unchanged (no new query).

### 3.4 Operators list
The Operator cell leads with `{% avatar_tile operator.display_name %}` before the `cell-title` link (the dashboard's tile), in one line; the Archived and Needs review badges stay. Phone: the avatar stays beside the name.

### 3.5 Status chips where statuses are text or hand-written
- **Outbox**: `views/outbox.py` gives each row `status_tone` (`queued` → pending, `held` → pending, `sent` → paid, `failed` → unpaid, `cancelled` / combined → neutral; Opus maps every `MessageStatus` value and tests each). The Status cell renders `{% chip tone label %}` with the status's display word ("Queued", "Held", "Sent", "Failed", "Cancelled", "Combined") and, under it, `span.cell-subline` with the rest of today's `messaging.status_text` (the held reason, e.g. "mail not set up"; the combined row's `<a href="#message-…">Combined into the D message: Waiting</a>` link kept verbatim). The cell carries `data-role="status"`.
- **Bank deposits**: the Suggestion cell's confidence becomes `{% chip %}`: Exact → paid "Exact", Strong → pending "Strong", Weak → partial "Weak", no match → neutral "No match"; the "Could be: …" line stays; the kind tag ("Wire", "Zelle") stays a badge. `data-role="status"` on the cell (the table has inputs, so no roles today — the attribute is harmless and documents the cell).
- **Invoices tab** (store and operator pages): the Status cell renders `{% chip %}` by the invoice's internal status — paid → paid, partly paid → partial, sent/unpaid → unpaid, draft/ready → neutral, void/closed → neutral — with the display word; `data-role="status"`. The provider status column stays as it is.
- Every chip text keeps today's words; no new wording.

### 3.6 Bank deposits filter as a segmented control
The filter card (one `status` select) is replaced by `div.segmented[role=group][aria-label=State]` of links New · Confirmed · Split · Ignored · All (`?status=…`, the current one `aria-current="page"`), placed in `page-actions` before Fetch now; the readiness warning stays above the table. The `#filters` form, chips and panel go from this page only (`{% filters %}` is not used here any more); `filters.js` is not loaded when nothing uses it.

### 3.7 CSS
Only additions: `.kpi2-value.tone-paid`, `.tone-muted`; the operators cell's avatar + title row (`.cell-title-avatar`, flex, gap 8 px); the list header's segmented control at 390 (nowrap, scrolling like `.period-control`). No change to the table, pager, filter or badge rules.

## 4. Work breakdown (one Opus session; one commit per task)

1. **Headers** on the seven pages (+ `.list-counts` removal) and the re-pins: `test_plan012fa_b_lists.py` (`list-counts` → `p.page-sub#list-counts`; the family-structure test's header regex), `test_design_components.py::PAGE_HEADER` and `test_every_list_page_has_its_page_header`, `test_plan014_a_dashboard_lists.py` where it reads the header. New: one test asserting the seven headers share the structure (`page-title-block` > `h1` + `p.page-sub#list-counts`; export before the primary).
2. **Payments**: §3.2 and §3.3 with tests: five kpi2 cards with the tones and ids; zero muted; the tooltip; the four period links (active detection for each; other filters kept; All time clears; `date_by=initiated` kept; an arbitrary range → none active); e2e `test_lists_14_a.py::test_the_tiles_tooltip_opens_on_focus` re-pinned; `test_responsive_roles.py::test_the_payments_list_carries_roles` (`#payments-totals` still present).
3. **Operators avatar, Outbox chips, Bank deposits chips and segmented, Invoices tab chips** (§3.4–§3.6) with tests: the avatar initials in the cell; every Outbox status tone and the combined link; every suggestion chip and the kind badge; the segmented links and `aria-current`; the invoices tab chips for each internal status on the store and the operator page; re-pins in `test_banking.py` (`Exact` chip), `test_messaging_engine.py`, `test_digest_messages.py`, `test_plan012fa_b_lists.py::test_the_operators_list_…`, `test_plan014_a_dashboard_lists.py` (the bank deposits filter row if asserted).
4. **Visual**: regenerate `stores`, `operators`, `leases`, `payments`, `bank-deposits`, `outbox`, `store-invoices`, `operator`, `operator-form`, `search-new-invoice` (whichever changed) with `docs/qa/tools/visual.sh "stores or operators or leases or payments or bank-deposits or outbox or store-invoices or operator or search-new-invoice"`; list every changed PNG and why; no page outside the list family may change (the Admin pages, dashboard, reports stay byte-identical).
5. **Self-review and checks** (the brief).

## 5. Test plan (Fable verifies)

Happy: each header renders for billing and admin; the counts sentence unchanged in words; the period links; the KPI tones; chips per status; the avatar for one-, two- and three-word names. Boundaries: zero payments (the totals row at $0.00 all muted; the empty row); a quarter window at the year's edge (Oct–Dec); an Outbox row with no store; a deposit with no suggestion. Adversarial: `?date_from=garbage` (the existing parse keeps the page green; no link active); `?status=<script>` on Bank deposits (refused as today, no link active); an operator name of punctuation only (avatar "?" or the component's rule — Opus states it). Accessibility: every chip leads with its glyph; the segmented controls carry `role=group` + an `aria-label`; the contrast pairs test passes for the new tones. Rule 8: no provider literal. Query budgets of the four pages unchanged (the period links add no query; Outbox tone is from the row).

## 6. Risks

- The list family's structure is pinned by many tests (§4 of the survey): re-pin, never loosen; the header regex covers the new shape.
- `responsive.py` does not detect `.status-chip` as a status cell: every chip cell writes `data-role="status"`.
- `filters.js` swaps `#results`: the Payments KPI row stays inside `#results` so it refreshes as you type.
- The Bank deposits page loses its `#filters` form: e2e and unit tests that drove it are re-pinned.
- No view adds a query; no model, route or migration.

## 7. Acceptance criteria

1. The seven headers share one structure (`page-title-block` + `page-actions`), with the counts in `p.page-sub#list-counts`.
2. Payments shows five `kpi2` cards with the tones of §3.2 and the four period links of §3.3 that keep every other filter.
3. Operators rows lead with the avatar tile; Outbox, Bank deposits and the Invoices tab show chips for every status with `data-role="status"`.
4. Bank deposits' state is a segmented control; its filter card is gone.
5. Visual: only the list-family baselines change; Fable's by-eye check in both themes and widths recorded in QA-015.
6. Small gate green locally (fast checks, targeted tests, e2e once on PostgreSQL, visual on both databases) and the full suites green in CI.

## 8. Told to the owner / candidates (not built here)
- A `/invoices/` list page does not exist; the Invoices tab is restyled instead.
- Statuses already rendered with `{% status_badge %}` keep the badge (same look as the chip); converting them is a mechanical change the owner can ask for.
- An Operators balance / status column (the dashboard tile's figure) needs new queries on the list; not a restyle.
