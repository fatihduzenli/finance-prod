#!/bin/zsh
# Rerun ONLY the PostgreSQL unit selection, then e2e E2E_RUNS times (default 4; 0 for the units alone),
# on worktree $WT (log tag $TAG) against the gate container left by pg-split.sh (created if missing).
# For a stage that stalled: root CLAUDE.md §4.1 says rerun only that stage, never the whole gate.
set +e
TOOLS="$(cd "$(dirname "$0")" && pwd)"; . "$TOOLS/_env.sh"
cd "${WT:?set WT to the worktree to gate}" || exit 1; : "${TAG:?set TAG, the log tag}"
pg_ensure "$GATE_PG_NAME" "$GATE_PG_PORT" "$GATE_PG_DB" || exit 1
export DATABASE_URL="$(pg_url 127.0.0.1 "$GATE_PG_PORT" "$GATE_PG_DB")"
echo "== unit selection (rerun)"; date
${=PYTEST} -m "not e2e" ${=PG_UNIT_EXCLUDES} -rf 2>&1 | tee "$QA_SCRATCH/$TAG-pg-unit-rerun-raw.txt" \
  | grep -E "passed|failed|FAILED|randomly-seed" | tail -15; echo "exit=${pipestatus[1]}"; date
n="${E2E_RUNS:-4}"; echo "== e2e x$n (rerun)"
for i in $(seq 1 "$n"); do
  ${=PYTEST} backend/tests/e2e -rf 2>&1 | tee "$QA_SCRATCH/$TAG-pg-e2e-rerun-$i.txt" | grep -E "passed|failed|FAILED|assert" | tail -5
done
echo "== RERUN DONE"; date
