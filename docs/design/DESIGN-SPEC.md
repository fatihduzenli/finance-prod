# Monvera — design specification (owner-approved, 2026-10-22)

**Merge of 2026-10-24:** the owner's updated copy of this file (new §4 sections Profit and loss,
Daily charges, Help, States, Add payment, and §4a; the owner's own text for Stores list, Calendar,
Create invoice and Charge detail, which replaces Fable's drafts) was merged with the committed
version: the owner's text wins everywhere the two describe the same thing; the §1 Batch A note and
values, the Buttons bullet, the §5 date rule and the Login decision of 2026-10-22 (owner-approved
and built) were carried back in because the owner's copy predates them. Calendar reads Month / List
(§9 item 4).

**Change of 2026-10-22 (owner's decision, PLAN-012f Batch A):** four §1 values were nudged so
that every text pair meets WCAG AA, which §5 requires: `--muted` `#6B7A90` → `#5F6E85`, `--link`
`#0E8F86` → `#0B7C74`, `--paid-text` `#1B7F3B` → `#176B33`; the primary button is `#0E8F86` with a
1px `#0B7C74` border (`--btn-primary-bg`, `--btn-primary-border`) while `--teal` stays `#13A89E`
for the logo, the active tab and the bottom bar; `--faint` is decoration only, never for text
that must be read. White on the primary button measures 3.97:1, the one pair under AA, accepted
by the owner for the button's fill; the contrast test pins it. The values below are the ones built.

Reference screenshots in this folder are the source of truth for the look. Where this file and a screenshot differ, the screenshot wins.

- `dashboard-laptop.png`, `store-workspace-laptop.png`, `monthly-rent-overview-laptop.png` — laptop (1440)
- `operator-workspace-laptop.png`, `transfer-wizard-laptop.png`, `login-laptop.png`, `stores-list-laptop.png`, `calendar-laptop.png`, `create-invoice-laptop.png`, `charge-detail-laptop.png` — laptop (1440)
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

### Stores list (`stores-list-laptop.png`) — the list-page family (Operators, Leases, Invoices, Payments lists follow it)
Header: title with a muted count line ("517 stores · 486 with a lease · 31 vacant"), "Download spreadsheet" (secondary) and "+ New store" (primary). Filter card: search field (flex), selects (State, Status, Balance, Sort), a navy "Filter" button. Table card: header on `--surface-2` with the sort arrow on the sorted column; first cell "#number Name" bold link with the city below; Operator link; right-aligned Rent; Collected by; a **Balance badge** per row (✓ Paid up / ● unpaid amount / ● overdue amount dark red / ◑ partly paid / ○ Vacant n days); Status badge (Active green, Vacant grey, Closed red); a "›" link at the end. Footer: "1–7 of 7 · filtered from 517", Previous / page number pill / Next.

### Calendar (`calendar-laptop.png`)
Header: breadcrumb "Reports ›", title, a Month / List segmented control (no Week view in this step: owner's decision on PLAN-012f-a §9 item 4, 2026-10-24), "Subscribe in Google Calendar" (secondary). Main card: ‹ month › navigation with the month name centred, a "Today" button, Show and Store selects at the right; a 7-column grid (Sun–Sat header row), 104 px-high day cells, today's number in a teal circle, events as small rounded chips (11px/600) coloured by kind — rent day teal, ACH settles green, e-mail invoices grey, insurance yellow, promise due orange, installment teal, lease ends red, jobs grey; a legend row under the grid. Right column: the selected day's events (4 px coloured bar + title + muted detail with a link), "Next 7 days" list, and a Feed card (ICS link: Copy link · Reset token).

### Create invoice (`create-invoice-laptop.png`) — the form/builder family
Header: breadcrumb, "New invoice" with a muted "store · operator · lease" line, right-aligned "Draft · nothing reaches Stax until you choose how to collect". Main column: **Lines** card — one combo per line showing "Rent · November 2026", "Existing charge · Security deposit" or "Sign fee (catalogue)"; a Rent line reveals lease and month selects under the combo; an existing-charge line has read-only Qty and Unit amount (grey); Description, Qty, Unit amount inputs (right-aligned numbers), bold Total, trash icon; dashed "+ Add line" with one help sentence. **How to collect** card — six radio cards in two columns (Save draft, Send by e-mail, Charge now, Schedule for a date, Record payment now, and a full-width **Set up autopay**); the chosen card is tinted `#E0F4F2` with a teal border and may hold its own fields (Record payment now: method select, date, reference; Set up autopay: a Monthly / Weekly / Yearly segmented control, First debit date, Ends = After a number of payments · Never (until stopped) · On a date, with the count or date field beside it, and one teal sentence previewing the debits — "4 debits of $6,000.00 on the 1st: Nov 1, Dec 1, Jan 1, Feb 1"). Rent stays on the lease's own autopay; this option is for fees and plans. The resulting plan appears on the store's Autopay tab with Pause / Resume / Stop and an editable end rule. Right column, sticky: **Summary** card (lines, total 16px/700, memo input, due), the primary button whose label follows the choice ("Record $6,000.00 as paid", "Send", "Charge $6,000.00 now", "Schedule", "Save draft"), a secondary Discard. Other forms (New store, New lease, Add payment with allocation, Edit operator) use the same card, label, input and primary-button rules: one field per row on phones, help text as one muted line.

### Charge detail (`charge-detail-laptop.png`) — the detail-page family (Invoice and Payment pages follow it)
Header: breadcrumb (Stores › store › Charges › name), title 24px/700 with the status badge and a kind badge ("Deposit · held, refundable"), one muted line (store · operator · created … · due …); actions Add payment (primary), Split into installments, More ▾ (Edit charge, Waive, Cancel in Stax, Void in red). Main column: three tiles (Amount, Paid, Balance — balance red when owed); Invoices card (number bold with a muted line, sent, due, total, Status badge, In-Stax badge, "Attach Stax invoice" link); Payments card with an **empty state** (grey circle icon, "No payment yet.", a link to the action); Activity card as a timeline list (muted timestamp column, bold event, actor). Right column: Details definition list; a yellow "Open promise" callout with Kept / Reschedule / Broken links when a promise exists; nothing else.

### Login (`login-laptop.png`)
Two panels. Left: the sidebar gradient at 160°, two soft teal circles (12% and 10% alpha) as decoration, logo 52 px, a 34px/700 headline ("Rent collection for every store, in one place."), one muted sentence, three check-marked lines; company name at the bottom. Right: on `--bg`, a 400 px form card area — sandbox banner as a rounded strip, "Sign in" 26px/700, muted sub-line, E-mail and Password fields (12px 14px padding, radius 10px, autocomplete attributes), "Forgot password?" link in the password label row, "Keep me signed in" checkbox, full-width teal Sign in button (13px padding, radius 10px), a faint "Access is by invitation" line. On phones the left panel collapses to a short gradient header with the logo and the headline; the form takes the full width. **Owner's decision of 2026-10-22 (PLAN-012f D13):** the first field is labelled "Username" (accounts are not e-mail addresses); "Forgot password?" and "Keep me signed in" are left out (no reset flow, no session change); the layout is otherwise as the screenshot shows.

### Profit and loss (`profit-and-loss-laptop.png`) — the report family
Header: breadcrumb "Reports ›", title, Print (secondary) and "Download spreadsheet" (secondary). Filter card: From / To month selects, Group by (Store / Operator / State / Month), State, a "Show the rows" checkbox, a navy "Show" button. Four KPI cards: Billed (muted split "rent · fees"), Collected (green, "incl. withheld deposit" line), Costs ("landlord · fee costs"), Margin (collected) in green or red with an "n stores unknown · why" link. Table card: title "October 2026 · by store" with a muted count line and a right-aligned grey pill "Deposits held at Oct 31: $3,000.00"; two-row header (group headers Rent / Fees / Costs / Margin over Billed / Collected pairs, vertical hairlines between groups); first cell "#no Name" bold with the operator below; numbers right-aligned without the `$` (header says "amounts in US dollars"), negatives with a true minus in `#B42318`, unknown values as faint "unknown"; margin columns bold; a Flags column with grey pills ("Unknown: landlord rent", "Vacant"). Bold Total row on `--surface-2`; one muted footer sentence with a "How this report is built" link. A store whose landlord rent is not entered is never counted as zero: it shows "unknown" and a flag.

### Daily charges (`daily-charges-laptop.png`) — Admin › Daily charges
Header: breadcrumb "Admin ›", title with the date muted beside it, "Update from Stax" (secondary) and "Create today's charges" (primary). A green result strip ("✓ Run at 09:00 today. 37 charges created (3 catch-up), 2 leases skipped, 0 errors. Next automatic run tomorrow 09:00.") with a "Nightly check →" link; the strip turns yellow when something was skipped for a reason that needs a person, red when the run failed. Four KPI cards: Created today, Due today (amount, "n bank debits · n e-mail"), Skipped (yellow number, reasons), Large debits (red number, "above $2,000.00 · needs admin"). "Due today" card: segmented filter (All / Bank debit / E-mail / Catch-up), "Download spreadsheet" link; columns Store, Debit (day · catch-up), Collected by, Amount bold, Carried balance (red when owed), Status badge (Created teal, "Large debit · confirm" red, "Debit tonight" yellow, "Skipped · reason" grey), one action per row (Confirm button or "Open ›"). Two cards below: "Skipped today" (store · reason, one action link each: Debit again, Set up autopay) and "Recent runs" (date · time · automatic / by name, counts, OK / Failed badge).

### Help (`help-laptop.png`)
Header: title only. Left: a 240 px topic card — search field, then the topic list (Getting around, Stores and leases, Invoices and how to collect, Payments and autopay, Promises to pay, Lease ends and transfers, Needs review, Reports); the open topic uses the sidebar's active style on white (`#E0F4F2` tint, `--link` text, 600). Main: one article card — title 22px/700, muted "Updated … · n min read" line, body at 15px/1.65 with 18px `h3`s, lists with 8 px gaps, a tinted "Tip" box (`--surface-2`, 3 px teal left border); "Was this helpful? Yes · No" and "Still stuck? E-mail support" in the footer. Help pages are the only place for long explanations; the app itself keeps one muted line per block (§3).

### States (`states-laptop.png`) — empty lists, flash messages, errors
- **Empty state** inside a card: 48 px grey circle with a stroke icon, a 15px/600 line ("No disputes"), one muted sentence, one link.
- **Flash messages** at the top of the main column, dismissable (×), 10 px radius, icon + text: success `#E6F4EA`/`#176B33` ✓; warning `#FFF3C4`/`#6B4E00` !; error `#FDE8E6`/`#8A1C14` ●; info `#E8EEF6`/`#0B2545` i. Error text names what did not happen and what did ("Stax did not delete the invoice: Unavailable. Nothing changed here.").
- **Inline form error**: red 1px border on the field, 12px red line under it that says what to do ("Higher than the open balance ($2,100.00). Lower the amount or keep the rest as a credit.").
- **Confirmation dialog** (also the modal frame for New store / New operator, see §4a): 440 px, 14px radius, title 17px/600, one sentence, a secondary "Keep …" and a primary (red for destructive) button, right-aligned; backdrop `rgba(11,37,69,.45)`. On phones it slides up as a full-width sheet.
- **404 / 403** inside the shell: large faint code, a 20px/600 line, one sentence, "Go to the dashboard" primary + a search link.

### Add payment (`add-payment-laptop.png`) — one page for ACH debits and off-Stax payments
Header: breadcrumb (Stores › store › charge › Add payment), title with the charge's status badge beside it ("● Unpaid · $5,000.00 due"), one muted line (charge · store · operator · due). Main column: a **"How is this being paid?"** card with two radio cards (selected = `#E0F4F2` tint, 2 px teal border): (1) **Charge the operator's bank account (ACH via Stax)** — Bank account select showing bank + last 4 + holder ("on file"), Amount prefilled with the balance (max = balance), Debit on (today); its description says the charge shows Pending until the bank settles; when no bank account is on file this card is greyed with "No bank account on file — send <operator> the Stax link to add one" and the second card is selected. (2) **Record a payment received outside Stax** — Method (Zelle / Wire / Check / Cash), Amount, Received on, Reference (help: "Never an account number."). A separate Note card (textarea, optional). Right column, sticky: Summary tiles (Charge, Paid, Balance due red, This payment teal), an "After this payment → Paid in full / $x still owed" line, the primary button whose label follows the choice ("Charge $5,000.00 by ACH" / "Record $5,000.00 as paid"), a Cancel link; an Operator card (name · phone · Stax status · bank last 4 · returns this year). Large ACH debits (above the admin threshold) keep the existing confirmation. Phone: one column, Summary card above the button, full-width button.

### Split into installments (no reference image; built in 12f-c Batch C on the Add payment frame)
Header: full breadcrumb (Stores › store › charge › Split into installments; an operator's charge with no lease reads Operators › operator › charge), title "Split {charge} into installments" with the charge's status badge ("{Status} · {balance}"), one muted line "{charge} · #no Name · operator · {balance} left to pay". Main column: the **Plan** card — one short help line with a "Learn more: Payments" link; Number of installments as a − / + stepper around a number input (2–12; the buttons appear with the script, the input works without it); First installment date (a date input, the chosen day echoed beside the label as "Oct 26, 2026"); Cadence as the Monthly / Weekly / Yearly segmented control of Set up autopay (Monthly first); an inline info line "Collected by: {collection}"; with scripts off, a Preview button. The **Installments** card under it: a live table of number · date · amount that rebuilds as the fields change (the server renders the same table from the query string without the script), each row's date and amount editable within the engine's rules, the last row marked "Carries the rounding cent"; for autopay collection the large-debit tick appears only when a share is above the threshold. Right column, sticky: the **Summary** card — Balance to split and "{n} × {share}" tiles, "Last payment {date}", one line on the returned-payment fee rule, the primary "Create {n} installments" (disabled while the preview is empty), a secondary Cancel back to the charge. A charge that cannot be split shows the Plan card with the refusal sentence alone. Phone: one column, the Summary card above the primary, which sits above the tab bar.

## 4a. Additions from the owner's staging review (12f-b)
Not rendered as artboards; they follow the components in §3 and the pages above.

- **Dashboard · Cashflow card** (full width, below the KPI row): title "Cashflow", a date-range select at the right (This month / Last 3 months / Last 12 months / Custom From–To); a 12-bar monthly chart — Income (collected rent + fees, teal `#0E8F86`) and Expenses (landlord rent + other store costs + fee costs, muted `#8FA3BF`) as paired bars with a Net line in navy; three small totals under the title (Income, Expenses, Net in green/red). Deposits are not income (ADR-0012 §7). Inline SVG, no new chart library.
- **Dashboard · Payment status card** (one third width, same row as the mini calendar): a donut with the total in the centre, three boxes under it — Overdue (dark red `#8A1C14`), Paid (green `#176B33`), Upcoming (navy `#0B2545`) — each with count and amount, following the same date range as Cashflow.
- **Dashboard · Mini calendar card** (one third width): month name with ‹ › arrows, 7-column grid, today in a teal circle, a dot under a day that has anything (rent day, debit settles, promise due, installment, lease end, note); hovering (tapping on phones) opens a popover listing that day's items in the Calendar page's chip colours; the title links to Reports › Calendar.
- **Dashboard · "+ New invoice"** opens a small modal "Which store?" with a search field (type-ahead on number, name, operator) and a result list; choosing a store goes straight to that store's Create invoice page.
- **New store / Edit store / New operator / Edit operator as modals**: the confirmation-dialog frame at 640 px (full-screen sheet on phones), title + ×, fields in two columns (one on phones), Cancel (secondary) and Save (primary) right-aligned; on success the modal closes, the list behind it refreshes in place and a green flash confirms ("Store #5151 Rome added."). The operator form gets a **Type** control (Company / Person) that switches the name fields (Company name + Contact person · First name + Last name).
- **Store form fields**: Store number · Name · **Type** (optional select: Inline store, Kiosk, Cart, Pop-up, Warehouse — editable list in Admin › Settings; shown as a grey pill after the name everywhere the store is named, hidden when empty) · Street · City · **State as a select** (50 states + DC, two-letter code stored, full name shown) · ZIP · Open date · Closed date · Status · Store cost block (Landlord rent **optional**, Landlord lease end, Other monthly cost). A "Check address" link under the address fields runs the address check (USPS Addresses API) and offers the standardised address — "Use this / Keep mine" — filling City and State; when the service is unavailable the form still saves with an "Address not verified" note.
- **Create invoice · How to collect** (replaces the six cards in §4 Create invoice): **Save** · **Charge now** · **Schedule for a date** · **Record payment now** · **Set up autopay** · **Schedule settlement**. "Send by e-mail" is gone: after **Save** a dialog asks "Invoice saved. Send it to <operator> by e-mail now?" — **Send e-mail** / **Not now** (Not now keeps a draft, listed under Drafts on the store page). **Schedule settlement** splits the invoice total into equal debits: Number of payments (2–12), First debit date, cadence (Monthly / Weekly / Yearly, the autopay control), a preview sentence ("10 debits of $1,000.00 on the 1st: Nov 1, 2026 … Aug 1, 2027"), rounding differences on the last debit; it uses the existing installment-plan engine and shows on the charge as "Settlement plan · 3 of 10". When ACH is unavailable (no bank on file, or the processor has it off) the Charge now / Schedule / Settlement cards are greyed with one line saying why — never hidden.

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
