## QA Report — PLAN-006c: an edit of a linked operator updates the Stax customer

**Date:** 2026-10-02 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-006c-operator-update-sync.md`

**Why this exists.** Checkpoint 8 click 1 showed that editing a linked operator saved locally
and never called Stax; the page said "linked" with no failure. The gap was a Prompt 6 deviation
that Fable accepted in QA-006 and should have returned as a finding then. Recorded as such.

**Scope:** `backend/payments/operator_sync.py` (update path, retry path, audit, masking),
`backend/payments/stax/adapter.py` (email validated only when present), `backend/ledger/views/operators.py`,
the operator detail template. Tests (Opus): `test_operator_sync.py`, `test_operator_views.py`,
`test_stax_adapter.py`. Tests (Fable): additions to `test_operators_adversarial.py`. No migration.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .` | n/a | 0 | clean, 244 files |
| `uv run mypy` (strict) | n/a | 0 | 191 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 2,440 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 2,062 passed, 378 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 2,407 before, 2,445 after. No test removed or skipped. Rows 1 to 6 of the plan each
have passing tests, including one that drives the phone edit and the email removal through the
real adapter with a mock transport and asserts the `PUT /customer/{id}` body.

**Findings**

```
[MAJOR] backend/payments/stax/adapter.py — the email was validated as required, so an update with the email removed failed locally and nothing was sent.
Status: RESOLVED in the rework: the email is validated only when non-empty and omitted from the body otherwise; firstname is always sent, so the spec's at-least-one rule holds. Raised by Opus in its report.
```

Counts: BLOCKER 0, MAJOR 1 resolved, MINOR 0. Deviations accepted: company edits alone make no
call (the payload fills Stax's company from the legal or display name); a failed linked operator
updates again on its next payload edit without waiting for the retry button; the row passes
through `pending` between the local save and the call.

**Fable's adversarial additions (5):** a rejection whose message names the API key leaves no
trace of it on the page, in the log, in the stored failure or in the audit rows; a bad phone on
edit is refused by the form before any call; an edit of an archived linked operator calls
nothing; update audit rows carry no full email; a provider outage after retries keeps the local
change and fails once with no create.

**Residual risk:** TODO-VERIFY-056: whether omitting `email` on `PUT /customer/{id}` clears it at
Stax or leaves it; the webhook mismatch flag surfaces the difference either way. Staging
acceptance (the `PUT` in the transport log, the dashboard, the webhook) is the owner's next click.

**Verdict: PASS**
