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

### Batch B — components, dashboard cards, grid polish, print; the bar name shown once

**Scope.** `finance/static/finance/app.css` (one documented section per component: page header,
workspace, table, forms, badges, empty state, confirmation, toast, pager, steps, cards; tokens
only), `tokens.css` (two further status pairs, voided and attention), `print.css` (new, linked
with `media="print"`), `finance/templatetags/components.py` (new: `status_badge`, `with_done`,
`action_named`; no query), `finance/context_processors.py` (`logo_is_override`),
`finance/templates/finance/base.html` (the name as the logo's text alternative, shown as text
only beside an override logo; toasts; print header and footer), `ledger/views/transfers.py`
(the Confirm step linked only once it can be opened), about ninety templates under
`ledger/templates/ledger/` (classes and wrappers only; no id, field name or URL changed),
`stores/_form_field.html` (the `.field`), tests `tests/test_design_components.py` (new, 44
cases) and small updates of three test files. No migration. Fable's adversarial file:
`tests/test_plan012c_design_b_adversarial.py` (18 cases).

**Gate 1.** ruff, `ruff format --check` (511 files), mypy strict (415 files), `uv lock --check`,
`makemigrations --check` on sqlite and PostgreSQL 16: all clean, exit 0.

**Gate 2.**

| Suite | Engine | Exit | Result |
|---|---|---|---|
| Unit, full | sqlite | 0 | 5,587 passed, 653 skipped; 14:39 wall |
| CI "Migration tests" selection | PostgreSQL 16 | 0 | 28 passed; 11:43 wall |
| CI "Unit tests" selection | PostgreSQL 16 | 1 then 0 | first run 6,208 passed, 1 failed (`test_nsf::test_the_re_debit_date_is_a_calendar_item_without_an_amount`, passed alone at once); full re-run 6,209 passed; 16:57 and 17:14 wall |
| e2e (7), four runs | PostgreSQL 16 | 0 | 7 passed in each of four runs (30–32 s) |
| Fable adversarial B (18) | both | green inside the runs above |

Query budgets (`test_report_performance`, PostgreSQL) unchanged: no component adds a query (the
helpers work on what the view passed). The e2e grid test failed once for Opus during its first
cycle ("0 == 12" month headers, during a concurrent full-suite run); it passed four of four
runs on a clean worktree of the Batch A commit and four of four runs on the Batch B tree on PostgreSQL; the one failure Opus saw happened while its full unit suite ran on the same machine.

**Gate 3.** Diff read in full for the CSS, the template tags, the context processor, the base
template, the transfer view, the print stylesheet and a sample of twelve templates. Classes and
wrappers only; the responsive-table regions carry the table look so no `<table>` tag changed;
the badge glyph is in the markup (hidden from screen readers) so it prints and reads without
the style sheet; "Not due" is a plain badge (no status colour, no glyph).

**Gate 4.** Secret scans clean (`scripts/check-no-secrets.sh`; pattern scan over the diff and
every new file). A flash message and a company name with markup are escaped in the toast and
the print header; a hostile sort or page parameter answers 200 with `aria-sort` only on the
sorted column; the toast's fade is opacity only and never removes the text (warnings and errors
do not fade; no fade under reduced motion); every step link of the wizards answers (the
transfer's "end" step hands over to the lease-end wizard by design).

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| 1 | MAJOR | installments/cancel, invoices/cancel, one_off_charges/{cancel,waive}, contacts/delete | Confirmation pages without `.confirm` (no "What will happen", no secondary button) while the others had them | Resolved: `.confirm` with the question, the list, a `.btn-danger` primary and a Back link; ids and field names unchanged |
| 2 | MAJOR | fourteen hand-laid forms | Controls outside `.field`: the error above the control in the old style | Resolved through the field partial (label override and `mark` flag keep each page's exact label text); left out with reasons: radio and deduction tables, confirmation checkboxes, the upload drop box, the builder's line table, the grid filter row |
| 3 | MINOR | print.css, reports/statement.html | A forced page break before every month of the payment history (a two-year statement printed 24 pages); Fable's instruction said "each month", the plan says store groups | Resolved: no forced break in the statement (month rows kept, `break-inside: avoid`); the P&L keeps its break per store group |
| 4 | MINOR | components.py, app.css | The due glyph ● was the grid's note marker ● | Resolved: due is ○ |
| 5 | MINOR | stores, operators, leases lists | Two `.btn-primary` on an empty list page | Resolved: the empty state's next step is a plain `.btn`; a test asserts one primary per page |

One disputed case was Fable's: the adversarial test expected the transfer's "end" step to
answer 200; it is a redirector to the lease-end terms by design, so the test now accepts a
redirect to the lease-end wizard and refuses one back to an earlier transfer step. Opus's fix
behind that case stays: the Confirm step was linked before it could be opened.

Design choices accepted: the dashboard's card order attention, today's run, failed attempts,
figures, waived (when any), not sent, last reconciliation (an existing test wants the failed
attempts before the figures); only success and information toasts fade; dismissing removes the
message (the reader chose to); the question heading "Are you sure?" and the "What will happen"
label follow the lease-end pages' wording until Batch D; the statement keeps its inline print
rules (`test_reports` asserts them) and shares `print.css`; the `.badge-glyph` is `aria-hidden`
with the label as the text.

**Residual risk (B).** The print layout is asserted on the style sheet and the markup, not on a
rendered PDF: the owner's checkpoint click "print a statement" is the verification. The
component classes are applied where the templates render today; a template no test renders
could carry an unstyled corner (the visual suite of Batch E covers the ten main pages).

**Residual risk (B), continued.** Two single, unreproduced failures in the calendar area in
the full PostgreSQL unit runs of two consecutive batches (A: a calendar entry POST that created
nothing; B: the re-debit calendar item), each green on its re-run and alone, neither in code
either batch touched. The pattern is recorded here for the owner; a plain cause (an
order-dependent leak between tests, or a clock edge) has not been found, and the next full run
that fails will carry the diagnostic added in Batch A. Not a Batch B defect; not fixed here.

**Verdict (B):** PASS (two MAJOR and three MINOR, all resolved in one rework cycle; the
calendar flake recorded above).

### Batch C — forms show only what applies (D8)

**Scope.** `ledger/forms.py` (the `data-shown-when` attribute on dependent fields: the consents,
the second debit day, the reason note; `LeaseForm.twice_monthly`, `LeaseEndDeductionsForm.has_rows`,
`DepositResolutionForm.has_targets`, `TransferIncomingForm(deposit_offered=)`),
`ledger/services/returns.py` (`ReturnRow.debit`), `ledger/views/{lease_ends,leases,transfers}.py`
(the `refund` flag, `provider_ach`, the deposit offer), `ledger/static/ledger/shown_when.js`
(new, no dependency) and `payment_allocation.js`, eleven templates. No migration, no URL change,
no dependency; two new on-screen texts ("Debited twice a month", "Add deduction") for Batch D's
review. Tests: `tests/test_forms_show_what_applies.py` (29), `tests/e2e/test_dependent_fields.py`
(5, one with JavaScript off). Fable's adversarial file: `tests/test_plan012c_design_c_adversarial.py`
(20 cases), run first on a clean worktree of the Batch B commit (20 passed) so that it records
the server behaviour before the batch, then on the Batch C tree.

**How fields are hidden.** State known on the server: the field is not rendered but stays on
the form, so a posted value meets the same form and service rules; a refused hidden field is
rendered with its error. Dependent fields (another control on the same page): a small script
hides the `.field` and disables the control, so what is saved is what is shown; a field with a
server error stays shown; without the script every field stays visible and a tick that only
drives the script (`data-reveal`) stays hidden. Checked in the review: the script reads ids and
values only, writes `hidden` and `disabled`, no markup is built from data.

**The walk (plan §3.5), verified against the diff and the tests.** Settlement: refund method,
date and reference only when the refund due is above zero, else the amount owed and "Save and
continue". Deductions: no row with a value → the rows behind a native `<details>` "Add
deduction" (works without JavaScript). Terms: the reason note while the reason is Other. Transfer
incoming: the deposit field only when the catalogue offers a deposit (a posted amount still
refused), the setup section only with checklist items, the override form only with a debt
(already). Lease form: the second debit day behind a "Debited twice a month" tick that is never
posted; the lease page shows Enable autopay and the enrolment blockers and notes only on a
provider-ACH lease (pause, resume and stop follow the live schedules so a schedule left after a
channel change stays stoppable). Operator form: the consents follow the phone and the e-mail; the
operator page shows the provider identifiers and the enrolment only with a provider customer
(the form has no provider field: Opus's reading, accepted, noted for the owner). Store page
(already), builder (already), Record a payment: credit inputs (already), the keep-credit tick
only while something is unallocated. Deposit resolution: the transfer-to-lease field only when a
target lease exists (a posted one refused as before). Return queue: Reschedule only on a returned
ACH debit. Installments: Retry only on a failed release (already). Document upload: the owner's
kinds only (already).

**Gate 1.** ruff, `ruff format --check` (514 files), mypy strict (418 files), `uv lock --check`,
`makemigrations --check` on sqlite and PostgreSQL 16: all clean, exit 0.

**Gate 2.**

| Suite | Engine | Exit | Result |
|---|---|---|---|
| Unit, full | sqlite | 0 | 5,636 passed, 653 skipped; 14:50 wall |
| CI "Migration tests" selection | PostgreSQL 16 | 0 | 28 passed; 11:47 wall |
| CI "Unit tests" selection | PostgreSQL 16 | 0 | 6,258 passed, 3 skipped; 17:20 wall |
| e2e (12), four runs | PostgreSQL 16 | 0 | 12 passed in each of four runs (38–40 s) |
| Fable adversarial C (20) | both | green inside the runs above; 20 passed on the pre-C worktree |

Opus reported the local e2e suite on **sqlite** failing one test per full run with "database
table is locked" (a different test each time; each green alone; also seen once in four runs on
the pre-batch tree). CI and Fable's gate run the e2e suite on PostgreSQL, where the twelve cases passed four of four runs. The
sqlite lock is the known limit of the threaded live server on sqlite, not a Batch C defect.

**Gate 3.** Diff read in full (the script, the forms, the services, the views, every template).
Hiding is in templates and form helpers; no rule moved or weakened; the "already" items each
gained a test that pins today's behaviour. **Gate 4.** Secret scans clean (script and pattern
scan over the diff and every new file). Fable's cases: a hostile second debit day on a monthly
lease, hostile consents and contact details on an operator without any, a refund method posted
while the operator owes (nothing becomes refundable; what is owed is unchanged), a transfer to a
missing or another operator's lease, a kind outside the owner's list on an upload, the keep-credit
tick on a fully allocated payment, anonymous posts: all refused or ignored as before, no markup
echoed raw.

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| 1 | MINOR | transfers/incoming.html, `TransferIncomingForm` | The incoming step's second debit day has the same twice-monthly dependency as the lease form but no tick | Deferred by written decision to Batch D as a carry-over (the plan's list names the lease form only; the fix is the same tick and script; a re-gate for it alone is not worth a cycle) |

Design choices accepted: the dependent-field script over CSS `:has()`, because disabling the
hidden control keeps a stale value from being saved; with the script, removing the phone or the
e-mail clears that consent on save, and moving the reason away from Other clears the note
(without the script nothing is cleared); the service still saves a refund method posted while
the operator owes (as before: it has no effect on the figures).

**Residual risk (C).** The dependent fields are verified by unit tests on the markup and by
five e2e cases on sqlite and PostgreSQL; the owner's checkpoint click on the lease and operator
forms is the check in a real browser on staging.

**Verdict (C):** PASS (one MINOR deferred to Batch D by written decision; no rework cycle).

### Batch D — the copy pass (D9), built in slices

**How it was built.** The first implementation session renamed the copy across the app and
then stalled three times; a fresh session stalled twice more without an edit. On the owner's
decision (2026-10-18) the renames were kept and the rest was split into slices, each a separate
session with a narrow brief and its own intermediate commit, the full gate once at the end:
D1 `1439f02` (the renames of the first session; the Rule 8 fence; the money filter hardened; the
banner wording), D2a `7fa6881` (the §4.5 labels moved into the `TextChoices` classes; the
parallel label table and seventeen `get_<field>_display` overrides removed; choices-only
migrations ledger 0046 and payments 0018, `sqlmigrate` prints no-op for every operation),
D2b `97c9278`, D2c `10c1287`, D3 `ee8cd57`, D4 `778de0f`, D5 `a2129d5`, D5b `02cc3df` (the
tests of each area moved to the approved wording; defects of the copy pass fixed on the way).
Scope: 190 files against the Batch C commit.

**What the copy pass is.** Every §4.2–§4.7 pair applied in templates, view messages, form
labels, help texts, choice labels, e-mail lines and error strings; the §4.1 rules (the
processor named from company settings and never as a literal outside `payments/`; no machinery
words; autopay, not schedules; check, not tick; marked Needs review; connected and updated from
the processor, not synced; nightly check, not reconciliation; messages from the processor, not
webhooks; from the spreadsheet, not legacy; New York time without a suffix; codes never shown;
e-mail with a hyphen); the `money` filter and `format_amount` (`$2,000.00`, a leading minus,
never `$-`, `""` for anything that is not a finite number) everywhere an amount is shown, CSV
exports and the public statement keeping plain numbers; the banner "SANDBOX — test system, no
real money"; the carry-overs (the transfer step's twice-monthly tick; the Batch B and C texts
reviewed). Stored values, choice keys, URL names, ids, audit action names and CSV column keys
unchanged: Fable's adversarial file freezes every choice value and all 255 route names.

**Defects of the copy pass found and fixed in the slices.** Lazy provider sentences (resolved
at render so the name follows company settings) were stored as proxies in audit JSON
(`enable_autopay`, the insurance manual payment), joined without `str()` (the rent and one-off
review messages, the operator sync message), and compared on the left of `in` in tests; each
fixed at the point of storage or joining, and swept across the 72 constants. The dashboard said
"today" twice; the leases list named an action bar it does not have; the run page showed a
command name; the match report said "Linked with a total mismatch"; job results showed failure
classes and field names ("ProviderRejected (fields: …)"), now words ("(Rejected)") with the
class and the fields kept in the operator row, the audit rows and the message row.

**Gate 1.** ruff, `ruff format --check` (520 files), mypy strict (424 files), `uv lock --check`,
`makemigrations --check` on sqlite and PostgreSQL 16: all clean, exit 0.

**Gate 2.**

| Suite | Engine | Exit | Result |
|---|---|---|---|
| Unit, full | sqlite | 0 | 5,860 passed, 653 skipped; 9:25 wall (the first full run: 2 failed, both old-wording assertions in test files no slice listed, fixed by Fable as test edits, then the whole gate re-run) |
| CI "Migration tests" selection | PostgreSQL 16 | 0 | 28 passed; 8:07 wall |
| CI "Unit tests" selection | PostgreSQL 16 | 0 | 6,482 passed, 3 skipped; 11:50 wall (first run: the same 2) |
| e2e (12), four runs | PostgreSQL 16 | 0 | 12 passed in each of four runs (31 s) (first run: the operator e2e waited for the old "Linked" status text, four of four; fixed as a test edit) |
| Fable adversarial D (25) | both | green inside the runs above; the frozen values and routes passed before and after every slice |

**Gate 3.** Read: `finance/templatetags/money.py`, `ledger/wording.py`, the
`ProviderNameMiddleware` (the request's sentences read the same company row as the shell, so no
extra query), the migrations 0046 and 0018, `payments/one_off_invoices.py`, `payments/jobs/runners.py`,
`payments/operator_sync.py`, a sample of templates. No bare `${{` in any template and no
hand-formatted amount in Python (the copy tests grep for both). The lazy sentence is a Django
idiom (as `gettext_lazy`); its hazard (storage and joins) is now handled at every known site and
the test files exercise them. **Gate 4.** Secret scans clean (the script; a pattern scan over the
whole Batch D diff). Fable's cases: the money filter under hostile input (`NaN`, `Infinity`,
`1E+400`, strings, lists, markup) never raises and never prints a broken amount; the provider
name with markup is escaped on every page that names it and stored as data in the audit row;
user data containing forbidden words is shown as typed; the banner on the sign-in, error and
admin pages, once, with the title prefix kept.

**Findings.** None open from the gate. Three MINOR items deferred by written decision, each a
diagnostic code on an admin page rather than a sentence for staff: the webhook detail page shows
the handler's failure class in its error row; the nightly-check result shows the run's error
class; "Fields <processor> rejected: <names>" stays in the autopay and bank-method results (the
field names are the processor's, and staff fix them by name). Two paths no longer keep the
failure class in the database (a link refused by a processor error; the bank-method refresh):
the adapter's log line keeps it; a future audit row is a Step 13 item.

**Decisions for the owner at the checkpoint (departures from the approved list, each
reversible).**
1. A `TextChoices` label is fixed at import and cannot read company settings, so the labels
   that named the processor (six SyncFailure labels, most JobKind labels, PaymentChannel) carry
   the name through the sentence around them instead: "Bank debit through Stax" on the lease
   page, "Stax request in progress: Updating the operator" in the job banner, "Stax did not
   refund the payment: Rejected." The §4.5 wording is otherwise kept.
2. The owner's change 3 ("Returned payment (NSF) fee") reaches the public statement line for
   that category, where §3.6 says the public wording stays; the label is the category's.
3. Invoice line descriptions sent to the processor now carry the dollar sign ("Poster (0.5 ×
   $0.25)") per §4.8; this changes what the operator sees on the processor's invoice.
4. The three deferred MINOR items above.

**Residual risk (D).** The copy walk is asserted template by template and on rendered main pages;
the e-mail lines and the PDF text are asserted where tests render them. A sentence no test
renders may still carry an old word: the owner's checkpoint click list covers the main pages;
the forbidden-word grep covers them in CI.

**Verdict (D):** PASS (no open finding; three MINOR deferred by written decision; four decisions listed for the owner at the checkpoint).
