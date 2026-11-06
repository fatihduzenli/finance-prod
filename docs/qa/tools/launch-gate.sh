#!/bin/bash
# Start the full gate on worktree $1 with log tag $2 once no other full suite runs on this machine
# (an agent's or another gate's), with the Mac kept awake (caffeinate: root CLAUDE.md §4.1 — long gates
# crawl when it idles). $3 is the visual -k expression, $4 an optional e2e file for QA shots into $5.
# Run it as a tracked background task with a watcher on $QA_SCRATCH/$2-gate.txt (root CLAUDE.md §4.1).
TOOLS="$(cd "$(dirname "$0")" && pwd)"; . "$TOOLS/_env.sh"
export WT="${1:?worktree}" TAG="${2:?log tag}"
while pgrep -f "pytest -m not e2e" >/dev/null || pgrep -f "tools/full-gate.sh" >/dev/null || pgrep -f "tools/small-gate.sh" >/dev/null; do sleep 60; done
echo "start $(date)"
caffeinate -i zsh "$TOOLS/full-gate.sh" "${3:-test_the_page_looks}" "$4" "$5" > "$QA_SCRATCH/$TAG-gate.txt" 2>&1 &
G=$!; caffeinate -s -w $G >/dev/null 2>&1 & wait $G; echo "gate exit $? $(date)"
