## QA Report — PLAN-003b: refused-host requests log one warning line

**Date:** 2026-09-29 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-003b-refused-host-logging.md`

**Scope:** root project. Production (Opus): `backend/finance/log_redaction.py`,
`backend/finance/settings.py` (`LOGGING`), `README.md` (one sentence).
Tests: `backend/tests/test_logging.py` (Opus, appended), `backend/tests/test_staging_defects_adversarial.py` (Fable, appended).
Unchanged, verified: `pyproject.toml`, `uv.lock`, `infra/`, `.github/`, `backend/accounts/`, `Dockerfile`.

**Commands run by Fable after rework cycle 1** (repository root, placeholder environment):

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .` | n/a | 0 | clean |
| `uv run ruff format --check .` | n/a | 0 | 80 files |
| `uv run mypy` (strict) | n/a | 0 | 55 source files |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 270 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 266 passed, 4 skipped (PostgreSQL-only) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 2 passed |
| logging and adversarial files, two consecutive runs | PostgreSQL 16 | 0 | 58 passed each time |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 238 before, 272 after. No existing test edited, removed or skipped. Every row of
plan §5 has a passing test. Coverage tooling is not configured; every branch of the new
filter and helper is exercised, including the cut branch and the mismatched-arguments branch.

**Findings**

```
[MAJOR] backend/finance/log_redaction.py:30 — the host pattern backtracked exponentially.
Failure scenario: message "Invalid HTTP_HOST header: '" + N backslashes with no closing quote; 0.045 s at N=26, doubling every two characters, no completion at N=60, inside a logging filter on the request path.
Required change: linear matching for every input and a bounded input length.
Status: RESOLVED in rework cycle 1. Alternatives made disjoint; input cut to 512 characters.
Verified by Fable: 100,000-character hostile inputs of five shapes complete in under 0.5 s.
```

Found by a Fable adversarial test, not by the implementation tests. Django always writes a
terminated `repr`, so the stall was not reachable through the Host header today; it was
fixed because the pattern runs on attacker-influenced text.

Counts: BLOCKER 0, MAJOR 1 resolved, MINOR 0. Rework cycles: 1. Full gate re-run after it.

**Gate 3:** diff read in full; one filter, attached to one logger by name; no drive-by change.
**Gate 4:** Host value reduced to `[A-Za-z0-9.:\-\[\]_]`, at most 100 characters; nothing else
of the original message is written; CR, LF, ANSI escapes, format strings and non-ASCII tested.
A host equal to a configured secret is still redacted. Secret scan of the diff: clean. No
secret store was read by either model. The refusal (400) is unchanged.

**Residual risk**
- Not yet verified on staging; acceptance criterion 5 is checked after an approved push.
- The 4xx warning volume (QA-003a finding 2, second half) is unchanged by design.
- QA-003a finding 1 (command failures bypass the redacting handler) stays deferred.

**Verdict: PASS**
