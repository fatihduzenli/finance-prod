"""Ledger routes (PLAN-006-operators.md §4.1, PLAN-007-leases-and-rent-run.md §4.1,
PLAN-007b-stores.md §1, PLAN-008-webhooks.md D6, D8, PLAN-009-payments-ledger.md D2, D4,
D6 to D8, PLAN-010-worker-and-reconciliation.md D10, PLAN-010b D5 to D8, D11, PLAN-010c
D7, D10, PLAN-010d D1, D2, D4, D6, D8, PLAN-011 D0 to D5, PLAN-011b D4, D7, D8, PLAN-012 D2,
D3, D6 to D8, PLAN-012e D4 to D7, D10, D11, PLAN-012d D2, D3, D17).

Namespace ``ledger``.

Every unsafe route here has a rule in ``accounts.registry``; the read-only ones are
listed there too, so ``backend/tests/test_route_registry.py`` sees them classified.
There is no delete route for a record: archiving is the only way to retire an operator,
and a store is closed through its status. A document's delete is soft (PLAN-010c D7), and so
is a contact log entry's (PLAN-012 D2).

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
    disputes,
    documents,
    installments,
    invoices,
    lease_ends,
    leases,
    one_off_charges,
    operators,
    payments,
    reconciliation,
    reports,
    returns,
    run,
    search,
    statement_links,
    stores,
    transfers,
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
        # Replaced by the invoice builder (PLAN-012 D6): answers 410 Gone.
        path("<str:pk>/charges/new/", one_off_charges.create_form_gone, name="new_charge"),
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
        # The store's contact log (PLAN-012 D3).
        path("<str:pk>/contacts/", contacts.store_contacts_page, name="contacts"),
        path("<str:pk>/contacts/new/", contacts.store_contacts_add, name="contacts_add"),
        # The end-of-lease wizard (PLAN-012e D4): start or resume.
        path("<str:pk>/end-lease/", lease_ends.store_end_lease, name="end_lease"),
        # The transfer wizard (PLAN-012e D6): start or resume.
        path("<str:pk>/transfer/", transfers.store_transfer, name="transfer"),
        # The workspace's action bar (PLAN-012d D2, D6): Record a payment, Schedule a
        # payment and Charge now.
        path("<str:pk>/payments/new/", stores.store_record_payment, name="record_payment"),
        path("<str:pk>/schedule-payment/", stores.store_schedule_payment, name="schedule_payment"),
        path("<str:pk>/charge-now/", stores.store_charge_now_pick, name="charge_now_pick"),
        # PLAN-012d D13: Create missing charges (the rent run for this store).
        path("<str:pk>/create-charges/", stores.store_create_charges, name="create_charges"),
    ],
    "stores",
)

# The end-of-lease wizard's steps (PLAN-012e D4, D5, D7), keyed by the draft.
lease_end_patterns = (
    [
        path("<str:pk>/terms/", lease_ends.terms, name="terms"),
        path("<str:pk>/blockers/", lease_ends.blockers, name="blockers"),
        path("<str:pk>/statement/", lease_ends.statement, name="statement"),
        path("<str:pk>/settlement/", lease_ends.settlement, name="settlement"),
        path("<str:pk>/confirm/", lease_ends.confirm, name="confirm"),
        path("<str:pk>/cancel/", lease_ends.cancel, name="cancel"),
        path("<str:pk>/statement.pdf", lease_ends.pdf, name="pdf"),
    ],
    "lease_ends",
)

# The transfer wizard's steps (PLAN-012e D6), keyed by the transfer.
transfer_patterns = (
    [
        path("<str:pk>/operator/", transfers.operator, name="operator"),
        path("<str:pk>/end/", transfers.end, name="end"),
        path("<str:pk>/blockers/", transfers.blockers, name="blockers"),
        path("<str:pk>/incoming/", transfers.incoming, name="incoming"),
        path("<str:pk>/confirm/", transfers.confirm, name="confirm"),
        path("<str:pk>/cancel/", transfers.cancel, name="cancel"),
        path("<str:pk>/allow-balance/", transfers.override_debt, name="override_debt"),
    ],
    "transfers",
)

# One contact log entry (PLAN-012 D2): the author's edit, a correction, an admin's delete;
# Kept, Reschedule or Broken on an open promise (PLAN-012c-b D2).
contact_patterns = (
    [
        path("<str:pk>/edit/", contacts.contact_edit, name="edit"),
        path("<str:pk>/correct/", contacts.contact_correct, name="correct"),
        path("<str:pk>/delete/", contacts.contact_delete, name="delete"),
        re_path(
            r"^(?P<pk>[^/]+)/outcome/(?P<outcome>kept|rescheduled|broken)/\Z",
            contacts.contact_outcome,
            name="outcome",
        ),
    ],
    "contacts",
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
        path("<str:pk>/stop-autopay/", leases.lease_stop_autopay, name="stop_autopay"),
        path(
            "<str:pk>/setup-charges/new/",
            one_off_charges.lease_add_setup_charge,
            name="add_setup_charge",
        ),
        # Replaced by the invoice builder (PLAN-012 D6): answers 410 Gone.
        path("<str:pk>/charges/new/", one_off_charges.create_form_gone, name="new_charge"),
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
        path("<str:pk>/retry-release/", charges.charge_retry_release, name="retry_release"),
        path("<str:pk>/link-invoice/", charges.charge_link_invoice, name="link_invoice"),
        path("<str:pk>/send-step/", collections.rent_charge_send_step, name="send_step"),
        path("<str:pk>/split/", installments.charge_split, name="split"),
    ],
    "charges",
)

# Installment plans (PLAN-012e D10, D11).
plan_patterns = (
    [
        path("<str:pk>/", installments.plan_detail, name="detail"),
        path("<str:pk>/reschedule/", installments.plan_reschedule, name="reschedule"),
        path("<str:pk>/cancel/", installments.plan_cancel, name="cancel"),
    ],
    "plans",
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
        path("<str:pk>/split/", installments.one_off_split, name="split"),
    ],
    "one_off_charges",
)

# The invoice builder (PLAN-012 D6, D7).
invoice_patterns = (
    [
        path("new/store/<str:pk>/", invoices.new_store, name="new_store"),
        path("new/operator/<str:pk>/", invoices.new_operator, name="new_operator"),
        path("rent-amount/", invoices.rent_amount, name="rent_amount"),
        path("drafts/<str:pk>/", invoices.draft_page, name="draft"),
        path("drafts/<str:pk>/edit/", invoices.draft_edit, name="edit"),
        path("drafts/<str:pk>/preview/", invoices.draft_preview, name="preview"),
        path("drafts/<str:pk>/print/", invoices.draft_print, name="print"),
        path("drafts/<str:pk>/send/", invoices.draft_send, name="send"),
        path("drafts/<str:pk>/charge-now/", invoices.draft_charge_now, name="charge_now"),
        path("drafts/<str:pk>/cancel/", invoices.draft_cancel, name="cancel"),
        path("drafts/<str:pk>/delete/", invoices.draft_delete, name="delete_draft"),
    ],
    "invoices",
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

dispute_patterns = (
    [
        path("", disputes.disputes_page, name="page"),
        path("export.csv", disputes.disputes_export, name="export"),
        path("refresh/", disputes.disputes_refresh, name="refresh"),
        path("<str:pk>/evidence/", disputes.dispute_evidence, name="evidence"),
        path("evidence/<str:pk>/retry/", disputes.dispute_evidence_retry, name="evidence_retry"),
    ],
    "disputes",
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
    # The top-bar search (PLAN-012d D17): any verified role, read only.
    path("search/", search.search_page, name="search"),
    path("calendar/", include(calendar_patterns)),
    path("grid/", include(grid_patterns)),
    path("arrears/", include(arrears_patterns)),
    path("operators/", include(operator_patterns)),
    path("stores/", include(store_patterns)),
    path("contacts/", include(contact_patterns)),
    path("leases/", include(lease_patterns)),
    path("lease-ends/", include(lease_end_patterns)),
    path("transfers/", include(transfer_patterns)),
    path("run/", include(run_patterns)),
    path("charges/", include(charge_patterns)),
    path("one-off-charges/", include(one_off_charge_patterns)),
    path("plans/", include(plan_patterns)),
    path("invoices/", include(invoice_patterns)),
    path("payments/", include(payment_patterns)),
    path("disputes/", include(dispute_patterns)),
    path("webhooks/", include(webhook_patterns)),
    path("returns/", include(return_patterns)),
    # The operator's own statement behind a signed link, without a session (PLAN-011b D4).
    path("statement/<str:token>/", statement_links.statement_link_page, name="statement_link"),
    path("reconciliation/", include(reconciliation_patterns)),
    path("reports/", include(report_patterns)),
    path("documents/", include(document_patterns)),
    path("audit/", include(audit_patterns)),
]
