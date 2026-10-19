# PROVIDER.md — provider portability

**Date:** 2026-10-07 (Prompt 12, item 7). Companion to `docs/STAX_CLIENT.md` (how the Stax
client behaves) and `docs/CLAUDE.md` Rule 8 (one `PaymentProvider` interface, one
`external_refs` table). This file records which modules may know about Stax, the test that
holds that line, and what a second provider would have to implement. It changes no code.

## 1. The boundary

Everything the app knows about Stax lives in `backend/payments/stax/`: URLs, headers, field
names, status strings, the limiter, the breaker, the failure budget and the retry policy. The
rest of the app talks to `payments.PaymentProvider` (`backend/payments/provider.py`) in our own
vocabulary (`CustomerInput`, `InvoiceInput`, `ScheduleInput`, `TransactionRecord`, …) and gets a
provider from `payments.get_provider()`.

### Modules that may import `payments.stax` (as of 2026-10-07)

All inside the `payments` package; none outside it:

| Module | Why |
|---|---|
| `payments/__init__.py` | `get_provider()` builds the Stax adapter (or the fake) |
| `payments/health.py` | the status page reads the breaker and bucket state |
| `payments/offline_marking.py` | the cash/check mark-paid memo mapping |
| `payments/operator_sync.py` | failure-name mapping of provider errors |
| `payments/timezone_audit.py` | reads the transaction summary key names |
| `payments/reconciliation/__init__.py` | the limiter's departure counter |
| `payments/management/commands/{provider_environment_check,provider_ping,reconcile,worker}.py` | boot check, ping, the `STAX_CALLER` gate |

Nothing under `ledger/`, `accounts/`, `finance/`, `documents_storage/` or `mail/` imports it.

### The tests that hold the line

- `backend/tests/test_stax_isolation.py`: no file outside `payments/` contains the string
  "stax" except an allow-list of settings, the sandbox banner, log redaction, Sentry tagging,
  the `/healthz` field, the `payment_channel` value and the upload notice (each with its reason).
- `backend/tests/test_provider_portability_adversarial.py` (Prompt 12): no module outside
  `payments/` imports `payments.stax`; inside `payments/`, only the modules in the table above
  do; every method of `PaymentProvider` is implemented by both the Stax adapter and the fake;
  no `ledger` module names a Stax status string or a Stax field name.

## 2. What a second provider must implement

A second provider (the owner named Stax Bill and Forte as examples) is a new package
`backend/payments/<name>/` with an adapter class implementing every method of
`PaymentProvider` in our vocabulary, plus the pieces the Stax package provides around it:

| Area | What the adapter must provide | Where the app relies on it |
|---|---|---|
| Customers | create, update, get, find by e-mail; payment-method list and get (bank methods identified, last four and nickname only) | operator sync, enrolment page, Charge now |
| Invoices | create (several line items, our reference, due date), get, list by customer and date, delete, send by e-mail, mark paid cash/check with a memo, pay with a stored method and an idempotency key | one-off charges, rent linking, manual payments, Charge now, cancel |
| Schedules | create with a recurrence rule and a stored method, list, update active/rule, delete | autopay (D1-B), one-occurrence schedules |
| Transactions | list by date range and customer, get, refund, void | payments, refunds, voids, reconciliation |
| Deposits and rejections | list deposits and deposit detail, list ACH rejections for a window | reconciliation steps c and d |
| Webhooks | list, create, delete registrations; an inbound receiver that extracts the event name and object id and never trusts the body | `payments/webhooks` |
| Identity | a boot check that confirms the key belongs to the expected merchant and environment | `provider_environment_check` |
| Transport | one rate-limited, circuit-broken client with the project's retry policy; `RetryPolicy.NONE` for pay-with-stored-method | `payments/stax/transport.py` is the reference |
| Mapping | our statuses are the provider's strings stored verbatim; the app never maps a status to a guess | Rule 2 |

Data stays provider-agnostic: provider IDs live only in `external_refs` with the provider's
name in the `provider` column; snapshots in `provider_snapshots`. A second provider adds a value
of `provider`, never a column.

### Not portable without a decision

- The ACH-only, no-card rule and the mark-paid memo convention are business rules, not Stax
  rules; they carry over.
- "Charge now" relies on an idempotency key the provider honours; Forte's and Stax Bill's
  equivalents must be verified in their specs before the method is implemented.
- Webhook security differs per provider (Stax: URL token only). The receiver's "store, then
  re-fetch by id" rule stays whatever the provider offers.
- **Stored values that name the processor need a data migration** (owner's decision at
  Checkpoint 12c, 2026-10-19). Since PLAN-012c the screens never name the processor from code:
  every sentence takes the display name from the company settings row (`processor_name`,
  shown as `company.provider_name`), and the `TextChoices` labels carry no name (the sentence
  around a label adds it). What still names Stax is stored data, not copy: the payment channel
  value `stax` (`PaymentChannel.PROVIDER_ACH`, on leases and transfers) and the `provider`
  column of `external_refs` and `provider_snapshots` (`stax_pay`, `payments.operator_sync.PROVIDER_KEY`).
  A change of processor therefore needs one data migration for those rows (or a new channel
  value and provider key beside the old ones, with the old rows kept as history), and nothing
  in the templates. The value freeze in `tests/test_plan012c_design_d_adversarial.py` names
  every stored choice value, so that migration is written against a known list.

## 3. Nothing here is started before Step 17

This document and its test are the whole of Prompt 12 item 7. No second adapter is written
until after cutover (Checkpoint 12).
