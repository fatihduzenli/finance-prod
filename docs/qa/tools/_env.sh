# Shared settings for the QA tools. Sourced by every script here, never run on its own.
# Each script sets TOOLS (this directory) from its own path before sourcing this file.
#
# QA_SCRATCH — where the logs, the raw pytest output and the Linux uv cache live. It is scratch:
#              nothing here is kept. Default: ${TMPDIR:-/tmp}/qa-gate.
# The placeholder environment below is the one .github/workflows/ci.yml uses: no credential, no
# live endpoint is reached, the fake key only feeds the secret scan (root CLAUDE.md §5).
# Both throwaway PostgreSQL containers use trust auth, exactly as the CI service does, so no
# password exists anywhere (root CLAUDE.md §5.2).

QA_SCRATCH="${QA_SCRATCH:-${TMPDIR:-/tmp}/qa-gate}"
mkdir -p "$QA_SCRATCH"

export APP_ENV=local STAX_ENV=sandbox STAX_API_BASE_URL=https://apiprod.fattlabs.com \
  APP_BASE_URL=http://localhost:8000 DJANGO_SECRET_KEY=placeholder-not-secret \
  STAX_API_KEY=FAKE-CI-KEY-XYZ123

GATE_PG_NAME=qa-gate-pg;     GATE_PG_PORT=55432;   GATE_PG_DB=gate
VISUAL_PG_NAME=qa-visual-pg; VISUAL_PG_PORT=55435; VISUAL_PG_DB=visual
PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.63.0-noble   # the same tag as ci.yml

# The PostgreSQL split (pg-split.sh, pg-rerun.sh): the tests that need a real migration run,
# and the flags that keep them out of the unit selection.
PG_MIGRATION_TESTS="backend/tests/test_migrations_roundtrip.py backend/tests/test_report_indexes.py \
backend/tests/test_one_off_charges.py::test_the_rename_migration_rewrites_rows_and_reverses \
backend/tests/test_one_off_charges.py::test_the_rename_migration_refuses_to_reverse_over_a_one_off_charge \
backend/tests/test_staging_defects_adversarial.py::test_two_concurrent_runs_both_succeed_and_migrate_once"
PG_UNIT_EXCLUDES="--ignore=backend/tests/test_migrations_roundtrip.py --ignore=backend/tests/test_report_indexes.py \
--deselect backend/tests/test_one_off_charges.py::test_the_rename_migration_rewrites_rows_and_reverses \
--deselect backend/tests/test_one_off_charges.py::test_the_rename_migration_refuses_to_reverse_over_a_one_off_charge \
--deselect backend/tests/test_staging_defects_adversarial.py::test_two_concurrent_runs_both_succeed_and_migrate_once"

PYTEST="uv run pytest -q -o faulthandler_timeout=300 -p no:cacheprovider"

pg_url() { echo "postgresql://postgres@$1:$2/$3"; }   # pg_url HOST PORT DB

pg_wait() {   # pg_wait NAME — up to 30 s for pg_isready
  i=0
  until docker exec "$1" pg_isready -U postgres >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -ge 30 ] && { echo "STOP: $1 did not come up"; return 1; }; sleep 1
  done
}

pg_up() {     # pg_up NAME PORT DB — a fresh throwaway PostgreSQL 16; replaces any container on the port
  docker rm -f "$1" >/dev/null 2>&1
  docker ps -q --filter "publish=$2" | xargs docker rm -f >/dev/null 2>&1
  docker run -d --name "$1" -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB="$3" \
    -p "127.0.0.1:$2:5432" postgres:16 >/dev/null || { echo "STOP: docker run $1 failed"; return 1; }
  pg_wait "$1"
}

pg_ensure() { # pg_ensure NAME PORT DB — keep a running trust-auth container, else (re)create it
  if docker exec "$1" pg_isready -U postgres >/dev/null 2>&1 \
     && docker inspect -f '{{.Config.Env}}' "$1" 2>/dev/null | grep -q POSTGRES_HOST_AUTH_METHOD=trust; then
    return 0
  fi
  pg_up "$1" "$2" "$3"
}

pg_ensure_db() { # pg_ensure_db NAME DB — create DB in the container when it is missing
  docker exec "$1" psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$2'" 2>/dev/null | grep -q 1 \
    || docker exec "$1" createdb -U postgres "$2"
}
