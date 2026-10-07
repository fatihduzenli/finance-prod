## QA Report — PLAN-012c: the store page's "Add entry" button

**Date:** 2026-10-07 · **QA owner:** Fable · **Implementer:** Opus
**Origin:** owner's click 2 of Prompt 12 on staging (store 2477: list, no button).
**Plan:** `docs/qa/PLAN-012c-store-add-entry.md` · ships with PLAN-012a and 012b in one push.

**Scope.** `backend/ledger/services/contacts.py` (`store_contact_lease`),
`backend/ledger/views/contact_log.py` (`store_add_url`), `backend/ledger/views/contacts.py`
(`store_contacts_add`, the locked lease on the store form, `_lease_state` in the title),
`backend/tests/test_contact_log.py`, `backend/tests/test_plan012c_adversarial.py` (Fable). No
migration, route, template structure, environment or provider change.

**Commands run (final code).** Env as in QA-012; PostgreSQL 16 in a throwaway container.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean |
| `uv run mypy` (strict) | n/a | 0 | no issues in 350 source files |
| `uv lock --check` | n/a | 0 | unchanged |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 4,591 passed, 576 skipped (4,576 before) |
| same | PostgreSQL 16 | 0 | 5,164 passed, 3 skipped (5,149 before) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff and new files | n/a | 0 | clean |
| Bicep build | n/a | — | not applicable: `infra/` untouched |

Baseline before PLAN-012c (QA-012b): PostgreSQL 5,149 passed; sqlite 4,576 passed.

**Tests (Gate 2).** Opus: six functions, eight cases, in `test_contact_log.py`: the button and
the form for a store whose only lease is active with a future start, prospective, paused with a
future start, ended, active with a past end, or paused and in effect (button for billing and
admin, the title suffix, the preselected lease, the saved entry on both pages, viewer 403 on
GET and POST); the lease in effect beats an older ended one; of two ended leases the later
start wins; an undated lease comes last; a lease with an archived operator or no operator is
skipped and, once only those remain, no button, the redirect and nothing written; no lease at
all; a forged lease or operator from store B, a blank lease and the operator's own lease at
another store refused on the store route.

Fable's adversarial file (4 cases): store A's route cannot record against store B's lease or
operator by any posted field (four forgeries; every saved row is A's lease and operator); an
archived operator's lease offers no button and the route writes nothing; the older lease with a
live operator wins over a newer archived one, and the form names the right operator; a viewer
sees no button on a non-current lease and gets 403, while billing does see it.

**Findings (Gates 3 and 4).** None blocking.

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 1 | MINOR | `views/contacts.py` `NO_STORE_LEASE` | The message still says "no lease in effect today" while it now shows only when the store has no usable lease at all | **Deferred by written decision (Fable, 2026-10-07):** the sentence is still true for such a store; wording only; next wording pass. |

Design choices accepted as built: the store form locks its lease (the only choice, required),
which is what the owner asked for ("the store/lease preselected and the operator fixed") and
what closes the forged-field case; an in-effect lease of an archived operator is skipped in
favour of the next lease (plan §6 over the literal D1); "lease pending" for prospective or
future-start leases and "lease ended YYYY-MM-DD" otherwise (there is no "pending" status, and a
paused lease that has started is in effect).

Gate 4: the route checks billing, the resolver excludes archived operators, `add_entry` refuses
an archived operator again under its lock, posted fields outside the form are ignored, the
lease choice is the store's lease only; no new text column, no provider call, no secret.

**Confidentiality gate (§5.4).** Secret scan clean; no secret-store read; no new file type; no
new secret name.

**Residual risk.** Store 2477's real lease data on staging is unverified until the owner's
click; no browser test of the button or title.

**Verdict: PASS** (pending the owner's click on store 2477).
