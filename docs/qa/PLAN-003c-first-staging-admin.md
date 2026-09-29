# PLAN-003c — First admin user on staging (PROPOSAL, awaiting owner approval)

**Date:** 2026-09-29 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Status:** proposed. Nothing in this plan has been built or run.

## 1. Restated objective

Staging has a working login page and no user. `seed_local_admin` refuses to run outside
`APP_ENV=local`, on purpose: it takes a password from the environment and prints backup
tokens. Staging needs one first admin created in a way where **no model, no log and no
shell history ever sees the password**, and where the account cannot be used without a
second factor. Every later user is created by that admin in the Django admin.

## 2. Proposed method

A new management command, `bootstrap_admin`, run **by the owner** inside the staging
container. The owner types the password at a hidden prompt. Fable and Opus never run it.

```
az containerapp exec -g finance-sandbox -n finance-staging-web \
  --command "python backend/manage.py bootstrap_admin --email <owner email>"
```

## 3. Interface contract

- Argument: `--email` (required, validated). No password argument, no password
  environment variable, no `--noinput` mode.
- Password: read twice with `getpass` (no echo), checked by Django's password validators.
  Requires an interactive terminal; without one it exits non-zero.
- **One-time:** refuses with exit 2 if any user already belongs to the `admin` group.
- Creates the user as staff, member of the `admin` group, **not** superuser, with no OTP
  device. Prints one line naming the email. Prints no password, token or secret.
- Allowed when `APP_ENV` is `staging` or `production`. In `local` it points to
  `seed_local_admin`.
- Writes one WARNING log line: first admin created, with the email.
- After creation the owner signs in at `/account/login/`, is held at the enrolment page by
  the existing MFA middleware, and scans the QR code. Until then the account can do nothing
  else.

## 4. Work breakdown

| Task | Files |
|---|---|
| T1 | `backend/accounts/management/commands/bootstrap_admin.py` |
| T2 | `backend/tests/test_bootstrap_admin.py` |
| T3 | `README.md`, `infra/README.md`: owner runbook |

## 5. Test plan

Happy path; second run refused; password mismatch; weak password rejected; no terminal;
invalid email; existing non-admin user with the same email; `APP_ENV=local` refused;
password absent from stdout, stderr, log and process arguments; created user has no
superuser flag and no device; created user cannot reach `/admin/` before enrolment (403).

## 6. Risk register

| Risk | Treatment |
|---|---|
| Password seen by a model or written to a log | Hidden prompt, owner runs it, test asserts absence |
| Window between creation and TOTP enrolment: password alone allows enrolment | Owner enrols immediately; command prints that instruction; one-time rule limits exposure to one account |
| Command reused later as a back door | One-time rule; refusal is tested |
| Owner email is personal data in the staging database | Accepted; staging is access-controlled |
| `az containerapp exec` session output stored by Azure | Nothing secret is printed; password is never echoed |

## 7. Alternatives rejected

- **Allow `seed_local_admin` on staging.** It reads the password from the environment and
  prints backup tokens; both would reach logs or shell history.
- **Fable or Opus creates the user.** A model would know the first admin's password.
- **Insert the user with SQL.** Bypasses validators and the audit line.

## 8. Approval needed

1. Approve this method, or name another.
2. After QA: a push approval for the code, as usual.
3. The `az containerapp exec` step is yours to run. It needs your Azure sign-in.
