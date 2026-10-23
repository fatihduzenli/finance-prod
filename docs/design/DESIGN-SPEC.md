# Monvera — design specification (owner-approved, 2026-10-22)

**Change of 2026-10-22 (owner's decision, PLAN-012f Batch A):** four §1 values were nudged so
that every text pair meets WCAG AA, which §5 requires: `--muted` `#6B7A90` → `#5F6E85`, `--link`
`#0E8F86` → `#0B7C74`, `--paid-text` `#1B7F3B` → `#176B33`; the primary button is `#0E8F86` with a
1px `#0B7C74` border (`--btn-primary-bg`, `--btn-primary-border`) while `--teal` stays `#13A89E`
for the logo, the active tab and the bottom bar; `--faint` is decoration only, never for text
that must be read. White on the primary button measures 3.97:1, the one pair under AA, accepted
by the owner for the button's fill; the contrast test pins it. The values below are the ones built.

Reference screenshots in this folder are the source of truth for the look. Where this file and a screenshot differ, the screenshot wins.

- `dashboard-laptop.png`, `store-workspace-laptop.png`, `monthly-rent-overview-laptop.png` — laptop (1440)
- `store-workspace-phone.png` — the phone layout (390); every page follows its rules (§6)
- `monvera_logo_dark_transparent.png` / `.svg` — logo for dark backgrounds, transparent (use in the sidebar; the existing `monvera_logo_dark.*` files have an opaque navy rectangle and must not be used on the gradient)

## 1. Tokens

```css
:root {
  /* brand */
  --navy: #0B2545;  --teal: #13A89E;  --green: #2EC4A6;
  --sidebar-gradient: linear-gradient(180deg, #0B2545 0%, #0E4A5C 100%);
  --sidebar-text: #D6E0EE;  --sidebar-active-bg: rgba(46,196,166,.25);  --sidebar-active-text: #FFFFFF;

  /* surfaces */
  --bg: #F4F6F9;  --surface: #FFFFFF;  --surface-2: #F9FAFC;
  --border: #E3E8EF;  --border-soft: #EEF1F5;  --input-border: #D5DCE6;

  /* text */
  --text: #0B2545;  --text-2: #3D4B60;  --muted: #5F6E85;  --faint: #9AA7B8;  /* faint: decoration only */
  --link: #0B7C74;
  --btn-primary-bg: #0E8F86;  --btn-primary-border: #0B7C74;  /* the primary button; --teal stays the brand accent */

  /* status (bg / text) — always paired with an icon or word, never colour alone */
  --paid-bg: #E3F5E8;      --paid-text: #176B33;
  --unpaid-bg: #FDE8E6;    --unpaid-text: #B42318;
  --overdue-bg: #B42318;   --overdue-text: #FFFFFF;
  --pending-bg: #FFF3C4;   --pending-text: #6B4E00;
  --partial-bg: #FFE8D6;   --partial-text: #8A3B00;
  --waived-bg: #E8ECF2;    --waived-text: #3D4B60;
  --neutral-bg: #E8ECF2;   --neutral-text: #3D4B60;

  /* type */
  --font: Inter, system-ui, -apple-system, "Segoe UI", sans-serif;  /* self-host Inter woff2; no external font loading at runtime */
  --fs-xs: 11px; --fs-sm: 12px; --fs-base: 13px; --fs-md: 14px; --fs-lg: 15px; --fs-h1: 20px; --fs-h1-store: 24px; --fs-kpi: 28px;

  /* shape & space */
  --radius-card: 12px; --radius-ctl: 8px; --radius-pill: 99px;
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px; --space-5: 20px; --space-6: 24px; --space-8: 32px;
  --shadow-none: none;  /* no drop shadows; borders carry the hierarchy */
}
```

Sandbox banner: `#FFF3C4` background, `#6B4E00` text, 12px, 600, letter-spacing .04em, centred, full width, above the page header.

## 2. Shell

- **Sidebar**, 232px, `--sidebar-gradient`, padding 20px 14px. Logo (transparent dark variant) 48px tall at the top with 26px below. Items: 10px 12px padding, radius 8px, `--sidebar-text`, weight 500; active item `--sidebar-active-bg` + white, weight 600. Order: Dashboard, Stores, Operators, Monthly rent, Reports, Admin. Bottom: user card (name, role, Sign out) on `rgba(255,255,255,.08)`.
- On phones the sidebar becomes the existing collapsible menu (Prompt 11); content takes full width.
- **Content column**: `--bg` background. Page header on `--surface` with a 1px `--border` bottom: title (20px/600 on list pages, 24px/700 on the store page), breadcrumb above the store title (12px, muted), primary action(s) at the right, search box where the page has one.
- Content area: 28px 32px padding, max-width 1240px, sections separated by 20–24px.

## 3. Components

- **Card**: `--surface`, 1px `--border`, radius 12px, padding 18px 20px; header row 15px/600 with an optional count pill and a right-aligned link ("All payments →").
- **Section label inside right-column cards**: 13px, 600, `--muted`, uppercase, letter-spacing .04em (LEASE, NEXT, STORE COST, DOCUMENTS).
- **Buttons**: primary `--btn-primary-bg` (#0E8F86) with a 1px `--btn-primary-border` and white text, 10px 16px, radius 8px, 600; secondary white with `--input-border`, same size; small row buttons 6px 12px, 12px text; destructive text `#B42318`. Drop-down buttons end with "▾".
- **Inputs/selects**: 9px 12px, `--input-border`, radius 8px, `--surface-2` background for search fields.
- **Segmented filter** (Open / Paid / All): grey track `--bg` radius 8px, active segment white with a 1px shadow.
- **Tabs**: 13px, 10px 14px, 2px bottom border teal on the active tab, muted text otherwise, horizontal scroll on phones.
- **Status badge**: pill, 12px/600, 3px 9px, icon + word: ✓ Paid, ● Unpaid, ● Overdue (dark red, white text), ◐ Pending, ◑ Partly paid, – Waived, ○ neutral.
- **Tables**: 13px; header 12px/500 muted on `--surface-2`; rows separated by `--border-soft`; numbers right-aligned; first cell carries a bold link and a muted 12px second line; totals row `--surface-2` with a 2px top border; wide tables in an `overflow-x:auto` box; the store column of the grid is sticky.
- **KPI card**: label 12px muted, value 28px/700 (red for unpaid balances), third line 12px muted.
- **Progress bar**: 10px high, stacked segments in status colours, legend of 8px squares below.
- **Date chip** (Coming up list): 44px wide, month 11px muted over day 15px/700, on `--bg` radius 8px.
- **Contact log entry**: 34px round avatar with a letter (P promise = pending colours, C call = neutral), bold kind, muted meta line, row actions at the right.
- **Empty cell in the grid**: blank before/after the lease; "Not yet due" only for future months inside the lease, italic faint text in a dashed 1px `--input-border` box.
- **Help**: at most one 12px muted line under a block; everything else on the help pages.

## 4. Pages

### Dashboard (`dashboard-*.png`)
Header: title + date, search box, "+ New invoice". Row 1: four KPI cards (Unpaid balances, Collected this month, Pending bank debits, Today's daily charges). Row 2: "Needs attention" table spanning two thirds (store, why-badge, amount, one action button per row) + right column with "October collection" progress card and "Coming up" list. Row 3: "Recent payments" table.

### Store workspace (`store-workspace-*.png`)
Header: breadcrumb, "#1991 Istanbul" + green Active badge, one address line + open date; actions Create invoice (primary), Add payment, Lease ▾; tabs Charges · Invoices · Payments · Autopay · Documents · Contact log · Activity. Body: two-thirds column = Charges card (segmented filter, table, open-balance total, "Create missing charges" text link) and Contact log card; one-third column = LEASE, NEXT, STORE COST (with Change), DOCUMENTS (with Upload) cards. Operator workspace uses the same layout with a store selector.

### Monthly rent overview (`monthly-rent-overview-*.png`)
Header: title + "Download spreadsheet". Filter card: search, From/To, Status, Channel, Show. Legend pills. Grid card: sticky store column ("#no Name" bold link, operator below), month columns with the current month tinted `#EEF6F5`, each cell a rounded status block (amount + status line), totals row "Collected" with paid · unpaid · pending counts.

### Login (`login-laptop.png`)
Two panels. Left: the sidebar gradient at 160°, two soft teal circles (12% and 10% alpha) as decoration, logo 52 px, a 34px/700 headline ("Rent collection for every store, in one place."), one muted sentence, three check-marked lines; company name at the bottom. Right: on `--bg`, a 400 px form card area — sandbox banner as a rounded strip, "Sign in" 26px/700, muted sub-line, E-mail and Password fields (12px 14px padding, radius 10px, autocomplete attributes), full-width primary Sign in button (13px padding, radius 10px), a muted "Access is by invitation" line. On phones the left panel collapses to a short gradient header with the logo and the headline; the form takes the full width. **Owner's decision of 2026-10-22 (PLAN-012f D13):** the first field is labelled "Username" (accounts are not e-mail addresses); "Forgot password?" and "Keep me signed in" are left out (no reset flow, no session change); the layout is otherwise as the screenshot shows.

## 5. Rules
- No gradients except the sidebar; no drop shadows; no emoji; icons are inline stroke SVG (Lucide-style, 16–18px).
- Colour is never the only signal (icon or word beside it); AA contrast on every pair above.
- Money through the existing `money` filter (`$2,000.00`).
- Phone width: see §6; nothing is hidden that exists on the laptop.

## 6. Phone layout (≤ 768 px) — `store-workspace-phone.png`

This is the installed-app (PWA) layout, not a shrunken laptop page.

- **Top bar** replaces the sidebar: same gradient, 64 px, menu button (opens the full navigation as a sheet), logo 30 px, search button. Sandbox banner directly under it, shortened to "SANDBOX — TEST SYSTEM".
- **Bottom tab bar**, white, 1px top border, four items with 22 px icons and 11 px labels: Dashboard, Stores, Monthly rent, More (Operators, Reports, Admin, Help, Sign out). Active item teal. Content gets 90 px bottom padding so nothing hides behind it. Touch targets ≥ 44 px.
- **One column** everywhere: the laptop's two-thirds/one-third split stacks, main content first, side cards after.
- **Store page**: breadcrumb, title + badge, one muted line (operator · city), a 3-cell summary strip (Open balance, Rent, Autopay), actions as a full-width primary "Create invoice" over two half-width secondaries, horizontally scrolling tabs, then the tab content.
- **Tables become cards** on phones: one card per row, title and amount on the first line, meta and status badge on the second; totals as a final card. The monthly rent overview keeps the table in a horizontal scroll box with the store column sticky.
- **Dashboard**: KPI cards two per row, Needs attention as cards with the action button full width, the rest stacked.
- Forms: one field per row, sticky primary button at the bottom; wizards show the step list as "Step 2 of 7".
