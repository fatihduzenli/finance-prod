## QA Report — PLAN-012c: the visual design pass

**Date:** 2026-10-16 · **QA owner:** Fable · **Implementer:** Opus · **Plan:**
`docs/qa/PLAN-012c-design-pass.md` (the copy list approved by the owner on 2026-10-16 with three
changes)

### Batch A — tokens, company settings, the shell, the brand files, PWA, titles

**Scope.** `backend/finance` (new `static/finance/tokens.css`; `app.css` on tokens only with the
top bar, user menu, shell grid, left nav and the folded phone menu; `templates/finance/base.html`
the shell; `pwa.py` (brand icons, navy theme, the name from company settings);
`context_processors.py` (`company`, `nav_group`); `settings.py` (the processor; the TOTP issuer
from the row); the brand files under `static/finance/brand/` (the SVGs byte-for-byte, the PNGs
resized to 512 and 192), the old `icons/` removed), `backend/ledger` (`models.py`
`CompanySettings` singleton with its CHECKs and `current()`; `company_defaults.py`; migration 0045
with the seed, the guard trigger on five text columns and the admin group's permissions;
`admin.py` change-only with the audit; templates: literal colours replaced by tokens on nine
pages, `_job_status.html` names the provider from the row, titles from the short name),
`accounts/registry.py`, the brand originals in `static/brand/` (committed in this step), tests.
No new dependency or environment variable; no provider call.

**Commands run (final Batch A code).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 508 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 412 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 5,450 passed, 653 skipped; 14:15 wall |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 28 passed; 12:43 wall |
| the "Unit tests" selection | PostgreSQL 16 | 0 | 6,072 passed, 3 skipped; 16:42 wall (the first run of this selection had one failure, `test_calendar` creating no entry on a POST; it passed alone three times, the whole selection re-ran green, and the file passed ten times in a row; the test now asserts the POST's status and prints the page on a refusal so a recurrence names its cause) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `8542436` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_design_shell.py` (101: the shell on 16 pages × 3 roles, the
Admin group by role, link ids kept, the user menu, the phone menu without script, the folded
grid, tokens and pairs, no literal colour, titles, the TOTP issuer, the brand files and the
manifest, the singleton, the admin edit and audit, the logo override and its fallback, names in
the bar and the job banner), the seeded round trip of 0045 on both engines, nine test files
updated for the labels, groups and colours. Fable (`test_plan012c_design_a_adversarial.py`, 8
cases, both engines): a company or provider name with markup is escaped on the dashboard, the
Stores list, the search page and the sign-in page; the manifest stays behind the sign-in and
carries the name as data with the brand icons present among the static files; five hostile
logo overrides (an external URL, a protocol-relative one, a path traversal, `javascript:`, a
data URI) are refused or fall back to the brand logo with nothing of them on the page; the phone
menu is a checkbox and label (no script), the SANDBOX banner stays, the new nav labels are there
and the old ones gone, the sign-in title carries the short name.

**Findings (A).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 1 | MAJOR | `finance/context_processors.company` | Three queries on every page (a `get_or_create` with its savepoint and the logo lookup on each render); the grid broke its budget in the volume test (21 of 20) and every page rose by three | Resolved: the row is read once per request with a plain query (no create, no savepoint) and kept on the request; the logo lookup only when an override is set; the manifest shares the row; every page is back to its earlier count plus one (grid 19, revenue 30, arrears 19, statement 20, asserted by a new PostgreSQL test) |

Two points for the owner at the checkpoint: (1) the dark logo
already carries the "Monvera" wordmark, so the bar reads the name twice ("Monvera Monvera |
Rent ledger"); Batch B shows the company name as text only when the logo is overridden (the
brand logo speaks for itself) and keeps it as the logo's accessible name; (2) the grid page keeps
the folded Menu at every width so twelve months still fit at 1,280 px (PLAN-011b D2).

Design choices accepted: the column is `processor_name` (the schema rule refuses "provider"
columns; the property `provider_name` reads it); the Admin group follows what each role may
open (Daily charges, Disputes and Nightly check for billing and admin; Activity log and System
settings for admin; Messages from Stax readable by all; Disputes hidden from viewers instead of
leading to a 403); the group heading `aria-current="true"`, the item `"page"`; the logo
override column guarded like `documents.blob_path`; `current()` falls back to the seed values
on an unmigrated database so the 401 page renders; the banner's wording waits for Batch D.
Neutrals: ink `#1A2433`, ink-2 `#4A5568`, paper `#FFFFFF`, paper-2 `#F3F5F8`, link `#0B6E66`,
border `#7B8794`; every text pairing at 4.5 or better (teal and green are fills, never text on
white); the measured ratios are in the report of the batch and in `tokens.css`.

**Verdict (A):** PASS (one MAJOR, the per-page queries, resolved in one rework cycle; one unreproduced single failure recorded as residual risk with a diagnostic added).
