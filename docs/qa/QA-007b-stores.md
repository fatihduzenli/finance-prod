## QA Report — PLAN-007b: store page, read-only admin, second debit day

**Date:** 2026-10-01 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-007b-stores.md`

**Scope:** `backend/ledger/services/stores.py`, `views/stores.py`, `forms.py` (store forms),
`urls.py`, `admin.py` (read-only base class; `Store`, `Operator`, `Lease` registered), templates
`ledger/stores/`, migration 0008 (admin group `view_store`, `view_operator`, `view_lease`,
reversible), `backend/accounts/registry.py`, base template nav, `README.md`. Tests (Opus):
`test_store_views.py`. Tests (Fable): `test_stores_adversarial.py`. Unchanged, verified:
`pyproject.toml`, `uv.lock`, `infra/`, `.github/`, `Dockerfile`, auth middleware.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .` | n/a | 0 | clean, 198 files |
| `uv run mypy` (strict) | n/a | 0 | 154 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 1,975 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 1,613 passed, 362 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 1,924 before, 1,983 after. No test removed or skipped. Every row of the plan has a
passing test. Fable's additions: a card number in any store text field saves nothing; the second
debit day input carries no `value` on the lease forms; date edge cases (leap day, impossible
date, closed before open, missing open date); a replayed key with other values changes nothing;
viewer and billing cannot create or edit.

**Owner's report that "Second debit day defaults to 1":** not reproduced. The field has no
initial value and the rendered input has no `value` attribute on create or edit (asserted by
both Opus's and Fable's tests). Possible cause: a browser number input with `min=1` shows the
spinner's lower bound as a hint in some browsers. If it still shows 1 after this deploy, a
screenshot will settle it.

**Findings:** BLOCKER 0, MAJOR 0, MINOR 0. Deviations accepted: `ReadOnlyAdmin` base class;
store creation serialised on a `sync_state` lock row; store number limited to 1 to 2,147,483,647.

**Verdict: PASS**
