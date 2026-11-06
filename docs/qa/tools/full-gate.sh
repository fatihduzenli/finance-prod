#!/bin/zsh
# The FULL gate class (root CLAUDE.md §1.3) on worktree $WT with log tag $TAG:
#   lint, format, strict types, the lock, makemigrations, the secret scan;
#   the full sqlite suite;
#   the PostgreSQL split (pg-split.sh: makemigrations, the migration selection, the unit selection, e2e ×4);
#   the visual suite (visual.sh: the named pages' baselines regenerated — $1, a -k expression —
#   then every baseline compared on PostgreSQL and on sqlite; optional QA shots from e2e file $2 into $3).
# Usually started through launch-gate.sh. Logs: $QA_SCRATCH/$TAG-*.txt.
set +e
TOOLS="$(cd "$(dirname "$0")" && pwd)"; . "$TOOLS/_env.sh"
cd "${WT:?set WT to the worktree to gate}" || exit 1; : "${TAG:?set TAG, the log tag}"
KEXPR="${1:-test_the_page_looks}"; E2E="$2"; SHOTS="$3"
export DATABASE_URL=sqlite:///:memory:
echo "== FULL GATE branch $(git branch --show-current) $(git rev-parse --short HEAD)"; date
fail=0
run() { echo "== $1"; eval "$2" 2>&1 | tail -2; rc=${pipestatus[1]}; echo "exit=$rc"; [ "$rc" = "0" ] || fail=1; }
run "ruff check" "uv run ruff check ."
run "ruff format" "uv run ruff format --check ."
run "mypy" "uv run --directory backend mypy ."; rm -rf backend/.mypy_cache
run "uv lock" "uv lock --check"
run "makemigrations sqlite" "(cd backend && uv run python manage.py makemigrations --check --dry-run)"
run "check-no-secrets" "bash scripts/check-no-secrets.sh"
[ "$fail" = "0" ] || { echo "== STOP: fast checks failed"; exit 1; }
echo "== full sqlite"; date
${=PYTEST} -m "not e2e" backend/tests -rf 2>&1 | tee "$QA_SCRATCH/$TAG-sqlite-raw.txt" \
  | grep -E "passed|failed|FAILED|randomly-seed" | tail -12; rc=${pipestatus[1]}; echo "exit=$rc"; date
[ "$rc" = "0" ] || { echo "== STOP: sqlite failed"; exit 1; }
echo "== PG split"; date
zsh "$TOOLS/pg-split.sh" 2>&1 | tee "$QA_SCRATCH/$TAG-pg.txt" | tail -30
echo "== visual: regenerate $KEXPR, compare both databases, e2e shots"; date
bash "$TOOLS/visual.sh" "$KEXPR" "$E2E" "$SHOTS" 2>&1 | tee "$QA_SCRATCH/$TAG-visual.txt" | tail -20
echo "== GATE DONE"; date
