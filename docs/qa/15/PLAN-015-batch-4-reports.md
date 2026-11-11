# PLAN-015 Batch 4 — reports and the calendar in the dashboard's language (owner, 2026-11-10)

Fable's plan for Batch 4 of `docs/qa/PLAN-015-design-v2.md` (§2: "report pages' headers and KPI rows, the P&L chart in the Collections chart's style, the calendar's cards"). References: `docs/design/dashboard-v2-dark-laptop.png` and the built dashboard (the Collections chart, the KPI row, the card heads); the approved `docs/design/reports-dark-laptop.png` (2026-11-03), `profit-and-loss-laptop.png` and `calendar-laptop.png` for what this batch does not change. Standing approval of 2026-11-10. Gate class: **small change + the full visual comparison** and Fable's by-eye check in both themes and widths; the full suites on the PR's CI.

## 1. Objective

The report family and the list-like report pages take the dashboard's header; the Profit and loss figures become KPI cards v2 and the page gains a monthly chart in the Collections chart's style when its range spans more than one month; Aging gains a two-card figure row from figures it already has; the Calendar's side cards take the card-head shape. The Reports index already has the v2 report cards and keeps them. No new query anywhere: the chart reads the rows the page already read.

## 2. Packages and scope

`backend/` only: `ledger/templates/ledger/reports/{pnl,aging,revenue,payments_by_method,deposits,index}.html`, `ledger/templates/ledger/{arrears/page,returns/list,disputes/page,search/results}.html`, `ledger/templates/ledger/calendar/page.html`, `ledger/views/reports.py` (the chart rows for the P&L, from the rows already read), `finance/templatetags/charts.py` (a second bar chart sharing the Collections chart's geometry), `ledger/static/ledger/dashboard.js` → a shared `charts.js` for the tooltip (or the existing script generalised; no duplicated tooltip code), `finance/static/finance/app.css`, tests, visual baselines. **Out of scope:** the filter cards and `_range.html` (they stay), the tables, the Reports index cards, the calendar grid and its JS, the mini calendar, the dashboard, any model/route change, the arrears bucket table (a candidate, §8).

## 3. Interface contract

### 3.1 Headers
- Report family (P&L, Aging, Revenue, Payments by method, Bank account check, Calendar) already has `page-title-block` with the breadcrumb and `h1`; each gains `p.page-sub` under the `h1` where the page has a one-line summary today: Aging — the "As of <day>" text (keep `id="aging-as-of"` on it); P&L — none (its summary is the table card's title); the Reports index — the lede "Every report downloads as a spreadsheet." (keep `id="reports-lede"`, class `page-sub` replacing `page-date`). The actions stay.
- The list-like pages (Unpaid balances, Returned payments, Disputes, Search) take Batch 2's shape exactly: `div.page-title-block` > `h1` + `p.page-sub#list-counts` with today's sentence; `page-actions` with Download (the dashboard's icon) and the page's buttons, the primary last. `.list-counts` stays in the CSS only if a template still uses it after this (the P&L table card's `#pnl-count` keeps it — it is a card line, not a header).

### 3.2 Profit and loss figures as KPI cards v2
`#pnl-kpis` becomes `section.kpi-row.kpi2-row#pnl-kpis` of four `{% kpi2 %}` cards keeping the ids on the figure (`value_id`): **Billed** (icon `banknote`, sub "rent $X · fees $Y"), **Collected** (`check`, value tone `paid`, sub "incl. $Z withheld deposit" when it applies), **Costs** (`dash`, sub "landlord $X · fee costs $Y"; "unknown" as the value when unknown, tone `muted`), **Margin (collected)** (`circle-half`, value tone `paid` / `unpaid` by sign, "unknown" muted; sub "n store(s) unknown"; the "why" explanation becomes the card's footer link "Why ›" to `#pnl-explained`, keeping `id="kpi-unknown"` on that link). Words unchanged. Phone: two a row.

### 3.3 The P&L chart
- When the query spans two or more months, a `section.card#pnl-chart-card` sits between the figures and the table: `div.card-head` (`h2.section-label` "By month") , then `div.collections-chart.pnl-chart#pnl-chart` holding `{% pnl_bars rows %}` and a `div.chart-tooltip#pnl-tooltip hidden`, then a legend `ul.collections-legend` with `{% chart_key "collected" %}` Collected and a new key `costs` (`--chart-expenses`). One month: no chart card (the figures say it all). A range with nothing billed or collected: no chart card.
- `{% pnl_bars rows %}` (`finance/templatetags/charts.py`): paired bars per month — Collected (`--chart-income`) and Costs (`--chart-expenses`, "deliberately quiet", DESIGN-SPEC §7.3) — on the Collections chart's geometry (the same viewBox, axis, ticks, `nice_step`, `tick_label`, radius, label style), refactored so both tags share the geometry and axis code (no copy-paste). Each month is `<g class="collection-bar pnl-month" data-label data-collected data-costs data-margin>` with its two `<rect>`s, a `<title>` ("Oct 2026 · Collected $13,000.00 · Costs $9,600.00 · Margin $3,400.00") and the month label. An unknown cost for a month draws no cost bar and `data-costs="unknown"`.
- The rows come from the view: `pnl.grouped(rows, GROUP_MONTH)` over the rows already read (no new query), passed as `chart_rows` (label, collected, costs or None, margin or None); the month order is the range's.
- The tooltip: `dashboard.js`'s tooltip code moves to `ledger/static/ledger/charts.js` (loaded by both pages) and reads the container's `data-tooltip-fields` (e.g. `collected:Collected,due:Due` on the dashboard; `collected:Collected,costs:Costs,margin:Margin` on the P&L) instead of the hard-coded ids and words; the dashboard's behaviour is unchanged (its e2e tests pass unchanged).
- The chart has `role="img"` and an `aria-label` sentence; the SVG keeps `<title>` per month so it reads without JS.

### 3.4 Aging figures
`aging.html` gains `section.kpi-row.kpi2-row#aging-kpis` above the table when `report` is set: **Unpaid** (icon `alert`, `report.total` in the `unpaid` tone when > 0, sub "n store(s)"), **Oldest** (`calendar`, "N days" from the report's oldest bucket/day when the report exposes it — Opus checks `views/reports.py::_aging_page` and the service; if no oldest figure exists in the context, the row has the Unpaid card alone and the report says so). No new query.

### 3.5 Calendar side cards
`#calendar-day-panel`, `#calendar-next-days` and `#calendar-feed-card` open with `div.card-head` > `h2.section-label` (+ the day panel's `#panel-add-entry` as the right `a.card-link`; the Feed card's "Get the link" as the right link). The day-panel head keeps its existing classes beside `card-head`. Nothing else on the calendar changes.

### 3.6 CSS
Additions only: `.pnl-chart` (the Collections rules apply through the shared class), the `costs` chart key colour, `.kpi-row#pnl-kpis` phone rule if needed; removals of the old `.pnl-kpis` / `.kpi` rules only when no page uses `.kpi` any more (Opus greps; the old `{% kpi %}` tag and its CSS go only if unused after this batch — then the components test's `kpi` cases are replaced, named in the report).

## 4. Work breakdown (one Opus session; one commit per task)
1. **Headers** (§3.1) on the ten pages; re-pin `test_pnl_layout.py:66`, `test_report_index.py:361`, `test_plan012fa_b_lists.py:402-412` (the four other-family headers), `test_design_components.py` (LIST_PAGES / PAGE_HEADER if they cover these), the aging/revenue header tests; new test: every report-family and list-like page's header matches the shape.
2. **P&L figures and Aging figures** (§3.2, §3.4); re-pin `test_pnl_layout.py:107-146`; new tests: the four cards' ids, tones (negative margin unpaid, unknown muted), the Why link; the aging cards from the report's totals; zero states.
3. **The P&L chart** (§3.3): the tag, the shared geometry refactor, `charts.js`, the view's `chart_rows`; tests: a three-month range draws three month groups with two bars each and the right `data-*`; one month → no card; an unknown-cost month → no cost bar; the scale and labels (as `test_the_chart_scale_and_its_labels`); the dashboard chart tests unchanged; e2e: hover a P&L month reads Collected, Costs and Margin; the dashboard e2e tooltip test still passes; query count of the P&L page unchanged.
4. **Calendar side cards** (§3.5); re-pin `test_plan012fa_d_calendar.py:215-308` as needed.
5. **Visual**: regenerate `pnl`, `aging`, `reports`, `calendar`, `calendar-entry`, `search-new-invoice`, `dashboard` (must be byte-identical — proof the tooltip refactor changed nothing) with `visual.sh "pnl or aging or reports or calendar or search-new-invoice or dashboard"`; list every changed PNG; nothing else may change. The visual world's P&L range: Opus states whether the world's `pnl` page spans one month or several (the chart card shows only in the latter) and, if one month, adds a `pnl-months` page to `test_pages.py` (`/reports/pnl/?from=…&to=…` over three months) with new baselines so the chart is in the suite.
6. **Self-review and checks** (the brief).

## 5. Test plan (Fable verifies)
Happy paths per §4. Boundaries: a range whose months have zero collected (bars of height 0, the axis still drawn); a month with costs above collected (negative margin in the title); the `unknown` cost month; the largest figure sets the scale; 12 months fit the viewBox (labels do not overlap — the Collections chart's label rule); the P&L with `detail` on (rows shown) still carries the chart; the statement timeout path (`too_slow`) shows no chart. Adversarial: a month label never carries user text; the tooltip reads `textContent` only; no provider literal; the P&L query budget flat with and without the chart. Accessibility: the chart's `aria-label`, the `<title>`s, the card heads' `h2`s with ids, the kpi2 AA pairs.

## 6. Risks
- The chart is new work not in the owner's P&L mockup: built to the Collections chart's style and the retired Cashflow card's content (DESIGN-SPEC §4a, §7.3); the owner is told and can drop it (§8).
- The tooltip refactor touches the dashboard: its unit and e2e tests and its byte-identical baselines are the proof.
- Many header strings are pinned: re-pin, never loosen.
- No new query: the chart groups the rows already read.

## 7. Acceptance criteria
1. The ten pages' headers share the dashboard's shape; the list-like four carry `p.page-sub#list-counts`.
2. The P&L shows four `kpi2` cards with the ids and tones of §3.2, and the chart card for a multi-month range with the markup of §3.3; one month shows no chart.
3. Aging shows its figure row; the Calendar's three side cards open with `card-head`.
4. The dashboard's baselines are byte-identical; only the named pages' baselines change; Fable's by-eye check recorded.
5. Small gate green locally; the full suites green in CI.

## 8. Told to the owner / candidates
- The P&L chart's content (Collected and Costs bars per month, the margin in the tooltip) was settled from the references; say if you want it dropped or changed (e.g. Billed instead of Collected).
- Unpaid balances' bucket table could become a KPI row of buckets; left as a table (data, not style).
- Revenue, Payments by method and Bank account check keep their range card; a period control there would need the view to map periods onto their `from`/`to` months (small, if wanted).
