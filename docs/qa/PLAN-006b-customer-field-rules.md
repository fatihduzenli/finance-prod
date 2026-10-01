# PLAN-006b — Validate what Stax requires before any customer request leaves

**Date:** 2026-10-01 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Origin:** Checkpoint 6, owner finding of 2026-10-01: the 422s came from an incomplete phone
number; nine failed requests were spent on it. Owner instruction: validate the fields Stax
requires in the form before any request leaves, per the Stax docs; keep the 4xx field logging.

## 1. The rules, read from `/reference/create-customer.md` on 2026-10-01

| Field we send | Stax rule (verbatim from the specification block) | Our rule, applied in the form and again in `stax/validation.py` |
|---|---|---|
| `phone` | `Matching regex: /[0-9]{10,15}/` | Optional. When given: strip spaces, hyphens, dots, parentheses and one leading `+`; what remains must be **10 to 15 digits**, and is what is sent. Anything else is a form error: "Enter 10 to 15 digits, for example 4155550132." An empty phone is **omitted** from the payload, never sent as `""` |
| `firstname`, `lastname`, `email`, `company` | each `Max: 255`; at least one of the four must be present | `display_name` is required and at most 255; `legal_name`, `company` at most 255; `email` at most 255 (`EmailField` already) |
| `reference` | `Max: 255` | our UUID, 36 characters, unchanged |
| `notes` | `Max: 21844`, internal only | not sent (unchanged) |
| `address_*` | `address_state` max 2, `address_zip` max 16, `address_country` max 3 (ISO 3) | not sent in this prompt (PLAN-006 D5 deviation); the model field `country` stays two letters until Prompt 7 maps it |
| `cc_emails`, `cc_sms` | arrays of valid emails / phones | not sent |

Error shape confirmed by the spec's example: `{"<field>": ["<message>"]}`, so PLAN-006a's
`error_fields` will name the field on the next 4xx.

## 2. Changes

1. `backend/ledger/forms.py`: a `clean_phone` that normalises per the table and rejects the
   rest; `max_length` 255 on display name, legal name, company (model already 200; keep 200,
   which is within 255). The error text is fixed and value-free.
2. `backend/payments/stax/validation.py`: the same phone rule (`validate_phone`) and the
   255-character caps, applied to `CustomerInput` before any request; a violation raises
   `ValidationFailed` and nothing leaves. `customer_input()` in `operator_sync.py` passes the
   normalised digits.
3. `backend/payments/stax/mapping.py` (customer body): omit `phone` when empty; omit
   `lastname` when empty (it is always empty today) instead of sending `""` (closes
   TODO-VERIFY-033's risk without waiting). Never send an empty string for an optional field.
4. The edit form applies the same rule, so an existing operator with a bad phone cannot be
   re-synced until it is fixed; the detail page's retry button is replaced by "Fix the phone
   number first" when the stored phone fails the rule.
5. Existing rows: migration 0006 `RunPython` that normalises stored phones that pass the
   rule and sets `needs_review = True` on operators whose stored phone does not (no data is
   deleted; reversible as a no-op).

## 3. Tests

| # | Case | Author |
|---|---|---|
| 1 | Phones: `4155550132`, `+1 (415) 555-0132`, `415.555.0132`, 15 digits accepted and normalised; `555-0132`, `12345678901234567`, `+44 20 7946 0958 ext 12`, letters, empty → empty is accepted and omitted, the rest refused with the fixed text; zero provider calls on refusal | Opus |
| 2 | Payload: no `phone` key when empty; no `lastname` key; digits only when given | Opus |
| 3 | Transport-level: `CustomerInput` with a bad phone raises `ValidationFailed` before any request (adapter test) | Opus |
| 4 | Edit of a synced operator with a bad phone refused; retry button replaced by the fix sentence; after fixing the phone the retry works | Opus |
| 5 | Migration 0006 on rows with good, bad and empty phones | Opus |
| 6 | Adversarial: unicode digits in a phone, `+` in the middle, 10 digits of zeros, a phone that is a Luhn-valid 16-digit run (must be refused as too long, and must never trip the card guard's message either way) | Fable |
| 7 | Whole suite both engines, lint, format, types, secret scan | Fable |

## 4. Acceptance

Lint, format, types exit 0; both pytest commands exit 0 on both engines; `makemigrations
--check` clean; no existing test removed or skipped; every row above has a passing test; the
next operator saved on staging with a complete phone produces no 4xx.
