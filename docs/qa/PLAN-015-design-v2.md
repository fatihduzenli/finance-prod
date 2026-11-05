# PLAN-015 — Design v2: every page in the style of the owner's dashboard mockup

Fable's plan under the root `CLAUDE.md` §1.2 for the owner's request of 2026-11-05: "re-design my
entire pages according to this screenshot". The reference is `docs/design/dashboard-v2-dark-laptop.png`
(717 × 619, dark, laptop). Opus builds; Fable gates each batch; one PR per batch, merged in order.
**Status: draft awaiting the owner's answers to §9 before Batch 0 starts.**

## 1. Objective

The dashboard becomes the mockup: a header with a period control (This month · Quarter · Year),
"Download spreadsheet" and the primary "Record payment"; four KPI cards (Due this month, Collected,
Overdue, Bank deposits), each with an icon in a tinted square, a title, a sub-line, a large figure, a
delta or count badge and a footer link with an arrow; a Collections card (total collected in the year
so far, a monthly bar chart with the current month highlighted and a hover tooltip, Monthly · Yearly);
an Operators card (a grid of operator tiles: initials, name, balance, store count, a status chip); a
Recent activity card (a unified feed of payments, invoices sent, promises, off-provider payments and
lease ends, with a search field, an "All activity" filter and paging). The sidebar gains a user card
with an avatar (initials, name, a role label) and the Reports group a chevron. Every other page then
adopts the same language: KPI cards with icons and footer links, section labels with a right-hand
link, tiles with initials, status chips with a dot, the header's segmented controls, the denser
tables. The tokens (colours, type, radii, spacing) do not change: the mockup uses the existing dark
palette; this is a component and layout redesign, not a new theme.

## 2. Batches and order

| Batch | Scope | Gate class |
|---|---|---|
| 0 | **Shared components**: KPI card v2 (`{% kpi2 %}`: icon, title, sub-line, figure, badge, footer link), the header period control, the avatar initials tile, the status chip with a dot, the card header (section label + right link), the sidebar's user card and the Reports chevron; `app.css` rules; the design-spec update (§4 of `DESIGN-SPEC.md`) | full (shared code on every page) |
| 1 | **Dashboard v2** as the mockup: the services (`dashboard.kpis_v2`, `collections_by_month`, `operator_tiles`, `activity`), the period scoping, the chart, the activity feed with search/filter/paging, the spreadsheet download; the fate of the current sections per §9 | full (money figures; data services) |
| 2 | **List pages** (stores, operators, leases, invoices, payments, bank deposits, outbox): the header with period/segmented controls where a list has a range, the table style, chips, the paging bar | small + full visual |
| 3 | **Workspaces** (store, operator): the summary strip as KPI cards v2, the side cards' headers, tiles for related stores/operators | small + full visual |
| 4 | **Reports and calendar**: report pages' headers and KPI rows, the P&L chart in the Collections chart's style, the calendar's cards | small + full visual |
| 5 | **Forms, builders, wizards, detail pages**: the New lease page already follows the step-card style; invoice builder, add payment, transfers, charge/invoice/payment detail aligned to it | small + full visual |
| 6 | **Admin, help, states, sign-in**: Admin pages' cards, help, the empty states and flashes; the sign-in page stays (redesigned 2026-11-01) | small + full visual |

"small + full visual": the small-change class of CLAUDE.md §1.3, with the visual comparison run for
**every** page (a restyle moves many baselines) and the owner's by-eye check of each batch's pages in
both themes and both widths. Batches 2–6 can be split further by Fable when a batch's diff exceeds
what one review can hold. Phone layouts follow §6 of `DESIGN-SPEC.md` (one column, the fixed save bar).

## 3. Affected packages

`backend/` only: `finance` (templates, `app.css`, templatetags/components), `ledger` (dashboard
services, views, templates; list, workspace, report, form templates), `accounts` (the user card's
role label), `banking` (the Bank deposits KPI reads the connection's institution and last four),
tests and the visual suite. Out of scope: `payments/`, `portal/` (the operator portal keeps its own
sheet), `infra/`, CI, the tokens' values, any model change except §4.1's optional title field.

## 4. Interface contract (Batches 0–1; later batches are templates and CSS only)

### 4.1 Components (Batch 0)
- `{% kpi2 icon title sub value badge badge_tone link link_text %}` — one card; `value` may take a
  tone (`unpaid` red as "Overdue"); `badge` optional ("↑ 4.2%" paid tone, "3 unmatched" pending tone).
- `{% period_control current choices %}` — the header's segmented control, a GET form (`?period=`),
  values `month` (default), `quarter`, `year`.
- `{% avatar name %}` — a 28 px tinted square with the initials (`--link` on `--month-current`).
- `{% chip tone icon text %}` — the status chip with a dot or check ("✓ Paid", "● Due Nov 20, 2026",
  "○ Vacant · 23 days"); tones paid / unpaid / overdue / pending / partial / neutral.
- Card header: `.card-head` with `.section-label` left and `.card-link` right ("All operators ›").
- Sidebar user card: avatar, the display name, a role label, the three links. Role label: the role's
  display ("Admin", "Billing", "Viewer") unless §9.4 adds a title field.
- Reports in the sidebar: a chevron that opens its sub-items (the group stays open on report pages).

### 4.2 Dashboard services (Batch 1), all read-only, each with a flat query count
- `kpis_v2(period, today) -> Kpis`: `due` (the period's rent and charges due, the stores counted,
  the next due day), `collected` (settled in the period, the percent of what is due, the delta
  against the previous period), `overdue` (open and past due, the stores, the oldest days),
  `bank_deposits` (the sum of the period's bank credits, the connection's institution and last four,
  the unmatched count) — each from the existing services (`receivables`, `reports`, `banking`), the
  Aging and Payments-by-method figures reconciled as the Reports index's are.
- `collections_by_month(year, today)` and `collections_by_year(...)`: collected per month (settled),
  the current month flagged; the total since January; the tooltip's Collected and Due per month.
- `operator_tiles(today, limit=4)`: per operator with a current lease — initials, name, open
  balance, store count, a status chip (Paid up / Overdue N days / Partial / Promise · date), ordered
  by balance desc; "All operators ›" to the list.
- `activity(today, query, kind, page)`: the unified feed from the existing timeline/contact log and
  payments: Payment received, Invoice sent, Promise to pay, Paid outside {provider}, Lease ended,
  with store, operator, date, amount and the status chip; `kind` filter (All activity, Payments,
  Invoices, Promises, Leases); 5 a page on the dashboard with ‹ Previous · 1 2 3 · Next ›.
- The spreadsheet download: the period's KPI rows and the activity rows as CSV (`dashboard_export`).
- "Record payment": the existing Add payment page.

### 4.3 Routes
`ledger:dashboard` keeps its name and takes `?period=`, `?q=`, `?kind=`, `?page=`; one new route
`ledger:dashboard_export` (read-only, billing). Everything else is template and CSS.

## 5. Work breakdown (Batches 0–1; later batches get their own tables when scheduled)

| Task | Files | Done when |
|---|---|---|
| 0.1 components | `finance/templatetags/components.py`, `finance/templates/finance/components/*.html`, `app.css` | the seven components render in both themes; `test_design_components.py` pins their markup and the accessibility test passes |
| 0.2 sidebar | `finance/templates/finance/base.html`, `_sidebar.html`, `app.css` | avatar user card; Reports chevron; every page's sidebar baseline moves once |
| 0.3 spec | `docs/design/DESIGN-SPEC.md` §3–§4, `docs/design/HANDOFF.md` §4 | the components documented |
| 1.1 services | `ledger/services/dashboard.py` (or `dashboard_v2.py`) | §4.2 with reconciliation tests against the reports |
| 1.2 page | `ledger/views/dashboard.py`, `ledger/templates/ledger/dashboard/*.html`, `dashboard.js` (the chart's tooltip and the Monthly/Yearly switch) | the mockup in both themes and widths |
| 1.3 export | `views/dashboard.py`, `services/csv_export.py` | CSV of the KPIs and the activity rows |
| 1.4 the current sections | per §9.1 | moved or kept as decided; nothing silently dropped |
| 1.5 tests | `tests/test_dashboard_v2.py`, `e2e/test_dashboard_v2.py`, visual baselines | §6 |

## 6. Test plan (Batches 0–1)

Components: each renders its parts and tones; no colour outside the tokens; every chip has an icon
or dot (the badge rule); the period control is a GET form with three values and refuses a fourth
(falls back to month); the avatar's initials for one-, two- and three-word names and a name with
punctuation; the sidebar card for each role; the Reports chevron open on report pages only.
Dashboard: each KPI equals the report it mirrors for the same period (Due vs the Monthly rent and
charges due; Collected vs Payments by method; Overdue vs Aging's total, store count and oldest
days; Bank deposits vs the Bank deposits page's sum and unmatched count); the delta against the
previous period (positive, negative, zero, no previous data → no badge); the percent of what is due
(0 %, 77 %, over 100 %); the Collections chart's months and current-month flag, the total since
January, the tooltip values; Yearly; the operator tiles' order, chips and store counts; the
activity feed's five kinds, search by store/operator/amount, the kind filter, paging (1–5 of 24),
an empty feed; the period scoping of every figure; the CSV's rows equal the page's; a viewer sees
the page without "Record payment"; query budget flat in stores × operators × payments; the
statement timeout wraps the figures. Adversarial: a payment voided after the period's end; a
promise kept; an operator with two stores in different states; search input with `<script>`;
`?page=9999`; `?period=../etc`. The e2e test hovers a bar and reads the tooltip, switches Monthly →
Yearly, pages the activity.

## 7. Risk register

- **Figures that disagree with the reports** — the reconciliation tests pin each KPI to its report.
- **Shared components change every page's sidebar** — Batch 0 moves every baseline once; Fable
  reads a sample of pages in both themes; the visual suite pins the rest.
- **Dropped dashboard sections** (Needs attention, calendar, Payment status, Coming up, Today's run,
  Failed attempts, Invoices not sent, Last nightly check) — a product decision (§9.1); nothing is
  removed without the owner's word and a home for it.
- **Role label / title** — a model field if §9.4 says so (a migration → full gate).
- **Performance** — the activity feed and the chart add queries to the most-visited page; budgets.
- **Scope creep** — Batches 2–6 restyle; any behaviour change found on the way is listed, not built.

## 8. Acceptance criteria

1. The dashboard matches the mockup in dark and light at 1280 and 390 px (Fable's by-eye compare
   recorded in QA-015), with the departures listed.
2. Every KPI reconciles with its report in a test; the activity feed's rows equal the pages they
   come from.
3. Batch 0's components are used by Batch 1 and by every later batch (no page draws its own KPI).
4. Each batch's gate is green at its class; each merge verified on staging.
5. No token value changes; no portal change; no new dependency.

## 9. Decisions for the owner (before Batch 0)

1. **The current dashboard sections** not in the mockup: Needs attention (7), the mini calendar,
   Payment status (the donut), Coming up, Today's run, Failed payment attempts, Invoices created but
   not sent, Last nightly check. Fable's recommendation: Needs attention becomes a kind in the
   Recent activity filter ("Needs attention") and keeps its count in the Overdue KPI's sub-line;
   the calendar and Coming up stay on the Calendar page; Today's run, Failed attempts, Invoices not
   sent and the nightly check move to Admin › Daily charges as cards. Confirm or change.
2. **Period control**: This month · Quarter · Year scope the four KPIs only (the chart has its own
   Monthly · Yearly; the feed is not scoped). Confirm.
3. **Primary action**: "Record payment" (Add payment) replaces "New invoice" in the dashboard's
   header; New invoice stays in the store workspace. Confirm.
4. **Role label**: the mockup's "Property manager" under the name — use the role's display (Admin /
   Billing / Viewer), or add a free-text title per user (a migration)?
5. **Batch order**: 0 → 1 → 2 → 3 → 4 → 5 → 6 as in §2, one PR per batch, each merged on green
   and verified on staging under the standing approval. Confirm, or name the pages you want first.
6. **Activity feed scope**: five kinds (payment received, invoice sent, promise to pay, paid outside
   the provider, lease ended) at 5 per page on the dashboard. Add or remove kinds?
