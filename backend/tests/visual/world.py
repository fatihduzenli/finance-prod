"""The visual suite's one data set (docs/qa/PLAN-012c-design-pass.md D10). Not a test module.

Built through the existing test helpers, every date fixed: two stores of one operator with a
provider customer reference, a year of rent charges with paid, unpaid, partly paid, pending
and waived cells, a one-off charge, an invoice draft with two lines, an installment plan, a
lease end at the statement step with one deduction, a promise in the contact log and a
calendar entry. The accessibility tests (``tests/test_design_accessibility.py``) render
their pages over the same rows.

:class:`FrozenClock` stands in for ``django.utils.timezone.now``. While the rows are built
it moves one second per reading from 09:00 New York time, so rows written in one go still
get distinct, repeatable ``created_at`` values (a list that orders by them does not depend
on chance); then :meth:`FrozenClock.stop` holds it at 12:00 for the pages.
:func:`counted_ids` does the same for the ids: a draft's number on screen is the start of its
id (``DRAFT-`` and eight characters), so a random id would move the pixels between runs.
"""

from __future__ import annotations

import importlib
import itertools
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from django.apps import apps
from django.contrib.auth.models import User
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.roles import Role
from accounts.themes import set_theme
from ledger.models import (
    Brand,
    CalendarEntry,
    ContactKind,
    DeductionKind,
    InstallmentCadence,
    InvoiceDraft,
    Lease,
    LeaseBilling,
    LeaseEnd,
    MessageTemplate,
    OneOffCategory,
    Operator,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    Store,
)
from ledger.services import (
    contacts,
    installments,
    invoice_builder,
    lease_end,
    messaging,
    store_costs,
)
from ledger.services.invoice_builder import LineInput
from ledger.services.lease_end import DeductionInput
from tests.auth_helpers import make_user
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_paid_rent_charge,
    a_payment,
    a_rent_charge,
    a_store,
)
from tests.test_lease_services import make_operator

D = Decimal
#: The day every page is seen on (a Tuesday).
TODAY = date(2026, 10, 20)
#: 12:00 in New York on that day (EDT, UTC-4): the time the pages are seen at.
NOON = datetime(2026, 10, 20, 16, 0, tzinfo=UTC)
#: 09:00 in New York: the first reading while the rows are built.
SEEDING_FROM = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
USERNAME = "visual-billing"
ADMIN_USERNAME = "visual-admin"
DARK_USERNAME = "visual-billing-dark"
DARK_ADMIN_USERNAME = "visual-admin-dark"
#: The grid's twelve months end with this one; one status per month, oldest first.
MONTHS = [date(2025, 11, 1)] + [date(2026, month, 1) for month in range(1, 11)]
CYCLE = (
    RentChargeStatus.PAID,
    RentChargeStatus.PAID,
    RentChargeStatus.PARTIAL,
    RentChargeStatus.PAID,
    RentChargeStatus.WAIVED,
    RentChargeStatus.PAID,
    RentChargeStatus.UNPAID,
    RentChargeStatus.PAID,
    RentChargeStatus.PAID,
    RentChargeStatus.UNPAID,
    RentChargeStatus.PENDING,
)


class FrozenClock:
    """A ``timezone.now`` replacement: one second per reading, then held at noon."""

    def __init__(self) -> None:
        self._ticks = itertools.count()
        self._held: datetime | None = None

    def __call__(self) -> datetime:
        if self._held is not None:
            return self._held
        return SEEDING_FROM + timedelta(seconds=next(self._ticks))

    def stop(self) -> None:
        self._held = NOON


@contextmanager
def counted_ids() -> Iterator[None]:
    """Every ``uuid4`` default of every model gives a counted id while the rows are built:
    ``0000000N-0000-4000-8000-00000000000N``. Restored on exit."""
    numbers = itertools.count(1)

    def next_id() -> uuid.UUID:
        n = next(numbers)
        return uuid.UUID(f"{n:08x}-0000-4000-8000-{n:012x}")

    patched = [
        model_field
        for model in apps.get_models()
        for model_field in model._meta.concrete_fields
        if model_field.default is uuid.uuid4
    ]
    for model_field in patched:
        # Field.get_default reads this cached property; the instance attribute wins.
        model_field.__dict__["_get_default"] = next_id
    try:
        yield
    finally:
        for model_field in patched:
            model_field.__dict__.pop("_get_default", None)


@dataclass(frozen=True)
class SignIn:
    username: str
    device: TOTPDevice


@dataclass(frozen=True)
class World:
    user: User
    #: Who the pages are seen as: "billing" (every page) and "admin" (the lease-end wizard).
    sign_ins: dict[str, SignIn]
    operator: Operator
    store: Store
    second_store: Store
    lease: Lease
    second_lease: Lease
    draft: InvoiceDraft
    lease_end: LeaseEnd


def _rent_year(lease: Lease) -> None:
    for month, status in zip(MONTHS, CYCLE, strict=True):
        settled = month.replace(day=6)
        if status == RentChargeStatus.PAID:
            a_paid_rent_charge(lease, month, "3500.00", settled)
        elif status == RentChargeStatus.PARTIAL:
            charge = a_rent_charge(lease, month, status, "3500.00", "1500.00")
            a_payment(charge, "1500.00", settled)
        elif status == RentChargeStatus.PENDING:
            charge = a_rent_charge(lease, month, status, "3500.00")
            a_payment(charge, "3500.00", None, PaymentStatus.PENDING, initiated_on=settled)
        else:
            a_rent_charge(lease, month, status, "3500.00")


def build() -> World:
    """Write the data set with counted ids; the caller holds the clock (:class:`FrozenClock`)."""
    with counted_ids():
        return _build()


def _build() -> World:
    # PLAN-014 B2: the Settings page lists the three brands of ledger 0058; the suite's
    # transaction tests flush them, so the world writes them again.
    for order, name in enumerate(("iFixandRepair", "Techy", "Talk&Fix"), start=1):
        Brand.objects.update_or_create(name=name, defaults={"active": True, "sort_order": order})
    user, device = make_user(USERNAME, Role.BILLING)
    assert device is not None
    operator = make_operator(email="northwind@example.com", customer="cus-visual-1")

    store = a_store("0101 MAPLE AVENUE", 101, "GA")
    # PLAN-012e-2 D7 and D8: the address line and the Store cost block have something to show.
    Store.objects.filter(pk=store.pk).update(
        address_line1="101 Maple Avenue", city="Savannah", postal_code="31401"
    )
    store.refresh_from_db()
    cost = store_costs.set_cost(
        store,
        D("2400.00"),
        date(2025, 1, 1),
        other_monthly_cost=D("150.00"),
        other_cost_description="Bins",
        landlord_lease_end=date(2030, 12, 31),
        actor=user,
    )
    assert cost.ok, cost.message
    lease = a_lease(store, operator, started=date(2025, 1, 1), rent="3500.00")
    _rent_year(lease)

    second_store = a_store("0202 HARBOR ROAD", 202, "FL")
    second_lease = a_lease(
        second_store, operator, started=date(2025, 11, 1), rent="2000.00", debit_day=1
    )
    for month in MONTHS[:-1]:
        a_paid_rent_charge(second_lease, month, "2000.00", month.replace(day=2))

    a_one_off_charge(operator, "250.00", date(2026, 10, 10), lease=lease)
    arrears = a_one_off_charge(
        operator,
        "1200.00",
        date(2026, 9, 1),
        lease=lease,
        category=OneOffCategory.ARREARS,
        description="Arrears July to September 2026",
    )
    proposed, refused = installments.preview(
        arrears, 3, date(2026, 11, 1), InstallmentCadence.MONTHLY, today=TODAY
    )
    assert refused is None, refused
    plan = installments.create_plan(
        arrears, 3, date(2026, 11, 1), InstallmentCadence.MONTHLY, proposed, user, today=TODAY
    )
    assert plan.ok, plan.message

    lines = [
        LineInput("Sign repair", "Front sign, two letters", None, D("180.00"), False),
        LineInput("Lock change", "", D("2"), D("85.00"), False),
    ]
    drafted = invoice_builder.create_draft(
        operator,
        lease,
        "Works of October at the store.",
        date(2026, 11, 5),
        lines,
        user,
        today=TODAY,
    )
    assert drafted.ok and drafted.draft is not None, drafted.message

    promised = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.PROMISE,
            happened_on=date(2026, 10, 15),
            body="Will pay September by Friday.",
            promised_date=date(2026, 10, 23),
            promised_amount=D("3500.00"),
            lease=lease.pk,
        ),
        actor=user,
    )
    assert promised.ok, promised.message
    CalendarEntry.objects.create(
        kind="note",
        on=date(2026, 10, 22),
        title="Inspect the Harbor Road store",
        store=second_store,
        author=user,
    )

    end = lease_end.start(second_lease, user)
    terms = lease_end.set_terms(
        end, date(2026, 10, 19), "moved_out", True, note="", actor=user, today=TODAY
    )
    assert terms.ok, terms.message
    end = LeaseEnd.objects.get(pk=end.pk)
    deducted = lease_end.set_deductions(
        end,
        [DeductionInput(DeductionKind.CLEANING, "Cleaning after move-out", D("150.00"))],
        actor=user,
        today=TODAY,
    )
    assert deducted.ok, deducted.message

    # PLAN-014 D: the seven templates of ledger 0061 (flushed by the suite's transaction tests,
    # as the brands are), the Maple Avenue lease's billing e-mail, and one message of rule B for
    # its oldest unpaid rent, held in the Outbox ("not sent (mail not set up)": the suite runs
    # without a key) for the Outbox page and the store timeline.
    seed = importlib.import_module("ledger.migrations.0061_messaging_engine")
    for code, name, internal, subject, body in seed.TEMPLATES:
        MessageTemplate.objects.get_or_create(
            code=code,
            defaults={"name": name, "internal": internal, "subject": subject, "body": body},
        )
    LeaseBilling.objects.update_or_create(
        lease=lease,
        defaults={"company_name": "Northwind Stores LLC", "email": "maple@example.com"},
    )
    unpaid = (
        RentCharge.objects.filter(lease=lease, status=RentChargeStatus.UNPAID)
        .order_by("due_on")
        .first()
    )
    assert unpaid is not None
    reminder = MessageTemplate.objects.get(code="reminder_due")
    message = messaging.queue(
        "B", lease, unpaid, reminder, messaging.context_for(lease, unpaid, "B", today=TODAY)
    )
    assert message is not None
    messaging.send(message.pk)

    # Last, so that the rows above keep their ids whoever else signs in.
    _, admin_device = make_user(ADMIN_USERNAME, Role.ADMIN)
    assert admin_device is not None
    # PLAN-012g Batch D: the same two roles with the Dark appearance stored (DESIGN-SPEC §7.1),
    # for the dark baseline set; after the admin so every other row keeps its id.
    dark_billing, dark_billing_device = make_user(DARK_USERNAME, Role.BILLING)
    dark_admin, dark_admin_device = make_user(DARK_ADMIN_USERNAME, Role.ADMIN)
    assert dark_billing_device is not None and dark_admin_device is not None
    set_theme(dark_billing, "dark")
    set_theme(dark_admin, "dark")
    return World(
        user=user,
        sign_ins={
            "billing": SignIn(USERNAME, device),
            "admin": SignIn(ADMIN_USERNAME, admin_device),
            "billing-dark": SignIn(DARK_USERNAME, dark_billing_device),
            "admin-dark": SignIn(DARK_ADMIN_USERNAME, dark_admin_device),
        },
        operator=operator,
        store=store,
        second_store=second_store,
        lease=lease,
        second_lease=second_lease,
        draft=drafted.draft,
        lease_end=LeaseEnd.objects.get(pk=end.pk),
    )
