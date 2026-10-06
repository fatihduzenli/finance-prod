"""Ledger routes (PLAN-006-operators.md §4.1, PLAN-007-leases-and-rent-run.md §4.1,
PLAN-007b-stores.md §1, PLAN-008-webhooks.md D6, D8, PLAN-009-payments-ledger.md D2, D4,
D6 to D8, PLAN-010-worker-and-reconciliation.md D10, PLAN-010b D5 to D8, D11, PLAN-010c
D7, D10, PLAN-010d D1, D2, D4, D6, D8).

Namespace ``ledger``.

Every unsafe route here has a rule in ``accounts.registry``; the read-only ones are
listed there too, so ``backend/tests/test_route_registry.py`` sees them classified.
There is no delete route for a record: archiving is the only way to retire an operator,
and a store is closed through its status. A document's delete is soft (PLAN-010c D7).

``pk`` is matched as text, as the admin matches its object IDs, so every operator path
resolves to its route and the role registry decides on an unsafe request before the
view runs. The view then parses the UUID and loads the row, or answers 404.
"""

from __future__ import annotations

from django.urls import include, path

from ledger.views import (
    audit,
    charges,
    deposits,
    documents,
    leases,
    one_off_charges,
    operators,
    payments,
    reconciliation,
    reports,
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
        path("<str:pk>/charges/new/", one_off_charges.operator_new_charge, name="new_charge"),
    ],
    "operators",
)

store_patterns = (
    [
        path("", stores.store_list, name="list"),
        path("new/", stores.store_create, name="create"),
        path("<str:pk>/", stores.store_detail, name="detail"),
        path("<str:pk>/edit/", stores.store_edit, name="edit"),
        path("<str:pk>/cost/", stores.store_set_cost, name="set_cost"),
        path("<str:pk>/cost/close/", stores.store_close_cost, name="close_cost"),
        path("<str:pk>/costs/<str:row>/correct/", stores.store_correct_cost, name="correct_cost"),
    ],
    "stores",
)

lease_patterns = (
    [
        path("", leases.lease_list, name="list"),
        path("new/", leases.lease_create, name="create"),
        path("<str:pk>/", leases.lease_detail, name="detail"),
        path("<str:pk>/edit/", leases.lease_edit, name="edit"),
        path("<str:pk>/end/", leases.lease_end, name="end"),
        path("<str:pk>/enrol/", leases.lease_enrol, name="enrol"),
        path("<str:pk>/pause/", leases.lease_pause, name="pause"),
        path("<str:pk>/resume/", leases.lease_resume, name="resume"),
        path(
            "<str:pk>/setup-charges/new/",
            one_off_charges.lease_add_setup_charge,
            name="add_setup_charge",
        ),
        path("<str:pk>/charges/new/", one_off_charges.lease_new_charge, name="new_charge"),
        path(
            "<str:pk>/deposits/<str:charge>/resolve/",
            deposits.resolve_deposit,
            name="resolve_deposit",
        ),
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

one_off_charge_patterns = (
    [
        path("<str:pk>/", one_off_charges.charge_detail, name="detail"),
        path("<str:pk>/payments/new/", one_off_charges.charge_payment, name="payment"),
        path("<str:pk>/retry-mark/", one_off_charges.charge_retry_mark, name="retry_mark"),
        path("<str:pk>/link-invoice/", one_off_charges.charge_link, name="link"),
        path("<str:pk>/edit/", one_off_charges.charge_edit, name="edit"),
        path("<str:pk>/waive/", one_off_charges.charge_waive, name="waive"),
        path("<str:pk>/cancel/", one_off_charges.charge_cancel, name="cancel"),
        path("<str:pk>/retry/", one_off_charges.charge_retry, name="retry"),
        path("<str:pk>/charge-now/", one_off_charges.charge_now, name="charge_now"),
    ],
    "one_off_charges",
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

report_patterns = (
    [
        path("pnl/", reports.pnl_report, name="pnl"),
        path("pnl/export.csv", reports.pnl_export, name="pnl_export"),
    ],
    "reports",
)

document_patterns = (
    [
        path("stores/<str:pk>/upload/", documents.upload_store, name="upload_store"),
        path("leases/<str:pk>/upload/", documents.upload_lease, name="upload_lease"),
        path("<str:pk>/download/", documents.download, name="download"),
        path("<str:pk>/preview/", documents.preview, name="preview"),
        path("<str:pk>/delete/", documents.delete, name="delete"),
    ],
    "documents",
)

audit_patterns = (
    [
        path("", audit.audit_page, name="page"),
        path("export.csv", audit.audit_export, name="export"),
    ],
    "audit",
)

urlpatterns = [
    path("operators/", include(operator_patterns)),
    path("stores/", include(store_patterns)),
    path("leases/", include(lease_patterns)),
    path("run/", include(run_patterns)),
    path("charges/", include(charge_patterns)),
    path("one-off-charges/", include(one_off_charge_patterns)),
    path("payments/", include(payment_patterns)),
    path("webhooks/", include(webhook_patterns)),
    path("returns/", include(return_patterns)),
    path("reconciliation/", include(reconciliation_patterns)),
    path("reports/", include(report_patterns)),
    path("documents/", include(document_patterns)),
    path("audit/", include(audit_patterns)),
]
