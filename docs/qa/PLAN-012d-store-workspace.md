# PLAN-012d — The store page as the single workspace

**Date:** 2026-10-15 · **Origin:** the owner's Step 12d text of 2026-10-15 (items 1–10, 8a, 8b, 9a) and
finding 12d-1 (drafts unreachable). **Lead/QA:** Fable · **Implementer:** Opus · **Ships:** one push
after every batch is gated and the owner's checkpoint; **before 12c** (the design pass styles the
final structure).

## 1. Restated objective

Staff worked from one spreadsheet row per store. The app must feel the same: everything about a
store on its page, with as few steps as possible. The **store page** gets a header card (operator,
lease summary, autopay, open balance, credit, next scheduled payment, open promise), one **action
bar** whose actions open the existing forms with the store and lease pre-filled (a vacant store
shows "New lease" instead), and **tabs** that move today's lists here: Charges, Invoices (with the
store's drafts and their Edit, Send and Delete), Payments, Schedules, Documents, Contact log,
History. **Record a payment** from the store page allocates one amount over the store's open
charges by hand, with the remainder kept as a **credit** on the lease (ADR-0013). The **lease page**
keeps lease terms only; the **operator page** becomes the same workspace across the operator's
stores; charge and invoice pages keep a "← Store NNNN" link; the dashboard, attention list, grid and
reports link to store pages; old URLs keep working; bare "are you sure" pages stay only for Charge
now, End lease, Transfer, refunds, voids and deletes; forms opened from the store hide the known
fields and return to the store tab. The **setup checklist** loses its tick boxes (a price means
charged; the catalogue default is a placeholder; a "Show in setup checklist" flag). The **invoice
builder** gets one combo box per line (Rent, Existing open charge, catalogue items, typed names), a
month select, the amount filled at once, the width bug fixed, a trash-icon remove, errors by
visible line, and the "already on draft" link. The **rent run** leaves the main navigation (charges
are created automatically); the store page gets "Create missing charges"; the run page lists what
it created and the dashboard shows a one-line summary. The **grid** shows 10 past months, the
current and the next, with a From/To picker.

## 2. Affected packages

`backend/ledger` (views `stores`, `leases`, `operators`, `charges`, `one_off_charges`, `invoices`,
`payments`, `dashboard` (grid), `run`, `documents`, `contacts`; services `stores` (new workspace
service), `payments_entry` (new: the allocation), `credits` (new), `rent_run`, `grid`,
`attention`, `invoice_builder`, `one_off_charges`; forms; templates incl. `finance/base.html` nav;
static JS and CSS; models `Payment.allocation_group`, `LeaseCredit`, `PaymentMethod.CREDIT`,
`FeeType.show_in_setup`, `InvoiceDraftLine.existing_charge`; migrations 0042–0044),
`accounts/registry.py` (new routes), tests, docs (RUNBOOK §18, DATA_OWNERSHIP, README matrix,
ADR-0013). Out of scope: visual design (12c), new provider calls, mail, the public statement page,
the Leases list (kept), e-mail receipts, insurance autopay changes.

## 3. Decisions

### 3.1 The store workspace (owner's 1, 4, 5, 6, 7, 8; finding 12d-1)

- **D1 — one service builds the page.** `services/workspace.py`: `store_workspace(store, today,
  role) -> Workspace` with: `header` (operator, current lease: rent, started_on, ended_on,
  autopay state from `leases.live_schedules` + `autopay_move`, `open_balance` = the store's lines
  of `receivables.open_balances`, `credit` = `credits.balance(lease)`, `next_payment` = the
  earliest of the lease's next rent debit (`next_debit_at`), the next one-off autopay due date,
  the next installment due, the next return re-debit (`Payment.redebit_date`), `open_promise` =
  `contacts.open_promise(operator, lease)`), `actions` (which of the bar's actions apply; see D2)
  and the tabs' data built **lazily per tab** (`?tab=` selects; every tab is server-rendered, the
  default tab Charges; the URL fragment `#tab-<name>` scrolls to it). Query budget: the header and
  the open tab, at most 25 queries, tested with the fixed-query pattern.
- **D2 — the action bar.** Actions, their targets and roles (all pre-filled with the store and
  its current lease by query parameters the existing forms already read or gain: `?store=`,
  `?lease=`): **Create invoice** → `invoices:new_store` (billing); **Record a payment** →
  `stores:record_payment` (new, D6; billing); **Charge now** → `stores:charge_now_pick` (new:
  the store's charges with a live Stax invoice and an operator with a bank method, one button
  each to the existing confirm page; admin); **Add setup charge** → `leases:add_setup_charge`
  (billing); **Schedule a payment** → `stores:schedule_payment` (**Batch B**, amended 2026-10-15: the
  setup-charge form cannot schedule a debit; a small form, item from the catalogue or typed,
  description, amount, date, creating a one-off charge collected by "Autopay on the due date"
  through `one_off_charges.add_charge` with `CollectionMethod.AUTOPAY_DUE`, so the existing
  `schedule_create` job makes the one-occurrence Stax schedule; needs the operator's linked bank
  method; billing); **Upload
  document** → the documents section's form (scrolls to it; billing); **Add contact entry** →
  `stores:contacts_add` (billing); **Edit lease** → `leases:edit` (admin); **End lease** →
  `stores:end_lease` (admin); **Transfer to another operator** → `stores:transfer` (admin). A
  store with no current lease shows **New lease** → `leases:create?store=` (admin) and hides the
  lease-dependent actions (everything but Upload document and Add contact entry). Viewers see
  no bar. Each action's form, when opened with `?store=&lease=`, hides those fields and shows one
  read-only line at the top ("Store 2477 · Lease from 2026-10-13 · Operator Acme"); after a
  successful save it returns to `stores:detail?tab=<tab>#tab-<tab>` (the tab that shows the
  result) instead of a detail page (D8 lists the mapping). "Create missing charges" (D13) is on
  the bar too (billing).
- **D3 — the tabs.** Charges: the lease's rent charges, one-off charges and insurance charges in
  one table (kind, description, due, billed, paid, balance, status, invoice link), filter
  `open|paid|all` (default open), with each row's existing actions (Record payment, Split…) as
  links to the detail page; Invoices: the operator's invoices for this store's leases with the
  provider status (`invoice_sync` summary as the operator page shows), filter `unpaid|all`
  (default unpaid), and **the store's drafts** (`InvoiceDraft` with `lease__store=store`, status
  draft) with **Edit** (`invoices:draft`), **Send** (the existing POST `invoices:send` with the
  same single-submit guard) and **Delete** (`invoices:cancel` of a never-sent draft is a plain
  delete: no note, no confirm page; a sent invoice keeps its cancel form); Payments: the ledger
  rows for this store (`payments_ledger.ledger_payments(PaymentFilter(store=...))`) with the
  credits applied and held; Schedules: the lease's rent and insurance schedules (as the lease
  page shows) plus the one-off autopay charges due and installment plans live; Documents: the
  existing section; Contact log: the existing table and Add entry; History (admin): the newest
  200 audit rows whose entity is the store, its leases, their charges, invoices, payments,
  schedules, documents, contact entries, lease ends and transfers (`audit.rows_for_store`),
  with a link to the audit page filtered by `documents_of`-style parameter `store_of=<pk>`.
- **D4 — the lease page.** Keeps: terms, dates, rent history and price changes, debit days,
  the proration switch, autopay (enable, pause, resume, stop), notes, documents. Loses: the
  charges table and the setup-and-one-off table; in their place one line "Charges, invoices and
  payments are on the store page" linking `stores:detail?tab=charges#tab-charges`. The anchors
  `#charges` and `#setup-charges` on the lease page redirect by a tiny script? No: anchors are
  not URLs; the two export links move to the store tabs and the lease page keeps a short
  "Exports" line pointing at them. `leases:add_setup_charge`, `leases:resolve_deposit` stay as
  routes (opened from the store page's actions and rows).
- **D5 — links and navigation.** Charge, one-off charge, invoice, plan and payment pages get a
  breadcrumb line "← Store 2477" (`stores:detail`) when the entity has a store. The attention
  list's lease-page items (insurance due, premium missing, lease ending, schedule review) link to
  the store page's Schedules or Charges tab; the grid's row header already links the store; the
  run page's lease links become store links; reports link operators as today and, where a row
  is a store, the store. The main nav loses **Rent run** (it moves under **Admin** as
  "Operations: rent run" for billing and admin; the Admin link becomes a small menu: Django
  admin, Rent run, Audit) and gains nothing else; the nav marks the current section
  (`aria-current="page"`, a context processor from the resolved URL namespace). Old URLs: every
  existing route stays; `leases:detail` stays; nothing redirects except the removed lease-page
  lists, which live on the store tabs (the owner's item 6 is satisfied by keeping the routes).
- **D7 — confirmation pages.** Kept (irreversible by the owner's list): Charge now (both),
  End lease confirm, Transfer confirm, refunds, voids, deletes (Stop autopay deletes at Stax:
  kept; the draft's Delete is a never-sent draft: no confirm; document delete keeps its inline
  note). Waive, cancel of a sent invoice or charge, plan cancel and deposit resolution are
  forms with required fields, not bare confirms: unchanged. Pause and resume stay POST-only.
- **D8 — return after save.** The mapping: Record a payment → Payments tab; Create invoice
  (Send) → Invoices tab; Add setup charge / Schedule a payment → Charges tab; Upload document →
  Documents tab; Add contact entry → Contact log tab; Edit lease → the store page header; End
  lease and Transfer keep their confirmed pages (they end with a summary); forms opened from
  elsewhere (an operator page, a detail page) return where they came from as today (`next`
  validated as a local path).

### 3.2 Record a payment with manual allocation (owner's 1, last bullet; ADR-0013)

- **D6 — the form.** `stores:record_payment` (`stores/<pk>/payments/new/`, billing, GET and
  POST). Fields: `creation_key`, `method` (zelle, wire, other), `amount`, `paid_on`, `reference`,
  `note`; then **every open charge of the store's current lease** (rent, one-off incl. the
  installments' and the lease-end balance, insurance) oldest due first with its balance, each
  with an **allocation** input; nothing pre-filled except when exactly one charge is open (then
  `min(amount, balance)`); a line **Credit on the lease** at the bottom shows the credit held
  and lets the user apply part of it to the listed charges in the same form (its own allocation
  inputs, "from credit"). JavaScript shows the remaining unallocated amount live
  (`#unallocated`); the server refuses to save unless the allocations add up to the payment
  amount **or** the user ticked "Keep $X as a credit on the lease" (`keep_credit`), in which
  case the remainder becomes a `LeaseCredit`. Each allocation is at most the charge's balance.
- **D6a — what it writes** (`services/payments_entry.record(store, input, actor)` in one
  transaction): one settled `Payment` per allocated charge through the existing services
  (`rent_run.record_manual_payment`, `one_off_charges.record_payment`, and new
  `insurance.record_manual_payment` with the same rules), each with `creation_key` derived from
  the form's key and the charge id (idempotent), all sharing `Payment.allocation_group` (new
  UUID column, migration 0042) and the form's method, date, reference and note; the remainder
  as `LeaseCredit(lease, amount, remaining, method, received_on, reference, note, created_by,
  allocation_group)`; a credit applied to a charge is a settled `Payment` of method `credit`
  (new `PaymentMethod.CREDIT`, never a provider method) with `credit_id`, reducing
  `LeaseCredit.remaining`. Stax marking as today per charge (paid-as-cash when the charge's
  invoice is fully covered; builder invoices when every line is covered), queued through the
  existing mark jobs with the per-line keys. Audited: `payment_entry.recorded` on the lease
  (group, lines, credit), plus today's per-charge rows.
- **D6b — credits elsewhere.** The header card shows "Credit $X" when `remaining > 0`; the
  Payments tab lists credits held and applied; the P&L footer gains "Credits held" next to
  "Deposits held" (cash received and not applied is a liability, never revenue; a credit
  application counts as collected in the month applied, under the charge's category); the
  lease-end statement lists a remaining credit as a refund-due line (settled by the settlement
  method like the overpaid month); a transfer refuses while a credit remains (it is the
  outgoing operator's money; refund it in the lease end). `receivables` ignores credits (they
  are not owed). ADR-0013 states all of this.

### 3.3 The setup checklist (owner's 8a)

- **D9.** `FeeType.show_in_setup` (bool, default False; migration 0043 sets it True for the
  active types that are not NSF fees and were created before the migration, i.e. the seeded
  catalogue: deposit, access, sign, architecture, business purchase, phone, insurance; items
  saved from the builder default False; the Django admin and the catalogue page expose the
  flag). `checklist_types()` returns `active and show_in_setup`. The lease form and the
  transfer's incoming step drop the `_tick` fields: a row is **included when its price has a
  value**; the price input is empty with the catalogue's `default_price` as a grey
  `placeholder` (never a filled value); vendor, cost and due date as today; a total line
  "Setup charges: $X" under the table updates live and renders server-side. Posting a tick
  field is ignored. The transfer wizard's deposit stays its own field.

### 3.4 The invoice builder lines (owner's 8b; finding 12d-1)

- **D10 — one combo per line.** The `kind` select goes; the Item box is one `<input list>` whose
  datalist starts with **Rent**, then **Existing open charge: <label, balance>** for each open
  one-off charge of the draft's lease that is not on a live invoice or plan and is not an
  installment (e.g. the checklist's deposit), then the catalogue items; a typed name stays a
  typed item. Choosing Rent reveals the lease select and a **month select** (the current month
  ± 12, labelled "October 2026"); when both are set the unit amount is filled at once from
  `invoices:rent_amount?lease=&month=` (new read-only JSON route, billing: the existing
  charge's balance or `first_month`'s amount, with the proration text), visible and editable.
  Choosing an existing charge sets `InvoiceDraftLine.existing_charge` (new FK, null; CHECK: not
  with a rent line or a fee type) and fills the amount with the charge's balance (editable down,
  not up); on send the line **attaches** that charge to the invoice instead of creating one
  (the same rules as a rent line's attach: unpaid, nothing live, no plan). The box renders at
  full width (the CSS bug: the input has no width inside the table cell; give `td.line-item
  input` `width:100%`), and the chosen value is visible. A new draft opens with **one** line;
  "Add line" adds more; **Remove** is a trash icon button at the end of the line (sets `DELETE`,
  hides the row); validation skips removed lines and numbers errors by the visible line ("Line
  2: …" counts only rows the user sees); the refusal "rent <month> of store X is already on
  draft DRAFT-…" links to that draft. The same table rules (trash icon, visible numbering, live
  total) apply to the setup checklist and the deductions table of the lease end.

### 3.5 Operations pages (owner's 9, 9a)

- **D11 — rent run out of the nav** (D5). The run page lists **every charge it just created**
  (store, month, amount, "catch-up" or "today", linked) after the button, and the daily tick's
  last report: the tick writes an audit row `rent_run.ticked` with counts and the created
  charge ids; the run page shows the latest tick's list; the dashboard shows one line "Today's
  rent run: N created, M skipped" from that row, linking the run page.
- **D13 — Create missing charges per store.** `stores:create_charges` (POST, billing):
  `rent_run.catch_up_lease` for the store's current lease (and its ended leases of the last 24
  months), then `create_month` for today's debit if due; the message lists what it created
  ("Created: Rent 2026-10 $1,225.81 (catch-up)") or "Nothing missing"; returns to the Charges
  tab; audited as the run is.
- **D12 — the grid window.** Default range: 10 past months, the current and the next (12);
  `from`/`to` month selects on the page (the current ± 24, `MAX_MONTHS` 24 kept); a future
  month's cell shows what is billed or paid in advance with the invoice number (the cell logic
  already reads charges by month; the "not due" state applies only to months before the lease
  started).

### 3.6 The operator page (owner's 3)

- **D14.** The operator page gets the same header card and tabs (D1, D3) across all the
  operator's stores, with a **store selector** on the action bar (the actions act on the chosen
  store's current lease; the default is the operator's only store, else the first by label) and
  the operator-level items kept above the tabs: contact details, SMS phone and consents, the
  payment-provider block, Collection e-mails, the operator's contact log and notes. Its drafts
  table gets the same Edit, Send and Delete actions (12d-1). `workspace.operator_workspace`
  reuses `store_workspace` per store with one merged query set.

## 4. Interface contract

Routes (new): `ledger:stores:record_payment` (billing), `ledger:stores:schedule_payment`
(billing, Batch B), `ledger:stores:charge_now_pick` (admin), `ledger:stores:create_charges` (POST, billing), `ledger:invoices:rent_amount` (GET
JSON, billing), `ledger:invoices:delete_draft` (POST, billing; a never-sent draft). Query
parameters: `?tab=`, `?store=`, `?lease=`, `?collection=`, `?next=` (local path). Models:
`Payment.allocation_group` (UUID, null), `Payment.credit` (FK `LeaseCredit`, null),
`PaymentMethod.CREDIT`, `LeaseCredit`, `FeeType.show_in_setup`, `InvoiceDraftLine.existing_charge`.
Services: `workspace.store_workspace`, `workspace.operator_workspace`, `payments_entry.record`,
`credits.balance/apply/held`, `insurance.record_manual_payment`, `audit.rows_for_store`,
`rent_run.catch_up_store`, `grid.default_range` (10+1+1). Audit actions: `payment_entry.recorded`,
`lease_credit.created/applied`, `rent_run.ticked`, `invoice_draft.deleted`. Migrations
`0042_payment_allocation_and_credits`, `0043_fee_type_show_in_setup`, `0044_draft_existing_charge`.
No new dependency, environment variable or provider call.

## 5. Work breakdown (batches, each gated)

| Batch | Decisions | Done when |
|---|---|---|
| A | D1, D2 (bar without the new payment form), D3, D4, D5, D7, D8; drafts' Edit/Send/Delete on the store page; the "already on draft" link | the store page shows header, bar and tabs with today's data; the lease page is details only; detail pages link the store; nav and attention link stores; every old route answers; forms opened from the store hide the known fields and return to the tab |
| B | D6, D6a, D6b, insurance manual payment, `stores:schedule_payment`, migration 0042, ADR-0013 (Fable) | a payment split over charges with a credit remainder, applied later; Stax marked when covered; P&L footer; lease end and transfer rules |
| C | D9, D10, migrations 0043, 0044 | the checklist without ticks and with the flag; the builder's combo, month select, amount endpoint, existing-charge line, width, trash, numbering |
| D | D11, D12, D13 | the run page's list and the tick's row; the dashboard line; "Create missing charges"; the grid window and picker |
| E | D14 | the operator page as the workspace with the store selector and the drafts' actions |

Fable: adversarial tests per batch, the gate per batch, ADR-0013, RUNBOOK §18, DATA_OWNERSHIP,
README matrix, QA-012d, the checkpoint report.

## 6. Test plan (before implementation)

**A.** Header figures for a store with rent, one-off and insurance open, a credit (seeded), a next
debit, an open promise; the vacant-store bar; each action's target with `store` and `lease`
pre-filled and the hidden fields shown as the read-only line; return to the right tab after
each form; the tabs' filters and the query budget; drafts listed with Edit, Send, Delete (delete
of a never-sent draft needs no note; a sent one keeps its form); the lease page without lists
and with the link; the breadcrumb on charge, one-off, invoice, plan and payment pages; the
attention items' urls; the nav without Rent run and with Admin's menu; `aria-current`; every
route in `tests/test_route_registry.py` classified; viewers see no bar and no History tab;
History rows limited to the store's entities. **Adversarial (Fable):** `?store=`/`?lease=` of
another store or a mismatched pair are refused or ignored (never a form pre-filled with a lease
of another store); `next` cannot leave the site; a viewer gets 403 on every bar action; the
History tab never shows another store's rows; the drafts' Delete cannot delete a sent invoice.
**B.** Allocations must add up (short, over, negative, over a charge's balance refused; nothing
written); the single-charge pre-fill; the live remainder; keep-credit creates the credit; a
later payment applies the credit (method credit, no provider marking from credit alone unless
the invoice is covered); Stax paid-as-cash when a charge's invoice is fully covered by the
allocation (builder invoice: all lines); a partial allocation flags review as today; insurance
allocation; idempotent replay of the form key; the P&L footer; the lease-end statement's
credit line; the transfer refused while a credit remains; `receivables` unchanged by credits.
**Adversarial (Fable):** a charge id of another store in the allocation is refused and nothing is
written; a replayed key never double-pays; the credit cannot be applied beyond its remaining;
totals never double count under random allocations (property test); viewer and anonymous
refused; a card number in the note refused.
**C.** Checklist: empty price → not charged; placeholder never charged; a value → charged at that
value; the flag hides an item; items saved from the builder default hidden; the total line;
the transfer's incoming step the same. Builder: Rent reveals lease and month, the amount filled
from the endpoint (prorated, existing charge), editable; an existing open charge joins the
invoice and is attached on send (the deposit from the checklist), refused when live or on a plan;
removed lines ignored and errors numbered by visible lines; one line on a new draft; the trash
removes; the width (a CSS rule test); the draft link in the refusal. **Adversarial (Fable):** the
rent-amount endpoint refuses another operator's lease and a viewer; an existing charge of
another lease or operator cannot be attached; a removed line with bad values never blocks the
save; the amount of an existing-charge line cannot exceed its balance.
**D.** The run page lists created charges with links after the button and after the tick; the
dashboard line; "Create missing charges" creates only this store's charges and says "Nothing
missing" otherwise; the grid's default window and picker; a future month billed in advance.
**Adversarial (Fable):** "Create missing charges" on store X never touches store Y; the picker's
range is capped at 24 months and a reversed range swapped; a viewer cannot create charges.
**E.** The operator workspace across two stores with the selector; the drafts' actions; the
operator-level items kept. **Adversarial (Fable):** the selector cannot pick another operator's
store; actions act on the selected store's lease only.

## 7. Risk register

- Money: the allocation writes several payments in one transaction through the existing
  services; the per-line creation keys keep replays idempotent; credits are a new liability
  with a stated rule (ADR-0013).
- Reach: the store page becomes the hub; its query budget is tested; tabs render lazily.
- URLs: no route is removed; the lease page's lists move, nothing breaks a bookmark.
- Migrations 0042–0044 are additive (nullable columns, one table, one flag with a data step).
- The nav change and the breadcrumbs touch every page's template through `base.html`; the e2e
  suite covers the main paths.

## 8. Acceptance criteria

Each batch's gate green on both engines; the §6 cases present; on staging: the owner's everyday
path for one store needs no lease or charge page (invoice, payment with allocation, document,
contact entry, missing charges) and the drafts are reachable from the store and operator pages
with Edit, Send and Delete; the checklist charges nothing by accident; the builder line works
in Safari; the grid opens on the 12-month window.
