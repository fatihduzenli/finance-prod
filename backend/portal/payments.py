"""The Pay and Bank account pages (PLAN-014 H4, H5). Every function takes the signed-in
:class:`portal.models.PortalUser` and the store :func:`portal.services.store_of` found for it,
and reads only what is billed to the user's operator; no portal view touches a model.

**Pay** (:func:`pay_page`, :func:`request_payment`). "Pay" on one open charge, or "Pay the
whole balance" of a store: the charges are the store's open charges billed to the operator
(:func:`ledger.services.receivables.open_balances`); a charge id that is not one of them
answers ``None`` (the view's 404). The amount is that charge's balance, or the store's whole
open balance, **only**: the posted amount must be exactly it, or the request is refused with a
sentence and nothing is written. Each charge must be one a staff debit today could take
(:func:`ledger.services.one_off_charges.portal_debit_refusal`: a rent or a one-off charge, no
debit in flight, not collected by the lease's autopay or by a plan, within the processor's
limit) and billed to the bank account the page shows. With the pay-an-invoice flag off
(:func:`payments.pay_invoice_enabled`; off on the sandbox and staging) the page says
:data:`PAY_UNAVAILABLE` and nothing is created; without a usable bank method on file the user
is sent to the Bank account page first. Under the amount (with the flag off, under its
sentence), the memo line names each sent invoice of the charges paid ("Paying by wire or
Zelle? Put INV-2026-0012 in the memo.", PLAN-014 I5), so a payer who wires instead is matched
by the Bank deposits module.

A request writes, in one transaction, a :class:`portal.models.PortalPayment` (the page's
one-time ``creation_key``: a replay answers the first request and queues nothing), one
:class:`portal.models.PortalPaymentCharge` per charge with its debit prepared
(:func:`ledger.services.one_off_charges.start_portal_debit`) and one ``portal_pay`` job each
(Rule 2, ADR-0017: the worker makes the single pay-an-invoice attempt with the stored
idempotency key; nothing retries by itself). A charge that changed since the page (a debit
started elsewhere, the limit) refuses the whole request: the payment is kept as ``refused``
and nothing else is written. The worker reports each charge's outcome
(:func:`record_charge_outcome`): the payment is ``paid`` once every charge's debit was accepted
(it settles in a few days, as any bank debit), ``failed`` once none waits and one failed.
Audited ``portal_payment.requested``, ``paid``, ``failed``, ``refused`` (no staff actor; the
portal user's id in the row).

**Bank account** (:func:`bank_page`, :func:`request_bank`). The processor's hosted fields
(:func:`payments.hosted_fields.get_hosted_fields`, as the staff bank-entry page) under the
processor customer of the account that bills the lease: the lease's own, or the account it
shares, whose replacement changes the bank account of every lease sharing it (the page says
so). The browser sends the account to the processor; the app receives only the new
payment-method id (Rule 1), and ``lease_method_attach`` verifies and links it, superseding the
old method (its processor object is kept; TODO-VERIFY-069). Without hosted fields (no web
payments token: staging) the page says bank entry is not set up. Automatic debits cannot be
changed here (:data:`AUTOPAY_BY_STAFF`). Audited ``portal_bank.attached`` or ``replaced`` once
the worker linked the method (:func:`record_bank_outcome`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import QueryDict

from finance.dates import day_text
from ledger.models import (
    AuditLog,
    CompanySettings,
    Invoice,
    InvoiceLineItem,
    InvoiceStatus,
    Lease,
    OneOffCharge,
    RentCharge,
    Store,
)
from ledger.services import installments, leases, receivables
from ledger.services import one_off_charges as debits
from ledger.wording import format_amount, memo_line
from payments import jobs, lease_sync
from payments.hosted_fields import HostedFieldsConfig, get_hosted_fields
from payments.models import Job, JobKind, JobStatus
from portal import services
from portal.models import (
    PortalChargeKind,
    PortalPayment,
    PortalPaymentCharge,
    PortalPaymentStatus,
    PortalUser,
)

# --- Sentences ----------------------------------------------------------------------------

PAY_UNAVAILABLE = "Online payment is not available yet."
SUBMITTED = "Submitted — bank debits take a few days to settle."
SUBMITTING = "Sending your payment to the bank."
PAY_FAILED = (
    "The payment could not be made: nothing was debited for the charges marked Failed. "
    "Try again later, or contact {support_email}."
)
PAY_REFUSED = (
    "This payment was not made: a charge changed since the page was opened. Nothing was debited."
)
NOTHING_OWED = "Nothing is owed on this store."
NO_PAYMENT_YET = "No payment was made from this page yet."
TAMPERED = "The amount is not what is owed now. Nothing was paid."
PAGE_EXPIRED = "This page has expired. Open Pay again. Nothing was paid."
SOME_NOT_PAYABLE = (
    "Some of these charges cannot be paid online now, so the whole balance cannot either. "
    "Pay the others one by one from the store page."
)
CHARGE_PENDING = "A payment of this charge is already on its way."
CHARGE_WAIT = "The last attempt failed a moment ago. Try again in a minute."
CHARGE_AUTOPAY = "Your automatic debit collects this charge on {date}."
CHARGE_PLAN = "This charge is collected in installments."
CHARGE_LIMIT = (
    "This charge is above the limit of one online payment ({limit}). "
    "Contact {support_email} to pay it."
)
CHARGE_NOT_HERE = "This charge cannot be paid online. Contact {support_email} to pay it."
CHARGE_OTHER_BANK = (
    "This charge is billed to another bank account. Contact {support_email} to pay it."
)
PAY_NEEDS_BANK = "Add a bank account to pay online."

AUTOPAY_BY_STAFF = "Automatic debits are set up by our staff. Contact {support_email}."
BANK_NOT_SET_UP = "Adding a bank account online is not set up yet. Contact {support_email}."
BANK_NOT_READY = (
    "Your account is not ready for a bank account yet. Contact {support_email} to add one."
)
BANK_ENDED = "The lease of this store has ended: its bank account cannot be changed here."
BANK_ONLY_THE_ID = "Only the bank account reference can be sent here. Nothing was saved."
BANK_INVALID = "That bank account reference is not valid. Nothing was saved."
BANK_BUSY = "A bank account change is already on its way. Nothing more was sent."
BANK_CHECKING = "Checking the bank account."
BANK_DONE = "Bank account ····{last4} connected."
BANK_DONE_NO_DIGITS = "Bank account connected."
BANK_FAILED = (
    "The bank account could not be connected. Check it and try again, or contact {support_email}."
)
SHARED_REPLACE = (
    "This bank account is shared with {stores}: replacing it changes it for each of them too."
)

ENTITY = "portal_payment"
REQUESTED_ACTION = "portal_payment.requested"
PAID_ACTION = "portal_payment.paid"
FAILED_ACTION = "portal_payment.failed"
REFUSED_ACTION = "portal_payment.refused"
BANK_ATTACHED_ACTION = "portal_bank.attached"
BANK_REPLACED_ACTION = "portal_bank.replaced"
#: The ``portal_pay`` job's parameter naming its :class:`PortalPaymentCharge`.
CHARGE_PARAM = "portal_payment_charge"
#: The ``lease_method_attach`` job's parameters of a portal user's request.
PORTAL_USER_PARAM = "portal_user"
REPLACES_PARAM = "replaces"
METHOD_PARAM = "payment_method_id"

#: The kinds of a bank account change waiting on the account's lease.
BANK_JOB_KINDS = (JobKind.LEASE_METHOD_ATTACH, JobKind.LEASE_ACCOUNT_SWITCH)


def _support(text: str, **values: str) -> str:
    support = str(CompanySettings.read().support_email or "us")
    return text.format(support_email=support, **values)


def store_name(store: Store) -> str:
    """How a portal page names a store: ``#1991``, else its label."""
    return f"#{store.store_number}" if store.store_number is not None else store.external_label


# --- Pay ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PayCharge:
    """One open charge of the payment, and why it cannot be paid online (``""``: it can)."""

    row: receivables.OpenBalance
    charge: RentCharge | OneOffCharge | None
    refusal: str

    @property
    def kind(self) -> str:
        if self.row.kind == receivables.KIND_RENT:
            return PortalChargeKind.RENT
        return PortalChargeKind.ONE_OFF


@dataclass(frozen=True, slots=True)
class PayPage:
    store: Store
    lease: Lease | None
    #: The charges paid: the one asked for, or every open charge of the store.
    charges: list[PayCharge]
    whole: bool
    amount: Decimal
    #: The pay-an-invoice flag (off: :data:`PAY_UNAVAILABLE`).
    enabled: bool
    #: A usable bank method is on file (none: the Bank page first), and its last four.
    has_bank: bool
    last4: str
    #: ``#1991`` when the lease debits the account of another store's lease.
    shared_with: str
    #: Why this payment cannot be made (``""``: it can).
    refusal: str
    #: PLAN-014 I5: the memo line of each sent invoice of the charges paid, once each.
    memo_lines: tuple[str, ...] = ()

    @property
    def payable(self) -> bool:
        return self.enabled and self.has_bank and not self.refusal


def _charge_of(row: receivables.OpenBalance) -> RentCharge | OneOffCharge | None:
    if row.kind == receivables.KIND_RENT:
        return RentCharge.objects.select_related("lease__operator", "lease__store").get(
            pk=row.charge_id
        )
    if row.kind == receivables.KIND_ONE_OFF:
        return OneOffCharge.objects.select_related("operator", "lease").get(pk=row.charge_id)
    return None  # insurance: no online debit (its own schedule collects it)


def _refusal_text(code: str, row: receivables.OpenBalance) -> str:
    if code == debits.PORTAL_PENDING:
        return CHARGE_PENDING
    if code == debits.PORTAL_WAIT:
        return CHARGE_WAIT
    if code == debits.PORTAL_AUTOPAY:
        return CHARGE_AUTOPAY.format(date=day_text(row.due_on))
    if code == debits.PORTAL_PLAN:
        return CHARGE_PLAN
    if code == debits.PORTAL_LIMIT:
        limit: Decimal | None = settings.PROVIDER_MAX_TXN_AMOUNT
        return _support(CHARGE_LIMIT, limit=format_amount(limit) if limit is not None else "")
    return _support(CHARGE_NOT_HERE)


#: A sent invoice the memo line names (never a draft, one not sent yet or a void one).
MEMO_INVOICE_STATUSES = (InvoiceStatus.SENT, InvoiceStatus.CLOSED)
#: The invoice's column that names each kind of charge.
_INVOICE_COLUMNS = {
    receivables.KIND_RENT: "rent_charge_id",
    receivables.KIND_ONE_OFF: "one_off_charge_id",
    receivables.KIND_INSURANCE: "insurance_charge_id",
}


def _memo_lines(user: PortalUser, rows: list[receivables.OpenBalance]) -> tuple[str, ...]:
    """The memo line of every sent invoice to the user's operator that bills one of ``rows``
    (as its charge, or by a line of a builder invoice), in the rows' order, each number once
    (two queries)."""
    wanted = {(row.kind, row.charge_id) for row in rows}
    found: dict[tuple[str, uuid.UUID], set[str]] = {}
    invoices = Invoice.objects.filter(
        operator_id=user.operator_id, internal_status__in=MEMO_INVOICE_STATUSES
    )
    by_kind: dict[str, list[uuid.UUID]] = {}
    for kind, charge_id in wanted:
        by_kind.setdefault(kind, []).append(charge_id)
    direct = Q()
    for kind, ids in by_kind.items():
        direct |= Q(**{f"{_INVOICE_COLUMNS[kind]}__in": ids})
    for number, *columns in invoices.filter(direct).values_list(
        "invoice_number", *_INVOICE_COLUMNS.values()
    ):
        for kind, charge_id in zip(_INVOICE_COLUMNS, columns, strict=True):
            if (kind, charge_id) in wanted:
                found.setdefault((kind, charge_id), set()).add(number)
    lines = InvoiceLineItem.objects.filter(invoice__in=invoices).filter(
        Q(rent_charge_id__in=by_kind.get(receivables.KIND_RENT, []))
        | Q(one_off_charge_id__in=by_kind.get(receivables.KIND_ONE_OFF, []))
    )
    for number, rent, one_off in lines.values_list(
        "invoice__invoice_number", "rent_charge_id", "one_off_charge_id"
    ):
        for key in ((receivables.KIND_RENT, rent), (receivables.KIND_ONE_OFF, one_off)):
            if key in wanted:
                found.setdefault(key, set()).add(number)
    numbers: list[str] = []
    for row in rows:
        for number in sorted(found.get((row.kind, row.charge_id), ())):
            if number not in numbers:
                numbers.append(number)
    return tuple(memo_line(number) for number in numbers)


def _billing_account(charge: RentCharge | OneOffCharge) -> Lease | None:
    return leases.account_lease(leases.billing_lease(charge))


def _pay_charge(row: receivables.OpenBalance, account: Lease | None, today: date) -> PayCharge:
    charge = _charge_of(row)
    if charge is None:
        return PayCharge(row, None, _support(CHARGE_NOT_HERE))
    if account is None or _billing_account(charge) != account:
        return PayCharge(row, charge, _support(CHARGE_OTHER_BANK))
    code = debits.portal_debit_refusal(charge, row.balance, today)
    return PayCharge(row, charge, _refusal_text(code, row) if code else "")


def _usable_bank(lease: Lease | None) -> tuple[bool, str]:
    """Whether a usable bank method is on file for the lease's account, and its last four."""
    method = leases.linked_method_record(lease)
    if method is None or not method.is_usable_bank:
        return False, ""
    return True, method.last_four


def _shared_with(lease: Lease | None) -> str:
    account = leases.account_lease(lease)
    if lease is None or account is None or account.pk == lease.pk:
        return ""
    return store_name(account.store)


def pay_page(user: PortalUser, store: Store, charge_raw: object, today: date) -> PayPage | None:
    """The Pay page of ``store`` for one charge (``charge_raw``, a charge id of the store's
    open charges) or, without one, the whole open balance; ``None`` when ``charge_raw`` names
    no open charge of the store billed to the user's operator."""
    rows = [
        row
        for row in receivables.open_balances(today, user.operator_id)
        if row.store_id == store.pk
    ]
    whole = charge_raw in (None, "")
    if not whole:
        try:
            wanted = uuid.UUID(str(charge_raw))
        except ValueError:
            return None
        rows = [row for row in rows if row.charge_id == wanted]
        if not rows:
            return None
    lease = services.lease_of(user, store)
    enabled = debits.charge_now_enabled()
    has_bank, last4 = _usable_bank(lease) if enabled else (False, "")
    account = leases.account_lease(lease)
    charges = [_pay_charge(row, account, today) for row in rows] if has_bank else []
    refusal = ""
    if not rows:
        refusal = NOTHING_OWED
    elif whole and any(charge.refusal for charge in charges):
        refusal = SOME_NOT_PAYABLE
    elif not whole and charges and charges[0].refusal:
        refusal = charges[0].refusal
    return PayPage(
        store=store,
        lease=lease,
        charges=charges or [PayCharge(row, None, "") for row in rows],
        whole=whole,
        amount=receivables.total(rows),
        enabled=enabled,
        has_bank=has_bank,
        last4=last4,
        shared_with=_shared_with(lease),
        refusal=refusal,
        memo_lines=_memo_lines(user, rows) if rows else (),
    )


@dataclass(frozen=True, slots=True)
class Requested:
    """What a Pay request did: the payment made (or the earlier one a replay found), or the
    sentence of a refusal with nothing written (``payment`` ``None``); ``to_bank``: no bank
    method on file, the Bank page first. A request refused under the charges' locks keeps
    its payment, ``refused``, with the sentence."""

    payment: PortalPayment | None = None
    refusal: str = ""
    to_bank: bool = False


class _Refused(Exception):  # noqa: N818 (reads as an outcome)
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def _exact_amount(raw: object) -> Decimal | None:
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _audit(action: str, payment: PortalPayment, after: dict[str, Any]) -> None:
    AuditLog.objects.create(
        actor=None,
        action=action,
        entity_type=ENTITY,
        entity_id=str(payment.pk),
        before=None,
        after={
            "portal_user": str(payment.user_id),
            "lease": str(payment.lease_id),
            "amount": str(payment.amount),
            **after,
        },
    )


def _key(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def _replayed(user: PortalUser, store: Store, key: uuid.UUID) -> Requested | None:
    """The request this key already made, or ``None``; a key of another user's or another
    store's payment is a refusal (nothing is shown of it)."""
    earlier = PortalPayment.objects.select_related("lease").filter(creation_key=key).first()
    if earlier is None:
        return None
    if earlier.user_id != user.pk or earlier.lease.store_id != store.pk:
        return Requested(refusal=PAGE_EXPIRED)
    return Requested(payment=earlier)


def request_payment(
    user: PortalUser,
    store: Store,
    charge_raw: object,
    amount_raw: object,
    key_raw: object,
    today: date,
) -> Requested | None:
    """The Pay page's POST (see the module docstring); ``None`` when the charge is not one of
    the store's open charges billed to the user's operator (the view's 404)."""
    key = _key(key_raw)
    if key is None:
        return Requested(refusal=PAGE_EXPIRED)
    replay = _replayed(user, store, key)
    if replay is not None:
        return replay
    page = pay_page(user, store, charge_raw, today)
    if page is None:
        return None
    if not page.enabled:
        return Requested(refusal=PAY_UNAVAILABLE)
    if not page.has_bank:
        return Requested(to_bank=True)
    if _exact_amount(amount_raw) != page.amount:
        return Requested(refusal=TAMPERED)
    if page.refusal or page.lease is None:
        return Requested(refusal=page.refusal or NOTHING_OWED)
    with transaction.atomic():
        try:
            with transaction.atomic():
                payment = PortalPayment.objects.create(
                    user=user, lease=page.lease, amount=page.amount, creation_key=key
                )
        except IntegrityError:
            # The same key committed first (a double submit): that request is the answer.
            return _replayed(user, store, key) or Requested(refusal=PAGE_EXPIRED)
        try:
            with transaction.atomic():
                made = [_start(payment, charge, key, today) for charge in page.charges]
        except _Refused as refused:
            # A charge changed since the page: no charge, debit or job is kept.
            payment.status = PortalPaymentStatus.REFUSED
            payment.save(update_fields=["status", "updated_at"])
            _audit(REFUSED_ACTION, payment, {"refusal": refused.text})
            return Requested(payment=payment, refusal=refused.text)
        _audit(
            REQUESTED_ACTION,
            payment,
            {
                "charges": [
                    {"kind": line.kind, "charge": str(line.charge_id), "amount": str(line.amount)}
                    for line in made
                ]
            },
        )
    return Requested(payment=payment)


def _start(
    payment: PortalPayment, charge: PayCharge, key: uuid.UUID, today: date
) -> PortalPaymentCharge:
    """One charge of the payment: its debit prepared, its row and its ``portal_pay`` job."""
    if charge.charge is None:  # a payable page holds every charge; never reached
        raise _Refused(_support(CHARGE_NOT_HERE))
    debit_key = uuid.uuid5(key, f"{charge.kind}:{charge.row.charge_id}")
    outcome = debits.start_portal_debit(
        charge.charge,
        charge.row.balance,
        key=debit_key,
        portal_payment=payment.pk,
        today=today,
    )
    if not outcome.ok or outcome.charge is None:
        raise _Refused(_refusal_text(outcome.message, charge.row))
    line = PortalPaymentCharge.objects.create(
        payment=payment,
        kind=charge.kind,
        charge_id=charge.row.charge_id,
        debit=outcome.charge,
        amount=charge.row.balance,
    )
    job = jobs.enqueue(
        JobKind.PORTAL_PAY,
        outcome.charge,
        {CHARGE_PARAM: line.pk},
        None,
        replay_key=uuid.uuid5(key, f"job:{line.pk}"),
    )
    line.job_id = job.pk
    line.save(update_fields=["job_id"])
    return line


def record_charge_outcome(line_id: uuid.UUID, ok: bool) -> None:
    """The worker's outcome of one charge's ``portal_pay`` job: the charge ``paid`` (the
    debit was accepted) or ``failed``; once no charge of the payment waits, the payment is
    ``paid`` (every charge paid) or ``failed``, audited once."""
    with transaction.atomic():
        line = PortalPaymentCharge.objects.select_for_update().filter(pk=line_id).first()
        if line is None or line.status != PortalPaymentStatus.REQUESTED:
            return
        line.status = PortalPaymentStatus.PAID if ok else PortalPaymentStatus.FAILED
        line.save(update_fields=["status"])
        payment = PortalPayment.objects.select_for_update().get(pk=line.payment_id)
        states = set(payment.charges.values_list("status", flat=True))
        if (
            PortalPaymentStatus.REQUESTED in states
            or payment.status != PortalPaymentStatus.REQUESTED
        ):
            return
        paid = states == {PortalPaymentStatus.PAID}
        payment.status = PortalPaymentStatus.PAID if paid else PortalPaymentStatus.FAILED
        payment.save(update_fields=["status", "updated_at"])
        _audit(
            PAID_ACTION if paid else FAILED_ACTION,
            payment,
            {
                "failed": [
                    str(charge_id)
                    for charge_id in payment.charges.filter(
                        status=PortalPaymentStatus.FAILED
                    ).values_list("charge_id", flat=True)
                ]
            },
        )


@dataclass(frozen=True, slots=True)
class ChargeState:
    description: str
    amount: Decimal
    status: str


@dataclass(frozen=True, slots=True)
class PaymentState:
    payment: PortalPayment | None
    status: str
    sentence: str
    charges: list[ChargeState]

    @property
    def waiting(self) -> bool:
        return self.status == PortalPaymentStatus.REQUESTED


def _charge_status(line: PortalPaymentCharge) -> str:
    """A charge's state as the page reads it: a job that finished without reporting (it
    stopped twice and gave up) reads as failed."""
    job = Job.objects.filter(pk=line.job_id).first() if line.job_id is not None else None
    if line.status != PortalPaymentStatus.REQUESTED or job is None:
        return str(line.status)
    if jobs.is_active(job):
        return PortalPaymentStatus.REQUESTED
    return PortalPaymentStatus.FAILED


def _description(line: PortalPaymentCharge) -> str:
    if line.kind == PortalChargeKind.RENT:
        rent = RentCharge.objects.filter(pk=line.charge_id).first()
        return installments.parent_screen_label(rent) if rent is not None else "Rent"
    charge = OneOffCharge.objects.filter(pk=line.charge_id).first()
    return str(charge.description) if charge is not None else "Charge"


def payment_state(user: PortalUser, store: Store, payment_raw: object) -> PaymentState | None:
    """The outcome page of a payment of the user on ``store`` (``payment_raw``), else of the
    user's latest one there; ``None`` for an id that is not the user's payment on the store."""
    found = PortalPayment.objects.filter(user=user, lease__store=store)
    if payment_raw not in (None, ""):
        key = _key(payment_raw)
        payment = found.filter(pk=key).first() if key is not None else None
        if payment is None:
            return None
    else:
        payment = found.order_by("-created_at").first()
    if payment is None:
        return PaymentState(None, "", NO_PAYMENT_YET, [])
    lines = list(payment.charges.order_by("kind", "charge_id"))
    charges = [ChargeState(_description(line), line.amount, _charge_status(line)) for line in lines]
    states = {charge.status for charge in charges}
    if payment.status == PortalPaymentStatus.REFUSED:
        status, sentence = str(PortalPaymentStatus.REFUSED), PAY_REFUSED
    elif PortalPaymentStatus.REQUESTED in states:
        status, sentence = str(PortalPaymentStatus.REQUESTED), SUBMITTING
    elif states == {PortalPaymentStatus.PAID}:
        status, sentence = str(PortalPaymentStatus.PAID), SUBMITTED
    else:
        status, sentence = str(PortalPaymentStatus.FAILED), _support(PAY_FAILED)
    return PaymentState(payment, status, sentence, charges)


# --- Bank account ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BankPage:
    store: Store
    lease: Lease | None
    #: The lease whose processor customer holds the bank account (the one it shares, or itself).
    account: Lease | None
    last4: str
    #: ``#1991`` when the account is another store's lease's.
    shared_with: str
    #: The other stores whose leases debit the same account (a replacement changes theirs).
    sharing: list[str]
    #: The hosted fields' configuration, or ``None`` with :attr:`message` saying why not.
    config: HostedFieldsConfig | None
    message: str
    replaces: bool
    autopay_note: str
    #: The latest bank change this user asked for: ``""``, waiting, done or failed.
    change: str
    change_text: str

    @property
    def waiting(self) -> bool:
        return self.change == "waiting"


def _sharing(account: Lease, lease: Lease) -> list[str]:
    """The stores of the current leases debiting ``account``'s bank, other than ``lease``'s."""
    leases_of_account = Lease.objects.filter(
        pk__in=[account.pk, *account.account_sharers.values_list("lease_id", flat=True)]
    ).select_related("store")
    names = {
        store_name(other.store)
        for other in leases_of_account
        if other.pk != lease.pk and other.store_id != lease.store_id and services.is_current(other)
    }
    return sorted(names)


def _bank_state(
    lease: Lease | None, account: Lease | None
) -> tuple[HostedFieldsConfig | None, str]:
    hosted = get_hosted_fields()
    if not hosted.available():
        return None, _support(BANK_NOT_SET_UP)
    if lease is None or account is None or not services.is_current(lease):
        return None, BANK_ENDED
    customer = lease_sync.current_refs(account).get(lease_sync.KIND_CUSTOMER, "")
    config = hosted.config_for(customer) if customer else None
    return config, "" if config is not None else _support(BANK_NOT_READY)


def _latest_change(user: PortalUser, account: Lease | None) -> Job | None:
    if account is None:
        return None
    return (
        Job.objects.filter(
            kind=JobKind.LEASE_METHOD_ATTACH,
            entity_id=account.pk,
            params__portal_user=str(user.pk),
        )
        .order_by("-created_at", "-id")
        .first()
    )


def bank_page(user: PortalUser, store: Store, *, after_send: bool = False) -> BankPage:
    """The Bank account page of ``store``; ``after_send``: the page right after a request, which
    shows how the latest one went."""
    lease = services.lease_of(user, store)
    account = leases.account_lease(lease)
    config, message = _bank_state(lease, account)
    change, change_text = "", ""
    job = _latest_change(user, account)
    if job is not None and jobs.is_active(job):
        change, change_text = "waiting", BANK_CHECKING
    elif job is not None and after_send:
        if job.status == JobStatus.DONE:
            digits = lease_sync.billing_of(account).bank_last4 if account is not None else ""
            change = "done"
            change_text = BANK_DONE.format(last4=digits) if digits else BANK_DONE_NO_DIGITS
        else:
            change, change_text = "failed", _support(BANK_FAILED)
    return BankPage(
        store=store,
        lease=lease,
        account=account,
        last4=services.bank_last4(lease),
        shared_with=_shared_with(lease),
        sharing=_sharing(account, lease) if account is not None and lease is not None else [],
        config=config,
        message=message,
        replaces=bool(leases.linked_payment_method(lease)),
        autopay_note=_support(AUTOPAY_BY_STAFF),
        change=change,
        change_text=change_text,
    )


def shared_replace_text(page: BankPage) -> str:
    """The confirmation's sentence when the account is shared, else ``""``."""
    if not page.sharing:
        return ""
    return SHARED_REPLACE.format(stores=", ".join(page.sharing))


def request_bank(user: PortalUser, store: Store, posted: QueryDict) -> str:
    """The Bank account page's POST: exactly one field, ``payment_method_id`` (the hosted
    fields' new method id), queues ``lease_method_attach`` on the account's lease with the
    user's id; the refusal sentence with nothing queued, or ``""``."""
    names = set(posted) - {"csrfmiddlewaretoken"}
    values = posted.getlist(METHOD_PARAM)
    if names != {METHOD_PARAM} or len(values) != 1:
        return BANK_ONLY_THE_ID
    value = values[0].strip()
    if not lease_sync.IDENTIFIER.match(value):
        return BANK_INVALID
    page = bank_page(user, store)
    if page.config is None or page.account is None:
        return page.message or BANK_ENDED
    with transaction.atomic():
        account = Lease.objects.select_for_update(of=("self",)).get(pk=page.account.pk)
        if jobs.is_active(jobs.latest_for(account, BANK_JOB_KINDS)):
            return BANK_BUSY
        jobs.enqueue(
            JobKind.LEASE_METHOD_ATTACH,
            account,
            {METHOD_PARAM: value, PORTAL_USER_PARAM: user.pk, REPLACES_PARAM: page.replaces},
            None,
        )
    return ""


def record_bank_outcome(job: Job, ok: bool) -> None:
    """The worker linked (``ok``) the method a portal user's request named: audited
    ``portal_bank.attached`` (no method before) or ``replaced``; nothing for a refusal (the
    lease's own ``lease.bank_attach_refused`` row says why)."""
    if not ok:
        return
    lease = Lease.objects.filter(pk=job.entity_id).first()
    replaced = job.params.get(REPLACES_PARAM) is True
    AuditLog.objects.create(
        actor=None,
        action=BANK_REPLACED_ACTION if replaced else BANK_ATTACHED_ACTION,
        entity_type="portal_user",
        entity_id=str(job.params.get(PORTAL_USER_PARAM, "")),
        before=None,
        after={
            "lease": str(job.entity_id),
            "last4": lease_sync.billing_of(lease).bank_last4 if lease is not None else "",
            "sharing": sorted(
                str(pk) for pk in lease.account_sharers.values_list("lease_id", flat=True)
            )
            if lease is not None
            else [],
        },
    )
