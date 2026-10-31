# Step 14 — Checkpoint 1 (after Part 1: Batches A, B, C)

Written 2026-10-30 13:20 EDT by Fable for the owner. Record: `docs/qa/14/QA-014.md`; plan:
`docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md`; decisions: ADR-0015 (and
ADR-0016 for Part 2, already appended).

## 1. Where Part 1 stands

| Batch | PR | Merged as | Staging | QA verdict |
|---|---|---|---|---|
| A — dashboard, lists, small fixes | #23 | `41d5d2d` | verified (web, worker, both jobs) | PASS |
| B — store page, brands, notes | #24 | `aec4f4e` | **incomplete**: web and the two jobs on B; the worker still on `41d5d2d` (Azure answered "containerapp does not exist" once during the update; the step tolerated it; the verify failed) | PASS (tests); deploy red |
| C — operator is a person, billing per lease | not opened | `step-14/batch-c` = `f258c42`, gate green on both databases | not deployed | PASS, merge held |

"Stop on any red": nothing has been pushed since B's deploy failed. The one action that
unblocks C (and then D, already gated locally) is your authorisation to re-run the failed
"Deploy staging" job of run 41024362999 (the Publish Request of 2026-10-30 ~05:20). A re-run
touches only staging; the images are already built; it is reversible by rolling the revision back.

## 2. Migrations in Part 1

| App | Migration | Batch | What it does |
|---|---|---|---|
| accounts | 0004_payment_status_period | A | the per-user Weekly/Monthly choice of the dashboard's Payment status |
| ledger | 0058_brands_and_store_notes | B | `brands` (seeded iFixandRepair · Techy · Talk&Fix), store type → brand, store notes with pinning |
| ledger | 0059_lease_billing | C | `lease_billing` (one row per lease), the processor columns on `leases`, **the one-time copy** (below) |
| ledger | 0060_transfer_incoming_billing | C | a transfer's incoming lease gets its own billing row (a copy, not a share) |
| payments | 0019_lease_job_kinds | C | the operator sync job kinds renamed to `lease_*`; `lease_method_attach` added |
| payments | 0020_lease_account_switch | C | the `lease_account_switch` job kind ("Switch to own account") |

Every one runs forward and back on sqlite and PostgreSQL 16 in the CI split and the local gate;
0059 refuses to reverse once two leases hold their own customers (it cannot know which was
primary); 0058's reverse keeps the brand rows for history.

## 3. The per-lease billing migration (0059) — what it does, and on staging data

**It has not run on staging yet.** It runs with Batch C's deploy, which waits on the re-run
above. What I can report now is the rule it applies and the evidence it was built against; the
staging numbers follow the deploy.

The copy, once, in one transaction, calling no provider:

1. Every lease gets one billing row with its operator's e-mail, phone and address; the company
   name is copied only when the operator was of the kind "company". A lease without an operator
   gets an empty row.
2. For each operator that holds a live processor customer, the **primary lease** is its earliest
   started active lease (ties: lowest store number, then id). The operator's customer and bank
   method references are re-pointed to that lease (the external ids do not change: the processor
   sees nothing), its sync state is copied, and its bank last four comes from the latest method
   snapshot. Every **other** lease of that operator shares the primary's account
   (`shares_account_of`) and, if not ended, is flagged Needs review "Own processor customer
   needed".
3. An operator with a customer and no active lease keeps its references (audited
   `operator.refs_kept`); nothing is deleted.

Each step writes an audit row (`lease_billing.migrated` per lease, `operator.refs_kept` per
operator), so after the deploy the numbers come from the Activity log on staging, read-only:
leases given a billing row, leases that became primary, leases that share, operators whose
references were kept. The command `create_lease_customers --dry-run` prints the sharing count
too, but running it on staging needs an `az containerapp exec` — a Section 6 action I will ask
for separately if you want the number from the command rather than the log.

Evidence so far: built worlds with one-lease and multi-lease operators on sqlite and
PostgreSQL, forward and back (the gate's migration selection, 36 tests, and
`test_plan014_c_billing.py`'s migration cases); a probe PostgreSQL database for the one
round-trip failure of run 3, which was my test's expectation, not the migration. Residual: real
staging data has operators without leases, archived operators with customers and leases without
start dates — the rule covers each (step 3; "no start date sorts last"), but the counts are what
the deploy will show.

**After the migration on staging:** every multi-lease operator's further leases share the first
lease's account until the sandbox run of `create_lease_customers` (one processor customer per
sharing lease; a Publish Request of its own, with the dry-run count) and, per lease, "Switch to
own account" once that customer holds a bank method. Rent collection is unchanged meanwhile:
the shared customer and method keep debiting.

## 4. Chosen differently from the prompt or the plan (all recorded in QA-014 and plan §8)

**Batch A**
- No Invoices list page exists, so none got the filters; not built unless you ask.
- Sort menus only where a page sorted before (Stores, Unpaid balances); Apply is the one dark
  button per filter panel; "Vacant" keeps the list's rule (a lease not ended before today is
  current).
- The P&L's From/To months live inside the Filters panel; the range reads in the table's title.
- The user menu's "Settings" became "Preferences" (in B) so that Admin › Settings is the one
  page called Settings.

**Batch B**
- No "Bill now" on the Insurance row and the row is not a link: there is no per-lease insurance
  creation route or insurance charge page today. **Decision 3 below.**
- The stores list gained a Brand filter and the CSV a Brand column.
- The Notes & follow-ups tab shows 20 rows; the All entries page the rest, paged; its
  promise-status filter became the four-way filter; note actions from that page return to the tab.
- `Operator.needs_review` had never been set by code, so 0058 records the flag's state and clears
  it only when the brand was its one reason.

**Batch C**
- "Use the same account as #1991" is implemented as sharing the other lease's processor customer
  and bank method (a bank method belongs to one customer and cannot be copied); a lease moves to
  its own customer later with "Switch to own account" (ADR-0015).
- `LeaseBilling.lease` cascades with its lease (plan said PROTECT): the row is part of the lease.
- The switch runs as its own job kind (`lease_account_switch`, payments 0020); the transfer's
  incoming billing row is a copy in ledger 0060.
- The operator's old processor references stay (not deleted) for the audit; a lease's references
  carry `entity_type="lease"`.
- The operator statement spans the operator's leases and prints the full name with each store's
  company; the signed statement link stays per operator.
- Hosted bank fields: the page is built, but staging cannot use it — no web-payments token exists
  for the sandbox and the sandbox cannot tokenize a bank account; the page says "not set up" and
  staff keep the Link form (customer id and method id from the Stax dashboard). **Decision 2.**
- The operator form has four fields (name, e-mail, phone, address); the operator search no
  longer matches first/last-name columns (they are gone).

## 5. Screenshots (both themes, both widths)

- `docs/qa/14/shots/a/` (9): dashboard with the Weekly | Monthly control, the lists with Filters,
  payments tiles, the phone pill.
- `docs/qa/14/shots/b/` (8): Notes & follow-ups with a pinned note and the NOTES card, the LEASE
  card, Admin › Settings' Brands.
- `docs/qa/14/shots/c/` (12): the LEASE card's Billing and Bank rows, the billing dialog of a
  sharing lease ("Uses the account of 1601"), New lease's Billing details after "Copy from
  operator" with "Use the same account as #1601 (····4821)", the four-field operator form, the
  bank-entry page ("not set up").

## 6. Decisions I need from you

1. **Re-run the failed "Deploy staging" job of run 41024362999** — the standing Publish Request;
   a plain "yes, re-run the deploy" covers that one re-run. Then C's PR opens and merges under your
   standing merge authorisation, and 0059 runs on staging.
2. **TODO-VERIFY-066 — hosted fields.** Please confirm with Stax whether Stax.js hosts the bank
   account and routing fields in its own iframe (then Rule 1 holds and the page can go live once a
   web-payments token exists), or takes them as plain values in our page (then the page must stay
   off and ADR-0017's premise changes). Also whether a sandbox web-payments token can be issued.
3. **Insurance "Bill now".** Build per-lease insurance charge creation (a small addition in a later
   batch) or leave the yearly command as the only path?
4. **The sandbox run of `create_lease_customers`** after C deploys: I will send its Publish
   Request with the dry-run count; say now if you would rather not create per-lease customers on
   the sandbox at all.
5. **Accept the departures** in §4, or name the ones to revert.

## 7. TODO-VERIFY added in Part 1
066 (Stax.js iframe and the sandbox token), 067 (the live per-lease customer run), 068–073
(Part 2's items, listed in `docs/TODO-VERIFY.md`).
