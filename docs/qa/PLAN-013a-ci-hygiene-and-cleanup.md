# PLAN-013a — CI hygiene and cleanup, before the data migration

Fable's Phase A plan for the owner's prompt of 2026-10-27 ("Step 13a — CI hygiene and cleanup").
Staging runs 12f-c at `62db08f`. Three batches, one pull request each, the usual gate; push, PR and
merge in order under the standing authorisation (green CI before, a verified staging deploy
after, stop on any red). The QA record is `docs/qa/13a/QA-013a.md`. Checkpoint after the last
batch with per-batch results and the new CI times.

## 1. Objective

**A — CI.** The migration-test job stays under 20 minutes on a merge run, its limit goes back to
30 minutes and the "temporary" comment goes; one workflow run per pull request (pull_request for
PR branches, push for main only). **B — business data out.** `docs/inputs/` and every other file or
line holding real business data (store numbers with towns, revenue totals, operator nicknames,
staff names with roles, the real store-cost rows) leave the working tree for a private Blob
container the owner alone can read; a `docs/inputs/README.md` says where; what history still holds
is stated, not rewritten. **C — cleanup.** The three "by" cells still showing usernames; the empty
Refunds card hidden on payments recorded outside the processor; the invoice builder's no-bank
sentence aligned with the Add payment card; C2-3 (the session note); F-2 (the shared wizard
redirect helper); B-3 and C-5 from 12f-a; the once-failed PostgreSQL feed test closed with evidence.

## 2. Packages and scope

`.github/workflows/ci.yml` (A; Fable may edit the job's shape as verification config, Opus edits
the deploy-adjacent triggers — both are in the same file, so Opus edits the file from Fable's
written shape and Fable gates it), `backend/tests/test_migrations_roundtrip.py` (A, Fable: the
shard markers), `docs/` (B, Fable), `infra/README.md` (B, a note), `ledger` services, views and
templates (C, Opus), `finance/static/finance/app.css` (C, B-3), tests. Out of scope: any history
rewrite; the data migration itself (13b); the USPS apply (waits for the owner's secrets).

## 3. Decisions

### A — CI

- **A1. The migration job: split, not squash.** Measured on PostgreSQL 16 locally (the same split
  CI runs): 36 tests in 1,657 s (27:37) for the job's selection, of which 26 measured one by one: twenty-two "over existing rows" tests at 21 to 57 s each (most 44 to 57 s), the three rename and concurrency tests at 32 to 36 s, the index round trip at 30 s, and four tests under two seconds; the ten others (the two fresh-database round trips, the rename over rows, the lease-end, transfer, installments, debt-override and setup-flag cases) take the remaining 570 s, about 60 s each. Every "over existing rows"
  test re-runs `manage.py migrate` from an empty database to its target and back, so the cost is
  (number of tests) × (full forward migration of 59 + 20 files); a squash to a checkpoint would cut
  each forward run but break the tests that target intermediate migrations by name (they exist to
  prove the data handling of those migrations), and a squash rewrites what production has applied
  — a change for 13b at the earliest, when the data migration fixes the schema it lands on. The
  split: the job becomes a matrix of **three shards** (`shard: [1, 2, 3]`), each running the
  round-trip file's tests of one shard mark (`@pytest.mark.roundtrip_shard(n)`, assigned by Fable
  from the measured durations so the shards are within two minutes of each other), plus the
  report-indexes file and the three rename/concurrency tests in shard 1; the job's name stays
  "Migration tests" with the shard in the matrix title, `timeout-minutes: 30`, the TEMPORARY comment
  gone, the comment stating the measured per-shard time. A fourth check, `migrations-complete`,
  needs the three shards so the deploy job's `needs` keeps one name. Expected: three shards of 490 to 545 s of tests each locally (shard 1: the fresh round trips, the rename and concurrency cases, the index round trip, the kind, item-on-sqlite and promise cases, the fast checks; shard 2: lease end, transfer, installments, debt override, statement opened, rent lines, credits, item-on-PostgreSQL; shard 3: setup flag, company settings, store address, check method, autopay plans), so 9 to 10 minutes of tests plus about two minutes of setup per shard on CI, which ran 10 to 25 percent slower than this Mac — 11 to 14 minutes a shard against the 20-minute target and the 30-minute limit. The assignment lives in one table in `backend/tests/conftest.py` (`ROUNDTRIP_SHARDS`), applied as the markers `shard1` … `shard3` at collection; a completeness test asserts every test of the five files is in exactly one shard.
- **A2. One run per PR.** `on: push: branches: [main]` and `on: pull_request:` (any base); the
  deploy job's condition is unchanged (`push` to `main`). The concurrency group keeps superseding
  in-flight runs of the same ref. Expected: one run per PR commit, one per merge.

### B — business data out of the repository

- **B1. What moves.** The nine files of `docs/inputs/` (the Google Sheet audits, the owner's
  answers, the Stax answers and endpoint notes) and `docs/templates/Store_Costs_Template.csv`, which
  is not a template but real rows (store numbers, towns, operator nicknames, rents); a template with
  three invented rows replaces it under the same name, so the import code's reference holds.
- **B2. What is redacted in place.** Lines in `docs/SCOPE.md`, `docs/COLUMN_MAPPING.md` and
  `docs/STATUS.md` that carry real store numbers with towns (3409 Leicester, 2629 Northbridge, 2922
  Tucson, 2204 Forest Park, International Plaza), the revenue total ($1,470,001.57 and the
  392 / 47 / 13 counts), the staff names with roles (Ahmed, Zahir, Fatih) and the operator nicknames:
  each becomes a placeholder ("store A", "a twice-monthly store", "the August 2026 total", "the
  three staff members"); the sentences keep their meaning. The staff first names in `docs/design/`
  screenshots ("fatih", "halid") are staging operator names in reference images and stay (they are
  not customer data). The sandbox merchant ID and the resource names stay (not personal data; the
  exposure check of 2026-10-26 listed them for the owner).
- **B3. Where it lives.** A new private container `owner-inputs` on the staging documents storage
  account `stfinancestagingavyxps6f` (`finance-sandbox`; public access already off at the account),
  with one role assignment, Storage Blob Data Contributor for the owner's user alone; the files
  uploaded under `inputs/<date>/`. Creating the container, the role assignment and the upload are
  Section 6 actions: a Publish Request names each, the owner's yes runs them (the files are kept
  locally in the scratchpad until the upload is verified by listing, never printed). `docs/inputs/README.md`
  says the location, the access rule and that the sheet itself was never in the repository.
- **B4. What remains in history.** Every file moved or redacted stays in every commit that held it,
  on `main` and on the merged branches, in the public clone anyone may have taken while the
  repository is public (until 2026-10-31), and in GitHub's cache of the old blobs. Only a history
  rewrite plus a force-push (Section 6.2 forbids it on `main` without a human running it) and
  GitHub's own cache purge would remove them; the owner decides that apart from this step.

### C — cleanup (Opus)

- **C1. The three "by" cells.** `store_costs` passes the user (not the username) to the cost
  history, `collections` passes `sent_by` as a user to the log, and the audit page's and the store
  History tab's "User" columns render through the `staff` filter; usernames nowhere on a page.
- **C2. The empty Refunds card.** Hidden on a payment recorded outside the processor (the same
  rule as the two tiles: `applied_tile` set); processor payments keep it.
- **C3. The builder's no-bank sentence.** `invoice_builder.bank_unavailable` uses the Add payment
  card's sentence "No bank account on file for {operator}." and the greyed tile carries the
  "Operator page" link under it (the `collect-link` class), the processor reason under that when the
  flag is off as well; `DEBIT_NO_METHOD` deleted when nothing else reads it.
- **C4. C2-3.** The today's-debit note is kept in the session under the key for at most one
  request: the rent charge page's GET and the Add payment page's GET pop every `rent-debit-note:<charge>:*`
  key of that charge, so an abandoned confirmation leaves nothing behind.
- **C5. F-2.** `_statement_first` lives once, in `ledger/views/wizard_frame.py` (the shared wizard
  module), imported by `lease_ends.py` and `transfers.py`.
- **C6. B-3.** The list family's phone meta line: the role card's inline links at text height while
  the row stays at least 44 px tall; the two badges stay on one line at 390 px (the stores list and
  the operators list; the baselines `stores-390`, `operators-390` regenerated).
- **C7. C-5.** The statement step's Deductions tile follows the table live (the script that already
  totals the typed amounts updates the tile; the server value stays for scripts off).
- **C8. The feed test (Fable).** `test_collections_adversarial.py::test_the_feed_never_carries_a_card_number_or_a_private_marker`
  asserted `"4111" not in body` over an ICS feed whose UIDs are random hex, so one UID in a
  thousand holds "4111": the once-failed PostgreSQL run of 12f-a is explained, not a product
  defect; the assertion checks the card number as typed and with its spaces removed, and the item
  closes with that evidence.

## 4. Test plan

- A: the three shards run the whole round-trip selection between them with no test twice or
  missing (a test that collects the file with each mark and asserts the union equals the file's
  tests); CI's first PR run and the merge run give the times.
- B: `scripts/check-no-secrets.sh` and a pattern grep for the moved values over the tree pass; the
  template CSV's import test (if one reads it) passes with the invented rows.
- C: a page test per cell (C1); the Refunds card absent for a Zelle payment, present for ACH
  (C2); the builder's tile sentence and link (C3); the session key gone after the charge page
  GET (C4); the wizard tests unchanged (C5); the list cards' phone layout pins (C6); the tile
  value with the script (C7, a browser test); the feed assertion (C8).

## 5. Risks

- A: a shard that forgets a test silently drops coverage — the union test guards it.
- B: the redaction must not change a decision's meaning; the upload must not be printed; the
  public history keeps the data regardless.
- C: C3 touches the builder's greyed-tile markup; C6 is CSS on every list page.

## 6. Acceptance

The merge run's migration shards each under 20 minutes; one workflow run per PR commit; no real
business data in the working tree (the grep list of B2 finds nothing); every C item has its test;
the QA file holds the three sections with the CI times.
