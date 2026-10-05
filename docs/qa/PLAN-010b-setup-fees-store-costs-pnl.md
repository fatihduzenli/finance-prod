# PLAN-010b — Setup fees, store costs, deposits, and profit and loss

**Date:** 2026-10-04 · **Author:** Fable (QA owner) · **Implementer:** Opus
**Origin:** owner-requested step after Prompt 10 (owner text of 2026-10-04). Pure application
work: no Stax call is added, no infrastructure changes, no new Container App.

## 1. Restated objective

The business leases each store from the landlord (Walmart) and subleases it to an operator at a
markup, and charges the operator one-time setup items (access fee, security deposit, front sign,
first-year insurance, architecture fee, business purchase fee, occasionally phone service) at a
markup over cost. None of this is in the app today: the sheet's Ins / Sign / "Deposit or Access"
columns are operator-side balances with no cost side, and landlord rents are nowhere. This step
adds a fee catalogue, per-lease setup charges collected through the existing payments ledger (and
an optional Stax one-off invoice linked with the existing link action), deposit resolution at lease
end (a deposit is a liability, never revenue), date-effective store costs with an importer for the
owner's template, a cost column on insurance premiums, and a profit-and-loss report by store,
operator and month with CSV export that a viewer can read.

## 2. Affected packages

`backend/ledger` (models, migrations, services, forms, views, templates, admin, URLs),
`backend/accounts/registry.py` (route rules), `backend/tests`, `docs/`. One new dependency:
`openpyxl==3.1.5` (MIT; reads the owner's `.xlsx` template; used only by the import command and
its tests). **Out of scope:** `backend/payments/*` except one enum value read-only
(`PaymentKind` lives in `ledger`); the Stax adapter; webhooks; infra; CI; Step 13's import itself
(§3 D13 records what it must do).

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | Table `fee_types`: `code` (slug, unique), `name`, `default_cost` (money, nullable = no standard cost), `default_price` (money, nullable), `refundable` (bool), `active` (bool), `sort_order`, timestamps. Admin-editable in Django admin (admin role). Seeded by a data migration with the seven types: `access_fee`, `security_deposit` (refundable), `sign_fee`, `first_year_insurance` (cost 300.00, price 695.00), `architecture_fee`, `business_purchase_fee`, `phone_service`. Only first-year insurance gets default amounts (the owner stated them); the others start empty because "no amount is standard" | Owner item 1 |
| D2 | **First-year insurance is seeded `active=False`.** The lease's first `insurance_charges` row already bills year one (ADR-0005), and its cost comes from `insurance_premiums.cost` (D9). An active setup charge of this type would bill the operator twice. The type stays in the catalogue (for Step 13 legacy balances and for the owner to activate if the business wants it as a setup line instead). Flagged to the owner in the plan message | Prevents a double charge; ADR-0005 |
| D3 | Table `setup_charges`: `lease` FK, `fee_type` FK (PROTECT), `cost` (money, nullable = unknown), `price` (money, > 0), `vendor` (guarded text), `due_date`, `amount_paid` (money, recomputed from settled payments, like `rent_charges`), `balance` (generated), `status` in {`owed`, `partial`, `paid`, `waived`} (derived from payments; `waived` set by the waive action), `waived_at`, `waived_by`, `waive_note` (mandatory when waived; CHECK), `note`, `legacy` (bool, Step 13), `needs_review`, timestamps. Unique `(lease, fee_type)` except when `legacy` (a legacy balance may coexist with a real charge of the same type). Status, like rent charges, is never edited by hand | Owner item 2 |
| D4 | `payments` gains `setup_charge` FK (nullable, PROTECT) and `PaymentKind` gains `setup` and `refund_deposit`. CHECK: `kind='setup'` ⇔ `setup_charge` set and `is_refund=False`; `kind='refund_deposit'` ⇔ `setup_charge` set, `is_refund=False`, `parent_payment` null. A `refund_deposit` row is money **out** (the business pays the operator back) with method zelle/wire/other and a mandatory note; it is never a Stax refund (the original debit may be months old and split). `invoices` gains `setup_charge` FK; the exactly-one-target CHECK on invoices (rent, insurance, adjustment) extends to four | Owner items 2 and 3 |
| D5 | Lease creation: the lease form gains a **setup checklist** (one row per active fee type: tick, cost, price, vendor, due date, defaulting cost/price from the catalogue and due date to `started_on`). One save creates the lease (as today) and the ticked charges in one transaction. The lease detail page lists its setup charges with balances and links to each charge page. Lease edit does not show the checklist; charges are added one at a time from the lease page (billing) | Owner item 2 |
| D6 | Setup charge pages: detail (balance, payments, linked invoice, status), **Record payment** (billing; the same `ManualPaymentForm`, kind `setup`; for the one-off Stax invoice the existing mark-paid path applies when a linked invoice exists and the payment covers the balance; otherwise local only), **Link invoice** (billing; the existing link rules against an unlinked invoice of the lease's operator, same sentences, confirmation and note when the total differs), **Waive** (admin; note mandatory; refused when any payment is settled), **Edit** cost/price/vendor/due date (billing; price not editable once a payment exists). ACH collection: staff create the one-off invoice in the Stax dashboard and link it, exactly as for rent; the app creates no Stax invoice (`STAX_PAY_INVOICE_ENABLED` stays off; TODO-VERIFY-019) | Owner item 2; "nothing new on the Stax side" |
| D7 | Deposit resolution: table `deposit_resolutions` (`setup_charge` one-to-one, `kind` in {`refunded`, `withheld`, `transferred`}, `amount` (> 0, ≤ amount paid on the deposit), `resolved_on`, `method` (refunded only), `note` (mandatory), `to_lease` FK (transferred only; same operator, not ended), `created_by`, timestamps). Admin action from the lease page once `ended_on` is set and the charge's fee type is refundable. `refunded` writes a `refund_deposit` payment (D4). `withheld` writes nothing to payments: the amount becomes revenue in the month of `resolved_on` (P&L "fees collected", labelled "withheld deposit"). `transferred` creates on the target lease a `security_deposit` setup charge with `price=amount`, `cost` null, and a `setup` payment of method `other`, note "Deposit transferred from <store label> lease" (so the target's balance derives from payments like every other charge). A deposit is resolved once; a partial refund plus a partial withholding is two rows? No: one resolution per deposit; the owner may pick refunded with an amount below the paid amount and a note that explains the rest (recorded as withheld remainder in the same row: `withheld_amount` = paid − amount, shown on the page). Deposits (refundable types) are excluded from every revenue figure: P&L, payments-ledger totals (shown on their own "Deposits held" line), the store and operator pages | Owner item 3 |
| D8 | Table `store_costs`, date-effective like `lease_rent_history`: `store` FK, `landlord_rent` (money, ≥ 0), `other_monthly_cost` (money, nullable), `other_cost_description`, `landlord_lease_end` (date, nullable), `effective_from`, `effective_to` (nullable; one open row per store; no overlap by construction: a new row closes the open one the day before), `source` in {`import`, `staff`}, `notes`, timestamps. "Current landlord rent" = the row effective today; **no row = cost unknown**, shown as "unknown", never 0. Store page: current landlord rent, landlord lease end, other cost, the operator rent of the current active lease, and the margin (operator rent − landlord rent − other cost) or "unknown". **Set landlord rent** form on the store page (billing): amount, effective from, optional other cost and description, landlord lease end; writes history | Owner item 4 |
| D9 | `insurance_premiums` gains `cost` (money, nullable = unknown); data migration sets 300.00 for 2025 and 2026. Price (`amount`) unchanged. Admin-editable | Owner item 5 |
| D10 | Import command `import_store_costs <path> [--dry-run]`: accepts `.csv` and `.xlsx` with the template's exact header row (sheet "Store costs" in `.xlsx`; the "How to fill" sheet is ignored); matches by `store_no` to `stores.store_number`; ignores the reference-only columns; a row whose `walmart_monthly_rent` is empty is "not filled" and skipped (the template says so); dates `MM/DD/YYYY` or ISO; empty `walmart_rent_effective_from` → the store's `open_date`, and a store without one is reported and skipped; amounts are plain numbers (digits with an optional decimal point; `$` or `,` rejected with the row number); unknown `store_no` reported; a `store_no` twice in the file rejected; every text cell goes through the card-number guard. **Idempotent:** a row equal to the store's open cost row changes nothing; a different amount with a later `effective_from` closes the open row the day before and adds a new one; the same `effective_from` updates in place; an earlier `effective_from` than the open row's is refused for that row. Writes `import_audit` rows (`target_table=store_costs`) and prints counts: read, skipped (not filled), unchanged, created, updated, refused, with each refusal's row number and reason. `--dry-run` validates and prints the same without writing. Runs in any process; no Stax | Owner item 4 |
| D11 | P&L report at `/reports/pnl/` (viewer and above) and `/reports/pnl/export.csv`. Parameters: `from`, `to` (months, inclusive; default the last three complete months plus the current one), `group` in {`store`, `operator`, `month`} (default `store`). One row per (store, operator, month) underneath; the group view sums rows and shows a breakdown toggle. Columns: rent billed (rent charges by `period_month`, excluding voided/cancelled), rent collected (settled `rent` payments by `settled_on`, minus settled refunds of them), fees billed (setup charges by `due_date` month, non-refundable types, not waived, **plus** insurance charges by `due_on` month), fees collected (settled `setup` and `insurance` payments by `settled_on`, non-refundable types, plus withheld deposits by `resolved_on`), landlord rent (the store cost row effective on the month's first day; "unknown" when none), other costs (same row), fee costs (setup charges' `cost` by `due_date` month, non-refundable; plus `insurance_premiums.cost` for the policy year of insurance charges billed that month; "unknown" when any cost is null), gross margin on a **billed basis** (rent billed + fees billed − landlord rent − other costs − fee costs) and on a **collected basis** (rent collected + fees collected − the same costs); a margin with any unknown input shows "unknown" and the row is flagged. A month in which a store has a cost row in effect (or a landlord lease not yet ended) and no lease active on any day of it is a **cost-only row**, highlighted. Deposits appear nowhere in this report except an optional "Deposits held" footer. All arithmetic in `Decimal`; CSV uses the payments ledger's formula-safe text cell and streams | Owner item 6 |
| D12 | Roles: fee types and premiums in Django admin (admin); setup charge create/edit/record payment/link (billing); waive and deposit resolution (admin); store cost set (billing); import command (operations, no role); P&L (viewer). Every write audited through the existing `AuditLog` pattern with masked identifiers | Pattern of PLAN-006/007 |
| D13 | Step 13 note: the sheet's Ins / Sign / "Deposit or Access" values import as `setup_charges` with `legacy=True`, `cost=None`, fee type `first_year_insurance` / `sign_fee` / `security_deposit` (or `access_fee` when the note says access), `price` = the sheet value, `amount_paid` from the note where it says paid, `needs_review` when ambiguous; the `store_balances` table of PLAN-004 is superseded by this and is removed in Step 13, not here. The insurance algorithm (ADR-0005) is unchanged | Owner item 7 |

## 4. Interface contract

Models: `FeeType`, `SetupCharge`, `SetupChargeStatus`, `DepositResolution`, `DepositResolutionKind`,
`StoreCost`, `StoreCostSource`; `Payment.setup_charge`, `PaymentKind.SETUP`, `PaymentKind.REFUND_DEPOSIT`;
`Invoice.setup_charge`; `InsurancePremium.cost`. Services: `ledger/services/setup_charges.py`
(`create_from_checklist`, `add_charge`, `edit_charge`, `waive`, `record_payment`, `link_invoice`,
`recompute`), `ledger/services/deposits.py` (`resolve_deposit`), `ledger/services/store_costs.py`
(`set_cost`, `current_cost`, `margin`, `import_rows`, `parse_file`), `ledger/services/pnl.py`
(`PnlQuery`, `rows`, `grouped`, `csv_lines`). URLs under `ledger:` — `setup_charges:detail|payment|link|waive|edit`,
`leases:add_setup_charge`, `leases:resolve_deposit`, `stores:set_cost`, `reports:pnl`, `reports:pnl_export`.
Command: `import_store_costs`. Migrations: one schema migration plus one data migration (seeds and premium costs).

## 5. Work breakdown

| Batch | Task | Done when |
|---|---|---|
| A | Models, migrations, seeds (D1–D4, D9); `setup_charges` service; lease checklist (D5); setup charge pages (D6); payments ledger shows kind `setup`/`refund_deposit` and the "Deposits held" line; admin registration; tests | Lease save creates charges; wire payment on a sign fee shows paid; waive needs a note; invoice link follows the rent rules |
| B | Store costs (D8), importer (D10), store page margin, deposit resolution (D7); the two-row example files `docs/templates/examples/store_costs_two_rows.{csv,xlsx}` (stores 1796 and 437, rents 2500.00 and 3000.00, effective 2026-01-01, one with other cost 50.00 "dumpster") plus a refused-rows fixture for tests; tests | Import of the example files creates two rows and a second run changes nothing; store page shows margin; refund appears as `refund_deposit` and not in revenue |
| C | P&L service, page, CSV (D11); nav link; registry; tests | Page shows the test store with the right margin for a known month; CSV equals the page; viewer can read, cannot write |
| Docs (Fable) | DATA_OWNERSHIP §3 and field tables; RUNBOOK §11 (setup charges, deposits, store costs import, P&L); DECISIONS ADR-0007 (deposit is a liability; first-year insurance stays an insurance charge); README commands; TODO-VERIFY if anything is assumed; QA-010b | |

Batches A, B and C touch disjoint service and view files; the model file and `urls.py` are edited
in A only, with B and C adding to them afterwards in sequence (A → B → C), never concurrently.

## 6. Test plan (before implementation)

Happy paths: checklist creates exactly the ticked charges with the typed amounts; untick all →
lease only; record a wire payment covering the price → `paid`, partial → `partial`; link an
unlinked invoice of the operator → linked; waive with note → `waived`; set landlord rent → margin
= operator rent − landlord − other; import two rows → two open cost rows; second import →
`unchanged=2`; P&L month row arithmetic for a store with rent 3,500, landlord 2,500, sign fee
price 1,000 cost 500 due that month → billed margin 1,500; deposit refunded → `refund_deposit`
row, revenue unchanged. Boundaries: price 0.01; cost null → "unknown" propagates to fee costs
and margins; a lease starting on the 31st; a store with cost rows changing mid-month (the 1st
rule); effective_to = effective_from − 1 day never produces an overlap; a month with two leases
(old ended, new started) counts both rents; import with a `store_no` as "01796", with trailing
spaces, with `2,500`, `$2500`, `2500.5`, `-1`, dates `1/5/2026`, `2026-01-05`, `13/01/2026`,
`02/30/2026`; `.xlsx` cells typed as numbers and as text; an `.xlsx` whose first sheet is not
"Store costs"; a CSV with BOM; a header with the reference-only columns missing (still accepted)
or a required header missing (refused as a whole). Error paths: waive after a settled payment →
refused; edit price after a payment → refused; deposit resolution on a non-refundable type, on
a lease not ended, amount above paid, transfer to another operator's lease, transfer to an ended
lease → each refused with a sentence; resolve twice → refused; import unknown store, duplicate
store in file, earlier `effective_from` than the open row, store without open date and no
effective date → refused rows, the rest applied, counts right; `--dry-run` writes nothing.
Adversarial (Fable): a note/vendor/description with a card number → refused everywhere including
the import (row refused, no partial write) and the CSV export never emits it; CSV cells starting
with `=`, `+`, `-`, `@` are apostrophe-prefixed; P&L query parameters with a 1,000-year range, a
`to` before `from`, garbage months → bounded and refused with 200 and a message, never 500;
viewer POSTs to every new write route → 403; billing cannot waive or resolve deposits → 403;
concurrent two "Record payment" clicks on one setup charge → one payment (creation key);
concurrent checklist double-submit → one lease, one set of charges; the P&L never counts a
deposit, a voided or returned payment, or a waived charge; a `refund_deposit` of more than was
paid is refused; the importer rejects a 5 MB file of 100,000 rows within the row cap (10,000)
with a sentence; `.xlsx` with a formula cell is read as its cached value or refused, never
evaluated; an `.xlsx` that is a zip bomb or not a zip → one sentence, no traceback.

## 7. Risk register

- **Double billing of year-one insurance** if `first_year_insurance` is active at the same time
  as the insurance algorithm (D2 mitigates; owner to confirm the choice).
- **Deposits as revenue**: every aggregate must exclude refundable types; the test plan checks
  the P&L, the ledger totals and the store/operator pages. A withheld deposit becomes revenue only
  in the month of resolution.
- **Date-effective overlap** in `store_costs`: enforced by the service (close-then-insert in one
  transaction under a row lock on the store) and a partial unique index on the open row, as in
  `lease_rent_history`.
- **Import of a wrong file** (e.g. the Rents tab): the header check refuses the whole file; the
  row cap and dry run limit damage; everything is audited and date-effective, so a wrong rent is
  corrected by a later row, never by deletion.
- **New dependency** `openpyxl==3.1.5` (MIT): pinned; flagged for review; used only by the
  importer; a malformed workbook must surface as one sentence.
- **Shared code**: `payments` and `invoices` gain a fourth target; the existing CHECKs, the
  ledger list, the payment detail page, the refund/void rules (a `setup` payment made by ACH is
  refundable like rent; a `refund_deposit` row is never refundable or voidable) and the
  reconciliation's transaction handler (which creates payments from Stax transactions; a
  transaction for a setup invoice must land on the setup charge) all change. Every consumer is
  enumerated in QA.
- **Performance** of the P&L over 500 stores × 12 months: computed with grouped queries, not per
  row; tested with 500 stores under 2 seconds on PostgreSQL.
- Irreversible: none (data migration seeds only; the Step 13 removal of `store_balances` is not
  in this step).

## 8. Acceptance criteria

Gate green on both engines; migrations clean; `openpyxl` pinned in `pyproject.toml` and `uv.lock`;
the owner's done-when on staging: lease creation with the checklist creates the setup charges; a
landlord rent set on the store shows the margin; a wire payment on the sign fee shows paid; the
two-row example import creates the costs (second run: unchanged); `/reports/pnl/` shows the test
store with the right margin and the CSV matches; a deposit refund at lease end appears in the
ledger as `refund_deposit` and in no revenue figure.
