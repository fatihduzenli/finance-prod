# QA — Step 12f-c, the owner's review of 12f-b on staging (four batches of fixes)

The plan is `docs/qa/PLAN-012f-c-staging-review-fixes.md`. One section per batch, appended as each
batch closes: scope, findings, departures, the gate (ruff, format, mypy strict, lock, makemigrations
on sqlite and PostgreSQL 16, the full sqlite suite, the CI split on PostgreSQL with the e2e suite
four times, the visual suite on PostgreSQL and sqlite with only the named baselines regenerated,
the secret scans, the provider fence), the shots (laptop 1280 and phone 390 of every changed page
from the Playwright image, under `docs/qa/12f-c/shots/<batch>/`) and the verdict.
