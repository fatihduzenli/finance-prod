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
  never deleted. Card guard tested on staging: a card number in a note was refused.
- **Owner notes for the record:** receipts go to the email stored in Stax, so an email
  mismatch flag tells staff to fix it in Stax; a database-level guard for bank routing and
  account patterns is added to the **Prompt 14** security scope.
- Open from this: where the old invoice numbers live (the Rents tab has none); needed before
  Prompt 13. D3 to D11 still await the owner's word.
