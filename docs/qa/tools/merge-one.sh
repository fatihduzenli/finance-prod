#!/bin/bash
# SECTION 6 (root CLAUDE.md): merging is a publish action. Run this only on the owner's explicit, written
# authorisation for THIS pull request; one authorisation covers one PR.
# One merge step for PR $1: retarget it to main, require clean mergeability and no failing or pending
# check, merge (a merge commit), wait for the main run and print its jobs (and any failure annotations),
# then verify staging (verify-staging.sh) and /healthz. Exit 1 at the first problem, with a STOP line.
set -u
TOOLS="$(cd "$(dirname "$0")" && pwd)"; cd "$TOOLS"
PR="${1:?pull request number}"
SLUG="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
gh pr edit "$PR" --base main >/dev/null || { echo "STOP: retarget failed"; exit 1; }
for i in $(seq 1 20); do st=$(gh pr view "$PR" --json mergeable,mergeStateStatus --jq '.mergeable+" "+.mergeStateStatus'); [ "${st%% *}" != "UNKNOWN" ] && break; sleep 10; done
echo "PR $PR: $st"
case "$st" in "MERGEABLE CLEAN"|"MERGEABLE UNSTABLE"|"MERGEABLE HAS_HOOKS") ;; *) echo "STOP: not cleanly mergeable"; exit 1;; esac
bad=$(gh pr checks "$PR" 2>/dev/null | grep -cE "fail|pending")
[ "$bad" = "0" ] || { echo "STOP: $bad failing or pending checks"; gh pr checks "$PR" | grep -E "fail|pending"; exit 1; }
gh pr merge "$PR" --merge >/dev/null || { echo "STOP: merge failed"; exit 1; }
SHA=$(gh pr view "$PR" --json mergeCommit --jq .mergeCommit.oid); echo "merged $SHA"
RUN=""; for i in $(seq 1 30); do RUN=$(gh run list --branch main --limit 5 --json databaseId,headSha,event --jq ".[] | select(.headSha==\"$SHA\" and .event==\"push\") | .databaseId" | head -1); [ -n "$RUN" ] && break; sleep 10; done
[ -n "$RUN" ] || { echo "STOP: no main run found"; exit 1; }
echo "run $RUN"
until [ "$(gh run view "$RUN" --json status --jq .status 2>/dev/null)" = "completed" ]; do sleep 30; done
gh run view "$RUN" --json conclusion,jobs --jq '"conclusion: "+.conclusion, (.jobs[] | "\(.name): \(.conclusion) \(.startedAt) \(.completedAt)")'
gh api "repos/$SLUG/actions/runs/$RUN/jobs" --jq '.jobs[] | select(.conclusion=="failure") | .id' | while read -r j; do
  gh api "repos/$SLUG/check-runs/$j/annotations" --jq '.[] | "annotation: "+.message' 2>/dev/null
done
[ "$(gh run view "$RUN" --json conclusion --jq .conclusion)" = "success" ] || { echo "STOP: main run not green"; exit 1; }
verify="$(bash "$TOOLS/verify-staging.sh" "$SHA")"; echo "$verify"
echo "$verify" | grep -qE ": 0$|: NO$" && { echo "STOP: staging not on $SHA"; exit 1; }
echo "$verify" | grep -q "healthz: 200" || { echo "STOP: healthz not 200"; exit 1; }
echo "STEP OK $PR ${SHA:0:7}"
