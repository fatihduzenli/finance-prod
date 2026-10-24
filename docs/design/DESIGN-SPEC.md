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
- `operator-workspace-laptop.png`, `transfer-wizard-laptop.png`, `login-laptop.png` — laptop (1440)
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

### Operator workspace (`operator-workspace-laptop.png`)
Same skeleton as the store workspace. Header: breadcrumb; 56 px round avatar with initials on `#E0F4F2`/`--link`; name 24px/700 with the Active badge and a "Connected to Stax" badge (teal pill) when a Stax customer exists; one muted line (phone · e-mail · SMS consent · store count). Actions: Statement, Edit, More ▾ (Connect to Stax, Archive) — no invoice or payment actions here; those live on the store page. Tabs as on the store page, with a **Store** select at the right of the tab row (All stores / each store). Body, two-thirds column: a strip of store cards (number + name, rent and autopay line, open balance in red or green) above the Charges card listing every store's charges with a Store column; one-third column: CONTACT, PAYMENT (Stax customer link, bank last-4, returns this year, open promise), RECENT CONTACT (Add entry link), PORTAL (Send invite).

### Transfer wizard (`transfer-wizard-laptop.png`) — the same frame for End lease
Header: breadcrumb (Stores › store › Transfer), title "Transfer #1991 Istanbul to another operator", right-aligned "Step 4 of 7 · nothing changes until you confirm". Body: a 260 px left card with the vertical step list (done = green circle with ✓ and muted label; current = teal circle with the number and a bold label; later = outlined circle, faint label), a divider, "outgoing → incoming" and the end date, and a red "Cancel the transfer" link; the main card holds the current step with a 17px/600 title and a muted one-line description. Statement step: three summary tiles (Open balance, Deposit held, Deductions) on `--surface-2`; a Deductions table (reason select, note input, right-aligned amount input, trash icon) with a dashed "+ Add deduction" button; a Settlement box listing deposit held, amounts applied and kept, ending in a green "Refund due" row (or a red "Still owed" row when the deposit does not cover); one muted help line. Footer: ← Back (secondary) and "Save and continue →" (primary), right-aligned. On phones the step card becomes a "Step 4 of 7" line with a progress bar above the main card.

### Stores list (`stores-list-laptop.png`) — the list family *(drafted from the screenshot by Fable, 2026-10-23, for the owner's approval; the screenshot wins)*
Header: title "Stores" with a muted counts line ("517 stores · 486 with a lease · 31 vacant"), "Download spreadsheet" (secondary) and "+ New store" (primary). One filter card: the search box, State, Status (Active), Balance (Any / Owing / Paid up / Overdue), Sort (Store number / Name / Balance) and a dark "Filter" button. Table in a card: Store (the number and name bold, "City, ST" muted under it; a sort arrow on the header), Operator (link), Rent, Collected by (Bank debit / E-mail / —), Balance as a badge ("$3,000.00 unpaid", "$2,100.00 overdue" in the dark red family, "$658.06 partly paid", "Paid up" green, "Vacant" / "Vacant 31 days" neutral outline), Status badge (Active green, Vacant neutral, Closed red outline), a chevron at the right. Footer inside the card: "1–7 of 7 · filtered from 517" left, "‹ Previous · 1 · Next ›" right. Operators, Leases, Payments, Returned payments, Disputes, Unpaid balances and Search follow: counts line, one filter row, title-and-subline first cell, money state as a badge, chevron, footer pager.

### Charge detail (`charge-detail-laptop.png`) — the detail family *(drafted from the screenshot by Fable, 2026-10-23, for the owner's approval; the screenshot wins)*
Header: breadcrumb Stores › #1991 Istanbul › Charges › Security deposit; title "Security deposit" with the status badge (Unpaid) and a kind badge ("Deposit · held, refundable"); one muted line "#1991 Istanbul · fatih · created Oct 14, 2026 from the lease checklist · due Nov 1"; actions "Add payment" (primary), "Split into installments", "More ▾". Body, two thirds: three tiles (Amount, Paid, Balance in red when open); an Invoices card (Number bold with "move-in · 2 lines" under it, Sent, Due, Total, Status badge, "In Stax: Open · view" as a teal pill with a link; header link "Attach Stax invoice"); a Payments card with the empty state (a round icon, "No payment yet.", "Add a payment · or wait for the invoice"); an Activity card (time "Oct 23 · 14:02", bold action, by whom, a quoted note). One third: DETAILS (Category, Collected by, Due, Cost, Price, Internal note), an "Open promise" card on the pending tint ("$3,000.00 by Nov 1 · Kept · Reschedule · Broken"), MORE ▾ as a card listing Edit charge, Waive, Cancel in Stax, Void (red). One-off charge, invoice, payment, lease and calendar entry pages follow: tiles, cards per relation, Activity, a DETAILS card, the secondary actions in a MORE card.

### Create invoice (`create-invoice-laptop.png`) — the form and builder family *(drafted from the screenshot by Fable, 2026-10-23, for the owner's approval; the screenshot wins)*
Header: breadcrumb Stores › #1991 Istanbul › New invoice; title "New invoice" with the muted context "#1991 Istanbul · fatih · lease from Oct 13, 2026"; right-aligned muted "Draft · nothing reaches Stax until you choose how to collect". Body, two thirds: a Lines card (columns Item, Description, Qty, Unit amount, Total, a trash icon; the item as a select reading "Rent · November 2026", "Existing charge · Security deposit", "Sign fee catalogue"; a rent line shows its lease and month selects under it; an existing charge's qty and amount read-only) with a dashed "+ Add line" button and the help sentence; a "How to collect" card ("One choice; the invoice and its charges are written the same way whichever you pick.") with the choices as bordered tiles two a row, each a radio, a bold name and one muted sentence; the chosen tile tinted `--surface-2` with a teal border and its fields inside it (Record payment now: method select, date, reference). One third: SUMMARY (the lines with amounts, "Total", Memo input "Shown on the invoice", Due, the primary button worded by the choice, "Discard" secondary) and a help card ("The primary button changes with the choice above: …"). New store, New lease, Add payment with allocation, Edit operator and every form follow: the context line, one card per group with a 15/600 heading, the summary card with the primary action on a laptop, the sticky primary on a phone.

### Calendar (`calendar-laptop.png`) *(drafted from the screenshot by Fable, 2026-10-23, for the owner's approval; the screenshot wins)*
Header: breadcrumb "Reports ›", title "Calendar"; a segmented Month · Week · List and "Subscribe in Google Calendar" (secondary). Body, two thirds: the month card with "‹ November 2026 ›", "Today", Show (Everything) and Store (All) selects; the weekday header; the grid with the day number (today a teal circle) and coloured chips per kind (Rent day teal-green, ACH settles green, E-mail invoices neutral, Insurance yellow, Promise due orange, Installment teal, Lease ends red, Jobs grey) truncated with an ellipsis; a legend row of outlined squares with the kind names. One third: the selected day's panel ("SUNDAY, NOVEMBER 1": each entry with a coloured left bar, bold title, muted amount and dates, a link), NEXT 7 DAYS (date · item left, amount or count right), FEED ("The same events as an ICS feed for Google Calendar or Outlook. Copy link · Reset token"). Built per PLAN-012f-a D6: Month · List (no Week view exists); the feed actions are the existing feed page's.

### Login (`login-laptop.png`)
Two panels. Left: the sidebar gradient at 160°, two soft teal circles (12% and 10% alpha) as decoration, logo 52 px, a 34px/700 headline ("Rent collection for every store, in one place."), one muted sentence, three check-marked lines; company name at the bottom. Right: on `--bg`, a 400 px form card area — sandbox banner as a rounded strip, "Sign in" 26px/700, muted sub-line, E-mail and Password fields (12px 14px padding, radius 10px, autocomplete attributes), full-width primary Sign in button (13px padding, radius 10px), a muted "Access is by invitation" line. On phones the left panel collapses to a short gradient header with the logo and the headline; the form takes the full width. **Owner's decision of 2026-10-22 (PLAN-012f D13):** the first field is labelled "Username" (accounts are not e-mail addresses); "Forgot password?" and "Keep me signed in" are left out (no reset flow, no session change); the layout is otherwise as the screenshot shows.

## 5. Rules
- No gradients except the sidebar; no drop shadows; no emoji; icons are inline stroke SVG (Lucide-style, 16–18px).
- Colour is never the only signal (icon or word beside it); AA contrast on every pair above.
- Money through the existing `money` filter (`$2,000.00`).
- Dates on screen (owner, 2026-10-23, Step 12f-a): a day reads `Nov 1, 2026` (the year dropped only where the surrounding text names it, as in a calendar month), a month reads `Oct 2026`, a time `14:02`; never ISO on a page. ISO (`2026-11-01`) stays in CSV and spreadsheet exports, the API, audit rows, e-mails to systems, form field values and URLs/query strings.
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
