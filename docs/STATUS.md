# Status note — 2026-09-29

Written at the end of the Azure and DNS work, before Prompt 4 starts.
Prompts 0 to 3 are complete. Prompt 4 has not been started.

## What is live

| Item | State |
|---|---|
| Staging address | `https://staging.monvera.io` |
| Certificate | Azure managed certificate, valid to 2027-03-29, renewed by Azure |
| Region | Central US for the app, network and database; East US for the registry, identity and staging vault |
| Subscription | Pay-As-You-Go, spending limit off |
| Image | `finance-web` built from commit `02ff92b` |
| Git | local `main` equals `origin/main` at `02ff92b`; this file is new and not committed |
| DNS zone | `monvera.io` delegated to Azure DNS; records: root SPF `v=spf1 -all`, `staging` CNAME, `asuid.staging` TXT |
| CI variable | `APP_BASE_URL` is `https://staging.monvera.io` |
| Static egress IP | `20.12.221.227`, available for Stax whitelisting (TODO-VERIFY-010) |

## Verified from outside on 2026-09-29

| Check | Result |
|---|---|
| `GET /healthz` | 200, `status ok`, `app_env staging`, `stax_env sandbox` |
| `GET /admin/` without a session | 401 |
| Plain HTTP | 301 to HTTPS |
| SANDBOX banner and `[SANDBOX]` title | present |
| `GET /account/login/` | 200 since commit `89d0066` (was 500, defect 1) |
| Default Azure address | 400 by design; the app accepts only `staging.monvera.io` |

## Defects found, not fixed

**Update 2026-09-29, later:** defects 1 and 2 are fixed in code under PLAN-003a, QA verdict
PASS (`docs/qa/QA-003a-staging-defects.md`). **Deployed to staging on 2026-09-29** at commit `89d0066` after the owner's push approval:
login page 200, migrations applied at start, errors with traceback in the container log. Defects 3 and 4 are unchanged.

Each needs a production-code or infrastructure change. Under the root
`CLAUDE.md` that means an Opus assignment, a QA gate and an approved push.

1. **Migrations never run on deploy.** All 27 migrations are unapplied on
   staging, so the login page returns 500. Nobody can sign in until this is
   fixed. The image starts gunicorn only.
2. **No error logging to the container log.** Django has no `LOGGING`
   configuration and no Sentry DSN is set, so a 500 leaves no trace.
3. **The database password rotates on every infrastructure deployment.** The
   template generates a new password each run. The app must be restarted
   afterwards. It was restarted after the hostname deployment. This design is
   not acceptable for production.
4. **The local Bicep compiler crashes** on this machine. The hostname
   deployments ran from a compiled JSON copy of the same template.

## Owner actions outstanding

- Overwrite the **placeholder** in the staging vault under
  `stax-api-key-sandbox` with a real Stax sandbox key. No Stax call may be made
  until then. The key in `finance-kv` is the live key and stays untouched.
- ~~Confirm in writing, per question: Q1, Q2, Q3, Q5, Q6.~~ **Done 2026-09-29**,
  together with Q13, Q21, Q22, Q24, Q26 and Q28. See `docs/SCOPE.md` §9 Round 3.
- Export the sheet as a spreadsheet file with cell notes into
  `docs/inputs/private/`.
- ~~Re-confirm explicitly: Q21, Q22, Q24, Q26, Q28.~~ Done, see above. They are recorded from the
  owner's single reply "yes for the first part" on 2026-09-27, not from a
  per-question answer. Numbers are the session's numbering in
  `docs/COLUMN_MAPPING.md`; `ANSWERS_ALL_25.md` numbers the same questions
  17, 18, 20, 21 and 23.
- Go-live blockers: payment channel for 453 leases, Stax ACH per-transaction
  limit above $12,581 and the daily cap, sheet version history check,
  TODO-VERIFY-019, and the ACH authorisation point in ADR-0005. Operator emails
  are no longer a blocker; they are a receipts gap.
- Checkpoint 3 on staging (own MFA enrolment, viewer destructive attempt, admin
  list) is no longer blocked by defect 1, but **no user exists on staging yet**.
  Creating the first admin is an owner-approved action still to be planned.

## Follow-ups for later prompts

- Redis replacement: Azure Cache for Redis is retired for new instances.
  Decision due at Step 10. `deployRedis` is off.
- Document the ID-based GitHub OIDC subject in `infra/README.md`.
- Pin CI actions to commit SHAs.
- `README.md` status section is stale: it says nothing is deployed.

## Before Prompt 4

The amendments file requires one change ahead of Prompt 4: scheduled invoices
are MVP scope, not backlog. That is recorded in ADR-0004. Prompt 4 must add
`stores`, `operators`, `leases`, `lease_rent_history` and `rent_charges`, plus
the `pending` and `returned` payment states. Since the owner statements of
2026-09-29 it must also add `charge_notes`, `charge_adjustments`,
`insurance_premiums` and `insurance_charges`. The sign convention is confirmed.

**Owner, in writing, 2026-09-29:** "confirmed: defects 1 and 2 are fixed on staging. I opened
the login page myself." The condition for starting Prompt 4 is met.

## Update, 2026-09-29 evening

- **Stax sandbox key:** the owner states a real sandbox key was stored in the staging vault.
  Metadata shows a second version of `stax-api-key-sandbox` created 2026-09-29 19:00 UTC.
  The value was not read. The app picks it up with the next revision.
- **Refused-host logging:** fixed under PLAN-003b, QA verdict PASS
  (`docs/qa/QA-003b-refused-host-logging.md`). Not on staging until the next push.
- **First staging admin:** proposal in `docs/qa/PLAN-003c-first-staging-admin.md`, awaiting
  owner approval.

## Update, 2026-09-29 night

- **Deployed at commit `2d5c1e6`:** new revision healthy, health endpoint reports staging and
  sandbox, login 200. A request to the default Azure hostname now writes one WARNING line.
  The revision started after the new Stax sandbox key version was stored, so it holds the
  current version. No code calls Stax yet; the key itself is first exercised in Prompt 5.
- **First admin:** plan 003c approved by the owner, implemented, QA verdict PASS
  (`docs/qa/QA-003c-first-staging-admin.md`). Needs a push, then the owner runs the command
  in `infra/README.md` §12.

## Update, 2026-09-29, owner confirmation

- **Deployed at commit `411e412`**, which contains `bootstrap_admin`. Revision healthy.
- **Owner, in writing:** "First staging admin created and MFA enrolled. Checkpoint 3 on
  staging: I signed in with password + authenticator myself. Confirmed."
- Checkpoint 3 items on staging:

| Item | State |
|---|---|
| Owner's own MFA enrolment and sign-in | Confirmed by the owner |
| A viewer attempts a destructive action and is refused | Confirmed by the owner in writing, 2026-09-29 ("both confirmed") |
| Owner has reviewed the list of admin users | Confirmed by the owner in writing, 2026-09-29 ("both confirmed") |

- `bootstrap_admin` now refuses on staging with exit status 2, because an admin exists.

- **Observed:** the "First admin created" warning is not in the container log. A command run
  through an exec session writes to that session, not to the container's own output. The
  creation is therefore evidenced by the owner's statement and the user row, not by a log
  line. A durable audit record for such events belongs with the audit log in a later prompt.
- **Checkpoint 3 on staging is complete**: all three items confirmed by the owner.

## Update, 2026-09-29, Prompt 4

- PLAN-004 implemented and QA verdict PASS (`docs/qa/QA-004-schema.md`), **not pushed**.
  The verdict is conditional on the owner accepting decisions D2 to D12 in
  `docs/qa/PLAN-004-schema.md` §3.
- `docs/DATA_OWNERSHIP.md` is written for the owner's Checkpoint 4 review.
- **Owner, in writing, 2026-09-29:** "accept D2 and D12". D2 (`customers` built as
  `operators`) and D12 (deposit and webhook tables keep their provider ID columns) are
  accepted. D3 to D11 are open. No push approval has been given for PLAN-004.

## Update, 2026-09-30, Prompt 4 on staging

- **Owner typed "push" on 2026-09-29.** `main` pushed to commit `5e66026`. The first deploy
  attempt failed while pulling the Python base image (a registry 502, outside our code); the
  failed job was re-run for the same commit and succeeded.
- Revision `finance-staging-web--0000006` is healthy on that image. Health endpoint 200,
  login 200, admin without a session 401. Gunicorn starts only after `deploy_migrate` exits 0,
  so the 23 new tables and the PostgreSQL triggers are applied on staging. All tables are
  empty; no demo data is loaded there.
- **D3 to D11 remain proposals** awaiting the owner's word. The schema is reversible while
  the tables are empty (`migrate ledger zero`, `migrate payments zero`).
- Checkpoint 4 items for the owner: read `docs/DATA_OWNERSHIP.md`; decide the invoice number
  format; confirm the two behaviours in its §6; test the card guard by hand.

## Update, 2026-09-30, Checkpoint 4 complete

- **Owner, in writing:** Checkpoint 4 complete. `docs/DATA_OWNERSHIP.md` read line by line.
  Invoice number format `INV-YYYY-NNNN` with old numbers carried forward; collection stops
  on a Stax total mismatch until staff resolve it; an unknown Stax-method payment is flagged,
  never deleted. Card guard: **corrected**, no browser test was possible because the new tables are not in
  the admin panel yet; Fable ran both attempts inside the staging container at the owner's
  request, both refused, messages recorded in `docs/DATA_OWNERSHIP.md` §6. Admin panel
  exposure of the new tables stays in Prompts 6 and 7.
- **Owner notes for the record:** receipts go to the email stored in Stax, so an email
  mismatch flag tells staff to fix it in Stax; a database-level guard for bank routing and
  account patterns is added to the **Prompt 14** security scope.
- Open from this: where the old invoice numbers live (the Rents tab has none); needed before
  Prompt 13. D3 to D11 still await the owner's word.

## Update, 2026-09-30, Checkpoint 4 closed

- **Owner, in writing:** "guard test confirmed, accept D2 TO D12 and push". Checkpoint 4 is
  closed. PLAN-004 decisions D2 to D12 are all accepted; the schema on staging is final for
  Prompt 5 to build on.
- Still open before Prompt 13: where the old invoice numbers live.

## Update, 2026-09-30, Prompt 5

- PLAN-005 implemented, QA verdict PASS (`docs/qa/QA-005-stax-client.md`), **not pushed**.
  No call to Stax has been made. Owner steps after the push: run `provider_ping` on staging,
  create the Sentry project and alert rule, send the whitelisting questions in
  `docs/STAX_CLIENT.md` §9 to Stax.

## Update, 2026-09-30, Prompt 5 on staging and the sandbox key verified

- **Owner typed "push"**; `main` at `9b68d63` deployed, revision healthy, login 200.
- **Owner instructed "run ping".** `provider_ping` on staging: `GET /self` returned 200 in one
  attempt; provider `stax`, environment `sandbox`, merchant `ea82…58f4`, expected merchant not
  set. No retry was needed.
- **Written evidence that the key belongs to the sandbox account** (read-only probe of the
  same response; the key was never printed):

| Field in `GET /self` | Value |
|---|---|
| `merchant.company_name` | `Global Retail Group` |
| `merchant.is_sandbox` | `True` |
| `merchant.status` | `ACTIVE` |
| `merchant.is_payfac` | `True` |
| `user.is_api_key` | `True` |
| `user.team_role` | `admin` |

  The manager's statement: the sandbox account is "Global retail group" and the live company
  account is "Global Retail Group LLP". The response names the former and carries
  `is_sandbox = True`, so the key in the staging vault belongs to the sandbox.
- Recommended owner action: set `STAX_EXPECTED_MERCHANT_ID` on staging to the merchant ID
  shown in full in the Stax dashboard, so every future ping also checks identity.
- TODO-VERIFY-005 and 024 closed by this observation.


## Update, 2026-09-30, Prompt 6

- PLAN-006 implemented, QA verdict PASS (`docs/qa/QA-006-operators.md`), **not pushed**.
- **Observed on the sandbox account (`GET /self`, 2026-09-30): `merchant.allow_ach = False`.**
  The sandbox account may not be able to run ACH debits at all. Not a Prompt 6 blocker
  (customers only), but Prompt 7's monthly run and the one-off-invoice test (TODO-VERIFY-019)
  cannot be exercised in the sandbox until Stax enables ACH there. Owner to raise with Stax.
- Interim: the staging web app becomes the single Stax caller (one replica, one process)
  until Step 10; set in the template and the image.
- Owner requests recorded: Phase 7 items (a) document upload, (b) templated outbound email,
  (c) DocuSign API; tables `documents` and `outbound_messages` reserved.

## Update, 2026-09-30, Prompt 6 on staging

- Owner typed "push" and "deploy". `main` at `1b5093a` deployed; then the staging template
  deployed (`staging-plan006-caller`, succeeded, no failed operations) and the revision was
  restarted for the rotated database secret (defect 3, unchanged).
- Revision `finance-staging-web--0000011`: `STAX_CALLER=worker`, `maxReplicas=1`,
  `STAX_EXPECTED_MERCHANT_ID` set to the sandbox merchant. Start-up log: migrations up to
  date, `GET /self` 200, **provider environment check: ok**, one gthread worker.
- Health 200, login 200, `/operators/` without a session 401.
- **Checkpoint 6 is now the owner's:** create an operator with an email, check the sandbox
  dashboard for exactly one customer, double-click save, try a malformed email. Fable reads
  the transport log afterwards.

## Update, 2026-10-01, Checkpoint 6 and PLAN-006a

- **Checkpoint 6, owner in writing:** test 2 (double click: one record, linked) and test 3
  (malformed email refused, no record) passed. Fable confirmed from the transport log that no
  request left for the malformed email, and by a read-only API lookup that each of the three
  created operators has exactly one Stax customer, with matching IDs. Test 1's first attempt
  returned 422 from `POST /customer` (email at the reserved `example.com` domain; hypothesis,
  body not logged); five further attempts within a minute opened the breaker at 20:34:01 UTC,
  which closed itself at 20:39:01. Dashboard duplicate check deferred by the owner to
  2026-10-01 (no sandbox dashboard access yet).
- **No-email rule, owner in writing:** "confirm. Allowed, complete record, not enrolled until
  staff link the IDs."
- **PLAN-006a** (owner's option A) implemented, QA PASS (`docs/qa/QA-006a-sync-diagnostics.md`),
  **not pushed**: 4xx field names in the log, 60-second retry lock, post-redirect-get on every
  operator POST, created ID in the create log line.
- Owner will edit the failed operator's email to a non-reserved domain and retry once.

## Update, 2026-10-01, cause of the 422 found by the owner

- **Owner, in writing:** the failed operator is now linked. Cause: an **incomplete phone
  number**. A non-reserved email alone still gave 422; adding the company name alone still
  gave 422; completing the phone number succeeded first time. The `example.com` hypothesis was
  wrong; the six failures that opened the breaker at 20:34 UTC most likely had the same cause.
  Three more failed requests were spent on the retries. Checkpoint 6 items 2 and 3 confirmed;
  item 1 (dashboard) is for 2026-10-01.
- The specification (`/reference/create-customer.md`, read 2026-10-01) states the phone rule
  `/[0-9]{10,15}/`, 255-character caps on names and email, and the error shape
  `{"<field>": ["<message>"]}`.
- **PLAN-006b** implemented, QA PASS (`docs/qa/QA-006b-customer-field-rules.md`), **not pushed**:
  the phone rule and the caps are enforced in the form and again before any request leaves;
  empty optional fields are no longer sent; stored phones normalised by migration 0006, bad
  ones flagged for review.


## Update, 2026-10-01, PLAN-006a and 006b on staging

- Owner typed "push". `main` at `518dfb0` deployed; pipeline green. Revision
  `finance-staging-web--0000012` healthy: migrations 0005 and 0006 applied at start,
  `GET /self` ok, environment check ok, one gthread worker. Health 200, login 200.
- Checkpoint 6 remaining: item 1, the sandbox dashboard duplicate check, when the owner has
  dashboard access.

## Update, 2026-10-01, Prompt 7

- PLAN-007 (7a lease setup and autopay enrolment, 7b monthly rent run, insurance charge
  generation) implemented, QA PASS (`docs/qa/QA-007-leases-and-rent-run.md`), **not pushed**.
- Spec facts settled on 2026-10-01: hosted-page `url` value; `meta.memo` / `meta.reference` /
  `meta.internalMemo` on invoices; payment-method list fields; schedule pause and resume.
  New open items TODO-VERIFY-035 to 038.
- One test failed once in twelve full runs and was not identified; carried as an open MINOR.
- **Sandbox ACH is still reported disabled.** The first real enrolment on staging is expected
  to stop at the schedule create with field names until Stax enables ACH. What to ask Stax:
  enable ACH (bank payment methods and ACH scheduled invoices) on sandbox merchant
  `ea824f2b-90b3-4c49-8f69-ba06943458f4` ("Global Retail Group"), and confirm the ACH
  per-transaction limit on the live account exceeds $12,581 (Q25).


## Update, 2026-10-01, Prompt 7 on staging

- Owner typed "push". `main` at `f96887a` deployed; pipeline green (the unidentified flake
  did not recur in CI). Revision `finance-staging-web--0000013` healthy: migration 0007
  applied, environment check ok, one gthread worker. Health 200, login 200, `/leases/` and
  `/run/` refuse anonymous requests with 401.
- Checkpoint 7 on staging waits on Stax enabling ACH in the sandbox. Steps once it is on:
  link a bank method in the Stax dashboard, enable autopay on one lease in the app, confirm
  one schedule, run "Match invoices" after the first debit day.

## Update, 2026-10-01, owner's three questions and the "flake" found

- **Re-enable after a rent change** now deletes the old Stax schedule (DELETE, 404 = gone)
  before any create; any other refusal creates nothing. Tested. **Enrolment page** shows the
  operator's receipts email; live operators need a real address before enrolment
  (`docs/STAX_CLIENT.md` §5b). **DTSTART at 12:00 UTC:** the spec is silent on run time; ACH
  batches at 8 pm Eastern; left as is.
- The one failure in twelve runs was **not a flake**: the routing-number check did not strip
  UUIDs, so about one in 6,400 writes carrying a UUID was refused. Fixed in
  `ledger/validators.py` with regression tests; closed in QA-007.


## Update, 2026-10-01, store page (PLAN-007b)

- Owner: nothing in Prompt 7 could be exercised on staging because no store existed and no
  store page did. PLAN-007b adds the store page (list, create, edit; open date required on the
  form), registers Store, Operator and Lease read-only in the admin with view permission for
  the admin group, and confirms the second debit day renders empty. QA PASS
  (`docs/qa/QA-007b-stores.md`), **not pushed**.

## Update, 2026-10-01, store page on staging

- Owner typed "push". `main` at `a9e40d4` deployed; revision `finance-staging-web--0000015`
  healthy; migration 0008 applied; environment check ok. `/stores/`, `/leases/`, `/run/`
  refuse anonymous requests with 401; login 200.
- Owner's staging walk-through can start: store, lease, rent change, run page, then "Enable
  autopay" expecting the ACH refusal with field names.

## Update, 2026-10-01, Prompt 7 walk-through and PLAN-007c

- **Owner's staging walk-through (in writing):** store created; lease created (day 1, ACH,
  $2,000); "Second debit day" was empty on a fresh form, the earlier "1" was a browser hint;
  rent changed to $2,100 with history shown; run page listed the lease under day 1 with the
  "Large debit" tag and the 2026 insurance charge with "Premium missing"; "Create today's
  charges" twice: 1 created, then 1 already created; "Match invoices": 0 schedules; "Enable
  autopay" refused by the large-debit hard block at $2,100; at $1,500 the page asked for a
  bank method; **the Stax sandbox dashboard cannot add a bank account: "Could not tokenize
  payment method: no tokenization gateway found".** Enrolment stays blocked on Stax; the
  request to Stax now includes this and the merchant ID.
- **PLAN-007c** (owner change request): the warning threshold no longer blocks; an admin
  confirms explicitly and the confirmation is audited; `STAX_MAX_TXN_AMOUNT` (new, per
  environment, empty until Stax answers Q25) is the confirmed limit above which enabling is
  hard-blocked. QA PASS (`docs/qa/QA-007c-large-debit-confirmation.md`), **not pushed**.


## Update, 2026-10-01, PLAN-007c on staging

- Owner typed "push". `main` at `04a51a1` deployed; revision healthy; environment check ok.
  The large-debit confirmation is live; `STAX_MAX_TXN_AMOUNT` is unset (unknown) on staging.
- Prompt 8 can be built and tested on the sandbox without ACH (customer, invoice and schedule
  webhook events); transaction, settlement and deposit events wait on Stax enabling ACH.

## Update, 2026-10-01, Checkpoint 6 closed

- **Owner, in writing:** "Checkpoint 6 closed: Stax sandbox dashboard shows exactly four active
  customers, one per operator, emails matching ours. No duplicates." This completes item 1
  (dashboard duplicate check); items 2 and 3 were confirmed on 2026-09-30, and the no-email
  rule was confirmed in writing. Checkpoint 6 is complete.

## Update, 2026-10-01, Prompt 8

- PLAN-008 (webhook receiver, processor, state machine, returns queue, registration) implemented,
  QA PASS (`docs/qa/QA-008-webhooks.md`), **not pushed**. One MAJOR (token in Sentry breadcrumbs)
  found by Opus and fixed before the gate. `docs/RUNBOOK.md` written.
- After the push: `register_webhooks` runs inside the staging container with the owner's word;
  then the owner's four clicks (operator edit → `update_customer` event; dashboard invoice →
  invoice events; replay; wrong token 404).


## Update, 2026-10-02, Prompt 8 on staging

- Owner typed "push". `main` at `46fcf80` deployed; revision `finance-staging-web--0000017`
  healthy; migrations `payments.0003` and `ledger.0009` applied; environment check ok.
- Receiver probed from outside: POST with a wrong token → 404, empty body; GET → 404; the
  path with no token → 404. The log shows the refusal lines with `[token]` in place of the
  path segment and no token value. Nothing stored (no row can exist before registration).
- Waiting for the owner's "register" before `register_webhooks` runs in the container.

## Update, 2026-10-02, webhooks registered on staging

- Owner typed "register". `register_webhooks` ran inside revision `finance-staging-web--0000017`:
  `GET /webhook` then fifteen `POST /webhook`, all 200, paced by the limiter. Registrations: 15,
  created: 15; IDs stored in `external_refs` (`webhook_registration`). Event list as in
  `docs/RUNBOOK.md` §2. The owner's four click paths are next.

## Update, 2026-10-02, Sentry wired on staging

- Owner created the Sentry project `finance-staging` (alert: level fatal emails the owner) and
  stored `sentry-dsn` in `finance-sandbox-kv`. Owner typed "push" and "deploy". Template
  deployment `staging-sentry-dsn` succeeded; the live revision `finance-staging-web--0000019`
  has `SENTRY_DSN` as a vault reference. The breaker and dead-letter alerts now reach Sentry.
- A one-off test event (level fatal) from inside the container awaits the owner's word.
- **Owner, in writing (2026-10-02):** the test event's alert email arrived; the Sentry event has
  no Request section, only tags, runtime and `sys.argv`, and no token or key anywhere.
  **Sentry is proven on staging.** Breaker and dead-letter alerts now page the owner.

## Update, 2026-10-02, Prompt 9

- PLAN-009 (payments ledger, Stax marking of manual payments, carried balance, refunds and voids)
  implemented, QA PASS (`docs/qa/QA-009-payments-ledger.md`), **not pushed**.
- Owner decision needed: a **void** marks the original payment voided rather than adding a row
  (amendment 4 said new rows for refunds and voids; a void is not a new transaction at Stax).
- TODO-VERIFY-016 closed; 028 (`apply_balance` unit) keeps partial payments local-only.

- **Owner, in writing (2026-10-02): "voids as built."** A void marks the original payment voided;
  refunds are new rows linked to their parent. Amendment 4 is read that way from here on.

## Update, 2026-10-02, Prompt 9 on staging

- Owner typed "push". `main` at `0725f8f`. The pipeline's deploy step reported an Azure
  InternalServerError from `az containerapp update`, yet Azure created and activated revision
  `finance-staging-web--0000021` on the new image: healthy, 100 % traffic, migration 0010 applied,
  environment check ok. The CI job is marked failed although the deployment took effect; no
  re-run was needed. Health 200, login 200, `/payments/` without a session 401.

## Carried into the next plan (owner request, 2026-10-02)

- **CI verification step after `az containerapp update`:** poll until the active revision's
  image tag equals the commit SHA and its health is Healthy and Running; pass the job on that
  evidence even when the update command itself returned a transient Azure error; fail the job
  when the revision is not live within the wait. Not urgent; goes into the next plan that
  touches `.github/workflows/ci.yml`.

## Update, 2026-10-02, Checkpoint 8 click 1 and PLAN-006c

- **Owner, in writing:** editing operator fatih's phone did not reach Stax; the Webhooks page
  stayed empty. Fable's checks: (a) the edit path made no provider call (a Prompt 6 deviation
  Fable had accepted in QA-006, wrongly); (b) no Stax call in the transport log at 16:54 UTC;
  (c) the operator showed `linked`, no failure, phone changed locally only.
- **PLAN-006c** fixes it: an edit of a linked operator updates the Stax customer through the
  same field rules as create, shows the outcome, and offers the single manual retry on failure.
  One MAJOR (email validated as required in the adapter) found and fixed. QA PASS
  (`docs/qa/QA-006c-operator-update-sync.md`), **not pushed**.


## Update, 2026-10-02, PLAN-006c on staging

- Owner typed "push". `main` at `ab8d075` deployed; the live revision runs that image tag,
  healthy; environment check ok. Click 1 of Checkpoint 8 can be repeated.

## Update, 2026-10-02, PLAN-008a (owner findings from the clicks)

- `send_invoice` events were `ignored: no object id`: the body is an email envelope with the
  invoice under `mergeVars.invoice`. The receiver now reads nested IDs and records the source;
  the handler re-fetches and derives `sent_at` from the event time (the invoice object has no
  timestamp keys). A dashboard invoice can now be linked to a charge: automatically on
  `create_invoice` when exactly one open charge of the operator matches total and period,
  otherwise by hand from the charge page ("Link invoice", billing or admin, confirmation and
  note when the total differs). QA PASS (`docs/qa/QA-008a-send-invoice-and-manual-link.md`),
  **not pushed**. TODO-VERIFY-015 and 042 (invoices) closed from the stored bodies.

- **Owner, in writing (2026-10-02): Checkpoint 8 click 1 passed** on revision 0000022: phone
  edit saved, "Provider customer updated" shown, new phone in the Stax dashboard, an
  `update_customer` row on the Webhooks page with the event name from the header, and the
  email still present in Stax after the update (the update carried the email; TODO-VERIFY-056,
  about omitting it, stays open).

## Update, 2026-10-02, PLAN-008a on staging

- Owner typed "push". `main` at `8fc2521` deployed; the live revision runs that image tag,
  healthy, 100 % traffic; migrations `payments.0004` and `ledger.0011` applied; environment
  check ok.

## Update, 2026-10-02, owner clicks on 0000023 and PLAN-009a

- **Owner, in writing:** Link invoice worked without confirmation. The replayed `send_invoice`
  still read "ignored: no object id". The wire payment was recorded locally but the Stax mark
  failed: `ProviderRejected (fields: email_receipt)`. The owner then marked that invoice paid by
  hand in the Stax dashboard and will run the memo test on a fresh charge.
- **PLAN-009a:** booleans in query strings are now `0`/`1` (Stax rejected `false`); a replay
  re-reads the object ID from the stored body. QA PASS (`docs/qa/QA-009a-query-booleans-and-replay.md`),
  **not pushed**. After the deploy: Retry on the first charge should find the invoice already
  paid in Stax and send nothing (mismatch recorded); the memo test runs on a fresh charge.


## Update, 2026-10-02, PLAN-009a on staging

- Owner typed "push". `main` at `938608b` deployed; the live revision runs that
  image tag, healthy; environment check ok. Owner's next clicks: Retry on the hand-marked charge
  (expect "already paid" with no call), the memo test on a fresh charge, replay of the old
  `send_invoice` event.

## Update, 2026-10-02, Checkpoints 8 and 9 closed except the ACH-blocked items

- **Owner, in writing:** on revision 0000024 (1) Retry on the hand-marked charge: "Provider
  invoice already showed paid before marking: not marked again. Check for a double payment."
  Nothing sent. (2) Fresh charge for operator halid: the dashboard invoice auto-linked,
  `create_invoice` and `send_invoice` processed, wire payment recorded, Stax shows the invoice
  paid with memo "Paid by wire on 10/02/2026. test wire two"; the resulting `create_transaction`
  event was ignored as not ACH (a cash-marked payment), which is right. (3) Replay of the old
  `send_invoice` event: processed, object ID from `mergeVars.invoice.id`.
- TODO-VERIFY-058 answered in practice: `email_receipt=0` accepted. TODO-VERIFY-055 answered:
  the paid status string is `PAID`.
- **Checkpoints 8 and 9 closed** except the items blocked on Stax ACH: transaction, settlement,
  return, refund, void and deposit events; refunds and voids of real debits; the
  "pay half, refund the half, pay in full" matrix row.

## Update, 2026-10-02, PLAN-010 (worker, database queue, reconciliation) — QA PASS, not pushed

- **Built:** the `jobs` table and `worker` command (the single Stax caller, container app
  `finance-staging-worker`, one replica, `STAX_CALLER=worker`); every screen action that
  reached Stax now queues a job and shows its state; the web app returns to `STAX_CALLER=none`
  and two replicas; the nightly reconciliation (`reconciliation_runs`, `reconciliation_mismatches`,
  `/reconciliation/` with Run now and Resolve); the scheduled Container Apps Job
  `finance-staging-reconcile` (cron 06:00/07:00 UTC, local gate 02:00 New York) **queues** the run
  for the worker; `age_pending_payment` fixture command; six D12 settings; CI updates the worker
  and the job image and verifies all three are live on the commit. ADR-0006: no Celery, no Redis.
- QA report `docs/qa/QA-010-worker-and-reconciliation.md`: PASS; 2,769 tests on PostgreSQL;
  one MAJOR (job as second caller) resolved by amending D5; one MINOR deferred in writing
  (invisible-character notes on the older mandatory notes).
- **Rollout order (two owner words):** first "push" (CI builds the image, rolls the web app,
  applies migrations 0005–0007; the worker and job updates fail harmlessly because the apps do
  not exist yet, so that one CI run ends red at the verification step), then "deploy" (Bicep with
  `webImage` = the new image: creates the worker app and the scheduled job, sets the web app to
  `none` / 2 replicas). Between the two, staff actions queue and wait; nothing is lost.
- Owner's done-when after the deploy: an operator edit reaches Stax within a minute; Run now
  shows counts; `age_pending_payment` + Run now sends the Sentry fatal email; the scheduled
  run row appears the next morning (two overnight runs for Checkpoint 10).

## Update, 2026-10-03, PLAN-010 on staging

- Owner typed "push": `main` at `3030829`; the web revision `finance-staging-web--0000026`
  went live on that image with migrations 0005–0007 applied; that CI run ended red at the new
  verification step because the worker app did not exist yet (expected, documented above).
- Owner typed "deploy": template deployment `staging-plan-010` succeeded (3 min 12 s, no failed
  operations). Now live: web `finance-staging-web--0000027` (`STAX_CALLER=none`, max 2
  replicas, healthz 200); worker `finance-staging-worker` (one replica, `STAX_CALLER=worker`,
  grace 60 s, environment check ok, "Worker started" 01:22 UTC, image tag `3030829`);
  scheduled job `finance-staging-reconcile` (cron `0 6,7 * * *`, `STAX_CALLER=none`,
  `reconcile --scheduled`, timeout 300, no retries). No job had queued between the two steps.
- Next: the owner's done-when clicks (operator edit through the worker; Run now;
  `age_pending_payment` then Run now for the Sentry email); the first scheduled run is expected
  at 06:00 UTC on 2026-10-03 (02:00 New York); Checkpoint 10 wants two overnight runs. The CI
  verification goes green on the next push, when all three resources exist.

## Update, 2026-10-04, Checkpoint 10 closed except the Sentry fixture proof

- **Owner, in writing:** operator edit through the worker: Queued then Done within a minute, the
  new phone visible in the Stax dashboard. Run now: Done, step f, 5 requests. Scheduled runs: one
  row per night on 2026-10-03 and 2026-10-04 at 02:00 EDT, both Done; the 07:00 UTC firing made
  no second row. The `age_pending_payment` + Sentry proof waits for a pending ACH payment (Stax
  ACH still blocked). **Checkpoint 10 closed** except that item.
- Next: Prompt 10b (setup fees, store costs, deposits, P&L), owner-requested, application only.

## Update, 2026-10-05, PLAN-010b (setup fees, store costs, deposits, P&L) — QA PASS, not pushed

- Built in three batches plus two rework cycles: fee catalogue and per-lease setup charges with
  the lease-creation checklist and their own pages; deposits as liabilities with refund/withhold/
  transfer at lease end (plus the new **End lease** action); date-effective store costs with the
  template importer (`import_store_costs`, `.csv`/`.xlsx`, idempotent, dry run); premium cost;
  `/reports/pnl/` with CSV. QA report `docs/qa/QA-010b-setup-fees-store-costs-pnl.md`: PASS,
  3,232 tests on PostgreSQL; new dependency `openpyxl==3.1.5`.
- **Owner to confirm (ADR-0007 §3):** `first_year_insurance` is seeded inactive because year one
  is already billed by the lease's first insurance charge; activate it only with a rule change.
- Rollout: one "push" (CI deploys; migrations ledger 0012–0014 and payments 0008–0009 apply at
  web start; no infra change). Then the owner's six done-when clicks (RUNBOOK §11; the two-row
  import runs inside the web container against `backend/tests/fixtures/store_costs/two_rows.xlsx`,
  after stores 1796 and 437 exist with those numbers).

## Update, 2026-10-05, PLAN-010b pushed

- **Owner, in writing:** "first_year_insurance stays inactive, as built." ADR-0007 §3 confirmed.
- Owner typed "push".
- The CI run on `bd34846` hung in the unit tests and was cancelled at the 30-minute limit;
  **nothing was deployed**; staging still runs `3030829`. Cause, reproduced in a one-CPU Linux
  container: PostgreSQL planned the P&L performance test's query from statistics that said the
  tables were empty (autovacuum had analysed them earlier in the session; the test's rows were
  uncommitted), giving nested sequential scans. Fix in the test helper (`ANALYZE` after loading);
  20 clean rounds where it hung one in three; full gate re-run green. The CI unit-test step now
  dumps stacks after five minutes and stops after fifteen. One MINOR deferred: no statement
  timeout on the report's queries (QA-010b addendum). **Needs a new "push".**
- **Owner, in writing (2026-10-05):** "The statement-timeout and post-import ANALYZE item must
  land before Prompt 13, since the migration is exactly the bulk import that would trigger it.
  Put it in the 10c or 11 plan, whichever comes first." **Binding carry-over** for the next
  plan: `statement_timeout` around the P&L queries with a value-free message, and `ANALYZE` of
  the affected tables at the end of every bulk importer (`import_store_costs` and the Step 13
  import). Owner typed "push" for `8959df1`.

## Update, 2026-10-05, PLAN-010b on staging

- `main` at `faef3b9`. CI run 37356571164 green: unit tests in 4 min 42 s (no hang), deploy, and
  the verification step ("web and worker run this commit, Healthy and Running; the reconcile job
  uses it"). `/healthz` 200. Migrations ledger 0012–0014 and payments 0008–0009 applied at web
  start (the revision is healthy; the start command fails otherwise).
- Fable's local Azure CLI session expired after the push, so the start logs were not read from
  this machine; the CI verification is the evidence. The owner signs in again (`az login`) when
  the next Azure read is needed; no model does it.
- Next: the owner's six done-when clicks (RUNBOOK §11). Binding carry-over for plan 10c or 11:
  statement timeout on the P&L queries and ANALYZE after bulk imports, before Prompt 13.

## Update, 2026-10-05, store-cost import proven on staging

- **Owner, in writing:** signed in to Azure with MFA; stores 1796 and 437 exist; "Run the three
  imports now and report the counts." Run by Fable inside `finance-staging-web--0000028` against
  `backend/tests/fixtures/store_costs/two_rows.xlsx`:
  - dry run: read 2, not filled 0, unchanged 0, created 2, updated 0, refused 0 (nothing written)
  - real run 1: read 2, not filled 0, unchanged 0, created 2, updated 0, refused 0
  - real run 2: read 2, not filled 0, unchanged 2, created 0, updated 0, refused 0
- Done-when item 4 of Prompt 10b is met (import creates the costs; a second run changes
  nothing). The owner checks the two store pages for the landlord rents. Items 1–3, 5 and 6 are
  the owner's clicks.

## Update, 2026-10-05, Prompt 10b closed on staging

- **Owner, in writing:** stores 1796 and 437 show the imported values. All 10b clicks passed: the
  checklist created the sign fee and the deposit; landlord rent set and the margin shown; the wire
  payment on the sign fee shows paid; the P&L for October alone shows landlord rent 1,500.00 and
  billed margin −1,000.00 as expected, and a July–October range shows "unknown" because no cost
  row covers July–September; End lease, deposit payment, then Resolve deposit → refunded produced
  a `refund_deposit` row and the P&L did not move. **Prompt 10b closed.**
- **Owner finding:** store cost rows cannot be edited or corrected. Wanted: correct the current
  row's amount or dates by a superseding row (history kept, no in-place overwrite of a past
  period) and close a row with a "to" date; admin only, audited. In the 10c plan, not a separate
  push.
- Next: Prompt 10c (document uploads on stores and leases), with the store-cost correction, the
  binding carry-over (statement timeout on reports, ANALYZE after imports) and the deferred
  invisible-note MINOR.

## Update, 2026-10-05, PLAN-010c (documents, cost corrections, report timeout) — QA PASS, not pushed

- Built: document uploads on stores and leases into a private Blob Storage container by managed
  identity (no keys, no public access, streamed through the app), with the bank-data check on PDF
  and DOCX text including form fields, annotations and properties; store-cost **Correct** and
  **Close** (admin, history kept); the P&L statement timeout and post-import ANALYZE (the owner's
  deadline before Prompt 13 is met); invisible-character notes. QA report
  `docs/qa/QA-010c-documents-and-cost-corrections.md`: PASS, 3,704 tests on PostgreSQL; three
  MAJOR findings resolved (two found by the QA adversarial tests).
- Rollout: "push" (the app boots with the feature off: "Document storage is not configured"),
  then "deploy" (Bicep: storage account, container, role assignment, web env). What-if run:
  only those changes.
- Owner's test files are made locally, not committed: see RUNBOOK §12 and the session message.

## Update, 2026-10-05, PLAN-010c pushed; storage deployment pending

- Owner typed "push". `main` at `3ddc75e`; CI run 37375480391 green (unit tests 4 min 52 s,
  deploy, verification). Web revision `finance-staging-web--0000029` Healthy and Running on that
  image; `deploy_migrate` succeeded (ledger 0015–0016); environment check ok; `/healthz` 200.
- The web app carries no document settings yet, so the Documents sections read "Document
  storage is not configured" until the infrastructure deployment (Publish Request 2, "deploy").
  Store-cost Correct and Close, the report timeout and the note rule are live now.

## Update, 2026-10-05, PLAN-010c storage deployed to staging

- Owner typed "deploy". Template deployment `staging-plan-010c` succeeded (3 min 6 s, no failed
  operations). Verified by metadata only: storage account `stfinancestagingavyxps6f`
  (Standard_LRS, public blob access off, **shared-key access off**, TLS 1.2, HTTPS only);
  versioning on; blob and container soft delete 30 days; container `documents` with no public
  access; one role assignment on the container, Storage Blob Data Contributor to the app's
  identity. Web revision `finance-staging-web--0000030` Healthy and Running with
  `DOCUMENTS_STORAGE_ACCOUNT_URL`, `DOCUMENTS_CONTAINER` and `AZURE_CLIENT_ID`; `/healthz` 200;
  the worker is untouched and running.
- Not yet proven: a real upload through the managed identity (the owner's first click; a new
  role assignment can take a few minutes to take effect).
- Next: the owner's 10c done-when clicks (RUNBOOK §12).

## Update, 2026-10-05, 10c document clicks: evidence from the audit log

- No audit-log screen exists yet (gap in the 10c plan; the owner sets it as Prompt 10d:
  admin-only, read-only `/audit/` with filters by action, user, record and date range, CSV
  export, and an Activity list in each Documents section).
- On the owner's instruction Fable ran a read-only query in the staging web container, twice.
  Rows under the owner's user: two `document.upload_refused` (reason `payment_data`; **owner
  confirmed both were the fake bank PDF, no false positive**), four `document.uploaded` (three
  PDFs/PNGs on a lease and a store, one PNG on a store), two `document.previewed` and two
  `document.downloaded` with `result=ok`. Four live `documents` rows: 2 on the lease, 2 on the
  store. The managed identity reached the private container on the first upload. Nothing was
  written by the queries.

## Update, 2026-10-05, Prompt 10c closed on staging

- **Owner, in writing:** all 10c clicks passed: PDF lease uploaded, previewed and downloaded; a
  floor-plan image on a store likewise; a viewer sees lists and downloads but has no upload box;
  the fake bank PDF was refused twice (the owner's test file, no false positive); the 30 MB file
  was refused; store cost Correct and Close work. Audit rows confirmed by the two read-only
  queries; the audit page goes into 10d. The document kinds on the store were the owner's
  dropdown choices and stay. **Prompt 10c closed.**

## Update, 2026-10-05, PLAN-010d (one-off charges, Charge now, audit page) — QA PASS, not pushed

- Built: `one_off_charges` as the single creation path (setup, arrears, adjustment, damage,
  other) with the Stax invoice created by the worker (our number as the reference, the
  description as the line; email, manual, autopay on the due date by a one-occurrence schedule,
  Charge now); cancel deletes the Stax invoice (Stax shows `DELETED`); Charge now behind
  `STAX_PAY_INVOICE_ENABLED` (off on staging until ACH), one request, admin confirmation; the
  audit page `/audit/` with filters and CSV; Activity lists in the Documents sections. ADR-0009;
  Rule 2 amended. QA report `docs/qa/QA-010d-one-off-charges-and-audit-page.md`: PASS, 3,934 tests
  on PostgreSQL.
- Rollout: one "push" (migrations ledger 0017–0019 and payments 0010–0011 at web start; the data
  migration renames existing setup rows and audit entries). No infrastructure change.
- Owner's done-when: the $3,000 arrears charge for halid by email; the dashboard shows our
  reference; a wire payment marks it paid with the memo; a second charge cancelled shows
  **DELETED** in Stax (there is no "voided" for an invoice); `/audit/` filtered by
  `document.uploaded` shows the owner's uploads and a billing user gets 403.

## Update, 2026-10-06, PLAN-010d push failed at the migration; fixed, awaiting a new push

- Owner typed "push"; `main` at `4b855f9`. CI tests green; the web revision 0000031 failed to
  start: `ledger.0017` was refused by PostgreSQL (`cannot ALTER TABLE "audit_log" because it has
  pending trigger events`); rolled back; **staging web still runs 10c (revision 0000030) on
  `ledger.0016`**. The worker and the reconcile job were rolled onto the new image before the
  web wait and run new code against the old schema until the next deployment (staging only).
- Cause and fix in the QA-010d addendum: migrations split into schema / rows / constraints with
  `SET CONSTRAINTS ALL IMMEDIATE` before the trigger is re-enabled; a seeded PostgreSQL round-trip
  test that reproduces the staging error; CI now waits for the web revision before touching the
  worker and the job. Full gate re-run (table in the addendum). **Needs a new "push".**

## Update, 2026-10-06, PLAN-010d on staging

- Owner typed "push". `main` at `a2b3842`; CI run 37409289582 green with the new order (web
  update → web wait → worker and job → verification). Web revision `finance-staging-web--0000032`
  Healthy and Running on that image; `deploy_migrate` applied ledger 0017–0021 and payments
  0010–0013 over the existing staging rows ("migrations are up to date"); environment check ok;
  `/healthz` 200. Worker revision 0000004 restarted on the commit ("Worker started" 03:42 UTC);
  the reconcile job uses the image. The worker and job are back in step with the schema.
- Next: the owner's 10d done-when clicks (RUNBOOK §13). Reminder: a cancelled charge shows
  `DELETED` in Stax.

## Update, 2026-10-06, PLAN-010d-a: invoice body dates

- **Owner, in writing:** 10d click 1 failed at the Stax call: `ProviderRejected (fields: due_at,
  invoice_date_at)` on `INV-2026-0003`; no invoice at Stax; the owner has not retried.
- Cause: we sent `YYYY-MM-DD`; Stax wants `Y-m-d H:i:s` (spec example `2025-12-19 23:59:59`;
  Stax's own webhook bodies `2026-10-02 20:50:23`). Fixed in `mapping.py`
  (`invoice_date_at` start of day, `due_at` 23:59:59); the 422 pinned through the real transport.
  QA `docs/qa/QA-010d-a-invoice-date-format.md`: PASS. TODO-VERIFY-026 part answered.
- After the deploy the owner presses the single **Retry** on `INV-2026-0003`; the retry first
  looks for an invoice Stax may already hold with that reference (none exists), then creates.

## Update, 2026-10-06, PLAN-010d-a on staging

- Owner typed "push". `main` at `6f153de`; CI run 37414601899 green (web first, then worker and
  job; all three on the commit). Web and worker healthy; `/healthz` 200. The owner now presses
  the single Retry on `INV-2026-0003`.

## Update, 2026-10-06, Prompt 10d closed on staging; waive finding

- **Owner, in writing:** 10d clicks 1–4 passed: the retry created `INV-2026-0003`, SENT, visible in
  the Stax dashboard with our reference and description; wire payment recorded and Stax shows it
  paid with the memo; Cancel on a third charge: Voided here, DELETED in Stax. Audit page: filter
  and CSV work; `viewer1` gets 403 on the URL. **Prompt 10d closed** (Charge now and autopay on
  the due date proven by unit tests until Stax enables ACH).
- **Owner finding (misclick):** Waive on a charge with a live Stax invoice marks the charge
  Waived while the invoice stays open at Stax, which could still collect it. Wanted: Waive must
  delete the provider invoice through the worker (Cancel's path) before marking waived, or refuse
  until cancelled; audit both; make Waive and Cancel explicit on the page; and give the already
  waived `INV-2026-0004` a way to have its Stax invoice deleted. In the Prompt 11 plan.

## Note, 2026-10-06: TIME_ZONE and settlement dates (owner decision wanted, not urgent)

- `TIME_ZONE = "UTC"`: a payment settled at 23:59 New York time is recorded with the next day's
  `settled_on`, so month-boundary figures (paid this month, revenue by month) follow UTC days.
  Changing the project time zone to America/New_York moves ledger dates and needs its own plan;
  deferred by written decision until the owner says which calendar the books keep.

## Update, 2026-10-06, PLAN-011 (dashboard, grid, arrears, reports, exports, mobile) — QA PASS, not pushed

- Built as one plan in three batches: Waive refusal and the "Delete provider invoice" remedy (for
  `INV-2026-0004`); the dashboard as home; the month grid with the sheet's CSV order; arrears;
  the reports menu (aging equal to the dashboard's outstanding by construction, revenue, payments
  by method, deposits vs transactions, printable statement, P&L); CSV on every list with
  accountant-readable headers; five indexes; the August 2026 fixture (392 / $1,470,001.57 / 47 /
  13) reproduced; phone layout, manifest, static-only service worker (executed under Node in QA),
  camera capture, MFA on a 390 px viewport in Playwright. QA report
  `docs/qa/QA-011-dashboard-reports-mobile.md`: PASS, 4,246 tests on PostgreSQL.
- Rollout: one "push" (migration ledger 0022, indexes only). No infrastructure change.
- Deferred by written decision: `TIME_ZONE` and settlement dates (note above); the Google Sheet
  push stays unbuilt (amendment 7).

## Update, 2026-10-06, PLAN-011 on staging

- Owner typed "push". `main` at `aebcd68`; CI run 37509045369 green (web first, then worker and
  job; all three on the commit). Web revision `finance-staging-web--0000034` Healthy and Running;
  migration 0022 applied; environment check ok; `/healthz` 200; `app.css` served; the manifest and
  `/sw.js` answer 401 to a signed-out request (by design) and every page carries
  `Cache-Control: private, no-store`.
- Next: the owner's laptop and phone clicks (RUNBOOK §14), including "Delete provider invoice"
  on `INV-2026-0004`.

## Update, 2026-10-06, Prompt 11 closed on staging

- **Owner, in writing:** all Prompt 11 clicks passed on the laptop and the phone: dashboard, grid
  with filters and CSV, arrears and today's run match the run page, aging total equals
  outstanding, report CSV, Delete provider invoice on `INV-2026-0004` shows DELETED in Stax,
  home-screen install, MFA sign-in, grid scroll and camera upload on the phone. **Prompt 11
  closed.** Usability note for 11b: the grid's months beyond the viewport need a visible
  horizontal scrollbar, a fixed store column, and columns sized so 12 months fit on a laptop.
- **Owner decision (item 7 of 11b):** the project time zone becomes America/New_York (storage
  stays UTC); nothing migrates silently: the rows whose date would change are listed first. Must
  land before Prompt 13.

## Update, 2026-10-06, PLAN-011b (collections, NSF, attention, calendar, New York time) — QA PASS, not pushed

- Built in three batches plus one rework: New York time with `tz_audit`/`tz_apply`; the grid at
  12 months per laptop screen with a visible scrollbar; attention list; contact log; calendar
  with a revocable ICS feed; the Gmail sender by delegated service account with a mandatory
  catch-all outside production; the collections ladder (3/10/20) run by a new daily job; the
  returns queue's Reschedule with the NSF fee (100/50, admin-editable, waivable) as a two-line
  Stax invoice, owed once and paying the original month when it settles; a signed, revocable
  statement link for operators. QA `docs/qa/QA-011b-collections-nsf-attention-calendar.md`:
  PASS, 4,740 tests on PostgreSQL. ADR-0010; RUNBOOK §15; plan §9 holds the owner's Workspace
  steps and the DNS findings (SPF covers Google; DKIM not published; recommended).
- Rollout: "push", then "deploy" with `mailEnabled=false` (daily job created, env added, key
  not required yet). Then Fable runs `tz_audit` on staging (read-only) and shows the list;
  `tz_apply` only on the owner's word. Mail is enabled in a later deployment once the owner has
  done plan §9 and stored the key.
- **Owner, in writing:** the brand folder is theirs (misnamed by Finder); the six Monvera brand
  files now sit in `static/brand/`, **uncommitted by instruction**, for Prompt 12c. Every commit
  until then excludes `static/brand/`.

## Update, 2026-10-06, PLAN-011b pushed; infrastructure deployment pending

- Owner typed "push". `main` at `698e6c4`; CI tests green; web revision
  `finance-staging-web--0000035` Healthy and Running on the commit (`deploy_migrate` applied
  ledger 0023–0027 and payments 0014; environment check ok; `/healthz` 200); worker revision
  0000007 and the reconcile job on the commit. The CI verification ended red because the daily
  job `finance-staging-daily` does not exist until the Bicep deployment (expected; documented
  in the plan). The app now keeps New York days.
- Next: "deploy" with `mailEnabled=false`; then `tz_audit` (read-only) and the owner's clicks.

## Update, 2026-10-06, PLAN-011b deployed; tz_audit clean

- Owner typed "deploy". Template deployment `staging-plan-011b` succeeded (3 min 9 s, no failed
  operations) with `mailEnabled=false`. Verified: job `finance-staging-daily` (cron `0 13 * * *`,
  `STAX_CALLER=none`, `collections_tick`, timeout 300, no retries); web revision
  `finance-staging-web--0000036` Healthy and Running with `MAIL_ENABLED=false`, empty sender and
  catch-all, `COMPANY_NAME`, and **no** Gmail secret reference; worker revision 0000008 healthy;
  `/healthz` 200.
- `tz_audit` run read-only inside the web container: **0 rows to realign** (ACH payments: none
  with a differing date; invoice issue dates all agree under New York); nothing differs for
  other reasons; the "unchanged by design" list printed. `tz_apply` is therefore not needed and
  was not run.
- Next: the owner's 11b clicks (RUNBOOK §15); mail enablement after the owner's Workspace steps
  (plan §9) in a later deployment with `mailEnabled=true gmailSender=… mailCatchAll=…`.

## Update, 2026-10-06, 11b click 1 failed: broken promises hidden; fixed (PLAN-011b-a)

- **Owner, in writing:** three promise entries on operator sena (two past dates, one future) all
  read "Open" and none showed on the attention list. Other clicks: the Oct 20 calendar note
  shows on `/calendar/`; the Google feed is added (confirmation after Google refreshes); the grid
  shows twelve months with a visible scrollbar and a fixed store column; Reschedule waits for
  Stax ACH.
- Cause: the rule required the operator to have an open balance and treated any later payment
  as keeping the promise; sena owes nothing. The list is computed live (not by the daily job).
- Fix: a past, unclosed promise is broken unless a settled, non-refund payment of at least the
  promised amount settled between the entry's recording day and the promised date (New York);
  balance plays no part. Note for the owner: a backdated promise (promised date before the day
  it was recorded) can only be cleared by a later entry; Prompt 12's rule (3) replaces this.
- **Owner findings for the Prompt 12 plan (contact log):** (1) edit by the author for 15 minutes,
  then appended corrections only; admin soft delete with a mandatory reason, shown struck
  through; (2) a store/lease field on entries (required with more than one active lease,
  preselected with one), entries shown on the store page too with an Add entry button, promises
  matched against that lease's balance; (3) a promise closes automatically when a payment at
  least equal to the promised amount settles on that lease on or before the promised date;
  (4) a "Previous promise" outcome (Kept, Rescheduled with new date and amount, Broken) replaces
  the "closes the promise" checkbox; log statuses open, kept, rescheduled, broken.

## Update, 2026-10-07, PLAN-011b-a on staging

- Owner typed "push". `main` at `37f5f9c`; CI run 37552860381 green end to end, including the
  verification of all four resources (web, worker, reconcile job, daily job) on the commit.
  `/healthz` 200. The owner refreshes the dashboard to see sena's two past promises.

## Update, 2026-10-07, Prompt 11b closed

- **Owner, in writing:** the two past promises show as not kept and the future one does not.
  **Prompt 11b closed** (mail enablement, Reschedule on a real return and the Google feed
  confirmation remain as follow-ups outside the step). Next: Prompt 12 with the owner's
  amendments (backlog and provider portability documents; contact-log changes; invoice
  builder; read-only dispute list with evidence upload).

## Update, 2026-10-07, PLAN-012 built and gated (not yet pushed)

- Three batches by Opus, gated by Fable: **A** contact log maturity (edit window, corrections,
  admin soft delete, store on entries, automatic promise close, previous-promise outcome);
  **B** the invoice builder (drafts with lines, preview and print, one Stax invoice with all
  lines and one charge per line, Charge now, cancel; the old form answers 410; sibling
  allocation generalised; deposits held and the returned-then-rescheduled rule read the lines);
  **C** the dispute list with evidence upload (two new provider methods, multipart transport,
  `disputes` and `dispute_evidence`, the operator as a document owner for evidence only).
- Documents: ADR-0011, RUNBOOK §16, DATA_OWNERSHIP, README matrix rows, `docs/CLAUDE.md`
  Rule 2 amendment, the endpoints file, TODO-VERIFY-062/063, QA-012.
- **Owner, in writing (2026-10-07): S1 yes, S2 yes** (recorded in ADR-0011 and BACKLOG §2);
  the columns are a small follow-up plan (PLAN-012a) before Step 13. The freeze statement to
  staff is the owner's.
- Next: the owner's "push"; then the staging clicks of the plan's §8 (store 2476 two-line
  invoice; store 2477 promise; the 15-minute edit; viewer cannot delete; the dispute list
  renders empty).

## Update, 2026-10-07, PLAN-012 on staging; PLAN-012a (S1, S2) built and gated

- Owner typed "push" (with S1 yes, S2 yes). `main` at `8623398`; GitHub's Git Operations incident
  rejected the push three times with an internal error, the fourth attempt went through; CI run
  37642705922 green end to end, four resources verified, `/healthz` ok. The owner's five clicks
  of plan §8 are pending.
- PLAN-012a (one Opus batch, gated by Fable): `invoice_line_items.kind`, `invoices.fees` and
  `invoices.surcharge` with the total CHECK recreated as the sum of the parts; `operators.sms_phone`
  (E.164), `sms_consent_at`, `email_consent_at` on the form, page and CSV; every invoice writer sets
  the new columns explicitly; migration 0031 with a seeded round trip; provider contract unchanged.
  Uncommitted until the gate's report (QA-012a); then its own Publish Request.

## Update, 2026-10-07, Prompt 12 click 1 finding → PLAN-012b

- **Owner, in writing:** the builder's "Fee type" column must be an Item combo box (pick a
  catalogue item or type a new name; no "Free text" value); columns Item, Description (optional
  detail), Quantity, Unit amount; the typed name is the Stax line text and the description its
  detail; an optional "Save as catalogue item" tick on a typed item (admin and billing); fix with
  the next push, the rest of the builder unchanged.
- Plan `docs/qa/PLAN-012b-builder-item-combo.md` written and assigned to Opus; it ships in the
  same push as PLAN-012a (`d94ea1c`), so the pending Publish Request is withdrawn until 012b is
  gated. Note for the owner (plan D4): a catalogue item's charges are category `setup`, as the
  catalogue has meant since 10b; a typed item without the tick is `other`.

## Update, 2026-10-07, PLAN-012b built and gated

- One Opus batch, gated by Fable: `invoice_draft_lines.item` with the fee type resolved by name,
  `invoice_line_items.details`, migration 0032 with a seeded round trip, the Item datalist with
  no "Free text", the Save-as-catalogue-item tick (slug code, reserved NSF and category codes,
  audited, one item under a race), the provider line's `details`. QA-012b. Ships with PLAN-012a in
  one push.

## Update, 2026-10-07, Prompt 12 click 2 finding → PLAN-012c

- **Owner, in writing:** store 2477's page lists the contact log but has no "Add entry" button;
  add it, opening the same form with the store/lease preselected and the operator fixed; same push
  as the builder fix.
- Cause: Batch A showed the button only for a lease in effect today (active or paused, started,
  not ended); 2477's lease is outside that rule. PLAN-012c widens it to any lease of the store with
  a non-archived operator (the current one first, else the most recent) and names a non-current
  lease's status in the form title. Assigned to Opus.

## Update, 2026-10-07, PLAN-012c built and gated

- `contacts.store_contact_lease` (current lease of a non-archived operator, else the most recent);
  the store page's Add entry button and route use it; the store form locks the lease and names a
  non-current lease's state in its title. No migration. QA-012c. Ships with 012a and 012b in one
  push; one Publish Request for the three commits.

## Update, 2026-10-07, Prompt 12 findings 3 and 4 → PLAN-012c-a

- **Owner, in writing (INV-2026-0006, store 2476, Damage Repair $300 + late fee $50):** (3) only
  line 1 is linked to the invoice; the late fee page shows no invoice, no Record a payment, and
  offers Waive; every builder line must be linked, Waive refused while the Stax invoice is live,
  and the staging data repaired; (4) a $300 wire on line 1 flags "partial amount" (fine); recording
  the remaining $50 must mark the invoice paid as cash at Stax with the memo and clear the flag;
  add a line-by-line test. Invoice-level payment entry is Step 12d.
- Plan `docs/qa/PLAN-012c-a-builder-lines-linked-and-paid-line-by-line.md` assigned to Opus: a
  charge's invoices resolved through the line items everywhere; Record a payment on any line; the
  invoice marked once its lines' settled payments cover the total, flags cleared on every line;
  a read-only `invoice_line_links --check` command for the staging verification (the line-item
  links already exist, so no data migration). Same push as 012a, 012b, 012c.

## Update, 2026-10-07, PLAN-012c-a built and gated

- A one-off charge's invoices are resolved through the line items (`invoices_of`,
  `rent_run.charge_invoices`); Record a payment on every line; a builder invoice is marked paid
  as cash once its lines' settled payments cover what it bills, flags cleared on every line; the
  waived-invoice delete remedy refuses a shared invoice; read-only `invoice_line_links --check`.
  No migration. QA-012c-a. Ships with 012a, 012b, 012c in one push.

## Update, 2026-10-07, PLAN-012a, 012b, 012c, 012c-a on staging

- Owner typed "push". `main` at `e0978d5`; the CI run green end to end (tests, deploy, the four
  resources verified); `/healthz` ok. Migrations 0031 and 0032 applied by the deploy. The owner's
  clicks: INV-2026-0006 (the late fee page, the $50 wire, the flags), the builder's Item box on
  store 2476, Add entry on store 2477, then clicks 3 to 5 of PLAN-012. `invoice_line_links
  --check` on staging awaits the owner's authorization (a read-only command in the web container).
- **Staging check (owner-authorized, 2026-10-07):** `python /app/backend/manage.py invoice_line_links
  --check` in the web container printed "Every builder invoice line names its charge." and listed
  nothing: INV-2026-0006's late fee line is linked; no data repair needed.

## Update, 2026-10-07, Prompt 12 finding 5 → PLAN-012c-b

- **Owner, in writing:** (a) "Still open (no change)" as the default outcome so a call can be
  recorded while a promise stands; (b) Kept / Reschedule / Broken buttons on every open promise
  row (store page, operator page, attention list), each a small form that creates the entry
  through the previous-promise path; (c) at most one open promise per lease: a new promise closes
  the open one as Rescheduled and the form says so; sena's three open promises to be reduced; the
  attention list never shows two per lease; (d) "Add entry" on both pages.
- Plan `docs/qa/PLAN-012c-b-promise-outcomes.md` assigned to Opus (data migration 0033 for the
  existing rows). The QA owner's own test of the 11b scenario (three promises on one operator,
  two broken items) is superseded by rule (c) and is rewritten on leases.

## Update, 2026-10-07, PLAN-012c-b built and gated

- "Still open (no change)" default outcome; Kept / Reschedule / Broken on every open promise row
  and attention item, through the previous-promise path (Kept and Broken ask how it was learned:
  call, e-mail or visit, since the log has no remark kind); one open promise per lease with the
  warning and the message; data migration 0033 keeps the newest per bucket and chains `closed_by`
  (one-to-one); `broken_promises` one per bucket; "Add entry" on all three pages. QA-012c-b.
- **Owner, in writing (finding 5e):** an entry that closes a promise shows nothing about it; show
  "Closed $2,000.00 by 2026-10-01 as Broken" with a link to the original promise on the store page,
  the operator page and the CSV. Added to PLAN-012c-b as D5; the Publish Request waits for it.
- D5 built and gated the same day: the closing entry's Promise cell names the closed promise with
  a link (operator page, log page, store page; the store page links to the operator page when the
  promise is elsewhere), CSV column "Closes promise"; query counts unchanged.

## Update, 2026-10-07, push of 012c-b: CI timed out, staging not deployed

- Owner typed "Push". `main` at `dce8a44`; CI run 37702713815 **failed**: the unit-test step hit
  its 15-minute limit (the last green run needed 11 of 15); the deploy was skipped, so staging
  still runs `e0978d5`. Not a hang: the suite has grown slower since 012a, mostly the seeded
  migration round trips (0031, 0032, 0033) and the older rename-migration tests, which together
  take about two minutes on sqlite and several on the CI PostgreSQL service. Local full runs:
  sqlite 8:48, PostgreSQL 15:04.
- Fix (verification-only CI config, Fable): the unit step limit 15 → 25 minutes, the job 30 → 40.
  Follow-up recorded in `docs/BACKLOG.md` §4: make the migration round trips cheaper (one CI step
  of their own, or a cached post-migration database) before the suite grows further.

## Update, 2026-10-08, PLAN-012c-b (finding 5 a–e) on staging

- Owner typed "push" for `81921bf`. CI run 37716995619 green end to end: the unit step took 12:47 of
  its new 25-minute limit, the deploy ran and the four resources were verified; `/healthz` ok.
  Staging runs `81921bf` (code of `dce8a44`): migration 0033 applied, so sena shows one open
  promise. The owner's clicks for finding 5 (a)–(e) and the earlier PLAN-012 clicks are pending.

## Update, 2026-10-13, Checkpoint 12 closed

- **Owner, in writing:** all 14 staging clicks passed (the $50 wire marking INV-2026-0006 paid at
  Stax, the Item box with a catalogue save, Add entry on store 2477, the promise buttons from all
  three pages, the one-open-promise rule, the "Still open" default, the closing-entry sentence and
  CSV column, the 15-minute edit window, viewer read-only, soft delete, the empty Disputes page,
  the SMS fields). **Checkpoint 12 closed**, with 12a and finding 5.
- Next: Step 12b (owner's text of 2026-10-13): lease-end wizard, operator transfer wizard,
  installment plans, proration (4a), deposits excluded from revenue everywhere; Fable plans
  (PLAN-012e), Opus builds in batches, one checkpoint report before the push.

## Update, 2026-10-13, PLAN-012e Batches A and B built and gated (not pushed)

- **A:** proration (one rounding; the owner's example $1,225.81; "Prorate first month" on the
  lease; months after the end never charged), the end-of-lease wizard (terms, blockers incl.
  live rent/insurance invoices and the new **Stop autopay** action, statement with the last month
  reduced/credited/created and after-end months waived or credited, deductions, settlement with
  the deposit split and the unpaid-balance offset counted once, confirm with "are you sure"), the
  PDF through `reportlab==5.0.1` (BSD; transitive `pillow==12.3.0`, MIT-CMU), the statement
  e-mail with an attachment through a seeded template; migrations ledger 0034–0036, payments
  0016–0017.
- **B:** the transfer wizard (operator pick or inline create, blockers incl. open balances per
  charge, the lease-end steps with reason `transfer`, the incoming lease ≥ end + 1 with one
  builder draft for the deposit and setup lines, the hand-over invariant); migration 0037.
- Gate (both batches, final code): ruff, format, mypy, lock, migrations clean; sqlite 4,854
  passed; PostgreSQL 5,458 passed (18:40); e2e 7; secret scans clean; Fable's adversarial file
  16 cases green. **For the owner at the checkpoint:** the proration rounding rule (D1), the new
  dependency, and whether a lease-end balance charge may remain on the outgoing operator after a
  transfer (Opus's choice: yes, as in a plain lease end).

## Update, 2026-10-14, PLAN-012e Batch C built and gated (not pushed)

Step 12b is complete in the working tree: Batch C (installment plans, D9–D12) on top of
`02c9765`. A plan splits what is owed on a rent or one-off charge into 2–12 installments
(equal, the rounding cent on the last; weekly, every two weeks or monthly from a first date;
preview editable before confirm), each an `installment` one-off charge collected as the parent
is; the parent leaves the open balances, aging, arrears and attention while the plan runs and is
marked paid by the plan at completion without a payment row; a returned installment follows the
NSF rule and puts the plan on the attention list; Reschedule (billing) moves the unpaid ones at
Stax through the worker; Cancel (admin, note) voids the rest and returns the remainder to the
parent; the P&L, ledger and grid count once under the parent, each payment in the month it
settled; migration ledger 0038. Gate after one rework cycle (three MAJOR: a 500 on an oversized
amount, the P&L month of installment payments, lease end not blocked by a live plan; all
resolved; one MINOR deferred, the twice-monthly grid cell): ruff, format, mypy (376 files), lock,
migrations clean; sqlite 4,973 passed; PostgreSQL 5,583 passed (20:39); e2e 7; secret scans
clean; Fable's adversarial file 20 cases green (+1 PostgreSQL-only). Docs: RUNBOOK §17 (transfer
and installment paragraphs), DATA_OWNERSHIP (`installment_plans`), README rows, ADR-0012, QA-012e.
**Checkpoint report to the owner before the push** with the decisions: proration rounding (D1),
the reportlab/pillow dependency, a lease-end balance left after a transfer, and the split of a
charge that has a live Stax invoice or schedule (follow-up proposal).

## Update, 2026-10-14, owner's decisions on 12b and D14 built (not pushed)

The owner decided: (1) the single rounding at the end stands ($1,225.81), the rule's wording
now says so (ADR-0012 §2); (2) `reportlab` accepted; (3) debt after a transfer blocks by
default, an admin may allow it with a written reason, audited, the "Lease-end balance" charge
staying on the outgoing operator and on the attention list until paid; (4) follow-up **12b-a**
approved: splitting a charge with a live Stax invoice or schedule deletes the parent's Stax
object without voiding the charge and recreates the collection per installment, to be built
right after this push and before Step 12d. Decision 3 is built as PLAN-012e D14 (migration
0039, `transfer.override_debt`, route `ledger:transfers:override_debt`, attention kind
`lease_end_balance`); Fable's six adversarial cases green; the whole tree re-gated: sqlite
5,011 passed; PostgreSQL 5,626 passed (21:37); e2e 7; secret scans clean. Committed on top
of `b3ad928`; the push waits for the owner's word on the updated Publish Request.

## Update, 2026-10-14, Step 12b (PLAN-012e A, B, C, D14) deployed to staging

Pushed `81921bf..3cde4bc` on the owner's word. CI run 38651101891: lint, types, unit and e2e
green (the unit step took 22:11 of its 25-minute limit; BACKLOG §4 is now urgent and goes first
in 12b-a: the seeded migration round trips as their own step with their own limit); deploy
staging green. Staging revision `finance-staging-web--0000041` is healthy on image `3cde4bc`,
`deploy_migrate` applied the pending migrations (ledger 0034–0039, payments 0016–0017) and
reports up to date, `/healthz` 200. Owner's staging clicks for Checkpoint 12b: a lease starting
on the 13th; End lease blocked on a live invoice and clean on another store; Transfer, including
one with deductions above the deposit (blocked, allowed with a reason, on the attention list);
a 3-installment split on a hand-collected charge and the P&L unchanged by it. Next: 12b-a.

## Update, 2026-10-14, PLAN-012b-a built and gated (not pushed)

Both parts of 12b-a, in the owner's order, on top of `c3f2b2f`; nothing pushed until the owner
confirms the 12b clicks. **Part 1 (Fable, CI config only):** the 21 seeded migration tests run
as their own CI step "Migration tests" (limit 15) and the unit step runs the complement (limit
20); measured locally on PostgreSQL: migration selection 8:04, unit selection
13:58 (the undivided suite was 21:44; CI ran it in 22:11); BACKLOG §4 closed. **Part 2
(Opus):** a one-off charge whose own single-charge Stax invoice or schedule is live can be split;
the worker releases that object (deleted at Stax, the charge never voided, the local invoice
void/DELETED) and only then creates the installments' objects; a payment on the old invoice
before or after the release puts the plan on the attention list ("Payment on a split charge");
a parent on a live plan can no longer be cancelled or waived on its own; multi-line live
invoices and rent invoices unchanged. One MAJOR found and resolved in the rework (the parent's
cancel/waive). Gate on the final tree: ruff, format, mypy (379 files), lock, migrations clean;
sqlite 5,039 passed; PostgreSQL as the two CI selections, 21 and 5,633 passed; e2e 7; secret
scans clean; Fable's adversarial file 6 cases green. QA: `docs/qa/QA-012b-a-…md`.

## Update, 2026-10-15, Checkpoint 12b clicks; 12b-a pushed; PLAN-012b-b started

The owner's staging clicks for 12b: tests 2a, 2b, 3, 4 and 5 passed; test 1 (proration on a
lease starting on the 13th) could not run: **finding 12b-1**, the first rent charge of a
backdated lease is never created (the run looks at today only). On the owner's word, pushed
`3cde4bc..a59de59` (the deploy note, the CI step split, 12b-a part 2). PLAN-012b-b written from
the owner's items 3 to 9: rent run catch-up and the first charge on save (12b-1), a Rent line in
the invoice builder, a lease end left at blockers leaves nothing behind, the wizards create the
final prorated rent charge, an unpaid deposit blocks, the P&L rule at lease end (ADR-0012 §7),
splitting rent through the release path; three batches, each gated; checkpoint before the push.
Batch A assigned.

## Update, 2026-10-15, PLAN-012b-a deployed to staging

CI run 38680501084 on `a59de59`: Migration tests 10:34 of 15, Unit tests 12:44 of 20 (under the
owner's 15 minutes), e2e and the deploy green. Staging revision `finance-staging-web--0000042`
healthy on image `a59de59`, no pending migration, `/healthz` 200. The 12b-a staging click
(split an e-mailed charge whose Stax invoice is live) is open for the owner.

## Update, 2026-10-15, PLAN-012b-b Batch A built and gated (not pushed)

Batch A (owner's items 3, 5, 6, 7): the rent run catches up every missing debit since each
lease's start (24 months back), the daily job now creates charges too (it never did; the owner's
text names it), a saved lease gets its first prorated charge at once; a lease end is "in
progress" only once its statement step opened (banner with Continue and Cancel, a transfer
adopts an earlier draft, term-less drafts swept after 7 days); opening the statement creates the
final prorated rent charge and catches up earlier months; an unpaid deposit blocks and the
statement reads "Deposit charged $X, collected $Y"; migration 0040. Gate: ruff, format, mypy
(383 files), lock, migrations clean; sqlite 5,075 passed; PostgreSQL 22 + 5,670 passed; e2e 7;
secret scans clean; Fable's adversarial file green (5 + 1 PostgreSQL-only); one of Fable's older
tests fixed for the first charge on save. **For the owner:** the first daily tick after the
deploy creates the missing months for every active lease (staging: test data). Batch B assigned.

## Update, 2026-10-15, PLAN-012b-b Batch B built and gated (not pushed)

Batch B (owner's items 4 and 8): the invoice builder's **Rent** line (lease and month, the
amount defaulting to the prorated first month, one per lease and month, refused for a paid,
invoiced or autopay month; sending creates or takes the month's rent charge and links the
invoice, so the run and the catch-up never bill it again; the grid shows the invoice number;
deposit, setup and rent on one move-in invoice, the payment split by line and the rent part
counted as rent); migration 0041. The P&L at lease end follows ADR-0012 §7 (the withheld deposit
billed and collected in the end month; the owner's 1300/1500 rows came from a typed $1,000 "Rent"
item counted as a fee, which the Rent line replaces). One MAJOR found by the reproduction and
fixed: the transfer's balance blockers ignored the unpaid-balance offset. Gate: ruff, format,
mypy (385 files), lock, migrations clean; sqlite 5,113 passed; PostgreSQL 23 + 5,708 passed;
e2e 7; secret scans clean; Fable's adversarial file 12 cases green. Batch C assigned.

## Update, 2026-10-15, PLAN-012b-b Batch C built and gated (not pushed); 12b-b complete

Batch C (owner's item 9): rent charges split through the release path. A rent charge with its
own live Stax invoice (a rent-alone builder invoice) is released like a one-off charge; a rent
charge that autopay would still debit this month is split in one step: the worker deletes the
lease's rent schedule at Stax and creates it again from next month's debit (each debit of a
twice-monthly lease keeps its own start; the lease page says "Autopay resumes <date>"), then the
installments' own schedules follow; a failure between delete and create is a failed job with
Retry and voids nothing; a late debit of the old schedule lands on the rent charge and puts the
plan on the attention list. One MAJOR found and fixed in the rework (the attention item covered
one-off parents only). TODO-VERIFY-064 (Stax runs a new `DTSTART` without catch-up) is open
until the first autopay split on staging. Gate: ruff, format, mypy (386 files), lock, migrations
clean; sqlite 5,133 passed; PostgreSQL 23 + 5,728 passed; e2e 7; secret scans clean; Fable's
adversarial file 4 cases green. 12b-b is complete in the working tree (Batches A, B, C
committed); the checkpoint report goes to the owner before the push.

## Update, 2026-10-15, push of 12b-b: tests green, the image build did not start

Pushed `a59de59..262e72c` on the owner's word. CI run 38757250671: Migration tests 11:34 of 15,
Unit tests 13:15 of 20, e2e green; **Deploy staging failed** at "Build the image in Azure
Container Registry": after uploading the build context, the ACR Tasks API answered the schedule
request with a body that is not JSON (`az acr build`: "Expecting value: line 1 column 1"); no run
was scheduled (the registry's last run is `ca15`, the 12b-a build; the registry reports
Succeeded). Staging stays on revision 42 (`a59de59`), healthy, `/healthz` 200. A transient fault
on the Azure side; the remedy is a re-run of the failed job, which is a publish action and waits
for the owner's word.

## Update, 2026-10-15, PLAN-012b-b deployed to staging

The owner authorized a re-run of the failed deploy job of run 38757250671; every step passed
(image build, web, worker, reconcile, daily, verification, smoke test). Staging revision
`finance-staging-web--0000043` healthy on image `262e72c`, `deploy_migrate` applied ledger 0040
and 0041 and reports up to date, `/healthz` 200. The owner's staging clicks for 12b-b are open:
Istanbul (1991) proration after the first daily tick or "Create today's charges"; Test store 2
transfer; Test Store Three P&L per ADR-0012 §7; a move-in invoice with a Rent line; an autopay
rent split (settles TODO-VERIFY-064). Next: Step 12d (invoice-level payment entry) or the
owner's findings from the clicks.

## Finding 12d-1 (owner, 2026-10-15, staging): invoice drafts are not reachable

An unsent draft (DRAFT-143BF4CD, store Istanbul 1991) is listed nowhere the owner looked: not
the store page, the lease page, the invoice builder, nor `/invoices/`. Today only the
**operator page** lists drafts ("Invoices made here", number linked, no actions), and a draft's
own URL is `/invoices/drafts/<uuid>/` (the DRAFT- label is the id's first eight characters, so
the label alone does not give the URL). **For Step 12d:**
the store page's Invoices tab lists the operator's drafts with Edit, Send and Delete (Cancel);
the operator page likewise; the "rent <month> of store X is already on draft DRAFT-…" refusal
(PLAN-012b-b D4, `LINE_RENT_ON_DRAFT`) links to that draft.

## Update, 2026-10-15, Checkpoint 12b-b closed; Step 12d received

The owner's clicks: Istanbul's October charge created by the catch-up (13–31 Oct, 19 of 31
days = $1,225.81); Test store 2 has no stuck lease end; Test Store Three's P&L row follows
ADR-0012 §7 (548.39 / 1,800 / 1,500 incl. 500 withheld); Istanbul's November rent invoice sent
with one charge linked. Click 5 (the autopay split, TODO-VERIFY-064) waits for ACH on the
sandbox. **Step 12d** received in writing: the store page as the single workspace (header card,
one action bar, tabs incl. drafts with Edit/Send/Delete, manual payment allocation with credits),
the lease page as details only, the operator page as the same workspace across stores, detail
pages with a store link, navigation to store pages, URLs kept with redirects, no "are you sure"
for reversible actions, forms opened from the store hide the known fields, the setup checklist
without tick boxes and a "Show in setup checklist" flag, the builder's single combo box per line
with "Existing open charge", a month select, the width bug, trash-icon remove, the rent run out
of the main navigation with "Create missing charges" per store, the grid window (10 past,
current, next) with a From/To picker; tests; a new ADR for manual payment allocation. Done
before 12c (the design pass). Plan: PLAN-012d.

## Update, 2026-10-15, PLAN-012d Batch A built and gated (not pushed)

Batch A of Step 12d: the store page is the workspace (header card; the action bar with Create
invoice, Record a payment (the Charges tab until Batch B), Charge now (a pick page), Add setup
charge, Upload document, Add contact entry, Edit lease, End lease, Transfer, Create missing
charges; "New lease" on a vacant store; viewers see no bar; forms opened from the store hide the
known fields, show one read-only line and return to the right tab); tabs Charges (every lease of
the store), Invoices (with the store's drafts and Edit, Send, Delete), Payments, Schedules,
Documents, Contact log, History (admin); the lease page keeps lease terms only; charge, invoice,
plan and payment pages carry "← Store NNNN"; the attention list, grid and run page link store
pages; Rent run left the main nav for an Admin menu; `aria-current` marks the section. No
migration. Two rework cycles: "Schedule a payment" moved to Batch B (the setup-charge form cannot
schedule a debit) and the insurance figure shared with receivables; then the nav's second role
check had added a query to every page (revenue broke its budget in the volume test) — the role
is now resolved once per request, every page two queries lighter. Gate: ruff, format, mypy (392
files), lock, migrations clean; sqlite 5,178 passed; PostgreSQL 23 + 5,773 passed; e2e 7; secret
scans clean; Fable's adversarial file 5 cases green. One MINOR deferred (12d-2: the Charges tab's
rent and one-off rows read the stored balance, so a rescheduled return's carried amount is not
subtracted as the header does; Batch D). Note: the PostgreSQL unit selection took 15:04 locally
on a slow run (12:44 on CI before this batch); CI's figure on the next push decides whether the
step needs a further split. Batch B assigned.

## Update, 2026-10-15, PLAN-012d Batch B built and gated (not pushed)

Batch B of Step 12d (ADR-0013): **Record a payment** from the store page with manual
allocation over the lease's open charges (rent, one-off, insurance), nothing pre-filled unless
one charge is open, the unallocated amount live, the remainder kept as a **credit on the lease**
when ticked; a credit is applied from the same form later (payment method `credit`), shown in
the header and the Payments tab, counted as "Credits held" in the P&L footer, refunded at the
lease end and blocking a transfer while it remains; each allocated part is a payment on its
charge with today's Stax marking rules; insurance manual payments exist now (flagged for review
instead of marked at the provider: no insurance mark job, deferred); **Schedule a payment**
creates an autopay one-off charge on a chosen date; migration 0042 (`lease_credits`,
`payments.allocation_group/credit_id`, method `credit`, kind `credit_refund`). One rework
cycle: the processor named outside `payments/`, the call-graph scan tripped by a parameter named
`provider`, a template load line, a route list, and the header's "Credit $0.00". Gate (re-run
under `caffeinate` after a four-hour idle crawl): ruff, format, mypy (396 files), lock,
migrations clean; sqlite 5,228 passed; PostgreSQL 24 + 5,831 passed; e2e 7; secret scans clean;
Fable's adversarial file 7 cases green on both engines. Batch C assigned.

## Update, 2026-10-16, PLAN-012d Batch C built and gated (not pushed)

Batch C of Step 12d (owner's 8a and 8b): the setup checklist without tick boxes (a price means
charged, the catalogue default is a placeholder, a "Show in setup checklist" flag on catalogue
items set for the seeded ones and off for builder-saved ones, a live total; the transfer's
incoming step and the lease-end deductions table follow the same table rules); the invoice
builder's single combo box per line (Rent, Existing open charge, catalogue items, typed names),
the month select, the amount filled at once from a read-only JSON route, existing open charges
(the checklist's deposit) attached on send, the full-width Item box, one line on a new draft, the
trash-icon remove, errors numbered by the visible line; migrations 0043 and 0044. Gate: ruff,
format, mypy (400 files), lock, migrations clean; sqlite 5,251 passed; PostgreSQL 26 + 5,853
passed; e2e 7; secret scans clean; Fable's adversarial file 5 cases green on both engines; one
test-list omission (a constraint case) filled by Fable. **For the owner:** billing an existing
charge below its balance reprices the charge (a $500 deposit billed at $450 becomes a $450
deposit, audited). Batch D assigned.

## Update, 2026-10-16, PLAN-012d Batch D built and gated (not pushed)

Batch D of Step 12d (owner's 9 and 9a, finding 12d-2): the run page lists every charge the
last run created (store, month, amount, "catch-up" or "today", linked), from the daily job or the
button (one audit row `rent_run.ticked` per run); the dashboard shows "Today's rent run: N
created, M skipped"; **Create missing charges** on the store's bar runs the catch-up for that
store only and says what it created or "Nothing missing"; the grid opens on 10 past months, the
current and the next, with From/To month **selects** (the owner's Safari rule; one rework cycle
replaced the month inputs) capped at 24 months; the Charges tab's rent and one-off rows now use
the receivables figures and read "Carried by re-debit" where a re-debit carries the balance, so
the tab total equals the header (12d-2 closed). No migration. Gate: ruff, format, mypy (402
files), lock, migrations clean; sqlite 5,272 passed; PostgreSQL 26 + 5,874 passed; e2e 7; secret
scans clean; Fable's adversarial file 3 cases green on both engines. Batch E assigned (the
operator page as the workspace; the reports pages' month inputs become selects under the same
rule).

## Update, 2026-10-16, PLAN-012d Batch E built and gated; Step 12d complete (not pushed)

Batch E: the operator page is the same workspace across the operator's stores (a store selector
on the bar, the tabs with a store column, the operator-level items kept above, the drafts' Edit,
Send and Delete); every month picker in the app is a plain select (the reports' month inputs
replaced under the owner's Safari rule). No migration. Gate: ruff, format, mypy (404 files),
lock, migrations clean; sqlite 5,304 passed; PostgreSQL 26 + 5,906 passed; e2e 7; secret scans
clean; Fable's adversarial file 3 cases green on both engines. **Step 12d is complete in the
working tree** (Batches A–E, migrations 0042–0044, ADR-0013 proposed); the checkpoint report
with the Publish Request goes to the owner.

## Update, 2026-10-16, owner's decisions on 12d; Batch F (search) added

The owner confirmed ADR-0013 (Accepted), ruled that an existing charge is never billed below its
balance from the invoice builder (edit the charge first, audited), and left insurance manual
payments flagged for review (the mark job a later item). The owner's item **9b** (a filter on the
Stores list and the Grid by store number, name, city or operator, kept in the URL; a top-bar
search that jumps to a store or operator) had not reached Fable with the Step 12d text and was
not built; added to PLAN-012d as Batch F (D16, D17, D10 amended) and assigned. The push waits
for Batch F's gate and the updated Publish Request.

## Update, 2026-10-16, PLAN-012d Batch F built and gated; Step 12d complete (not pushed)

Batch F (owner's 9b and decision 2): the Stores list and the Grid filter by store number, name,
city or operator (`?q=`, kept in the URL, the paging, the exports and the grid's From/To form,
"Clear"); the top-bar search jumps to the one matching store or operator, else lists the matches
(archived operators listed, never jumped to); an existing charge is never billed below its
balance from the invoice builder (the amount is read-only; edit the charge first). No migration.
Gate: ruff, format, mypy (408 files), lock, migrations clean; sqlite 5,335 passed; PostgreSQL
26 + 5,937 passed; e2e 7; secret scans clean; Fable's adversarial file 9 cases green on both
engines. Step 12d is complete in the working tree (Batches A–F); the updated Publish Request
goes to the owner.

## Update, 2026-10-16, PLAN-012d deployed to staging

Pushed `262e72c..aded787` on the owner's word. CI run 38933677713: Migration tests 10:47 of 15,
Unit tests 10:00 of 20, e2e green; deploy green (the image build 0:57, no repeat of the ACR
fault). Staging revision `finance-staging-web--0000044` healthy on image `aded787`,
`deploy_migrate` applied ledger 0042–0044 and reports up to date, `/healthz` 200, `/search/`
answers 401 to anonymous as every page does. Checkpoint 12d clicks for the owner: everyday work
on one store from its page (invoice, payment with allocation and a kept credit, document,
contact entry, missing charges); drafts from the store and operator pages with Edit, Send,
Delete; the checklist without ticks; the builder's Rent and existing-charge lines in Safari; the
grid's 12-month window and selects; the operator page's store selector; the top-bar search;
the below-balance refusal. Next: 12c (the design pass) on this structure.

## Update, 2026-10-16, Checkpoint 12d closed; Step 12c received

All twelve staging clicks passed (store workspace, allocation with a kept credit, drafts with
Edit/Send/Delete, checklist, builder in Safari, grid window and filter, operator workspace,
top-bar search, vacant-store bar). **Step 12c** received: the visual design pass on the 12d
structure, no new features: design tokens in one CSS file, the shell (navy top bar with the
dark logo and the company name from a new `company_settings` table, the search box, user menu,
sandbox banner, a left navigation on desktop collapsing to the phone menu), components, the
dashboard as cards, the grid polish, print stylesheets, PWA icon and favicon from
`static/brand/` (committed in this step), accessibility with an AA-contrast test, screenshot
baselines of ten pages at two widths compared in CI, forms that show only what applies (every
wizard and form walked), and a plain-language copy review with the full before/after list for
the owner's approval **before** any change. Plan: PLAN-012c; the copy list goes to the owner
first.

## Update, 2026-10-16, 12c copy list approved; Batch A started

The owner approved PLAN-012c §4 with three changes: "Deposits versus transactions" → "Bank
account check" (not "Bank deposits": deposit means the security deposit elsewhere); the badge
"Needs review" stays; "NSF fee" → "Returned payment (NSF) fee". Everything else as written,
including the left nav, the Admin group, the autopay labels and the money filter. Batch A
(tokens, company settings, the shell, the brand files, PWA, titles) assigned.

## Update, 2026-10-17, PLAN-012c Batch A built and gated (not pushed)

Batch A of the design pass: design tokens in `tokens.css` (brand colours, AA neutrals, status
tokens, spacing, type, radii, shadows, focus ring; no literal colour anywhere else, tested); the
`company_settings` singleton (migration 0045: name, short name, support e-mail, time zone, logo
override, processor display name "Stax"; admin-edited, audited) read once per request and shown
in the bar, the titles ("Monvera · <page>"), the sign-in app name and the job banner; the shell
(navy top bar with the dark logo, the search box and a user menu; the sandbox banner kept; a
left navigation on laptops with Dashboard, Stores, Operators, Monthly rent overview, Reports and
Admin groups, the Prompt 11 phone menu below 900 px); the brand files committed (originals in
`static/brand/`, served copies with 192 and 512 px icons), PWA icons, splash and favicon from
them. One MAJOR in the gate (three queries per page from the company processor) resolved. Gate:
ruff, format, mypy (412 files), lock, migrations clean; sqlite 5,450 passed; PostgreSQL 28 +
6,072 passed (one unreproduced calendar failure in the first run, re-run green, probe 10/10, a
diagnostic added to the test); e2e 7; secret scans clean; Fable's adversarial file 8 cases green
on both engines. **For the owner at the checkpoint:** the dark logo carries the wordmark, so the
bar read "Monvera Monvera"; Batch B shows the company name as text only with a logo override;
the grid page keeps the folded menu at every width (twelve months must fit at 1,280 px). Batch B
assigned.

## Update, 2026-10-17, PLAN-012c Batch B built and gated (not pushed)

Batch B of the design pass: the component set in `app.css` (one documented section each: page
header, the 12d workspace classes as the reference, tables with right-aligned money cells, zebra
rows, a sticky head row and `aria-sort` where a view sorts, `.field` forms with the error under
the control, status badges with a leading glyph so colour is never the only signal, empty states
with a next step, `.confirm` pages with "What will happen" and one primary and one secondary
button, flash messages as dismissable toasts that keep their text, pagers, wizard steps with
`aria-current="step"`); the dashboard as cards with the attention list first; the grid legend as
badges; `print.css` for the statement and the P&L (no bar or navigation, black on white, bordered
tables, the company name and the date in the header, "Printed <date>" in the footer, a page per
store group of the P&L); the bar shows the company name once (as the logo's text alternative, as
text only beside an override logo). Gate: ruff, format, mypy, lock, migrations clean (no
migration); sqlite 5,587 passed; PostgreSQL 28 + 6,209 passed (one unreproduced single failure in
`test_nsf`'s calendar case on the first run; see QA-012c, residual risk); e2e 7 passed in four
runs on PostgreSQL and four on a clean worktree of Batch A; secret scans clean; Fable's
adversarial file 18 cases green on both engines. Two MAJOR (`.confirm` and `.field` coverage) and
three MINOR (statement page breaks, the due glyph, one primary per page) resolved in one rework
cycle. Batch C (forms show only what applies) assigned.

## Update, 2026-10-17, PLAN-012c Batch C built and gated (not pushed)

Batch C of the design pass: forms show only what applies (D8). The lease-end settlement shows the
refund fields only when a refund is due (else the amount owed and "Save and continue"); the
deductions rows sit behind "Add deduction" until one exists; the reason note shows while the
reason is Other; the transfer's incoming step shows the deposit field only when the catalogue
offers a deposit and the setup section only with checklist items; the lease form's second debit
day sits behind a "Debited twice a month" tick (never posted) and the lease page offers autopay
only on a provider-ACH lease; the operator form's consents follow the phone and the e-mail and the
operator page shows the provider identifiers only with a provider customer; the keep-credit tick
only while something is unallocated; the transfer-to-lease field only when another lease exists;
Reschedule only on a returned ACH debit. Hiding never relaxes validation: hidden fields stay on
the forms, dependent fields are disabled by a small script and stay visible without it. Items the
plan marked "already" each gained a pinning test. Gate: ruff, format, mypy (418 files), lock,
migrations clean (no migration); sqlite 5,636 passed; PostgreSQL 28 + 6,258 passed; e2e 12 passed
in four runs on PostgreSQL (Opus saw the sqlite live server lock one test per full local run: a
sqlite-only limit, CI runs e2e on PostgreSQL); secret scans clean; Fable's adversarial file 20
cases green on the pre-C worktree and on the C tree. One MINOR (the transfer step's second debit
day lacks the tick) deferred to Batch D as a carry-over. **For the owner at the checkpoint:** the
operator page's "provider fields" and the lease page's "autopay controls" were read as the pages'
blocks (the forms have none). Batch D (the approved copy list) assigned.

## Update, 2026-10-18, PLAN-012c Batch D blocked: the implementation sessions stall

Batch D (the approved copy list, the money filter, the banner wording, two carry-overs) is half
built in the **uncommitted** working tree: the first Opus session renamed the copy across about
133 files, added `finance/templatetags/money.py`, `ledger/wording.py`, the twice-monthly partial
and `tests/test_copy_pass.py`, and ran the unit suite once (176 failures, almost all existing
tests asserting the old wording, plus the Rule 8 fence: two files outside `payments/` named the
processor). It then stalled three times while updating those tests; a fresh session stalled
twice more without a single edit (2026-10-17 22:09 is the last change). Fable does not write
production code (root `CLAUDE.md` §1.1), so the batch waits for a working implementation
session. The committed state is sound: Batches A, B and C (`4843c1d`, `23428eb`, `aa16490`)
each passed the full gate; nothing is pushed; staging still runs `aded787`. Fable's Batch D
adversarial file is ready (`tests/test_plan012c_design_d_adversarial.py`: frozen choice values
and route names, the money filter under hostile input, the provider name escaped, user data
shown as typed, the banner on every page). Next: resume Batch D when a session holds, gate it,
then Batch E (contrast and visual regression) and the checkpoint report with the Publish Request.

## Update, 2026-10-18, 12c Batches A–C deployed to staging; Batch D in slices

Pushed `aded787..e12d8f9` on the owner's "push" (Publish Request of 2026-10-18); CI green (tests
25:23, deploy 3:29); staging runs the shell, the tokens, the components and the hidden-field
rules of Batches A–C. The owner's decision on Batch D: keep the renames, finish in small slices,
each its own session and commit, the full gate at D6. Done so far: D1 (the Rule 8 fence, the money
filter hardened, the banner wording) as `1439f02`; D2a (the §4.5 labels moved into the TextChoices
classes with choices-only migrations ledger 0046 and payments 0018; the parallel label table and
the display overrides removed) as `7fa6881`. **Decision for the owner at the checkpoint:** a
TextChoices label cannot read company settings, so the labels that named the processor (six
SyncFailure labels, most JobKind labels, PaymentChannel "Bank debit") carry the name through the
sentence around them instead ("Bank debit through Stax" on the lease page; "Stax request in
progress: Updating the operator" in the job banner); the approved §4.5 wording is otherwise kept.
D2b (ledger leases, installments, deposits, builder; the lazy-sentence audit defect) in progress;
then D2c, D3, D4, D5, and the D6 gate.

## Update, 2026-10-18, PLAN-012c Batch D closed (D6 gate PASS, not pushed)

The copy pass is complete in eight slices (D1, D2a, D2b, D2c, D3, D4, D5, D5b; see QA-012c). The
approved §4 list is applied; the processor is named from company settings everywhere and never
as a literal outside `payments/`; the status labels live in the TextChoices classes (choices-only
migrations ledger 0046, payments 0018); amounts read `$2,000.00` through one filter; the banner
reads "SANDBOX — test system, no real money"; job results show failures in words with the class
and the fields kept in the audit rows. D6 gate: ruff, format, mypy (424 files), lock, migrations
clean; sqlite 5,860 passed; PostgreSQL 28 + 6,482 passed; e2e 12 passed in four runs on
PostgreSQL; secret scans clean; Fable's adversarial file (25 cases, frozen stored values and
route names) green. **Decisions for the owner at the checkpoint:** labels that named the
processor carry the name through the sentence around them; the "Returned payment (NSF) fee"
label reaches the public statement line; invoice line descriptions sent to Stax carry the dollar
sign; three admin-page diagnostic codes deferred (QA-012c, Batch D). Batch E (contrast and
label tests, visual regression baselines and the CI compare) assigned.

## Update, 2026-10-18, PLAN-012c Batch E closed; Step 12c complete locally; checkpoint

Batch E: the accessibility tests (AA contrast of every documented pair, computed by a test; a
label on every control of every form and 33 rendered pages; the focus ring; status never by
colour alone), which found and fixed three undocumented colour pairs and 74 table-cell controls
without a name (now visually-hidden labels naming the row and the column, kept in step by the
builder's script); the visual regression suite (fifteen pages at 1280 and 390 px, frozen clocks,
a fixed data set, a 0.5 % budget) with its thirty baselines generated inside the Playwright Linux
image, and the CI job "Visual regression" in that image, which the staging deploy now waits for.
Gate: ruff, format, mypy (429 files), lock, migrations clean; sqlite 5,872 passed; PostgreSQL
28 + 6,494 passed; e2e 12 passed in four runs; the visual suite 34 passed in the image on both
engines; secret scans clean. One MAJOR in the suite (a connection left open at teardown on
PostgreSQL) resolved. Repository finding: the project's `.gitignore` was never tracked (the
owner's global `~/.gitignore` ignores `.gitignore`); force-added in this commit. **Step 12c is
complete locally: Batches D and E await the owner's checkpoint and a Publish Request.**
