"""Ledger routes (PLAN-006-operators.md §4.1, PLAN-007-leases-and-rent-run.md §4.1,
PLAN-007b-stores.md §1, PLAN-008-webhooks.md D6, D8, PLAN-009-payments-ledger.md D2, D4,
D6 to D8, PLAN-010-worker-and-reconciliation.md D10, PLAN-010b D5 to D8, D11, PLAN-010c
D7, D10, PLAN-010d D1, D2, D4, D6, D8, PLAN-011 D0 to D5, PLAN-011b D4, D7, D8).

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

from django.urls import include, path, re_path

from ledger.views import (
    audit,
    calendar,
    charges,
    collections,
    contacts,
    dashboard,
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
    statement_links,
    stores,
    webhooks,
)

app_name = "ledger"

operator_patterns = (
    [
        path("", operators.operator_list, name="list"),
        path("export.csv", operators.operator_export, name="export"),
        path("new/", operators.operator_create, name="create"),
        path("<str:pk>/", operators.operator_detail, name="detail"),
        path("<str:pk>/edit/", operators.operator_edit, name="edit"),
        path("<str:pk>/archive/", operators.operator_archive, name="archive"),
        path("<str:pk>/retry-sync/", operators.operator_retry_sync, name="retry_sync"),
        path("<str:pk>/link/", operators.operator_link, name="link"),
        path("<str:pk>/charges/new/", one_off_charges.operator_new_charge, name="new_charge"),
        path("<str:pk>/statement/", reports.statement_page, name="statement"),
        path("<str:pk>/statement/export.csv", reports.statement_export, name="statement_export"),
        # The contact log (PLAN-011b D7).
        path("<str:pk>/contacts/", contacts.contacts_page, name="contacts"),
        path("<str:pk>/contacts/new/", contacts.contacts_add, name="contacts_add"),
        path("<str:pk>/contacts/export.csv", contacts.contacts_export, name="contacts_export"),
        # The collections ladder (PLAN-011b D4).
        path(
            "<str:pk>/handled-personally/",
            collections.operator_handled_personally,
            name="handled_personally",
        ),
        # The signed statement links (PLAN-011b D4): billing revokes every link sent.
        path(
            "<str:pk>/statement-links/revoke/",
            statement_links.statement_links_revoke,
            name="statement_links_revoke",
        ),
    ],
    "operators",
)

store_patterns = (
    [
        path("", stores.store_list, name="list"),
        path("export.csv", stores.store_export, name="export"),
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
        path("export.csv", leases.lease_export, name="export"),
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
        path("export.csv", run.run_export, name="export"),
        path("create-charges/", run.run_create_charges, name="create_charges"),
        path("match/", run.run_match, name="match"),
    ],
    "run",
)

charge_patterns = (
    [
        path("export.csv", charges.charge_export, name="export"),
        path("<str:pk>/", charges.charge_detail, name="detail"),
        path("<str:pk>/payments/new/", charges.charge_record_payment, name="record_payment"),
        path("<str:pk>/retry-mark/", charges.charge_retry_mark, name="retry_mark"),
        path("<str:pk>/link-invoice/", charges.charge_link_invoice, name="link_invoice"),
        path("<str:pk>/send-step/", collections.rent_charge_send_step, name="send_step"),
    ],
    "charges",
)

one_off_charge_patterns = (
    [
        path("export.csv", one_off_charges.charge_export, name="export"),
        path("<str:pk>/", one_off_charges.charge_detail, name="detail"),
        path("<str:pk>/payments/new/", one_off_charges.charge_payment, name="payment"),
        path("<str:pk>/retry-mark/", one_off_charges.charge_retry_mark, name="retry_mark"),
        path("<str:pk>/link-invoice/", one_off_charges.charge_link, name="link"),
        path("<str:pk>/edit/", one_off_charges.charge_edit, name="edit"),
        path("<str:pk>/waive/", one_off_charges.charge_waive, name="waive"),
        path("<str:pk>/cancel/", one_off_charges.charge_cancel, name="cancel"),
        path(
            "<str:pk>/delete-invoice/",
            one_off_charges.charge_delete_invoice,
            name="delete_invoice",
        ),
        path("<str:pk>/retry/", one_off_charges.charge_retry, name="retry"),
        path("<str:pk>/charge-now/", one_off_charges.charge_now, name="charge_now"),
        path("<str:pk>/send-step/", collections.one_off_charge_send_step, name="send_step"),
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
        path("export.csv", webhooks.webhook_export, name="export"),
        path("<str:pk>/", webhooks.webhook_detail, name="detail"),
        path("<str:pk>/replay/", webhooks.webhook_replay, name="replay"),
    ],
    "webhooks",
)

return_patterns = (
    [
        path("", returns.return_list, name="list"),
        path("export.csv", returns.return_export, name="export"),
        path("<str:pk>/resolve/", returns.return_resolve, name="resolve"),
        path("<str:pk>/reschedule/", returns.return_reschedule, name="reschedule"),
    ],
    "returns",
)

reconciliation_patterns = (
    [
        path("", reconciliation.reconciliation_page, name="page"),
        path("export.csv", reconciliation.reconciliation_export, name="export"),
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
        path("", reports.report_index, name="index"),
        path("pnl/", reports.pnl_report, name="pnl"),
        path("pnl/export.csv", reports.pnl_export, name="pnl_export"),
        path("aging/", reports.aging_report, name="aging"),
        path("aging/export.csv", reports.aging_export, name="aging_export"),
        path("revenue/", reports.revenue_report, name="revenue"),
        path("revenue/export.csv", reports.revenue_export, name="revenue_export"),
        path("payments-by-method/", reports.payments_by_method_report, name="payments_by_method"),
        path(
            "payments-by-method/export.csv",
            reports.payments_by_method_export,
            name="payments_by_method_export",
        ),
        path("deposits/", reports.deposits_report, name="deposits"),
        path("deposits/export.csv", reports.deposits_export, name="deposits_export"),
    ],
    "reports",
)

document_patterns = (
    [
        path("stores/<str:pk>/upload/", documents.upload_store, name="upload_store"),
        path("leases/<str:pk>/upload/", documents.upload_lease, name="upload_lease"),
        path("stores/<str:pk>/export.csv", documents.export_store, name="export_store"),
        path("leases/<str:pk>/export.csv", documents.export_lease, name="export_lease"),
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

grid_patterns = (
    [
        path("", dashboard.grid_page, name="page"),
        path("export.csv", dashboard.grid_export, name="export"),
    ],
    "grid",
)

arrears_patterns = (
    [
        path("", dashboard.arrears_page, name="page"),
        path("export.csv", dashboard.arrears_export, name="export"),
    ],
    "arrears",
)

calendar_patterns = (
    [
        path("", calendar.calendar_page, name="page"),
        path("new/", calendar.calendar_add, name="add"),
        # Public: the token in the path is the credential (PLAN-011b D8). Everything after
        # feed/ is captured, so every variant of the path reaches the view and gets its
        # silent 404 rather than an unresolved-path answer logged with the path.
        re_path(r"^feed/(?P<rest>[\s\S]*)\Z", calendar.calendar_feed, name="feed"),
        path("<str:pk>/", calendar.calendar_detail, name="detail"),
        path("<str:pk>/edit/", calendar.calendar_edit, name="edit"),
        path("<str:pk>/delete/", calendar.calendar_delete, name="delete"),
    ],
    "calendar",
)

urlpatterns = [
    path("", dashboard.home, name="home"),
    path("calendar/", include(calendar_patterns)),
    path("grid/", include(grid_patterns)),
    path("arrears/", include(arrears_patterns)),
    path("operators/", include(operator_patterns)),
    path("stores/", include(store_patterns)),
    path("leases/", include(lease_patterns)),
    path("run/", include(run_patterns)),
    path("charges/", include(charge_patterns)),
    path("one-off-charges/", include(one_off_charge_patterns)),
    path("payments/", include(payment_patterns)),
    path("webhooks/", include(webhook_patterns)),
    path("returns/", include(return_patterns)),
    # The operator's own statement behind a signed link, without a session (PLAN-011b D4).
    path("statement/<str:token>/", statement_links.statement_link_page, name="statement_link"),
    path("reconciliation/", include(reconciliation_patterns)),
    path("reports/", include(report_patterns)),
    path("documents/", include(document_patterns)),
    path("audit/", include(audit_patterns)),
]
