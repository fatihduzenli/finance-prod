## QA Report — PLAN-011b: collections ladder by Gmail, NSF re-debit, attention list, contact log, calendar and ICS, New York time, grid usability

**Date:** 2026-10-06 · **QA owner:** Fable · **Implementer:** Opus
**Plan:** `docs/qa/PLAN-011b-collections-nsf-attention-calendar.md` · **Decision record:** ADR-0010

**Scope.** `backend/finance`: `TIME_ZONE = "America/New_York"` (storage UTC), env guard and settings
for mail (`MAIL_ENABLED`, `GMAIL_SENDER`, `GMAIL_SERVICE_ACCOUNT_JSON` secret, `MAIL_CATCH_ALL`,
`COMPANY_NAME`), log and Sentry redaction of the key, `ratelimit`. New `backend/mail/` (Gmail API
sender by delegated service account through `httpx`, memory and null senders, catch-all wrapper;
Google imports fenced to `mail/gmail.py`). `backend/ledger`: `merge` (whitelisted merge fields),
services `attention`, `contacts`, `calendar`, `collections`, `nsf`, `statement_links`, and changes
in `receivables`, `returns`, `one_off_charges`, `rent_run`, `pnl`, `reports`; models and migrations
0023 (contacts, calendar, feeds), 0024/0025 (ladder tables and seed), 0026/0027 (NSF, reschedule,
statement-link revocation, fee types); views for the calendar, the feed, settings, contacts,
collections, returns (Reschedule), the signed statement page; `tz_audit`/`tz_apply`;
`collections_tick`; the grid CSS. `backend/payments`: `timezone_audit`, job kinds `message_send`,
`calendar_email` (migration 0014), two-line schedules and invoices, allocation to companion fee
charges and the source charge after a re-debit. Infra: mail secret references and env on web and
worker (only when `mailEnabled`), new daily scheduled job, CI image roll and verification.
Dependency: `google-auth==2.60.0` (Apache-2.0), transitive `pyasn1`, `pyasn1-modules` (BSD).

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0, 0 | clean, 400 files |
| `uv run mypy` (strict) | n/a | 0 | 331 source files |
| `az bicep build --file infra/main.bicep` | n/a | 0 | clean |
| `uv lock --check` | n/a | 0 | 65 packages |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | PostgreSQL 16 | 0 | 4,740 passed, 3 skipped |
| same | sqlite | 0 | 4,219 passed, 524 skipped |
| `uv run pytest -m e2e -q` (phone and laptop viewports) | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan over the diff (keys, tokens, private keys, Google access tokens) | n/a | 0 | clean |
| `az deployment group what-if` (read-only, staging, `mailEnabled=false`) | n/a | 0 | Create job `finance-staging-daily`; Modify web and worker containers (env); registries noise; nothing else |

**Tests.** 4,246 → 4,740 on PostgreSQL. Opus: `test_timezone` (29), `test_attention` (18),
`test_contacts` (27), `test_calendar` (39), `test_grid_usability`, e2e laptop grid, `test_mail` (67),
`test_collections` (59), `test_nsf` (43 + 13 for the source-charge fix), `test_statement_links` (15),
`test_infra_mail` (41), the report property test extended with rescheduled returns. QA-owned
`test_collections_adversarial.py` (17): the service-account key marker absent from logs, audit
rows, message rows, pages and errors after a failing token exchange; outside production every
envelope goes to the catch-all whatever the recipient; boot refusal without the catch-all with no
value echoed; nine template bodies that try attribute, index, conversion, format-spec, positional
and unknown access are refused on save and at render; Reschedule closed to viewers and the waiver
to billing; feed tokens not guessable by near misses and dead once revoked, never in the audit;
the feed never carries a card number; a statement link cannot be forged, bent or reused after
revocation and shows no other operator's data, no app links, no script; `tz_apply` refuses a wrong
count and writes nothing.

**Findings.** BLOCKER 0. **MAJOR 1, resolved:** after a rescheduled return was re-collected the
source rent month stayed unpaid in the grid and on the run page (the source charge now reflects
settled re-debit payments up to the re-debit's price, on settle, return, refund and void; a chain
double count in open balances found and fixed on the way). MINOR 2, resolved: the two new
identifier columns in the schema test; grid month columns at 80 px so twelve months fit at
1,280 px. Design choices accepted: the `{statement_link}` merge field became a signed 30-day
revocable read-only page (it pointed at a staff-only page); `mail_message_id` naming; one ladder
step per charge per tick; the pinned Google token endpoint; promise reminder = a promise due today
to its author; hard delete of calendar entries with the audit row; feed-token hash as bytes;
"owed once" for re-debits; the fee excluded from the schedule when waived; Reschedule refused for
archived operators or operators without a provider customer; the `.env.example` edit (names only).

**Residual risk.**
- Nothing ran against Google or Stax: domain-wide delegation, the Gmail send, the two-line
  schedule/invoice and `meta.reference` are verified against mocks only; the owner's catch-all
  e-mail after the owner's Workspace steps (plan §9) is the proof. Mail stays **off** on this
  deployment (`mailEnabled=false`) until the key is in the vault.
- `tz_audit` on staging runs after the deployment; its list goes to the owner before `tz_apply`.
- Two URL-credentialed read-only pages exist (the ICS feed and the statement link): revocable,
  rate-limited, audited, no bank data; the gunicorn access log can still record a 200's path.
- The in-memory rate limit is per replica (N replicas allow N× the rate).
- DKIM for the domain is not published (SPF covers Google; recommended in plan §9).
- Still deferred by written decision: the ledger "Deposits held" total over the filtered set.

**Verdict: PASS** (pending the staging done-when after the push and the deployment; the mail
done-when waits for the owner's Workspace steps).
