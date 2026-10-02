## QA Report — PLAN-009a: boolean query parameters; replay re-reads the object ID

**Date:** 2026-10-02 · **QA owner:** Fable · **Implementer:** Opus
**Origin:** owner clicks on revision 0000023: the wire payment's marking call was rejected with
`error_fields=email_receipt`; a replayed `send_invoice` event stayed "ignored: no object id".

**Causes.** (1) Python booleans in query parameters were serialised as the words `false`/`true`;
Stax's boolean validation accepts `0`/`1`. (2) A replay re-dispatched with the object ID stored at
receive time, which was empty for events stored before PLAN-008a, without re-reading the stored body.

**Scope:** `backend/payments/stax/mapping.py` (`query_bool`, used for every boolean query parameter;
`email_receipt` was the only one), `backend/payments/webhooks/processor.py` (`_recover_object_id` on
replay and on unprocessed events). Tests (Opus): `test_stax_adapter.py`, `test_manual_payment_marking.py`,
`test_webhooks.py`; three expectations updated from `"false"` to `"0"`. No migration.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .` | n/a | 0 | clean, 249 files |
| `uv run mypy` (strict) | n/a | 0 | 193 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 2,533 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 2,154 passed, 379 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 2,525 before, 2,538 after. The exact staging rejection body is pinned through the real
transport; the wire query is asserted as `email_receipt=0`; the stored envelope replays to
processed with the ID from `mergeVars.invoice.id`.

**Findings:** BLOCKER 0, MAJOR 2 resolved, MINOR 0. One function-level import of the receiver
inside the processor avoids an import cycle; accepted.

**Residual risk:** TODO-VERIFY-058, whether Stax also accepts `true`/`false` (it rejected
`false`); the next marking on staging confirms `0` is accepted. The owner marked the earlier
invoice paid by hand in the dashboard, so the retry on that charge must find it paid and send
nothing (the check-before-write path, plan D1).

**Verdict: PASS**
