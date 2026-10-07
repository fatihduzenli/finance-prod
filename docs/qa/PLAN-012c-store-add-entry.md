# PLAN-012c — The store page's "Add entry" button shows whenever the store has a lease with an operator

**Date:** 2026-10-07 · **Origin:** owner's click 2 of Prompt 12 on staging, in writing: *"the
store page (2477) shows the contact log list but has no 'Add entry' button; amendment (2) asked
for one. Add it: it opens the same form with the store/lease preselected and the operator fixed
to the lease's operator. Include it with the invoice-builder fix in the same push."*
**Lead/QA:** Fable · **Implementer:** Opus · **Ships with:** PLAN-012a and PLAN-012b, one push.

## 1. Restated objective

Batch A built the button (`stores:contacts_add`, `contact_log.store_add_url`) but showed it only
when the store has a lease **in effect today** (collecting or paused, started on or before today,
not ended before it). Store 2477's lease is outside that rule, so staff saw the list and no
button. From now on the store page offers **Add entry** to billing and above whenever the store
has **any lease with an operator who is not archived**: the lease in effect today when there is
one (the later start on a hand-over day, as now), else the store's most recent lease (latest
`started_on`, then `created_at`). The button opens the existing form with the store's lease
preselected and the operator fixed to that lease's operator; the form's title names the lease's
collection status when it is not current ("lease ended", "lease pending", …) so staff know which
tenancy the entry is about. Nothing else about the contact log changes.

## 2. Affected packages

`backend/ledger` (`services/contacts.py`: one new resolver; `views/contact_log.py` and
`views/contacts.py`: use it; the add form's title), `backend/tests`. Out of scope: the operator
page, the promise rules, the attention list, templates other than the title text.

## 3. Decisions

- **D1 — `contacts.store_contact_lease(store, today)`:** `store_lease(store, today)` when it
  returns a lease; else the store's leases with `operator__isnull=False` and
  `operator__archived_at__isnull=True`, ordered by `-started_on` (nulls last), `-created_at`,
  first. `store_add_url` and `store_contacts_add` both use it, so the button and the route agree.
- **D2 — the form:** unchanged fields; the lease preselected and locked as today when the view
  passes `lease=`; the title gains ", lease ended YYYY-MM-DD" / ", lease pending" / ", lease
  paused" when the lease is not in effect today (the status's display name; the end date when
  ended). An entry recorded on such a lease behaves as any other entry on that lease (the
  promise matching already keys on the lease).
- **D3 — no lease at all:** the button is absent and the route answers with the existing
  message and redirect.

## 4. Interface contract

`contacts.store_contact_lease(store: Store, today: date) -> Lease | None`. No schema, route,
template structure, environment or provider change.

## 5. Work breakdown (one task, Opus)

| Task | Files | Done when |
|---|---|---|
| Resolver, button, route, title; tests | `ledger/services/contacts.py`, `ledger/views/contact_log.py`, `ledger/views/contacts.py`, `tests/test_contact_log.py` | §6 green on both engines |

## 6. Test plan (before implementation)

Happy: a store whose only lease is `pending` (future start), `ended` (past end), or `paused`
shows the button to billing and admin; the form opens with that lease preselected and the
operator fixed; the saved entry carries the lease and appears on the store page and the
operator page. Boundaries: a store with a current lease and an older ended one → the current
one; two ended leases → the later start; a lease whose operator is archived → not chosen (the
next one is, or no button); a lease without an operator → skipped. Error paths: a viewer sees
no button and gets 403 on the route; a store with no lease → no button and the existing
message. Adversarial (Fable): the route for store A cannot record an entry against store B's
lease or operator by any posted field; the archived-operator rule holds on the route as on the
button.

## 7. Risk register

Low: a resolver and a title. Irreversible: none.

## 8. Acceptance criteria

Gate green on both engines; on staging, store 2477 shows **Add entry**, the form opens with the
store's lease and operator preset, and the entry appears on both pages.
