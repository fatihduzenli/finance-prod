#!/bin/zsh
# The SMALL-change gate class (root CLAUDE.md §1.3 "Gate classes", owner 2026-11-04) on worktree $WT,
# log tag $TAG: lint, format, strict types, the lock, makemigrations, the secret scan; the targeted
# tests ($TESTS, space-separated pytest paths or ids) on sqlite; e2e ONCE on PostgreSQL (the gate
# container, database $PGDB, default "gate"; a second name lets two gates share the container); the
# visual suite with the touched pages' baselines regenerated ($1, a -k expression) and compared on both
# databases. The full sqlite suite, the full PostgreSQL unit selection and e2e ×4 run on the PR's CI.
set +e
TOOLS="$(cd "$(dirname "$0")" && pwd)"; . "$TOOLS/_env.sh"
cd "${WT:?set WT to the worktree to gate}" || exit 1; : "${TAG:?set TAG, the log tag}"; : "${TESTS:?set TESTS, the targeted tests}"
KEXPR="${1:-test_the_page_looks}"; PGDB="${PGDB:-$GATE_PG_DB}"
export DATABASE_URL=sqlite:///:memory:
echo "== SMALL GATE branch $(git branch --show-current) $(git rev-parse --short HEAD)"; date
fail=0
run() { echo "== $1"; eval "$2" 2>&1 | tail -2; rc=${pipestatus[1]}; echo "exit=$rc"; [ "$rc" = "0" ] || fail=1; }
run "ruff check" "uv run ruff check ."
run "ruff format" "uv run ruff format --check ."
run "mypy" "uv run --directory backend mypy ."; rm -rf backend/.mypy_cache
run "uv lock" "uv lock --check"
run "makemigrations sqlite" "(cd backend && uv run python manage.py makemigrations --check --dry-run)"
run "check-no-secrets" "bash scripts/check-no-secrets.sh"
[ "$fail" = "0" ] || { echo "== STOP: fast checks failed"; exit 1; }
echo "== targeted tests (sqlite): $TESTS"; date
${=PYTEST} -m "not e2e" ${=TESTS} -rf 2>&1 | tee "$QA_SCRATCH/$TAG-targeted-raw.txt" \
  | grep -E "passed|failed|FAILED|randomly-seed" | tail -8; rc=${pipestatus[1]}; echo "exit=$rc"
[ "$rc" = "0" ] || { echo "== STOP: targeted tests failed"; exit 1; }
echo "== e2e once on PostgreSQL ($PGDB)"; date
pg_ensure "$GATE_PG_NAME" "$GATE_PG_PORT" "$GATE_PG_DB" || exit 1; pg_ensure_db "$GATE_PG_NAME" "$PGDB"
DATABASE_URL="$(pg_url 127.0.0.1 "$GATE_PG_PORT" "$PGDB")" ${=PYTEST} backend/tests/e2e -rf 2>&1 \
  | tee "$QA_SCRATCH/$TAG-e2e-raw.txt" | grep -E "passed|failed|FAILED|assert" | tail -5; rc=${pipestatus[1]}; echo "exit=$rc"
[ "$rc" = "0" ] || { echo "== STOP: e2e failed"; exit 1; }
echo "== visual: regenerate $KEXPR, compare both databases"; date
bash "$TOOLS/visual.sh" "$KEXPR" "" "" 2>&1 | tee "$QA_SCRATCH/$TAG-visual.txt" | grep -E "^== |passed|failed|FAILED" | tail -12
grep -qE "failed|FAILED" "$QA_SCRATCH/$TAG-visual.txt" && { echo "== STOP: visual failed"; exit 1; }
echo "== SMALL GATE DONE"; date
