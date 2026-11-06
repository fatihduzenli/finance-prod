#!/bin/bash
# SECTION 6 (root CLAUDE.md): run only on the owner's explicit authorisation for THIS pull request.
# The merge chain for PR $1 at head $2 (the full sha): wait for the PR's CI run on that head, require it
# green and print its jobs, then merge-one.sh. Run it as a tracked background task with a watcher on its
# log (root CLAUDE.md §4.1): the watcher should match "conclusion", "STOP", "merged" and "STEP OK".
TOOLS="$(cd "$(dirname "$0")" && pwd)"; cd "$TOOLS"
PR="${1:?pull request number}"; HEAD="${2:?head sha}"
BRANCH="$(gh pr view "$PR" --json headRefName --jq .headRefName)"
RUN=""; for i in $(seq 1 30); do RUN=$(gh run list --branch "$BRANCH" --limit 5 --json databaseId,headSha,event --jq ".[] | select(.headSha==\"$HEAD\" and .event==\"pull_request\") | .databaseId" | head -1); [ -n "$RUN" ] && break; sleep 10; done
[ -n "$RUN" ] || { echo "STOP: no PR run found for ${HEAD:0:7} on $BRANCH"; exit 1; }
echo "== PR $PR run $RUN on ${HEAD:0:7} ($BRANCH)"
until [ "$(gh run view "$RUN" --json status --jq .status 2>/dev/null)" = "completed" ]; do sleep 60; done
gh run view "$RUN" --json conclusion,jobs,createdAt,updatedAt --jq '"conclusion: "+.conclusion, "run: "+.createdAt+" -> "+.updatedAt, (.jobs[] | "\(.name): \(.conclusion) \(.startedAt) \(.completedAt)")'
[ "$(gh run view "$RUN" --json conclusion --jq .conclusion)" = "success" ] || { echo "STOP: PR $PR run not green"; exit 1; }
echo "== merge $PR"
bash "$TOOLS/merge-one.sh" "$PR"
