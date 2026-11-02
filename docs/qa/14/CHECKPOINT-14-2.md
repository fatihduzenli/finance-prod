# Checkpoint 2 — Step 14 Part 2 (Batches D–I and slice I-b)

**As of 2026-11-01.** Every Step 14 batch is merged to `main` and live on staging
(`8c9dda8`). Gated by Fable through Batch H's gate run 2 (finding H-5); from H-6 and the merge
of PR #30 on, by Opus (owner, 2026-10-31: the rest of Step 14 on Opus, Fable for this
checkpoint's review only). Fable's review of this checkpoint is §10. The record of
each batch is `docs/qa/14/QA-014.md`; the plan is
`docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md`.

## 1. What was merged

| Batch | PR | Merge | What |
|---|---|---|---|
| C | #25 | `8fdd053` | Billing details and the processor customer per lease; bank entry (Part 1, checkpoint 1) |
| D | #26 | `dbd77f2` | The messaging engine, message templates, the Outbox, the send window |
| E | #27 | `bf7bd00` | Reminder rules A–E; rent months keyed to lease and month (owner's E4 decision) |
| F | #28 | `1b865a6` | Returned bank debits: plain-words reasons, the NSF fee, the automatic re-debit, Stop |
| G | #29 | `9aeb95a` | The Aging report by store, reconciled with the Unpaid figure and the Charges tabs |
| H | #30 | `2a78dff` | The operator portal: separate sign-in, stores, charges, invoices, Pay, Bank account |
| I | #31 | `d4553da` | Bank deposits through Plaid (dark by default), matching, the memo line |
| I-b | #32 | `8c9dda8` | The owner's checkpoint 1 fixes (LEASE card, operator State, consent fields) and the sign-in redesign |

PRs #27 and #28 carried G's title by a script error; retitled on the owner's instruction.

## 2. Migrations (all reversible unless stated)

| App | Migration | Batch | Note |
|---|---|---|---|
| ledger | 0061 | D | message templates (seven seeded), rules, outbound messages |
| ledger | 0062 | E | `period_month`; one message per lease-month per rule |
| ledger | 0063 | F | `retry_date`/`retry_state`; one fee per returned payment |
| ledger | 0064 | H | portal messages; templates `portal_invite`, `portal_reset` |
| ledger | 0065 | I | `bank_deposits_enabled`, `bank_auto_confirm_exact` (both off) |
| ledger | 0066 | I-b | operator State to codes; unmatched → Needs review, audited `operator.state_unmapped` |
| payments | 0021, 0022 | D, H | `Job.run_after`; `PORTAL_PAY` |
| portal | 0001, 0002 | H | the portal's users, tokens, attempts, payments |
| banking | 0001 | I | connections, transactions, sender names; **refuses to reverse while rows exist** (by design) |

All ran on staging with their deploys (each deploy job green, staging verified on the merge
commit: web and worker revisions, both jobs, `/healthz` 200).

## 3. Tests and gates

Every batch: ruff, format, mypy strict, lock, makemigrations on sqlite and PostgreSQL, the secret
scan, the full sqlite suite, the PostgreSQL migration and unit selections, e2e four times, the
visual suite regenerated against PostgreSQL and compared on both databases — with two
qualifications: the H-6 and I-3 fixes (one CSS rule, one help sentence) were checked by their
pins, the touched tests and the visual suite, and the full suites ran in the PR's CI only; and
slice I-b's Gate 3/4 review was done by Opus, the model that built it (Fable read the sign-in
view, the banking module, Bill now and 0066 at this checkpoint, §10). Last gate (I-b):
sqlite **9,292 passed**; PostgreSQL migrations **36**, units **10,067**; e2e **86 ×4**; visual
**148 + 148**. Findings per batch, all resolved or with a written decision, are in QA-014.

Two things the gates found outside their batch:
- **Tests broken on every branch from 2026-11-01**: dashboard fixtures that patched a `today`
  name that does not exist (`raising=False`), so they pinned nothing and passed only in October;
  and consent-time tests on the real clock in the DST fall-back hour. Fixed in the tests and
  carried into H, I and I-b. Residual (backlog): a consent time typed in that repeated hour saves
  one hour early.
- **The CI unit split** outgrew its 20-minute halves (B 15 → 20 minutes over five merges, then a
  timeout on #30). A count-based rebalance timed half A out at 25 minutes; the unit tests now run
  in **three shards balanced by measured time** (A `test_a`–`test_i`, B `test_p`, C the rest).

## 4. CI times (main runs after each merge, minutes per job)

| PR | Unit A | Unit B | Unit C | Migrations (4 shards) | e2e | Visual | Deploy | Run |
|---|---|---|---|---|---|---|---|---|
| #25 C | 9 | 14 | — | 7, 10, 10, 11 | 4 | 3 | 3 | 19 |
| #26 D | 13 | 16 | — | 11, 12, 12, 12 | 4 | 3 | 3 | 20 |
| #27 E | 14 | 15 | — | 10, 11, 12, 12 | 4 | 3 | 3 | 20 |
| #28 F | 14 | 19 | — | 10, 12, 13, 14 | 4 | 3 | 3 | 23 |
| #29 G | 9 | 19 | — | 9, 12, 13, 14 | 4 | 3 | 3 | 24 |
| #30 H | 13 | 12 | 8 | 9, 14, 15, 15 | 4 | 4 | 3 | 20 |
| #31 I | 12 | 8 | 12 | 10, 11, 13, 16 | 4 | 3 | 3 | 20 |
| #32 I-b | 12 | 10 | 9 | 12, 13, 13, 15 | 4 | 3 | 3 | 19 |

With three shards every unit job is at most 13 minutes (limit 25). The migration shards are now
the longest jobs (up to 16 minutes of 30): the next thing to watch.

## 5. The daily job on staging (the Outbox listing) — NOT DELIVERED

**The Outbox listing the prompt asks for is not delivered.** The models cannot open the Outbox
(it needs a signed-in staff session with the second factor), no message was queued on staging
today, and the entries held from earlier days have not been read. The reminder rules A–E have
been seen only on built worlds in the tests, never rendered on real staging data. To close this:
the owner opens Admin › Outbox on staging and sends the held entries (template, store, scheduled
day, held reason), or authorises a read-only query that lists them without addresses.


The scheduled run of 2026-11-01 13:00 UTC (08:00 New York since the clocks went back; the
plan's "09:00" holds only in daylight time — messages queued before 09:00 wait for the send
window, so in winter every reminder waits on the worker for an hour; moving the cron to 14:00 UTC
in winter would be a Section 6 change outside this step): rent charges created 3 (1 already there), reminders
queued **A 0, B 0, C 0, D 0, E 0**, re-debits 0, morning e-mails 0, promise reminders 0, the
dispute list read queued. The manual run the owner authorised (19:45 UTC, after I-b's deploy)
succeeded and created nothing more ("Already created: 4"; "Bank deposits: off. …"), which is
the job's idempotency on real data. **No message was queued on staging today**, so there is no new
Outbox entry to list; the staging data has no lease within three days of a due date or debit, and
no rent five or ten days overdue with a billing e-mail on 2026-11-01. The Outbox page itself needs
a signed-in staff session (the owner's view); its held entries from earlier days stay held (mail is
not set up on staging).

## 6. Chosen differently (each with its reason; detail in QA-014)

- **E:** rules A and B keyed to the lease and the rent month, not a charge (owner's E4 decision);
  no catch-up for days the job missed; D counts from the day it becomes due.
- **F:** the fee is the first-return amount ($100.00) as template F says; only R01 and R09 are
  re-debited automatically; the retry's processor schedule is created on the eve.
- **G:** the Charges tab's total now counts what the open balance counts (deposits and plan
  parents listed, not summed); the kind filter changes amounts, the row filters never do.
- **H:** a separate principal at `/portal/` (ADR-0017); one-time links issued at send and never
  stored; an insurance charge cannot be paid online (no debit path), so a store with one offers
  its charges one by one.
- **I:** dark by default (ADR-0018); only exact matches may auto-confirm, and only when switched
  on; Choose and Split list the store's current lease only; no CSP on the settings page while
  Plaid Link loads (deferred to TODO-VERIFY-071).
- **I-b:** "Keep me signed in" never lengthens a session (30 min idle, 12 h absolute) and is
  unticked by default; "Forgot password?" and "Need help?" are mail to the support address (no
  self-service reset); the sandbox pill keeps Rule 6's text.

## 7. Open questions for the owner

1. **Ended leases in Choose and Split (Bank deposits):** fall back to the store's latest ended
   lease when it has open charges? Today a departed operator's wire without an invoice number is
   recorded from the lease page.
2. **A twice-monthly lease's second debit:** its own rule-A notice, or one per month?
3. **The bank's name** (approved 2026-11-01): a small item after this checkpoint — the processor's
   spec first; Rule 1 amended for the institution name only, with a new ADR.

## 8. TODO-VERIFY (Part 2)

| Item | What must be verified, and by whom |
|---|---|
| 066 | Stax.js hosted bank fields and the web-payments token (none issued; the bank-entry pages stay off) |
| 067 | Per-lease customers on live Stax (`create_lease_customers`'s live run) |
| 068 | Live ACH from the portal through pay-an-invoice |
| 069 | Whether a replaced bank method's schedules and the old method should be changed at Stax |
| 070 | Live mail after the owner's Workspace delegation |
| 071 | Plaid with a real linked business account (sync categories, payment details, Link's CSP) |
| 072 | Nacha re-presentment: two re-debits within 180 days against the merchant agreement; and the first live re-debit (the eve-created retry schedule on a live key, the R01/R09 return path) |
| 073 | The Federal Reserve holiday table's source and upkeep (2026–2028 as built) |

## 9. What remains a Publish Request

`create_lease_customers` on staging against the sandbox (plan §10; checkpoint 1, decision 4):
one processor customer per lease that shares another's account. It writes to the processor's
sandbox, so it needs its own authorisation; the dry run's count first.

## 10. Fable's review of this checkpoint (2026-11-01, read-only)

Fable read this checkpoint against QA-014, the plan, the prompt and `origin/main`, and
spot-checked the code Opus gated without Fable: the staff sign-in view ("Keep me signed in"
switches only the cookie's persistence; the 30-minute and 12-hour limits pinned), the banking
module (Fernet at the call, logs without bodies, role checks, row locks, creation keys,
`_is_cents`, the reverse refusal), insurance Bill now (billing twice, POST, idempotent, audited),
migration 0066, the three-shard CI and the date fixes. **No Gate 3 or Gate 4 defect, no waved-through
finding, no weakened test.** One MAJOR on this document (§5: the Outbox listing not delivered —
now stated plainly, the owner's view asked for) and five MINOR record corrections (the handover
point; the CI-only full suites and I-b's self-review; the three-shard CI's QA entry, added to
QA-014; the live re-debit in row 072; the 08:00 winter run) — all applied. Not verifiable by
Fable without GitHub or Azure: §4's CI times, the staging deploys, the two job runs.
