## QA Report — PLAN-005: payment provider interface and the Stax client

**Date:** 2026-09-30 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-005-stax-client.md`

**Scope:** root project. `backend/payments/` (`errors.py`, `provider.py`, `fake.py`, `alerts.py`,
`management/commands/provider_ping.py`, `stax/` with `adapter.py`, `transport.py`, `limiter.py`,
`breaker.py`, `budget.py`, `validation.py`, `mapping.py`, `clock.py`, `state.py`),
`backend/finance/env_guard.py` and `settings.py` (three configuration values, one logger entry),
`.env.example`, `pyproject.toml` and `uv.lock` (`httpx==0.28.1`, `sandbox` marker), `README.md`.
Tests (Opus): eight `test_stax_*`/`test_provider_interface.py` files, `stax_helpers.py`,
`backend/tests/sandbox/`. Tests (Fable): `backend/tests/test_stax_adversarial.py`.
Docs (Fable): `docs/STAX_CLIENT.md`, `docs/inputs/STAX_ENDPOINTS_2026-09-30.md`, TODO-VERIFY rows 020 to 030.
Unchanged, verified: `infra/`, `.github/`, `Dockerfile`, `backend/accounts/`, `backend/ledger/`.

**New dependency, approved by Fable in plan D5:** `httpx==0.28.1` (BSD-3), with `anyio`,
`h11`, `httpcore` as its locked transitive set. Its retries are set to 0 and redirects off,
asserted by test.

**Commands run by Fable after rework cycle 1:**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .` | n/a | 0 | clean |
| `uv run ruff format --check .` | n/a | 0 | 145 files |
| `uv run mypy` (strict) | n/a | 0 | 112 source files |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 1,289 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 977 passed, 312 skipped (PostgreSQL-only) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 2 passed |
| `uv run pytest -m sandbox -q` | either | 0 | 2 skipped by design (no variables) |
| limiter, breaker, retries and adversarial files, two consecutive runs | PostgreSQL 16 | 0 | 96 passed each time |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**No network call to Stax was made** by either model during this task. The authentication
check on staging is the owner's step (below).

**The three acceptance tests, read by Fable:**
- **(a) 4xx never retried:** `test_a_4xx_is_sent_once_and_never_retried` asserts, for 400,
  401, 403, 404, 409 and 422, that exactly one request reached the mock transport, that
  `ProviderRejected` carries the status, and that the clock recorded no sleep.
- **(b) breaker opens:** eleven tests assert the state after exactly three consecutive 4xx,
  after five mixed failures inside 60 s, not after two 4xx, not after five failures over 61 s,
  zero transport calls for 299.999 s while open, one trial when half-open, one alert per
  opening.
- **(c) two per second under a burst:** 50 calls from 8 threads with a fake clock; the test
  collects departure timestamps and asserts at most 2 in any sliding second, at most 2 per
  calendar second, at most 100 per minute, and at least 24 s of recorded waiting. A second
  test with the real clock shows 6 calls take at least 2.0 s.
They assert what they claim.

**Fable's adversarial tests (25):** 5xx and 4xx bodies holding a token, a card number, an
email and a marker never reach the log or the error text; non-JSON, empty, list-shaped and
redirect responses are handled without leaking; hostile object IDs (`../self`, spaces, encoded
slashes, 256 characters, a newline) never leave the process; 50 threads against a failing
endpoint open the breaker once and alert once; no limiter waiter is served after its own
allowance; money round-trips exactly at three magnitudes and floats are refused; a retried
`pay_invoice` reuses the same `idempotency_id`; date ranges longer than a year never leave.

**Findings**

```
[MAJOR] backend/finance/settings.py — the transport's INFO lines were dropped in deployment (root logger at WARNING).
Status: RESOLVED in rework cycle 1 ("payments" logger at INFO).

[MAJOR] backend/tests/sandbox/ — the sandbox suites could never receive a real key (the test conftest overwrites STAX_API_KEY).
Status: RESOLVED in rework cycle 1 (STAX_SANDBOX_API_KEY, read only by those suites).

[MINOR] backend/finance/log_redaction.py — STAX_SANDBOX_API_KEY is not in the redacting formatter's secret list.
Failure scenario: an unrelated log line that happened to contain the sandbox test key would not be redacted. The transport never logs a key.
Decision: deferred to the Prompt 8 plan, which next touches settings.

[MINOR] backend/payments/management/commands/provider_ping.py — one `# noqa: E501` on a docstring line holding the 144-character owner command.
Decision: accepted; the command must be copyable.
```

Counts: BLOCKER 0, MAJOR 2 resolved, MINOR 2 (1 deferred, 1 accepted). Rework cycles: 1.
All thirteen deviations in Opus's report are accepted; the material ones: time is integer
milliseconds (no `float` anywhere), the limiter returns each token one period after it was
spent, four attempts in total, 429 does not count towards the three-consecutive-4xx streak,
refund and offline payment are treated as non-idempotent, identifiers are restricted to
`[A-Za-z0-9_-]{1,255}`.

**Gate 3:** transport, limiter, breaker, budget, adapter and mapping read in full. One choke
point; collaborators injectable; no dead code; the isolation test's allow-list is explicit.
**Gate 4:** no body, header or token in any log or error; timeouts on all four phases;
validation before send; `STAX_CALLER=none` refuses before the network; feature flag refuses
before validation. Secret scan of the diff: clean. No secret store read by either model.

**TODO-VERIFY left in code:** 016, 020 to 030 (rows added), plus references to 001, 004, 014,
019. The one that matters before the first create is **027**, the `url` field on invoice and
schedule creation, whose value came from memory, not the specification.

**Residual risk**
- Nothing has run against Stax. Every response shape is an assumption until `provider_ping`
  and the first sandbox reads in Prompt 6.
- The single-bucket rule is enforced in configuration; the single-replica worker is a Bicep
  change at Step 10.
- The breaker's alert reaches a person only once Sentry and its alert rule exist (owner action).
- Deferred MINOR findings: QA-003a (3), QA-004 (1), this report (1).

**Verdict: PASS.** Acceptance criterion 6 (the identity check on staging) is verified after
the owner's push and ping.
