# PLAN-012g — dark theme and dashboard layout

Fable's Phase A plan for the owner's prompt of 2026-10-28 ("Step 12g — dark theme and dashboard
layout"). Staging runs 13a at `207077b`. Five batches (0, A, B, C, D), one pull request each,
the usual gate; push, PR and merge in order under the owner's authorisation in that prompt
("I approve each Step 12g PR for merge in order once its CI is green and the previous deploy is
verified on staging; this message is my explicit review and approval for those merges. Stop on any
red."). The QA record is `docs/qa/12g/QA-012g.md`, with the shots of every page in both themes
at both widths. Checkpoint after D, then the Publish Request.

The design source is `docs/design/DESIGN-SPEC.md` §7 (merged in Batch 0 from the owner's
`DESIGN-SPEC-DARK.md`, approved 2026-10-27) and the four reference screenshots
`dashboard-dark-laptop.png`, `dashboard-dark-phone.png`, `store-workspace-dark-laptop.png`,
`monthly-rent-overview-dark-laptop.png`. Where the text and a screenshot differ, the screenshot
wins; §3 below records what the screenshots settled.

## 1. Objective

A per-user **Light · Dark · System** appearance (default System, stored on the user, rendered by
the server with no flash), a complete dark token set with the same WCAG AA guarantee as the light
one, pill-shaped controls in both themes, the dashboard in the new order with the §7.3 chart
colours and the phone behaviours (6-month Cashflow, tap panel under the mini calendar), every page
checked in dark at both widths with a dark visual baseline set, and the light baselines regenerated
where the shape change moved them. PDFs, e-mails, print, invoice previews and exports stay light.
Before any of it, the test suite's page-wide bare-substring assertions are replaced so the new
page and visual tests start from a clean base.

## 2. Packages and scope

One Django project (`backend/`, uv workspace root). Touched: `finance/static/finance/tokens.css`,
`app.css`, `print.css` (scoping only), `finance/templates/finance/base.html`, `banner.html`,
`settings.html`, `finance/context_processors.py`, `finance/pwa.py`, `finance/templatetags/charts.py`
and `components.py`, `accounts/` (a new preference model and migration 0003, the registry), a new
`finance/views/theme.py` (or the smallest home that matches the package's conventions), the
dashboard templates under `ledger/templates/ledger/dashboard/`, `ledger/views/dashboard.py`,
`ledger/services/dashboard.py`, `ledger/static/ledger/dashboard.js`,
`ledger/static/ledger/document_drop.js`, `accounts/templates/two_factor/_base.html`, and tests
(`backend/tests/**`, the visual suite). Out of scope: any Stax or payments code; the e-mail bodies;
the ReportLab PDF; the public statement page (standalone, light, untouched); the brand SVGs (the
transparent dark logo already suits both sidebars); CSV exports; release config.

## 3. Decisions

### 3.0 What the screenshots settled (they win over the text)

Measured on the approved PNGs (pixel samples, 2026-10-28):

- The **page header** in dark sits on the canvas (`#171721` = `--bg`), not on a lighter surface;
  it keeps its 1px `--border` bottom. Light keeps `--surface`. Hence a `--header-bg` token.
- **Cards** in dark are `#1E1E2A` (`--surface`) on `#171721` with no border; hence a `--card-border`
  token (`--border` in light, `transparent` in dark). Table rows and dividers keep `--border`.
- The **sidebar** in dark is flat `#171721` with a 1px `#2E2E3D` right border; the active item is
  the teal tint on it. The wordmark reads teal; the existing transparent logo (white wordmark, teal
  mark) stays — the tint is optional in §7.2 and the logo is a brand asset, not a token.
- The **segmented control**'s thumb in dark is `--surface` (`#1E1E2A`) and its track one step
  lighter than what it sits on: `--surface-2` on the sidebar, `--bg` inside a card. Light keeps the
  `--bg` track and the white thumb. The thumb carries `--text`; no shadow in dark.
- **Secondary buttons** in dark are `--surface` with a `--border` edge and `--text`; the primary
  button is the same teal as light (`#0E8F86`, white text).
- The **popover** is `--surface-2` with a 1px `#3A3A4C` edge and the one allowed dark shadow.
- The dark **chart** colours match §7.3 exactly (income `#7C8CF8`, expenses `#3E4460`, net
  `#2EC4A6`, donut `#46C47E` / `#8FA3BF` …).
- The phone **bottom bar** labels its first tab "Home" in the mock. The app, the sidebar, the help
  pages and the e2e tests say "Dashboard"; the label stays **Dashboard** (a copy change across
  the navigation is outside "colours only, plus the shape change"). Reported at the checkpoint.
- The phone mock abbreviates large amounts ("$1.21M"). The money rule (§5) and the `money`
  filter stay: at the phone KPI size (20px) a 13-character amount fits a half-width card, and the
  Cashflow totals may wrap to a second line. Reported at the checkpoint.
- The laptop mock shows the Needs attention actions as teal text links; §4 (light) specifies one
  small button per row and the rows' tests pin it. The **button stays**; in dark it is the
  secondary-button style. Reported at the checkpoint.
- Neither mock shows the dashboard's **Collection progress**, **Coming up**, today's run, failed
  attempts, waived, not-sent and last-reconciliation cards. §5 says nothing that exists is hidden:
  Collection and Coming up move to a fifth row `[Coming up ⅔ | Collection ⅓]` under Payment
  status, the operational cards stack after it as today. Reported at the checkpoint; the owner may
  drop the two.

### Batch 0 — the bare-substring assertion sweep (Fable, tests only)

- **0.1 The pattern.** A page-wide `assert needle not in body` (or `in`) with a short alphanumeric
  needle can match inside a random hex id or a CSRF token (four instances so far: "4111" twice,
  "9009", "tx"). The inventory of 2026-10-28 (an AST pass over the 251 test files; `docs/qa/12g/QA-012g.md`
  Batch 0 lists every site by file and line with its rating) found 10 HIGH and 27 MEDIUM absence
  sites and 12 HIGH and 30 MEDIUM presence sites; about 115 LOW sites (punctuated or `$`-prefixed
  needles, JSON/CSV/message haystacks, element-scoped text) are out of the class. Every HIGH and
  MEDIUM site is replaced by one of two forms: a **whole-token
  match** — a shared helper `tests/text_helpers.py` with `as_a_token(needle)` (the regex
  `(?<![A-Za-z0-9])needle(?![A-Za-z0-9])`, escaped), `assert_absent(needle, haystack)` and
  `assert_present(needle, haystack)` — or an **element-scoped** check (the needle against the text
  of the one element that would carry it). Long, punctuated or `$`-prefixed needles and JSON
  haystacks are out of the class and left alone.
- **0.2 The guard.** A new test, `tests/test_assertion_hygiene.py`, reads every test file under
  `backend/tests` and fails on a new page-wide bare-substring assertion with a short needle
  (`assert "<1–8 alphanumerics>" (not )?in (body|page|html|text|content|response.content.decode()|page.content())`),
  with an allow-list of the lines reviewed as safe (each with its reason). It is the fence that
  keeps the base clean for Batch D's page tests.
- **0.3 Also in this PR (docs, Fable):** this plan; the spec merge — `DESIGN-SPEC-DARK.md` becomes
  `DESIGN-SPEC.md` §7 (7.1–7.5 verbatim, plus a §7.6 "What the screenshots settled" carrying §3.0
  above), §4a's three dashboard bullets (Cashflow, Payment status, Mini calendar) lose their
  colour values in favour of "§7.3" and their layout sentence in favour of "§7.5", §4's Dashboard
  paragraph points to §7.5, §1 gains one line pointing to §7.2 for the dark values; the four
  reference PNGs are committed; `DESIGN-SPEC-DARK.md` is deleted.

### Batch A — tokens and switching (Opus; Fable the tests)

- **A1 Tokens.** `tokens.css` keeps its single light `:root` block (the §1 values, unchanged) and
  gains the names the dark theme needs, each with its light value, so every colour in the app
  resolves through a token in both themes: `--sidebar-bg: #0B2545` (under the gradient),
  `--sidebar-border: transparent`, `--header-bg: var(--surface)`, `--card-border: var(--border)`,
  `--on-accent: #FFFFFF` (white text on the primary button, today's calendar number, the active
  pager and step — replacing the nine uses of `--surface` as "white text"), `--btn-primary-text:
  #FFFFFF` (the names `--btn-primary-bg/-border` stay as built; §7.2's `--primary-*` are the same
  three values under the existing names), `--sandbox-bg: #FFF3C4`, `--sandbox-text: #6B4E00`,
  `--seg-track: var(--bg)`, `--seg-thumb: var(--surface)`, `--popover-border: var(--border)`,
  `--popover-shadow: none`, `--month-current` (exists), the chart set `--chart-income #5B6BE8`,
  `--chart-expenses #C9CEDD`, `--chart-net #0E8F86`, `--chart-axis var(--border)`, `--chart-label
  var(--faint)`, `--chart-overdue #B42318`, `--chart-paid #1B7F3B`, `--chart-upcoming #0B2545`,
  `--chart-ring-empty var(--neutral-bg)`, the mini-calendar dots `--dot-rent #13A89E`,
  `--dot-settles #1B7F3B`, `--dot-promise #B7791F`, `--dot-lease-end #B42318`, and `--error-text`
  (exists). Then two **identical dark blocks**, both inside `@media screen` so print media never
  sees them: `:root[data-theme="dark"] { … }` and `@media (prefers-color-scheme: dark) {
  :root[data-theme="system"] { … } }`, holding the §7.2 values, the §7.3 dark chart and dot
  colours, `--header-bg: var(--bg)`, `--card-border: transparent`, `--seg-track: var(--surface-2)`
  on the sidebar (a scoped rule) and `var(--bg)` in cards, `--popover-border: #3A3A4C`,
  `--popover-shadow: 0 12px 32px rgba(0,0,0,.45)`, `--month-current: rgba(46,196,166,.08)`,
  `--error-text: #F2A3A8`, `--login-gradient: none`, `--sidebar-gradient: none`, and
  `color-scheme: dark` so native controls and scrollbars follow. **Chosen differently from
  §7.1's one-line inline script:** System resolves in CSS through `prefers-color-scheme`, which
  paints the right theme on the first frame with no script, works with scripts off, and needs
  nothing on the sign-in shell (whose comment forbids scripts). A test proves the two dark blocks
  declare the same names and values, so the duplication cannot drift.
- **A2 The preference.** `accounts.UserPreference` (one row per user, `OneToOneField(User,
  related_name="preference")`, `theme` CharField with choices `light`, `dark`, `system`, default
  `system`, `updated_at`), migration `accounts/0003_userpreference.py` (with its shard entry in
  `ROUNDTRIP_SHARDS` if a round-trip test is added; the accounts app's migrations are covered by
  the fresh round trips already). `accounts.themes.theme_for(user) -> str` returns the stored
  choice or `"system"` for anonymous or missing rows. A context processor `finance.context_processors.theme`
  exposes `theme`; `base.html` renders `<html lang="en" data-theme="{% block theme %}{{ theme }}{% endblock %}">`;
  the sign-in shell renders `data-theme="system"`; the invoice preview overrides the block with
  `light` (§7.1: previews stay light), as does the invoice print page implicitly (standalone, no
  attribute, no dark block applies).
- **A3 The control.** `POST /settings/theme/` (`finance:theme`, in the registry's POST rules for
  every signed-in role, CSRF-protected) takes `theme` ∈ {light, dark, system} and `next`
  (validated with `url_has_allowed_host_and_scheme`, else the dashboard), saves through
  `get_or_create`, redirects back. Rendered as a three-button form styled as the pill segmented
  control (`<form class="segmented theme-switch">` with three `<button name="theme" value=…>`,
  `aria-pressed` on the current one) under an "Appearance" label at the bottom of the sidebar
  above the user card, and as a row of the phone More sheet. No script is required; with scripts
  on, `dashboard.js`-style progressive enhancement is **not** added (one round trip is fine and
  the no-flash guarantee comes from the server). An audit row is not written (a display
  preference). The settings page gets the same control under a "Appearance" card so it is
  reachable from one more place (a small addition, reported).
- **A4 Charts through tokens.** `charts.py` emits `fill="var(--chart-income)"` etc. instead of
  hex; the legend swatches and the status boxes' keys the same; the light chart palette moves to
  §7.3's light values (income `#5B6BE8`, expenses `#C9CEDD`, net `#0E8F86`) — a visible light
  change on the dashboard, baselines regenerated. The mini calendar's dots take `--dot-*` by kind.
- **A5 Everything else that was hard-coded.** `banner.html` uses `--sandbox-bg/--sandbox-text`
  (its literal-colour exemption goes); `document_drop.js` toggles a class (`is-dragover`, styled
  with `--surface-2`) instead of an inline colour; `pwa.py` gains `THEME_COLOR_DARK = "#171721"` and
  `base.html` renders the `theme-color` meta for the stored choice (two metas with
  `media="(prefers-color-scheme: …)"` for System); the manifest stays light. The sidebar rule
  becomes `background: var(--sidebar-bg); background-image: var(--sidebar-gradient); border-right:
  1px solid var(--sidebar-border)`; cards `border: 1px solid var(--card-border)`; the header
  `--header-bg`; the user card and phone-bar button `color-mix` on `--surface` become `--surface-2`
  in dark through a token (`--sidebar-card-bg`). `--faint` stays decoration-only in both themes.
- **A6 Contrast.** `tokens.css`'s `pair:` comment table gains a `pair[dark]:` list for the same
  pairs (text on surface, text-2, muted, link, every status pair, the sandbox pair, `--on-accent`
  on `--btn-primary-bg`, the chart labels on `--surface`); Fable extends
  `test_design_accessibility.py` to check both lists with the 4.5:1 / 3:1 thresholds (3:1 for
  badges and large text as §7.2 allows). Any pair that fails is a BLOCKER for Opus to re-tune within
  the spec's intent — the screenshot values are expected to pass.
- **A7 Scope of light changes in A:** the dashboard chart palette (§7.3 light), nothing else
  visible in light; the light baselines of `dashboard-*` regenerate, every other light baseline
  must compare identical (the gate proves the token refactor is colour-neutral in light).

### Batch B — shape (Opus)

- **B1** `.btn` (all sizes), text inputs (`input[type=text|search|email|number|date|password|tel|url]`),
  `select`, `.badge`, `.count-pill`, `.pill-type`, the nav and phone search fields, and the sign-in
  fields and button take `--radius-pill`; `textarea` keeps `--radius-ctl` (a pill textarea is not
  in the mock). `.segmented` and `.radio-segmented` become pill tracks with a pill thumb
  (`--seg-track` / `--seg-thumb`, the thumb raised by `--surface` in dark and by the existing 1px
  shadow in light), including the Monthly/Weekly/Yearly control, the list filters and the new
  theme switch. Cards keep 12px; `.dialog` 14px; `.toast` 10px; `.mini-popover`, `.status-box`,
  `.date-chip`, `.menu` items, `.pager-page` keep `--radius-ctl` (§7.4: "calendar day cells and
  popovers keep 10–12px").
- **B2** The verbatim-CSS pins in `test_plan012f_components.py`, `test_plan012f_shell.py`,
  `test_plan012f_d_pages.py`, `test_states.py` are re-pinned by Opus by name, Fable reviews each.
- **B3** Every light baseline that B moves regenerates (most pages carry a button); the dark set
  does not exist yet (D).

### Batch C — dashboard (Opus)

- **C1 Order.** `home.html`: KPI row · Cashflow · `[Needs attention ⅔ | Mini calendar ⅓]` ·
  `[Recent payments ⅔ | Payment status ⅓]` · `[Coming up ⅔ | Collection ⅓]` · today's run ·
  failed attempts · waived · not sent · last reconciliation. `test_the_cards_are_in_the_owners_order`
  re-pinned to this list (three `dashboard-row-thirds`).
- **C2 Cashflow.** A `6m` preset ("Last 6 months") joins the select; the default with no `?range=`
  becomes `12m` on the laptop (the mock's "Last 12 months"); on the phone the default is `6m`:
  with no explicit range the view computes both ranges and renders the card twice (`.cashflow-wide`
  for ≥ 769px, `.cashflow-narrow` for ≤ 768px, one hidden by CSS), each with its own totals, bars
  and select; an explicit `?range=` renders one card for both widths. Legend (Income square,
  Expenses square, Net line) and the range select at the right of the title on laptop; the legend
  under the chart on phone. The Net total green when positive, red when negative (exists). The
  footnote sentence as §7.5. Chart colours by `--chart-*` (A4).
- **C3 Mini calendar.** The title links to Reports › Calendar; ‹ › as round `btn-sm`; today filled
  teal (`--on-accent` number), the selected (tapped or hovered) day ringed teal; dots coloured by
  kind (`--dot-*`, several kinds → up to three dots); a legend under the grid (Rent day · Debit
  settles · Promise · Lease ends). Laptop: the popover as today (`--surface-2`, `--popover-border`,
  `--popover-shadow`), items with a 4px bar in the kind's colour and the item's title and detail.
  Phone (≤ 768px): the popover is not shown; a tap moves the day's list into `#mini-panel` under
  the grid ("Wed, Oct 28 · tapped" heading, the same items); `dashboard.js` does the move; with
  scripts off the phone shows the lists in `details` under the grid for the days with items, so
  nothing is unreachable.
- **C4 Payment status.** Laptop: the donut with the total and the word "charges" in the centre, the
  three boxes with a 3px top border in their colour (`--chart-overdue/-paid/-upcoming`), count and
  amount, "+ n pending · rent and fees" under them, "Last 12 months" (the range label) muted at
  the right of the title. Phone: the donut at the left, the three as a list at the right (dot,
  label, count, amount) and the pending line under the list.
- **C5 Needs attention and Recent payments on the phone.** Attention rows as two-line cards (store
  and amount on line 1; operator, why-badge and the action on line 2), the first three rows shown
  and an "All n ›" link (to the arrears page, the existing `#all-unpaid`) in the card header on
  both widths; Recent payments as two-line rows (store; method · date; amount). Laptop tables as
  today.
- **C6 KPI phone labels.** The 2×2 KPI cards keep their full labels and amounts (§3.0).
- **C7 Tests.** Opus's page tests per item (the order, the 6m preset and the two cards, the
  legend, the tap panel markup, the status boxes, the attention "All n ›"); Fable's e2e at both
  widths (tap a day on the phone → panel under the grid, no popover; hover on laptop → popover;
  the range select; shots) and adversarial cases (`?range=6m` on the laptop, `?cal=` with the
  new default, an attention list of 0 and of 40 rows, a month with a day holding four kinds).

### Batch D — every page in dark, the dark baseline set (Opus the fixes; Fable the suite)

- **D1 The dark set.** The visual suite gains a `theme` dimension: a third and fourth sign-in in
  `world.sign_ins` (`billing-dark`, `admin-dark`, created last so counted ids stay) with
  `UserPreference.theme = "dark"`; `test_the_page_looks_as_its_baseline` is parametrized over
  `theme in ("light", "dark")`, the name `f"{page_name}-{width}"` for light (the 62 existing files
  keep their names) and `f"{page_name}-{width}-dark"` for dark; the login page's dark shot uses an
  anonymous context with `color_scheme="dark"` (System). 62 new baselines, generated in the
  Playwright image against PostgreSQL (the house rule), compared on sqlite. `REPORT_SHOTS` gains
  the four dark references at 1440 and 390 for `docs/design/*-built.png`.
- **D2 The walk.** Fable captures the dark set first (Batch A's tokens already apply everywhere)
  and reviews every page at both widths against §7 and the three laptop mocks: store and operator
  workspaces, the monthly rent overview (cells, current-month tint, "Not yet due" boxes, the
  sticky column), the wizards (step list, tiles, settlement box), the builder (tiles, the chosen
  card's tint — `#E0F4F2` is a light-only literal in the spec: dark uses
  `--sidebar-active-bg`-strength teal, i.e. `rgba(46,196,166,.18)` through a `--selected-bg`
  token added in A1), add payment, split, P&L (negatives in `--unpaid-text`, the hairlines),
  daily charges (the result strip's three tones), help (the active topic tint), states (flash
  tones in dark: success/warning/error/info pairs added to the token sets in A1 as
  `--flash-*`), the login (System, both schemes), the dialogs and sheets, the calendar page, the
  documents section, the 404/403 pages. Every defect goes to Opus as a finding; fixes are CSS and
  tokens only unless a template hard-codes something.
- **D3 CI time.** The visual job's time before and after the dark set is recorded (expected to
  double from about 2:20; the 20-minute limit stays).

## 4. Interface contract

- `accounts.models.UserPreference(user: OneToOne, theme: str in {"light","dark","system"} = "system", updated_at)`.
- `accounts.themes.theme_for(user) -> Literal["light","dark","system"]`.
- Context: `theme` (the stored choice) on every page; `<html data-theme>` ∈ {light, dark, system}.
- `POST /settings/theme/` → 302 to `next` or `/`; body `theme=light|dark|system`; 400 on any other
  value; 403/302 for anonymous; CSRF enforced; GET → 405.
- Tokens: the names in A1, present in all three blocks (light, dark, system-dark); the two dark
  blocks identical.
- Charts: `{% bars %}`, `{% donut %}`, `{% chart_key %}` emit `var(--chart-*)` only.
- Dashboard: `?range=` gains `6m`; default `12m`; `?cal=` unchanged.

## 5. Test plan (written before the code)

- **Switching:** anonymous → `data-theme="system"` on the sign-in shell; a user with no row →
  `system`; stored `dark` → `dark` on every page incl. the admin; the invoice preview → `light`
  regardless; the invoice print page has no attribute; POST each value saves and redirects to
  `next`; a foreign `next` → `/`; a bad value → 400 and nothing saved; GET → 405; a viewer may
  save; the control marks the current choice `aria-pressed="true"` in the sidebar, the More
  sheet and the settings card; the `theme-color` meta follows the choice (one meta for light/dark,
  two with media for system).
- **Tokens:** the light block equals §1 plus the added names with their light values; the two
  dark blocks are identical and equal §7.2 plus the added names; no literal colour outside
  `tokens.css`, the brand SVGs and the public statement; print.css and the `@media print` rules
  resolve to light values (a rendering test with `media="print"` emulation in Playwright
  screenshots the invoice preview and the P&L under print media in a dark session and asserts
  the page background is white); contrast AA for every pair in both lists.
- **Charts:** no hex in `charts.py`'s output; the dot colour per kind; the legend swatches.
- **Shape:** the radius rules as B1 (pins); the textarea keeps 8px; the segmented thumb and track
  tokens.
- **Dashboard:** C7 above; the order; the 6m preset bounds (`MAX_RANGE_MONTHS` unchanged); the
  default 12m; the two-card render only without `?range=`; the phone tap panel (e2e); the popover
  on laptop (e2e); the attention card's "All n ›" count equals the full list's length; zero rows.
- **Visual:** 62 light + 62 dark baselines pass on PostgreSQL and sqlite; the light set is
  pixel-identical to the previous set everywhere a batch did not change the page.
- **Adversarial:** `theme=<script>`, a 300-character `next`, a `next` to another host, a POST
  without CSRF, a user row deleted between request and save (get_or_create), `?range=6m&from=…`
  mixed parameters, `?cal=2026-13`, forty attention rows on a phone, a day with every kind.

## 6. Risks

- The token refactor touches every page: Batch A's gate proves light is unchanged except the
  dashboard chart palette (the full light visual set must compare identical).
- `--surface`-as-white-text (nine places) is the easiest regression: a dark surface would make the
  primary button's text vanish; the contrast test over `--on-accent` on `--btn-primary-bg` and
  the dark shots catch it.
- Print: a dark session printing the P&L must come out light; tested under print-media emulation.
- The two identical dark blocks can drift: the equality test.
- The visual job's time doubles; the limit is 20 minutes and today's run is 2:20.
- The dashboard's two-card Cashflow render is the one piece of new server logic; it is bounded
  to the no-range case and covered by unit tests on both ranges.
- Shape changes move nearly every light baseline (B): regenerate once, from the Playwright image
  on PostgreSQL, and compare sqlite pixel-identical.
- No security surface beyond the theme POST (CSRF, role, `next` validation, a closed value set).

## 7. Acceptance

- Light · Dark · System per user, default System, server-rendered, no flash (no script), the
  control in the sidebar, the More sheet and Settings; the login follows System.
- Every colour in templates, CSS and chart output resolves through a token; the dark set matches
  §7.2 / §7.3; AA for every pair in both themes, checked by a test.
- Pill controls in both themes per §7.4; segmented controls as pills.
- The dashboard in the §7.5 order with the §7.3 colours; phone: 6-month Cashflow by default, the
  tap panel under the grid, two-line cards.
- PDFs, e-mails, print output, invoice previews and exports light in a dark session.
- 62 dark baselines added, the light set regenerated only where B or the chart palette changed;
  the CI visual time reported.
- No page-wide bare-substring assertion remains, and the guard test keeps it so.
- Shots of every page in both themes at both widths in `docs/qa/12g/`.
