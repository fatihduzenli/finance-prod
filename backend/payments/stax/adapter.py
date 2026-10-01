"""``StaxProvider``: the :class:`~payments.provider.PaymentProvider` for Stax Pay.

Every path below is copied from ``docs/inputs/STAX_ENDPOINTS_2026-09-30.md`` (verified
by Fable against the specification pages, ``https://docs.staxpayments.com/reference/
<page>.md``, on 2026-09-30); the comment beside each constant names the page. A test
fails if a path used here is missing from that file.

Order inside every method: flag check (``pay_invoice`` only, D3), local validation
(nothing invalid leaves the process), the failure budget (debits only, D8), then the
transport, which applies the caller check, breaker, limiter and retry policy.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from payments.errors import (
    FeatureDisabled,
    IdentityMismatch,
    ProviderRejected,
    ValidationFailed,
)
from payments.provider import (
    AchRejection,
    CustomerInput,
    CustomerQuery,
    CustomerRecord,
    DateRange,
    DepositDetail,
    DepositRecord,
    InvoiceInput,
    InvoiceQuery,
    InvoiceRecord,
    InvoiceUpdate,
    OfflinePayment,
    PaymentMethodRecord,
    PaymentProvider,
    PayWithStoredMethod,
    ProviderIdentity,
    ScheduleInput,
    ScheduleQuery,
    ScheduleRecord,
    ScheduleUpdate,
    TransactionQuery,
    TransactionRecord,
    WebhookRecord,
)
from payments.stax import mapping, validation
from payments.stax.transport import Method, StaxTransport

PROVIDER_NAME = "stax"
#: TODO-VERIFY-016: the accepted values of {method} on mark-paid; these two are assumed.
OFFLINE_METHODS: tuple[str, ...] = ("cash", "check")
NOT_FOUND = 404

# --- Paths (page name from docs/inputs/STAX_ENDPOINTS_2026-09-30.md) ---------------------
CUSTOMERS = "/customer"  # create-customer, find-all-customers
CUSTOMER = "/customer/{id}"  # update-customer-info, get-a-customers-information
INVOICES = "/invoice"  # create-an-invoice, get-all-invoices
INVOICE = "/invoice/{id}"  # update-an-invoice, get-an-invoice, delete-an-invoice
INVOICE_SEND_EMAIL = "/invoice/{id}/send/email"  # send-an-invoice-via-email
INVOICE_PAY = "/invoice/{id}/pay"  # pay-an-invoice
#: mark-an-invoice-paid-in-cash-or-check; ``{method}`` is one of ``OFFLINE_METHODS``.
INVOICE_PAY_OFFLINE = "/invoice/{id}/pay/{method}"
TRANSACTIONS = "/transaction"  # list-and-filter-all-transactions
TRANSACTION = "/transaction/{id}"  # get-a-transactions-information
TRANSACTION_REFUND = "/transaction/{id}/refund"  # refund-transaction
TRANSACTION_VOID = "/transaction/{id}/void"  # void-transaction
DEPOSITS = "/query/deposit"  # get-list-of-deposits
DEPOSIT_DETAIL = "/query/depositDetail"  # get-detail-of-specific-deposit
SCHEDULE_CREATE = "/invoice/schedule/"  # create-a-scheduled-invoice (trailing slash as specified)
SCHEDULES = "/invoice/schedule"  # retrieve-all-scheduled-invoices
SCHEDULE = "/invoice/schedule/{id}"  # edit-an-invoice-schedule, delete-invoice-schedule
ACH_REJECTIONS = "/query/statement/v3/ach-rejects"  # ach-rejections
SELF = "/self"  # get-self-and-teams-info
#: get-all-payment-methods-for-a-customer; ``{customerId}`` is sent as the transport's
#: ``{id}`` (see :data:`_CUSTOMER_PAYMENT_METHODS_TEMPLATE`).
CUSTOMER_PAYMENT_METHODS = "/customer/{customerId}/payment-method"
PAYMENT_METHOD = "/payment-method/{id}"  # get-a-payment-method-by-id
WEBHOOKS = "/webhook"  # get-all-webhooks, create-new-webhook-for-all-of-my-users-merchants
WEBHOOK = "/webhook/{id}"  # get-webhook-by-id (titled "Delete a Webhook")
_CUSTOMER_PAYMENT_METHODS_TEMPLATE = CUSTOMER_PAYMENT_METHODS.replace("{customerId}", "{id}")

#: The required `url` body field of create-an-invoice and create-a-scheduled-invoice.
#: Confirmed 2026-10-01 from the field description of create-an-invoice ("Set it to
#: https://app.staxpayments.com/#/bill/ ..."); TODO-VERIFY-027 closed.
DEFAULT_PAYMENT_PAGE_URL = "https://app.staxpayments.com/#/bill/"


class StaxProvider(PaymentProvider):
    def __init__(
        self,
        transport: StaxTransport | None = None,
        *,
        payment_page_url: str = DEFAULT_PAYMENT_PAGE_URL,
        now: Callable[[], datetime] = timezone.now,
    ) -> None:
        self.transport = transport if transport is not None else StaxTransport()
        self.payment_page_url = payment_page_url
        self.now = now

    # --- Customers -----------------------------------------------------------------

    def _validate_customer(self, customer: CustomerInput) -> CustomerInput:
        """Refuse what Stax would reject; the result carries the normalised phone."""
        validation.email("operator_email", customer.operator_email)
        validation.emails("cc_emails", customer.cc_emails)
        return validation.customer(customer)

    def create_customer(self, customer: CustomerInput) -> CustomerRecord:
        """POST /customer (create-customer). Not idempotent: no retry after 5xx or timeout."""
        customer = self._validate_customer(customer)
        data = self.transport.send(
            Method.POST, CUSTOMERS, json=mapping.customer_body(customer), idempotent=False
        )
        return mapping.customer_record(data, self.now())

    def update_customer(self, external_id: str, customer: CustomerInput) -> CustomerRecord:
        """PUT /customer/{id} (update-customer-info)."""
        validation.external_id("external_id", external_id)
        customer = self._validate_customer(customer)
        data = self.transport.send(
            Method.PUT,
            CUSTOMER,
            object_id=external_id,
            json=mapping.customer_body(customer),
            idempotent=True,
        )
        return mapping.customer_record(data, self.now())

    def get_customer(self, external_id: str) -> CustomerRecord:
        """GET /customer/{id} (get-a-customers-information)."""
        validation.external_id("external_id", external_id)
        data = self.transport.send(Method.GET, CUSTOMER, object_id=external_id, idempotent=True)
        return mapping.customer_record(data, self.now())

    def find_customers(self, query: CustomerQuery) -> list[CustomerRecord]:
        """GET /customer (find-all-customers). First page only (TODO-VERIFY-021)."""
        if query.operator_email:
            validation.email("operator_email", query.operator_email)
        data = self.transport.send(
            Method.GET, CUSTOMERS, params=mapping.customer_params(query), idempotent=True
        )
        fetched_at = self.now()
        return [mapping.customer_record(item, fetched_at) for item in mapping.as_list(data)]

    # --- Payment methods (read only; no method is ever created here) -------------

    def list_payment_methods(self, customer_external_id: str) -> list[PaymentMethodRecord]:
        """GET /customer/{customerId}/payment-method (get-all-payment-methods-for-a-customer).

        An object whose ``customer_id`` names a different customer is dropped. Only the
        fields of :class:`PaymentMethodRecord` are kept. First page only
        (TODO-VERIFY-021).
        """
        validation.external_id("customer_external_id", customer_external_id)
        data = self.transport.send(
            Method.GET,
            _CUSTOMER_PAYMENT_METHODS_TEMPLATE,
            object_id=customer_external_id,
            idempotent=True,
        )
        return mapping.customer_payment_method_records(data, customer_external_id, self.now())

    def get_payment_method(self, external_id: str) -> PaymentMethodRecord:
        """GET /payment-method/{id} (get-a-payment-method-by-id)."""
        validation.external_id("external_id", external_id)
        data = self.transport.send(
            Method.GET, PAYMENT_METHOD, object_id=external_id, idempotent=True
        )
        return mapping.payment_method_record(data, self.now())

    # --- Invoices ------------------------------------------------------------------

    def create_invoice(self, invoice: InvoiceInput) -> InvoiceRecord:
        """POST /invoice (create-an-invoice). Not idempotent: no retry after 5xx or timeout."""
        validation.external_id("customer_external_id", invoice.customer_external_id)
        validation.amounts(invoice.amounts)
        data = self.transport.send(
            Method.POST,
            INVOICES,
            json=mapping.invoice_body(invoice, self.payment_page_url),
            idempotent=False,
        )
        return mapping.invoice_record(data, self.now())

    def update_invoice(self, external_id: str, update: InvoiceUpdate) -> InvoiceRecord:
        """PUT /invoice/{id} (update-an-invoice). Body fields: TODO-VERIFY-029."""
        validation.external_id("external_id", external_id)
        if update.amounts is not None:
            validation.amounts(update.amounts)
        data = self.transport.send(
            Method.PUT,
            INVOICE,
            object_id=external_id,
            json=mapping.invoice_update_body(update),
            idempotent=True,
        )
        return mapping.invoice_record(data, self.now())

    def get_invoice(self, external_id: str) -> InvoiceRecord:
        """GET /invoice/{id} (get-an-invoice)."""
        validation.external_id("external_id", external_id)
        data = self.transport.send(Method.GET, INVOICE, object_id=external_id, idempotent=True)
        return mapping.invoice_record(data, self.now())

    def list_invoices(self, query: InvoiceQuery) -> list[InvoiceRecord]:
        """GET /invoice (get-all-invoices). First page only (TODO-VERIFY-021)."""
        if query.operator_email:
            validation.email("operator_email", query.operator_email)
        if query.schedule_external_id:
            validation.external_id("schedule_external_id", query.schedule_external_id)
        data = self.transport.send(
            Method.GET, INVOICES, params=mapping.invoice_params(query), idempotent=True
        )
        fetched_at = self.now()
        return [mapping.invoice_record(item, fetched_at) for item in mapping.as_list(data)]

    def delete_invoice(self, external_id: str) -> None:
        """DELETE /invoice/{id} (delete-an-invoice)."""
        validation.external_id("external_id", external_id)
        self.transport.send(Method.DELETE, INVOICE, object_id=external_id, idempotent=True)

    def send_invoice_email(self, external_id: str, cc: list[str] | None = None) -> None:
        """PUT /invoice/{id}/send/email (send-an-invoice-via-email), body ``ccEmails``."""
        validation.external_id("external_id", external_id)
        cc_emails = validation.emails("cc", cc or [])
        self.transport.send(
            Method.PUT,
            INVOICE_SEND_EMAIL,
            object_id=external_id,
            json=mapping.send_email_body(cc_emails),
            idempotent=True,
        )

    def mark_invoice_paid_cash_or_check(
        self, external_id: str, payment: OfflinePayment
    ) -> InvoiceRecord:
        """POST /invoice/{id}/pay/{method} (mark-an-invoice-paid-in-cash-or-check).

        Treated as not idempotent: a repeat after an unanswered request could record
        the payment twice.
        """
        validation.external_id("external_id", external_id)
        validation.positive_amount("amount", payment.amount)
        if payment.method not in OFFLINE_METHODS:
            raise ValidationFailed("method must be one of the offline payment methods")
        data = self.transport.send(
            Method.POST,
            INVOICE_PAY_OFFLINE.replace("{method}", payment.method),
            object_id=external_id,
            params=mapping.offline_payment_params(payment),
            idempotent=False,
        )
        return mapping.invoice_record(data, self.now())

    def pay_invoice(self, external_id: str, payment: PayWithStoredMethod) -> TransactionRecord:
        """POST /invoice/{id}/pay (pay-an-invoice), behind STAX_PAY_INVOICE_ENABLED (D3).

        docs/CLAUDE.md Rule 2 forbids this call until TODO-VERIFY-019 is decided; the flag
        is off by default. The request carries ``idempotency_id``, so Stax deduplicates a
        repeat and a retry after a timeout or a 5xx is safe with the same identifier.
        """
        if not settings.STAX_PAY_INVOICE_ENABLED:
            raise FeatureDisabled("Paying an invoice from a stored method is disabled")
        validation.external_id("external_id", external_id)
        validation.external_id("payment_method_external_id", payment.payment_method_external_id)
        validation.idempotency_id(payment.idempotency_id)
        self.transport.budget.check_can_debit()
        data = self.transport.send(
            Method.POST,
            INVOICE_PAY,
            object_id=external_id,
            params=mapping.pay_params(payment),
            idempotent=True,
        )
        return mapping.transaction_record(data, self.now())

    # --- Transactions --------------------------------------------------------------

    def list_transactions(self, query: TransactionQuery) -> list[TransactionRecord]:
        """GET /transaction (list-and-filter-all-transactions)."""
        if query.date_range is not None:
            validation.date_range(query.date_range)
        for name, value in (
            ("customer_external_id", query.customer_external_id),
            ("payment_method_external_id", query.payment_method_external_id),
        ):
            if value:
                validation.external_id(name, value)
        validation.positive_int("page", query.page)
        validation.positive_int("per_page", query.per_page)
        data = self.transport.send(
            Method.GET, TRANSACTIONS, params=mapping.transaction_params(query), idempotent=True
        )
        fetched_at = self.now()
        return [mapping.transaction_record(item, fetched_at) for item in mapping.as_list(data)]

    def get_transaction(self, external_id: str) -> TransactionRecord:
        """GET /transaction/{id} (get-a-transactions-information)."""
        validation.external_id("external_id", external_id)
        data = self.transport.send(Method.GET, TRANSACTION, object_id=external_id, idempotent=True)
        return mapping.transaction_record(data, self.now())

    def refund_transaction(self, external_id: str, amount: Decimal) -> TransactionRecord:
        """POST /transaction/{id}/refund (refund-transaction), body ``total`` as a string.

        Treated as not idempotent: a repeated partial refund would refund twice.
        """
        validation.external_id("external_id", external_id)
        validation.positive_amount("amount", amount)
        data = self.transport.send(
            Method.POST,
            TRANSACTION_REFUND,
            object_id=external_id,
            json=mapping.refund_body(amount),
            idempotent=False,
        )
        return mapping.transaction_record(data, self.now())

    def void_transaction(self, external_id: str) -> TransactionRecord:
        """POST /transaction/{id}/void (void-transaction)."""
        validation.external_id("external_id", external_id)
        data = self.transport.send(
            Method.POST, TRANSACTION_VOID, object_id=external_id, idempotent=True
        )
        return mapping.transaction_record(data, self.now())

    # --- Deposits and rejections ---------------------------------------------------

    def list_deposits(self, date_range: DateRange) -> list[DepositRecord]:
        """GET /query/deposit (get-list-of-deposits), ``startDate`` and ``endDate``."""
        validation.date_range(date_range)
        data = self.transport.send(
            Method.GET, DEPOSITS, params=mapping.statement_params(date_range), idempotent=True
        )
        fetched_at = self.now()
        return [mapping.deposit_record(item, fetched_at) for item in mapping.as_list(data)]

    def get_deposit(self, date_range: DateRange, batch_id: str) -> DepositDetail:
        """GET /query/depositDetail (get-detail-of-specific-deposit), with ``batchId``."""
        validation.date_range(date_range)
        validation.external_id("batch_id", batch_id)
        params: mapping.Params = {**mapping.statement_params(date_range), "batchId": batch_id}
        data = self.transport.send(Method.GET, DEPOSIT_DETAIL, params=params, idempotent=True)
        # TODO-VERIFY-030: the detail response is assumed to be a list of rows.
        return DepositDetail(
            external_id=batch_id,
            raw_status="",
            fetched_at=self.now(),
            items=tuple(mapping.summary(item) for item in mapping.as_list(data)),
        )

    def list_ach_rejections(self, date_range: DateRange) -> list[AchRejection]:
        """GET /query/statement/v3/ach-rejects (ach-rejections)."""
        validation.date_range(date_range)
        data = self.transport.send(
            Method.GET,
            ACH_REJECTIONS,
            params=mapping.statement_params(date_range),
            idempotent=True,
        )
        fetched_at = self.now()
        return [mapping.ach_rejection(item, fetched_at) for item in mapping.as_list(data)]

    # --- Schedules -----------------------------------------------------------------

    def create_scheduled_invoice(self, schedule: ScheduleInput) -> ScheduleRecord:
        """POST /invoice/schedule/ (create-a-scheduled-invoice). Not idempotent."""
        validation.external_id("customer_external_id", schedule.customer_external_id)
        validation.external_id("payment_method_external_id", schedule.payment_method_external_id)
        validation.amounts(schedule.amounts)
        validation.rule(schedule.rule)
        data = self.transport.send(
            Method.POST,
            SCHEDULE_CREATE,
            json=mapping.schedule_body(schedule, self.payment_page_url),
            idempotent=False,
        )
        return mapping.schedule_record(data, self.now())

    def get_scheduled_invoice(self, external_id: str) -> ScheduleRecord:
        """One schedule, found in GET /invoice/schedule (retrieve-all-scheduled-invoices).

        TODO-VERIFY-020: the specification has no GET /invoice/schedule/{id}; the list is
        read (first page only, TODO-VERIFY-021) and filtered here by ID. Not found raises
        :class:`ProviderRejected` with status 404, as a missing object would.
        """
        validation.external_id("external_id", external_id)
        for schedule in self.list_scheduled_invoices(ScheduleQuery()):
            if schedule.external_id == external_id:
                return schedule
        raise ProviderRejected(NOT_FOUND, "The schedule was not found in the provider's list")

    def list_scheduled_invoices(self, query: ScheduleQuery) -> list[ScheduleRecord]:
        """GET /invoice/schedule (retrieve-all-scheduled-invoices)."""
        if query.customer_external_id:
            validation.external_id("customer_external_id", query.customer_external_id)
        data = self.transport.send(
            Method.GET, SCHEDULES, params=mapping.schedule_params(query), idempotent=True
        )
        fetched_at = self.now()
        return [mapping.schedule_record(item, fetched_at) for item in mapping.as_list(data)]

    def update_scheduled_invoice(self, external_id: str, update: ScheduleUpdate) -> ScheduleRecord:
        """PUT /invoice/schedule/{id} (edit-an-invoice-schedule): never ``total``/``meta`` (D2)."""
        validation.external_id("external_id", external_id)
        if update.rule is not None:
            validation.rule(update.rule)
        body = mapping.schedule_update_body(update)
        if not body:
            raise ValidationFailed("update must change at least one field")
        data = self.transport.send(
            Method.PUT, SCHEDULE, object_id=external_id, json=body, idempotent=True
        )
        return mapping.schedule_record(data, self.now())

    def delete_scheduled_invoice(self, external_id: str) -> None:
        """DELETE /invoice/schedule/{id} (delete-invoice-schedule)."""
        validation.external_id("external_id", external_id)
        self.transport.send(Method.DELETE, SCHEDULE, object_id=external_id, idempotent=True)

    # --- Webhook registrations (PLAN-008 D9; the registration commands only) ---------

    def list_webhooks(self) -> list[WebhookRecord]:
        """GET /webhook (get-all-webhooks). First page only (TODO-VERIFY-021)."""
        data = self.transport.send(Method.GET, WEBHOOKS, idempotent=True)
        fetched_at = self.now()
        return [mapping.webhook_record(item, fetched_at) for item in mapping.as_list(data)]

    def create_webhook(self, target_url: str, event: str) -> WebhookRecord:
        """POST /webhook (create-new-webhook-for-all-of-my-users-merchants), query
        ``target_url`` and ``event``. Not idempotent: a repeat could register twice."""
        validation.webhook_target_url(target_url)
        validation.external_id("event", event)
        data = self.transport.send(
            Method.POST,
            WEBHOOKS,
            params=mapping.webhook_params(target_url, event),
            idempotent=False,
        )
        return mapping.webhook_record(data, self.now())

    def delete_webhook(self, external_id: str) -> None:
        """DELETE /webhook/{id} (get-webhook-by-id, titled "Delete a Webhook")."""
        validation.external_id("external_id", external_id)
        self.transport.send(Method.DELETE, WEBHOOK, object_id=external_id, idempotent=True)

    # --- Identity ------------------------------------------------------------------

    def ping(self) -> ProviderIdentity:
        """GET /self (get-self-and-teams-info): the authentication check (D11, Rule 6).

        Raises :class:`IdentityMismatch` when ``STAX_EXPECTED_MERCHANT_ID`` is set and
        differs from the merchant the key belongs to.
        """
        data = self.transport.send(Method.GET, SELF, idempotent=True)
        merchant_id = mapping.merchant_id(data)
        expected = settings.STAX_EXPECTED_MERCHANT_ID
        if expected and merchant_id != expected:
            raise IdentityMismatch(merchant_id)
        return ProviderIdentity(
            provider_name=PROVIDER_NAME,
            environment=settings.STAX_ENV,
            merchant_id=merchant_id,
            matches_expected=(merchant_id == expected) if expected else None,
        )
