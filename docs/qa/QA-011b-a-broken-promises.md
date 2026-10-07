## QA Report — PLAN-011b-a: broken promises shown at once, whatever the balance

**Date:** 2026-10-06 · **QA owner:** Fable · **Implementer:** Opus
**Origin:** owner click 1 of Prompt 11b: three promise entries on an operator with nothing owed
(two past dates, one future) showed nowhere on the attention list.

**Cause.** `contacts.broken_promises` required the operator to have an open balance and treated
any settled payment on or after the promised date as keeping the promise. The attention list is
computed live on every dashboard request (not by the daily job); the rule hid the rows.

**Fix (`backend/ledger/services/contacts.py`, one query).** A promise is broken when it is a
promise, its promised date is before today (New York), it is not closed, and no settled,
non-refund payment of that operator of at least the promised amount settled between the day the
entry was recorded and the promised date, both inclusive. Balance plays no part. The attention
text, the dashboard and the operator banners are unchanged. The owner should know: a promise
whose date lies before the day it was recorded can only be cleared by a later entry; Prompt 12's
rule (3) supersedes this.

**Tests.** Opus: 15 new functions in `test_contacts.py` (the owner's scenario end to end for
viewer, billing and admin; equal, larger, smaller, earlier, later payments; refunds, pending and
returned never count; the New York recording day; one query) and the old-rule tests replaced or
strengthened as listed in the status log. QA: `test_collections_adversarial.py` gains the owner's
scenario from the outside (two items, oldest first, `since` before today, two "not kept" lines on
the dashboard for a viewer). 4,740 → 4,753 on PostgreSQL.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .`, `uv run mypy` | n/a | 0 | clean, 401 files; 331 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | PostgreSQL 16 | 0 | 4,753 passed, 3 skipped |
| same | sqlite | 0 | 4,232 passed, 524 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff | n/a | 0 | clean |

**Findings.** MAJOR 1, resolved. **Residual:** on staging the list will now show every past, open
promise the previous rule hid; no schema change; no migration.

**Verdict: PASS** (pending the owner's refresh of the dashboard).
