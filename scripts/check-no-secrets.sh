#!/usr/bin/env bash
# Fail the build if a Stax credential can reach a browser.
#
# Contract: docs/qa/PLAN-002-scaffolding.md §3. Scans a fresh collectstatic output
# plus every templates/ and static/ directory under backend/ for
#   (a) the literal STAX_API_KEY value, when the variable is set and non-empty;
#   (b) credential-shaped patterns.
# It prints file paths only, never the match and never the value (root CLAUDE.md §5.1).
# Exit 0 means clean, exit 1 means at least one hit.
#
# Needs a valid environment (see .env.example): collectstatic imports Django settings,
# and settings validate the environment at import time.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

static_dir="$(mktemp -d)"
trap 'rm -rf "$static_dir"' EXIT

# Inside an activated environment (uv run, CI) use it directly; otherwise let uv
# resolve one. Avoids nesting one uv run inside another.
if [ -n "${VIRTUAL_ENV:-}" ]; then
  python_cmd=("$VIRTUAL_ENV/bin/python")
else
  python_cmd=(uv run python)
fi
STATIC_ROOT="$static_dir" "${python_cmd[@]}" backend/manage.py collectstatic --noinput >/dev/null

scan_paths=("$static_dir")
while IFS= read -r -d '' directory; do
  scan_paths+=("$directory")
done < <(find backend -type d \( -name templates -o -name static \) -print0)

status=0

# (a) the configured key value. Passed on stdin so it never appears in an argument
# list or in this script's output.
if [ -n "${STAX_API_KEY:-}" ]; then
  key_hits="$(printf '%s\n' "$STAX_API_KEY" | grep -rlF -f - "${scan_paths[@]}" || true)"
  if [ -n "$key_hits" ]; then
    echo "FAIL: the STAX_API_KEY value appears in files that are served to browsers:"
    printf '%s\n' "$key_hits" | sed 's|^|  |'
    status=1
  fi
fi

# (b) credential-shaped patterns.
pattern='stax[_-]?api[_-]?key|apiprod\.fattlabs\.com|Bearer [A-Za-z0-9._-]{20,}'
pattern_hits="$(grep -rilE -e "$pattern" "${scan_paths[@]}" || true)"
if [ -n "$pattern_hits" ]; then
  echo "FAIL: a Stax credential pattern appears in files that are served to browsers:"
  printf '%s\n' "$pattern_hits" | sed 's|^|  |'
  status=1
fi

if [ "$status" -eq 0 ]; then
  echo "OK: no Stax credential found in browser-served files."
fi
exit "$status"
