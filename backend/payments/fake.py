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

from payments.errors import FeatureDisabled, ProviderError, ProviderRejected
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
    require_money,
)

NOT_FOUND = 404


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
        self._failures: dict[str, ProviderError] = {}
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
        record = InvoiceRecord(
            external_id=self._next_id("invoice"),
            raw_status="FAKE-OPEN",
            fetched_at=now,
            customer_external_id=invoice.customer_external_id,
            total=invoice.amounts.total,
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
        """Every invoice, or those whose summary names the queried schedule."""
        self._call("list_invoices", query)
        return [
            record
            for record in self.invoices.values()
            if not query.schedule_external_id
            or record.raw_summary.get("schedule_id") == query.schedule_external_id
        ]

    def delete_invoice(self, external_id: str) -> None:
        self._call("delete_invoice", external_id)
        self._get(self.invoices, external_id)
        del self.invoices[external_id]

    def send_invoice_email(self, external_id: str, cc: list[str] | None = None) -> None:
        self._call("send_invoice_email", external_id, tuple(cc or ()))
        self._get(self.invoices, external_id)

    def mark_invoice_paid_cash_or_check(
        self, external_id: str, payment: OfflinePayment
    ) -> InvoiceRecord:
        now = self._call("mark_invoice_paid_cash_or_check", external_id, payment)
        record = replace(
            self._get(self.invoices, external_id), raw_status="FAKE-PAID", fetched_at=now
        )
        self.invoices[external_id] = record
        return record

    def pay_invoice(self, external_id: str, payment: PayWithStoredMethod) -> TransactionRecord:
        if not self.pay_invoice_enabled:
            raise FeatureDisabled("Paying an invoice from a stored method is disabled")
        now = self._call("pay_invoice", external_id, payment)
        invoice = self._get(self.invoices, external_id)
        record = TransactionRecord(
            external_id=self._next_id("transaction"),
            raw_status="FAKE-PENDING",
            fetched_at=now,
            total=invoice.total,
        )
        self.transactions[record.external_id] = record
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
        self._get(self.transactions, external_id)
        record = TransactionRecord(
            external_id=self._next_id("transaction"),
            raw_status="FAKE-REFUNDED",
            fetched_at=now,
            total=amount,
        )
        self.transactions[record.external_id] = record
        return record

    def void_transaction(self, external_id: str) -> TransactionRecord:
        now = self._call("void_transaction", external_id)
        record = replace(
            self._get(self.transactions, external_id), raw_status="FAKE-VOIDED", fetched_at=now
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

    # --- Identity ------------------------------------------------------------------

    def ping(self) -> ProviderIdentity:
        self._call("ping")
        return ProviderIdentity(
            provider_name="fake",
            environment="fake",
            merchant_id="fake-merchant-0001",
            matches_expected=None,
        )
