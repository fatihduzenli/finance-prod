"""The payment-provider interface, in this app's vocabulary (docs/CLAUDE.md Rule 8).

Contract: ``docs/qa/PLAN-005-stax-client.md`` §4.1. Nothing here knows a provider's
URLs, field names or status strings; the adapter maps these types to its own.

Every input and record is a frozen dataclass. Money is :class:`~decimal.Decimal` and
nothing else: a ``float``, an ``int`` or a string where an amount is expected raises
:class:`~payments.errors.ValidationFailed` when the object is built, so an inexact
amount cannot reach an adapter (D12). Every record carries ``external_id``,
``raw_status`` (the provider's string, unmodified, Rule 2) and ``fetched_at``.
``raw_summary`` holds scalar fields only, already trimmed per Rule 1 by the adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from payments.errors import ValidationFailed

#: A provider object reduced to scalar fields (docs/CLAUDE.md Rule 1).
Summary = Mapping[str, str | int | bool | None | Decimal | Mapping[str, str | None]]

#: Offline payment methods this app may use. The provider also offers a gift-card method,
#: which is never used (PLAN-009 header).
OfflineMethod = Literal["cash", "check"]

#: ``PaymentMethodRecord.method`` of a bank account, the only kind this app may use.
METHOD_BANK = "bank"
#: ``PaymentMethodRecord.raw_status`` of a method the provider marks deleted or purged.
PAYMENT_METHOD_DELETED = "deleted"


def require_money(name: str, value: object) -> None:
    """Raise :class:`ValidationFailed` unless ``value`` is a finite ``Decimal``."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValidationFailed(f"{name} must be a finite Decimal")


def mask_identifier(value: str) -> str:
    """First four and last four characters; anything shorter than nine is fully masked."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


# --- Inputs -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class LineItem:
    description: str
    quantity: Decimal
    unit_price: Decimal
    details: str = ""

    def __post_init__(self) -> None:
        require_money("quantity", self.quantity)
        require_money("unit_price", self.unit_price)

    @property
    def total(self) -> Decimal:
        return self.quantity * self.unit_price


@dataclass(frozen=True, slots=True)
class Amounts:
    """What is owed: ``total == subtotal + tax`` and the lines sum to ``subtotal``."""

    total: Decimal
    subtotal: Decimal
    tax: Decimal
    line_items: tuple[LineItem, ...]

    def __post_init__(self) -> None:
        require_money("total", self.total)
        require_money("subtotal", self.subtotal)
        require_money("tax", self.tax)


@dataclass(frozen=True, slots=True)
class CustomerInput:
    first_name: str
    last_name: str
    company_name: str
    operator_email: str
    phone: str = ""
    cc_emails: tuple[str, ...] = ()
    #: Our local ref, carried so a retry can find the record (Rule 5, TODO-VERIFY-004).
    reference: str = ""


@dataclass(frozen=True, slots=True)
class CustomerQuery:
    operator_email: str = ""
    company_name: str = ""
    reference: str = ""


@dataclass(frozen=True, slots=True)
class InvoiceInput:
    customer_external_id: str
    amounts: Amounts
    due_on: date | None = None
    invoice_date: date | None = None
    memo: str = ""
    #: Our local ref (Rule 5, TODO-VERIFY-004).
    reference: str = ""


@dataclass(frozen=True, slots=True)
class InvoiceUpdate:
    """Only the fields that are not ``None`` are sent."""

    amounts: Amounts | None = None
    due_on: date | None = None
    memo: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceQuery:
    operator_email: str = ""
    company_name: str = ""
    schedule_external_id: str = ""


@dataclass(frozen=True, slots=True)
class OfflinePayment:
    """Marks a whole invoice paid outside the provider (PLAN-009 D1). ``amount`` is checked
    locally and never sent: a partial amount cannot be expressed yet (TODO-VERIFY-028)."""

    method: OfflineMethod
    amount: Decimal
    check_number: str = ""
    note: str = ""
    email_receipt: bool = False

    def __post_init__(self) -> None:
        require_money("amount", self.amount)


@dataclass(frozen=True, slots=True)
class PayWithStoredMethod:
    payment_method_external_id: str
    #: A UUID string, reused unchanged on every retry of the same payment.
    idempotency_id: str
    email_receipt: bool = False


@dataclass(frozen=True, slots=True)
class TransactionQuery:
    date_range: DateRange | None = None
    customer_external_id: str = ""
    payment_method_external_id: str = ""
    page: int | None = None
    per_page: int | None = None


@dataclass(frozen=True, slots=True)
class ScheduleInput:
    customer_external_id: str
    payment_method_external_id: str
    amounts: Amounts
    #: RRULE string beginning with ``DTSTART=``.
    rule: str
    email_notification: bool = False
    #: A reference the schedule's owner carries (PLAN-010d D2, TODO-VERIFY-035); empty:
    #: none is sent.
    reference: str = ""


@dataclass(frozen=True, slots=True)
class ScheduleQuery:
    customer_external_id: str = ""


@dataclass(frozen=True, slots=True)
class DisputeQuery:
    """The provider's disputes created in ``date_range`` (PLAN-012 D8); ``status`` empty:
    every status; ``page`` from 1."""

    date_range: DateRange
    status: str = ""
    page: int = 1


#: What ``upload_dispute_evidence`` accepts (PLAN-012 D8): less than this many bytes ...
EVIDENCE_MAX_BYTES = 10_000_000
#: ... of these types, each sent with this extension.
EVIDENCE_EXTENSIONS: Mapping[str, str] = {
    "application/pdf": "pdf",
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/tiff": "tiff",
}
#: Longest evidence name sent.
EVIDENCE_NAME_MAX_LENGTH = 150


@dataclass(frozen=True, slots=True)
class ScheduleUpdate:
    """D2: amounts and metadata are never edited, so this type has no field for them."""

    active: bool | None = None
    rule: str | None = None
    email_notification: bool | None = None


# --- Records ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CustomerRecord:
    external_id: str
    raw_status: str
    fetched_at: datetime
    operator_email: str = ""
    company_name: str = ""
    reference: str = ""
    raw_summary: Summary = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InvoiceRecord:
    external_id: str
    raw_status: str
    fetched_at: datetime
    customer_external_id: str = ""
    total: Decimal | None = None
    raw_summary: Summary = field(default_factory=dict)
    #: When the provider sent, the operator viewed and the provider marked paid the
    #: invoice, and the amount it reports paid; ``None`` when unknown (PLAN-008 D4).
    sent_at: datetime | None = None
    viewed_at: datetime | None = None
    paid_at: datetime | None = None
    amount_paid: Decimal | None = None
    #: The reference the invoice was created with (our invoice number, PLAN-010d D5;
    #: TODO-VERIFY-004), and the schedule that generated it; ``""`` when none or unknown.
    reference: str = ""
    schedule_id: str = ""


#: ``TransactionRecord.kind`` of a refund (PLAN-008 D5).
TRANSACTION_REFUND = "refund"
#: ``TransactionRecord.kind`` of a void.
TRANSACTION_VOID = "void"


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    """A provider transaction (PLAN-008 D4, D5).

    ``method`` is the provider's string (:data:`METHOD_BANK` for ACH). ``kind`` is the
    provider's transaction type (``charge``, :data:`TRANSACTION_REFUND`, ...). A refund
    names the transaction it refunds in ``parent_external_id``. ``settled_at`` is set
    once the provider reports the transaction settled; ``returned`` is true when the
    provider reports an ACH return, whose code and reason (already trimmed per Rule 1)
    are in ``return_code`` and ``return_reason``.
    """

    external_id: str
    raw_status: str
    fetched_at: datetime
    total: Decimal | None = None
    raw_summary: Summary = field(default_factory=dict)
    method: str = ""
    kind: str = ""
    invoice_external_id: str = ""
    customer_external_id: str = ""
    parent_external_id: str = ""
    success: bool | None = None
    is_voided: bool = False
    created_at: datetime | None = None
    settled_at: datetime | None = None
    returned: bool = False
    return_code: str = ""
    return_reason: str = ""


@dataclass(frozen=True, slots=True)
class DepositRecord:
    external_id: str
    raw_status: str
    fetched_at: datetime
    raw_summary: Summary = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DepositDetail:
    external_id: str
    raw_status: str
    fetched_at: datetime
    items: tuple[Summary, ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    external_id: str
    raw_status: str
    fetched_at: datetime
    customer_external_id: str = ""
    total: Decimal | None = None
    rule: str = ""
    active: bool | None = None
    raw_summary: Summary = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaymentMethodRecord:
    """A stored payment method, reduced to what staff may see (PLAN-007 D1, Rule 1).

    Nothing else of the provider's object is kept: no holder name, bank name, expiry,
    account or routing data, and no summary. ``method`` is the provider's string
    (``bank`` or ``card``); only :data:`METHOD_BANK` may ever be used. ``raw_status`` is
    :data:`PAYMENT_METHOD_DELETED` when the provider marks the method deleted or purged,
    and empty otherwise (the object has no status field).
    """

    external_id: str
    raw_status: str
    fetched_at: datetime
    method: str = ""
    last_four: str = ""
    bank_type: str = ""
    nickname: str = ""
    is_default: bool = False

    @property
    def is_usable_bank(self) -> bool:
        return self.method == METHOD_BANK and self.raw_status != PAYMENT_METHOD_DELETED


@dataclass(frozen=True, slots=True)
class AchRejection:
    external_id: str
    raw_status: str
    fetched_at: datetime
    amount: Decimal | None = None
    raw_summary: Summary = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DisputeRecord:
    """A dispute (PLAN-012 D8). ``raw_status`` and ``reason`` are the provider's strings,
    unmodified (Rule 2); ``raw_summary`` holds the documented scalar fields only, never
    an identifier."""

    external_id: str
    raw_status: str
    fetched_at: datetime
    transaction_external_id: str = ""
    reason: str = ""
    amount: Decimal | None = None
    respond_by: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw_summary: Summary = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DisputeEvidenceReceipt:
    """An evidence file the provider accepted; ``external_id`` is ``""`` when the
    provider's answer names none."""

    external_id: str
    raw_status: str
    fetched_at: datetime
    dispute_external_id: str = ""


@dataclass(frozen=True, slots=True)
class WebhookRecord:
    """A webhook registration (PLAN-008 D9). ``target_url`` carries the URL token and is
    kept out of ``repr``; callers never print it."""

    external_id: str
    raw_status: str
    fetched_at: datetime
    event: str = ""
    target_url: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    provider_name: str
    environment: str
    merchant_id: str = field(repr=False)
    #: ``None`` when no expected merchant is configured.
    matches_expected: bool | None


# --- Interface --------------------------------------------------------------------


class PaymentProvider(ABC):
    """Every outbound payment-provider call. All methods are synchronous."""

    @abstractmethod
    def create_customer(self, customer: CustomerInput) -> CustomerRecord: ...

    @abstractmethod
    def update_customer(self, external_id: str, customer: CustomerInput) -> CustomerRecord: ...

    @abstractmethod
    def get_customer(self, external_id: str) -> CustomerRecord: ...

    @abstractmethod
    def find_customers(self, query: CustomerQuery) -> list[CustomerRecord]: ...

    @abstractmethod
    def list_payment_methods(self, customer_external_id: str) -> list[PaymentMethodRecord]:
        """The customer's stored payment methods; never another customer's (PLAN-007 D1)."""

    @abstractmethod
    def get_payment_method(self, external_id: str) -> PaymentMethodRecord: ...

    @abstractmethod
    def create_invoice(self, invoice: InvoiceInput) -> InvoiceRecord: ...

    @abstractmethod
    def update_invoice(self, external_id: str, update: InvoiceUpdate) -> InvoiceRecord: ...

    @abstractmethod
    def get_invoice(self, external_id: str) -> InvoiceRecord: ...

    @abstractmethod
    def list_invoices(self, query: InvoiceQuery) -> list[InvoiceRecord]: ...

    @abstractmethod
    def delete_invoice(self, external_id: str) -> None: ...

    @abstractmethod
    def send_invoice_email(self, external_id: str, cc: list[str] | None = None) -> None: ...

    @abstractmethod
    def mark_invoice_paid_cash_or_check(
        self, external_id: str, payment: OfflinePayment
    ) -> InvoiceRecord: ...

    @abstractmethod
    def pay_invoice(self, external_id: str, payment: PayWithStoredMethod) -> TransactionRecord:
        """Behind ``STAX_PAY_INVOICE_ENABLED`` (D3)."""

    @abstractmethod
    def list_transactions(self, query: TransactionQuery) -> list[TransactionRecord]: ...

    @abstractmethod
    def get_transaction(self, external_id: str) -> TransactionRecord: ...

    @abstractmethod
    def refund_transaction(self, external_id: str, amount: Decimal) -> TransactionRecord: ...

    @abstractmethod
    def void_transaction(self, external_id: str) -> TransactionRecord: ...

    @abstractmethod
    def list_deposits(self, date_range: DateRange) -> list[DepositRecord]: ...

    @abstractmethod
    def get_deposit(self, date_range: DateRange, batch_id: str) -> DepositDetail: ...

    @abstractmethod
    def create_scheduled_invoice(self, schedule: ScheduleInput) -> ScheduleRecord: ...

    @abstractmethod
    def get_scheduled_invoice(self, external_id: str) -> ScheduleRecord: ...

    @abstractmethod
    def list_scheduled_invoices(self, query: ScheduleQuery) -> list[ScheduleRecord]: ...

    @abstractmethod
    def update_scheduled_invoice(
        self, external_id: str, update: ScheduleUpdate
    ) -> ScheduleRecord: ...

    @abstractmethod
    def delete_scheduled_invoice(self, external_id: str) -> None: ...

    @abstractmethod
    def list_ach_rejections(self, date_range: DateRange) -> list[AchRejection]: ...

    @abstractmethod
    def list_disputes(self, query: DisputeQuery) -> list[DisputeRecord]:
        """One page of the merchant's disputes, by respond-by date (PLAN-012 D8)."""

    @abstractmethod
    def upload_dispute_evidence(
        self, dispute_external_id: str, name: str, data: bytes, content_type: str
    ) -> DisputeEvidenceReceipt:
        """Send one evidence file: fewer than :data:`EVIDENCE_MAX_BYTES` bytes of a type in
        :data:`EVIDENCE_EXTENSIONS`, else :class:`~payments.errors.ValidationFailed` before
        any request. A dispute past its respond-by date is refused by the provider
        (:class:`~payments.errors.ProviderRejected`, with its sentence as ``reason``)."""

    @abstractmethod
    def list_webhooks(self) -> list[WebhookRecord]:
        """The registered webhooks (PLAN-008 D9); used only by the registration commands."""

    @abstractmethod
    def create_webhook(self, target_url: str, event: str) -> WebhookRecord: ...

    @abstractmethod
    def delete_webhook(self, external_id: str) -> None: ...

    @abstractmethod
    def ping(self) -> ProviderIdentity: ...
