# PLAN-012a — S1 (line-item kind, fee and surcharge columns) and S2 (operator consent columns)

**Date:** 2026-10-07 · **Owner decision:** S1 yes, S2 yes, in writing on 2026-10-07
(ADR-0011, `docs/BACKLOG.md` §2) · **Lead/QA:** Fable · **Implementer:** Opus
**Before:** Step 13 (the import), because the import is the one moment every operator row is
written with sheet data, and the builder is already writing several lines per invoice.

## 1. Restated objective

Two schema additions and nothing else: every invoice line item says what kind of line it is
(`charge` today; `fee`, `surcharge`, `tax`, `discount` reserved), and an invoice carries fees
and surcharge as separate stored amounts that default to zero, so a future card or
surcharging decision needs no backfill and no change to the reports that sum `total`; and an
operator carries an SMS phone in international format and the times consent was given for
SMS and for e-mail, editable by billing on the operator form, so the Step 13 import can fill
the phone from the sheet and so BACKLOG item 1 (reminders by Stax e-mail or SMS) has a
recorded consent to check. No channel uses the consent columns in this plan; no line of any
kind but `charge` is written; no report changes its numbers.

## 2. Affected packages

`backend/ledger` (models, one migration, the builder's line write, the operator form and
page, the operator CSV headers), `backend/tests`. **Out of scope:** `payments/` (the provider
interface does not change: fees and surcharge are not sent), the import command (Step 13 maps
the phone), the collections ladder, any SMS sending, `infra/`, workflows.

## 3. Decisions

- **D1 — `invoice_line_items.kind`.** Guarded choice column, values `charge`, `fee`,
  `surcharge`, `tax`, `discount`; default and backfill `charge`; CHECK on the closed list
  (`_valid_choice`). The builder, the one-off invoice path, the NSF two-line path and the rent
  invoice line all write `charge` explicitly. Nothing reads `kind` yet except the schema tests.
- **D2 — `invoices.fees` and `invoices.surcharge`.** `MoneyField(default=0)`, backfilled 0,
  CHECK `>= 0`; the existing CHECK `invoices_total_is_subtotal_plus_tax` becomes
  `total == subtotal + tax + fees + surcharge` within half a cent (dropped and recreated in
  the same migration, PostgreSQL and sqlite). Every writer of `Invoice` passes 0 (explicitly,
  so a grep finds them). `payments.provider.Amounts` is **unchanged** (`total == subtotal +
  tax`): the adapter keeps sending what it sends; the day fees or a surcharge are non-zero is
  the day a new plan touches the provider contract.
- **D3 — operator consent.** `operators.sms_phone` (guarded char, 20, blank allowed, must
  match `^\+[1-9][0-9]{7,14}$` when present: E.164, validated in the form and the model's
  `clean`, plus a CHECK), `sms_consent_at` and `email_consent_at` (nullable datetimes). The
  operator form shows the three as "SMS phone (international, +1…)", "SMS consent given on",
  "E-mail consent given on"; billing edits them; the operator page shows them under contact
  details; the operator CSV gains three reviewed headers. A consent time in the future is
  refused. The existing `operators.phone` stays (it is what Stax holds; Q16).
- **D4 — no behaviour.** No code sends an SMS, no reminder reads the consent, no builder line
  is anything but `charge`; the plan is columns, writers, form, page, CSV and tests.

## 4. Interface contract

- `InvoiceLineKind(TextChoices)`: `CHARGE`, `FEE`, `SURCHARGE`, `TAX`, `DISCOUNT`.
- `InvoiceLineItem.kind` (default `charge`), `Invoice.fees`, `Invoice.surcharge` (default 0).
- `Operator.sms_phone`, `Operator.sms_consent_at`, `Operator.email_consent_at`.
- `OperatorForm` gains the three fields; `csv_export.HEADERS` gains `"SMS phone"`,
  `"SMS consent given on"`, `"E-mail consent given on"`.
- Migration `ledger/0031_line_kind_fees_consent` (schema, backfill, constraints; the
  roundtrip test covers it). No `payments` migration. No new environment variable. No
  provider change.

## 5. Work breakdown (one batch, Opus)

| Task | Files | Done when |
|---|---|---|
| Columns, choices, constraints, migration | `ledger/models.py`, `ledger/migrations/0031_*.py` | both engines migrate forward and back on an empty and a seeded database; `makemigrations --check` clean |
| Writers pass `kind="charge"`, `fees=0`, `surcharge=0` | `invoice_builder.py`, `one_off_charges.py`, `nsf.py`, `rent_run.py` (or wherever `Invoice`/`InvoiceLineItem` rows are created; enumerate with a grep and list them in the report) | a grep for `InvoiceLineItem(` and `Invoice.objects.create(` shows every call site setting them |
| Operator form, page, CSV | `ledger/forms.py`, `ledger/views/operators.py`, operator templates, `csv_export.py` | billing saves a +1 number and two consent times; a viewer sees them and cannot edit; the CSV has the three columns |
| Tests | `tests/test_ledger_models.py`, `tests/test_operator_views.py`, `tests/test_csv_export.py`, `tests/test_invoice_builder.py`, `tests/test_migrations_roundtrip.py` | §6 cases green on both engines |

## 6. Test plan (before implementation)

Happy: a builder invoice's lines are all `charge`; an invoice created by each path has
`fees = surcharge = 0` and `total = subtotal + tax`; billing saves `+14045551234`, a consent
time today, the page and CSV show them. Boundaries: `sms_phone` blank allowed; `+1` alone
refused; 16 digits refused; a national-format number `404-555-1234` refused with a sentence
naming the `+` format; a consent time one minute in the future refused, now accepted; the
backfill on a seeded database leaves every existing line `charge` and every invoice's total
unchanged. Error paths: a line `kind` outside the list fails the CHECK; negative fees fail the
CHECK; a total that ignores fees fails the recreated CHECK. Adversarial (Fable): a viewer
cannot change consent (403, row unchanged); a card number or a routing-and-account pair in
`sms_phone` is refused by the guard; the provider contract (`Amounts`) is unchanged and the
Stax adapter sends no `fees` or `surcharge` key (grep of the mapping and a request-shape
test); the reports, the ledger total and the P&L produce the same numbers before and after
the migration on the seeded fixture; no report sums `fees` or `surcharge` into a shown figure
yet (they are zero, and the test pins the queries' column lists).

## 7. Risk register

- Recreating the invoice total CHECK is the one risky migration step; it runs in its own
  migration operation with `SET CONSTRAINTS ALL IMMEDIATE` as 10d taught, and the roundtrip
  test runs on a seeded database.
- `Amounts` unchanged means fees and surcharge can never silently reach Stax; a future plan
  that fills them must change the contract on purpose.
- The sheet's phone column is not E.164; Step 13 normalises (US numbers to `+1`) and leaves
  the rest blank with an import-audit row; this plan only makes the column exist.
- Irreversible: none (the reverse migration drops the columns; data in them is lost, which
  is acceptable while they are empty on staging).

## 8. Acceptance criteria

Gate green on both engines; the roundtrip test on a seeded database; every `Invoice` and
`InvoiceLineItem` writer sets the new columns explicitly; the operator form round-trips the
three fields for billing and refuses them for viewers; the CSV headers reviewed; QA-012a
published; no provider request shape changes.
