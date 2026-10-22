# PLAN-012f — Step 12f: apply the approved design

**Owner's text:** 2026-10-22 ("Step 12f — Apply the approved design (before Step 13)").
**Inputs:** `docs/design/DESIGN-SPEC.md` (§1 tokens, §2 shell, §3 components, §4 pages, §5 rules,
§6 phone) and the four reference screenshots; where the spec is silent the screenshot wins. The
transparent dark logo `monvera_logo_dark_transparent.{png,svg}` in `docs/design/` and
`static/brand/`. All of these are already tracked (they entered the repository with `990f480`).
**Author:** Fable. **Implementation:** Opus, four batches. **Status:** Phase A plan; the
decisions marked *pending* are built under the stated assumption, reversibly.

## 1. Restated objective

The app takes the approved look without changing what it does: the spec's tokens replace the
12c tokens in one file, with Inter self-hosted; the shell becomes the 232 px gradient sidebar
with the transparent logo and the user card, the sandbox banner above a surface page header with
breadcrumb, title and actions, content on `--bg` with 28/32 px padding and a 1,240 px maximum;
every component follows §3 (cards and section labels, the button set, inputs, the segmented
filter, tabs, the six status families with icon and word, tables with the muted header, bold
first-cell link and muted second line, totals row and scroll box, KPI cards, the stacked progress
bar, date chips, contact-log entries with avatar letters, the grid's cell blocks with blank cells
outside the lease and the dashed "Not yet due", one help line per block); the dashboard, the
store workspace (and the operator workspace with its store selector) and the monthly rent
overview take the layouts of §4 and the screenshots; every other page takes the shell, tokens and
components without a new layout; the phone layout of §6 replaces the sidebar with a top bar and
a four-item bottom tab bar, stacks the columns, turns tables into cards, keeps the grid in a
scroll box, pads 90 px at the bottom and keeps touch targets at 44 px; the PWA icon and splash
come from `static/brand/`. No new behaviour, URL, stored value or copy beyond the labels the
screenshots show; the visual baselines are regenerated and the three laptop pages and the phone
store page are shown beside the references in the report.

## 2. Affected packages

`backend/finance` (static: `tokens.css`, `app.css`, `print.css`, `fonts/`, `brand/`; templates:
`base.html`, the banner, the phone shell, the help and sign-in bases; `context_processors.py`
navigation; `pwa.py` colours), `backend/ledger` (templates of every page; the dashboard,
workspace and grid services and views for the new derived blocks; `templatetags/components.py`
for the badge families, the KPI, progress, date-chip and avatar helpers), `backend/tests`
(the design tests, the visual suite, the adversarial files Fable updates), `docs/`. Out of scope:
`payments/`, every service that changes money state, the copy list beyond §3.4 below, Step 13.

## 3. Decisions

### 3.1 Tokens and type (Batch A)

- **D1.** `tokens.css` holds exactly the spec's §1 `:root` block (its names), plus the `pair:`
  documentation lines the AA test reads, rewritten for the spec's pairs: text on bg/surface/
  surface-2, text-2 and muted on surface, link on surface, sidebar-text on the gradient's two
  ends and on `--navy`, sidebar-active-text on the active background over the gradient ends,
  each status text on its status background, the banner text on its background, white on teal
  (primary button) and white on `#B42318`; every text pair ≥ 4.5 (`--faint` is documented as
  decorative, never for text that must be read, and its pair is marked `(ui)` ≥ 3). The old
  12c token names are removed; `app.css`, `print.css`, the grid page's inline styles and every
  template `style` attribute are moved to the new names (a test asserts no old name remains and
  no literal colour outside `tokens.css`, the sidebar gradient included in the token).
- **D2.** Inter is self-hosted: the variable font `InterVariable.woff2` (or the four static
  weights 400/500/600/700 if the variable file is above 400 KB) from the official Inter release,
  pinned by version and SHA-256 in the report, with its OFL licence text beside it; originals
  under `static/fonts/`, served copies under `backend/finance/static/finance/fonts/`;
  `@font-face` with `font-display: swap` in `tokens.css`; no external font URL anywhere (a test
  greps the templates and the stylesheets for `fonts.googleapis`, `fonts.gstatic` and any
  `http` in `@font-face`). *Approved network read for Opus in this step only:* downloading the
  Inter release asset from its official GitHub release; nothing else.

### 3.2 Shell and phone shell (Batch A)

- **D3.** The sidebar per §2: 232 px, `--sidebar-gradient`, padding 20/14, the transparent logo
  48 px high with 26 px below, items 10/12 radius 8 `--sidebar-text` 500, the active item
  `--sidebar-active-bg` white 600, the order Dashboard, Stores, Operators, Monthly rent, Reports,
  Admin (Reports and Admin keep their pages as a nested list that opens under the active group,
  as the 12c navigation did; the screenshot shows the groups collapsed), the user card at the
  bottom (name, role · Sign out) on `rgba(255,255,255,.08)`. The sandbox banner above the page
  header: `#FFF3C4`/`#6B4E00`, 12 px, 600, letter-spacing .04em, centred, full width. The page
  header on `--surface` with a 1 px `--border` bottom: title 20/600 (24/700 on the store page),
  the breadcrumb above the store title (12 px muted), the actions at the right, the search box
  where the page has one (dashboard, stores, grid). Content 28/32 px padding, max-width 1,240 px.
  The 12c top bar goes; its ids that tests use are kept where the element still exists
  (`nav-search`, `nav-search-q`, `user-menu`, `user-menu-name`, `nav-sign-out`, the `nav-*` item
  ids) and listed where they go.
- **D4.** Phone (≤ 768 px, §6): a 64 px gradient top bar (menu button opening the full navigation
  as a sheet, the logo 30 px, a search button that reveals the search field), the banner under
  it shortened to "SANDBOX — TEST SYSTEM" (the title prefix "[SANDBOX]" stays), a white bottom
  tab bar with a 1 px top border and four items with 22 px inline SVG icons and 11 px labels
  (Dashboard, Stores, Monthly rent, More: a sheet with Operators, Reports, Admin, Help, Sign
  out), the active item teal, 90 px bottom padding on the content, touch targets ≥ 44 px, one
  column. The menu and the More sheet are native `<details>` (or a checkbox-and-label as the
  11c menu) so they work without script; a small script closes them on a click outside and on
  Escape. The PWA manifest's theme and background colours follow the tokens; the icon and splash
  are the brand files.
- **D5.** Icons: inline stroke SVG in a template library (`finance/templates/finance/icons/`
  or a `{% icon "name" %}` tag with the paths inline), 16–18 px, `currentColor`, `aria-hidden`;
  the set needed by the screenshots (dashboard, stores, operators, monthly rent, reports, admin,
  search, menu, more, plus, file, image, calendar, user, check, circle, half, dash). No icon
  library, no emoji.

### 3.3 Components (Batch B)

- **D6.** Each §3 component becomes one `app.css` section and, where markup is shared, one
  template partial or tag: `.card` (header row 15/600, count pill, right link), `.section-label`,
  the buttons (`.btn-primary` teal, `.btn` secondary, `.btn-sm`, `.btn-danger` text red, the
  "▾" menu button), inputs and selects (9/12, `--input-border`, search on `--surface-2`), the
  segmented filter (the Charges tab's Open/Paid/All; the grid's status filter stays a select),
  the tabs (2 px teal bottom border, scroll on phones), the status badges (`status_badge` with
  the six families: Paid ✓, Unpaid ●, Overdue ● dark red white text, Pending ◐, Partly paid ◑,
  Waived –, neutral ○; the grid's `due` and `unpaid` map to Unpaid and Overdue), the tables
  (`.table`: 12/500 muted header on `--surface-2`, `--border-soft` rows, `td.money`, the first
  cell's bold link and muted line through a `cell_title` include, the totals row, the
  `overflow-x: auto` box, the sticky store column on the grid), the KPI card tag, the progress
  bar tag (segments from the four status colours with the 8 px legend), the date chip tag, the
  contact-log entry partial (34 px avatar, P pending colours, C neutral), the grid cell block
  (amount over status line; blank outside the lease; "Not yet due" italic faint in a dashed box),
  the help line (`p.help`, 12 px muted, one per block: the 12e rule test holds).

### 3.4 Pages (Batch C) and the rest (Batch D)

- **D7. Dashboard.** Header: title with the long date, the search box, "+ New invoice" (to the
  operator-less builder choice: the New invoice page that picks the store, the route that exists
  for the operator page's New invoice, or the stores list when none; say which). Row 1: four
  KPI cards from figures the dashboard service already computes or can compute in its budget:
  Unpaid balances (red value; "N stores · M overdue"), Collected this month ("x% of <month>
  rent"), Pending bank debits ("N debits · settle by <date>": pending ACH debits with the
  documented five-business-day window as the latest settle date), Today's daily charges
  ("created · skipped · details"). Row 2: the Needs attention table over two thirds (store as
  bold link with the operator below, the why badge, the amount, one action button per row from
  the attention item's existing link), the "<Month> collection" card (collected ÷ billed this
  month, the stacked bar with Paid, Pending, Partly, Unpaid, the legend) and "Coming up" (the
  next three of: the next rent debit day with its store count, the next ACH settlement, the
  next insurance renewals; from existing rows). Row 3: Recent payments (the last five settled
  payments: when, store, for, method, amount) with "All payments →". The dashboard's query
  budget (`test_report_performance`: 25 + 3 × due) is kept or raised by at most the three new
  blocks' queries, each one query, documented. The failed-attempts, not-sent, waived and last
  nightly-check blocks move below Recent payments as cards (nothing hidden).
- **D8. Store workspace.** Header: breadcrumb "Stores › #1991", title "#1991 Istanbul" with the
  status badge, one line "<address> · Open since <date>", the three actions; the tabs with the
  count pill on Charges; body two thirds (the Charges card: segmented filter, table with the
  charge as bold link and its muted line, Due, Status, Amount, Paid, Balance, the Open balance
  total, "Create missing charges" link; the Contact log card with "+ Add entry" and the entry
  partial with its row actions) and one third (LEASE: operator, rent, since, debit day, autopay
  with its Set up link, deposit; NEXT: the next rent, the open promise due, the next insurance
  renewal; STORE COST with Change; DOCUMENTS with Upload and the list with file icons). The
  other tabs render their content in the two-thirds column with the same side cards. The phone:
  the summary strip (Open balance, Rent, Autopay), the full-width primary over two half-width
  secondaries, scrolling tabs, cards. The operator workspace: the same layout with its store
  selector in the header.
- **D9. Monthly rent overview.** Header with "Download spreadsheet"; the filter card (search,
  From/To, Status, Channel, Show); the legend pills (six families; "Not yet due" shown as its
  dashed box in the legend); the grid card: sticky store column ("#no Name" bold link, operator
  below), the current month tinted `#EEF6F5`, each cell a rounded block (amount, then the status
  line with icon and word, "Overdue N d" for an overdue cell, the invoice number or "ACH" where
  the cell has one), the Collected totals row with "paid · unpaid · pending" counts. Twelve months
  still fit at 1,280 px (the e2e laptop test).
- **D10. Every other page** (Batch D) takes the shell, tokens and components: the page header
  with breadcrumb where a parent exists, cards for blocks, the table and form styles; no new
  layout. The sign-in pages take the tokens and the logo. Print styles follow the tokens.
- **D11. Labels that follow the screenshots** (*pending the owner; the copy list otherwise
  stands*): the sidebar item "Monthly rent" (the page title stays "Monthly rent overview"); the
  store tab "Activity" for the admin's history tab; the KPI and card titles of the dashboard as
  the screenshot writes them ("Unpaid balances", "Collected this month", "Pending bank debits",
  "Today's daily charges", "Needs attention", "<Month> collection", "Coming up", "Recent
  payments", "All unpaid balances →", "All payments →"); the section labels LEASE, NEXT, STORE
  COST, DOCUMENTS; the phone banner "SANDBOX — TEST SYSTEM"; the bottom bar "More". Every other
  word is the approved list's.
- **D12. Baselines and the report** (Batch D): the visual suite's thirty baselines regenerated
  in the Playwright image at 1,280 and 390; in addition the three pages at 1,440 and the store
  page at 390 captured from the final tree and placed beside the references in the QA report
  (`docs/qa/12f/` holds the eight images: `*-reference.png` copied from `docs/design/`,
  `*-built.png` from the suite's data set).

## 4. Interface contract

```
tokens.css: the spec's :root block verbatim (names and values), @font-face Inter, the pair: lines
finance/templatetags/components.py: status_badge(label, value) -> six families (paid, unpaid, overdue, pending, partial, waived, neutral)
                                     kpi(label, value, note, tone="") ; progress(segments: list[(family, amount)]) ; date_chip(date) ; avatar(letter, family)
{% icon "name" %} (finance/templatetags/icons.py) -> inline SVG, aria-hidden, 16–18 px
ledger/services/dashboard.py (or the existing data builder): collection_progress(month) -> (collected, billed, segments); coming_up(today) -> up to 3 items; recent_payments(limit=5)
ledger/services/workspace.py: Header gains lease_since, debit_day_text, deposit_text, next_items (rent, promise, insurance)
Phone shell ids: #phone-bar, #phone-menu, #phone-search, #tab-bar (items #tab-dashboard, #tab-stores, #tab-grid, #tab-more), #more-sheet
Sidebar ids: #sidebar, #sidebar-logo, the nav-* item ids kept, #user-card, #nav-sign-out
No route, stored value or audit action changes.
```

## 5. Work breakdown (batches, each gated; checkpoint before the owner's push)

| Batch | Scope | Done when |
|---|---|---|
| A | D1–D5: tokens, Inter, the sidebar, the page header, the banner, the phone top bar, bottom bar and More sheet, the icon tag, the PWA colours | every page renders in the new shell on laptop and phone; the AA test passes on the new pairs; no old token name and no external font |
| B | D6: the component set | every component class exists with tokens only and the templates use them; the six badge families; the one-line help rule holds |
| C | D7–D9: the dashboard, the store and operator workspaces, the grid | the three pages match the screenshots at 1,440 and the phone store page at 390; budgets kept |
| D | D10–D12: every other page, baselines, the side-by-side report | the suite green against new baselines in the image; the report images in place |

## 6. Test plan (before implementation)

A: the tokens file equals the spec's block (a test parses both); the AA test over the new pairs;
no old token name in any stylesheet, template or inline style; no external font; the sidebar
items and order, the active item, the user card for each role; the banner above the header on
every page; the page header's title size per page kind; the phone bar, bottom bar (four items,
the active one) and More sheet markup at a phone width (e2e) and without script; touch targets
≥ 44 px (the existing a11y check extended); the PWA colours. B: a rendered case per component
on a page that uses it; the six badge families with icon and word; the table's first-cell title
and muted line; the totals row; the scroll box; the KPI, progress (segments sum to 100 %), date
chip and avatar tags; the one-line help rule. C: the dashboard's blocks with their numbers from
seeded data (the KPIs, the attention rows with one action each, the progress percentage and
segments, the coming-up items in date order, the five recent payments); the store page's cards
(LEASE, NEXT, STORE COST, DOCUMENTS) with their values; the operator page's store selector; the
grid's tinted current month, cell blocks, totals counts; the query budgets. D: every page
rendered without error for every role at both widths (the accessibility walk extended to the
phone width); the visual suite green; the report images present and the right size.
**Adversarial (Fable):** a company name, operator name, store name and address with markup in
the sidebar user card, the breadcrumb, the KPI notes, the attention rows and the coming-up list;
a dashboard with no data (every card renders with a sentence, no division by zero in the
progress); a month with billed 0 (the percentage reads "—"); a progress bar whose segments
exceed 100 % by rounding never overflows its box (the widths sum ≤ 100); the phone More sheet
offers Sign out as a POST (never a GET) and nothing a viewer may not open; the bottom bar's
active item follows the page; the sidebar's groups show only the pages the role may open (12c
rule); the attention action button never leads a viewer to a form (viewers see the row, not the
button); a store with no number and no address keeps a sensible header; the fonts load from the
app's origin only (an e2e case lists the font requests' hosts).

## 7. Risk register

- **Breadth:** every template changes; tests pinned on the 12c shell (`test_design_shell`,
  `test_responsive`, `test_pwa`, `test_search`, the accessibility walk, Fable's design_a and e4
  files) move with the shell, each change listed; the stored contract (routes, values, audit
  actions) is frozen by test and does not move.
- **Fonts:** a binary asset from the network, once, pinned and checksummed, with its licence; a
  test refuses any external font request.
- **Dashboard queries:** three new blocks, each one query, within the budget test.
- **Phone shell:** two overlays (menu, More) without script, `<details>`-based, a 44 px rule.
- **Visual baselines:** regenerated once, at the end, in the image.
- **Security:** no new input; the search box and the filters keep their validation; the icons
  are inline markup from a fixed table, never from data.

## 8. Acceptance criteria

On staging, at 1,440 and 390: the three pages read like the references side by side; every
other page carries the shell and the components; the phone bottom bar and sheets work with a
thumb; the fonts load from the app; the visual job is green; nothing behaves differently.
