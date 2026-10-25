# QA — Step 12f-b, the owner's staging review (findings 1–7)

Plan: `docs/qa/PLAN-012f-b-staging-review.md`. One section per batch, in order of delivery: Batch 0 (CI
and guards), A (dashboard), B (stores and operators), C (Add payment with ACH), D (Create invoice:
Save, the e-mail prompt, Schedule settlement), E (the remaining pages to the spec). Each section
records the scope, the findings with their severity and outcome, the commands run with exit codes
on both engines, the tests added, every baseline regenerated (in the Playwright image against
PostgreSQL) and the laptop and phone screenshots of every changed page under `docs/qa/12f-b/shots/`.

Each batch is its own pull request from a `step-12f-b/<batch>` branch (owner's instruction,
2026-10-25); the push, the pull request and the merge are each a publish action under the root
CLAUDE.md §6 and wait for the owner's written authorisation.

## Batch 0 — CI and guards (branch `step-12f-b/batch-0`)

**Scope (Fable alone; test files and verification config under CLAUDE.md §1.1).**
`backend/tests/test_migrations_roundtrip.py`: the module list becomes `GUARD_MODULES` with ledger
0046, 0048, 0050 and payments 0018 added, read through `guarded_columns()` (a migration that
declares no `GUARDED_COLUMNS` counts as none). `backend/tests/test_card_number_guard.py`: its list
becomes `GUARD_MODULES` with 0041, 0046, 0048, 0050 and payments 0018 added, and a new test
`test_every_migration_that_may_guard_a_column_is_in_both_guard_lists` walks both apps' migration
folders: every migration that declares `GUARDED_COLUMNS` must be in the card-number list, and every
migration from ledger 0041 and payments 0018 on must be in both lists, so a new migration left out
fails the suite. `.github/workflows/ci.yml`: the migration job's `timeout-minutes` 30 → 45 with a
comment marking it temporary (Step 12f-b adds several migrations; Step 13 owns the split or squash;
the job's time is reported at the checkpoint). The STATUS commit `871a846` (slice F deployed) rides
on this branch, as the owner asked. Docs: `docs/qa/PLAN-012f-b-staging-review.md` (the Phase A plan)
and this file's skeleton.

**Findings.** None in the code under review (no production code changed). Noted for Step 13: the
round-trip list omits ten migrations that the card-number list carries (ledger 0004, 0005, 0009,
0010, 0012, 0015, 0016, 0024; payments 0003, 0007), by design — their triggers were later replaced
or renamed — so the completeness test requires both lists only from the thresholds on.

**Gate.** ruff, `ruff format --check` (487 files), mypy strict (487 files), `uv lock --check`: clean. sqlite: full suite 7,022 passed, 674 skipped, 98 deselected in 1,412 s (the new completeness test included). PostgreSQL 16 (CI's split on a throwaway container): migration selection 36 passed in 1,392 s (the round trips, 23:12 locally), unit selection 7,657 passed, 3 skipped, 101 deselected, e2e 31 passed on each of four runs.
Visual: no page changed; not run (the CI visual job runs on the PR). Secret scans clean; no secret-
store read; no new file type; the workflow edit is one number and a comment, parsed by a YAML load.

**Verdict (0):** **PASS.** No production code; the two lists complete and guarded by a test; the job limit raised to 45 minutes and marked temporary; the Phase A plan for the step committed with it.
