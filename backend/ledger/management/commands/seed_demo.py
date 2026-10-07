"""Fill a local database with invented demo data (docs/qa/PLAN-004-schema.md §4.5).

Refused (exit 2) unless APP_ENV=local. Idempotent: every row has a fixed UUID derived
from a label and is created only when missing, so a second run changes nothing.
Deterministic: the only varied values come from a random generator with a fixed seed.
Every name is invented; every e-mail address is at example.com; no external ID resembles
a real one.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from ledger.models import (
    AdjustmentReason,
    ChargeAdjustment,
    ChargeNote,
    CollectionSchedule,
    CollectionStatus,
    EnrolmentStatus,
    InsuranceCharge,
    InsurancePremium,
    Invoice,
    InvoiceLineItem,
    InvoiceLineKind,
    InvoiceStatus,
    Lease,
    LeaseRentHistory,
    LegacyStatus,
    Note,
    NoteEntityType,
    Operator,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    RentSource,
    ReviewReason,
    ScheduleKind,
    ScheduleState,
    Store,
    StoreBalance,
    StoreBalanceKind,
    StoreStatus,
)
from payments.demo import write_demo_provider_statuses
from payments.models import ExternalRef, ProviderSnapshot

REFUSED_EXIT_CODE = 2
RANDOM_SEED = 20260929
#: Every seeded primary key is uuid5(SEED_NAMESPACE, label).
SEED_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "seed-demo.example.com")
SEED_USER_EMAIL = "seed-demo@example.com"
DEMO_PROVIDER = "stax_pay"

#: Invoice statuses as the provider reports them (docs/CLAUDE.md Rule 2), plus "" for
#: an invoice the provider has not seen yet.
DEMO_PROVIDER_STATUSES = (
    "",
    "DRAFT",
    "SENT",
    "VIEWED",
    "PAID",
    "PARTIALLY APPLIED",
    "PARTIALLY PAID",
    "ATTEMPTED",
    "DELETED",
)

PREMIUMS = {2025: Decimal("595.00"), 2026: Decimal("695.00")}
RENT_AMOUNTS = (
    Decimal("1850.00"),
    Decimal("2100.00"),
    Decimal("2450.00"),
    Decimal("2975.50"),
    Decimal("3200.00"),
    Decimal("12581.00"),
)
MONTHS = (date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1))
CENT = Decimal("0.01")

OPERATOR_NAMES = (
    ("Maple Ridge Foods LLC", "maple.ridge@example.com"),
    ("Harbor Lane Kitchens Inc", "harbor.lane@example.com"),
    ("Blue Finch Hospitality LLC", "blue.finch@example.com"),
    ("Cedar Point Eateries LLC", "cedar.point@example.com"),
    ("Juniper Street Group Inc", "juniper.street@example.com"),
    ("Copper Kettle Ventures LLC", "copper.kettle@example.com"),
    ("Silver Oak Dining LLC", "silver.oak@example.com"),
    ("Willow Bend Partners LLC", "willow.bend@example.com"),
    ("Granite Hill Foods LLC", None),
    ("Amber Field Restaurants Inc", None),
)
CITIES = (
    ("Springfield", "IL"),
    ("Riverton", "WY"),
    ("Fairview", "TN"),
    ("Georgetown", "TX"),
    ("Salem", "OR"),
    ("Madison", "WI"),
)
STORE_COUNT = 14
#: Lease index -> operator index. Operator 0 runs two stores (the cross-store case).
LEASE_OPERATOR = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1, 2, None)
TWICE_MONTHLY_LEASE = 3
PAUSED_LEASE = 11
WIRE_LEASE = 12
PROSPECTIVE_LEASE = 13
ENROLLED_OPERATORS = range(8)

#: Rent charge status per (lease, month index); anything not listed is paid in July and
#: August and due in September.
CHARGE_STATUS_OVERRIDES = {
    (5, 0): RentChargeStatus.WAIVED,
    (6, 0): RentChargeStatus.UNPAID,
    (7, 1): RentChargeStatus.PARTIAL,
    (8, 1): RentChargeStatus.PARTIAL,
    (11, 1): RentChargeStatus.UNPAID,
    (0, 2): RentChargeStatus.PENDING,
    (9, 2): RentChargeStatus.PENDING,
}
RETURNED_ACH = (6, 0)
ZELLE_PAYMENT = (PAUSED_LEASE, 0)
REFUNDED_PAYMENT = (0, 0)


def _seed_id(label: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, label)


def _ensure[ModelT: models.Model](model: type[ModelT], label: str, **fields: Any) -> ModelT:
    """Create the row with the id derived from ``label`` unless it exists already."""
    instance, _ = model._default_manager.get_or_create(pk=_seed_id(label), defaults=fields)
    return instance


def _at(day: date, hour: int = 15) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _InvoiceSpec:
    number: int
    internal_status: str
    provider_status: str


class Command(BaseCommand):
    help = "LOCAL ONLY: fill the database with invented demo data. Safe to run twice."

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.APP_ENV != "local":
            raise CommandError(
                "seed_demo runs only when APP_ENV=local.", returncode=REFUSED_EXIT_CODE
            )
        with transaction.atomic():
            _Seeder().run()
        self.stdout.write(
            "seed_demo: demo data is in place "
            f"({Operator.objects.count()} operators, {Store.objects.count()} stores, "
            f"{Lease.objects.count()} leases, {Invoice.objects.count()} invoices, "
            f"{Payment.objects.count()} payments)."
        )


class _Seeder:
    def __init__(self) -> None:
        self.rng = random.Random(RANDOM_SEED)  # noqa: S311 - demo values, not security
        self.user = self._seed_user()
        self.operators: list[Operator] = []
        self.stores: list[Store] = []
        self.leases: list[Lease] = []
        self.rents: dict[int, Decimal] = {}
        #: (lease, month index, seq) -> charge
        self.charges: dict[tuple[int, int, int], RentCharge] = {}
        self.insurance: dict[tuple[int, int], InsuranceCharge] = {}
        self.provider_statuses: dict[uuid.UUID, str] = {}

    def run(self) -> None:
        self._operators()
        self._stores_and_leases()
        self._rent_history_and_schedules()
        self._rent_charges()
        self._insurance()
        adjustment = self._adjustment()
        self._invoices(adjustment)
        self._payments()
        self._notes_and_balances()
        write_demo_provider_statuses(self.provider_statuses)

    # --- people and places -------------------------------------------------------

    @staticmethod
    def _seed_user() -> User:
        user, created = User.objects.get_or_create(
            username=SEED_USER_EMAIL,
            defaults={"email": SEED_USER_EMAIL, "is_active": False, "is_staff": False},
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        return user

    def _operators(self) -> None:
        for index, (name, email) in enumerate(OPERATOR_NAMES):
            city, state = self.rng.choice(CITIES)
            enrolled = index in ENROLLED_OPERATORS
            self.operators.append(
                _ensure(
                    Operator,
                    f"operator-{index}",
                    display_name=name,
                    legal_name=name,
                    company=name,
                    email=email,
                    phone=f"+1 555 010 {index:04d}",
                    address_line1=f"{100 + index} Demo Avenue",
                    city=city,
                    state=state,
                    postal_code=f"{10000 + index * 111:05d}",
                    notes="Invented operator for local development.",
                    enrolment_status=(
                        EnrolmentStatus.ENROLLED if enrolled else EnrolmentStatus.NOT_ENROLLED
                    ),
                )
            )
            if enrolled:
                _ensure(
                    ExternalRef,
                    f"external-ref-operator-{index}",
                    entity_type="operator",
                    entity_id=self.operators[-1].pk,
                    provider=DEMO_PROVIDER,
                    external_kind="customer",
                    external_id=f"demo-customer-{index:04d}",
                )

    def _stores_and_leases(self) -> None:
        for index in range(STORE_COUNT):
            city, state = self.rng.choice(CITIES)
            prospective = index == PROSPECTIVE_LEASE
            store = _ensure(
                Store,
                f"store-{index}",
                external_label=f"Store {101 + index} {city}",
                store_number=101 + index,
                city=city,
                state=state,
                open_date=None if prospective else date(2020 + index % 5, 1 + index % 12, 1),
                open_date_note="Opening date not agreed yet." if prospective else "",
                status=StoreStatus.PROSPECTIVE if prospective else StoreStatus.ACTIVE,
            )
            self.stores.append(store)
            self.leases.append(self._lease(index, store))

    def _lease(self, index: int, store: Store) -> Lease:
        operator_index = LEASE_OPERATOR[index]
        fields: dict[str, Any] = {
            "store": store,
            "operator": None if operator_index is None else self.operators[operator_index],
            "debit_day": 1 + index % 28,
            "collection_status": CollectionStatus.ACTIVE,
            "payment_channel": PaymentChannel.PROVIDER_ACH,
            "started_on": date(2021 + index % 4, 1, 1),
        }
        if index == TWICE_MONTHLY_LEASE:
            fields.update(debit_day=1, second_debit_day=15)
        elif index == PAUSED_LEASE:
            fields.update(collection_status=CollectionStatus.PAUSED, needs_review=True)
        elif index == WIRE_LEASE:
            fields.update(payment_channel=PaymentChannel.WIRE, review_reason=ReviewReason.WIRE)
        elif index == PROSPECTIVE_LEASE:
            fields.update(
                debit_day=None,
                collection_status=CollectionStatus.PROSPECTIVE,
                payment_channel=PaymentChannel.UNKNOWN,
                started_on=None,
            )
        return _ensure(Lease, f"lease-{index}", **fields)

    # --- rent --------------------------------------------------------------------

    def _billed_leases(self) -> range:
        return range(PROSPECTIVE_LEASE)

    def _rent_history_and_schedules(self) -> None:
        for index in self._billed_leases():
            lease = self.leases[index]
            rent = self.rng.choice(RENT_AMOUNTS)
            self.rents[index] = rent
            if index < 5:
                _ensure(
                    LeaseRentHistory,
                    f"rent-history-{index}-2025",
                    lease=lease,
                    amount=rent - Decimal("100.00"),
                    effective_from=date(2025, 1, 1),
                    effective_to=date(2026, 1, 1),
                    source=RentSource.SHEET_INFERRED,
                )
            _ensure(
                LeaseRentHistory,
                f"rent-history-{index}-2026",
                lease=lease,
                amount=rent,
                effective_from=date(2026, 1, 1),
                source=RentSource.SHEET_CURRENT,
            )
            if lease.payment_channel != PaymentChannel.PROVIDER_ACH:
                continue
            state = ScheduleState.PAUSED if index == PAUSED_LEASE else ScheduleState.ACTIVE
            for seq, day in self._debit_days(lease):
                _ensure(
                    CollectionSchedule,
                    f"schedule-{index}-rent-{seq}",
                    lease=lease,
                    kind=ScheduleKind.RENT,
                    seq=seq,
                    debit_day=day,
                    amount=self._instalment(index, lease),
                    state=state,
                )

    @staticmethod
    def _debit_days(lease: Lease) -> list[tuple[int, int]]:
        days = [(1, lease.debit_day or 1)]
        if lease.second_debit_day is not None:
            days.append((2, lease.second_debit_day))
        return days

    def _instalment(self, index: int, lease: Lease) -> Decimal:
        rent = self.rents[index]
        if lease.second_debit_day is None:
            return rent
        return (rent / 2).quantize(CENT)

    def _rent_charges(self) -> None:
        for index in self._billed_leases():
            lease = self.leases[index]
            for month_index, month in enumerate(MONTHS):
                default = RentChargeStatus.DUE if month_index == 2 else RentChargeStatus.PAID
                status = CHARGE_STATUS_OVERRIDES.get((index, month_index), default)
                for seq, day in self._debit_days(lease):
                    amount_due = self._instalment(index, lease)
                    if status == RentChargeStatus.PAID:
                        amount_paid = amount_due
                    elif status == RentChargeStatus.PARTIAL:
                        amount_paid = (amount_due / 2).quantize(CENT)
                    else:
                        amount_paid = Decimal("0.00")
                    label = f"rent-charge-{index}-{month:%Y-%m}-{seq}"
                    self.charges[(index, month_index, seq)] = _ensure(
                        RentCharge,
                        label,
                        lease=lease,
                        period_month=month,
                        seq=seq,
                        due_on=month.replace(day=day),
                        amount_due=amount_due,
                        amount_paid=amount_paid,
                        status=status,
                        local_ref=_seed_id(f"{label}-local-ref"),
                    )

    # --- insurance ---------------------------------------------------------------

    def _insurance(self) -> None:
        for year, amount in PREMIUMS.items():
            _ensure(InsurancePremium, f"premium-{year}", policy_year=year, amount=amount)
        for index in self._billed_leases():
            lease = self.leases[index]
            self.insurance[(index, 2025)] = _ensure(
                InsuranceCharge,
                f"insurance-{index}-2025",
                lease=lease,
                policy_year=2025,
                due_on=date(2025, 1, 15),
                amount=PREMIUMS[2025],
                legacy=True,
                legacy_status=LegacyStatus.UNPAID if index == 6 else LegacyStatus.PAID,
            )
            self.insurance[(index, 2026)] = _ensure(
                InsuranceCharge,
                f"insurance-{index}-2026",
                lease=lease,
                policy_year=2026,
                due_on=date(2026, 1, 15),
                amount=PREMIUMS[2026],
            )
        # Next year's premium is not set yet, so its charge waits for one.
        _ensure(
            InsuranceCharge,
            "insurance-0-2027",
            lease=self.leases[0],
            policy_year=2027,
            due_on=date(2027, 1, 15),
            amount=None,
        )

    # --- adjustments, invoices, payments ------------------------------------------

    def _adjustment(self) -> ChargeAdjustment:
        existing = ChargeAdjustment.objects.filter(pk=_seed_id("adjustment-cross-store")).first()
        if existing is not None:
            return existing
        adjustment = ChargeAdjustment(
            pk=_seed_id("adjustment-cross-store"),
            from_charge=self.charges[(10, 1, 1)],
            to_charge=self.charges[(0, 2, 1)],
            amount=Decimal("150.00"),
            reason=AdjustmentReason.CROSS_STORE_BALANCE,
            note="Credit left on the second store moved to the first store (demo).",
            created_by=self.user,
        )
        adjustment.full_clean()
        adjustment.save()
        return adjustment

    def _invoice_sources(
        self, adjustment: ChargeAdjustment
    ) -> list[tuple[str, dict[str, Any], Decimal, Decimal, date, str]]:
        """(label, source fk, amount, amount paid, due date, description) per invoice."""
        sources: list[tuple[str, dict[str, Any], Decimal, Decimal, date, str]] = []
        for month_index in (1, 2):
            for index in range(10):
                charge = self.charges[(index, month_index, 1)]
                sources.append(
                    (
                        f"invoice-rent-{index}-{month_index}",
                        {"rent_charge": charge},
                        charge.amount_due,
                        charge.amount_paid,
                        charge.due_on,
                        f"Rent for {charge.period_month:%B %Y}",
                    )
                )
        for index in range(3):
            charge_2026 = self.insurance[(index, 2026)]
            amount = charge_2026.amount or PREMIUMS[2026]
            sources.append(
                (
                    f"invoice-insurance-{index}",
                    {"insurance_charge": charge_2026},
                    amount,
                    amount if index == 0 else Decimal("0.00"),
                    charge_2026.due_on,
                    "Store insurance 2026",
                )
            )
        sources.append(
            (
                "invoice-adjustment",
                {"charge_adjustment": adjustment},
                adjustment.amount,
                Decimal("0.00"),
                adjustment.to_charge.due_on,
                "Cross-store balance transfer",
            )
        )
        wire_charge = self.charges[(WIRE_LEASE, 0, 1)]
        sources.append(
            (
                "invoice-legacy-wire",
                {},
                wire_charge.amount_due,
                wire_charge.amount_due,
                wire_charge.due_on,
                "Rent for July 2026 (imported from the sheet)",
            )
        )
        return sources

    def _invoice_specs(self) -> list[_InvoiceSpec]:
        draft, ready, sent = InvoiceStatus.DRAFT, InvoiceStatus.READY, InvoiceStatus.SENT
        closed, void = InvoiceStatus.CLOSED, InvoiceStatus.VOID
        statuses = [
            # August rent: paid, except leases 7 and 8 (partial).
            *[(closed, "PAID")] * 7,
            (sent, "PARTIALLY APPLIED"),
            (sent, "PARTIALLY PAID"),
            (closed, "PAID"),
            # September rent: due or pending.
            (sent, "ATTEMPTED"),
            (sent, "SENT"),
            (sent, "VIEWED"),
            (ready, "DRAFT"),
            (ready, ""),
            (draft, ""),
            (draft, ""),
            (sent, "SENT"),
            (sent, "VIEWED"),
            (sent, "ATTEMPTED"),
            # Insurance 2026.
            (closed, "PAID"),
            (sent, "VIEWED"),
            (void, "DELETED"),
            # Cross-store adjustment, then the legacy wire invoice.
            (draft, ""),
            (closed, ""),
        ]
        return [
            _InvoiceSpec(number=number, internal_status=internal, provider_status=provider)
            for number, (internal, provider) in enumerate(statuses, start=1)
        ]

    def _invoices(self, adjustment: ChargeAdjustment) -> None:
        sources = self._invoice_sources(adjustment)
        specs = self._invoice_specs()
        for (label, source, amount, paid, due, description), spec in zip(
            sources, specs, strict=True
        ):
            operator = self._invoice_operator(source)
            sent = spec.internal_status in (InvoiceStatus.SENT, InvoiceStatus.CLOSED)
            invoice = _ensure(
                Invoice,
                label,
                invoice_number=f"INV-2026-{spec.number:04d}",
                operator=operator,
                issue_date=due - timedelta(days=10),
                due_date=due,
                memo=description,
                subtotal=amount,
                tax=Decimal("0.00"),
                fees=Decimal("0.00"),
                surcharge=Decimal("0.00"),
                total=amount,
                amount_paid=paid,
                internal_status=spec.internal_status,
                sent_at=_at(due - timedelta(days=10)) if sent else None,
                paid_at=_at(due) if paid == amount and sent else None,
                creation_key=_seed_id(f"{label}-creation-key"),
                legacy=label == "invoice-legacy-wire",
                **source,
            )
            _ensure(
                InvoiceLineItem,
                f"{label}-line-1",
                invoice=invoice,
                position=1,
                description=description,
                quantity=Decimal("1.0000"),
                unit_price=amount,
                line_total=amount,
                kind=InvoiceLineKind.CHARGE,
            )
            self.provider_statuses[invoice.pk] = spec.provider_status
            if spec.provider_status:
                ref = _ensure(
                    ExternalRef,
                    f"external-ref-{label}",
                    entity_type="invoice",
                    entity_id=invoice.pk,
                    provider=DEMO_PROVIDER,
                    external_kind="invoice",
                    external_id=f"demo-invoice-{spec.number:04d}",
                )
                _ensure(
                    ProviderSnapshot,
                    f"snapshot-{label}",
                    external_ref=ref,
                    payload={"status": spec.provider_status, "total": str(amount)},
                    fetched_at=_at(due, hour=6),
                )

    def _invoice_operator(self, source: dict[str, Any]) -> Operator:
        lease: Lease
        if "rent_charge" in source:
            lease = source["rent_charge"].lease
        elif "insurance_charge" in source:
            lease = source["insurance_charge"].lease
        elif "charge_adjustment" in source:
            lease = source["charge_adjustment"].to_charge.lease
        else:
            lease = self.leases[WIRE_LEASE]
        if lease.operator is None:
            raise CommandError("Every demo invoice belongs to a lease with an operator.")
        return lease.operator

    def _payments(self) -> None:
        for (index, month_index, seq), charge in self.charges.items():
            if (
                charge.status
                not in (
                    RentChargeStatus.PAID,
                    RentChargeStatus.PARTIAL,
                )
                and (index, month_index) != RETURNED_ACH
            ):
                continue
            label = f"payment-{index}-{month_index}-{seq}"
            fields: dict[str, Any] = {
                "rent_charge": charge,
                "invoice": charge.invoices.first(),
                "kind": PaymentKind.RENT,
                "method": PaymentMethod.ACH,
                "amount": charge.amount_paid,
                "status": PaymentStatus.SETTLED,
                "initiated_on": charge.due_on,
                "settled_on": charge.due_on + timedelta(days=3),
                "provider_created_at": _at(charge.due_on, hour=1),
                "raw_summary": {"source": "demo"},
            }
            if (index, month_index) == RETURNED_ACH:
                fields.update(
                    amount=charge.amount_due,
                    status=PaymentStatus.RETURNED,
                    settled_on=None,
                    returned_at=_at(charge.due_on + timedelta(days=4)),
                    return_code="R01",
                    return_reason="Insufficient funds (demo).",
                )
            elif index == WIRE_LEASE:
                fields.update(
                    method=PaymentMethod.WIRE,
                    note="Wire received at the bank, matched by staff (demo).",
                    reference="DEMO-WIRE-REF",
                    recorded_by=self.user,
                    provider_created_at=None,
                )
            elif (index, month_index) == ZELLE_PAYMENT:
                fields.update(
                    method=PaymentMethod.ZELLE,
                    note="Zelle transfer received from the operator (demo).",
                    recorded_by=self.user,
                    provider_created_at=None,
                )
            payment = _ensure(Payment, label, **fields)
            if (index, month_index) == REFUNDED_PAYMENT:
                _ensure(
                    Payment,
                    f"{label}-refund",
                    rent_charge=charge,
                    kind=PaymentKind.RENT,
                    method=PaymentMethod.ACH,
                    amount=Decimal("50.00"),
                    status=PaymentStatus.SETTLED,
                    is_refund=True,
                    parent_payment=payment,
                    initiated_on=charge.due_on + timedelta(days=5),
                    settled_on=charge.due_on + timedelta(days=8),
                    note="Refund of a duplicate late fee (demo).",
                    recorded_by=self.user,
                )
        for index in range(5):
            charge_2026 = self.insurance[(index, 2026)]
            _ensure(
                Payment,
                f"payment-insurance-{index}-2026",
                insurance_charge=charge_2026,
                invoice=charge_2026.invoices.first(),
                kind=PaymentKind.INSURANCE,
                policy_year=2026,
                method=PaymentMethod.ACH,
                amount=charge_2026.amount,
                status=PaymentStatus.SETTLED,
                initiated_on=charge_2026.due_on,
                settled_on=charge_2026.due_on + timedelta(days=3),
            )

    # --- notes and balances --------------------------------------------------------

    def _notes_and_balances(self) -> None:
        _ensure(
            Note,
            "note-operator-0",
            entity_type=NoteEntityType.OPERATOR,
            entity_id=self.operators[0].pk,
            body="Prefers e-mail over phone calls (demo).",
            author=self.user,
            author_label="Seed data",
        )
        _ensure(
            Note,
            "note-store-prospective",
            entity_type=NoteEntityType.STORE,
            entity_id=self.stores[PROSPECTIVE_LEASE].pk,
            body="Lease terms still under negotiation (demo).",
            author=self.user,
            author_label="Seed data",
        )
        _ensure(
            ChargeNote,
            "charge-note-returned",
            rent_charge=self.charges[(*RETURNED_ACH, 1)],
            body="ACH debit returned; operator asked to confirm the bank method (demo).",
            author=self.user,
            author_label="Seed data",
        )
        _ensure(
            ChargeNote,
            "charge-note-insurance",
            insurance_charge=self.insurance[(6, 2025)],
            body="Imported as unpaid from the sheet (demo).",
            author_label="Sheet import",
        )
        _ensure(
            StoreBalance,
            "store-balance-0-insurance",
            store=self.stores[0],
            kind=StoreBalanceKind.INSURANCE_OPENING,
            amount=Decimal("595.00"),
        )
        _ensure(
            StoreBalance,
            "store-balance-1-sign",
            store=self.stores[1],
            kind=StoreBalanceKind.SIGN,
            amount=Decimal("-50.00"),
            needs_review=True,
        )
