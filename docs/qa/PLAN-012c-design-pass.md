# PLAN-012c — The visual design pass on the 12d structure

**Date:** 2026-10-16 · **Origin:** the owner's Step 12c text of 2026-10-16 (items 1–9 and the
done-when list). **Lead/QA:** Fable · **Implementer:** Opus · **Ships:** one push after every batch
is gated and the owner's checkpoint; before Step 13. **Gate on the owner's side:** the copy list
(§4) is approved in writing before any copy changes; the other batches wait for that approval
too, as the owner wrote "report the copy list first for approval, then build".

## 1. Restated objective

No new feature. Every existing page gets one consistent look built from design tokens in one CSS
file (the brand colours navy `#0B2545`, teal `#13A89E`, green `#2EC4A6`, neutrals with AA
contrast, a system sans-serif stack), a shell with a navy top bar (the dark logo, the company
name from a new `company_settings` table, the product name, the 12d search box, the user menu, the
sandbox banner kept prominent) and a left navigation on desktop that collapses to the phone menu
of Prompt 11, a component set (page header, the 12d workspace as the reference layout, tables,
forms, badges, empty states, confirmations, flash messages, pagination, wizard steps), the
dashboard as cards with the attention list first, the grid with its fixed store column and
visible scrollbar, print stylesheets for the statement and the P&L, the PWA icon, splash and
favicon from `static/brand/` (committed in this step), accessibility with an AA-contrast test,
forms that show only what applies in the current state, a plain-language copy pass (approved
list), money always with a thousands separator, and screenshot baselines of ten pages at laptop
and phone widths compared in CI. Server-rendered templates stay; CSS and minimal JS only.

## 2. Affected packages

`backend/finance` (`templates/finance/base.html`, `static/finance/app.css` → `tokens.css` +
`app.css` + `print.css`, `static/finance/icons/`, the PWA manifest and service worker, context
processors, settings for the new app table), `backend/ledger` (every template under
`ledger/templates/ledger/`, the forms' labels and help texts, the services' fixed sentences that
reach the screen, `models.py` for `CompanySettings` (migration 0045), `admin.py`, the grid and
dashboard templates, the statement and P&L print views), `backend/tests` (contrast test, copy
test, the screenshot suite, the forms-visibility tests), `static/brand/` (committed), `.github/
workflows/ci.yml` (the screenshot comparison step: verification-only config, Fable's), `docs/`.
Out of scope: any new behaviour, URLs, audit action names, the public statement page's content,
e-mail templates (they keep their wording; the provider name substitution applies), the Django
admin's look.

## 3. Decisions

### 3.1 Tokens and shell (owner's 1, 2, 5)

- **D1 — tokens.** `finance/static/finance/tokens.css`: CSS custom properties on `:root` for
  colours (`--navy #0B2545`, `--teal #13A89E`, `--green #2EC4A6`, neutrals `--ink`, `--ink-2`,
  `--line`, `--paper`, `--paper-2` chosen for AA on their pairings, status colours `--status-paid`
  (green), `--status-unpaid` (navy outline), `--status-pending` (teal), `--status-partial`
  (amber), `--status-waived` (grey), `--status-due` (teal fill), `--danger`), spacing (`--s-1`
  … `--s-8` on a 4 px base), the type scale (`--t-xs` … `--t-2xl`, a system stack:
  `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`; no
  external font), radii, shadows, focus ring. `app.css` uses tokens only (no literal colour
  outside `tokens.css`; a test greps). The status tokens are the single source for the grid
  cells, badges, dashboard figures and report rows.
- **D2 — company settings.** New singleton model `CompanySettings` (table `company_settings`,
  one row enforced by a CHECK `id = 1`; migration 0045 seeds the row: name "Monvera", short name
  "Monvera", support e-mail blank, time zone `America/New_York`, logo override blank, provider
  display name "Stax"); fields `name`, `short_name`, `support_email`, `time_zone`, `logo` (a
  `documents_storage` path or blank; the brand logo when blank), `provider_name`. Edited in the
  Django admin (admin only; audited as other admin edits). A context processor `company` exposes
  `company.name`, `short_name`, `provider_name`, `logo_url` to every template; **every template
  or sentence that names the provider uses `company.provider_name`** (the fence rule stays: the
  code never names it; the display name comes from the row). The `[SANDBOX]` title prefix and the
  banner stay; page titles read "Monvera · <page>" (`short_name · <page>`).
- **D3 — the shell.** `base.html`: a navy top bar with the dark logo (`monvera_logo_dark.svg`),
  "Monvera" (`company.name`) beside the product name "Rent ledger", the 12d search box, a user
  menu (name, Settings, Sign out), the sandbox banner above the bar, unchanged in prominence; a
  left navigation on desktop (≥ 900 px) with Dashboard, Stores, Operators, Monthly rent overview
  (the grid), Reports (its index), Admin (the menu of 12d: Daily charges, Audit, Django admin,
  Settings), the current item marked (`aria-current`); below 900 px the nav collapses to the
  Prompt 11 phone menu (the `#nav-toggle` checkbox, no JS). Pages that today are nav items but
  not in this list (Arrears → under Reports as "Unpaid balances"; Calendar, Leases, Payments,
  Disputes, Returns, Webhooks, Reconciliation → under Reports or Admin as the copy list says)
  stay reachable; no URL changes.
- **D4 — PWA and favicon.** `static/brand/` committed; the manifest's icons from
  `monvera_icon.svg` / `monvera_icon_512.png` (plus a 192 px PNG derived by Opus with Pillow,
  committed), splash from the icon on the navy background, `<link rel="icon">` SVG with a PNG
  fallback, `apple-touch-icon`; theme colour navy.

### 3.2 Components (owner's 3)

- **D5.** One stylesheet section per component, each with a documented class: `.page-header`
  (title, one primary `.btn-primary`, secondary actions as `.btn`), the 12d workspace as the
  reference (`.workspace-header`, `.action-bar`, `.tabs`), `.table` (right-aligned numbers via
  `td.money`, zebra rows, sticky `thead`, sortable headers where the view already sorts, marked
  with `aria-sort`), forms (`.field` with label, help text, inline error under the control, one
  primary button per form), `.badge-<status>` (text plus a leading icon glyph, never colour
  alone), `.empty` (a sentence and the next step as a link or button), `.confirm` (a confirmation
  page: the question, what will happen, one primary and one secondary button), flash messages
  as `.toast` (role `status`, dismissable, auto-fade after 8 s with the message kept in the DOM
  for screen readers), `.pager`, `.steps` (the wizards' step list, clickable where the step is
  reachable, `aria-current="step"`). Every existing template adopts the classes; no markup
  rewrite beyond classes and wrappers.

### 3.3 Dashboard, grid, print (owner's 4)

- **D6.** The dashboard as cards: the attention list (11b) first, then Today's rent run, the
  figures, the recent items; the grid keeps the fixed store column and the visible mirrored
  scrollbar (11b) with the new tokens; `print.css` for the statement and the P&L: no nav, no
  bar, black on white, table borders, page breaks before each store group, the company name and
  the date in the header, "Printed <date>" in the footer.

### 3.4 Accessibility (owner's 6)

- **D7.** Focus visible on every control (the token ring), a label on every control (a test walks
  every form), status never colour-only (icon or text), AA contrast **checked by a test**: a unit
  test parses `tokens.css`, computes the WCAG contrast ratio of every documented foreground and
  background pairing (text on paper, text on navy, badge text on badge fill, link on paper) and
  asserts ≥ 4.5 (≥ 3 for large text and UI borders), failing with the pair named.

### 3.5 Forms show only what applies (owner's 8)

- **D8.** The rule: a field or section that cannot be used in the current state is not rendered.
  The walk (Opus lists every change in the report; Fable verifies): lease-end settlement (refund
  method, date and reference only when the refund due is above zero; when the operator owes, the
  amount owed and "Save and continue"), the deductions table (no rows → "Add deduction" only),
  the lease-end terms (reason note only when reason is "other"), the transfer's incoming step
  (the deposit field only when a deposit type exists; the checklist only when items are flagged),
  the override form only when debt is above zero (already), the lease form (autopay controls
  only on a provider-ACH lease; the second debit day field only when twice-monthly is chosen),
  the operator form (provider fields only for operators with a provider customer; consents only
  with a phone or e-mail), the store page (lease-dependent actions hidden on a vacant store,
  already), the builder (rent fields only on a Rent line, already; the Save-as-catalogue tick
  only on a typed item, already), Record a payment (the credit inputs only when a credit is held,
  already; the keep-credit tick only when something is unallocated), the deposit resolution
  (the transfer-to-lease field only when another lease exists), the return queue (reschedule
  controls only on a returned debit), the installment pages (Retry only on a failed release),
  the document upload (the kind list limited to the owner's kinds). Each hidden field keeps its
  server-side rule; hiding never relaxes validation.

### 3.6 Copy (owner's 9)

- **D9.** The copy list in §4 is the contract: every button, menu item, page title, status
  label and help text as before → after; audit action names, URLs, CSV headers and the public
  statement's wording unchanged (a test asserts the audit actions and route names of today's
  registry). No technical term on screen: provider → `company.provider_name` ("Stax") where a
  name is needed, else dropped; sync → "updated from Stax"; worker/job/queue → "in progress" or
  dropped; schedule state → "autopay on/paused/stopped"; needs review → "check this"; flag →
  "marked"; legacy → "from the spreadsheet". Money always `$2,000.00` (one `money` template
  filter and one `format_amount` used everywhere, incl. promise cells and the "Closed … as …"
  sentence; a test greps templates for bare `${{` and services for Decimal f-strings without
  the formatter).

### 3.7 Visual regression (owner's 7)

- **D10.** A Playwright suite `tests/visual/` (marker `visual`), run after e2e: signs in, seeds
  one fixed data set (a deterministic fixture: two stores, one operator, charges, a draft, a plan,
  a lease end at the statement step), takes full-page screenshots of the ten pages (dashboard,
  stores list, the store workspace on each tab, the operator workspace, the grid, the invoice
  builder, Record a payment, the lease-end wizard at the statement step, the P&L, Daily charges)
  at 1280 × 800 and 390 × 844, with animations disabled and the clock frozen, and compares them
  pixel-wise to the committed baselines under `tests/visual/baseline/` with a 0.5 % tolerance;
  a difference fails the test and writes the diff images to `tests/visual/out/` (ignored). The
  baseline is regenerated by `pytest -m visual --update-baseline` and committed with the change
  that moved the pixels, reviewed in the PR. CI: a "Visual regression" step after e2e (Fable).

## 4. Copy list (before → after)

The inventory behind this list is `docs/qa/COPY-INVENTORY-012c.md` (646 lines: navigation, every
page title, every button, every status label, the sentences with a technical term, the money
spots, the empty states, the attention texts). **Nothing below changes an audit action name, a
URL, a CSV header or the public statement's wording.** "Stax" stands for the provider display
name from company settings (`company.provider_name`), so it follows a change of processor.
Rules first, then the lists; anything not listed keeps its text.

### 4.1 Rules applied everywhere

| Rule | Before | After |
|---|---|---|
| The processor is named, not described | "the payment provider", "the provider", "provider" | "Stax" (from company settings) |
| No machinery words | "the worker", "queued", "job", "request waiting" | dropped, or "in progress", "will be sent", "waiting" |
| Autopay, not schedules | "schedule", "provider schedule", "schedule state" (the Stax scheduled invoice) | "autopay" ("Autopay will collect this charge") |
| Plain verbs | "tick the box", "tick the confirmation" | "check the box" |
| Review wording | "flagged for review", "Needs review" | "marked Check this", badge **Check this** |
| Marking | "marked paid", "mark" | "marked paid in Stax" |
| Linking the processor | "sync", "synced", "link provider IDs", "linked" | "update from Stax", "connected to Stax" |
| Nightly job | "reconciliation", "reconcile" | "nightly check" |
| Inbound events | "webhook", "webhook event", "dead-letter", "payload" | "message from Stax", "given up", "content" |
| Old data | "legacy", "Sheet" | "from the spreadsheet" |
| Time | "… UTC" | New York time, no suffix (the app's time zone) |
| Codes never shown | "TODO-VERIFY-036", "DTSTART", raw field names (`other_cost_description`), ALL-CAPS Stax statuses in sentences | removed or spelt out ("First debit"; "Other cost description"; "not yet paid") |
| Money | `$2000.00`, `2000.00`, `paid 2,000.00` | `$2,000.00` everywhere (one `money` filter and `format_amount`), the grid cells too ("Paid $2,000.00") |
| Button names | mixed ("Record a payment" / "Record payment") | one name per action, the list below |
| Spelling | "email" and "e-mail" mixed | "e-mail" |

### 4.2 Navigation and shell

| Before | After | Where it lives |
|---|---|---|
| Grid | **Monthly rent overview** | left nav |
| Dashboard | Dashboard | left nav |
| Stores | Stores | left nav |
| Operators | Operators | left nav |
| Reports | Reports (menu) | left nav |
| Arrears | **Unpaid balances** | Reports menu |
| Profit and loss | Profit and loss | Reports menu |
| Payments | Payments | Reports menu |
| Returns | Returned payments | Reports menu |
| Calendar | Calendar | Reports menu |
| Leases | Leases | Reports menu |
| Disputes | Disputes | Reports menu |
| Admin | Admin (menu) | left nav (billing and admin) |
| Rent run | **Daily charges** | Admin menu |
| Audit | Activity log | Admin menu (admin) |
| Webhooks | Messages from Stax | Admin menu (admin) |
| Reconciliation | Nightly check | Admin menu (admin) |
| Settings | Settings | user menu |
| Django admin | System settings | Admin menu (admin) |
| Sign out | Sign out | user menu |
| Menu (phone toggle) | Menu | phone |
| Store, city or operator (search placeholder) | Store, city or operator | top bar |
| `<page> \| Finance` (tab title) | **Monvera · <page>** | every page |
| SANDBOX — NO REAL MONEY | SANDBOX — test system, no real money (the word SANDBOX stays: docs/CLAUDE.md Rule 6) | banner |
| Finance (SANDBOX) (sign-in) | Monvera (SANDBOX) | sign-in |

### 4.3 Page titles (the `<h1>`; the tab title follows "Monvera · …")

| Before | After |
|---|---|
| Record a payment (charge, one-off, store) | **Add payment** |
| Resolve deposit {store} / Resolve {fee type} | **Return deposit** |
| Link an invoice | **Attach Stax invoice** |
| Autopay for {store} / Enable autopay | **Set up autopay** |
| Stop autopay: lease {store} | Turn off autopay |
| Link {operator} / Link payment provider IDs | **Connect to Stax** |
| Rent run {date} | **Daily charges, {date}** |
| Arrears | Unpaid balances |
| Grid | Monthly rent overview |
| A/R aging | Unpaid by age |
| Deposits versus transactions | Bank deposits |
| Reconciliation | Nightly check |
| Webhook events / Webhook event: {type} | Messages from Stax / Message from Stax: {type in words} |
| Audit log | Activity log |
| Schedule a payment: store {store} | Schedule a payment (unchanged: plain English) |
| End lease {store}: live items | End lease {store}: still open at Stax |
| Transfer store {store}: live items and balances | Transfer store {store}: still open at Stax, and balances |
| Charge {store} {YYYY-MM} / Rent {YYYY-MM}, debit {n} [Needs review] | Rent {Month YYYY}, debit {n} [Check this] |
| Lease {store} [Needs review] | Lease {store} [Check this] |
| {charge label} {store} [Voided] [Deposit] [Legacy] [Needs review] | … [Voided] [Deposit] [From the spreadsheet] [Check this] |
| Store {store} [Needs review] | Store {store} [Check this] |
| Payment / Refund of ${amount} | Payment of $2,000.00 / Refund of $2,000.00 |
| Cancel {number} (a never-sent draft) | Delete draft {number} |
| Create one-off charge (the 410 page) | This form has moved (the invoice builder) |
| Statement: {operator} | Statement: {operator} |
| Store page tab "Schedules" | **Autopay** |
| Store page tab "History" | Activity |
| Filters Open / Paid / All, Unpaid / All | unchanged |

### 4.4 Buttons and action links

| Before | After |
|---|---|
| Record a payment / Record payment | **Add payment** |
| Resolve deposit | **Return deposit** |
| Link invoice | **Attach Stax invoice** |
| Link / Link provider IDs | **Connect to Stax** |
| Enable autopay | **Set up autopay** |
| Pause autopay / Resume autopay | Pause autopay / Resume autopay |
| Stop autopay | Turn off autopay |
| Match invoices | **Update from Stax** |
| Delete provider invoice | **Cancel in Stax** |
| Retry sync | Try again |
| Retry marking the invoice paid | Mark paid in Stax again |
| Retry | Try again |
| Export CSV / Export rent charges CSV / Export one-off charges CSV | Download spreadsheet / Download rent charges / Download one-off charges |
| Refund at the payment provider / Void at the payment provider | Refund through Stax / Void through Stax |
| Replay | Process again |
| Run now | Run the nightly check now |
| Resolve with a note / Resolve with note | Close with a note |
| Reschedule (a returned payment) | Debit again |
| Charge ${amount} now / Send ${total} by e-mail | Charge $2,000.00 now / Send $2,000.00 by e-mail |
| Refresh methods | Update bank methods from Stax |
| Refresh (disputes) | Update from Stax |
| Store and send (dispute evidence) | Save and send to Stax |
| Create today's charges | Create today's charges |
| Create missing charges | Create missing charges |
| Schedule payment | Schedule the payment |
| Allow the balance to remain | Allow the balance to remain |
| Split into installments / Create the plan / Move the unpaid installments / Cancel the plan | unchanged |
| Add entry (contact log, everywhere) | Add entry |
| Add setup charge / Add deduction / Add line / Add note or reminder | unchanged |
| New invoice / New lease / New operator / New store | unchanged |
| Set landlord rent / Correct | Set landlord rent / Correct |
| Open in the audit log | Open in the activity log |
| Open in the payments ledger | Open in Payments |
| Revoke every statement link sent | Cancel every statement link sent |
| Send again / Send by e-mail / Preview / Print / Download / Upload / Edit / Delete / Save / Save and continue / Cancel / Close | unchanged |
| Transfer the store / End the lease / Continue the lease end / Cancel the lease end / Cancel the transfer | unchanged |
| Action bar: Create invoice, Record a payment, Charge now, Add setup charge, Schedule a payment, Create missing charges, Upload document, Add contact entry, New lease, Edit lease, Transfer to another operator, End lease | Create invoice, **Add payment**, Charge now, Add setup charge, Schedule a payment, Create missing charges, Upload document, **Add entry**, New lease, Edit lease, Transfer to another operator, End lease |

### 4.5 Status labels and badges (stored values unchanged)

| Set | Before | After |
|---|---|---|
| Charge statuses | Due, Pending, Paid, Partially paid, Unpaid, Waived; Owed, Voided | unchanged (Owed stays) |
| Grid filter and cells | paid / unpaid / pending / partial / due / waived / not due; "paid 2,000.00" | Paid / Unpaid / Pending / Partly paid / Due / Waived / Not yet due; "Paid $2,000.00" |
| Autopay (ScheduleState) | Not created, Active, Paused, Failed, Ended | Not set up, On, Paused, Failed, Off |
| Stax link (SyncStatus) | Not applicable, Awaiting link, Pending, Linked, Failed | Not needed, Waiting to connect, In progress, Connected, Failed |
| Stax link failures (SyncFailure) | Provider error; Refused before sending; Not permitted from this process; Feature disabled; Calls paused after repeated failures; Failure budget exhausted; Rate limit wait exceeded; Rejected by the provider; Provider unavailable; Provider unreachable; Wrong merchant; Linked to another operator | Stax error; Not sent; Not allowed here; Turned off; Paused after repeated errors; Too many errors today; Stax is busy, try later; Rejected by Stax; Stax unavailable; Cannot reach Stax; Wrong Stax account; Connected to another operator |
| Payment method | ACH | Bank debit (ACH) |
| Payment channel | ACH debit through the payment provider | Bank debit through Stax |
| One-off category | NSF fee | Returned-payment fee |
| Installment collection | Send by email | Send by e-mail |
| Requests in progress (JobKind, the banner) | Operator sync, Mark the provider invoice paid, Create the provider invoice, Create the provider schedule, Replay webhook event, Run reconciliation, … | Updating the operator in Stax, Marking the invoice paid in Stax, Creating the invoice in Stax, Setting up autopay in Stax, Processing a message from Stax again, Running the nightly check, … (every kind in plain words; list in the inventory) |
| Request states (JobStatus) | Queued, Running, Done, Failed, Dead | Waiting, In progress, Done, Failed, Gave up |
| Messages from Stax (WebhookOutcome) | Received, Processed, Ignored, Failed, Dead letter | Received, Processed, Ignored, Failed, Gave up |
| Nightly check findings (MismatchKind) | Invoice status differed; Payment restored; Deposit row unmatched; Settled payment not deposited; Return missed by webhook; Pending too long | Invoice status differed; Payment restored; Bank deposit not matched; Settled payment not deposited; Return not received; Pending too long |
| E-mail status (MessageStatus) | Queued, Sent to the catch-all address | Waiting, Sent to the test inbox |
| Evidence status | Queued | Waiting |
| Rent source | Sheet, current rent; Sheet, inferred; Staff | From the spreadsheet; From the spreadsheet (inferred); Entered by staff |
| Badges | Needs review; Legacy | Check this; From the spreadsheet |
| Attention labels | schedules to review; dead-letter webhooks; reconciliation mismatches | autopay to check; messages from Stax given up; nightly check findings |

### 4.6 Sentences with a technical term (the 123 of the inventory, grouped; the rest unchanged)

| Where | Before → After |
|---|---|
| installments PLAN_HELP | "…until it is resolved from the returns queue." → "…until it is dealt with on the Returned payments page." |
| installments RESCHEDULE_HELP | "Autopay schedules and e-mailed invoices at the payment provider are updated by the worker." → "Autopay and e-mailed invoices are updated in Stax automatically." |
| installments CANCEL_HELP | "(their invoices or schedules are deleted at the payment provider by the worker)" → "(their invoices and autopay are cancelled in Stax)" |
| installments LIVE_SCHEDULE | "A payment provider schedule will collect this charge: …" → "Autopay will collect this charge: …" |
| installments SCHEDULE_FIRED | "The schedule already generated its invoice; …" → "Autopay already made this month's invoice; …" |
| installments RELEASE_NOTICE_* | "Invoice X at the payment provider will be deleted (the charge stays open here)…" → "Invoice X will be cancelled in Stax (the charge stays open here)…"; "The payment provider schedule of this charge…" → "The autopay of this charge…"; "its payment provider schedule is deleted and created again from next month's debit" → "its autopay is moved to next month's debit" |
| installments RELEASE_PENDING_* | "Waiting for the payment provider to release…" → "Waiting for Stax to cancel invoice X / to move the autopay" |
| installments RELEASED* | "Released at the payment provider; N installment requests queued." → "Cancelled in Stax; the N installments are being set up."; "…nothing more was queued" → "…nothing more to do"; "…no installment was queued" → "…no installment was set up" |
| installments REQUEST_WAITING, SHIFT_BUSY | "A payment provider request of this charge is waiting: try again when it is done." → "A change is still in progress in Stax: try again in a minute." |
| installments NO_CUSTOMER | "The operator has no payment provider customer: …" → "The operator is not connected to Stax: …" |
| installments CANCELLED | "…are cancelled at the payment provider by the worker; …" → "…are cancelled in Stax; …" |
| lease_end / transfer / refunds / collections / disputes CONFIRM_REQUIRED, NOTICE_REQUIRED, HAND_NEEDS_CONFIRM, LINK_CONFIRM | "Tick the box / Tick the confirmation" → "Check the box" |
| lease_end DOCUMENTS_DONE | "…the e-mail to the operator is queued." → "…the e-mail to the operator will be sent." |
| transfer SETUP_TWICE | "{name} is ticked twice." → "{name} appears twice." |
| payments_entry SCHEDULE_NOTE | unchanged (an internal note: "Scheduled from the store page") |
| invoice_builder LINE_EXISTING_NOT_OPEN | "(paid, pending, billed, scheduled or on an installment plan)" → "(paid, pending, invoiced, on autopay or on an installment plan)" |
| invoice_builder NO_PROVIDER_CUSTOMER | "The operator has no payment provider customer: …" → "The operator is not connected to Stax: …" |
| invoice_builder SENT, CHARGE_REQUESTED | "The payment provider e-mails it once it has the invoice; …" → "Stax e-mails it shortly; …"; "The single debit is made once the payment provider has the invoice" → "The debit is made shortly" |
| one_off_charges EDIT_ONE_OFF | "…its invoice is at the payment provider." → "…its invoice is already in Stax." |
| one_off_charges WAIVE_LIVE_INVOICE, CANCEL_MEANING, WAIVE_MEANING, WAIVED_LIVE_HEADING, DELETE_INVOICE_* | "Stax" stays (it is the name); "live" → "still open in Stax"; "Delete requested. The invoice is marked void once the payment provider confirms the delete" → "Cancelling in Stax. The invoice is void once Stax confirms"; "The provider invoice was deleted." → "The invoice was cancelled in Stax." |
| one_off_charges TOTAL_MISMATCH, SEVERAL_PROVIDER_INVOICES, OTHER_OPERATOR_REFERENCE, SECOND_PROVIDER_INVOICE | "at the payment provider" → "in Stax"; "the payment provider's dashboard" → "the Stax dashboard" |
| one_off_charges NO_PROVIDER_CUSTOMER, CREATED_MANUAL_LOCAL | "…no payment provider customer…" → "…not connected to Stax…" |
| one_off_charges CREATED_NO_INVOICE | "The payment provider schedules the invoice for {date}." → "Stax will debit it on {date}." |
| one_off_charges CANCEL_BUSY, CHARGE_NOW_BUSY, views/jobs BUSY | "A request to the payment provider is already waiting for this charge/record." → "A change is still in progress in Stax for this charge: try again in a minute." |
| one_off_charges CANCEL_REQUESTED | "…once the payment provider confirms the delete." → "…once Stax confirms." |
| one_off_charges CHARGE_NOW_NOT_LINKED | "The charge's invoice is not at the payment provider yet. Charge now is possible once it is linked." → "The invoice is not in Stax yet. Charge now works once it is." |
| one_off_charges INVOICE_RELEASED | "…was released at the payment provider for the installment plan." → "…was cancelled in Stax for the installment plan." |
| rent_run MATCH_OTHER_SCHEDULE, MATCH_FAILED | "…does not name this schedule as its source" → "…does not come from this autopay"; "The payment provider could not list the invoices of a schedule. Matching stopped." → "Stax did not answer. The update stopped." |
| rent_run MATCH_CHARGE_INVOICED, LINK_DONE_DIFFERS | "flagged for review" → "marked Check this" |
| rent_run MATCH_MISMATCH, TOTAL_MISMATCH, SECOND_INVOICE | "at the payment provider" → "in Stax" |
| rent_run MARK_DONE | "The invoice at the payment provider is marked paid." → "The invoice is marked paid in Stax." |
| leases NOT_PROVIDER_CHANNEL | "…is not ACH debit through the payment provider." → "…is not bank debit through Stax." |
| leases DAY_NOT_SCHEDULABLE | "Debit days 29 to 31 cannot be scheduled yet (TODO-VERIFY-036). Choose a day from 1 to 28." → "Autopay cannot use the 29th, 30th or 31st. Choose a day from 1 to 28." |
| leases NO_PROVIDER_CUSTOMER | "Operator has no payment provider customer." → "The operator is not connected to Stax." |
| leases SCHEDULE_DIFFERS, PAUSE_FIRST, RESUME_REFUSED | "Schedule amount differs from rent; pause and re-enable autopay." → "The autopay amount differs from the rent: pause autopay and set it up again."; "A schedule no longer matches the lease…" → "The autopay no longer matches the lease…" |
| leases NOTHING_TO_PAUSE/RESUME/STOP/MOVE | "No active schedule to pause." → "Autopay is not on."; "No paused schedule to resume." → "Autopay is not paused."; "No active or paused schedule to stop." → "Autopay is already off."; "…nothing was changed at the payment provider." → "…nothing was changed in Stax." |
| leases AUTOPAY_STOPPED | "Autopay stopped: the provider schedules are deleted." → "Autopay is off: it was cancelled in Stax." |
| leases METHOD_NOT_FOUND, METHOD_DELETED, SEVERAL_MATCHES, PROVIDER_REJECTED, PROVIDER_FAILED, LIMIT_EXCEEDED | "at the payment provider" → "in Stax"; "the provider's dashboard" → "the Stax dashboard"; "The payment provider refused the request." → "Stax refused the request."; "The payment provider could not be reached or did not answer." → "Stax could not be reached."; "the confirmed provider limit" → "the Stax debit limit" |
| refunds NOT_ACH, NO_REFERENCE, REFUNDED, REFUND_PENDING, VOIDED, CALL_FAILED, OUTCOME_UNKNOWN, REFETCH_FAILED, REFUND_SENTENCE, VOID_SENTENCE | "Only an ACH payment made through the payment provider…" → "Only a bank debit made through Stax…"; "…never a provider transaction" → "…never went through Stax"; "The payment has no payment provider transaction on record." → "This payment is not in Stax."; "Refund of ${amount} made at the payment provider" → "Refund of $2,000.00 made through Stax"; "until the provider settles it" → "until Stax settles it"; "The payment provider did not {verb} the payment ({failure}{fields})" → "Stax did not {verb} the payment: {failure in words}"; "The provider could not be reached, so…" → "Stax could not be reached, so…"; "The payment provider could not be read back afterwards ({failure})" → "Stax could not be read back afterwards"; "…sent to the payment provider at once…through the payment provider…" → "…sent to Stax at once…through Stax…" |
| collections HAND_QUEUED | "The e-mail is queued; …" → "The e-mail will be sent; …" |
| disputes REFRESH_QUEUED, BUSY, EVIDENCE_QUEUED, PAST_RESPOND_BY, NO_PROVIDER_DISPUTE, SENT, REFUSED, SYNC_MORE | "Refresh queued. The list updates when the payment provider has answered." → "Updating from Stax; the list refreshes when it answers."; "A refresh is already waiting for the payment provider." → "An update from Stax is already in progress."; "It is sent to the payment provider in the background." → "It is being sent to Stax."; "the payment provider no longer takes evidence" → "Stax no longer takes evidence"; "The dispute has no payment provider reference." → "This dispute is not in Stax."; "Evidence sent to the payment provider." → "Evidence sent to Stax."; "The payment provider refused the evidence ({error})." → "Stax refused the evidence: {error in words}."; "More pages than read in one sync; the rest is read next time." → "More disputes than fit in one update; the rest come next time." |
| insurance NOT_MARKED, offline_marking (shown) | "Provider invoice not updated: …" → "Not marked paid in Stax: …"; "partial amount" → "part of the amount"; "the payment differs from the invoice total"; "already showed paid before marking; not marked again. Check for a double payment" → "already paid in Stax; not marked again. Check for a double payment"; "not re-read" → "not confirmed" |
| nsf NO_PROVIDER_CUSTOMER | "The operator is not linked to the payment provider: …" → "The operator is not connected to Stax: …" |
| reports CARDS_NOTE | "A manual payment marked at the payment provider as cash or check…" → "A payment recorded by hand and marked in Stax as cash or check…" |
| pnl UNREADABLE, views/invoices BAD_MONTH | "(use YYYY-MM)" → "(choose a month)"; "The month must be YYYY-MM." → "Choose a month." |
| store_costs NOT_CSV, ROW_DATE, ROW_TOO_LONG | "The file is not a CSV in UTF-8 text: save it as "CSV UTF-8" or as .xlsx." → "The file could not be read: save it as CSV UTF-8 or as .xlsx."; dates "(MM/DD/YYYY or YYYY-MM-DD)" → "(month/day/year)"; raw column names → "Other cost description", "Notes" |
| views/reconciliation RUN_QUEUED, views/run MATCH_QUEUED | "Reconciliation queued. Its state appears below; …" → "The nightly check is starting; its progress shows below; …"; "Matching queued. The report appears below when it is done." → "Updating from Stax; the report appears below when it is done." |
| Templates (§5c of the inventory) | `_job_status.html` "Payment provider request ({kind}, {time} UTC)… This page refreshes every 5 seconds." → "Stax request in progress: {kind in words}, started {time}. This page refreshes every 5 seconds."; dashboard "An invoice whose last provider status is ATTEMPTED is listed here." → "Invoices Stax could not collect are listed here."; "Last reconciliation / No reconciliation has run yet." → "Last nightly check / No nightly check has run yet."; disputes "Last synced … UTC. Not synced yet." → "Last updated from Stax … / Not updated yet."; lease and store "Provider schedule / Autopay schedules / …the provider runs the schedule." → "Autopay in Stax / Autopay / …Stax runs the autopay."; enrol "One payment provider schedule is created per debit day." → "One autopay is set up in Stax per debit day."; "First debit (DTSTART)" → "First debit"; "Methods as of … UTC" → "Bank methods as of …"; lease form "…keeps its schedules; a … flagged …" → "…keeps its autopay; … marked Check this"; stop autopay "the provider schedule is deleted; re-enrol to start again." → "autopay is cancelled in Stax; set it up again to restart."; one-off cancel "The payment provider's invoice (or … its schedule) is deleted; the provider then shows the invoice as deleted, as it has no "void"." → "The invoice (or its autopay) is cancelled in Stax, which shows it as deleted."; link-invoice pages "…payment provider's dashboard … flagged … tick" → "…the Stax dashboard … marked Check this … check the box"; payment forms "…outside the payment provider … marked …" → "…outside Stax … marked paid in Stax …"; confirm pages "tick the box" → "check the box", "Nothing is sent to the payment provider." → "Nothing is sent to Stax."; blockers "Nothing is live at the payment provider…" → "Nothing is still open in Stax…"; operator page "Retry sync / Provider customer updated … UTC / SMS phone / SMS consent given on" → "Try again / Updated from Stax … / Text-message phone / Text-message consent given on"; reconciliation page → nightly check wording throughout, "flags" → "marks", "job" → dropped; deposits report "Open findings of the nightly reconciliation's deposit step" → "Open findings of the nightly check's bank-deposit step"; P&L column "Flags" → "Notes"; run page "webhook event… dead-lettered" → "a message from Stax was given up", "The payment provider's schedules generate the invoices" → "Stax's autopay makes the invoices", "reads the invoices of the provider's schedules only" → "reads Stax's autopay invoices only"; history tab "…schedules…" → "…autopay…"; webhooks detail "Payload (redacted)" → "Content (without account data)", "the body was not JSON…" → "the message could not be read…"; audit page "Time (UTC)" → "Time" |
| Forms (§5d) | "Customer ID / Payment method ID" → "Stax customer ID / Stax bank method ID" (needed to connect; the words stay plain); payment reference help "…are sent with the invoice when it is marked paid." → "…are sent to Stax when the invoice is marked paid."; "Refund method … never the payment provider." → "… never through Stax."; everything else unchanged |
| Documents notice | "Those stay in Stax or DocuSign." → unchanged (the names are right) |

### 4.7 Empty states (the next step added where one exists)

"No invoices yet." → "No invoices yet. Create one from the action bar."; "No payments." → "No payments yet. Add one from the action bar."; "No documents." → "No documents yet. Upload one below."; "No contact entries." → "No entries yet. Add one."; "No draft." → "No draft."; "No leases." → "No lease yet. New lease from the action bar."; "No reconciliation has run yet." → "No nightly check has run yet."; "No webhook events match." → "No messages from Stax match."; "Nothing is live at the payment provider…" → "Nothing is still open in Stax…"; "None. An invoice whose last provider status is ATTEMPTED…" → "None. Invoices Stax could not collect are listed here."; the rest unchanged.

### 4.8 Money (one filter, everywhere)

A template filter `money` (`$2,000.00`) replaces every bare `${{ x }}` in the 45 templates listed in
inventory §6 (incl. the promise cells, the store header, the payment page's title and the charge
buttons), and `format_amount` replaces the raw Decimals in `contacts.py` ("Closed $2,000.00 by
2026-10-01 as Broken"), `workspace.py` (the deposit resolution text), `deposits.py`, `refunds.py`
(the refund and void sentences) and `invoice_builder.py` (the line text); the grid cells read
"Paid $2,000.00"; a test greps the templates and the services.

## 5. Work breakdown (batches, each gated; all after the owner approves §4)

| Batch | Decisions | Done when |
|---|---|---|
| A | D1, D2, D3, D4 (tokens, company settings + migration 0045, the shell, the brand files, PWA, titles) | every page renders in the new shell with tokens; the logo and company name show; the phone menu works; the manifest and favicon point at the brand files |
| B | D5, D6 (components, dashboard cards, grid polish, print) | every template uses the component classes; the statement and P&L print clean |
| C | D8 (forms show only what applies) | the listed forms hide what does not apply; validation unchanged; the report lists every change |
| D | D9 (copy and money) | every string on the approved list changed; no technical term on screen (test); money formatted everywhere (test) |
| E | D7, D10 (contrast test, labels test, the visual suite and baselines, the CI step) | the tests pass; the baselines committed; CI compares |

## 6. Test plan (before implementation)

A: the shell on every page (a test renders the main pages and asserts the bar, the logo, the
company name, the nav items and `aria-current`); the phone menu toggle present; the manifest
and icons answer 200 with the right types; titles "Monvera · <page>"; `company_settings` seeded,
singleton CHECK, admin-editable, the provider name substituted on a page that names it. B: the
component classes present on the main pages; `td.money` right-aligned (CSS test); sortable
headers carry `aria-sort`; empty states carry a next step; print.css linked on the statement and
P&L; the dashboard's attention card first. C: each hidden field absent in the state that hides it
and present otherwise; posting a hidden field is ignored or refused as today (validation
unchanged). D: a test walks the approved list (old text absent, new text present per template);
no technical term in rendered pages (a grep test over rendered HTML of the main pages for the
forbidden words); money formatted (template and service greps; the promise cell and the
"Closed … as …" sentence rendered). E: the contrast test over every pairing; a label on every
control (a test walks every form class and every rendered `<input>`); the visual suite green
against its baselines at both widths.
**Adversarial (Fable):** a company name or provider name with markup is escaped in the bar, the
titles and the sentences; the logo override cannot point outside the documents storage; the
phone menu is usable without JavaScript; the toast never hides a refusal before it is read (the
message stays in the DOM); a hidden field posted with a hostile value is refused or ignored
exactly as before; the visual suite fails on a one-pixel change of a baseline (a deliberate
mutation) and passes unchanged; the contrast test fails when a token is darkened below 4.5.

## 7. Risk register

- Reach: every template changes; the e2e and visual suites are the guard; no URL or action name
  changes.
- `company_settings` is a new singleton; the seed row and the admin edit are audited.
- The copy change touches strings tests assert on; the approved list drives both the templates
  and the tests in one batch.
- Screenshots are platform-sensitive: the baselines are taken in CI's Chromium and compared with
  a tolerance; a local run documents the Chromium version.

## 8. Acceptance criteria

On staging, on laptop and phone: every page uses the new shell and tokens; the logo and company
name show in the header; the grid and dashboard look finished; printing a statement produces a
clean page; the installed phone app shows the new icon; no technical term on screen; money
formatted everywhere; the visual baselines committed and compared in CI.
