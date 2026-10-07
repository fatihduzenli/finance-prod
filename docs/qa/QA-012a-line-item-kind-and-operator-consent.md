## QA Report — PLAN-012a: line-item kind, fee and surcharge columns (S1); operator consent (S2)

**Date:** 2026-10-07 · **QA owner:** Fable · **Implementer:** Opus
**Plan:** `docs/qa/PLAN-012a-line-item-kind-and-operator-consent.md` · **Decision:** ADR-0011
(owner: S1 yes, S2 yes, in writing, 2026-10-07)

**Scope.** `backend/ledger` (models, migration `0031_line_kind_fees_consent`, operator form,
view, detail template, CSV headers, the invoice writers in `invoice_builder`, `nsf`,
`one_off_charges`, `rent_run`, `seed_demo`), `backend/payments` (`operator_sync` field list
and audit snapshot; the two `Invoice` writers `one_off_invoices.py` and
`webhooks/handlers/invoice.py` set the new columns), tests. **Unchanged:**
`payments/provider.py` (`Amounts`), `payments/stax/`, `infra/`, workflows, `pyproject.toml`,
`.env.example`. No new dependency, no new environment variable, no provider request changed.

**Commands run (final code).** Env as in QA-012; PostgreSQL 16 in a throwaway container.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean; 422 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 347 source files |
| `uv lock --check` | n/a | 0 | unchanged |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 4,539 passed, 566 skipped (4,483 before) |
| same | PostgreSQL 16 | 0 | 5,102 passed, 3 skipped (5,040 before) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff and new files | n/a | 0 | clean |
| `TODO-VERIFY` comments all have a register row | n/a | 0 | yes |
| Bicep build | n/a | — | not applicable: `infra/` untouched |

Baseline before PLAN-012a (QA-012): PostgreSQL 5,040 passed; sqlite 4,483 passed.

**Tests (Gate 2).** Opus: constraint cases for the five new constraints
(`invoices_total_is_sum_of_parts`, `invoices_fees_not_negative`,
`invoices_surcharge_not_negative`, `invoice_line_items_kind_valid`,
`operators_sms_phone_e164`); E.164 accept and refuse lists with the sentence; the guard on
`sms_phone` in the application and, on PostgreSQL, in the trigger through `QuerySet.update`;
a consent time one minute ahead refused, now accepted; billing saves the three fields with
no provider call and one audit row, an identical re-save writes none; a viewer's POST 403;
CSV columns; every builder line `charge`, fees and surcharge 0; AST checks that every
`Invoice.objects.create` in `ledger/` and `payments/` passes `fees` and `surcharge` and
every `InvoiceLineItem(` passes `kind`; the seeded round trip over 0031 on both engines
(existing lines become `charge`, totals unchanged, the reverse refused once an invoice has
fees). The first run surfaced two of Opus's own test failures (the trigger registry and the
E.164 sample for `test_card_number_guard`), fixed before the report.

Fable's adversarial file (`test_plan012a_adversarial.py`, 8 cases): the Stax adapter's
create-invoice request carries no `fees`, `surcharge` or `kind` key, `Amounts` has no such
field and nothing under `payments/stax/` names them; the builder's single create carries
the same lines and total as before and the stored invoice's total is the sum of the parts;
fees cannot be added behind the total, a negative surcharge and a kind outside the list fail
the CHECKs, and the only `kind=` written anywhere in production code is `CHARGE`; no module
under `ledger/services` names the two new money columns as literals, so no report figure
changed; a viewer cannot write consent (403, row unchanged), a card number, a `+`-prefixed
card number, a routing-and-account pair and a spaced number in `sms_phone` are refused with
the row unchanged and no provider call; a consent time in 2099 and forged field names change
nothing; the operator CSV neutralises the `+` and the viewer reads consent without a form;
every invoice's total equals subtotal plus tax with the new parts at zero.

No test weakened, skipped or deleted. Coverage is not measured in this repository; every
new branch named above has a test.

**Findings (Gates 3 and 4).** None blocking. Design choices accepted as built: the CHECK
renamed to `invoices_total_is_sum_of_parts` (the old name would have lied); `sms_phone` as a
plain `CharField` with the payment-data validator and the regex (the `guarded_char` helper
takes no extra validators; the PostgreSQL trigger covers the column from 0031); no
normalisation of the phone (spaces and dashes refused with the sentence naming the format);
consent to the second through `datetime-local`; audit snapshots write datetimes as UTC ISO
text; the three fields in `OPERATOR_FIELDS` but not in `PAYLOAD_FIELDS`, so editing them
queues no provider update. Recorded, not changed: editing only the consent fields on an
operator without a provider link still moves it to `pending` (existing behaviour of every
operator edit); migration 0029's docstring still says no `kind` column exists (historical
text).

**Confidentiality gate (§5.4).** Secret scan clean. No secret-store read by either model. No
new credential-capable file type. No new secret name.

**Residual risk.**
- The reverse of 0031 drops the consent columns' data; acceptable while they are empty on
  staging, and the reverse refuses once any invoice has fees or any line a kind but `charge`.
- Step 13 must normalise the sheet's phone column to E.164 (US numbers to `+1`) or leave the
  field blank with an import-audit row; this plan only makes the column exist.
- The `datetime-local` widget was not exercised in a browser (Django-client tests only).
- Consent times are kept to the second; an imported value with microseconds is truncated on
  the next form save.

**Verdict: PASS.**
