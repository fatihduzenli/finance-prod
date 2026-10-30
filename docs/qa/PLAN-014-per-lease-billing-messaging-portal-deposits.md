# PLAN-014 — owner review of 12g, per-lease billing, messaging, reminders, returns, aging, portal, bank deposits

Fable's Phase A plan for the owner's prompt of 2026-10-29 ("Step 14 — owner review of 12g,
per-store billing, reminders, aging, portal, bank matching"; decisions of 2026-10-28 and
2026-10-29). Staging runs 12g at `7ecad9b`. Nine batches in two parts (A, B, C; D, E, F, G, H, I),
one pull request each, the usual gate; push, PR and merge in order under the owner's authorisation
in that prompt ("I approve each Step 14 PR for merge in order once its CI is green and the previous
deploy is verified on staging; this message is my explicit review and approval for those merges.
Stop on any red. Batches run in the order below: Part 1 changes the data model that Part 2 builds
on."). The QA record is `docs/qa/14/QA-014.md`. Checkpoint after C (shots of every changed page in
both themes at both widths; the migration's results on staging data), then Part 2 continues
without waiting unless something needs the owner's decision; checkpoint after I (the Outbox from a
staging run of the daily job; the TODO-VERIFY items); then the Publish Request.

Four decisions are recorded as ADRs because they change what earlier ADRs or `docs/CLAUDE.md`
rules say: **ADR-0015** operator is a person and billing details, with the processor customer,
belong to each lease (C); **ADR-0016** the messaging engine, the reminder rules and automatic
re-debits of R01/R09 returns (D, E, F; supersedes ADR-0010's "NSF only by staff" for the fee and
the retry); **ADR-0017** the operator portal with the processor's hosted fields (H; reverses the
owner's 2026-09-29 statement in ADR-0004 and the Rule 1 fence "do not embed Stax.js"); **ADR-0018**
bank deposit matching with a pluggable source and the Plaid connector (I; a new external service
and two new secret names). Each ADR is written by Fable in the batch that implements it, with the
matching edits to `docs/CLAUDE.md` (Rule 1 and Rule 2 text, "customers never log in here") and
`docs/SCOPE.md` §2 and §4 (bank details, "no portal"). Rule 1's core does not change: no card or
bank account number ever enters this app, its database or its logs; the processor's hosted fields
run in the operator's browser inside an iframe the processor serves, and our server receives a
payment-method token and a last-four only.

## 1. Objective

Part 1 fixes what the 12g review found and moves the billing model to where the owner's business
has it: an **operator is a person** (full name, address, phone, e-mail) kept so that all of one
person's stores and their total debt are seen together; **billing details live on each lease**
(company name, e-mail, phone, address, the processor customer and its bank method, last four
only), copied once from the operator by a migration and independent from then on; invoices,
statements, autopay, Add payment and every message use the lease's details. Before that, Batch A
closes the 12g carry-overs and the dashboard, list and table fixes, and Batch B gives the store
page its Brand, its LEASE card rows (sign fee, insurance by year, four states), store notes with
pinning and the renamed "Notes & follow-ups" timeline.

Part 2 builds on the lease's billing e-mail: a **messaging engine** on the existing Gmail
delegated-account transport (Outbox mode until the secret exists, a sandbox allowlist, admin-edited
templates with a closed placeholder set, a 9:00–18:00 New York send window, one message per lease
per charge per rule), the **reminder rules** A–E as a daily job, **returned debits** with template F,
the NSF fee and the automatic re-debit of R01/R09 returns within Nacha's limits, the **Aging
report** by store that reconciles with the store pages and the Unpaid KPI, the **operator portal**
under `/portal/` with its own login, invitations, reset, lockout, isolation, ACH payment and bank
entry through hosted fields, and the **Bank deposits** module with a Plaid connector that stays off
until an admin enables it and its keys exist.

## 2. Packages and scope

One Django project (`backend/`, uv workspace root; `uv.lock` unchanged — no new Python dependency
is planned: Plaid and Stax.js are reached over HTTPS JSON through `httpx` as `finance/usps.py` does,
and the access-token encryption uses `cryptography`, already in the lock as google-auth's
dependency). Touched, by batch:

- **A:** `ledger/services/dashboard.py`, `ledger/views/dashboard.py`, the dashboard templates,
  `accounts/models.py` (+ migration 0004: `payment_status_period`), `ledger/services/stores.py`,
  `ledger/templates/ledger/stores/list.html`, every list page template and its filter form
  (`ledger/forms.py` filter forms, `ledger/views/{stores,operators,payments,leases,arrears,
  reports,grid}.py`), a shared `finance/templates/finance/_filters.html` +
  `finance/static/finance/filters.js`, `ledger/templates/ledger/payments/list.html`, the `<th>`
  classes of every table, `app.css`; `docs/qa/12g/QA-012g.md` and `docs/design/*-dark-built.png`
  (Fable).
- **B:** `ledger/models.py` (`StoreType` → `Brand` rename, `Note` fields; ledger migration 0058
  with the seed and the "Brand needs choosing" review), `ledger/admin.py`, a new Admin › Settings
  page (`ledger/views/settings.py`, `ledger/templates/ledger/settings/*`, registry), the store
  header and tabs (`stores/_header.html`, `_tab_contacts.html` → notes and timeline),
  `ledger/services/{workspace,detail_cards,insurance,notes}.py`, `ledger/views/{stores,notes}.py`,
  the rename of "Contact log" in templates, code comments and help.
- **C:** `ledger/models.py` (`LeaseBilling`; migration 0059 data copy), `payments/models.py` (no
  schema change; `entity_type="lease"` rows), `payments/operator_sync.py` → `lease_sync.py` (the
  customer per lease), `ledger/services/leases.py` (`customer_ref(lease)`,
  `linked_payment_method(lease)`), every caller listed in §3.C.5, the operator form, the New lease
  and Transfer wizards (`ledger/forms.py`, `ledger/views/{leases,lease_transfer}.py`), the
  invoice and statement "Bill to", the hosted-fields integration (`payments/hosted_fields.py`,
  `payments/stax/hosted.py`, `ledger/static/ledger/bank_entry.js`, env guard + settings for
  `STAX_WEB_PAYMENTS_TOKEN`, infra secret name `stax-web-payments-token`), the management command
  `create_lease_customers`, ADR-0015, `docs/CLAUDE.md`, `docs/SCOPE.md`.
- **D:** `mail/` (the Outbox and allowlist dispositions), `ledger/models.py` (`OutboundMessage`
  fields, `MessageTemplate` fields, `CompanySettings` sender/reply-to/manager list/allowlist;
  migration 0060 incl. the six seeded templates and the retirement of `CollectionStep`),
  `ledger/merge.py`, `ledger/services/{messages,collections}.py` → `messaging.py`,
  `payments/models.py` (`Job.run_after`; payments migration 0019), `payments/jobs/__init__.py`,
  Admin › Settings › Message templates and Mail, Admin › Outbox page, the timeline union
  (`ledger/services/timeline.py`), env guard (`GMAIL_SENDER` retired), `.env.example`, ADR-0016.
- **E:** `ledger/services/reminders.py`, `ledger/management/commands/collections_tick.py`,
  `ledger/services/contacts.py` (promise pause hook).
- **F:** `payments/webhooks/handlers/transaction.py` (return hook), `ledger/services/nsf.py`
  (fee at return, automatic re-debit, stop), `ledger/services/returns.py`, `payments/stax/
  return_codes.py` (plain-words table), `ledger/services/business_days.py`, the charge page's
  "Stop automatic retry", `ledger/services/attention.py`.
- **G:** `ledger/services/receivables.py` (`aging` by store with filters), `ledger/views/
  reports.py`, `reports/aging.html`, the CSV header, the store charges tab's drill-down filter.
- **H:** a new app `backend/portal/` (models, middleware, views, templates, static, urls,
  migrations 0001), `accounts/registry.py` and the auth middleware exemptions, `finance/urls.py`,
  the PORTAL card on the operator page, `payments/jobs` (`PORTAL_PAY`), `ledger/services/
  portal_payments.py`, ADR-0017.
- **I:** a new app `backend/banking/` (models, services, the Plaid client under
  `banking/plaid/`, views, templates, migrations 0001), env guard + settings for `PLAID_*`,
  infra secret names `plaid-client-id`, `plaid-secret`, `plaid-token-key`, Admin › Settings ›
  Bank deposits, the invoice and portal memo line, ADR-0018.

Out of scope for every batch: version bumps and release config; the Django admin's theming; SMS
sending (the channel abstraction only); late fees; any card acceptance (`allow_card` stays
CHECK false); the live Stax run of the per-lease customers, live ACH, live mail and a real Plaid
account (all TODO-VERIFY at the second checkpoint); an Invoices list page (none exists; §8).

## 3. Decisions

Numbering (as built through C): ledger migrations 0058 (B), 0059 (C: billing per lease), 0060 (C:
the transfer's incoming billing), 0061 (D), 0062 (F), 0063 (G if needed); payments 0019 (C: the
lease job kinds and the attach kind), 0020 (D: `Job.run_after`); accounts 0004 (A); portal 0001 (H);
banking 0001 (I). ADRs 0015–0018. TODO-VERIFY 066–073 added with C.

### Batch A — dashboard, lists and small UI fixes (Opus; Fable the carry-overs and tests)

- **A1 12g carry-overs (Fable).** `docs/qa/12g/QA-012g.md` gains Batch D's merge note (PR #22,
  `7ecad9b`, staging verified 2026-10-29) and the CI visual time with 124 shots (3:10 against
  2:06–2:33 before). The four dark report shots are written by the visual suite with
  `--report-dir` on PostgreSQL in the Playwright image and committed as
  `docs/design/{dashboard-dark-laptop,dashboard-dark-phone,store-workspace-dark-laptop,
  monthly-rent-overview-dark-laptop}-built.png`.
- **A2 Fifth row removed.** `home.html` drops the `[Coming up ⅔ | Collection ⅓]` row at both
  widths; `coming_up` and `collection` leave the view's context and the service's `blocks`
  (dead code goes with them: `dashboard.coming_up`, `collection_progress` and their templates
  `_coming_up.html`, `_collection.html`, only if no other page includes them — Opus reports).
  The order pin becomes 10 ids and two `dashboard-row-thirds`.
- **A3 Payment status period.** The card gets a `Weekly | Monthly` segmented pill (the 12g
  segmented component, `aria-pressed`), default Weekly. Weekly = charges **due** in the current
  calendar week Sunday–Saturday (New York dates); Monthly = due in the current month. `‹ ›` step
  one week or one month; the period reads under the title ("Oct 25 – 31", "Oct 25 – Nov 1" when
  a week crosses a month, "October 2026"). Query: `?ps=weekly|monthly&ps_at=YYYY-MM-DD` (any day
  of the week or month; an invalid value falls back to the stored choice and today; never a
  500). The chosen period kind (not the date) is remembered per user in
  `accounts.UserPreference.payment_status_period` (`weekly|monthly`, default weekly; migration
  0004; saved by the same POST pattern as the theme: `POST /settings/payment-status/`, body
  `period=…`, 400 otherwise; the pill's links carry `?ps=` so a GET never writes — the save
  happens when the pill is used, through a tiny form per segment). The donut, centre count and
  the three boxes follow the period; the pending line too. The service:
  `payment_status(period: Period, today) -> PaymentStatusCard` where `Period(kind, start, end,
  label, prev_at, next_at)` is built by `period_for(kind, at, today)`; the Cashflow range no
  longer feeds it (`?range=` keeps working for Cashflow only).
- **A4 Vacant store rows.** Stores list: for a store without a lease in effect, Balance shows
  "—", or the leftover balance of the last lease as "● $1,200.00 unpaid" (the unpaid dot, the
  `money` filter) when `services/stores.store_balances` leaves one; Status shows "○ Vacant ·
  23 days" (from `lease_end.vacant_since`) or "○ Vacant · never leased" (no lease ever);
  the row ends with a small "+ Add lease" link to `ledger:leases:create?store=<uuid>` for
  admins (the route is admin-only; other roles see no link). The phone card shows the same.
- **A5 List pages: search + Filters.** One shared component `{% filters %}` in
  `finance/templates/finance/_filters.html` with `finance/static/finance/filters.js`: a row with
  the wide search field (`input[type=search]`, `name=q`, the page's existing search parameter
  name kept where one exists) and a "Filters" button; the panel under the row holds the page's
  existing filter fields (unchanged names and values) with **Clear** and **Apply**; on phones
  (≤ 768px) the panel is a bottom sheet (the 12g More-sheet mechanism and `--backdrop`). The
  button reads "Filters" or "Filters · 2" (count of filter fields with a non-default value;
  search and sort do not count). Active filters render as removable chips under the search
  field ("State: New Jersey ×", each a link to the same URL without that parameter). Sort stays
  beside the search as a "Sort: Store number ▾" menu (the existing `.menu`), only on pages that
  sort today (arrears; any page whose view already reads a `sort` parameter). **Results as you
  type:** `filters.js` submits the form as a GET after 300 ms idle and swaps the page's
  `#results` region from the response (`fetch` of the same URL with `X-Requested-With`; the view
  does not change — the client extracts `#results` from the full page), updating the URL with
  `history.replaceState`; without script the search field submits on Enter and the old
  Filter button's job is done by Apply. The pages: Stores, Operators, Monthly rent overview,
  Payments, Leases, Arrears, the report filter forms (P&L, revenue, deposits, the aging of G).
  A page keeps its parameter names and defaults so every existing test URL still works.
- **A6 Payments totals tiles.** `payments/list.html`'s totals card becomes five tiles in a
  `.tile-row` (label above value, numbers right-aligned in their tile): Settled (collected; green
  `--paid-text`), Pending, Returned (red `--unpaid-text` when > 0), Refunded, Deposits held; a
  zero tile is muted; the explanation sentence becomes an info icon (`button.info` with
  `aria-describedby` + a `role="tooltip"` popover using `--popover-*`, the 12g popover tokens;
  works on focus and tap); two tiles per row ≤ 768px.
- **A7 Numeric headers.** Every `<th>` of a column whose cells carry `class="money"` (or the
  numeric classes `num`, `count`) carries the same class so it right-aligns. Opus walks every
  template under `ledger/templates/` and `finance/templates/`; the known misses are
  `payments/list.html:45`, `leases/enrol.html:17`, `run/today.html:137`, `arrears/page.html:41`.
  Fable's test renders every page of the visual suite's world and asserts the rule over every
  table (§5).

### Batch B — store page: lease card, brand, notes (Opus; Fable the tests)

- **B1 Brand.** `StoreType` is renamed `Brand` (model, table `brands`, FK `Store.brand`,
  `related_name` kept), migration 0058: rename, then seed exactly **iFixandRepair · Techy ·
  Talk&Fix** (`sort_order` 1–3, active) and deactivate every other row (Inline store, Kiosk, Cart,
  Pop-up, Warehouse); every store whose brand is now inactive gets `brand=None`,
  `needs_review=True` and a `StoreReview`-style reason: the existing store `needs_review` flag
  plus a new attention item kind `BRAND_NEEDED` ("Brand needs choosing") listed under Needs
  review for the store, cleared when a brand is set. Backward: reactivates the old rows; the
  cleared brands cannot be restored (the migration says so, `CANNOT_REVERSE` pattern of 0018 for
  the data step only when rows exist). "Store type" reads "Brand" everywhere: form label, list
  filter ("Brand: Techy ×"), badge (`_type_pill.html` → `_brand_pill.html`), the stores CSV
  header, help text, `STORE_TYPE_REFUSED` → `BRAND_REFUSED`. The Django admin registration moves
  to the new **Admin › Settings** page (B2); `StoreTypeAdmin` goes.
- **B2 Admin › Settings page.** New `ledger:settings:index` (`/admin-settings/`, admin only, GET
  in `READ_ONLY_ROUTE_NAMES`, POSTs as `RouteRule(ADMIN, POST)`), linked from `_admin_links.html`
  as "Settings" under Admin (the user menu's "Settings" is renamed "Preferences" to avoid two
  links with one name — reported at the checkpoint). Sections are cards; B adds **Brands** (list,
  add, rename, activate/deactivate, reorder; never delete; audited `brand.created|changed`).
  D adds **Message templates** and **Mail**; I adds **Bank deposits**. "System settings" (the
  Django admin link) stays for the models that remain there.
- **B3 LEASE card.** `stores/_header.html`: the "Since" row goes; Open balance in
  `--unpaid-text` when > 0, "$0.00 ✓" in `--paid-text` at zero (`detail_cards.lease_open_balance`).
  Under Deposit: **Sign fee** (the one-off charge with `fee_type.code="sign_fee"` on this lease,
  newest) and **Insurance 2026–27** (the `InsuranceCharge` of the current policy year,
  `services/insurance.py`; the label is `policy_year`–`policy_year+1` short), each with one of four
  states from `detail_cards.collection_state(charge)`: `✓ collected` (green; balance 0 and paid
  > 0), `$x of $y · partly collected` (orange; 0 < paid < due), `not collected` (red; charge
  exists, paid 0), `not billed` (grey; no charge) with a small "Bill now" link — to the invoice
  builder prefilled with the sign fee item, or to the insurance charge creation for the year
  (existing routes; Opus names them). Each row links to its charge. When the previous policy year
  was not collected or never billed, a yellow note (`--flash-warning-*` tokens) under the
  Insurance row: "Insurance 2025–26 ($300.00) not collected" / "Insurance 2025–26 never
  billed" (the amount is the premium when known, else omitted).
- **B4 "Notes & follow-ups".** Every "Contact log" becomes "Notes & follow-ups": the tab
  (`workspace.TAB_CONTACTS` label), the card title on the store page, the operator page card
  and its "All entries" page title and breadcrumbs, `contacts/edit.html`, `correct.html`, the
  calendar e-mail text, help pages; code comments follow. Route names and ids do not change.
- **B5 Store notes.** `Note` gains `lease` (nullable FK; a store note is `entity_type=store`),
  `pinned` (bool), `pinned_at`, `edited_at`, `deleted_at/deleted_by/delete_reason` (soft delete,
  the `ContactEntry` pattern), `updated_at`. Service `ledger/services/notes.py`: `add(store,
  body, author)`, `edit(note, body, actor)` (author or admin, else refused; sets `edited_at`),
  `pin/unpin` (billing), `delete(note, reason, actor)` (author or admin; soft delete; audit
  `note.deleted` with the body in `before`), `pinned_for(store)`. Body: `free_text`, the
  card/account-number validator, 2000 characters. UI: at the top of Notes & follow-ups a
  textarea "Write a note about this store…" with Save (billing); each note shows avatar "N",
  the author's name (`|staff`), date and time (`finance.dates`), "edited" when `edited_at`,
  a pin control, and Edit / Delete for the author or an admin; pinned notes also render in a
  **NOTES** card in the right column (`stores/_notes_card.html`, pinned first by `pinned_at`,
  at most five, "All ›" to the tab). Routes `ledger:notes:add|edit|pin|delete` (POST, BILLING;
  delete BILLING with the author check in the service, admin override).
- **B6 Timeline filter.** The tab's timeline becomes one list from `ledger/services/timeline.py`
  `entries(lease|store, kind_filter)`: notes, contact entries (calls, visits, promises with
  their outcomes) and, after D, messages; the filter is a segmented pill `All · Notes · Calls &
  promises · E-mails` (`?show=all|notes|contacts|emails`; "E-mails" present from B with an
  empty state "No e-mails yet" until D). Order newest first; the existing per-entry actions keep
  working.

### Batch C — operator is a person; billing details per lease (Opus; Fable the tests, ADR-0015)

- **C1 Operator form.** Fields: **Full name** (required; stored in `display_name`; `kind` is set
  to PERSON for new operators), Address (the existing line1/line2/city/state/postal), Phone,
  Email. Type, Company name, Contact person and Legal name leave the form and every page;
  the columns stay (hidden history; `legal_name`, `company`, `contact_person`, `first_name`,
  `last_name`, `kind` are no longer written by forms; `save()` keeps deriving `display_name`
  only when it is empty). Existing operators keep their `display_name` as Full name. Every
  place that printed `legal_name` or `company` for the operator (invoice "Billed to", statement
  headers, the details card) now prints the full name, or the lease's company (C6).
- **C2 `LeaseBilling`.** New model, OneToOne `lease` (`related_name="billing"`): `company_name`
  (blank), `email` (EmailField, blank, lower-cased), `phone`, the address fields, `bank_last4`
  (4 chars, blank; a cached copy of the linked method's last four for lists and cards),
  `shares_account_of` (nullable FK to another `Lease`; "Use the same account as #1991"),
  `created_at`, `updated_at`. Processor state per lease: `ExternalRef(entity_type="lease",
  external_kind="customer"|"payment_method")`, provider `stax_pay`; the method's snapshot list
  hangs off the lease's customer ref as today. `Lease` gains `sync_status`, `sync_failure`,
  `sync_failure_fields`, `sync_attempted_at`, `creation_key` (moved from the operator's
  semantics; the operator's columns stay for history). Table `lease_billing`.
- **C3 Two helpers.** `leases.customer_ref(lease)` and `leases.linked_payment_method(lease)`
  resolve through `shares_account_of` (one hop; a cycle is refused by a CHECK that a lease
  sharing another's account has no customer ref of its own, and by the form) and replace the
  operator versions; `operator_sync.py` becomes `payments/lease_sync.py` (`customer_input(lease)`
  sends the lease's company as `company_name`, the operator's full name as first/last name split
  on the last space, the lease's e-mail and phone; check-before-create by e-mail stays;
  `reference` = store number). `OPERATOR_SYNC|LINK|METHODS_REFRESH` jobs become `LEASE_*`
  (`JobEntity.LEASE`); the operator page's "Connect to Stax" and provider card move to the
  lease (the LEASE card's "Edit billing details" dialog carries the Link form for staff-entered
  IDs, which stays as the dashboard path). The webhook mapping `customer_operator()` becomes
  `customer_lease()`; a customer ref that still belongs to an operator (none after the migration)
  is reported as a mismatch.
- **C4 New lease and Transfer.** The wizards gain a **Billing details** step when an operator is
  assigned: Company name (optional), Email, Phone, Address, and the bank account: either
  **Use the same account as #1991** (a select of the operator's other leases that hold a
  customer and a usable bank method; sets `shares_account_of`), or **Enter a bank account**
  through the processor's hosted fields (C7), or **Later** (the lease gets its own customer at
  the processor through `LEASE_SYNC` and no method; the card shows "no bank account on file" and
  autopay stays blocked by `NO_BANK_METHOD` as today). A "Copy from operator" button fills
  e-mail, phone and address from the operator in the browser; nothing is copied automatically
  (the fields start empty). Transfer: the new lease gets its own billing details; the ended
  lease keeps its own as history (its refs are not superseded).
- **C5 Callers.** Every reader of the operator's customer, method, e-mail, phone or address for
  billing switches to the lease's (`autopay_blockers`, `enrolment_notes`, `enable/pause/resume/
  stop/move_autopay`, `_create_one`'s `email_notification`, the enrolment page's "Receipts go
  to", `one_off_invoices.create_provider_invoice/create_rent_invoice/schedule/find_provider_
  invoices`, the builder's `_charge_now_refusal/bank_tiles/charge_now_details`, `one_off_
  charges` checks and `bank_debit_options`, `autopay_plans`, `installments`, `nsf`, the Add
  payment summary and `_how.html`, `workspace_cards.bank_method`, `messages.queue`'s
  `to_address`, `collections` skip rules, `lease_end` statements, `statement_links`).
  `OutboundMessage` gains `lease` now (D fills the rest). A one-off charge without a lease
  (`lease=None`, operator set) bills the operator's **first active lease**'s details; if the
  operator has none, the charge is refused for provider collection with `NO_LEASE_BILLING`
  (a new refusal; local recording still works) — reported at the checkpoint.
- **C6 "Bill to".** `invoices/_invoice.html`, `reports/statement.html`, `statement_public.html`,
  `lease_ends/statement.html`: the lease's company name (when set) with the operator's full
  name under it, then the lease's address and e-mail. The `Preview` carries `bill_to` built by
  `invoice_builder.bill_to(lease, operator)`; a statement that spans several leases of one
  operator (the operator statement) prints the operator's full name and, per store section,
  that lease's company.
- **C7 Hosted fields (the one new processor surface before H).** `payments/hosted_fields.py`
  defines `HostedFields` (Protocol: `config_for(customer_external_id) -> HostedFieldsConfig`,
  `verify(payment_method_external_id, customer_external_id) -> PaymentMethodRecord`) with
  `StaxHostedFields` (`payments/stax/hosted.py`: the page loads Stax.js from Stax's documented
  CDN URL with the **web payments token** from `settings.STAX_WEB_PAYMENTS_TOKEN` — a per-merchant
  publishable token, still handled as a secret: Key Vault name `stax-web-payments-token`, env
  `STAX_WEB_PAYMENTS_TOKEN`, redacted in logs, never in source; the browser tokenizes the bank
  account under the lease's customer; the server receives **only** the payment-method id and
  verifies it with the existing read `get_payment_method` (worker-only transport → the verify
  runs in a job `LEASE_METHOD_ATTACH`; the page polls the job's outcome as Charge now does):
  the method must belong to that customer and be a bank method; then the lease's
  `payment_method` ref is written (superseding the old one), the snapshot refreshed and
  `bank_last4` cached) and `FakeHostedFields` (`APP_ENV=local` or `STAX_PROVIDER=fake` only: a
  plain form that posts a fake method id to the same endpoint; the e2e and visual tests use it).
  On staging the token secret does not exist and the sandbox cannot tokenize a bank ("no
  tokenization gateway found", `docs/STATUS.md:342`): the bank step shows "Bank account entry is
  not set up on this environment yet" and the Link form (staff-entered IDs) remains; recorded as
  TODO-VERIFY-066. Rule 1 text changes (ADR-0015 / ADR-0017): "do not embed Stax.js" becomes
  "Stax.js runs only on the bank-entry pages, inside the processor's iframe; the app never
  renders a field for an account or routing number itself and never receives one."
  `test_stax_isolation` keeps "stax" out of templates except the two bank-entry templates
  (allow-listed by path). No CSP exists on HTML today; the bank-entry pages get a page-level
  `Content-Security-Policy` meta that allows scripts and frames from the Stax host only
  (reported; a site-wide CSP is not in scope).
- **C8 Migration 0059 (data copy, once).** For every lease: create `LeaseBilling` with the
  operator's e-mail, phone and address and `company_name` = the operator's `company` when its
  kind is COMPANY (else blank). Processor refs: for each operator holding a `customer` ref, pick
  its **primary lease** (the active lease with the earliest `started_on`, then the lowest store
  number; an operator with no active lease keeps its refs on the operator, flagged `needs_review`
  "Processor customer not moved: no active lease"), re-point the operator's live `customer` and
  `payment_method` refs to that lease (`entity_type="lease"`, `entity_id=lease.id`; the
  external ids do not change, so Stax sees nothing); every **other** lease of that operator gets
  `shares_account_of = primary lease` (so its schedules, invoices and messages keep working on the
  existing customer and bank method) and `needs_review` with reason "Own processor customer
  needed" listed under Needs review. The operator's sync columns are copied to the primary
  lease. Audit: one `lease_billing.migrated` row per lease (entity lease) with the source
  operator id and whether refs moved. Backward: deletes `lease_billing` rows and re-points refs
  to the operator when exactly one lease held them (else `CANNOT_REVERSE`). Runs inside the
  transaction with `run_pending_checks` around the data step as 0051 does. Nothing in the
  migration calls the processor.
- **C9 The per-lease customers (Section 6).** A management command `create_lease_customers
  [--dry-run] [--limit N]` iterates the leases with `shares_account_of` set and no own customer,
  creates one processor customer per lease through `LEASE_SYNC` (the same job and check-before-
  create as a new lease), and reports counts; it never touches bank methods (none can be
  created by the app; the new customer has none until staff enter it in the dashboard or the
  operator enters it in the portal of H). A lease keeps `shares_account_of` until its own
  customer holds a usable bank method and staff press **Switch to own account** on the LEASE
  card (moves future schedules through the existing `move_autopay`; audited). The command's run
  on staging against the sandbox is a Publish Request after C's merge (a write to the
  processor); the live run is TODO-VERIFY-067. The dry run is part of the checkpoint report
  ("the migration's results on staging data": counts of leases, billing rows, refs moved,
  leases sharing, operators flagged).
- **C10 ADR-0015 and the docs.** Fable writes ADR-0015 (context: the owner's model — in Stax
  every customer is a store; decision: C1–C9; alternatives: keep one customer per operator and
  label invoices per store (rejected: the processor's receipts, schedules and payment methods
  cannot be split per store), duplicate the bank token per lease (impossible: a method belongs to
  one customer); consequences: `shares_account_of` as the only way to reuse a bank account
  without re-entry, the staged switch, the `Operator.email` uniqueness kept for portal login (H)).
  `docs/SCOPE.md` §4's "One Stax customer per operator" and caveat 1 are updated; `docs/
  CLAUDE.md` Rule 1 gets the C7 sentence; `docs/qa/PLAN-006-operators.md` is not edited (history).

### Batch D — messaging engine (Opus; Fable the tests, ADR-0016)

- **D1 Transport.** The existing `mail/gmail.py` (service account, domain-wide delegation,
  scope `gmail.send` only; the key from Key Vault `gmail-service-account-key`, already wired in
  Bicep as `GMAIL_SERVICE_ACCOUNT_JSON` when `mailEnabled`) stays the sender. The **From** address
  and the **Reply-To** come from `CompanySettings.sender_email` and `reply_to_email` (new fields,
  edited in Admin › Settings › Mail, audited); `GMAIL_SENDER` is retired from env guard, settings
  and `.env.example` (the Bicep parameter stays until Step 16 and is listed as a follow-up).
  `MAIL_ENABLED` keeps its meaning (false = nothing is ever handed to Gmail).
- **D2 Outbox mode.** Dispositions grow: `held`. `mail.get_sender()` returns `OutboxSender`
  when `MAIL_ENABLED` is false **or** the key is empty **or** `sender_email` is blank; it marks
  the message `held` with `held_reason="not_set_up"` ("not sent (mail not set up)"). On the
  sandbox environment (`STAX_ENV == "sandbox"`), a recipient not on the allowlist is `held`
  with `held_reason="sandbox"` ("held on sandbox"); the allowlist is `CompanySettings.
  sandbox_allowlist` (addresses, one per line; edited in Admin › Settings › Mail, empty by
  default — the owner adds their own address on staging; no personal address is seeded in
  source, §8). `CatchAllSender` and `MAIL_CATCH_ALL` are retired in favour of the allowlist
  (env guard and `.env.example` updated; `NullSender` goes). A held message is re-sendable from
  the Outbox once mail is set up ("Send now", admin).
- **D3 `OutboundMessage`.** Gains `lease` (FK, nullable for legacy rows), `channel` (`email`,
  `sms`; SMS has no sender and is refused at queue time with `ChannelUnavailable`), `rule`
  (the rule code: `A`…`F`, `final_statement`, `manual`), `occurrence` (int, 1; D's repeats
  count up), `rendered_subject`, `rendered_body` (stored now so the Outbox and the timeline show
  the message; `body_sha256` stays), `held_reason`, `scheduled_for` (the send-window time),
  `to_address` as today. The unique constraint becomes `(lease, charge_entity_type,
  charge_entity_id, rule, occurrence)` where `manual=False` ("one message per lease per charge
  per rule; re-runs never duplicate"); `step` and the `step_iff_ladder` constraint go with
  `CollectionStep` (D6). `MessageStatus` adds `held`.
- **D4 Templates.** `MessageTemplate` gains `name` (shown in the list), `channel`, `active`,
  `internal` (E's manager message). The placeholder set is exactly `{operator_name}
  {store_number} {store_name} {amount} {charge_name} {due_date} {debit_date} {retry_date}
  {days_late} {bank_last4} {return_reason} {pay_link} {store_link} {support_email}
  {company_name}`; `merge.FIELDS` replaces `LADDER_FIELDS` (the final statement keeps its own
  set); an unknown placeholder is refused on save with the field named and no value shown
  (`merge.problems`). `{operator_name}` is the operator's full name; `{pay_link}` is the
  portal's pay page for the charge once H exists and, until then, the signed statement link
  (documented in the Mail card); `{store_link}` is the staff store page URL; `{company_name}`
  and `{support_email}` come from `CompanySettings`; `{bank_last4}` is the lease's cached last
  four or "—"; `{retry_date}` and `{return_reason}` are empty outside F. Admin › Settings ›
  **Message templates**: list, edit subject and body, a **Preview** with a real store (a store
  select; renders with that lease's values and sample amounts; nothing is queued), audited
  `message_template.changed`. The Django admin registration goes.
- **D5 Timeline and Outbox.** Every queued message (sent, held, failed, suppressed) appears in
  the lease's store timeline (B6's union; kind "E-mail", the template name and the delivery
  result; the rendered body opens in a details element). **Admin › Outbox** (`ledger:outbox:
  page`, admin): every message with status, reason, recipient (masked below admin; full for
  admins), filters by status and date, "Send now" for held messages when mail is set up.
- **D6 The ladder retires.** `CollectionStep`, `collections.due_steps/queue_due`, the "Send a
  step now" form and the seeded templates `friendly_reminder`, `formal_notice`, `final_notice`
  are replaced by the six seeded templates and E's rules (migration 0060 drops
  `collection_steps`, deactivates the three old templates — rows kept for the audit history —
  and seeds A–F **exactly as written in the owner's prompt**, appended to this plan as §9; codes
  `reminder_autopay`, `reminder_due`, `overdue_day5`, `overdue_day10`, `internal_day30`,
  `returned_debit`). The charge page's manual send becomes "Send a message now" with a template
  select (manual rows, `rule="manual"`). `Operator.handled_personally` keeps suppressing
  operator-facing messages (held, reason `handled_personally`).
- **D7 Send window.** `Job` gains `run_after` (nullable datetime; `claim_next` skips rows with
  `run_after` in the future; payments migration 0019). A message queued outside 9:00–18:00
  America/New_York gets `scheduled_for` = the next 9:00 New York and its `MESSAGE_SEND` job
  `run_after` = that instant; inside the window it goes at once. The internal E message and the
  Outbox-held rows follow the same rule (held rows record their `scheduled_for` for the record).
- **D8 Channel abstraction.** `messaging.Channel` Protocol (`send(rendered) -> Disposition`)
  with `EmailChannel` (the mail sender) only; `OutboundMessage.channel` selects it; `sms` is
  refused at queue time with a clear sentence. No SMS provider.
- **D9 ADR-0016 (Fable).** Context, decision (D1–D8, E, F), alternatives (a third-party mail
  API — rejected, ADR-0010 stands; per-recipient opt-out — out of scope, consent fields exist),
  consequences (the body is stored; the ladder's `CollectionStep` is gone; the NSF fee and the
  retry are automatic for R01/R09 — supersedes ADR-0010 item "NSF only by staff" for those
  codes; staff keep "Debit again" for the rest).

### Batch E — reminder and overdue rules (Opus; Fable the tests)

- **E1 Engine.** `ledger/services/reminders.py` `queue_due(today) -> Summary`, called by
  `collections_tick` in place of `collections.queue_due`. Sources: `receivables.open_balances
  (today)` (rent, insurance, one-off incl. installments, deposits left out) plus rent charges
  of the coming three days for A. Per open row the rule due today:
  - **A** — the charge is a rent charge whose lease has an active rent `CollectionSchedule`
    (autopay) and `debit date − 3 == today` → A. Nothing else for rent on autopay while its
    debits succeed (B–E do not fire for such a charge until a return (F) or the schedule stops).
  - **B** — any other open charge, `due − 3 == today`.
  - **C** — `today − due == 5`.
  - **D** — `today − due == 10`, then `occurrence` 2, 3 … at `today − due ∈ {40, 50, 60, …}`
    while the balance is above zero (the 20-day gap between 10 and 40 is the owner's rule).
  - **E** (internal) — `today − due == 30`, once, to `CompanySettings.manager_emails` (one per
    line; when empty the message is `held` with reason `no_manager_list` and listed in the
    Outbox), `internal=True`, never to the operator.
  Catch-up: a rule whose day has passed is **not** sent later (no flood after a pause or at the
  first deploy: only the rule of `today` fires; the first staging run therefore sends D to
  charges exactly 10, 40, 50 … days late and nothing to the rest; reported at the second
  checkpoint with the Outbox listing). Idempotent by D3's constraint; `queue_due` runs
  `select_for_update` on the lease like `collections._queue` did.
- **E2 Stop and pause.** Paid, waived or voided charges are not open and get nothing; a message
  already queued whose charge closes before the send is `suppressed` (`ChargeClosed`, as
  today). An **open promise** on the lease with `promised_date >= today` pauses B–E for every
  charge of that lease (A still goes: the debit will happen); a broken promise (the day after
  its date, `contacts.broken_promises`) resumes the rules from the next due day. Plans apply per
  installment because each installment is its own one-off row in `open_balances`.
- **E3 No late fee.** Nothing creates a fee here; E's wording says nothing about fees.

### Batch F — returned bank debits (Opus; Fable the tests)

- **F1 Plain-words reasons.** `payments/stax/return_codes.py` `plain_words(code) -> str`:
  R01 "Insufficient funds"; R02 "The bank account is closed"; R03, R04 "The account number could
  not be found"; R08 "You asked your bank to stop this payment"; R09 "Uncollected funds"; R10,
  R29 "Your bank reports this debit was not authorized"; R16 "The account is frozen"; any other
  or missing code "Your bank returned the payment; please contact your bank for details".
  `RETRYABLE = {"R01", "R09"}`.
- **F2 At the return.** The transaction webhook's return transition (`_refresh_charge`) now
  calls `nsf.on_return(payment, today)` inside the same transaction: (a) the **$100.00 NSF
  fee** as its own one-off charge (`nsf_fee_first` fee type, activated by migration 0061;
  companion of the return, `source_payment`; one per returned payment by a unique partial index
  on `(source_payment, category=nsf_fee)`), `operator.nsf_return_count += 1`; (b) for R01/R09
  an **automatic re-debit**: a `REDEBIT` one-off charge for the original amount only, state
  `auto_pending`, `retry_date` = the 3rd business day after the notice date (Mon–Fri, US
  Federal Reserve holidays excluded; `ledger/services/business_days.py` with the 2026–2028
  holiday table), unless this original debit already had two re-debits within 180 days (count
  over the chain `source_payment → redebit charges → their returned payments`; then no retry,
  Needs review "Re-debit limit reached (Nacha)"); (c) template **F** queued at once (D7's window
  applies) with `{return_reason}`, `{retry_date}` and, for non-retryable codes, F's retry
  sentence replaced by "Please contact us within 2 business days to set up a new payment
  method." (the template holds the owner's text; the engine swaps that one sentence through a
  `{retry_sentence}`-free rule: the body is rendered from the seeded text and the sentence
  beginning "If we do not hear from you" is replaced — Opus implements it as a template
  variant `returned_debit_no_retry` seeded with the replacement sentence so admins see both;
  §8); (d) non-retryable codes set the lease's `needs_review` with reason "Returned debit
  ({code}): new payment method needed" and the existing `RETURNED_DEBIT` attention item.
- **F3 The retry's schedule.** The daily job (`nsf.create_due_redebits(today)`) creates the
  Stax single-occurrence schedule (existing `SCHEDULE_CREATE` path) for every `auto_pending`
  re-debit whose `retry_date` is tomorrow's business day or today and that is not stopped;
  nothing reaches the processor before then, so a stop is local. **Stop automatic retry**: a
  staff POST on the charge (`ledger:one_off_charges:stop_retry`, BILLING) marks the re-debit
  `stopped` (voided locally with reason "Stopped by staff"; audited `redebit.stopped`); a
  `ContactEntry` of kind call, e-mail or visit added by staff for that lease within 2 business
  days of the notice stops it the same way (hook in `contacts.add_entry`, audit reason "Staff
  contact logged"); a promise does too. After the two days the re-debit proceeds. If a
  re-debit's own payment returns, F2 runs again on it (limit counted on the original).
- **F4 Staff paths.** "Debit again" (`nsf.reschedule`) stays for non-retryable codes and for
  stopped retries; it no longer adds a fee when F2 already did (the unique index refuses a second;
  `waive_fee` becomes "Waive the NSF fee" on the fee charge — the existing waive). A **staff
  void** (`refunds.void_payment`, `void_manual_payment`) never queues a message. The returns
  queue page shows the retry state ("Automatic re-debit on Nov 3", "Stopped", "No retry: R10").
- **F5 Attention and dashboard.** `RETURNED_DEBIT` keeps firing for returns without a pending
  retry; a new kind `REDEBIT_PENDING` (low) lists upcoming automatic re-debits.

### Batch G — Aging report (Opus; Fable the reconciliation tests)

- **G1 By store.** `receivables.aging(today, selected: AgingFilter) -> AgingReport` groups
  `open_balances` rows **per store** (lease → store; a one-off charge without a lease groups
  under "No store" with its operator) with the operator's full name, buckets **Current** (due
  today or later), **1–30**, **31–60**, **61–90**, **90+** days past due, a row total and an open
  charge count; a totals row; **As of** = today (the only date; the report has no range so that
  its grand total is the dashboard's Unpaid KPI). Filters (A5's component): state (the store's
  state), operator, brand (B1), charge kind (rent / insurance / one-off / all), **include
  deposits** (off by default; on adds the deposits still owed from `payments_entry._deposits`,
  which are not in the KPI — the page says so under the total). Drill-down: the store name links
  to the store's Charges tab filtered to open charges (`?status=open`), each bucket amount links
  to the same tab with `?bucket=<n>` (the tab highlights the rows due in that range).
  Spreadsheet: `aging_export` with the new columns, same filters.
- **G2 Reconciliation (Fable's tests, Opus's code must satisfy them).** For a world with every
  kind (rent partial, insurance derived, installments, a re-debit with carried balance, a credit,
  a deposit, a waived and a voided charge, a lease-less one-off): grand total without deposits
  == `dashboard.figures().unpaid` == Σ `store_balances(store).open` over all stores; each store
  row == its Charges tab's open total; each bucket == the sum of the drill-down rows; the CSV
  totals == the page; a filter never changes a row's amounts, only which rows appear.
- **G3 Labels.** "Unpaid by age" page title becomes "Aging" (Reports menu "Aging"); the bucket
  header bug (`{{ label|money }}` on a text label, `reports/aging.html:14`) goes with the rewrite.

### Batch H — operator portal (Opus; Fable the isolation tests, ADR-0017)

- **H1 Principal.** New app `portal`: `PortalUser` (`operator` FK, `email` unique, lower-cased,
  must equal the operator's e-mail at invitation time; `password` via Django's hashers
  (`make_password`/`check_password`, `AbstractBaseUser` not used — no entanglement with the
  staff `AUTH_USER_MODEL`), `is_active`, `invited_by`, `invited_at`, `activated_at`,
  `disabled_at`, `last_login_at`, `theme` light/dark/system), `PortalToken` (`user` FK, `kind`
  invite|reset, `token_hash` sha256, `expires_at`, `used_at`; invitation 7 days, reset 1 hour;
  compared with `hmac.compare_digest`; one live token per kind — issuing a new one voids the
  old), `PortalLoginAttempt` (e-mail hash, IP, at, ok). Lockout: 5 failures per e-mail **or**
  per IP in 15 minutes → the login answers "Try again in n minutes" for 15 minutes without
  checking the password (the same sentence for an unknown e-mail; no account enumeration).
  Session: a separate cookie `portal_sessionid` (path `/portal/`, HttpOnly, Secure outside
  local, SameSite Strict) through `portal.middleware.PortalSessionMiddleware` (Django's
  `SessionStore`, the DB backend), 30 min idle, 12 h absolute, rotated at login; the staff
  session and middleware never see it. `AuthRequiredMiddleware`, `MfaRequiredMiddleware`,
  `RoleEnforcementMiddleware` exempt the `portal:` namespace, and `portal.middleware.
  PortalAuthMiddleware` requires a signed-in portal user for every `portal:` route except
  `PORTAL_PUBLIC_ROUTE_NAMES = {portal:login, portal:invite, portal:reset_request,
  portal:reset_confirm, portal:logout}`; the registry gains `PORTAL_ROUTE_NAMES` and the
  classification test pins all three sets (staff public set unchanged: five names).
- **H2 Invitations (staff).** The operator page's PORTAL card: "Invite to the portal" (admin or
  billing; requires the operator's e-mail; creates the `PortalUser` inactive + an invite token
  and queues a message through D on the `portal_invite` template (seeded in H, internal
  placeholder `{set_password_link}` allowed only in that template) to the **operator's** e-mail,
  not a lease's), "Resend invitation", "Disable" / "Enable", "Reset password" (sends the reset
  message), the last sign-in. Audited `portal_user.invited|disabled|enabled|reset_sent`.
  Statement-link revoke stays on the card.
- **H3 What they see.** `portal/base.html` (its own light shell: the 12g tokens, phone-first,
  a top bar with the company name, the store switcher, the theme switch, Sign out; no staff
  navigation, no PWA manifest), pages: **Home** (all of the operator's stores from `Lease.
  operator` incl. ended leases marked "ended"; per store the open balance), **Store** (open
  charges with due dates and balances, autopay status "on"/"off" read-only, the bank on file
  "····4821" or "none"), **Invoices** (the lease's invoices, number, date, amount, status),
  **Payments** (settled, pending, returned, with method and last four), **Statement** (the
  existing per-operator statement PDF, scoped to the portal user's operator), **Pay** (H4),
  **Bank account** (H5), **Account** (e-mail read-only, change password, theme). Every query
  goes through `portal/services.py` whose functions take the `PortalUser` and filter by
  `operator_id`; no view touches a model directly (reviewed in Gate 3).
- **H4 Pay by bank debit.** "Pay" on a charge or "Pay the whole balance" on a store: the portal
  user confirms the amount and the bank last four (the lease's linked method; no method → the
  Bank account page first); the server creates a `PortalPayment` (user, lease, charges, amount,
  `creation_key`, status requested|paid|failed|refused) and queues `PORTAL_PAY` per charge
  (worker-only: `one_off_invoices.pay` / the rent equivalent through `pay_invoice`, the stored
  idempotency ID, one attempt, no automatic retry, the `STAX_MAX_TXN_AMOUNT` limit, behind
  `STAX_PAY_INVOICE_ENABLED`); the page polls the outcome (Charge now's pattern) and says
  "Submitted — bank debits take a few days to settle". Rule 2 (ADR-0017) widens pay-an-invoice
  to "an admin's Charge now and a portal user's own payment", still one attempt per request.
  On the sandbox ACH is off: with the flag off the page says "Online payment is not available
  yet" and the e2e tests run on the fake provider; TODO-VERIFY-068.
- **H5 Bank account.** "Add a bank account" / "Replace" on a store: C7's hosted fields with the
  lease's customer, `LEASE_METHOD_ATTACH` verifies and links; the old method is superseded (its
  Stax object is not deleted — the app has no delete; TODO-VERIFY-069 whether a superseded
  method should be removed in Stax). Autopay cannot be toggled; the page says "Automatic debits
  are set up by our staff. Contact {support_email}." A lease that shares another's account
  (`shares_account_of`) shows that store's last four and "Shared with #1991"; replacing it
  changes it for every sharing lease, said in the confirmation.
- **H6 Isolation.** Every portal URL takes ids from the URL only after the service filtered by
  the user's operator; a foreign id is a 404 (never 403, no enumeration). Fable's tests: for
  every `portal:` route with parameters, operator A's user gets 404 on operator B's store, lease,
  charge, invoice, payment, statement and bank page; A's listing pages never contain B's store
  numbers (whole-token checks); a disabled user is signed out on the next request; an expired or
  used token is refused; a staff session does not open a portal page and the reverse; CSRF on
  every POST; the lockout counts per e-mail and per IP; the reset form answers the same for
  unknown e-mails.
- **H7 ADR-0017 (Fable).** Context (the owner's 2026-10-29 decision; what ADR-0004 item 2 and
  Rule 1 said), decision (H1–H6 and C7), alternatives (Stax's hosted invoice page — rejected: the
  owner wants the operator inside our portal with all stores; staff-only bank entry — kept as the
  fallback path), consequences (the Rule 1 fence text, Rule 2's second allowed caller, `docs/
  CLAUDE.md` "Customers never log in here" → "Staff sign in at `/`; operators sign in at
  `/portal/` with a separate principal", `docs/SCOPE.md` §2 and §4 updated, Nacha authorisation
  text on the bank-entry page is the processor's — TODO-VERIFY-006 remains open).

### Batch I — bank deposit matching with a Plaid connector (Opus; Fable the tests, ADR-0018)

- **I1 Module and source.** New app `banking`: `BankTransactionSource` Protocol (`sync(connection)
  -> SyncResult` adding `BankTransaction` rows idempotently by `external_id`), `PlaidSource`
  (`banking/plaid/client.py`: `httpx` as `finance/usps.py` — one client per call, 10 s timeouts,
  no redirects, one retry on 5xx, logs host/status/latency only; endpoints `/link/token/create`,
  `/item/public_token/exchange`, `/transactions/sync` with the stored cursor, `/accounts/get`;
  env `PLAID_ENV` sandbox|development|production, `PLAID_CLIENT_ID`, `PLAID_SECRET`,
  `PLAID_TOKEN_KEY` — a pair-or-nothing guard like USPS; Key Vault names `plaid-client-id`,
  `plaid-secret`, `plaid-token-key`; redacted in logs and Sentry) and `FakeSource` (tests and
  local: transactions from a fixture). `BankConnection` (one row: `item_id`, `account_id`,
  `institution`, `mask` (last four of the bank account **of the business**, Plaid's own field),
  `access_token_encrypted` (Fernet with `PLAID_TOKEN_KEY`; never logged, never in the admin),
  `cursor`, `linked_by`, `linked_at`, `last_sync_at`, `last_error` class name only, `active`).
  `BankTransaction` (`source`, `external_id` unique, `posted_on`, `amount` (credits only are
  kept), `description`, `counterparty`, `kind` wire|zelle|ach_credit|check|other from Plaid's
  category/payment channel mapping, `status` new|confirmed|ignored|split, `confidence`
  exact|strong|weak|none, `suggested_lease`, `suggested_charge_type/id`, `confirmed_by/at`,
  `ignored_by/at`, `ignore_reason`, `allocation_group` when confirmed, `undone_at`).
  `SenderAlias` (`lease` FK, `text`, `normalised` upper-cased collapsed whitespace, unique per
  lease, `created_by`).
- **I2 Off by default.** `CompanySettings.bank_deposits_enabled` (false) and
  `bank_auto_confirm_exact` (false), edited in Admin › Settings › **Bank deposits**; the card
  shows whether the keys exist (names only: "Plaid keys: present / missing in this
  environment"), the connection (institution and mask) and **Connect a bank account** (admin;
  Plaid Link: the server creates a link token, the page loads Plaid's Link script from Plaid's
  documented CDN URL, the browser returns a `public_token`, the server exchanges it and stores
  the encrypted access token; audited `bank_connection.linked`) and **Disconnect**. The daily
  job and a "Fetch now" button sync only when enabled, keys exist and a connection is active;
  otherwise the Bank deposits page says why. Nothing in `infra/` sets the Plaid names (the
  owner adds them with Step 16's parameters; a Publish Request when the time comes).
- **I3 Matching.** `banking/matching.py` `suggest(transaction, today) -> Suggestion` over
  `receivables.open_balances(today)`: **exact** — an invoice number (`INV-\d{4}-\d{4,}`) in the
  description belonging to an open charge (via `Invoice.rent_charge|one_off_charge`, or a
  builder invoice's lines) whose balance equals the amount; **strong** — a `SenderAlias` whose
  normalised text occurs in the normalised description or counterparty, and the amount equals
  one open charge of that lease or that lease's whole open balance; **weak** — the amount equals
  exactly one open charge anywhere; else **none**. Confidence order exact > strong > weak;
  ties within a level → none with the candidates listed. Auto-confirm applies to exact only and
  only when the setting is on (audited `bank_transaction.auto_confirmed`).
- **I4 Staff screen.** "Bank deposits" under Admin (billing): each new credit with date, amount,
  description, the suggestion (store, charge, confidence badge), **Confirm** (records through
  `payments_entry.record_entry` for the lease with method from `kind` (wire → wire, zelle →
  zelle, check → check, ach_credit and other → other with the kind in the note), `reference`
  = the bank transaction id, note "Bank deposit matched: {description}", `paid_on` = `posted_on`,
  `creation_key` from the transaction id; partials allowed, the remainder kept as credit when
  ticked), **Choose another** (the store's open charges), **Split** (several charges of one or
  more stores, or several checks: the allocation form with the amount fixed to the transaction's),
  **Ignore** (reason required: "internal transfer", "not rent", other + text). A transaction is
  applied once (`status` and a unique partial index on `allocation_group`). On Confirm, when
  the counterparty is not yet an alias of that lease: "Remember FATIH TRADING LLC for #1991
  Istanbul" (a checkbox, on by default). Aliases are listed and editable on the lease's billing
  details dialog (C). **Undo** on a confirmed row voids its payments through
  `refunds.void_manual_payment` (one per allocation line; reason "Bank deposit match undone")
  and returns the transaction to `new`; audited `bank_transaction.confirmed|ignored|undone`.
- **I5 Memo line.** `invoices/_invoice.html` and the portal's Pay and Invoice pages show
  "Paying by wire or Zelle? Put {invoice_number} in the memo." (the real number; drafts show
  nothing).
- **I6 ADR-0018 (Fable).** Context, decision (I1–I5), alternatives (bank CSV import — kept as a
  possible second source, not built; Stax's deposit report — different thing: processor
  settlements), consequences (a long-lived bank credential encrypted at rest with a Key Vault
  key; the module is dark until the owner enables it; Plaid's sandbox for tests), the three new
  secret names in `.env.example` and `docs/CLAUDE.md`'s secrets table. The page title is "Bank
  deposits" (the owner's word for this module; the existing "Bank account check" report keeps
  its name and the `docs/STATUS.md:1362` rule is noted as superseded for this page only).

## 4. Interface contract

- **A:** `accounts.UserPreference.payment_status_period ∈ {weekly, monthly}` (default weekly);
  `POST /settings/payment-status/` body `period=` → 302/400; dashboard query `?ps=weekly|monthly
  &ps_at=YYYY-MM-DD`; `dashboard.period_for(kind, at, today) -> Period`, `payment_status(period,
  today) -> PaymentStatusCard`; `{% filters %}` tag (`form`, `search_name`, `sort_options`,
  `results_id`); `filters.js` contract: the form `id="filters"`, the results `id="results"`.
- **B:** `ledger.Brand(name unique, sort_order, active)`; `Store.brand` nullable FK PROTECT;
  `ledger:settings:index` GET admin, `ledger:settings:brand_add|brand_save|brand_toggle` POST
  admin; `Note(entity_type, entity_id, lease?, body, author, pinned, pinned_at, edited_at,
  updated_at, deleted_at, deleted_by, delete_reason)`; `notes.add|edit|pin|unpin|delete|
  pinned_for`; `ledger:notes:add|edit|pin|delete` POST BILLING; `timeline.entries(lease, show)`,
  `?show=all|notes|contacts|emails`; `detail_cards.collection_state(charge) -> State(code ∈
  {collected, partial, uncollected, unbilled}, text, paid, due, url)`.
- **C:** `ledger.LeaseBilling(lease 1:1, company_name, email, phone, address…, bank_last4,
  shares_account_of?)`; `ExternalRef(entity_type="lease", external_kind ∈ {customer,
  payment_method})`; `leases.customer_ref(lease)`, `leases.linked_payment_method(lease)`,
  `leases.billing_for(charge)`; `payments.lease_sync.{sync_lease, link_lease, customer_input}`;
  `JobKind.LEASE_SYNC|LEASE_LINK|LEASE_METHODS_REFRESH|LEASE_METHOD_ATTACH` on `JobEntity.LEASE`;
  `payments.hosted_fields.HostedFields` Protocol (`config_for`, `verify`), `get_hosted_fields()`;
  `POST /leases/<pk>/billing/` (dialog save), `POST /leases/<pk>/bank/attach/` (`payment_method_
  id` only; any other field is refused), `POST /leases/<pk>/bank/switch-own/`; settings
  `STAX_WEB_PAYMENTS_TOKEN` (optional; blank = hosted fields unavailable); the command
  `create_lease_customers [--dry-run] [--limit N]`; migration 0059's audit `lease_billing.
  migrated`; refusal code `NO_LEASE_BILLING`.
- **D:** `OutboundMessage` fields of D3; `MessageStatus.HELD`; `CompanySettings.sender_email,
  reply_to_email, manager_emails, sandbox_allowlist`; `merge.FIELDS` (15 names) and
  `merge.render` unchanged in behaviour; `messaging.queue(rule, lease, charge, template,
  context, *, occurrence=1, manual=False, actor=None) -> OutboundMessage | None` (None on the
  unique constraint); `messaging.send(message_id, now)`; `mail.OutboxSender`; `Job.run_after`;
  `ledger:settings:templates|template_edit|template_preview|mail_save`, `ledger:outbox:page|
  send_now`; template codes `reminder_autopay, reminder_due, overdue_day5, overdue_day10,
  internal_day30, returned_debit, returned_debit_no_retry` (+ `portal_invite`, `portal_reset` in H).
- **E:** `reminders.queue_due(today) -> Summary(queued_by_rule, paused, skipped)`;
  `reminders.rule_for(row, today) -> Rule | None`; `reminders.PAUSED_BY_PROMISE`.
- **F:** `return_codes.plain_words(code)`, `RETRYABLE`; `nsf.on_return(payment, today)`,
  `nsf.create_due_redebits(today)`, `nsf.stop_retry(charge, actor, reason)`;
  `business_days.add(date, n)`, `is_business_day(date)`; `OneOffCharge.retry_date`,
  `retry_state ∈ {auto_pending, scheduled, stopped, done}`; `ledger:one_off_charges:stop_retry`
  POST BILLING; attention kind `REDEBIT_PENDING`; migration 0061 activates `nsf_fee_first`.
- **G:** `receivables.AgingFilter(state, operator, brand, kind, include_deposits)`,
  `receivables.aging(today, selected) -> AgingReport(rows: list[AgingStoreRow], totals,
  as_of)`, buckets `("current","1_30","31_60","61_90","over_90")`; `?state=&operator=&brand=
  &kind=&deposits=1`; the store Charges tab accepts `?status=open&bucket=<key>`.
- **H:** app `portal`: models of H1; `portal:login|logout|invite|reset_request|reset_confirm`
  public, `portal:home|store|invoices|payments|statement|pay|pay_status|bank|account|password`
  portal-authenticated; `PortalSessionMiddleware`, `PortalAuthMiddleware`; `registry.
  PORTAL_ROUTE_NAMES`, `PORTAL_PUBLIC_ROUTE_NAMES`; `portal.services.*(user, …)`;
  `PortalPayment`, `JobKind.PORTAL_PAY`; `ledger:operators:portal_invite|portal_disable|
  portal_enable|portal_reset` POST BILLING; the `{pay_link}` placeholder resolves to
  `portal:pay` for the charge.
- **I:** app `banking`: models of I1; `BankTransactionSource`, `PlaidSource`, `FakeSource`,
  `get_source()`; `matching.suggest(transaction, today) -> Suggestion(confidence, lease,
  charge_type, charge_id, candidates)`; `banking:page|confirm|choose|split|ignore|undo|
  fetch_now|link_token|link_exchange|disconnect`; settings `PLAID_ENV, PLAID_CLIENT_ID,
  PLAID_SECRET, PLAID_TOKEN_KEY`; `CompanySettings.bank_deposits_enabled,
  bank_auto_confirm_exact`.

## 5. Test plan (written before the code)

Every batch: the full suite on sqlite and the CI split on PostgreSQL, the visual suite (new
pages get baselines in both themes at both widths; changed pages regenerated and compared on
sqlite), makemigrations check on both databases, the secret scan, the provider-name fence.

- **A:** period maths (a week crossing a month, DST weeks, a year boundary, `ps_at` on a
  Saturday/Sunday, `ps_at=2026-02-30`, `ps=<script>`); the stored choice wins without `?ps=`;
  a viewer may save; GET never writes; the donut's count equals the boxes' sum in both periods;
  vacant rows (never leased, 0 days, 23 days, leftover balance, an ended lease yesterday, the
  admin-only link); the filters component on every list page (count, chips remove one
  parameter, Clear keeps search and sort, Apply keeps all; without script Enter submits; the
  `fetch` swap keeps the table's ids; a 500-character search; a chip for an unknown value);
  every existing list test URL still answers 200 with the same rows; the five tiles' values
  equal the old totals (a world with every status; zero → muted; Returned red only when > 0);
  the header-alignment rule over every table of every page in the visual world (both roles).
- **B:** migration 0058 forward on a world with every old store type (three seeded, five
  inactive, stores cleared and flagged, the attention item present and cleared by choosing a
  brand) and backward; Brand CRUD (duplicate name, empty name, deactivating a brand in use,
  the pill, the filter chip, the CSV header); the LEASE card's twelve states (sign fee ×4,
  insurance ×4, previous year note ×3, open balance 0/positive) incl. a lease with no insurance
  charge and a policy year boundary (July 1 if that is the year start — read from
  `services/insurance.py`); notes: add/edit/pin/delete by author, by another billing user
  (refused), by admin, "edited", soft delete in the audit with the body, the NOTES card's cap
  of five and order, a note with payment data (refused), 2001 characters, the timeline union
  order and the four filters, "E-mails" empty state; the rename everywhere (a grep test that no
  template or help page says "Contact log").
- **C:** migration 0059 on a world with: an operator with one lease and refs (moved), one with
  three leases (primary chosen by `started_on` then store number; two sharing and flagged), one
  with no active lease (refs stay, flagged), one with no refs, an ended lease (billing row, no
  refs), a lease whose operator is null; backward with one lease (restores) and with several
  (`CANNOT_REVERSE`); audit rows per lease; the two helpers through one hop and the cycle CHECK;
  every caller in C5 (autopay enable/move/stop on a sharing lease uses the primary's customer;
  an invoice for a sharing lease names the primary's method; Add payment shows the lease's last
  four; the enrolment page says the lease's e-mail; collections/messages address the lease's
  e-mail; the operator statement's Bill to per section); the operator form without the removed
  fields (posting `kind=company` is ignored, not an error); New lease's billing step (Copy from
  operator is client-side: a POST with empty fields keeps them empty; "same account as" lists
  only leases with a usable method; the Later path creates a customer and no method); Transfer
  keeps the old lease's refs; hosted fields: the attach endpoint refuses any field but the id,
  a method of another customer (verify fails → refused, audited, nothing written), a card
  method, a replay of the same id, a fake id on the fake provider (linked, last four cached,
  the old ref superseded); the token setting blank → the page's "not set up" text and no script
  tag; the Stax host in the page-level CSP meta; `test_stax_isolation` allow-list for the two
  templates; `create_lease_customers --dry-run` counts and the real run on the fake provider
  (one customer per sharing lease, idempotent on a second run, `--limit`); "Switch to own
  account" refused without a usable method on the own customer, then allowed and schedules moved.
- **D:** `OutboxSender` dispositions (no key → held not_set_up; sandbox + not allowlisted →
  held sandbox; allowlisted → sent; live env → no allowlist); the allowlist parser (blank lines,
  case, a line with two addresses → refused on save); `held` rows in the Outbox with "Send now"
  (admin; refused while not set up); the unique constraint (same lease/charge/rule/occurrence
  twice → one row; occurrence 2 allowed; a manual row never collides); rendered body stored and
  shown in the timeline; the 15 placeholders render; `{unknown}` refused on save with the name
  and no value; the preview with a real store queues nothing; sender/reply-to in the MIME
  headers; the send window (queued 18:01 NY → `scheduled_for` next 9:00, `run_after` set and
  `claim_next` skips; 8:59 → 9:00; DST change days; a job with `run_after` null runs); SMS
  refused; `CollectionStep` gone (migration 0060 forward/backward with the old templates
  deactivated and the six seeded byte-for-byte equal to §9); the old "Send a step now" tests
  replaced by "Send a message now"; the handled-personally hold.
- **E:** a calendar of days around one charge for each rule (A at −3 only with an active rent
  schedule; B at −3 without; C at +5; D at +10, +40, +50 and not at +20/+30/+45; E at +30 once to
  the manager list; nothing on other days); idempotent re-run the same day; paid/waived/voided
  before the send → suppressed; an open promise pauses B–E and not A, a broken promise resumes;
  installments per row; a lease-less one-off through `billing_for`; the first run on a world of
  old overdue charges sends only today's rules; `handled_personally`; an operator with no
  billing e-mail → held `no_address` and listed.
- **F:** each return code's plain words; R01 → fee + auto re-debit with the 3rd business day
  over a weekend and over a Federal holiday; a second R01 on the re-debit → a third re-debit
  refused by the 180-day/2 limit; R10 → fee, no retry, needs review, the no-retry sentence;
  a missing code; the fee once per returned payment (webhook replay); "Stop automatic retry"
  by staff, by a logged call within 2 business days, not by a call on day 3; the schedule
  created only on the eve; a stopped retry creates no schedule; "Debit again" after a stop
  adds no second fee; a staff void queues nothing; attention kinds; the returns page's states.
- **G:** G2's reconciliation world; bucket edges (due today → Current; 1, 30, 31, 60, 61, 90,
  91 days); filters (state, operator, brand incl. "no brand", kind, deposits on/off and the
  note under the total); the drill-down URLs and the Charges tab's `?bucket=`; the CSV equals
  the page; the `too_slow` guard still answers; the PostgreSQL query budget for 500 stores.
- **H:** H6's isolation matrix over every portal route (generated from the URL conf so a new
  route cannot escape the test); the invitation (7-day expiry, a used token, a second invite
  voids the first, an operator without e-mail refused, the e-mail must match); login (wrong
  password, disabled user, lockout by e-mail and by IP, the same sentence for unknown e-mails,
  the count resets on success, the cookie's flags, rotation, idle and absolute timeouts); reset
  (1 hour, same sentence for unknown e-mails, the old password stops working); the staff
  middleware chain untouched (the staff public set still exactly five names; a portal cookie on
  a staff URL is ignored); Pay (amount equals the charge or the store's balance only — a tampered
  amount is refused; a charge of another operator → 404; the flag off → the page's sentence;
  with the fake provider → `PortalPayment` paid, the payment settled, the charge paid, audited;
  a replay of the `creation_key` → one job); Bank (attach on the fake, replace supersedes, a
  sharing lease's warning, autopay not toggleable: no such route); the statement PDF scoped;
  the portal pages in both themes at both widths in the visual suite (a portal world: one user,
  two stores); PWA manifest absent on portal pages; an `X-Frame-Options`/CSP check on the
  bank page.
- **I:** `FakeSource` sync idempotent on `external_id`; matching: exact by invoice number and
  equal balance, exact refused when the amount differs (→ weak/none), strong by alias and one
  charge, strong by alias and the whole balance, weak with exactly one candidate, none with two
  candidates (listed), debits ignored; Confirm records through `record_entry` with the method
  mapping, reference = transaction id, partial with credit kept, applied once (a second Confirm
  refused), the alias offered and saved, the alias normalisation; Split across two stores and
  three checks; Ignore with reasons; Undo voids each line through the existing path and the
  row returns to new; auto-confirm only exact and only when on; the settings card's key-presence
  text without values; the Plaid client against `httpx.MockTransport` (link token, exchange,
  sync with cursor paging, a 5xx retried once, a 4xx not retried, logs carry no body); the access
  token round-trips through Fernet and never appears in `repr`, logs, the admin or the page; the
  pair-or-nothing guard; the module dark when disabled (the page's sentence, the daily job skips,
  no HTTP); the memo line on an invoice and in the portal, absent on a draft.
- **Adversarial (Fable, every batch):** oversize and unicode inputs on every new text field;
  payment-data strings in notes, aliases, ignore reasons and template bodies (refused); concurrent
  double-submit of Confirm, Pay, Attach and Note save (one effect); a migration run twice; a
  template body of 20 000 characters; a `{pay_link}` for a charge whose lease has no portal
  user (the statement link); an invoice number of another operator in a deposit description
  (exact requires the open charge, so it is weak at most); a portal user whose operator is
  archived (signed out); `ps_at` far in the past (bounded like `?range=`).

## 6. Risks

- **C is the pivot.** Every billing path changes its subject from operator to lease; a missed
  caller bills the wrong details silently. Mitigation: C5's list is the review checklist; the
  gate greps for `operator.email`, `customer_ref(operator`, `linked_payment_method(operator` and
  `operator_sync` after C and expects none outside history and tests.
- **Migration 0059 re-points live processor refs.** The external ids do not change and Stax is
  not called, so a mistake is local and reversible for single-lease operators; multi-lease
  operators share by design until the command and the staff switch. The dry run on staging data
  is in the checkpoint. The migration time on 500 leases is small (one pass).
- **Hosted fields** are new attack surface: the server accepts only an id and verifies it with
  the processor before linking; the web payments token is a secret in Key Vault; the sandbox
  cannot tokenize, so the real flow is TODO-VERIFY and reviewed by the owner on live later.
- **Messaging retroactivity.** The first daily run with the engine must not flood: only today's
  rules fire (E1); Outbox mode and the allowlist hold everything on staging anyway. On live the
  owner enables mail after the Workspace setup (a Publish Request for the secret's Bicep
  parameter is not part of this step).
- **The body is stored** (D3) where ADR-0010 kept a hash only; the Outbox needs it; the body
  holds no payment data by the `check_payment_data` rule and the recipient is masked below admin.
- **Automatic re-debits move money without a staff click** (F). Bounded by the code set
  (R01/R09), the 2-day stop window, the limit of two within 180 days, the eve-of-retry schedule
  creation (nothing at the processor before day 2), the `STAX_MAX_TXN_AMOUNT` limit and the
  audit trail. ADR-0016 records the change from ADR-0010.
- **Portal** is the first non-staff principal: a separate cookie, a separate middleware, services
  that filter by operator, 404 on foreign ids, lockout, token hashing, and a generated isolation
  matrix. The staff chain's tests pin that nothing changed for staff.
- **Plaid** adds a long-lived bank credential: encrypted at rest with a Key Vault key, the
  module dark until enabled, no bank account numbers of operators (the business's own `mask`
  only), read-only scopes (transactions). The dependency question is closed: `httpx` and
  `cryptography` are already locked.
- **Scope size.** Nine batches; each PR stands alone and passes the gate; Part 2's batches
  depend on C's model and D's engine only in the order given. Fresh Opus sessions per batch
  (and a split in C: C1–C6, then C7–C9) keep sessions short.
- **CI time.** New visual pages (portal ×2 themes ×2 widths ×~6 pages, Admin › Settings, Outbox,
  Bank deposits, Aging) add about 40 shots (~1 min); the migration shards gain four
  migrations' round trips — rebalance the shard table if any shard passes 15 minutes locally.

## 7. Acceptance

- A: no fifth row; Payment status with its own Weekly|Monthly pill, stepping, period text,
  remembered per user; vacant rows as specified; the search+Filters component on every list page
  with count, chips, Clear/Apply, bottom sheet on phones, results as you type; five tiles; every
  numeric header right-aligned (test-enforced); QA-012g complete; four dark built shots committed.
- B: Brand everywhere with exactly the three options, editable in Admin › Settings; stores with
  retired types flagged "Brand needs choosing"; the LEASE card rows and states; "Notes &
  follow-ups" everywhere; store notes with author, time, pinning, NOTES card, author/admin
  edit-delete, "edited", deletions audited; the four-way timeline filter.
- C: the operator form with four fields; billing details per lease with the processor customer
  per lease; "Copy from operator" manual; "Use the same account as #…"; invoices, statements,
  autopay, Add payment and messages on the lease's details; the LEASE card's billing rows and
  "Edit billing details"; migration 0059 copies once; the command creates per-lease customers on
  the sandbox on the owner's yes; ADR-0015 and the docs updated; checkpoint 1 delivered.
- D: Outbox mode until the secret exists; the sandbox allowlist; sender/reply-to from company
  settings; templates with the closed placeholder set, preview, refusal; the six templates seeded
  byte-for-byte; every message in the timeline; the send window; idempotent per lease/charge/rule.
- E: rules A–E exactly; stop on paid/waived/voided; promise pause and resume; per installment;
  no late fee.
- F: template F at once with plain words; the fee once; R01/R09 automatic re-debit on the 3rd
  business day, two per original within 180 days; staff stop and contact stop within 2 days;
  other codes → no retry, Needs review, the replacement sentence; staff voids silent.
- G: Aging by store with the five buckets, totals, As-of, five filters, drill-down, CSV; the
  reconciliation tests pass.
- H: `/portal/` with its own login, invitation (7 days), reset, lockout, timeouts; the operator
  sees all their stores and only theirs; pay by ACH and bank add/replace through hosted fields
  (mock on sandbox, TODO-VERIFY); autopay read-only; staff invite/disable from the PORTAL card;
  phone-first, both themes; the generated isolation matrix passes; ADR-0017 and the docs.
- I: the Bank deposits module dark by default; the Plaid connector behind the setting and the
  keys; Link by an admin; matching with the four levels; Confirm/Choose/Split/Ignore/Undo; aliases
  on the lease's billing details; auto-confirm setting; the memo line; audited; ADR-0018.
- Every batch: QA-014 section with scope, departures, findings, gate, shots, verdict; checkpoint 2
  with the staging Outbox listing and the TODO-VERIFY list; then the Publish Request.

## 8. Departures and questions carried to the checkpoints

- **No Invoices list page exists**; the filters component goes on the pages that exist. An
  Invoices list is not built unless the owner asks (checkpoint 1).
- **"Sort" menus** appear only where a page sorts today (the arrears page); others gain none.
- The user menu's "Settings" is renamed **"Preferences"** so that "Admin › Settings" is the one
  page called Settings.
- **Sandbox allowlist default:** the owner's address is not seeded in source (personal data in
  a migration or parameter file); the allowlist is empty until the owner adds it in Admin ›
  Settings › Mail on staging.
- **F's replacement sentence** is a second seeded template `returned_debit_no_retry` (the
  owner's F text with the one sentence swapped) so admins can edit both; the engine picks by code.
- **"Use the same account as #1991"** is implemented as sharing the other lease's processor
  customer and bank method (`shares_account_of`), because a bank method belongs to one processor
  customer and cannot be copied; a lease moves to its own customer when a method exists there
  ("Switch to own account"). Multi-lease operators therefore share after the migration until the
  command and the switch. ADR-0015.
- **Hosted fields on staging** cannot tokenize (sandbox has no tokenization gateway); the bank
  step shows "not set up" and staff keep the Link form; TODO-VERIFY-066.
- **Catch-up:** reminder rules fire only on their day; a charge already 15 days late at the
  first run gets D on day 40, not C or D immediately.
- **Lease-less one-off charges** bill the operator's first active lease; none → `NO_LEASE_BILLING`.
- **Aging "No store" row** for lease-less one-off charges keeps the grand total equal to the KPI.
- **Plaid access token at rest** is encrypted with a key from Key Vault (`plaid-token-key`, a
  third secret name beyond the owner's two) rather than stored in clear.

## 9. The six templates to seed (verbatim from the owner's prompt; `{company_name}` is ours)

```
A — Subject: Rent for #{store_number} {store_name} will be debited on {debit_date}
Hello {operator_name},
This is a reminder that your rent of {amount} for store #{store_number} {store_name} will be debited from your bank account ending in {bank_last4} on {debit_date}.
Please make sure the funds are available. If anything has changed with your account, reply to this e-mail before {debit_date}.
Thank you,
{company_name}

B — Subject: Payment due {due_date} — #{store_number} {store_name}
Hello {operator_name},
A payment of {amount} for {charge_name} at store #{store_number} {store_name} is due on {due_date}.
You can pay here: {pay_link}
If you have already paid, thank you, and please ignore this message.
{company_name}

C — Subject: Payment overdue — #{store_number} {store_name}
Hello {operator_name},
We have not yet received {amount} for {charge_name} at store #{store_number} {store_name}, which was due on {due_date}.
Please pay as soon as possible: {pay_link}. If you need more time, reply to this e-mail and let us know when we can expect the payment.
{company_name}

D — Subject: Notice: {amount} overdue — #{store_number} {store_name}
Hello {operator_name},
The payment of {amount} for {charge_name} at store #{store_number} {store_name} is now {days_late} days overdue (due {due_date}).
Please pay today at {pay_link} or contact us at {support_email} to arrange payment. Continued non-payment may affect your lease.
{company_name}

E (internal) — Subject: 30 days overdue: #{store_number} {store_name} — {amount}
{operator_name} has not paid {amount} for {charge_name} (due {due_date}). Reminders were sent on day 5 and day 10. Open the store: {store_link}

F — Subject: Your payment for #{store_number} {store_name} was returned
Hello {operator_name},
The bank debit of {amount} for {charge_name} at store #{store_number} {store_name}, made on {debit_date}, was returned by your bank.
Reason: {return_reason}
A returned-payment (NSF) fee of $100.00 has been added to your account. The original amount is still owed.
If we do not hear from you within 2 business days, the payment of {amount} will be debited again automatically on {retry_date}. To change this, contact us at {support_email} before then.
{company_name}
```

`returned_debit_no_retry` = F with the sentence "If we do not hear from you within 2 business
days, the payment of {amount} will be debited again automatically on {retry_date}. To change
this, contact us at {support_email} before then." replaced by "Please contact us within 2
business days to set up a new payment method."

## 10. Section 6 actions in this step (each its own Publish Request or standing authorisation)

- Push, PR and merge per batch: covered by the owner's written authorisation in the prompt,
  in order, CI green, previous deploy verified; stop on any red.
- `create_lease_customers` on staging against the sandbox (after C's deploy): a Publish Request.
- A staging run of the daily job for checkpoint 2's Outbox listing: the job already runs daily
  at 09:00 New York; reading the Outbox is read-only. A manual trigger of the job is a Publish
  Request if the owner wants it sooner.
- Key Vault secrets (`stax-web-payments-token`, `plaid-client-id`, `plaid-secret`,
  `plaid-token-key`, the Gmail key's presence) and their Bicep parameters: the owner's actions;
  nothing in this step reads or writes a vault.
- Plaid Link against a real account, live ACH, live mail, live per-lease customers, live
  re-debits: TODO-VERIFY at checkpoint 2, not actions of this step.

## 11. TODO-VERIFY rows to add

066 Stax.js bank tokenization under a customer and the web payments token (sandbox has no
gateway); 067 the per-lease customer creation on live Stax (the command's live run); 068 live
ACH through `pay_invoice` from the portal; 069 whether a superseded bank method should be
deleted in Stax; 070 live mail after the owner's Workspace delegation; 071 Plaid with a real
linked business account and the `/transactions/sync` categories for wire, Zelle, ACH credit and
check deposits; 072 Nacha: the re-presentment count and window as implemented (2 within 180
days) against the merchant agreement; 073 the Federal Reserve holiday table's source and
upkeep.
