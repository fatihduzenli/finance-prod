# PLAN-006a — Sync diagnostics: 4xx field names, retry lock, create ID in the log

**Date:** 2026-10-01 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Origin:** Checkpoint 6 test 1 on staging (a 422 whose reason no log recorded; five further
attempts in one minute opened the breaker). Owner instruction of 2026-10-01: "Log error field
names (never values) on 4xx, lock the retry button for 60 seconds, post-redirect-get. Include
the new customer ID in the create log line."

## 1. Restated objective

When Stax rejects a request, the log must say **which fields** Stax complained about, never
what it said about them. A failed operator cannot be retried more than once a minute, by the
server, not just the button. Every POST on the operator screens ends in a redirect, so a
browser refresh never resubmits. A successful create logs the ID Stax assigned.

## 2. Affected packages

Root project: `backend/payments/stax/transport.py`, `backend/payments/errors.py` (one attribute),
`backend/payments/operator_sync.py`, `backend/ledger/views/operators.py`, the detail template,
tests, `docs/STAX_CLIENT.md` (Fable). **Out of scope:** everything else.

## 3. Interface contract

### 3.1 4xx field names (transport)

On a 4xx response (including 422, 400, 404; and 429 after its retries), the transport reads
the body **once** as JSON and extracts **key names only**:
- top-level keys of a JSON object;
- for the common shapes `{"errors": {...}}`, `{"error": {...}}`, `{"message": ..., "errors": {...}}`,
  the keys one level below `errors` / `error` as well;
- if the body is a JSON list or a scalar: the word `list` or `scalar`;
- if the body is not JSON: `non-json`.
Each key is reduced to `[A-Za-z0-9_.-]`, cut to 40 characters; at most 20 keys; sorted.
Logged on the existing line as `error_fields=email,lastname` (comma-separated, no spaces).
**No value, message text, or nested scalar is ever read into a string that is logged,
stored or raised.** The list is also attached to `ProviderRejected` as `error_fields:
tuple[str, ...]` so the service can store it.

### 3.2 Create ID in the log

For `POST /customer`, `POST /invoice`, `POST /invoice/schedule/`, `POST /invoice/{id}/pay`,
`POST /transaction/{id}/refund` and `POST /transaction/{id}/void`, a 2xx response's `id`
(top-level string, at most 64 characters of `[A-Za-z0-9_-]`) is logged as `object_id=<id>`
instead of `-`. Only the `id` key is read for this purpose.

### 3.3 Retry lock (service)

`sync_operator(..., retry=True)` refuses with a new outcome `RETRY_TOO_SOON` when
`sync_attempted_at` is less than **60 seconds** old. The detail page shows the retry button
only when the lock has passed, and otherwise the sentence "Retry available in N seconds."
The view maps `RETRY_TOO_SOON` to a message, never to an exception. The check is inside the
row lock so two clicks cannot both pass.

### 3.4 Post-redirect-get (views)

Every POST handler (`create`, `edit`, `archive`, `retry_sync`, `link`) returns a redirect on
**every** outcome, including validation errors. A form with errors is re-rendered by the
following GET from the session (`request.session["operator_form_errors"]`, consumed once), or,
simpler and acceptable, the invalid form data and errors are kept in the session for one GET.
Choose one and say which. Refreshing the page after any POST issues a GET only.

### 3.5 Stored diagnostics

`operators.sync_failure` keeps the class name. New column `sync_failure_fields` (text, blank,
guarded by the card-number guard, migration 0005) holds the comma-separated field names from
3.1 when the failure is `ProviderRejected`. The detail page shows it as "Fields the provider
rejected: email, lastname". `audit_log` `operator.sync_failed` rows carry the same list.

## 4. Test plan

| # | Case | Author |
|---|---|---|
| 1 | 422 with `{"email": ["bad"], "lastname": ["required"]}` → log line has `error_fields=email,lastname`, no value string appears in log, error or audit; `ProviderRejected.error_fields == ("email", "lastname")` | Opus |
| 2 | Shapes: `{"errors": {...}}`, `{"error": {...}}`, list, scalar, non-JSON, empty body, 25 keys (cut to 20), key with hostile characters (reduced), nested value containing a card number and the token (never logged) | Opus |
| 3 | 2xx create logs `object_id=<id>`; a non-conforming `id` logs `-`; 2xx on GET unchanged | Opus |
| 4 | Retry within 60 s refused with `RETRY_TOO_SOON`, no provider call; at 60 s allowed; two concurrent retries → one call (PostgreSQL) | Opus |
| 5 | Every POST route redirects on success and on failure (status 302 for valid and invalid data); the following GET shows the errors once and a second GET shows none | Opus |
| 6 | `sync_failure_fields` stored and shown; cleared on a later success; guarded against card numbers | Opus |
| 7 | Adversarial: error body of 5 MB; keys that are numbers, empty strings, unicode; a 4xx whose JSON is deeply nested; the token or a card number as a key name | Fable |
| 8 | Whole suite both engines, lint, format, types, secret scan | Fable |

## 5. Risk register

| Risk | Treatment |
|---|---|
| A value leaks through a key name | Keys reduced to a safe alphabet and cut; test 2 and 7 |
| Reading a huge body | Read at most 64 KB of the body for key extraction |
| Session-held form data holds a card number | The form never accepted it (validator); stored for one GET only |

## 6. Acceptance criteria

Lint, format, strict types exit 0; both pytest commands exit 0 on both engines; no existing
test removed or skipped (Fable updates the two log-format assertions in
`test_stax_adversarial.py` if the new token changes them); every row of §4 has a passing
test; `makemigrations --check` clean; after deployment the next 4xx on staging shows
`error_fields=` in the log.
