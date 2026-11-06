# QA tools — the gate and merge scripts

The scripts Fable runs for the two gate classes of root `CLAUDE.md` §1.3 and for the merge chain of
§6. They used to live in the session's scratchpad, which macOS purges after about three days; they
live here so they survive and so each gate is run the same way every time. They change nothing in the
product: `backend/` is read, tested and screenshotted, never written to (the visual scripts write
baselines under `backend/tests/visual/`, which the QA entry then commits).

## The two classes

| Class | Script | What runs locally | What the PR's CI adds |
|---|---|---|---|
| Small change | `small-gate.sh` | lint, format, strict types, the lock, makemigrations, the secret scan; the targeted tests on sqlite; e2e once on PostgreSQL; the visual suite for the touched pages, compared on both databases | the full sqlite suite, the full PostgreSQL unit selection, e2e ×4 |
| Full gate | `launch-gate.sh` → `full-gate.sh` | everything above, then the full sqlite suite, the PostgreSQL split (`pg-split.sh`: makemigrations, the migration selection, the unit selection, e2e ×4) and the visual suite (`visual.sh`) | the same suites again |

`pg-rerun.sh` reruns only the PostgreSQL unit selection and e2e (a stalled stage is rerun alone,
§4.1). `verify-staging.sh` is read-only: is a commit live on staging? `wait-merge.sh` and
`merge-one.sh` are the merge chain and are Section 6 actions: run only on the owner's written
authorisation for that pull request.

## Running

Every script takes the worktree to gate in `WT` and a log tag in `TAG`; logs and raw pytest output go
to `QA_SCRATCH` (default `${TMPDIR:-/tmp}/qa-gate`). The placeholder environment is set by `_env.sh`
and is the one `.github/workflows/ci.yml` uses: no credential, no live endpoint. Both throwaway
PostgreSQL containers (`qa-gate-pg` on 55432, `qa-visual-pg` on 55435) use trust auth, as CI's
service does, so no password exists anywhere.

```sh
T=docs/qa/tools                      # from the repository root

# Small change: the touched apps' tests, e2e once on PostgreSQL, the dashboard's baselines redone
WT=/path/to/worktree TAG=dash TESTS="backend/tests/test_dashboard.py backend/tests/visual" \
  zsh $T/small-gate.sh "dashboard"

# Full gate, started once the machine is idle, the Mac kept awake; QA shots from one e2e file
bash $T/launch-gate.sh /path/to/worktree batch-c "store or lease" backend/tests/e2e/test_store.py /scratch/shots-c

# Only the PostgreSQL units and e2e, twice
WT=/path/to/worktree TAG=batch-c E2E_RUNS=2 zsh $T/pg-rerun.sh

# Is staging on a commit?  (read-only)
bash $T/verify-staging.sh <full sha>

# The merge chain for PR 53 at its head — Section 6: only with the owner's yes for PR 53
bash $T/wait-merge.sh 53 <full head sha>
```

Long runs are tracked background tasks with a watcher re-armed before it expires (§4.1); the lines
to watch for are `== STOP`, `GATE DONE`, `SMALL GATE DONE`, `conclusion:`, `merged` and `STEP OK`.
A gate's verdict is read from its log and from the raw files, never from the exit code alone: the
PostgreSQL split and the visual suite print their counts and carry on, so a `failed` inside them is
found by reading `$QA_SCRATCH/<TAG>-pg.txt` and `<TAG>-visual.txt`.

## Notes

- `small-gate.sh` keeps the gate container between runs; `PGDB=gate2` gives a second gate its own
  database on the same container. `pg-split.sh` and `visual.sh` always start fresh containers.
- The visual suite runs in the same Playwright image as CI (`_env.sh`, `PLAYWRIGHT_IMAGE`; keep it
  equal to the tag in `ci.yml`), against PostgreSQL first and then sqlite: the two must be
  pixel-identical, which is what CI checks.
- `caffeinate` in `launch-gate.sh` is there because a long local gate crawls when the Mac idles.
- Check that Docker answers (`docker version`) before a gate; a daemon that has been up for weeks
  can hang and needs a restart.
- The scripts are plain zsh (`${pipestatus[1]}`, `${=VAR}`) and bash 3.2 (macOS); nothing beyond
  `uv`, `docker`, `gh`, `az` and `curl` is needed.
