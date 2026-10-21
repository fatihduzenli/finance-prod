# PLAN-012e-2 — Step 12e: fewer actions and a simpler store page

**Owner's text:** 2026-10-21 ("Step 12e — Fewer actions and a simpler store page (before Step 13)").
**Numbering:** the owner calls this Step 12e; `PLAN-012e-lease-end-transfer-installments.md` is
the earlier plan of Step 12b, so this file is PLAN-012e-2.
**Author:** Fable (lead engineer, QA owner). **Implementation:** Opus, per batch.
**Status:** Phase A plan; the decisions marked *pending* wait for the owner's written word and
are built under the stated assumption, reversibly.

## 1. Restated objective

The store page's action bar shrinks from eleven buttons to three: **Create invoice**, **Add
payment** and a **Lease ▾** menu (Edit lease, Transfer to another operator, End lease as the
destructive item); a vacant store shows **New lease** only. "Add setup charge", "Charge now" and
"Schedule a payment" fold into the invoice builder, which keeps its lines and ends with one
**How to collect** choice: Save draft, Send by e-mail, Charge now (autopay, with the large-debit
confirmation), Schedule for a date (a one-off autopay on that date), Record payment now (wire,
Zelle or check with date and reference: the invoice is created, marked paid as cash in Stax, and
its charges created paid, in one go). Every path writes the same charges and audit rows as
today; the three separate forms go, their URLs redirecting to the builder with the choice
pre-selected. "Upload document" moves into the Documents tab, "Add entry" into the Contact log
tab, "Create missing charges" becomes a small link in the Charges tab; the operator page follows
the same layout. The store details show facts only: one Address line, Store number, Open date,
Closed date when set, Status as a badge; Edit stays; "Set landlord rent" moves into a small
**Store cost** block (landlord rent "from <date>", landlord lease end, other monthly cost when
set, one Change link) that replaces "Costs and margin"; margin stays in the P&L. The New store
form asks for landlord rent, landlord lease end and other monthly cost. Help text that explains
internal rules leaves the store page for help pages; every page shows at most one line of help
under a block. Tests cover each collection path, the redirects, the vacant-store bar, the
address line and the status badge; the visual baselines are regenerated; the copy list is
otherwise unchanged.

## 2. Affected packages

In scope: `backend/ledger` (services `invoice_builder`, `workspace`, `payments_entry`,
`one_off_charges`, `stores`; views `invoices`, `stores`, `one_off_charges`, `operators`;
forms; templates under `stores/`, `operators/`, `invoices/`, `leases/`; one migration), `backend/accounts/registry.py`
(route rules), `backend/finance` (the help pages: a view, templates, a URL, the nav user menu),
`backend/tests` (unit, e2e, visual baselines), `docs/`. Out of scope: `payments/` (no new
provider call: the paths reuse `one_off_invoices.create/send/pay/schedule` and the mark-paid
job), the collection ladder, the public statement, Step 13.

## 3. Decisions

### 3.1 The builder's "How to collect" (owner's 1)

- **D1.** One service entry point, `invoice_builder.collect(draft_id, choice, *, actor, today,
  confirmed=False, due_date=None, payment=None) -> Outcome`, with `choice` one of
  `CollectChoice`: `draft` (save only; the existing `create_draft`/`update_draft` path),
  `email` (today's `send`), `charge_now` (today's `charge_now`, admin, the large-debit
  confirmation unchanged), `schedule` (new: the draft is converted as today with collection
  `autopay_due`, one Stax invoice with every line, and **one one-off autopay on `due_date`** for
  the invoice total carried by the first charge, as `payments_entry.schedule` does for a single
  debit today; `due_date` must be after today and within the existing scheduling horizon;
  billing), `record` (new: `payment` is `{method, paid_on, reference, note}` with `method` one of the manual methods the app records today (`rent_run.MANUAL_METHODS`: Zelle, wire, other; a check is "other" with its reference, as "Add payment" does; no new stored value);
  the draft is converted with collection `manual`, the Stax invoice created but not sent, each
  charge paid by the payment in line order through the existing allocation
  (`one_off_charges.record_payment` with one `allocation_group`), and the invoice marked paid
  as cash in Stax by the existing mark-paid job once the create job has reported the Stax id;
  the amount is the invoice total, never typed: a different amount is a payment on the charge
  page afterwards; billing). `send` and `charge_now` stay as thin wrappers so nothing that calls
  them changes.
- **D2.** Same rows as today: every path makes the same `OneOffCharge` rows, `Invoice`,
  `InvoiceLine`s, `external_refs`, audit rows (`invoice_draft.sent` with the choice in `after`,
  `one_off_charge.created`, the payment and allocation rows of `record`, the schedule rows of
  `schedule`) as the separate forms wrote. A test per path asserts the row set against a
  recorded expectation built from today's forms before they are removed (Batch A writes those
  expectations first, from the current code, then switches the forms off).
- **D3.** The page: the draft form ends with a "How to collect" fieldset of five radio choices;
  the fields of a choice show only while it is chosen (the PLAN-012c D8 `data-shown-when`
  script, visible without script): the date for `schedule`, method/date/reference/note for
  `record`; `charge_now` keeps its separate confirmation page (the owner's 10d rule) and the
  radio leads there; the submit button names the choice ("Save draft", "Send $2,000.00 by
  e-mail", "Charge $2,000.00 now", "Schedule $2,000.00 for 2026-11-05", "Record $2,000.00
  received"). The saved draft page keeps Preview, Print view, Split and Cancel. A viewer sees
  no builder; billing sees every choice but Charge now (admin).
- **D4.** Redirects: `ledger:stores:schedule_payment` → the store's new-invoice page with
  `?collect=schedule`; `ledger:stores:charge_now_pick` → `?collect=charge_now`;
  `ledger:leases:add_setup_charge` → `?collect=draft&item=<setup>` (the setup item pre-chosen on
  line 1 from the lease's catalogue, `?store=&lease=` kept); `GET` only, 302, the same role
  gate as the old route (a viewer gets 403, not a redirect); the old `POST`s answer 405. The
  route names stay in the registry as read-only redirects (the planned-route tests change
  accordingly); the three templates and their form classes are deleted.

### 3.2 The action bar (owner's 2–4)

- **D5.** `workspace.actions` returns at most three items on a store with a lease: `create_invoice`
  (primary), `record_payment` ("Add payment"), `lease` (a menu: `edit_lease`,
  `transfer`, `end_lease` with `danger=True`); on a vacant store `new_lease` only (admin), and
  nothing for a viewer. The menu is a native `<details class="menu">` with a `<summary
  class="btn">Lease ▾</summary>` and a list of links: works without script; closes on a click
  outside through the existing small script pattern (optional, no library). `upload_document`
  moves into the Documents tab's header (the upload form is already there: the action becomes a
  link to it, or is dropped when the form is on the tab), `add_contact` into the Contact log
  tab's header (already there as "Add entry": the bar's duplicate goes), `create_charges` is a
  small link under the Charges tab's filters ("Create missing charges" as a text link, billing).
  `charge_now`, `add_setup_charge` and `schedule_payment` go (D4).
- **D6.** The operator page follows: its bar is Statement · New invoice (primary) · Edit, with
  "Connect to Stax" kept when it applies and Archive in a Lease-style "More ▾" menu as the
  destructive item; Add entry stays in the contact section's header.

### 3.3 The store details and the Store cost block (owner's 4a–4c)

- **D7.** Store details show: Address (one line: street, city, state ZIP, joined with ", " and a
  space before the ZIP; parts missing are left out; "—" when all are empty), Store number, Open
  date, Closed date (only when set), Status as a badge (`status_badge`: "Active" paid-green,
  "Inactive"/"Closed"/"Terminated" danger-red, "Prospective" neutral), "Needs open date" kept as
  today. Edit stays in the block's header; "Set landlord rent" leaves it. **Migration 0047** adds
  `address_line1` and `postal_code` to `stores` (blank, guarded like the operator's); the store
  form asks street, city, state and ZIP separately. The stores list and search are unchanged
  (the search already matches city).
- **D8.** The "Costs and margin" block becomes **Store cost**: Landlord rent ("$3,500.00 a month
  from 2026-01-01", or "unknown"), Landlord lease end, Other monthly cost (only when set, with
  its description), one "Change" link to the existing cost-row form (`ledger:stores:set_cost`,
  admin). No margin line, no paragraph. The cost history table stays below it, collapsed behind
  a `<details>` "History" (it is a fact list, not a rule). Margin stays in the P&L only.
- **D9.** The New store form asks landlord rent (required; `0.00` means "no cost", as today's
  rule), landlord lease end (optional) and other monthly cost with its description (optional);
  saving creates the store and its first cost row effective from the open date (or today when
  there is none), audited as today's set-cost does; the Edit store form does not show them (the
  Change link does). `import_store_costs` keeps its columns. *Pending the owner:* landlord rent
  required on a new store (assumed yes).
- **D10.** Help pages. There are none in the app today; the owner's text assumes them. *Pending
  the owner, assumed:* a small set of signed-in, static help pages under `/help/` (`finance`
  app: a view, a template per topic, no model), reachable from the user menu ("Help") and from
  a one-line "Learn more" link under the block that lost its paragraph: topics "Store costs and
  margin" (the two paragraphs of today's store page), "How an invoice is collected" (the five
  choices), "Autopay and the daily charges", "Needs review". The rule for every page: at most one
  line of help under a block (a test counts `p.muted`/`.help` elements per block in the main
  templates and fails on a second, and on any sentence longer than 160 characters); paragraphs
  that break it move to the matching help page in this plan's inventory (Batch C lists them).

### 3.4 Visual baselines and copy (owner's 5)

- **D11.** The visual suite's data set gains the store's address, a cost row and a draft at the
  "How to collect" step; the baselines are regenerated by Fable in the Playwright image for the
  pages that change (store tabs, operator, invoice builder, stores list) and committed with the
  batch. New copy is limited to the labels named here ("How to collect", the five choices and
  their buttons, "Lease ▾", "More ▾", "Store cost", "Change", "History", "Address", "Learn
  more", "Help"); everything else follows the approved §4 list of PLAN-012c.

## 4. Interface contract

```
ledger.services.invoice_builder
  class CollectChoice(StrEnum): DRAFT="draft" EMAIL="email" CHARGE_NOW="charge_now"
                                SCHEDULE="schedule" RECORD="record"
  @dataclass(frozen=True) PaymentInput: method: str  # one of rent_run.MANUAL_METHODS
                                        paid_on: date; reference: str = ""; note: str = ""
  def collect(draft_id: UUID, choice: CollectChoice | str, *, actor, today=None,
              confirmed: bool = False, due_date: date | None = None,
              payment: PaymentInput | None = None) -> Outcome
      # Outcome.ok, .message (value-free), .invoice, .charges, .payment (record), .schedule (schedule)
  send(...) and charge_now(...) unchanged (wrappers)

ledger.services.workspace
  Action(key, label, url, primary=False, danger=False, items: tuple[Action, ...] = ())  # a menu has items
  actions(...) -> at most: create_invoice, record_payment, lease(menu) | new_lease

ledger.services.stores
  def address_line(store) -> str
  def create_store(form_data, *, actor) -> Store   # creates the first cost row (D9)

Routes (unchanged names, new behaviour): ledger:stores:schedule_payment, ledger:stores:charge_now_pick,
  ledger:leases:add_setup_charge -> GET 302 to ledger:invoices:new_store/new_lease with ?collect=...
New routes: finance:help (/help/), finance:help_topic (/help/<slug>/), signed-in, every role.
Migration: ledger 0047_store_address (address_line1, postal_code; blank; guarded).
Query string: ?collect=draft|email|charge_now|schedule|record (anything else -> draft, no error); ?item=<catalogue code>.
```

## 5. Work breakdown (batches, each gated)

| Batch | Scope | Done when |
|---|---|---|
| A | D1–D4: `collect`, the five paths, the fieldset, the redirects, the forms removed; row-set parity tests recorded from today's forms first | every path writes today's rows; the old URLs redirect with the choice; the three templates and forms are gone |
| B | D5–D6: the action bar and the Lease menu, the tab moves, the operator page | three buttons (or New lease); the tabs carry Upload, Add entry, Create missing charges; the operator page matches |
| C | D7–D10: address columns and line, the status badge, the Store cost block, the New store form's cost fields, the help pages, the one-line rule and the moved paragraphs | the store page shows facts only; the help pages answer; the one-line test passes on every main template |
| D | D11: the visual data set and baselines (Fable, in the image); QA; the checkpoint report | baselines committed; CI's visual job green |

## 6. Test plan (before implementation)

A: a test per choice asserts the rows (charges, invoice, lines, refs, audit, payment and
allocation, schedule) equal the recorded expectation of the removed form; `record` with a method
outside wire/zelle/check refused; `record` with a date in the future refused; `schedule` with
today or a past date refused; `schedule` beyond the horizon refused; `charge_now` still needs
the confirmation and the admin; the submit button names the choice and the amount; the choice's
fields hidden until chosen (markup + the e2e dependent-fields pattern); the redirects (302, the
query string, the role gate: viewer 403; POST 405); the old templates absent; the builder opened
with `?collect=schedule` pre-selects the radio and shows the date. B: the bar on a leased store
(three), a vacant store (New lease), for a viewer (none), the menu's three items in order with
End lease marked danger, the menu usable without script (a `<details>`), the tabs' moved
controls, Create missing charges as a link, the operator page's bar; the route registry and the
planned-route tests updated. C: the address line (all parts, some parts, none), the badge per
status, Closed date only when set, no margin on the store page, the Store cost block's three
lines and the Change link (admin only), the New store form creating the first cost row with the
right effective date and audit row, the import command unchanged, every help page answering 200
to every role and 401 anonymous, the one-line rule over the main templates, the moved paragraphs
present on their help page and absent from the store page. D: the visual suite green against
the new baselines in the image.
**Adversarial (Fable):** `?collect=<script>` and `?item=../../x` answer 200 with the draft choice
and nothing echoed; `record` posted by a viewer or with a tampered total creates nothing; a
double submit of `record` (same creation key) creates one payment; `schedule` posted twice makes
one schedule; the redirect never carries user text into the query string unescaped; a store
address with markup is escaped in the line and the title; the Lease menu's End lease link is
absent for billing; the help pages carry no form and no user data; the one-line rule test fails
on a deliberate second paragraph (a mutation of a template copy).

## 7. Risk register

- **Row parity** is the contract: the expectations are recorded from today's forms before they
  are removed (Batch A step 1), so a drift is a failing test, not a judgement.
- **`record` is two Stax steps** (create the invoice, then mark it paid as cash): the mark waits
  for the create job's id as the one-off manual path already does; if the create fails, the
  payment is recorded locally and the charge shows "Not marked paid in Stax" as today.
- **Migration 0047** adds two blank columns: additive, reversible; the card-number guard list
  gains the two columns (they are guarded text).
- **Removing three forms** removes routes' handlers: the registry keeps the names (read-only
  redirects), so the planned-route tests and the role tests change; a bookmark still lands.
- **Help pages** are new surface: static, signed-in, no data, no form; the smallest thing that
  meets "the help pages".
- **Visual baselines** move for four page groups; Fable regenerates in the image.
- **Security:** `record` keeps the manual-method whitelist (Zelle, wire, other) and the reference guard (no card or
  account number, the existing validator); `schedule` keeps the horizon and the autopay role;
  the redirects do not interpolate user text beyond a whitelisted choice and a catalogue code
  validated against the lease's catalogue.

## 8. Acceptance criteria

On staging: the store page shows Create invoice, Add payment and Lease ▾ (New lease on a vacant
store); a setup charge, a scheduled debit and an immediate charge are all made from the builder's
"How to collect"; a payment recorded from the builder shows the invoice paid in Stax; the three
old URLs land in the builder with the choice chosen; the store details show one Address line and
a status badge; the Store cost block shows rent, lease end and other cost with Change; no margin
or rule text on the store page; the help pages open from the user menu; the visual job is green.
