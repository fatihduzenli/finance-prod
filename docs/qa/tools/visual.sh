#!/bin/bash
# The visual suite for a gate, on worktree $WT: regenerate ONLY the named pages' baselines ($1, a -k
# expression) in the Playwright image against a throwaway PostgreSQL 16, then compare every baseline on
# PostgreSQL and on sqlite (they must be pixel-identical: CI compares on both). Optionally run the e2e
# file $2 with QA_SHOTS_DIR=$3 (a path inside the container under /scratch) for the QA screenshots.
# The Linux uv cache lives in $QA_SCRATCH/uvcache-linux so the image's sync is fast after the first run.
set +e
TOOLS="$(cd "$(dirname "$0")" && pwd)"; . "$TOOLS/_env.sh"
cd "${WT:?set WT to the worktree to gate}" || exit 1
KEXPR="$1"; E2E="$2"; SHOTS="$3"
pg_up "$VISUAL_PG_NAME" "$VISUAL_PG_PORT" "$VISUAL_PG_DB" || exit 1
mkdir -p "$QA_SCRATCH/uvcache-linux"
run() {   # run DATABASE_URL "pytest args" QA_SHOTS_DIR
  docker run --rm --add-host=host.docker.internal:host-gateway \
    -v "$PWD":/work -v "$QA_SCRATCH/uvcache-linux":/uvcache -v "$QA_SCRATCH":/scratch -w /work \
    -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/uvcache \
    -e APP_ENV -e STAX_ENV -e STAX_API_BASE_URL -e APP_BASE_URL -e DJANGO_SECRET_KEY -e STAX_API_KEY \
    -e DATABASE_URL="$1" -e QA_SHOTS_DIR="$3" \
    "$PLAYWRIGHT_IMAGE" \
    bash -c "pip install -q uv 2>/dev/null; uv sync --all-groups -q 2>/dev/null; uv run pytest $2 -q -o faulthandler_timeout=300 -p no:cacheprovider 2>&1 | tail -4"
}
PG="$(pg_url host.docker.internal "$VISUAL_PG_PORT" "$VISUAL_PG_DB")"
echo "== regenerate on PostgreSQL: $KEXPR"; date
run "$PG" "backend/tests/visual -m visual --update-baseline -k \"$KEXPR\"" ""
echo "== compare on PostgreSQL"; date
run "$PG" "backend/tests/visual -m visual" ""
echo "== compare on sqlite"; date
run "sqlite:////tmp/visual.sqlite3" "backend/tests/visual -m visual" ""
if [ -n "$E2E" ]; then echo "== e2e shots on PostgreSQL: $E2E -> $SHOTS"; date; run "$PG" "$E2E" "$SHOTS"; fi
docker rm -f "$VISUAL_PG_NAME" >/dev/null 2>&1
echo "== done"; date
