"""Ledger routes (PLAN-006-operators.md §4.1, PLAN-007-leases-and-rent-run.md §4.1,
PLAN-007b-stores.md §1, PLAN-008-webhooks.md D6, D8, PLAN-009-payments-ledger.md D2, D4,
D6 to D8, PLAN-010-worker-and-reconciliation.md D10).

Namespace ``ledger``.

Every unsafe route here has a rule in ``accounts.registry``; the read-only ones are
listed there too, so ``backend/tests/test_route_registry.py`` sees them classified.
There is no delete route: archiving is the only way to retire an operator, and a store
is closed through its status.

``pk`` is matched as text, as the admin matches its object IDs, so every operator path
resolves to its route and the role registry decides on an unsafe request before the
view runs. The view then parses the UUID and loads the row, or answers 404.
"""

from __future__ import annotations

from django.urls import include, path

from ledger.views import (
    charges,
    leases,
    operators,
    payments,
    reconciliation,
    returns,
    run,
    stores,
    webhooks,
)

app_name = "ledger"

operator_patterns = (
    [
        path("", operators.operator_list, name="list"),
        path("new/", operators.operator_create, name="create"),
        path("<str:pk>/", operators.operator_detail, name="detail"),
        path("<str:pk>/edit/", operators.operator_edit, name="edit"),
        path("<str:pk>/archive/", operators.operator_archive, name="archive"),
        path("<str:pk>/retry-sync/", operators.operator_retry_sync, name="retry_sync"),
        path("<str:pk>/link/", operators.operator_link, name="link"),
    ],
    "operators",
)

store_patterns = (
    [
        path("", stores.store_list, name="list"),
        path("new/", stores.store_create, name="create"),
        path("<str:pk>/", stores.store_detail, name="detail"),
        path("<str:pk>/edit/", stores.store_edit, name="edit"),
    ],
    "stores",
)

lease_patterns = (
    [
        path("", leases.lease_list, name="list"),
        path("new/", leases.lease_create, name="create"),
        path("<str:pk>/", leases.lease_detail, name="detail"),
        path("<str:pk>/edit/", leases.lease_edit, name="edit"),
        path("<str:pk>/enrol/", leases.lease_enrol, name="enrol"),
        path("<str:pk>/pause/", leases.lease_pause, name="pause"),
        path("<str:pk>/resume/", leases.lease_resume, name="resume"),
    ],
    "leases",
)

run_patterns = (
    [
        path("", run.run_today, name="today"),
        path("create-charges/", run.run_create_charges, name="create_charges"),
        path("match/", run.run_match, name="match"),
    ],
    "run",
)

charge_patterns = (
    [
        path("<str:pk>/", charges.charge_detail, name="detail"),
        path("<str:pk>/payments/new/", charges.charge_record_payment, name="record_payment"),
        path("<str:pk>/retry-mark/", charges.charge_retry_mark, name="retry_mark"),
        path("<str:pk>/link-invoice/", charges.charge_link_invoice, name="link_invoice"),
    ],
    "charges",
)

payment_patterns = (
    [
        path("", payments.payment_list, name="list"),
        path("export.csv", payments.payment_export, name="export"),
        path("<str:pk>/", payments.payment_detail, name="detail"),
        path("<str:pk>/refund/", payments.payment_refund, name="refund"),
        path("<str:pk>/void/", payments.payment_void, name="void"),
    ],
    "payments",
)

webhook_patterns = (
    [
        path("", webhooks.webhook_list, name="list"),
        path("<str:pk>/", webhooks.webhook_detail, name="detail"),
        path("<str:pk>/replay/", webhooks.webhook_replay, name="replay"),
    ],
    "webhooks",
)

return_patterns = (
    [
        path("", returns.return_list, name="list"),
        path("<str:pk>/resolve/", returns.return_resolve, name="resolve"),
    ],
    "returns",
)

reconciliation_patterns = (
    [
        path("", reconciliation.reconciliation_page, name="page"),
        path("run/", reconciliation.reconciliation_run, name="run"),
        path(
            "mismatches/<str:pk>/resolve/",
            reconciliation.reconciliation_resolve,
            name="resolve",
        ),
    ],
    "reconciliation",
)

urlpatterns = [
    path("operators/", include(operator_patterns)),
    path("stores/", include(store_patterns)),
    path("leases/", include(lease_patterns)),
    path("run/", include(run_patterns)),
    path("charges/", include(charge_patterns)),
    path("payments/", include(payment_patterns)),
    path("webhooks/", include(webhook_patterns)),
    path("returns/", include(return_patterns)),
    path("reconciliation/", include(reconciliation_patterns)),
]
