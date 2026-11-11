# PLAN-015 Batch 3 — the store and operator workspaces in the dashboard's language (owner, 2026-11-10)

Fable's plan for Batch 3 of `docs/qa/PLAN-015-design-v2.md` (§2: "the summary strip as KPI cards v2, the side cards' headers, tiles for related stores/operators"). References: `docs/design/dashboard-v2-dark-laptop.png`, the built dashboard baselines, and the approved workspace mockups `docs/design/store-workspace-dark-laptop.png` (2026-10-28) and `operator-workspace-laptop.png` (2026-10-23) for everything this batch does not change. Standing approval of 2026-11-10 (push, PR, merge on green, verify staging). Gate class: **small change + the full visual comparison** and Fable's by-eye check of every changed page in both themes and widths; the full suites on the PR's CI.

## 1. Objective

Both workspaces gain the dashboard's figure row: a `kpi-row kpi2-row` of KPI cards v2 under the tabs, on laptop and phone alike, replacing the phone-only summary strip. Every side card's header takes the card-head shape (the section label left, the card's link right). The operator page's store strip becomes tiles in the dashboard's operator-tile style (title, the rent line, the balance figure, a status chip; the whole tile a link). Nothing else moves: the header, the action bar, the tabs, the tab cards, the side cards' rows and the attention banners stay as the approved mockups show them. No new query: every figure is in `Header` / `OperatorWorkspace` / the view context already.

## 2. Packages and scope

`backend/` only: `ledger/templates/ledger/stores/{detail,_summary_strip,_header,_notes_card,_cost_history}.html`, `ledger/templates/ledger/operators/detail.html`, `finance/static/finance/app.css`, tests, visual baselines. `_summary_strip.html` is replaced by `_figures.html` (the KPI row). **Out of scope:** the views and services (no new figure, no query), the tab partials, the action bar, the header (the 56 px operator avatar stays per the spec), the attention banners, the portal, any model/route change.

## 3. Interface contract

### 3.1 The figure row (store page)
`stores/_figures.html`, included where `_summary_strip.html` was (after the action bar, before the tabs — the dashboard's KPIs sit under its header): `<section class="kpi-row kpi2-row" id="summary-strip" aria-label="Summary">` with three `{% kpi2 %}` cards, each wrapped in a `div#figure-<key>`:
- **Open balance** — icon `alert`, title "Open balance", sub "n open charge(s)" from `header.open_count` (empty when 0), value `header.open_balance|money` with `value_tone="unpaid"` when > 0, link `?tab=charges` "Open charges".
- **Rent** — icon `banknote`, title "Rent", sub `header.debit_day_text` ("Debit day 5th") when set, value `header.rent.amount|money` + " / month" (or "—"), link to the lease page "Lease" when there is a lease.
- **Autopay** — icon `calendar`, title "Autopay", sub `header.next_rent`'s day text when set ("Next debit Nov 5, 2026" — the words the NEXT card uses), value `header.autopay` (the strip's string: "Not set up", "Active", …), link `?tab=schedules` "Autopay".
The ids `#summary-strip` and the three values' texts stay what the strip's tests read (`Open balance`, `Rent`, `Autopay` as the titles; the money strings). Phone: two cards a row, the third full width is acceptable (the dashboard's rule); the `.summary-strip` / `.summary-cell` CSS goes.

### 3.2 The figure row (operator page)
`operators/_figures.html`, included after `#operator-actions` / the tabs row where the strip was, **always** (All stores and a selected store alike), four cards: **Open balance** (`ows.open_balance`, sub "n open charge(s)" from `ows.open_count`, unpaid tone when > 0, link `#tab-charges`), **Stores** (value the store count from `ows.stores|length`, sub "" , link `ledger:stores:list?operator=<pk>` "All stores" if that filter exists — else no link; Opus checks `stores.store_list` for an operator filter and reports), **Returns this year** (`returns_this_year`, unpaid tone when > 0, link `?tab=payments` "Payments"), **Promise** (the open promise's "$X by <day>" or "None", pending badge "Open" when one exists, link `#contacts` "Notes & follow-ups"). The selected-store strip (`space` set) is no longer shown: the selected store's figures are in its LEASE card.

### 3.3 Side-card headers
Every side card on both pages takes one shape: `<div class="card-head"><h2 class="section-label" id="…">Label</h2>[<a class="card-link" …>Link</a>]</div>`: LEASE, NOTES ("All notes (n) ›" moves up into the head as the right link when the card has it — Opus checks the current notes card's footer link and keeps its id), NEXT, STORE COST (Change), DOCUMENTS (Upload), CONTACT, PAYMENT, RECENT CONTACT (Log contact), PORTAL. The heading stays an `<h2>` (the labelled-by ids and the tests' regex), not the include's `<p>`; the `.card-header` class on the workspace side goes. The History `details` card stays as it is. The CSS `.workspace-header .card-head` margin matches the old `.card-header`'s.

### 3.4 Store tiles on the operator page
`ul.store-strip#store-strip` keeps its id and becomes a grid of `li > a.store-tile` (the dashboard's `.operator-tile` rules shared through one class, `.tile-v2`, applied to both; no duplicated CSS): head = the store title (bold) + the brand pill; the rent line (`card.rent|money` " / month · " + `card.collect_line`); the balance figure 20/700 (`tone-unpaid` when > 0); a chip: balance > 0 and `card.overdue` → unpaid "Overdue"; balance > 0 → pending "Open balance"; else paid "Paid up". `aria-current="true"` on the selected store's `li` stays; the whole tile links to the store page (`card.store`). Phone: one tile a row. The empty state `#no-stores` stays.

### 3.5 CSS
Additions only: `.tile-v2` (moved from `.operator-tile`'s rules, which keep working), `.store-tile` grid (`auto-fill minmax(13rem, 1fr)` as the strip), `.workspace-header .card-head`; removals: `.summary-strip*` and the old `.store-strip` / `.store-card*` rules once unused. No token change.

## 4. Work breakdown (one Opus session; one commit per task)
1. **Store figures** (§3.1): `_figures.html`, `detail.html`, CSS; re-pin `test_plan012f_pages.py` :162–165 (the strip's dt list → the three kpi2 titles and values) and :239–253 (the operator page's strip assertion → §3.2), `test_plan012f_d_pages.py` phone rules if the strip's are pinned; new tests: the three cards' titles, values, tones, links, for a store with and without a lease; the row present at 1280 (not display:none).
2. **Operator figures and tiles** (§3.2, §3.4): re-pin `test_plan012fa_d_operator.py` :276, :283–311, :347 (the tile strings → the tile's title, rent line, balance figure and chip), `test_operator_workspace.py` order tests (`#summary-strip` position); new tests: the four cards for All stores and a selected store; each tile's chip per balance/overdue; `aria-current`; the query budgets unchanged (`test_plan012fa_d_operator.py:667` 54 / 67 and `test_operator_workspace.py:502` flat).
3. **Side-card heads** (§3.3) on both pages: re-pin `test_plan012f_pages.py:195`, `test_plan012fa_d_operator.py:615, :639–660`, `test_one_line_help.py:354`, `test_store_details_and_help.py:193–252`, `test_plan014_b_store_page.py:330` (`lease_card()` cut — keep LEASE a `div` ending `</div>\n`), `test_notes_card.py`, `test_plan012f_components.py:407`; new test: every side card's first child is `div.card-head` with an `h2.section-label`.
4. **Visual**: regenerate `store-charges`, `store-invoices`, `store-payments`, `store-schedules`, `store-documents`, `store-contacts`, `operator`, `operator-form`, `store-log-contact`, `operator-log-contact` (`visual.sh "store or operator"`); list every changed PNG; nothing outside these may change.
5. **Self-review and checks** (the brief).

## 5. Test plan (Fable verifies)
Happy paths as in §4. Boundaries: a store with no lease (Rent "—", no lease link; Autopay "Not set up"); an operator with no store (no tiles, `#no-stores`, Stores 0); a selected store on the operator page (no second strip; the LEASE card has the figures); balance exactly 0 with a credit. Adversarial: a store title with `<script>` (escaped in the tile and the figure); the viewer role sees the figures and no links it may not open (links are pages the viewer may read: charges tab, lease page, schedules tab). Accessibility: the figure row has `aria-label`; each card head's `h2` keeps its id where an `aria-labelledby` uses it; chips lead with a glyph; contrast pairs. Rule 8. Query budgets flat.

## 6. Risks
- The LEASE card's `div` cut and the side-card label regexes are pinned: re-pin, never loosen.
- The strip's phone CSS is pinned in `test_plan012f_d_pages.py`; add, do not edit pinned strings.
- Duplicated figures (the LEASE card still shows rent, autopay, open balance): accepted — the dashboard's language; the owner may ask to drop rows from the card in a later note.
- No view change: if any figure is missing from the context, stop and report (do not add a query).

## 7. Acceptance criteria
1. Both pages show the `kpi-row kpi2-row#summary-strip` at 1280 and 390 with the cards of §3.1 / §3.2 and no `.summary-strip` CSS left.
2. Every side card on both pages opens with `div.card-head > h2.section-label` (+ `a.card-link` where the card has a link).
3. The operator's stores are `.store-tile` links with the chip of §3.4 and `aria-current` on the selected one.
4. Query budgets unchanged; only the ten workspace baselines change; Fable's by-eye check recorded.
5. Small gate green locally; the full suites green in CI.

## 8. Told to the owner / candidates
- The figures duplicate three LEASE-card rows on the store page; say if the card should drop them.
- The operator header's 56 px round avatar stays (the approved mockup); the v2 square tile is used only in lists and tiles.
