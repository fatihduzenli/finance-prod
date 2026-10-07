## QA Report — PLAN-012c-b: promise outcomes

**Date:** 2026-10-07 · **QA owner:** Fable · **Implementer:** Opus
**Origin:** owner's finding 5 (a)–(d) of Prompt 12 on staging.
**Plan:** `docs/qa/PLAN-012c-b-promise-outcomes.md` (D2 amended 2026-10-07: no remark contact
kind exists, so Kept and Broken ask how the outcome was learned).

**Scope.** `backend/ledger` (`services/contacts.py`: "open" outcome, `_close_superseded`,
`broken_promises` one per bucket; `services/attention.py`; `forms.py`: `OUTCOME_CHOICES`,
`ContactOutcomeForm`; `views/contacts.py`: the outcome route, `local_next`, the store choice;
`views/contact_log.py`, `views/dashboard.py`, `views/operators.py` for the buttons; `urls.py`;
templates; data migration `0033_one_open_promise_per_lease`), `accounts/registry.py`, tests. No
schema change, no provider or environment change.

**Commands run (final code).** Env as in QA-012; PostgreSQL 16 in a throwaway container.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean |
| `uv run mypy` (strict) | n/a | 0 | no issues in 354 source files |
| `uv lock --check` | n/a | 0 | unchanged |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 4,636 passed, 577 skipped (4,612 before) |
| same | PostgreSQL 16 | 0 | 5,210 passed, 3 skipped (5,185 before) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff and new files | n/a | 0 | clean |
| Bicep build | n/a | — | not applicable: `infra/` untouched |

Baseline before PLAN-012c-b (QA-012c-a): PostgreSQL 5,185 passed; sqlite 4,612 passed.

**Tests (Gate 2).** Opus (`test_contact_log.py`, `test_contacts.py`,
`test_migrations_roundtrip.py`): "Still open" through the form with and without the field and
its audit; the four outcomes through the previous-promise block; the buttons on the operator
page, its log page, the store page and its log page for billing only, none on closed, deleted or
archived promises, `next` encoded; Kept, Reschedule and Broken each close the promise exactly as
the previous-promise path (status, `closed_by`, audit); Reschedule pre-fill and the refusal
without a new date; Broken from the dashboard returning to the dashboard anchor; refusals for a
promise closed meanwhile, deleted, or on an archived operator; a card number in the note, a
forged `kind=promise`, 404s; the store choice when a no-store promise's operator has several
leases; D3's warning, message, audit, other leases and the no-store bucket untouched, a deleted
promise never a candidate, the null `closed_by` case; the labels and the registry rule; the
0033 round trip over seeded rows on both engines (a chain with a link already closing another
promise, a deleted promise, another lease, the no-lease bucket, forward, back, forward, the
audit rows).

Fable: `test_plan012c_b_adversarial.py` (6 cases): the outcome route cannot close another
operator's promise by forged `previous` or `operator` fields and a second outcome on a closed
promise writes nothing; `next` to an external host, a protocol-relative path, a `javascript:`
URL or a backslash path never leaves the site; a viewer has no buttons and gets 403 on the
three routes; a new promise closes only its own lease's open promise, never another lease's
nor a deleted one; the attention list and `broken_promises` show one promise per bucket even
with two open rows forced in by a raw update; migration 0033's forward function keeps the
newest, chains the closers, writes two audit rows, and a second run changes nothing.
`test_collections_adversarial.py`: the 11b scenario rewritten onto three leases (two broken
items) plus the one-bucket case (two Rescheduled, one Open, `closed_by` chained). Opus reported
two defects in Fable's migration test (a `closed_by` expectation that one-to-one cannot meet,
and a schema editor opened inside sqlite's atomic block) and did not edit the file; Fable fixed
both.

No test weakened, skipped or deleted; two of Opus's earlier tests changed meaning with D1 and
D3 and were updated accordingly (listed in the report). Coverage is not measured.

**Findings (Gates 3 and 4).** None blocking. Design choices accepted as built: the store choice
on the outcome form for a no-store promise whose operator has several leases (an entry must
name one; the plan did not foresee it); `closed_by` chains because the column is one-to-one,
and stays null when the closer already closes another promise (the audit row carries the
link); `broken_promises` keeps the most recently recorded per bucket in one query; `next` is
validated as a same-site path with the fragment stripped and defaults to the operator page;
the warning on the add form updates by a small inline map when the store select changes.

Gate 4: the outcome route checks billing (registry and view), resolves the promise by id and
its operator from the row, ignores every posted field outside the form, and refuses a promise
no longer open; the redirect guard; the auto-close runs under the operator's row lock on the
same bucket only; the migration is idempotent and reverse is a no-op; no new secret.

**Confidentiality gate (§5.4).** Secret scan clean; no secret-store read; no new file type; no
new secret name.

**Residual risk.**
- The data migration changes staging rows (sena's promises): the newest stays open, the older
  ones become Rescheduled with audit rows; reverse is a no-op by design.
- No browser test of the buttons or of the warning that follows the store select.
- The migration has not been run on staging data; the owner's click on sena is the check.

**Verdict: PASS** (pending the owner's clicks).
