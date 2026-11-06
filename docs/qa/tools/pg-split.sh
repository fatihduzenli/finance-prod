#!/bin/zsh
# The PostgreSQL half of the full gate on worktree $WT (log tag $TAG): a fresh throwaway PostgreSQL 16
# (trust auth, as CI), makemigrations against it, the migration selection, the unit selection, e2e ×4.
# Called by full-gate.sh; runs alone for a PostgreSQL-only check.
set +e
TOOLS="$(cd "$(dirname "$0")" && pwd)"; . "$TOOLS/_env.sh"
cd "${WT:?set WT to the worktree to gate}" || exit 1; : "${TAG:?set TAG, the log tag}"
pg_up "$GATE_PG_NAME" "$GATE_PG_PORT" "$GATE_PG_DB" || exit 1
export DATABASE_URL="$(pg_url 127.0.0.1 "$GATE_PG_PORT" "$GATE_PG_DB")"
echo "== makemigrations PG"
(cd backend && uv run python manage.py migrate -q >/dev/null 2>&1; uv run python manage.py makemigrations --check --dry-run 2>&1 | tail -1)
echo "== migration selection"; date
${=PYTEST} -m "not e2e" ${=PG_MIGRATION_TESTS} 2>&1 | tail -3; echo "exit=${pipestatus[1]}"; date
echo "== unit selection"; date
${=PYTEST} -m "not e2e" ${=PG_UNIT_EXCLUDES} -rf 2>&1 | tee "$QA_SCRATCH/$TAG-pg-unit-raw.txt" \
  | grep -E "passed|failed|FAILED|randomly-seed" | tail -15; echo "exit=${pipestatus[1]}"; date
echo "== e2e x4"
for i in 1 2 3 4; do
  ${=PYTEST} backend/tests/e2e -rf 2>&1 | tee "$QA_SCRATCH/$TAG-pg-e2e-raw-$i.txt" | grep -E "passed|failed|FAILED|assert" | tail -5
done
echo "== all done"; date
