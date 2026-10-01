# PLAN-007b — Store page, read-only admin for stores, operators and leases, second debit day

**Date:** 2026-10-01 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Origin:** owner, 2026-10-01: no store can be created on staging, so nothing in Prompt 7 can be
exercised; the admin shows only users and devices; "Second debit day" defaults to 1.

## 1. Scope

1. **Store page** in the main app: list (search by label and city, paging 25), create, edit.
   Fields: store number (optional integer), name = `external_label` (required), city, state,
   **open date (required on this form, per ADR-0005)**, closed date (optional, not before the
   open date), status. The model keeps `open_date` nullable for the import; the form requires
   it; `needs_open_date` follows the model's existing rule. No delete. Audit rows
   `store.created`, `store.updated`. Roles: create and edit `admin`; list and detail read for
   any verified role. Routes `ledger:stores:list|create|detail|edit` classified in the registry.
2. **Admin, read-only:** register `Store` and `Lease` like `Operator` (no add, change or
   delete); give the `admin` group `view_*` permission on `Store`, `Operator` and `Lease`
   through a data migration so an admin-group user (not only a superuser) can inspect them.
3. **Lease form:** `second_debit_day` has no default (empty), `required=False`; a second day
   equal to the first is refused ("The second debit day must differ from the first."), a
   second day without a first is refused. Both already partly exist; make the default empty
   and keep the two refusals.

## 2. Tests

| # | Case | Author |
|---|---|---|
| 1 | Store create and edit with audit rows; open date required on the form; closed date before open date refused; status closed list; replayed creation key saves once; PRG on every POST | Opus |
| 2 | List search and paging; detail shows leases of the store | Opus |
| 3 | Roles: admin creates and edits, billing and viewer read only (403 on POST), anonymous 401; CSRF; registry test green | Opus |
| 4 | Admin: `Store`, `Operator`, `Lease` listed read-only; an admin-group user who is not a superuser can open the three changelists; no add/change/delete permission | Opus |
| 5 | Lease form: second debit day renders empty by default; equal days refused; second without first refused; a valid pair accepted | Opus |
| 6 | Adversarial: store label with a card number refused; 5,000-character search; closed date equal to open date; open date far in the future accepted but flagged nowhere (just saved); paging nonsense | Fable |
| 7 | Whole suite both engines, lint, format, types, migrations, secret scan | Fable |

## 3. Acceptance

Gate green on both engines; `makemigrations --check` clean; no existing test removed or
skipped; on staging the owner can create a store, a lease and reach "Enable autopay".
