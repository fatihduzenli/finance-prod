"""In-memory :class:`~payments.provider.PaymentProvider` for tests and local development.

Implements every method against dictionaries, records each call, and can be told to
fail. It knows no provider: identifiers are ``fake-<kind>-<n>`` and statuses are
invented placeholders. It never touches the network.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from django.utils import timezone

from payments.errors import FeatureDisabled, ProviderError, ProviderRejected, ValidationFailed
from payments.provider import (
    EVIDENCE_EXTENSIONS,
    EVIDENCE_MAX_BYTES,
    TRANSACTION_REFUND,
    AchRejection,
    CustomerInput,
    CustomerQuery,
    CustomerRecord,
    DateRange,
    DepositDetail,
    DepositRecord,
    DisputeEvidenceReceipt,
    DisputeQuery,
    DisputeRecord,
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
    require_money,
)

NOT_FOUND = 404
#: The status the services read as deleted (``payments.invoice_sync.PROVIDER_DELETED``).
DELETED = "DELETED"
#: The status the services read as sent (``payments.invoice_sync.PROVIDER_SENT``).
SENT = "SENT"
#: What the fake answers once a dispute's respond-by date has passed (PLAN-012 D8).
RESPOND_BY_PASSED = "The respond by date for this dispute has passed."
BAD_REQUEST = 400
#: The fake's list page size (PLAN-012 D8: the sync reads every page).
DISPUTES_PAGE_SIZE = 2


class FakeProvider(PaymentProvider):
    """Every call is appended to :attr:`calls` as ``(method_name, arguments)``."""

    def __init__(self, *, pay_invoice_enabled: bool = False) -> None:
        self.pay_invoice_enabled = pay_invoice_enabled
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.customers: dict[str, CustomerRecord] = {}
        self.invoices: dict[str, InvoiceRecord] = {}
        self.transactions: dict[str, TransactionRecord] = {}
        self.schedules: dict[str, ScheduleRecord] = {}
        #: Payment methods by ID, with the customer each belongs to.
        self.payment_methods: dict[str, tuple[str, PaymentMethodRecord]] = {}
        self.deposits: list[DepositRecord] = []
        self.ach_rejections: list[AchRejection] = []
        self.webhooks: dict[str, WebhookRecord] = {}
        #: Disputes by ID, listed by respond-by date; evidence uploads as received.
        self.disputes: dict[str, DisputeRecord] = {}
        self.evidence: list[tuple[str, str, bytes, str]] = []
        self._failures: dict[str, ProviderError] = {}
        #: Transactions made by ``pay_invoice``, by idempotency identifier.
        self._paid_by_key: dict[str, str] = {}
        self._counter = 0

    # --- Test controls -------------------------------------------------------------

    def fail_next(self, method_name: str, error: ProviderError) -> None:
        """Make the next call of ``method_name`` raise ``error`` (once)."""
        self._failures[method_name] = error

    def _call(self, method_name: str, *arguments: object) -> datetime:
        self.calls.append((method_name, arguments))
        error = self._failures.pop(method_name, None)
        if error is not None:
            raise error
        return timezone.now()

    def _next_id(self, kind: str) -> str:
        self._counter += 1
        return f"fake-{kind}-{self._counter}"

    @staticmethod
    def _get[T](store: dict[str, T], external_id: str) -> T:
        try:
            return store[external_id]
        except KeyError:
            raise ProviderRejected(NOT_FOUND) from None

    # --- Customers -----------------------------------------------------------------

    def create_customer(self, customer: CustomerInput) -> CustomerRecord:
        now = self._call("create_customer", customer)
        record = CustomerRecord(
            external_id=self._next_id("customer"),
            raw_status="",
            fetched_at=now,
            operator_email=customer.operator_email,
            company_name=customer.company_name,
            reference=customer.reference,
        )
        self.customers[record.external_id] = record
        return record

    def update_customer(self, external_id: str, customer: CustomerInput) -> CustomerRecord:
        now = self._call("update_customer", external_id, customer)
        record = replace(
            self._get(self.customers, external_id),
            fetched_at=now,
            operator_email=customer.operator_email,
            company_name=customer.company_name,
            reference=customer.reference,
        )
        self.customers[external_id] = record
        return record

    def get_customer(self, external_id: str) -> CustomerRecord:
        self._call("get_customer", external_id)
        return self._get(self.customers, external_id)

    def find_customers(self, query: CustomerQuery) -> list[CustomerRecord]:
        self._call("find_customers", query)
        return [
            record
            for record in self.customers.values()
            if (not query.operator_email or record.operator_email == query.operator_email)
            and (not query.company_name or record.company_name == query.company_name)
            and (not query.reference or record.reference == query.reference)
        ]

    # --- Payment methods -----------------------------------------------------------

    def add_payment_method(
        self, customer_external_id: str, record: PaymentMethodRecord
    ) -> PaymentMethodRecord:
        """Test control: a method staff entered in the provider's dashboard."""
        self.payment_methods[record.external_id] = (customer_external_id, record)
        return record

    def list_payment_methods(self, customer_external_id: str) -> list[PaymentMethodRecord]:
        self._call("list_payment_methods", customer_external_id)
        return [
            record
            for owner, record in self.payment_methods.values()
            if owner == customer_external_id
        ]

    def get_payment_method(self, external_id: str) -> PaymentMethodRecord:
        self._call("get_payment_method", external_id)
        return self._get(self.payment_methods, external_id)[1]

    # --- Invoices ------------------------------------------------------------------

    def create_invoice(self, invoice: InvoiceInput) -> InvoiceRecord:
        now = self._call("create_invoice", invoice)
        dates = {
            key: value.isoformat()
            for key, value in (
                ("invoice_date_at", invoice.invoice_date),
                ("due_at", invoice.due_on),
            )
            if value is not None
        }
        record = InvoiceRecord(
            external_id=self._next_id("invoice"),
            raw_status="FAKE-OPEN",
            fetched_at=now,
            customer_external_id=invoice.customer_external_id,
            total=invoice.amounts.total,
            raw_summary=dates,
            reference=invoice.reference,
        )
        self.invoices[record.external_id] = record
        return record

    def update_invoice(self, external_id: str, update: InvoiceUpdate) -> InvoiceRecord:
        now = self._call("update_invoice", external_id, update)
        record = self._get(self.invoices, external_id)
        total = update.amounts.total if update.amounts is not None else record.total
        record = replace(record, fetched_at=now, total=total)
        self.invoices[external_id] = record
        return record

    def get_invoice(self, external_id: str) -> InvoiceRecord:
        self._call("get_invoice", external_id)
        return self._get(self.invoices, external_id)

    def list_invoices(self, query: InvoiceQuery) -> list[InvoiceRecord]:
        """Every invoice, or those of the customers with the queried e-mail, or those
        that name the queried schedule."""
        self._call("list_invoices", query)
        customers = {
            record.external_id
            for record in self.customers.values()
            if record.operator_email == query.operator_email
        }
        return [
            record
            for record in self.invoices.values()
            if (
                not query.schedule_external_id
                or query.schedule_external_id
                in (record.schedule_id, record.raw_summary.get("schedule_id"))
            )
            and (not query.operator_email or record.customer_external_id in customers)
        ]

    def delete_invoice(self, external_id: str) -> None:
        """The invoice stays readable with the deleted status, as the provider keeps it."""
        now = self._call("delete_invoice", external_id)
        record = self._get(self.invoices, external_id)
        self.invoices[external_id] = replace(record, raw_status=DELETED, fetched_at=now)

    def send_invoice_email(self, external_id: str, cc: list[str] | None = None) -> None:
        now = self._call("send_invoice_email", external_id, tuple(cc or ()))
        record = self._get(self.invoices, external_id)
        self.invoices[external_id] = replace(record, raw_status=SENT, sent_at=now, fetched_at=now)

    def mark_invoice_paid_cash_or_check(
        self, external_id: str, payment: OfflinePayment
    ) -> InvoiceRecord:
        now = self._call("mark_invoice_paid_cash_or_check", external_id, payment)
        current = self._get(self.invoices, external_id)
        # The status the services read as paid (payments.invoice_sync.PROVIDER_PAID).
        record = replace(
            current, raw_status="PAID", fetched_at=now, paid_at=now, amount_paid=current.total
        )
        self.invoices[external_id] = record
        return record

    def pay_invoice(self, external_id: str, payment: PayWithStoredMethod) -> TransactionRecord:
        if not self.pay_invoice_enabled:
            raise FeatureDisabled("Paying an invoice from a stored method is disabled")
        now = self._call("pay_invoice", external_id, payment)
        invoice = self._get(self.invoices, external_id)
        # A repeat with the same idempotency identifier answers the first transaction, as
        # the provider deduplicates it.
        earlier = self._paid_by_key.get(payment.idempotency_id)
        if earlier is not None:
            return self.transactions[earlier]
        method = self.payment_methods.get(payment.payment_method_external_id)
        record = TransactionRecord(
            external_id=self._next_id("transaction"),
            raw_status="FAKE-PENDING",
            fetched_at=now,
            total=invoice.total,
            method=method[1].method if method is not None else "",
            kind="charge",
            invoice_external_id=external_id,
            customer_external_id=invoice.customer_external_id,
            success=True,
            created_at=now,
        )
        self.transactions[record.external_id] = record
        self._paid_by_key[payment.idempotency_id] = record.external_id
        return record

    # --- Transactions --------------------------------------------------------------

    def list_transactions(self, query: TransactionQuery) -> list[TransactionRecord]:
        self._call("list_transactions", query)
        return list(self.transactions.values())

    def get_transaction(self, external_id: str) -> TransactionRecord:
        self._call("get_transaction", external_id)
        return self._get(self.transactions, external_id)

    def refund_transaction(self, external_id: str, amount: Decimal) -> TransactionRecord:
        require_money("amount", amount)
        now = self._call("refund_transaction", external_id, amount)
        original = self._get(self.transactions, external_id)
        # A new transaction of type refund naming its parent, not settled yet (as the
        # webhook handlers read one, PLAN-008 D5).
        record = TransactionRecord(
            external_id=self._next_id("transaction"),
            raw_status="FAKE-REFUNDED",
            fetched_at=now,
            total=amount,
            method=original.method,
            kind=TRANSACTION_REFUND,
            invoice_external_id=original.invoice_external_id,
            customer_external_id=original.customer_external_id,
            parent_external_id=external_id,
            success=True,
            created_at=now,
        )
        self.transactions[record.external_id] = record
        return record

    def void_transaction(self, external_id: str) -> TransactionRecord:
        now = self._call("void_transaction", external_id)
        record = replace(
            self._get(self.transactions, external_id),
            raw_status="FAKE-VOIDED",
            fetched_at=now,
            is_voided=True,
        )
        self.transactions[external_id] = record
        return record

    # --- Deposits and rejections ---------------------------------------------------

    def list_deposits(self, date_range: DateRange) -> list[DepositRecord]:
        self._call("list_deposits", date_range)
        return list(self.deposits)

    def get_deposit(self, date_range: DateRange, batch_id: str) -> DepositDetail:
        now = self._call("get_deposit", date_range, batch_id)
        for deposit in self.deposits:
            if deposit.external_id == batch_id:
                return DepositDetail(external_id=batch_id, raw_status="", fetched_at=now)
        raise ProviderRejected(NOT_FOUND)

    def list_ach_rejections(self, date_range: DateRange) -> list[AchRejection]:
        self._call("list_ach_rejections", date_range)
        return list(self.ach_rejections)

    # --- Schedules -----------------------------------------------------------------

    def create_scheduled_invoice(self, schedule: ScheduleInput) -> ScheduleRecord:
        now = self._call("create_scheduled_invoice", schedule)
        record = ScheduleRecord(
            external_id=self._next_id("schedule"),
            raw_status="",
            fetched_at=now,
            customer_external_id=schedule.customer_external_id,
            total=schedule.amounts.total,
            rule=schedule.rule,
            active=True,
        )
        self.schedules[record.external_id] = record
        return record

    def get_scheduled_invoice(self, external_id: str) -> ScheduleRecord:
        self._call("get_scheduled_invoice", external_id)
        return self._get(self.schedules, external_id)

    def list_scheduled_invoices(self, query: ScheduleQuery) -> list[ScheduleRecord]:
        self._call("list_scheduled_invoices", query)
        return [
            record
            for record in self.schedules.values()
            if not query.customer_external_id
            or record.customer_external_id == query.customer_external_id
        ]

    def update_scheduled_invoice(self, external_id: str, update: ScheduleUpdate) -> ScheduleRecord:
        now = self._call("update_scheduled_invoice", external_id, update)
        record = self._get(self.schedules, external_id)
        record = replace(
            record,
            fetched_at=now,
            active=record.active if update.active is None else update.active,
            rule=record.rule if update.rule is None else update.rule,
        )
        self.schedules[external_id] = record
        return record

    def delete_scheduled_invoice(self, external_id: str) -> None:
        self._call("delete_scheduled_invoice", external_id)
        self._get(self.schedules, external_id)
        del self.schedules[external_id]

    # --- Disputes -----------------------------------------------------------------

    def add_dispute(self, record: DisputeRecord) -> DisputeRecord:
        """Test control: a dispute the provider reports."""
        self.disputes[record.external_id] = record
        return record

    def list_disputes(self, query: DisputeQuery) -> list[DisputeRecord]:
        """Every dispute created in the range (or without a creation date), of the
        queried status, by respond-by date, :data:`DISPUTES_PAGE_SIZE` a page."""
        now = self._call("list_disputes", query)
        start, end = query.date_range.start, query.date_range.end
        found = sorted(
            (
                replace(record, fetched_at=now)
                for record in self.disputes.values()
                if (not query.status or record.raw_status == query.status)
                and (record.created_at is None or start <= record.created_at.date() <= end)
            ),
            key=lambda record: (record.respond_by is None, record.respond_by, record.external_id),
        )
        first = (query.page - 1) * DISPUTES_PAGE_SIZE
        return found[first : first + DISPUTES_PAGE_SIZE]

    def upload_dispute_evidence(
        self, dispute_external_id: str, name: str, data: bytes, content_type: str
    ) -> DisputeEvidenceReceipt:
        now = self._call("upload_dispute_evidence", dispute_external_id, name, content_type)
        if not data or len(data) >= EVIDENCE_MAX_BYTES:
            raise ValidationFailed("file must be smaller than the provider's limit")
        if content_type not in EVIDENCE_EXTENSIONS:
            raise ValidationFailed("file must be a PDF, JPEG, PNG or TIFF")
        dispute = self._get(self.disputes, dispute_external_id)
        if dispute.respond_by is not None and dispute.respond_by < now:
            raise ProviderRejected(BAD_REQUEST, reason=RESPOND_BY_PASSED)
        self.evidence.append((dispute_external_id, name, data, content_type))
        # The status the provider reports once evidence is attached (PLAN-012 D8).
        self.disputes[dispute_external_id] = replace(
            dispute, raw_status="EVIDENCE_UPLOADED", updated_at=now, fetched_at=now
        )
        return DisputeEvidenceReceipt(
            external_id=self._next_id("dispute-file"),
            raw_status="",
            fetched_at=now,
            dispute_external_id=dispute_external_id,
        )

    # --- Webhook registrations -----------------------------------------------------

    def list_webhooks(self) -> list[WebhookRecord]:
        self._call("list_webhooks")
        return list(self.webhooks.values())

    def create_webhook(self, target_url: str, event: str) -> WebhookRecord:
        now = self._call("create_webhook", target_url, event)
        record = WebhookRecord(
            external_id=self._next_id("webhook"),
            raw_status="",
            fetched_at=now,
            event=event,
            target_url=target_url,
        )
        self.webhooks[record.external_id] = record
        return record

    def delete_webhook(self, external_id: str) -> None:
        self._call("delete_webhook", external_id)
        self._get(self.webhooks, external_id)
        del self.webhooks[external_id]

    # --- Identity ------------------------------------------------------------------

    def ping(self) -> ProviderIdentity:
        self._call("ping")
        return ProviderIdentity(
            provider_name="fake",
            environment="fake",
            merchant_id="fake-merchant-0001",
            matches_expected=None,
        )
