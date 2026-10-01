## QA Report — PLAN-008: webhook receiver, processor, payment state machine, ACH return path

**Date:** 2026-10-01 · **QA owner:** Fable · **Implementer:** Opus (two batches plus one fix) · **Plan:** `docs/qa/PLAN-008-webhooks.md`

**Scope:** `backend/payments/webhooks/` (receiver, events, queue, processor, handlers for customer,
invoice, schedule, transaction, deposit, dispute, registration), `payments/redaction.py`,
`payments/admin.py`, `payments/invoice_sync.py`, `payments/alerts.py`, `payments/provider.py`,
`stax/adapter.py`, `stax/mapping.py`, `stax/validation.py`, `fake.py`, commands `process_webhooks`,
`register_webhooks`, `unregister_webhooks`, `provider_ping` (docstring), migration `payments.0003`
(event source, payload, attempts, duplicates, trigger), migration `ledger.0009` (return resolution
columns, trigger), `ledger/services/returns.py`, `rent_run.py` (matching reuse), views and templates
for `/webhooks/` and `/returns/`, `finance/urls.py`, `finance/sentry.py` and the `init` call in
`settings.py`, `accounts/registry.py`, base template nav, `README.md`.
Tests (Opus): `test_webhooks.py`, `test_webhook_handlers.py`, additions to `test_sentry_scrub.py`,
`test_route_registry.py`, `test_payments_models.py`, `test_card_number_guard.py`, `test_migrations_roundtrip.py`,
`test_stax_adapter.py`, `test_provider_interface.py`, `test_ledger_models.py`. Tests (Fable): `test_webhooks_adversarial.py`.
Docs (Fable): `docs/RUNBOOK.md`, DATA_OWNERSHIP §3, TODO-VERIFY 039 to 054.
Unchanged, verified: `pyproject.toml`, `uv.lock`, `infra/`, `.github/`, `Dockerfile`, auth middleware.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .` | n/a | 0 | clean, 230 files |
| `uv run mypy` (strict) | n/a | 0 | 182 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 2,247 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 1,875 passed, 372 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| the four webhook and Sentry files, two consecutive runs | PostgreSQL 16 | 0 | 194 passed each time |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 2,048 before, 2,252 after. Two existing assertions were changed by Opus for stated
reasons (the public-route set now includes the receiver; a Sentry test asserted the request URL
survives, which the new rule forbids). No test removed or skipped.

**The four mandatory tests of Prompt 8, read by Fable**
- Replay five times: byte-identical redeliveries store one row and count four duplicates;
  five distinct deliveries of every event kind leave the state of the first; replaying every
  stored event changes nothing.
- Wrong or missing token: ten token variants and every non-POST method get an empty 404, write
  no row, and produce one receiver log line with `[token]` in place of the path segment.
- **Lying payload:** a body claiming PAID while the re-fetch says SENT leaves the invoice SENT
  and the charge unpaid; an AST test proves no handler module reads a stored event or its body;
  a transaction body claiming settled while the re-fetch is pending pays nothing.
- Refund: a new payment with `is_refund` linked to its parent, parent invoice re-fetched and
  updated; a refund whose parent was never seen records the parent first.

**Fable's adversarial tests (19):** the compare is the constant-time function; the token never
appears in any log line on a 200 or a 404; a card number in a body is redacted in the stored
payload and absent from the error text; eight hostile object IDs never reach the provider;
300 distinct events dedupe cleanly; the same delivery arriving twice at the same moment stores
one row (PostgreSQL); viewer cannot replay, billing can; an invoice of another operator's
customer is not linked to ours; a transaction for another operator's invoice does not pay our
charge; `provider_status` is written only through the sync writer during processing; a lying
settled body pays nothing; a Sentry event with the token in URL, query, breadcrumb and message
comes out clean.

**Findings**

```
[MAJOR] backend/finance/sentry.py — HTTP breadcrumbs could carry the webhook URL token to Sentry.
Failure scenario: register_webhooks sends the target URL (with the token) as a query parameter; sentry-sdk's httpx integration records the URL and query in a breadcrumb; a later error event in the same process would carry it.
Status: RESOLVED. before_breadcrumb strips url, query and fragment from HTTP breadcrumbs; scrub_event does the same on event breadcrumbs and replaces every occurrence of the token and the API key (raw and URL-encoded) anywhere in the event. Raised by Opus in its Batch B report; fixed in a third assignment; covered by Opus's and Fable's tests.
```

Counts: BLOCKER 0, MAJOR 1 resolved, MINOR 0. Deviations accepted, the material ones: staff
route names under `ledger:webhooks:*`; a catch-all receiver pattern so every token variant gets
the silent 404; event names from the body accepted only when they are one of the fifteen known
names; settlement only on `update_transaction_settled` with `settled_at`, return on any event
that shows `achDescription`; `returned` and `voided` are final; an invoice that names an active
schedule but cannot be matched yet is left for the rent run; a provider 404 on re-fetch is
`ignored: not at the provider`; deposits are re-fetched through the last 31 days' list.

**Gate 3 and 4:** receiver, processor, handlers and redaction read in full. Handlers receive the
event type and the ID only. One worker thread; limiter and breaker in front of every re-fetch;
no retry loop; dead letter after three. The receiver imports nothing from the adapter package
(AST test). Secret scan clean. No secret store read by either model.

**Sandbox proof**
- Possible now: customer, invoice, schedule events; registration; wrong-token 404; replay.
- **Blocked on Stax ACH:** transaction, settlement, return, refund, void and deposit events.
  Unit-tested on shapes built from the specification's examples (TODO-VERIFY-041, 045 to 049,
  052, 054).

**Residual risk**
- Processing runs in the web process's worker thread until Step 10; an event in flight during
  a restart is caught by `process_webhooks`.
- Every provider key named in TODO-VERIFY-039 to 054 is an assumption until the first real
  deliveries.
- The admin group has no view permission on webhook events (superuser only in the admin); the
  `/webhooks/` page is the staff surface. MINOR, deferred.
- Deferred MINOR findings from earlier reports: nine, unchanged.

**Verdict: PASS.** Acceptance on staging (the owner's four clicks) follows the push and the
registration.
