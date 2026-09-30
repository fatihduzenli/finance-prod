## QA Report — PLAN-006: operators module, boot-time identity check, reserved tables

**Date:** 2026-09-30 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-006-operators.md`

**Scope:** root project. `backend/ledger/` (views, forms, urls, admin, templates, migrations
0003 and 0004, models), `backend/payments/` (`operator_sync.py`, `get_provider()`,
`provider_environment_check`, transport boot path), `backend/accounts/registry.py` (rules only),
`backend/finance/` (`urls.py`, `env_guard.py`, `settings.py`, base template), `Dockerfile`
(`CMD`), `infra/` (two parameters, staging values), `.env.example`, `README.md`.
Tests (Opus): `test_operator_sync.py`, `test_operator_views.py`, `test_provider_environment_check.py`,
`test_reserved_tables.py`, `e2e/test_operators.py`. Tests (Fable): `test_operators_adversarial.py`;
three existing tests updated by Fable because they pinned the previous schema and image command
(`test_card_number_guard.py`, `test_ledger_models.py`, `test_staging_defects_adversarial.py`).
Unchanged, verified: `pyproject.toml`, `uv.lock`, `.github/`, the auth middleware.

**Commands run by Fable after rework cycle 1:**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .` | n/a | 0 | clean |
| `uv run ruff format --check .` | n/a | 0 | 163 files |
| `uv run mypy` (strict) | n/a | 0 | 127 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 1,483 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 1,137 passed, 346 skipped (PostgreSQL-only) |
| `uv run pytest -m e2e -q` (Playwright) | PostgreSQL 16 | 0 | 3 passed, incl. the double-click test |
| operator and boot-check files, two consecutive runs | PostgreSQL 16 | 0 | 138 passed each time |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |
| `az bicep build --file infra/main.bicep` | n/a | 0 | compiles |
| image built and run locally, empty database, local env | PostgreSQL 16 | 0 | migrate, check skipped, one gthread worker, health 200, login 200, operators 401 |
| image run as staging with an unreachable Stax host | — | refused | check failed with `TransportError`; gunicorn never started |

**Tests:** 1,001 before, 1,486 after. No test removed or skipped; three existing tests updated
by Fable for the new schema and command, as recorded above. Every row of plan §6 has a passing test.

**Findings**

```
[MAJOR] Dockerfile:90 — three gunicorn worker processes while the web app is the single Stax caller.
Failure scenario: three independent limiters and breakers; up to 6 requests per second from one replica.
Status: RESOLVED in rework cycle 1: one process, gthread, four threads.

[MAJOR] backend/ledger/views/operators.py:128 — a NUL byte in the search string returned 500.
Failure scenario: GET /operators/?q=%00 → database error → Internal Server Error on demand.
Status: RESOLVED in rework cycle 1: control characters removed before the query. Found by a Fable adversarial test.

[MINOR] backend/ledger/admin.py — the admin group has no view permission on operators, so only a superuser sees the read-only admin page.
Decision: deferred; the operator screens are the staff surface. Revisit when Prompt 7 adds more admin registrations.

[MINOR] backend/payments/operator_sync.py — a row lock is held across the provider lookup; a slow provider holds the lock for up to about 80 s.
Decision: accepted for now; gthread does not kill a long request, and the lock is what guarantees one Stax customer.

[MINOR] TODO-VERIFY-031 — if Stax's email filter is case-sensitive, a customer typed in another case in the dashboard is not found and a second one is created.
Decision: open until Checkpoint 6; recorded in the register.
```

Counts: BLOCKER 0, MAJOR 2 resolved, MINOR 3 (2 deferred, 1 open pending verification). Rework cycles: 1.
Deviations accepted: `documents.external_id` left out (a DocuSign envelope ID belongs in `external_refs`);
address fields not sent to Stax yet; `operators.needs_review` added; `sync_failure` is a closed list;
string route parameters parsed to UUID in the view; editing a linked operator does not update Stax.

**Fable's adversarial tests (32):** replaying a used creation key with other values changes
nothing; a creation key from another user cannot overwrite; nine hostile search strings render
200 without echoing a setting; paging nonsense renders; an archived operator refuses edit, retry
and link through every route; unknown IDs are 404; email matching is exact after lower-case and
strip (Cyrillic look-alikes and `ß` do not match); bank fields in a provider payload never survive
the summary; retry and link at the same moment leave one customer reference; the boot check never
prints a hostile body and refuses a non-boolean flag; the outstanding balance sums exact cents.

**Gate 3:** service, views, forms, registry and Bicep diff read in full. Views never touch the
provider. Templates render no setting and mask IDs. **Gate 4:** CSRF on every form; roles on
every route (registry test green); the link form takes two identifiers only; failure text is a
class name; no delete route; secret scan clean; no secret store read. The staging parameter file
holds the sandbox merchant ID, an identifier, not a secret.

**Residual risk**
- Nothing has run against the Stax sandbox. Checkpoint 6 on staging is the first real create.
- `merchant.allow_ach = False` on the sandbox account (observed 2026-09-30) does not affect
  customers, but it will block Prompt 7's debits in the sandbox until Stax changes it.
- Interim single-caller arrangement on the web app until Step 10 (plan D1); a Stax outage
  blocks new revisions from starting (plan D2), by the owner's rule.
- Deferred MINOR findings across QA-003a, 004, 005, 006: nine in total, all listed in their reports.

**Verdict: PASS.** Acceptance criterion 4 is verified on staging after the push and the
infrastructure deployment.
