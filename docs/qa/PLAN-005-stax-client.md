# PLAN-005 — Payment provider interface and the Stax client

**Date:** 2026-09-30 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Inputs:** Prompt 5 and the owner's addendum (2026-09-30); `docs/inputs/PROMPT_SERIES_AMENDMENTS.md`
Prompt 5 section; ADR-0004, ADR-0005; `docs/CLAUDE.md` Rules 1, 2, 3, 7, 8;
`docs/inputs/STAX_ENDPOINTS_2026-09-30.md` (every path and field verified by Fable against the
specification on 2026-09-30); `docs/TODO-VERIFY.md`.

## 1. Restated objective

Build the single choke point through which every Stax call passes. A provider-agnostic
interface in our vocabulary, one Stax adapter behind it, and around the adapter a process-wide
token-bucket limiter at half of Stax's published rate, a circuit breaker that opens well before
Stax's ten-failures-per-minute lockout, retries only where a retry cannot make things worse,
local validation so that a request that would fail never leaves the process, a failure budget
that also counts failed payment attempts, and an alert that cannot be missed when the breaker
opens. Nothing in this step calls Stax except one authentication check the owner runs.

## 2. Affected packages

Root project only. `backend/payments/` (new modules and a `stax/` subpackage),
`backend/finance/env_guard.py` and `settings.py` (three new configuration values),
`.env.example`, `pyproject.toml` and `uv.lock` (one dependency, §3 D5), tests, `README.md`.
**Out of scope:** any screen, URL or admin registration; webhooks (Prompt 8); Celery and the
queue (Step 10); the sync layer that writes `provider_status` (Prompt 8); the monthly run
(Prompt 7); `infra/`; `.github/`; `backend/accounts/`; `backend/ledger/`.

## 3. Decisions taken in this plan

| # | Decision | Reason |
|---|---|---|
| D1 | Method names are Python `snake_case` versions of the prompt's names | Prompt 4 and 5 allow the stack's conventions |
| D2 | `update_scheduled_invoice` accepts only `active`, `rule` and `email_notification`. It has no way to send `total` or `meta` | Owner: schedule amounts are never edited |
| D3 | `pay_invoice` exists behind `STAX_PAY_INVOICE_ENABLED` (default off). When off it raises `FeatureDisabled` before any validation or network call | Owner addendum point 2 |
| D4 | No card method of any kind. `create_card_payment_method`, `charge` and tokenisation are not in the interface | Owner: no cards; Rule 1 |
| D5 | HTTP client: **`httpx`**, pinned exactly, added by Opus with `uv add "httpx==<latest>"`. Its own retry and redirect features are turned off (`transport=httpx.HTTPTransport(retries=0)`, `follow_redirects=False`) so no retry hides under ours | Testable mock transport, explicit timeouts. Dependency approved here by Fable; flagged in the QA report |
| D6 | Retries only on network errors, 429 and 5xx, with backoff 1 s, 4 s, 15 s and at most 3 attempts; **and** never for the three non-idempotent creates (`POST /customer`, `POST /invoice`, `POST /invoice/schedule/`) on a network error or 5xx, because Stax may have created the record. Those are retried on 429 only | Prompt 5 plus Rule 5; check-before-create is the recovery path |
| D7 | Single bucket enforced by configuration: `STAX_CALLER` is `none` (default) or `worker`. With `none` the adapter refuses every call with `ProviderNotPermittedHere`. Only the process that owns the Stax bucket runs with `worker`. The web app never does | Owner addendum point 4 |
| D8 | Failure budget counts failed HTTP calls **and** failed payment attempts reported later (returned ACH). Debits are refused when the last minute holds 5 failures or the last hour holds 25, half of Stax's 10 and 50 | Owner addendum point 5; TODO-VERIFY-014 |
| D9 | Breaker and budget state live in memory **and** in `sync_state` rows (`stax_breaker`, `stax_failure_budget`) so they survive a restart and the dashboard can read them | Owner addendum point 7 |
| D10 | Alert on open: `payments.alerts.breaker_opened()` logs at CRITICAL, sends a Sentry event with level fatal and fingerprint `stax-breaker-open`, and writes the `sync_state` row. Email paging is Sentry's alert rule on that fingerprint (owner action, see §9) | Owner addendum point 7 |
| D11 | The authentication check is `GET /self`. It compares the returned merchant ID with `STAX_EXPECTED_MERCHANT_ID` when that is set and refuses to report success on a mismatch | Rule 6; `docs/CLAUDE.md` §4 lists the variable as not yet wired |
| D12 | The interface dataclasses use exact `Decimal` money and our names (`operator_email`, not `firstname`); the adapter maps them to Stax field names in one module | Rule 8 |

## 4. Interface contract

### 4.1 `backend/payments/provider.py`

Abstract class `PaymentProvider` with these abstract methods (all synchronous):

```
create_customer(CustomerInput) -> CustomerRecord
update_customer(external_id, CustomerInput) -> CustomerRecord
get_customer(external_id) -> CustomerRecord
find_customers(CustomerQuery) -> list[CustomerRecord]
create_invoice(InvoiceInput) -> InvoiceRecord
update_invoice(external_id, InvoiceUpdate) -> InvoiceRecord
get_invoice(external_id) -> InvoiceRecord
list_invoices(InvoiceQuery) -> list[InvoiceRecord]
delete_invoice(external_id) -> None
send_invoice_email(external_id, cc: list[str] = []) -> None
mark_invoice_paid_cash_or_check(external_id, OfflinePayment) -> InvoiceRecord
pay_invoice(external_id, PayWithStoredMethod) -> TransactionRecord      # flag, D3
list_transactions(TransactionQuery) -> list[TransactionRecord]
get_transaction(external_id) -> TransactionRecord
refund_transaction(external_id, amount: Decimal) -> TransactionRecord
void_transaction(external_id) -> TransactionRecord
list_deposits(DateRange) -> list[DepositRecord]
get_deposit(DateRange, batch_id) -> DepositDetail
create_scheduled_invoice(ScheduleInput) -> ScheduleRecord
get_scheduled_invoice(external_id) -> ScheduleRecord                  # TODO-VERIFY-020
list_scheduled_invoices(ScheduleQuery) -> list[ScheduleRecord]
update_scheduled_invoice(external_id, ScheduleUpdate) -> ScheduleRecord   # D2
delete_scheduled_invoice(external_id) -> None
list_ach_rejections(DateRange) -> list[AchRejection]
ping() -> ProviderIdentity
```

Dataclasses are frozen, typed, and hold `Decimal` for money. Every record carries
`external_id: str`, `raw_status: str` (the provider string, unmodified, Rule 2) and
`fetched_at: datetime`. `raw_summary` on records is already trimmed per Rule 1: at most
`id`, `method`, `card_type`, `card_last_four`, `card_exp`, `nickname` of any payment-method
object, and never account or routing data. Errors form one hierarchy in
`backend/payments/errors.py`: `ProviderError` → `ValidationFailed` (never sent),
`ProviderNotPermittedHere` (D7), `FeatureDisabled` (D3), `CircuitOpen`, `FailureBudgetExhausted`,
`RateLimitWaitExceeded`, `TransportError`, `ProviderRejected` (4xx, with `status`),
`ProviderUnavailable` (5xx or 429 after retries), `IdentityMismatch` (D11).

`backend/payments/fake.py`: `FakeProvider`, in memory, implements every method, records calls,
can be told to fail. Used by tests and local development. Nothing Stax-specific in it.

### 4.2 `backend/payments/stax/` (the only place that knows Stax)

| Module | Content |
|---|---|
| `adapter.py` | `StaxProvider(PaymentProvider)`. Maps our dataclasses to the verified paths and field names in `docs/inputs/STAX_ENDPOINTS_2026-09-30.md`. Every path constant carries the page name it was verified against. Unverified points carry `# TODO-VERIFY-nnn` |
| `transport.py` | `httpx.Client` with `base_url` from settings, `Authorization: Bearer` from `STAX_API_KEY`, `Accept`/`Content-Type: application/json`, `timeout=15.0` on connect, read, write and pool, no redirects, library retries 0. One method `send(method, path, *, params, json, idempotent: bool) -> Response` that runs, in order: caller check (D7), breaker check, limiter acquire, request, classification, retry policy (D6), breaker and budget update, log line |
| `limiter.py` | `TokenBucket` pair: 2 per second (burst 2) and 100 per minute (burst 100), process-wide singleton, thread-safe, monotonic clock injectable for tests. `acquire(max_wait=30.0)` blocks, and raises `RateLimitWaitExceeded` past `max_wait`. Comments explain why the bucket is process-wide and how D7 keeps it global |
| `breaker.py` | Opens on 3 consecutive 4xx responses **or** 5 failures (4xx, 5xx, network) within 60 s. Open for 300 s: every call raises `CircuitOpen` without touching the network. After 300 s one trial call is allowed (half-open); success closes, failure re-opens for another 300 s. State persisted to `sync_state` (D9). Calls `payments.alerts.breaker_opened()` exactly once per opening |
| `budget.py` | Sliding windows of failure timestamps, 60 s and 3600 s, persisted to `sync_state`. `record_http_failure()`, `record_payment_failure(external_id)` (for Prompt 8 to call on a returned ACH), `check_can_debit()` raising `FailureBudgetExhausted` at 5 per minute or 25 per hour (D8). Used before `pay_invoice` and before any future debit-creating call |
| `validation.py` | Local checks before any request: email syntax (Django `validate_email`), `amount > 0` with at most two decimals, `total == subtotal + tax`, line items sum to `subtotal`, `idempotency_id` is a UUID string of at most 255 characters, `rule` is an RRULE string beginning with `DTSTART=`, date ranges start before end and span at most 366 days, IDs are non-empty strings without whitespace. A failure raises `ValidationFailed` and never reaches the transport |
| `mapping.py` | Our field names ↔ Stax field names, in one place. Money to string with two decimals where the spec types it as string (`refund total`), to number where it types it as number |
| `logging.py` or inside `transport.py` | One INFO line per request: `stax method=GET path=/invoice/{id} status=200 ms=123 object_id=<id>`; on failure the class of failure. **Never** a body, header, token, email or amount |

### 4.3 Configuration

| Name | Values | Default | Where enforced |
|---|---|---|---|
| `STAX_CALLER` | `none`, `worker` | `none` | `env_guard.py`; the adapter reads `settings.STAX_CALLER` |
| `STAX_PAY_INVOICE_ENABLED` | `true`, `false` | `false` | `env_guard.py`; `pay_invoice` |
| `STAX_EXPECTED_MERCHANT_ID` | string | empty | already documented; now read by `ping()` |

All three documented in `.env.example` with empty values and in the README table. The guard's
value-free error messages remain value-free.

### 4.4 Management command `provider_ping` (app `payments`)

Runs `ping()` through the real adapter and prints only: provider name, environment, masked
merchant ID (first 4 and last 4 characters), and `merchant matches expected: yes/no/not set`.
Exit 0 on success, 1 on `IdentityMismatch`, 2 on any other failure. **No retry loop**: a
failed authentication is reported once. This is the "trivial read call" of the owner's
addendum point 6; the owner runs it on staging.

### 4.5 Sandbox test suites (pytest marker `sandbox`, always skipped unless enabled)

- `backend/tests/sandbox/test_authentication.py`: enabled by `STAX_SANDBOX_TESTS=1` and a
  non-empty `STAX_API_KEY`; one `ping()`; asserts an identity is returned. No retry.
- `backend/tests/sandbox/test_pay_invoice_documented.py`: enabled additionally by
  `STAX_SANDBOX_CUSTOMER_ID` and `STAX_SANDBOX_PAYMENT_METHOD_ID` (a sandbox customer whose
  bank method staff entered in the dashboard) and `STAX_PAY_INVOICE_ENABLED=true`. Creates
  one sandbox invoice for 1.00 with a memo naming the test and date, calls `pay_invoice`,
  and writes the raw status and the resulting transaction status to the test log as the
  documented outcome. It never runs by default and never against `STAX_ENV=live`
  (asserts `sandbox` first).

## 5. Work breakdown

| Batch | Task | Files |
|---|---|---|
| A | A1 errors, dataclasses, interface, fake | `backend/payments/errors.py`, `provider.py`, `fake.py` |
| A | A2 configuration | `backend/finance/env_guard.py`, `settings.py`, `.env.example`, `pyproject.toml`, `uv.lock` (httpx only) |
| A | A3 limiter, breaker, budget, alerts | `backend/payments/stax/__init__.py`, `limiter.py`, `breaker.py`, `budget.py`, `backend/payments/alerts.py` |
| A | A4 transport, validation, mapping, adapter | `backend/payments/stax/transport.py`, `validation.py`, `mapping.py`, `adapter.py` |
| A | A5 `provider_ping`, sandbox suites, marker registration | `backend/payments/management/commands/provider_ping.py`, `backend/tests/sandbox/…`, `pyproject.toml` (`markers` only) |
| A | A6 tests | `backend/tests/test_provider_interface.py`, `test_stax_limiter.py`, `test_stax_breaker.py`, `test_stax_budget.py`, `test_stax_retries.py`, `test_stax_validation.py`, `test_stax_adapter.py`, `test_stax_isolation.py` |
| B | B1 README rows | `README.md` |

`docs/STAX_CLIENT.md` is written by Fable after the gate.

## 6. Test plan

| # | Case | Author |
|---|---|---|
| a | **4xx never retried:** for 400, 401, 403, 404, 409, 422, one request leaves the transport, `ProviderRejected` raised, no sleep called | Opus |
| b | **Breaker opens:** after exactly 3 consecutive 4xx; after 5 mixed failures within 60 s; not after 2 consecutive 4xx; not after 5 failures spread over 61 s. While open, calls raise `CircuitOpen` with zero transport calls for 300 s; the half-open trial is allowed once; alert hook called exactly once per opening | Opus |
| c | **Rate under burst:** 50 calls from 8 threads with a fake clock: no second contains more than 2 departures and no minute more than 100; with a real clock 6 calls take at least 2.0 s | Opus |
| 4 | 429 and 500, 502, 503, 504 retried with sleeps 1, 4, 15 and at most 3 attempts; the 4th failure raises `ProviderUnavailable`; network error same | Opus |
| 5 | Non-idempotent creates: network error and 5xx not retried; 429 retried (D6) | Opus |
| 6 | Every validation rule of §4.2 rejects and accepts; nothing reaches the transport on rejection | Opus |
| 7 | `STAX_CALLER=none`: every method raises `ProviderNotPermittedHere` before any network call | Opus |
| 8 | `pay_invoice` flag off: `FeatureDisabled` before validation; flag on: `idempotency_id` present and a UUID, request is `POST /invoice/{id}/pay` with query parameters as verified | Opus |
| 9 | `update_scheduled_invoice` cannot send `total` or `meta` (no such field exists on its input; the request body has no such key) | Opus |
| 10 | Budget: 5 failures in a minute or 25 in an hour make `check_can_debit` raise; a payment failure counts like an HTTP failure; windows expire | Opus |
| 11 | Log lines contain method, path, status, duration and object ID, and never a body, token, email or amount (the token and a body marker are asserted absent) | Opus |
| 12 | Isolation: no module outside `backend/payments/` imports `payments.stax`; no file outside `backend/payments/` contains the string `stax` except `finance/settings.py`, `finance/env_guard.py`, the banner and the tests. The ledger's `payment_channel` value is the allowed exception | Opus |
| 13 | Every path used by the adapter appears in `docs/inputs/STAX_ENDPOINTS_2026-09-30.md` (test reads the file) | Opus |
| 14 | `httpx` library retries are 0 and redirects off (inspect the client) | Opus |
| 15 | Breaker state and budget survive a new adapter instance (read back from `sync_state`) | Opus |
| 16 | Adversarial: 5xx that returns a body with a card-like number is not logged; a 4xx body with the token is not logged; a response that is not JSON; a 200 with an unexpected shape; a 30-second stall hits the 15 s timeout | Fable |
| 17 | Adversarial: breaker under 50 concurrent threads opens exactly once and alerts once; limiter fairness (no thread starves beyond `max_wait`) | Fable |
| 18 | Adversarial: money mapping for `12581.00`, `0.01`, `9999999999.99` and rejection of `float` inputs at the interface | Fable |
| 19 | Adversarial: a retry after a network error on `pay_invoice` reuses the same `idempotency_id` | Fable |
| 20 | Whole suite on PostgreSQL and sqlite, lint, format, strict types, secret scan; `provider_ping` refuses without `STAX_CALLER=worker` | Fable |

## 7. Risk register

| Risk | Treatment |
|---|---|
| A retry hidden in the HTTP library | D5, test 14 |
| Two processes each with their own bucket | D7; the web app runs with `STAX_CALLER=none`; the worker (Step 10) runs one replica; recorded in `docs/STAX_CLIENT.md` and to be enforced in Bicep at Step 10 |
| Breaker opens and nobody notices | D10; Sentry alert rule is an owner action listed in §9 |
| A malformed batch triggers the lockout | Validation before send; breaker at 3 consecutive 4xx; budget |
| Sandbox test creates or charges something | The authentication test is `GET /self` only; the pay test is off by default, sandbox-only, 1.00, and needs three explicit variables |
| Secrets in logs | Test 11 and 16; transport never logs bodies or headers |
| The one-off invoice question (TODO-VERIFY-019) | Left open on purpose; the flagged method and the documented test are how the owner decides |

## 8. Acceptance criteria

1. `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` exit 0.
2. Both pytest commands exit 0 on PostgreSQL 16 and sqlite; the `sandbox` marker is deselected
   by default and its tests are skipped when selected without the variables.
3. Tests a, b and c exist, are read by Fable, and assert what they claim.
4. No existing test edited, removed or skipped. `pyproject.toml` changes are the `httpx` pin and
   the `sandbox` marker only.
5. Every unverified point in the code is a `# TODO-VERIFY-nnn` with a row in `docs/TODO-VERIFY.md`
   (rows added by Fable from Opus's list).
6. `provider_ping` on staging, run by the owner, reports a matching identity.

## 9. Owner actions after the gate

- Run `provider_ping` on staging (command in the QA report).
- Create the Sentry project and set `sentry-dsn`, then an alert rule on fingerprint
  `stax-breaker-open` that emails the owner. Until Sentry exists, the CRITICAL log line and
  the `sync_state` row are the only signals.
- Ask Stax support about IP whitelisting for `20.12.221.227` (TODO-VERIFY-010) and about the
  50-failures-per-hour block (TODO-VERIFY-014).
- Decide TODO-VERIFY-019 after the documented sandbox test has run.
