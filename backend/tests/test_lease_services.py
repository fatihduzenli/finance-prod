"""Lease services (docs/qa/PLAN-007-leases-and-rent-run.md §6 rows 1, 3 and 4; D2 to D5).

``payments.get_provider`` is replaced by a :class:`~payments.fake.FakeProvider`, or by
the Stax adapter on a mock transport where the exact request body matters. Nothing
opens a socket.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    Lease,
    LeaseBilling,
    LeaseRentHistory,
    Operator,
    PaymentChannel,
    ScheduleState,
    Store,
    StoreStatus,
)
from ledger.services import leases as service
from ledger.services.leases import AutopayAction, LeaseRefused
from ledger.wording import format_amount
from payments.errors import CircuitOpen, ProviderError, ProviderRejected, ProviderUnavailable
from payments.fake import FakeProvider
from payments.models import ExternalRef, ProviderSnapshot
from payments.provider import (
    PAYMENT_METHOD_DELETED,
    PaymentMethodRecord,
    ScheduleInput,
    ScheduleRecord,
    ScheduleUpdate,
    mask_identifier,
)
from payments.stax.adapter import DEFAULT_PAYMENT_PAGE_URL, StaxProvider
from tests.auth_helpers import make_user
from tests.stax_helpers import make_rig, sequence
from tests.test_lease_sync import requires_postgresql

pytestmark = pytest.mark.django_db

D = Decimal
#: 2026-10-01 15:00 UTC: the 1st's 12:00 debit time has passed.
NOW = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
CUSTOMER = "cus-1"
BANK = "pm-bank-1"
RENT = Decimal("1500.00")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def actor() -> User:
    user, _ = make_user("admin-leases", Role.ADMIN)
    return user


def method(
    external_id: str = BANK, kind: str = "bank", raw_status: str = ""
) -> PaymentMethodRecord:
    return PaymentMethodRecord(
        external_id=external_id,
        raw_status=raw_status,
        fetched_at=NOW,
        method=kind,
        last_four="6789",
        bank_type="checking",
        nickname="Operating",
    )


#: What :func:`make_operator` was given to bill with, by operator: (billing e-mail,
#: provider customer). PLAN-014 C3 moved both to the lease; :func:`bill_lease` gives them
#: to each lease made for the operator.
_OPERATOR_BILLING: dict[uuid.UUID, tuple[str | None, str | None]] = {}


def make_operator(
    email: str | None = "ops@example.com", customer: str | None = CUSTOMER
) -> Operator:
    """An operator with ``email``. The billing e-mail and the provider ``customer`` are the
    lease's since PLAN-014 C3: :func:`make_lease` (or :func:`bill_lease`) gives them to
    each lease of the operator, as ledger migration 0059 copied them."""
    operator = Operator.objects.create(display_name="Northwind Stores", email=email)
    _OPERATOR_BILLING[operator.pk] = (email, customer)
    return operator


def bill_lease(lease: Lease) -> Lease:
    """Give ``lease`` the billing :func:`make_operator` was given for its operator: a
    billing row with that e-mail and the provider customer reference. The first lease of
    the customer holds the reference; a later one shares its account (one hop), as
    migration 0059 re-pointed an operator's references. Nothing for any other operator."""
    if lease.operator_id is None or lease.operator_id not in _OPERATOR_BILLING:
        return lease
    email, customer = _OPERATOR_BILLING[lease.operator_id]
    if not email and not customer:
        return lease
    holder = (
        ExternalRef.objects.filter(
            entity_type="lease",
            provider="stax_pay",
            external_kind="customer",
            external_id=customer,
            superseded_at__isnull=True,
        )
        .exclude(entity_id=lease.pk)
        .first()
        if customer
        else None
    )
    billing, _ = LeaseBilling.objects.update_or_create(
        lease=lease,
        defaults={
            "email": email or "",
            "shares_account_of": (
                Lease.objects.get(pk=holder.entity_id) if holder is not None else None
            ),
        },
    )
    if customer and holder is None:
        ExternalRef.objects.get_or_create(
            entity_type="lease",
            entity_id=lease.pk,
            provider="stax_pay",
            external_kind="customer",
            external_id=customer,
        )
    lease.billing = billing
    return lease


def lease_fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "store": Store.objects.create(external_label="Store 0101", status=StoreStatus.ACTIVE),
        "operator": None,
        "debit_day": 15,
        "second_debit_day": None,
        "payment_channel": PaymentChannel.PROVIDER_ACH,
        "started_on": date(2026, 1, 1),
        "collection_status": CollectionStatus.ACTIVE,
    }
    if "operator" not in overrides:
        values["operator"] = make_operator()
    return {**values, **overrides}


def make_lease(rent: Decimal = RENT, **overrides: Any) -> Lease:
    """A lease saved through the service the day before it starts. Saving runs the rent
    run's catch-up up to the day it is saved (PLAN-012b-b D2), so this lease has no
    charge until a run creates one; ``test_rent_catch_up.py`` saves leases late."""
    fields = lease_fields(**overrides)
    started: date | None = fields["started_on"]
    lease, created = service.create_lease(
        fields,
        rent=rent,
        creation_key=uuid.uuid4(),
        actor=None,
        today=started - timedelta(days=1) if started is not None else None,
    )
    assert created
    return bill_lease(lease)


def edit(lease: Lease, rent: Decimal, starts: date | None = None, **changes: Any) -> Lease:
    fields = {name: getattr(lease, name) for name in service.LEASE_FIELDS}
    return service.update_lease(
        lease.pk, {**fields, **changes}, rent=rent, rent_effective_from=starts, actor=None
    )


def enable(lease: Lease, method_id: str = BANK) -> service.AutopayOutcome:
    return service.enable_autopay(lease.pk, method_id, actor=None, now=NOW)


def creates(fake: FakeProvider) -> list[ScheduleInput]:
    return [args[0] for name, args in fake.calls if name == "create_scheduled_invoice"]  # type: ignore[misc]


def updates(fake: FakeProvider) -> list[tuple[str, ScheduleUpdate]]:
    return [
        (str(args[0]), args[1])  # type: ignore[misc]
        for name, args in fake.calls
        if name == "update_scheduled_invoice"
    ]


def schedule_ids(lease: Lease) -> list[str]:
    return [service.schedule_ref(row) for row in service.live_schedules(lease)]


def actions(lease: Lease) -> list[str]:
    return list(
        AuditLog.objects.filter(entity_type="lease", entity_id=str(lease.pk))
        .order_by("id")
        .values_list("action", flat=True)
    )


# --- Row 1: lease, rent history, split --------------------------------------------------------


def test_create_writes_the_lease_its_first_rent_row_and_one_audit_row(actor: User) -> None:
    fields = lease_fields()
    lease, created = service.create_lease(
        fields, rent=D("1500.00"), creation_key=uuid.uuid4(), actor=actor
    )
    assert created
    [row] = LeaseRentHistory.objects.filter(lease=lease)
    assert (row.amount, row.effective_from, row.effective_to) == (
        D("1500.00"),
        date(2026, 1, 1),
        None,
    )
    [entry] = AuditLog.objects.filter(entity_id=str(lease.pk))
    assert entry.action == "lease.created" and entry.actor == actor
    assert entry.after is not None and entry.after["rent"] == "1500.00"


def test_create_without_a_start_date_starts_the_rent_today() -> None:
    lease, _ = service.create_lease(
        lease_fields(started_on=None),
        rent=D("10.00"),
        creation_key=uuid.uuid4(),
        actor=None,
        today=date(2026, 10, 1),
    )
    assert service.current_rent(lease).effective_from == date(2026, 10, 1)  # type: ignore[union-attr]


def test_a_replayed_creation_key_makes_one_lease() -> None:
    fields = lease_fields()
    key = uuid.uuid4()
    first, created = service.create_lease(fields, rent=D("1500.00"), creation_key=key, actor=None)
    again, created_again = service.create_lease(
        {**fields, "debit_day": 3}, rent=D("9.99"), creation_key=key, actor=None
    )
    assert (created, created_again) == (True, False)
    assert again.pk == first.pk and Lease.objects.count() == 1
    assert LeaseRentHistory.objects.count() == 1
    assert service.lease_for_creation_key(str(key)) == first
    assert service.lease_for_creation_key("not-a-uuid") is None


def test_a_rent_change_closes_the_open_row_the_day_before_and_opens_a_new_one() -> None:
    lease = make_lease()
    edit(lease, D("1650.00"), date(2026, 11, 1))
    old, new = LeaseRentHistory.objects.filter(lease=lease).order_by("effective_from")
    assert (old.amount, old.effective_from, old.effective_to) == (
        D("1500.00"),
        date(2026, 1, 1),
        date(2026, 10, 31),
    )
    assert (new.amount, new.effective_from, new.effective_to) == (
        D("1650.00"),
        date(2026, 11, 1),
        None,
    )
    entry = AuditLog.objects.filter(action="lease.updated").get()
    assert (entry.before or {})["rent"] == "1500.00" and (entry.after or {})["rent"] == "1650.00"


def test_an_unchanged_rent_writes_no_history_and_an_unchanged_lease_no_audit() -> None:
    lease = make_lease()
    edit(lease, D("1500.00"), date(2026, 11, 1))
    assert LeaseRentHistory.objects.filter(lease=lease).count() == 1
    assert actions(lease) == ["lease.created"]
    edit(lease, D("1500.00"), debit_day=20)
    assert actions(lease) == ["lease.created", "lease.updated"]


@pytest.mark.parametrize("starts", [date(2026, 1, 1), date(2026, 1, 2), date(2025, 12, 1)])
def test_a_rent_change_too_close_to_the_current_start_is_refused_and_nothing_changes(
    starts: date,
) -> None:
    lease = make_lease()
    with pytest.raises(LeaseRefused) as raised:
        edit(lease, D("1650.00"), starts, debit_day=20)
    assert raised.value.field == "rent_effective_from"
    assert "Jan 1, 2026" in str(raised.value)
    lease.refresh_from_db()
    assert lease.debit_day == 15
    [row] = LeaseRentHistory.objects.filter(lease=lease)
    assert (row.amount, row.effective_to) == (D("1500.00"), None)
    assert actions(lease) == ["lease.created"]


def test_the_rent_defaults_to_today_when_no_date_is_given() -> None:
    lease = make_lease()
    service.update_lease(
        lease.pk,
        {name: getattr(lease, name) for name in service.LEASE_FIELDS},
        rent=D("1600.00"),
        rent_effective_from=None,
        actor=None,
        today=date(2026, 10, 1),
    )
    assert service.current_rent(lease).effective_from == date(2026, 10, 1)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("rent", "twice", "expected"),
    [
        (D("1500.00"), False, (D("1500.00"),)),
        (D("1500.00"), True, (D("750.00"), D("750.00"))),
        (D("1000.01"), True, (D("500.01"), D("500.00"))),
        (D("0.03"), True, (D("0.02"), D("0.01"))),
        (D("12581.99"), True, (D("6291.00"), D("6290.99"))),
    ],
)
def test_the_twice_monthly_split_gives_the_odd_cent_to_the_first_debit(
    rent: Decimal, twice: bool, expected: tuple[Decimal, ...]
) -> None:
    amounts = service.debit_amounts(rent, twice_monthly=twice)
    assert amounts == expected
    assert sum(amounts, D(0)) == rent
    assert all(
        isinstance(amount, Decimal) and amount.as_tuple().exponent == -2 for amount in amounts
    )


def test_planned_debits_follow_the_debit_days() -> None:
    lease = make_lease(D("1000.01"), debit_day=5, second_debit_day=20)
    assert service.planned_debits(lease, D("1000.01")) == [
        service.PlannedDebit(1, 5, D("500.01")),
        service.PlannedDebit(2, 20, D("500.00")),
    ]


@pytest.mark.parametrize(
    ("day", "now", "expected"),
    [
        (15, datetime(2026, 10, 1, 15, 0, tzinfo=UTC), datetime(2026, 10, 15, 12, tzinfo=UTC)),
        (1, datetime(2026, 10, 1, 11, 59, 59, tzinfo=UTC), datetime(2026, 10, 1, 12, tzinfo=UTC)),
        (1, datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC), datetime(2026, 11, 1, 12, tzinfo=UTC)),
        (28, datetime(2026, 12, 29, 0, 0, tzinfo=UTC), datetime(2027, 1, 28, 12, tzinfo=UTC)),
        (31, datetime(2026, 2, 1, 0, 0, tzinfo=UTC), datetime(2026, 3, 31, 12, tzinfo=UTC)),
    ],
)
def test_dtstart_is_the_next_occurrence_strictly_in_the_future(
    day: int, now: datetime, expected: datetime
) -> None:
    assert service.next_debit_at(day, now) == expected
    assert service.next_debit_at(day, now) > now
    assert service.schedule_rule(day, now) == f"DTSTART={expected:%Y%m%dT%H%M%SZ};FREQ=MONTHLY"


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"))
def test_the_large_debit_warning_starts_above_the_threshold() -> None:
    assert not service.is_large_debit(D("2000.00"))
    assert service.is_large_debit(D("2000.01"))


# --- Row 3: enable autopay ----------------------------------------------------------------------


def test_enable_creates_one_schedule_per_debit_day_with_the_planned_fields(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(D("1000.01"), debit_day=5, second_debit_day=20)
    outcome = enable(lease)
    assert outcome.action is AutopayAction.ENABLED
    first, second = creates(fake)
    for schedule, day, amount in ((first, 5, D("500.01")), (second, 20, D("500.00"))):
        assert schedule.customer_external_id == CUSTOMER
        assert schedule.payment_method_external_id == BANK
        assert schedule.amounts.total == amount
        assert schedule.rule == f"DTSTART=202610{day:02d}T120000Z;FREQ=MONTHLY"
        assert schedule.email_notification is True
        [line] = schedule.amounts.line_items
        assert (line.description, line.quantity, line.unit_price) == (
            "Rent, store Store 0101",
            D(1),
            amount,
        )
    rows = service.live_schedules(lease)
    assert [(r.seq, r.debit_day, r.amount, r.state) for r in rows] == [
        (1, 5, D("500.01"), ScheduleState.ACTIVE),
        (2, 20, D("500.00"), ScheduleState.ACTIVE),
    ]
    assert len(set(schedule_ids(lease))) == 2
    assert actions(lease).count("lease.autopay_enabled") == 2


def test_a_monthly_lease_gets_one_schedule_for_the_whole_rent(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(D("1500.00"))
    enable(lease)
    [schedule] = creates(fake)
    assert schedule.amounts.total == D("1500.00")


def test_no_email_means_no_email_notification(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(operator=make_operator(email=None))
    enable(lease)
    [schedule] = creates(fake)
    assert schedule.email_notification is False
    assert service.NO_EMAIL in service.enrolment_notes(lease)


def test_a_second_click_creates_nothing_and_calls_nothing(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(debit_day=5, second_debit_day=20)
    enable(lease)
    calls = len(fake.calls)
    outcome = enable(lease)
    assert outcome.action is AutopayAction.ALREADY
    assert outcome.message == service.ALREADY_ENABLED
    assert len(fake.calls) == calls
    assert CollectionSchedule.objects.filter(lease=lease).count() == 2
    assert ExternalRef.objects.filter(external_kind="schedule").count() == 2


def test_the_schedule_create_body_is_exactly_the_planned_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig(
        sequence(
            [
                httpx.Response(200, json=[{"id": BANK, "method": "bank", "customer_id": CUSTOMER}]),
                httpx.Response(200, json={"id": "sch-1", "active": True}),
            ]
        )
    )
    monkeypatch.setattr("payments.get_provider", lambda: StaxProvider(rig.transport))
    lease = make_lease(D("1000.01"), debit_day=5, second_debit_day=None)
    with override_settings(STAX_CALLER="worker"):
        outcome = enable(lease)
    assert outcome.action is AutopayAction.ENABLED
    listing, create = rig.recorder.requests
    assert (listing.method, listing.url.path) == ("GET", "/customer/cus-1/payment-method")
    assert (create.method, create.url.path) == ("POST", "/invoice/schedule/")
    assert b'"total":1000.01' in create.content
    assert json.loads(create.content, parse_float=Decimal) == {
        "url": DEFAULT_PAYMENT_PAGE_URL,
        "total": D("1000.01"),
        "rule": "DTSTART=20261005T120000Z;FREQ=MONTHLY",
        "customer_id": CUSTOMER,
        "payment_method_id": BANK,
        "email_notification": True,
        "meta": {
            "lineItems": [{"item": "Rent, store Store 0101", "quantity": 1, "price": D("1000.01")}]
        },
    }
    assert schedule_ids(lease) == ["sch-1"]


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (method("pm-card-1", kind="card"), service.CARD_REFUSED),
        (method(BANK, raw_status=PAYMENT_METHOD_DELETED), service.METHOD_DELETED),
        (method("pm-other", kind="paypal"), service.CARD_REFUSED),
    ],
    ids=["card", "deleted", "unknown-kind"],
)
def test_a_card_or_unusable_method_is_refused_even_when_posted_directly(
    fake: FakeProvider, record: PaymentMethodRecord, message: str
) -> None:
    fake.add_payment_method(CUSTOMER, record)
    lease = make_lease()
    outcome = enable(lease, record.external_id)
    assert (outcome.action, outcome.message) == (AutopayAction.REFUSED, message)
    assert creates(fake) == []
    assert CollectionSchedule.objects.count() == 0
    entry = AuditLog.objects.filter(action="lease.autopay_refused").get()
    assert record.external_id not in json.dumps(entry.after)


def test_a_method_of_another_customer_is_refused(fake: FakeProvider) -> None:
    fake.add_payment_method("cus-2", method("pm-of-cus-2"))
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    outcome = enable(lease, "pm-of-cus-2")
    assert (outcome.action, outcome.message) == (AutopayAction.REFUSED, service.METHOD_NOT_FOUND)
    assert creates(fake) == []


@pytest.mark.parametrize("bad", ["", "pm 1", "../pm", "pm/1", "x" * 256])
def test_a_malformed_method_id_is_refused_before_any_call(fake: FakeProvider, bad: str) -> None:
    lease = make_lease()
    outcome = enable(lease, bad)
    assert (outcome.action, outcome.message) == (AutopayAction.REFUSED, service.CHOOSE_METHOD)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"payment_channel": PaymentChannel.ZELLE}, service.NOT_PROVIDER_CHANNEL),
        ({"collection_status": CollectionStatus.PAUSED}, service.NOT_ACTIVE),
        ({"debit_day": 29}, service.DAY_NOT_SCHEDULABLE),
        ({"debit_day": 10, "second_debit_day": 31}, service.DAY_NOT_SCHEDULABLE),
        (
            {"operator": None, "collection_status": CollectionStatus.PROSPECTIVE},
            service.NO_OPERATOR,
        ),
    ],
)
def test_local_blockers_refuse_before_any_call(
    fake: FakeProvider, overrides: dict[str, Any], message: str
) -> None:
    lease = make_lease(**overrides)
    assert message in service.autopay_blockers(lease)
    outcome = enable(lease)
    assert outcome.action is AutopayAction.REFUSED
    assert fake.calls == []


def test_an_operator_without_a_stax_customer_is_a_blocker(fake: FakeProvider) -> None:
    lease = make_lease(operator=make_operator(customer=None))
    assert service.NO_PROVIDER_CUSTOMER in service.autopay_blockers(lease)
    assert enable(lease).message == service.NO_PROVIDER_CUSTOMER
    assert service.payment_method_choices(lease).error == service.NO_PROVIDER_CUSTOMER
    assert fake.calls == []


def test_an_archived_operator_is_a_blocker(fake: FakeProvider) -> None:
    operator = make_operator()
    lease = make_lease(operator=operator)
    Operator.objects.filter(pk=operator.pk).update(archived_at=NOW)
    lease.refresh_from_db()
    assert enable(lease).message == service.OPERATOR_ARCHIVED
    assert fake.calls == []


def test_a_provider_rejection_leaves_the_lease_unchanged_and_names_the_fields(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    before = Lease.objects.filter(pk=lease.pk).values().get()
    fake.fail_next(
        "create_scheduled_invoice",
        ProviderRejected(422, "CANARY-provider-text", error_fields=("payment_method_id",)),
    )
    outcome = enable(lease)
    assert outcome.action is AutopayAction.FAILED
    assert outcome.fields == ("payment_method_id",)
    assert outcome.text == ("Stax refused the request. Fields Stax rejected: payment_method_id.")
    assert "CANARY" not in outcome.text
    assert Lease.objects.filter(pk=lease.pk).values().get() == before
    [row] = CollectionSchedule.objects.filter(lease=lease)
    assert row.state == ScheduleState.FAILED
    assert schedule_ids(lease) == [""]
    entry = AuditLog.objects.filter(action="lease.autopay_failed").get()
    assert entry.after == {"seq": 1, "failure": "ProviderRejected", "fields": ["payment_method_id"]}


def test_a_failed_method_list_creates_nothing(fake: FakeProvider) -> None:
    lease = make_lease()
    fake.fail_next("list_payment_methods", CircuitOpen("x"))
    outcome = enable(lease)
    assert (outcome.action, outcome.failure) == (AutopayAction.FAILED, "CircuitOpen")
    assert outcome.message == service.PROVIDER_FAILED
    assert creates(fake) == [] and CollectionSchedule.objects.count() == 0


def test_the_second_debit_failing_keeps_the_first_and_a_retry_creates_only_the_second(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(debit_day=5, second_debit_day=20)

    original = fake.create_scheduled_invoice
    attempts: list[int] = []

    def create(schedule: ScheduleInput) -> ScheduleRecord:
        attempts.append(1)
        if len(attempts) == 2:
            raise ProviderRejected(422, error_fields=("rule",))
        return original(schedule)

    fake.create_scheduled_invoice = create  # type: ignore[method-assign]
    assert enable(lease).action is AutopayAction.FAILED
    first, second = service.live_schedules(lease)
    assert (first.state, second.state) == (ScheduleState.ACTIVE, ScheduleState.FAILED)
    assert service.schedule_ref(first) and not service.schedule_ref(second)

    assert enable(lease).action is AutopayAction.ENABLED
    assert len(attempts) == 3
    assert all(service.schedule_ref(row) for row in service.live_schedules(lease))
    assert CollectionSchedule.objects.filter(lease=lease).count() == 2


def _provider_schedule(fake: FakeProvider, external_id: str, day: int, total: Decimal) -> None:
    fake.schedules[external_id] = ScheduleRecord(
        external_id=external_id,
        raw_status="",
        fetched_at=NOW,
        customer_external_id=CUSTOMER,
        total=total,
        rule=f"DTSTART=202610{day:02d}T120000Z;FREQ=MONTHLY",
        active=True,
    )


def test_a_retry_after_an_unanswered_create_adopts_the_schedule_it_made(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    fake.fail_next("create_scheduled_invoice", ProviderUnavailable(None))
    assert enable(lease).action is AutopayAction.FAILED
    # The unanswered request did reach the provider and made the schedule.
    _provider_schedule(fake, "sch-made", 15, D("1500.00"))
    _provider_schedule(fake, "sch-other-day", 16, D("1500.00"))
    _provider_schedule(fake, "sch-other-amount", 15, D("1499.99"))

    outcome = enable(lease)
    assert outcome.action is AutopayAction.ENABLED
    assert len(creates(fake)) == 1  # only the first, failed attempt
    assert schedule_ids(lease) == ["sch-made"]
    assert "lease.autopay_adopted" in actions(lease)


def test_several_matching_provider_schedules_are_never_guessed(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    fake.fail_next("create_scheduled_invoice", ProviderUnavailable(None))
    enable(lease)
    _provider_schedule(fake, "sch-a", 15, D("1500.00"))
    _provider_schedule(fake, "sch-b", 15, D("1500.00"))
    outcome = enable(lease)
    assert (outcome.action, outcome.message) == (AutopayAction.REFUSED, service.SEVERAL_MATCHES)
    assert len(creates(fake)) == 1
    [row] = service.live_schedules(lease)
    assert (row.state, row.needs_review) == (ScheduleState.FAILED, True)


# --- Row 4: pause and resume ---------------------------------------------------------------------


def test_pause_sends_only_active_false(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(debit_day=5, second_debit_day=20)
    enable(lease)
    outcome = service.pause_autopay(lease.pk, actor=None)
    assert outcome.action is AutopayAction.PAUSED
    sent = updates(fake)
    assert [update for _, update in sent] == [ScheduleUpdate(active=False)] * 2
    assert sorted(ref for ref, _ in sent) == sorted(schedule_ids(lease))
    assert {row.state for row in service.live_schedules(lease)} == {ScheduleState.PAUSED}
    assert actions(lease).count("lease.autopay_paused") == 2

    assert service.pause_autopay(lease.pk, actor=None).action is AutopayAction.ALREADY
    assert len(updates(fake)) == 2


def test_resume_sends_active_true_and_a_new_future_dtstart_only(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(debit_day=5, second_debit_day=20)
    enable(lease)
    service.pause_autopay(lease.pk, actor=None)
    later = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    outcome = service.resume_autopay(lease.pk, actor=None, now=later)
    assert outcome.action is AutopayAction.RESUMED
    resumed = [update for _, update in updates(fake)][2:]
    assert resumed == [
        ScheduleUpdate(active=True, rule="DTSTART=20261205T120000Z;FREQ=MONTHLY"),
        ScheduleUpdate(active=True, rule="DTSTART=20261120T120000Z;FREQ=MONTHLY"),
    ]
    assert {row.state for row in service.live_schedules(lease)} == {ScheduleState.ACTIVE}
    assert service.resume_autopay(lease.pk, actor=None).action is AutopayAction.ALREADY


def test_pause_and_resume_bodies_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider()
    fake.add_payment_method(CUSTOMER, method())
    monkeypatch.setattr("payments.get_provider", lambda: fake)
    lease = make_lease()
    enable(lease)
    [ref] = schedule_ids(lease)
    rig = make_rig(lambda request: httpx.Response(200, json={"id": ref}))
    monkeypatch.setattr("payments.get_provider", lambda: StaxProvider(rig.transport))
    with override_settings(STAX_CALLER="worker"):
        service.pause_autopay(lease.pk, actor=None)
        service.resume_autopay(lease.pk, actor=None, now=NOW)
    pause, resume = rig.recorder.requests
    assert (pause.method, pause.url.path) == ("PUT", f"/invoice/schedule/{ref}")
    assert json.loads(pause.content) == {"active": False}
    assert json.loads(resume.content) == {
        "active": True,
        "rule": "DTSTART=20261015T120000Z;FREQ=MONTHLY",
    }


def test_a_pause_failure_keeps_the_schedule_active_and_names_the_fields(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    enable(lease)
    fake.fail_next("update_scheduled_invoice", ProviderRejected(422, error_fields=("active",)))
    outcome = service.pause_autopay(lease.pk, actor=None)
    assert (outcome.action, outcome.fields) == (AutopayAction.FAILED, ("active",))
    [row] = service.live_schedules(lease)
    assert row.state == ScheduleState.ACTIVE
    assert "lease.autopay_pause_failed" in actions(lease)


# --- D5: changes under an active schedule --------------------------------------------------------


def test_a_rent_change_flags_the_schedule_and_it_is_never_edited_for_an_amount(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    enable(lease)
    [old_ref] = schedule_ids(lease)
    edit(lease, D("1650.00"), date(2026, 10, 1))
    [row] = service.live_schedules(lease)
    assert (row.needs_review, row.amount) == (True, D("1500.00"))
    assert service.schedule_review_message(lease, row, date(2026, 10, 1)) == (
        service.SCHEDULE_DIFFERS
    )

    assert enable(lease).message == service.PAUSE_FIRST
    service.pause_autopay(lease.pk, actor=None)
    assert service.resume_autopay(lease.pk, actor=None).message == service.RESUME_REFUSED

    assert enable(lease).action is AutopayAction.ENABLED
    old, new = CollectionSchedule.objects.filter(lease=lease).order_by("created_at")
    assert (old.state, old.amount) == (ScheduleState.ENDED, D("1500.00"))
    assert (new.state, new.amount, new.needs_review) == (ScheduleState.ACTIVE, D("1650.00"), False)
    assert schedule_ids(lease) != [old_ref]
    assert [schedule.amounts.total for schedule in creates(fake)] == [D("1500.00"), D("1650.00")]
    # Only the pause touched the old schedule, and no update carries an amount.
    assert updates(fake) == [(old_ref, ScheduleUpdate(active=False))]
    assert "lease.autopay_schedule_deleted" in actions(lease)


# --- PLAN-007a: stale schedules are deleted at the provider before any create ---------------


def _stale_paused(fake: FakeProvider, **overrides: Any) -> tuple[Lease, list[str]]:
    """An enabled lease whose rent then changed (flagged) and which was paused."""
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(**overrides)
    assert enable(lease).action is AutopayAction.ENABLED
    edit(lease, D("1650.00"), date(2026, 10, 1))
    assert service.pause_autopay(lease.pk, actor=None).action is AutopayAction.PAUSED
    rows = service.live_schedules(lease)
    assert all(row.needs_review and row.state == ScheduleState.PAUSED for row in rows)
    fake.calls.clear()
    return lease, schedule_ids(lease)


def _provider_calls(fake: FakeProvider) -> list[str]:
    return [
        name
        for name, _ in fake.calls
        if name in ("delete_scheduled_invoice", "create_scheduled_invoice")
    ]


def test_re_enabling_deletes_the_stale_schedule_before_the_create(fake: FakeProvider) -> None:
    lease, [old_ref] = _stale_paused(fake)
    assert enable(lease).action is AutopayAction.ENABLED
    assert _provider_calls(fake) == ["delete_scheduled_invoice", "create_scheduled_invoice"]
    assert ("delete_scheduled_invoice", (old_ref,)) in fake.calls
    assert old_ref not in fake.schedules
    old, new = CollectionSchedule.objects.filter(lease=lease).order_by("created_at")
    assert (old.state, new.state) == (ScheduleState.ENDED, ScheduleState.ACTIVE)
    entry = AuditLog.objects.filter(action="lease.autopay_schedule_deleted").get()
    assert entry.after == {"state": ScheduleState.ENDED, "schedule": mask_identifier(old_ref)}
    assert old_ref not in json.dumps(entry.after)


@pytest.mark.parametrize(
    "error",
    [
        ProviderRejected(422, error_fields=("id",)),
        ProviderRejected(403),
        ProviderUnavailable(503),
        CircuitOpen("x"),
    ],
    ids=["422", "403", "unavailable", "circuit-open"],
)
def test_a_failed_delete_creates_nothing_and_ends_nothing(
    fake: FakeProvider, error: ProviderError
) -> None:
    lease, refs = _stale_paused(fake)
    before = list(CollectionSchedule.objects.filter(lease=lease).values())
    fake.fail_next("delete_scheduled_invoice", error)
    outcome = enable(lease)
    assert outcome.action is AutopayAction.FAILED
    assert outcome.failure == type(error).__name__
    if isinstance(error, ProviderRejected):
        assert outcome.fields == error.error_fields
    assert _provider_calls(fake) == ["delete_scheduled_invoice"]
    assert creates(fake) == []
    assert list(CollectionSchedule.objects.filter(lease=lease).values()) == before
    assert schedule_ids(lease) == refs
    assert "lease.autopay_schedule_deleted" not in actions(lease)
    assert "lease.autopay_delete_failed" in actions(lease)


def test_a_404_on_delete_counts_as_gone_and_the_create_proceeds(fake: FakeProvider) -> None:
    lease, [old_ref] = _stale_paused(fake)
    del fake.schedules[old_ref]  # already deleted at the provider
    assert enable(lease).action is AutopayAction.ENABLED
    assert _provider_calls(fake) == ["delete_scheduled_invoice", "create_scheduled_invoice"]
    old, new = CollectionSchedule.objects.filter(lease=lease).order_by("created_at")
    assert (old.state, new.state) == (ScheduleState.ENDED, ScheduleState.ACTIVE)
    assert "lease.autopay_schedule_deleted" in actions(lease)


def test_two_stale_schedules_are_both_deleted_before_either_create(fake: FakeProvider) -> None:
    lease, refs = _stale_paused(fake, debit_day=5, second_debit_day=20)
    assert len(refs) == 2
    assert enable(lease).action is AutopayAction.ENABLED
    assert _provider_calls(fake) == [
        "delete_scheduled_invoice",
        "delete_scheduled_invoice",
        "create_scheduled_invoice",
        "create_scheduled_invoice",
    ]
    deleted = [args[0] for name, args in fake.calls if name == "delete_scheduled_invoice"]
    assert sorted(deleted) == sorted(refs)  # type: ignore[type-var]
    states = CollectionSchedule.objects.filter(lease=lease).values_list("state", flat=True)
    assert sorted(states) == sorted([ScheduleState.ENDED] * 2 + [ScheduleState.ACTIVE] * 2)
    assert actions(lease).count("lease.autopay_schedule_deleted") == 2


def test_the_second_delete_failing_ends_nothing_and_a_retry_treats_the_first_as_gone(
    fake: FakeProvider,
) -> None:
    lease, refs = _stale_paused(fake, debit_day=5, second_debit_day=20)
    original = fake.delete_scheduled_invoice
    attempts: list[str] = []

    def delete(external_id: str) -> None:
        attempts.append(external_id)
        if len(attempts) == 2:
            raise ProviderUnavailable(503)
        original(external_id)

    fake.delete_scheduled_invoice = delete  # type: ignore[method-assign]
    assert enable(lease).action is AutopayAction.FAILED
    assert creates(fake) == []
    assert schedule_ids(lease) == refs
    assert all(row.state == ScheduleState.PAUSED for row in service.live_schedules(lease))

    assert enable(lease).action is AutopayAction.ENABLED  # the first now answers 404
    assert len(creates(fake)) == 2
    assert actions(lease).count("lease.autopay_schedule_deleted") == 2


# --- QA owner's decision on future-dated rent (2026-10-01) ---------------------------------------


def test_current_rent_is_the_row_in_effect_today_and_a_later_row_is_upcoming() -> None:
    lease = make_lease()
    edit(lease, D("1650.00"), date(2026, 11, 1))
    today = date(2026, 10, 1)
    assert service.current_rent(lease, today).amount == D("1500.00")  # type: ignore[union-attr]
    assert service.upcoming_rent(lease, today).amount == D("1650.00")  # type: ignore[union-attr]
    assert service.open_rent(lease).amount == D("1650.00")  # type: ignore[union-attr]
    # The day before, the day of, and after the change.
    assert service.rent_on(lease, date(2026, 10, 31)).amount == D("1500.00")  # type: ignore[union-attr]
    assert service.current_rent(lease, date(2026, 11, 1)).amount == D("1650.00")  # type: ignore[union-attr]
    assert service.upcoming_rent(lease, date(2026, 11, 1)) is None
    assert service.rent_on(lease, date(2025, 12, 31)) is None


def test_a_future_rent_change_flags_with_the_dated_message_and_early_re_enabling_keeps_it(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    enable(lease)
    edit(lease, D("1650.00"), date(2026, 11, 1))
    [row] = service.live_schedules(lease)
    expected = service.RENT_CHANGES.format(date="Nov 1, 2026")
    assert expected == (
        "Rent changes on Nov 1, 2026; pause and re-enable autopay on or after that date."
    )
    assert row.needs_review
    assert service.schedule_review_message(lease, row, date(2026, 10, 1)) == expected

    # Re-enabled before the change: the first DTSTART is 2026-10-15, when 1500.00 is in
    # effect, and the new schedule is flagged at once with the same dated message.
    service.pause_autopay(lease.pk, actor=None)
    assert enable(lease).action is AutopayAction.ENABLED
    [early] = service.live_schedules(lease)
    assert (early.amount, early.needs_review) == (D("1500.00"), True)
    assert service.schedule_review_message(lease, early, date(2026, 10, 1)) == expected
    assert creates(fake)[-1].rule == "DTSTART=20261015T120000Z;FREQ=MONTHLY"
    assert enable(lease).message == service.PAUSE_FIRST

    # On or after the change date: pause and enable again; the new rent, no flag.
    service.pause_autopay(lease.pk, actor=None)
    after = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    outcome = service.enable_autopay(lease.pk, BANK, actor=None, now=after)
    assert outcome.action is AutopayAction.ENABLED
    [late] = service.live_schedules(lease)
    assert (late.amount, late.needs_review) == (D("1650.00"), False)
    assert service.schedule_review_message(lease, late, date(2026, 11, 2)) == ""
    assert [schedule.amounts.total for schedule in creates(fake)] == [
        D("1500.00"),
        D("1500.00"),
        D("1650.00"),
    ]


def test_a_change_effective_before_the_first_dtstart_is_enrolled_at_the_new_rent(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    edit(lease, D("1650.00"), date(2026, 10, 10))  # after today, before 2026-10-15
    assert enable(lease).action is AutopayAction.ENABLED
    [row] = service.live_schedules(lease)
    assert (row.amount, row.needs_review) == (D("1650.00"), False)


def test_a_twice_monthly_lease_splits_the_rent_in_effect_on_each_first_debit(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(debit_day=5, second_debit_day=20)
    edit(lease, D("1000.01"), date(2026, 11, 1))
    # NOW is 2026-10-01, so the first debits are 2026-10-05 and 2026-10-20 at 1500.00.
    assert enable(lease).action is AutopayAction.ENABLED
    first, second = service.live_schedules(lease)
    assert (first.amount, second.amount) == (D("750.00"), D("750.00"))
    assert first.needs_review and second.needs_review


def test_no_rent_in_effect_on_the_first_debit_is_refused_without_a_provider_call(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(started_on=date(2027, 1, 1))
    outcome = enable(lease)
    assert (outcome.action, outcome.message) == (
        AutopayAction.REFUSED,
        service.RENT_NOT_STARTED.format(date="Oct 15, 2026"),
    )
    assert fake.calls == []
    assert service.live_schedules(lease) == []


@pytest.mark.parametrize(
    "change",
    [{"debit_day": 16}, {"second_debit_day": 25}, "operator"],
    ids=["day", "second-day", "operator"],
)
def test_a_day_or_operator_change_flags_the_schedule(fake: FakeProvider, change: Any) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    enable(lease)
    if change == "operator":
        change = {"operator": make_operator(email="other@example.com", customer="cus-9")}
    edit(lease, D("1500.00"), **change)
    [row] = service.live_schedules(lease)
    assert row.needs_review


def test_an_unrelated_edit_does_not_flag_the_schedule(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    enable(lease)
    edit(lease, D("1500.00"), started_on=date(2026, 2, 1))
    [row] = service.live_schedules(lease)
    assert not row.needs_review


# --- Payment methods from the snapshot (PLAN-010 Batch A follow-up) -----------------------------


def _method_snapshots() -> list[ProviderSnapshot]:
    return [row for row in ProviderSnapshot.objects.all() if isinstance(row.payload, list)]


def test_payment_method_choices_without_a_snapshot_says_so_and_calls_nothing(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    choices = service.payment_method_choices(make_lease())
    assert (choices.methods, choices.error, choices.fetched_at) == ((), "", None)
    assert fake.calls == []


def test_payment_method_choices_without_a_customer_names_the_blocker(fake: FakeProvider) -> None:
    choices = service.payment_method_choices(make_lease(operator=make_operator(customer=None)))
    assert choices.error == service.NO_PROVIDER_CUSTOMER
    assert fake.calls == []


def test_a_refresh_reads_the_list_once_and_the_page_reads_only_the_snapshot(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    fake.add_payment_method(CUSTOMER, method("pm-card-1", kind="card"))
    lease = make_lease()
    assert lease.operator is not None
    outcome = service.refresh_payment_methods(lease.pk)
    assert (outcome.ok, outcome.message) == (True, service.METHODS_READ.format(count=2))
    assert [name for name, _ in fake.calls] == ["list_payment_methods"]

    fake.calls.clear()
    choices = service.payment_method_choices(lease)
    assert fake.calls == []
    assert [m.external_id for m in choices.methods] == [BANK, "pm-card-1"]
    assert choices.has_bank and choices.error == "" and choices.fetched_at == NOW
    [snapshot] = _method_snapshots()
    assert snapshot.external_ref.external_id == CUSTOMER
    assert all(set(item) == set(service.METHOD_SNAPSHOT_FIELDS) for item in snapshot.payload)


def test_a_second_refresh_replaces_the_list_and_keeps_the_customer_snapshot(
    fake: FakeProvider,
) -> None:
    lease = make_lease()
    assert lease.operator is not None
    ref = ExternalRef.objects.get(external_id=CUSTOMER)
    customer = ProviderSnapshot.objects.create(
        external_ref=ref, payload={"id": CUSTOMER}, fetched_at=NOW
    )
    fake.add_payment_method(CUSTOMER, method())
    service.refresh_payment_methods(lease.pk)
    fake.add_payment_method(CUSTOMER, method("pm-bank-2"))
    service.refresh_payment_methods(lease.pk)
    [snapshot] = _method_snapshots()
    assert [item["external_id"] for item in snapshot.payload] == [BANK, "pm-bank-2"]
    assert ProviderSnapshot.objects.filter(pk=customer.pk).exists()
    # The newer customer snapshot under the same reference is not read as a list.
    ProviderSnapshot.objects.filter(pk=customer.pk).update(fetched_at=timezone.now())
    choices = service.payment_method_choices(lease)
    assert [m.external_id for m in choices.methods] == [BANK, "pm-bank-2"]


def test_a_failed_refresh_keeps_the_previous_list_and_names_fields_only(
    fake: FakeProvider,
) -> None:
    lease = make_lease()
    assert lease.operator is not None
    fake.add_payment_method(CUSTOMER, method())
    service.refresh_payment_methods(lease.pk)
    fake.fail_next(
        "list_payment_methods", ProviderRejected(404, "CANARY", error_fields=("customer",))
    )
    outcome = service.refresh_payment_methods(lease.pk)
    assert (outcome.ok, outcome.fields, outcome.failure) == (
        False,
        ("customer",),
        "ProviderRejected",
    )
    assert outcome.message == service.PROVIDER_REJECTED and "CANARY" not in outcome.message
    [snapshot] = _method_snapshots()
    assert [item["external_id"] for item in snapshot.payload] == [BANK]


def test_a_refresh_without_a_customer_calls_nothing_and_writes_nothing(
    fake: FakeProvider,
) -> None:
    lease = make_lease(operator=make_operator(customer=None))
    outcome = service.refresh_payment_methods(lease.pk)
    assert (outcome.ok, outcome.message) == (False, service.NO_PROVIDER_CUSTOMER)
    assert service.refresh_payment_methods(uuid.uuid4()).ok is False
    assert fake.calls == [] and not ProviderSnapshot.objects.exists()


def test_a_method_holding_a_card_number_is_not_stored_and_the_old_list_stays(
    fake: FakeProvider,
) -> None:
    lease = make_lease()
    assert lease.operator is not None
    fake.add_payment_method(CUSTOMER, method())
    service.refresh_payment_methods(lease.pk)
    dirty = PaymentMethodRecord(
        external_id="pm-dirty", raw_status="", fetched_at=NOW, nickname="4111111111111111"
    )
    fake.add_payment_method(CUSTOMER, dirty)
    with pytest.raises(ValidationError):
        service.refresh_payment_methods(lease.pk)
    [snapshot] = _method_snapshots()
    assert [item["external_id"] for item in snapshot.payload] == [BANK]
    assert "4111111111111111" not in str(ProviderSnapshot.objects.values_list("payload"))


def test_malformed_snapshot_items_are_skipped_and_unknown_keys_dropped(
    fake: FakeProvider,
) -> None:
    lease = make_lease()
    ref = ExternalRef.objects.get(external_id=CUSTOMER)
    ProviderSnapshot.objects.create(
        external_ref=ref,
        payload=[
            "text",
            {"method": "bank"},
            {"external_id": 7},
            {"external_id": BANK, "method": "bank", "is_default": "yes", "routing": "x"},
        ],
        fetched_at=NOW,
    )
    [only] = service.payment_method_choices(lease).methods
    assert (only.external_id, only.method, only.is_default) == (BANK, "bank", False)
    assert not hasattr(only, "routing")


def test_the_service_module_uses_no_float() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(service))
    assert not any(
        (isinstance(node, ast.Constant) and isinstance(node.value, float))
        or (isinstance(node, ast.Name) and node.id == "float")
        for node in ast.walk(tree)
    )


# --- Concurrency (PostgreSQL row locks) ----------------------------------------------------------


def _race(*targets: Any) -> list[Any]:
    """Run each target in its own thread, released together; their results in order."""
    barrier = threading.Barrier(len(targets))
    results: list[Any] = [None] * len(targets)

    def run(index: int, target: Any) -> None:
        try:
            barrier.wait()
            results[index] = target()
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=pair) for pair in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_enable_clicks_create_one_schedule_per_debit_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowFake(FakeProvider):
        def create_scheduled_invoice(self, schedule: ScheduleInput) -> ScheduleRecord:
            threading.Event().wait(0.3)
            return super().create_scheduled_invoice(schedule)

    provider = SlowFake()
    provider.add_payment_method(CUSTOMER, method())
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    lease = make_lease(debit_day=5, second_debit_day=20)

    outcomes = _race(lambda: enable(lease), lambda: enable(lease))

    assert sorted(outcome.action for outcome in outcomes) == sorted(
        [AutopayAction.ENABLED, AutopayAction.ALREADY]
    )
    assert len(creates(provider)) == 2
    assert CollectionSchedule.objects.filter(lease=lease).count() == 2
    assert ExternalRef.objects.filter(external_kind="schedule").count() == 2


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_creates_with_one_key_make_one_lease() -> None:
    fields = lease_fields()
    key = uuid.uuid4()

    def create() -> bool:
        return service.create_lease(fields, rent=RENT, creation_key=key, actor=None)[1]

    assert sorted(_race(create, create)) == [False, True]
    assert Lease.objects.count() == 1
    assert LeaseRentHistory.objects.count() == 1


# --- PLAN-007c: large-debit confirmation and the confirmed provider limit -----------------------

LARGE_SETTINGS = {"LARGE_DEBIT_WARNING_AMOUNT": D("2000.00"), "PROVIDER_MAX_TXN_AMOUNT": None}
CONFIRM_2100 = (
    "I confirm this debit of $2,100.00 exceeds the warning threshold of $2,000.00 "
    "and may be enabled."
)


def enable_confirmed(
    lease: Lease, *, confirmed: bool, actor: User | None = None
) -> service.AutopayOutcome:
    return service.enable_autopay(lease.pk, BANK, actor=actor, confirmed=confirmed, now=NOW)


def _audit_before_create(fake: FakeProvider) -> list[list[str]]:
    """Wrap ``create_scheduled_invoice``; record the audit actions present at each call."""
    original = fake.create_scheduled_invoice
    seen: list[list[str]] = []

    def create(schedule: ScheduleInput) -> ScheduleRecord:
        seen.append(list(AuditLog.objects.order_by("id").values_list("action", flat=True)))
        return original(schedule)

    fake.create_scheduled_invoice = create  # type: ignore[method-assign]
    return seen


@override_settings(**LARGE_SETTINGS)
def test_a_debit_below_the_warning_needs_no_confirmation(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(D("2000.00"))
    outcome = enable_confirmed(lease, confirmed=False)
    assert outcome.action is AutopayAction.ENABLED
    assert len(creates(fake)) == 1
    assert "lease.autopay_large_debit_confirmed" not in actions(lease)


@override_settings(**LARGE_SETTINGS)
def test_a_large_debit_without_confirmation_is_refused_with_no_call(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(D("2100.00"))
    outcome = enable_confirmed(lease, confirmed=False)
    assert outcome.action is AutopayAction.REFUSED
    assert outcome.text == CONFIRM_2100
    assert fake.calls == []
    assert CollectionSchedule.objects.filter(lease=lease).count() == 0
    assert "lease.autopay_large_debit_confirmed" not in actions(lease)


@override_settings(**LARGE_SETTINGS)
def test_a_confirmed_large_debit_is_audited_before_the_create(
    fake: FakeProvider, actor: User
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(D("2100.00"))
    seen = _audit_before_create(fake)
    outcome = enable_confirmed(lease, confirmed=True, actor=actor)
    assert outcome.action is AutopayAction.ENABLED
    assert [schedule.amounts.total for schedule in creates(fake)] == [D("2100.00")]
    [at_create] = seen
    assert at_create[-1] == "lease.autopay_large_debit_confirmed"
    entry = AuditLog.objects.get(action="lease.autopay_large_debit_confirmed")
    assert entry.actor == actor
    assert entry.entity_id == str(lease.pk)
    assert entry.after == {"amount": "2100.00", "threshold": "2000.00", "confirmed_by": actor.pk}
    assert actions(lease)[-2:] == ["lease.autopay_large_debit_confirmed", "lease.autopay_enabled"]


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=D("5000.00"))
@pytest.mark.parametrize("confirmed", [False, True])
def test_a_debit_above_the_confirmed_limit_is_refused_even_when_confirmed(
    fake: FakeProvider, confirmed: bool
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(D("5000.01"))
    outcome = enable_confirmed(lease, confirmed=confirmed)
    assert outcome.action is AutopayAction.REFUSED
    assert outcome.text == ("Exceeds the Stax debit limit of $5,000.00. Autopay cannot be enabled.")
    assert fake.calls == []
    assert "lease.autopay_large_debit_confirmed" not in actions(lease)


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=D("5000.00"))
def test_between_the_warning_and_the_limit_the_confirmation_path_works(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(D("4999.99"))
    refused = enable_confirmed(lease, confirmed=False)
    assert refused.action is AutopayAction.REFUSED
    assert refused.text == (
        "I confirm this debit of $4,999.99 exceeds the warning threshold of $2,000.00 "
        "and may be enabled."
    )
    assert fake.calls == []
    assert enable_confirmed(lease, confirmed=True).action is AutopayAction.ENABLED
    assert len(creates(fake)) == 1


@override_settings(**LARGE_SETTINGS)
def test_an_empty_limit_never_hard_blocks(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(D("9999999.99"))
    assert enable_confirmed(lease, confirmed=True).action is AutopayAction.ENABLED
    assert service.provider_limit_refusal([D("9999999.99")]) == ""
    assert not service.exceeds_provider_limit(D("9999999.99"))


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=D("3000.00"))
def test_twice_monthly_compares_the_per_debit_amount(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    # 3800.00 is above the limit as a whole; each half (1900.00) is below the warning.
    lease = make_lease(D("3800.00"), debit_day=5, second_debit_day=20)
    outcome = enable_confirmed(lease, confirmed=False)
    assert outcome.action is AutopayAction.ENABLED
    assert [s.amounts.total for s in creates(fake)] == [D("1900.00"), D("1900.00")]
    assert "lease.autopay_large_debit_confirmed" not in actions(lease)


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=D("3000.00"))
def test_twice_monthly_halves_above_the_warning_need_confirmation_for_the_larger_half(
    fake: FakeProvider, actor: User
) -> None:
    fake.add_payment_method(CUSTOMER, method())
    # 4200.01 splits into 2100.01 and 2100.00; both above the warning, below the limit.
    lease = make_lease(D("4200.01"), debit_day=5, second_debit_day=20)
    refused = enable_confirmed(lease, confirmed=False)
    assert refused.text == (
        "I confirm this debit of $2,100.01 exceeds the warning threshold of $2,000.00 "
        "and may be enabled."
    )
    assert fake.calls == []
    assert enable_confirmed(lease, confirmed=True, actor=actor).action is AutopayAction.ENABLED
    entry = AuditLog.objects.get(action="lease.autopay_large_debit_confirmed")
    assert entry.after == {"amount": "2100.01", "threshold": "2000.00", "confirmed_by": actor.pk}


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=D("3000.00"))
def test_twice_monthly_with_a_half_above_the_limit_is_refused(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(D("6000.02"), debit_day=5, second_debit_day=20)
    outcome = enable_confirmed(lease, confirmed=True)
    assert outcome.text == ("Exceeds the Stax debit limit of $3,000.00. Autopay cannot be enabled.")
    assert fake.calls == []


@override_settings(**LARGE_SETTINGS)
def test_a_confirmation_with_a_refused_method_writes_no_confirmation_row(
    fake: FakeProvider,
) -> None:
    fake.add_payment_method(CUSTOMER, method(kind="card"))
    lease = make_lease(D("2100.00"))
    outcome = enable_confirmed(lease, confirmed=True)
    assert outcome.message == service.CARD_REFUSED
    assert creates(fake) == []
    assert "lease.autopay_large_debit_confirmed" not in actions(lease)


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (D("0.01"), "$0.01"),
        (D("2000.00"), "$2,000.00"),
        (D("1234567.89"), "$1,234,567.89"),
        (D("5"), "$5.00"),
    ],
)
def test_amounts_in_messages_are_formatted_with_separators(amount: Decimal, text: str) -> None:
    assert format_amount(amount) == text


# --- Autopay moved past a split month (PLAN-012b-b D13) ------------------------------------------

#: An installment plan's id as the move audits it (the move only records it).
PLAN = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


def linked_lease(fake: FakeProvider, rent: Decimal = RENT, **overrides: Any) -> Lease:
    """A lease with rent autopay enabled on :data:`NOW` (the 15th's first debit is 15
    October) and the lease's bank method linked, as the move needs it."""
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(rent, **overrides)
    assert enable(lease).ok
    ExternalRef.objects.create(
        entity_type="lease",
        entity_id=lease.pk,
        provider="stax_pay",
        external_kind="payment_method",
        external_id=BANK,
    )
    return lease


def move(lease: Lease, actor: User | None = None) -> service.AutopayOutcome:
    return service.move_autopay(
        lease.pk, seq=1, skipped=date(2026, 10, 15), plan_id=PLAN, actor=actor, now=NOW
    )


def test_the_move_deletes_the_schedule_and_creates_it_from_next_months_debit(
    fake: FakeProvider, actor: User
) -> None:
    lease = linked_lease(fake)
    [old] = service.live_schedules(lease)
    old_ref = service.schedule_ref(old)
    outcome = move(lease, actor)
    assert (outcome.action, outcome.message) == (
        AutopayAction.MOVED,
        "Autopay moved: it resumes Nov 15, 2026; this month is collected in installments.",
    )
    assert [args for name, args in fake.calls if name == "delete_scheduled_invoice"] == [(old_ref,)]
    first, second = creates(fake)
    assert first.rule == "DTSTART=20261015T120000Z;FREQ=MONTHLY"
    assert second.rule == "DTSTART=20261115T120000Z;FREQ=MONTHLY"
    assert (second.amounts.total, second.payment_method_external_id) == (D("1500.00"), BANK)
    old.refresh_from_db()
    assert old.state == ScheduleState.ENDED
    [new] = service.live_schedules(lease)
    assert (new.seq, new.debit_day, new.amount, new.state) == (
        1,
        15,
        D("1500.00"),
        ScheduleState.ACTIVE,
    )
    assert service.schedule_ref(new) not in ("", old_ref)
    assert actions(lease)[-3:] == [
        service.DELETED_FOR_MOVE_ACTION,
        "lease.autopay_enabled",
        service.MOVED_ACTION,
    ]
    entry = AuditLog.objects.get(action=service.MOVED_ACTION)
    assert entry.actor == actor
    assert entry.after is not None
    assert (entry.after["plan"], entry.after["skipped"], entry.after["starts"]) == (
        str(PLAN),
        "2026-10-15",
        "2026-11-15T12:00:00+00:00",
    )
    assert service.schedule_ref(new) not in str(entry.after) and old_ref not in str(
        AuditLog.objects.get(action=service.DELETED_FOR_MOVE_ACTION).after
    )


def test_the_move_run_again_changes_nothing(fake: FakeProvider) -> None:
    lease = linked_lease(fake)
    assert move(lease).action is AutopayAction.MOVED
    provider_calls, audited = len(fake.calls), AuditLog.objects.count()
    outcome = move(lease)
    assert (outcome.action, outcome.message) == (AutopayAction.ALREADY, service.NOTHING_TO_MOVE)
    assert outcome.ok
    assert (len(fake.calls), AuditLog.objects.count()) == (provider_calls, audited)


def test_a_schedule_already_gone_at_the_provider_counts_as_deleted(fake: FakeProvider) -> None:
    lease = linked_lease(fake)
    fake.fail_next("delete_scheduled_invoice", ProviderRejected(404))
    assert move(lease).action is AutopayAction.MOVED
    assert len(creates(fake)) == 2


def test_a_failed_delete_leaves_the_schedule_as_it_was(fake: FakeProvider) -> None:
    lease = linked_lease(fake)
    [old] = service.live_schedules(lease)
    fake.fail_next("delete_scheduled_invoice", ProviderUnavailable(503, "unavailable"))
    outcome = move(lease)
    assert (outcome.action, outcome.failure) == (AutopayAction.FAILED, "ProviderUnavailable")
    old.refresh_from_db()
    assert old.state == ScheduleState.ACTIVE and service.schedule_ref(old)
    assert len(creates(fake)) == 1
    assert "lease.autopay_move_failed" in actions(lease)
    # The repeat deletes and creates.
    assert move(lease).action is AutopayAction.MOVED


def test_the_move_is_refused_without_a_linked_bank_method(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease()
    enable(lease)
    calls = len(fake.calls)
    outcome = move(lease)
    assert (outcome.action, outcome.message) == (AutopayAction.REFUSED, service.NO_BANK_METHOD)
    assert len(fake.calls) == calls
    assert [row.state for row in service.live_schedules(lease)] == [ScheduleState.ACTIVE]


def test_a_lease_without_active_autopay_has_nothing_to_move(fake: FakeProvider) -> None:
    lease = linked_lease(fake)
    assert service.pause_autopay(lease.pk, actor=None).ok
    calls = len(fake.calls)
    assert move(lease).action is AutopayAction.ALREADY
    assert len(fake.calls) == calls


def test_the_debit_instant_is_noon_utc_of_the_day() -> None:
    assert service.debit_instant(date(2026, 10, 15)) == datetime(2026, 10, 15, 12, tzinfo=UTC)
    assert service.next_debit_at(15, service.debit_instant(date(2026, 10, 15))) == datetime(
        2026, 11, 15, 12, tzinfo=UTC
    )


# --- PLAN-014 C4: a new lease's billing details and bank-account choice -------------------------

BILLING_IN = {
    "company_name": "Northwind Two LLC",
    "email": "two@example.com",
    "phone": "4155550132",
    "address_line1": "1 Main St",
    "address_line2": "",
    "city": "Austin",
    "state": "TX",
    "postal_code": "78701",
}


def account_holder(fake: FakeProvider, *, kind: str = "bank", raw_status: str = "") -> Lease:
    """A lease of a new operator holding its own customer, a linked method and the method
    list read (the latest snapshot lists the method)."""
    fake.add_payment_method(CUSTOMER, method(kind=kind, raw_status=raw_status))
    lease = make_lease()
    ExternalRef.objects.create(
        entity_type="lease",
        entity_id=lease.pk,
        provider="stax_pay",
        external_kind="payment_method",
        external_id=BANK,
    )
    assert service.refresh_payment_methods(lease.pk).ok
    return lease


def operator_of(lease: Lease) -> list[uuid.UUID]:
    assert lease.operator_id is not None
    return [lease.operator_id]


def new_lease(operator: Operator | None, billing: dict[str, Any] | None, account: str) -> Lease:
    lease, created = service.create_lease(
        lease_fields(
            operator=operator,
            store=Store.objects.create(external_label="Store 0303", status=StoreStatus.ACTIVE),
        ),
        rent=RENT,
        creation_key=uuid.uuid4(),
        actor=None,
        today=date(2025, 12, 31),
        billing=billing,
        account=account,
    )
    assert created
    return lease


def test_a_new_lease_without_billing_details_keeps_no_billing_row() -> None:
    lease = new_lease(make_operator(customer=None), None, service.ACCOUNT_LATER)
    assert not LeaseBilling.objects.filter(lease=lease).exists()
    assert lease.sync_status == "not_applicable"


def test_later_with_an_email_saves_the_details_as_typed_and_queues_one_sync() -> None:
    from payments.models import Job, JobKind

    lease = new_lease(make_operator(customer=None), BILLING_IN, service.ACCOUNT_LATER)
    billing = LeaseBilling.objects.get(lease=lease)
    assert {name: getattr(billing, name) for name in BILLING_IN} == BILLING_IN
    assert billing.shares_account_of is None and billing.bank_last4 == ""
    lease.refresh_from_db()
    assert lease.sync_status == "pending"
    [job] = Job.objects.filter(entity_id=lease.pk)
    assert job.kind == JobKind.LEASE_SYNC
    [audit] = AuditLog.objects.filter(action="lease_billing.changed", entity_id=str(lease.pk))
    assert audit.before is None and audit.after["email"] == "two@example.com"
    assert ExternalRef.objects.filter(entity_id=lease.pk).count() == 0


def test_later_without_an_email_waits_for_a_link_and_queues_nothing() -> None:
    from payments.models import Job

    lease = new_lease(make_operator(customer=None), {"company_name": ""}, service.ACCOUNT_LATER)
    lease.refresh_from_db()
    assert lease.sync_status == "awaiting_link"
    assert LeaseBilling.objects.get(lease=lease).email == ""
    assert not Job.objects.filter(entity_id=lease.pk).exists()


def test_enter_is_later_for_the_service_the_page_continues_to_the_bank_entry() -> None:
    from payments.models import Job

    lease = new_lease(make_operator(customer=None), BILLING_IN, service.ACCOUNT_ENTER)
    assert Job.objects.filter(entity_id=lease.pk).count() == 1


def test_the_same_account_shares_a_usable_lease_of_the_operator(fake: FakeProvider) -> None:
    from payments.models import Job

    holder = account_holder(fake)
    [found] = service.shareable_accounts(operator_of(holder))
    assert (found.lease, found.last4, found.value) == (holder, "6789", f"same:{holder.pk}")
    lease = new_lease(holder.operator, BILLING_IN, found.value)
    billing = LeaseBilling.objects.get(lease=lease)
    assert billing.shares_account_of == holder and billing.email == BILLING_IN["email"]
    lease.refresh_from_db()
    assert lease.sync_status == "not_applicable"
    assert not Job.objects.filter(entity_id=lease.pk).exists()
    assert service.customer_ref(lease) == CUSTOMER
    assert service.linked_payment_method(lease) == BANK
    assert service.shareable_accounts(operator_of(holder)) == [found]  # a sharer is not listed


@pytest.mark.parametrize("why", ["card", "deleted", "other_operator", "sharer", "no_method"])
def test_a_same_account_that_cannot_be_shared_is_refused_and_nothing_is_saved(
    fake: FakeProvider, why: str
) -> None:
    holder = account_holder(
        fake,
        kind="card" if why == "card" else "bank",
        raw_status=PAYMENT_METHOD_DELETED if why == "deleted" else "",
    )
    operator = holder.operator
    target = holder
    if why == "other_operator":
        operator = make_operator(email="other@example.com", customer=None)
    elif why == "sharer":
        target = new_lease(holder.operator, BILLING_IN, f"same:{holder.pk}")
    elif why == "no_method":
        ExternalRef.objects.filter(external_kind="payment_method").update(superseded_at=NOW)
    leases_before = Lease.objects.count()
    if why in ("card", "deleted", "no_method"):
        assert service.shareable_accounts(operator_of(holder)) == []
    with pytest.raises(LeaseRefused) as refused:
        new_lease(operator, BILLING_IN, f"same:{target.pk}")
    assert (refused.value.field, str(refused.value)) == ("account", service.ACCOUNT_NOT_SHAREABLE)
    assert Lease.objects.count() == leases_before
    assert LeaseBilling.objects.filter(email=BILLING_IN["email"]).count() == (
        1 if why == "sharer" else 0
    )


@pytest.mark.parametrize("account", ["", "same:", "same:not-a-uuid", "sometime", "SAME:x"])
def test_an_unknown_account_choice_is_refused(account: str) -> None:
    with pytest.raises(LeaseRefused) as refused:
        new_lease(make_operator(customer=None), BILLING_IN, account)
    assert str(refused.value) == service.ACCOUNT_INVALID
    assert not Lease.objects.filter(store__external_label="Store 0303").exists()


def test_a_same_account_of_an_unknown_lease_is_refused() -> None:
    with pytest.raises(LeaseRefused) as refused:
        new_lease(make_operator(customer=None), BILLING_IN, f"same:{uuid.uuid4()}")
    assert str(refused.value) == service.ACCOUNT_NOT_SHAREABLE
