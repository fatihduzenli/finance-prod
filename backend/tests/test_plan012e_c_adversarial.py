"""Adversarial cases for PLAN-012e Batch C (installment plans), by the QA owner (plan §6).
A plan binds to its charge's own operator and lease whatever the form posts; two confirms
at the same moment leave one plan; hostile line values write nothing; random plans,
payments and cancels never double count in open balances, the P&L or the ledger; an
installment payment counts in the month it settled like every other payment; a lease
cannot end while a plan is live on one of its charges; the cancel note never carries
payment data into a row; and nobody below billing reaches a split or a plan action."""

from __future__ import annotations

import random
import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, connections, transaction
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    LIVE_PLAN_STATUSES,
    AuditLog,
    InstallmentCadence,
    InstallmentPlan,
    InstallmentPlanStatus,
    Lease,
    LeaseEndReason,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentChannel,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
)
from ledger.services import (
    installments,
    lease_end,
    one_off_charges,
    payments_ledger,
    pnl,
    receivables,
    rent_run,
)
from ledger.services.installments import Line
from tests.receivables_helpers import a_lease, a_one_off_charge, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
ZERO = D("0.00")
TODAY = date(2026, 10, 6)
FIRST = date(2026, 10, 20)
OCTOBER = date(2026, 10, 1)
CARD = "4111111111111111"
requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="row locks and partial indexes: PostgreSQL only"
)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    return TODAY


Parent = RentCharge | OneOffCharge


def _wire_lease(operator: Operator | None = None) -> Lease:
    """A lease paid by wire of an operator with nothing at the payment provider: its
    charges are collected by hand, so a split makes nothing at the provider."""
    return a_lease(a_store(), operator or an_operator(), channel=PaymentChannel.WIRE)


def _manual_charge(lease: Lease, price: str = "900.00", due: date = OCTOBER) -> OneOffCharge:
    assert lease.operator is not None
    return a_one_off_charge(lease.operator, price, due, lease=lease)


def _plan(
    parent: Parent, count: int = 3, cadence: str = InstallmentCadence.MONTHLY
) -> InstallmentPlan:
    lines, refused = installments.preview(parent, count, FIRST, cadence, today=TODAY)
    assert refused is None, refused
    made = installments.create_plan(parent, count, FIRST, cadence, lines, None, today=TODAY)
    assert made.ok and made.plan is not None, made.message
    return made.plan


def _pay(charge: Parent, amount: str, on: date = TODAY) -> None:
    if isinstance(charge, RentCharge):
        outcome = rent_run.record_manual_payment(
            charge.pk,
            method=PaymentMethod.ZELLE,
            amount=D(amount),
            paid_on=on,
            note="Paid by Zelle",
            actor=None,
            today=on,
        )
    else:
        outcome = one_off_charges.record_payment(
            charge.pk,
            method=PaymentMethod.ZELLE,
            amount=D(amount),
            paid_on=on,
            note="Paid by Zelle",
            actor=None,
            today=on,
        )
    assert outcome.ok, outcome.message


def _lines_post(lines: list[Line], count: int, **extra: str) -> dict[str, str]:
    data = {"count": str(count), "first_due": FIRST.isoformat(), "cadence": "monthly"}
    for number, line in enumerate(lines, start=1):
        data[f"due_{number}"] = line.due.isoformat()
        data[f"amount_{number}"] = str(line.amount)
    return {**data, **extra}


def _nothing_written() -> bool:
    return (
        not InstallmentPlan.objects.exists()
        and not OneOffCharge.objects.filter(category=OneOffCategory.INSTALLMENT).exists()
    )


# --- The plan binds to the charge, not to the form --------------------------------------------


def test_the_split_binds_the_plan_to_the_charges_own_operator_and_lease_whatever_is_posted() -> (
    None
):
    mine, theirs = _wire_lease(), _wire_lease()
    parent = _manual_charge(mine)
    other = _manual_charge(theirs, "600.00")
    lines, _ = installments.preview(parent, 3, FIRST, "monthly", today=TODAY)
    billing, _ = client_for(Role.BILLING)
    forged = _lines_post(
        lines,
        3,
        operator=str(theirs.operator_id),
        lease=str(theirs.pk),
        one_off_charge=str(other.pk),
        rent_charge=str(other.pk),
        collection="autopay_due",
        status="completed",
        installment_no="9",
    )
    response = billing.post(f"/one-off-charges/{parent.pk}/split/", forged)
    assert response.status_code == 302, response.content[:200]
    plan = InstallmentPlan.objects.get()
    assert plan.one_off_charge_id == parent.pk and plan.rent_charge_id is None
    assert plan.operator_id == mine.operator_id and plan.lease_id == mine.pk
    assert plan.collection == "manual" and plan.status == InstallmentPlanStatus.ACTIVE
    rows = installments.installments_of(plan)
    assert [row.installment_no for row in rows] == [1, 2, 3]
    assert all(row.operator_id == mine.operator_id and row.lease_id == mine.pk for row in rows)
    assert sum((row.price for row in rows), ZERO) == D("900.00")
    assert not InstallmentPlan.objects.filter(one_off_charge=other).exists()
    other.refresh_from_db()
    assert other.balance == D("600.00")


def test_lines_that_add_up_to_another_charges_balance_are_refused() -> None:
    parent = _manual_charge(_wire_lease(), "900.00")
    other = _manual_charge(_wire_lease(), "600.00")
    lines, _ = installments.preview(other, 3, FIRST, "monthly", today=TODAY)
    billing, _ = client_for(Role.BILLING)
    response = billing.post(f"/one-off-charges/{parent.pk}/split/", _lines_post(lines, 3))
    assert response.status_code == 302 and "/split/" in response["Location"]
    assert _nothing_written()


@pytest.mark.parametrize(
    "amounts",
    [
        ("NaN", "300.00", "600.00"),
        ("-300.00", "600.00", "600.00"),
        ("Infinity", "0.00", "0.00"),
        ("300.001", "299.999", "300.00"),
        ("1E+400", "300.00", "300.00"),
        ("300.00", "300.00", "299.99"),  # a sum short of the balance
        ("'=1+1", "300.00", "600.00"),
    ],
)
def test_hostile_line_values_are_refused_and_write_nothing(amounts: tuple[str, ...]) -> None:
    parent = _manual_charge(_wire_lease(), "900.00")
    lines, _ = installments.preview(parent, 3, FIRST, "monthly", today=TODAY)
    data = _lines_post(lines, 3)
    for number, amount in enumerate(amounts, start=1):
        data[f"amount_{number}"] = amount
    billing, _ = client_for(Role.BILLING)
    response = billing.post(f"/one-off-charges/{parent.pk}/split/", data)
    assert response.status_code == 302 and "/split/" in response["Location"], amounts
    assert _nothing_written()
    # The preview page comes back without a server error and echoes no raw value.
    page = billing.get(response["Location"])
    assert page.status_code == 200
    assert "'=1+1" not in page.content.decode()


def test_a_date_in_the_past_or_out_of_order_cannot_be_smuggled_in_as_a_line() -> None:
    parent = _manual_charge(_wire_lease(), "900.00")
    lines, _ = installments.preview(parent, 3, FIRST, "monthly", today=TODAY)
    billing, _ = client_for(Role.BILLING)
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    for edits in (
        {"due_1": yesterday},
        {"due_2": lines[0].due.isoformat()},
        {"due_3": "2026-13-01"},
        {"due_1": "2026-10-20T00:00:00"},
    ):
        response = billing.post(
            f"/one-off-charges/{parent.pk}/split/", {**_lines_post(lines, 3), **edits}
        )
        assert response.status_code == 302, edits
        assert _nothing_written(), edits


# --- Two confirms at the same moment ----------------------------------------------------------


def test_a_second_confirm_finds_the_first_plan_and_the_index_refuses_a_forced_second() -> None:
    parent = _manual_charge(_wire_lease())
    lines, _ = installments.preview(parent, 3, FIRST, "monthly", today=TODAY)
    first = installments.create_plan(parent, 3, FIRST, "monthly", lines, None, today=TODAY)
    assert first.ok
    second = installments.create_plan(parent, 3, FIRST, "monthly", lines, None, today=TODAY)
    assert not second.ok and second.message == installments.HAS_PLAN
    assert InstallmentPlan.objects.count() == 1
    assert OneOffCharge.objects.filter(category=OneOffCategory.INSTALLMENT).count() == 3
    assert parent.operator is not None
    with pytest.raises(IntegrityError), transaction.atomic():
        InstallmentPlan.objects.create(
            one_off_charge=parent,
            operator=parent.operator,
            lease=parent.lease,
            count=2,
            first_due=FIRST,
            cadence="monthly",
            collection="manual",
            status=InstallmentPlanStatus.ATTENTION,
            confirmed_at=timezone.now(),
        )


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_confirms_at_the_same_moment_leave_one_plan() -> None:
    parent = _manual_charge(_wire_lease())
    lines, _ = installments.preview(parent, 4, FIRST, "weekly", today=TODAY)
    barrier = threading.Barrier(2)
    outcomes: list[installments.Outcome] = []
    errors: list[BaseException] = []

    def confirm() -> None:
        try:
            barrier.wait(timeout=10)
            outcomes.append(
                installments.create_plan(parent, 4, FIRST, "weekly", lines, None, today=TODAY)
            )
        except BaseException as error:
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert sorted(outcome.ok for outcome in outcomes) == [False, True]
    assert InstallmentPlan.objects.exclude(status=InstallmentPlanStatus.CANCELLED).count() == 1
    assert OneOffCharge.objects.filter(category=OneOffCategory.INSTALLMENT).count() == 4
    assert sum(
        (row.price for row in OneOffCharge.objects.filter(category=OneOffCategory.INSTALLMENT)),
        ZERO,
    ) == D("900.00")


# --- Totals never double count under random plans ----------------------------------------------


class _World:
    """Random parents (rent of a wire lease, one-off by hand), splits, payments on
    installments and on parents that are not on a live plan, and cancels; the oracle is
    the sum of the parents' prices and of the settled payments."""

    def __init__(self, seed: int) -> None:
        self.random = random.Random(seed)  # noqa: S311 - test data, not security
        self.lease = _wire_lease()
        self.parents: list[Parent] = []
        self.plans: list[InstallmentPlan] = []
        self.paid = ZERO
        self.billed = ZERO
        self.admin = client_for(Role.ADMIN)[1]
        self.months = [date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]

    def cents(self, low: int, high: int) -> Decimal:
        return (D(self.random.randint(low, high)) / 100).quantize(D("0.01"))

    def new_parent(self) -> None:
        price = self.cents(200, 400_000)
        used = {p.period_month for p in self.parents if isinstance(p, RentCharge)}
        free = [m for m in self.months if m not in used]
        if free and self.random.random() < 0.5:
            parent: Parent = a_rent_charge(self.lease, self.random.choice(free), due=str(price))
        else:
            parent = _manual_charge(self.lease, str(price), self.random.choice(self.months))
        self.parents.append(parent)
        self.billed += price

    def _fresh(self, parent: Parent) -> Parent:
        return type(parent).objects.get(pk=parent.pk)

    def _on_live_plan(self, parent: Parent) -> bool:
        return parent.installment_plans.filter(status__in=LIVE_PLAN_STATUSES).exists()

    def split(self) -> None:
        free = [p for p in self.parents if not self._on_live_plan(p) and self._fresh(p).balance > 0]
        if not free:
            return
        parent = self._fresh(self.random.choice(free))
        count = self.random.randint(2, 12)
        cadence = self.random.choice(list(InstallmentCadence.values))
        first = TODAY + timedelta(days=self.random.randint(0, 40))
        lines, refused = installments.preview(parent, count, first, cadence, today=TODAY)
        if refused is not None:
            assert refused.message == installments.CENT_TOO_SMALL, refused
            return
        made = installments.create_plan(parent, count, first, cadence, lines, None, today=TODAY)
        assert made.ok and made.plan is not None, made.message
        self.plans.append(made.plan)

    def pay_installment(self) -> None:
        rows = list(
            OneOffCharge.objects.filter(
                category=OneOffCategory.INSTALLMENT,
                plan__status__in=LIVE_PLAN_STATUSES,
                status__in=[OneOffChargeStatus.OWED, OneOffChargeStatus.PARTIAL],
            )
        )
        if not rows:
            return
        row = self.random.choice(rows)
        balance = Decimal(row.balance)
        amount = balance if self.random.random() < 0.5 else self.cents(1, int(balance * 100))
        _pay(row, str(amount))
        self.paid += amount

    def pay_parent(self) -> None:
        free = [self._fresh(p) for p in self.parents if not self._on_live_plan(p)]
        free = [p for p in free if p.balance > 0]
        if not free:
            return
        parent = self.random.choice(free)
        balance = Decimal(parent.balance)
        amount = balance if self.random.random() < 0.5 else self.cents(1, int(balance * 100))
        _pay(parent, str(amount))
        self.paid += amount

    def cancel(self) -> None:
        live = [
            plan
            for plan in InstallmentPlan.objects.filter(status__in=LIVE_PLAN_STATUSES)
            if not OneOffCharge.objects.filter(
                plan=plan, status=OneOffChargeStatus.PARTIAL
            ).exists()
        ]
        if not live:
            return
        done = installments.cancel(
            self.random.choice(live), "Stopped at the operator's request.", self.admin
        )
        assert done.ok, done.message

    def run(self, steps: int) -> None:
        for _ in range(3):
            self.new_parent()
        for _ in range(steps):
            action = self.random.choice(
                [
                    self.new_parent,
                    self.split,
                    self.split,
                    self.pay_installment,
                    self.pay_installment,
                    self.pay_parent,
                    self.cancel,
                ]
            )
            action()


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_plans_payments_and_cancels_never_double_count(seed: int) -> None:
    world = _World(seed)
    world.run(28)
    operator = world.lease.operator_id
    # Open balances: every cent billed and not paid is owed exactly once, on a parent or
    # on an installment, never on both.
    owed = sum((row.balance for row in receivables.open_balances(TODAY, operator)), ZERO)
    assert owed == world.billed - world.paid
    # Payments: every settled payment is a row of the ledger, once.
    settled = Payment.objects.filter(status=PaymentStatus.SETTLED, is_refund=False)
    assert sum((p.amount for p in settled), ZERO) == world.paid
    totals = payments_ledger.totals(
        payments_ledger.ledger_payments(payments_ledger.PaymentFilter(operator=operator))
    )
    assert totals.settled == world.paid
    # The P&L: billed once (never an installment), collected once (under the parent).
    query = pnl.parse_query({"from": "2026-09", "to": "2027-03"}, today=TODAY)
    rows = [row for row in pnl.rows(query) if row.operator_id == operator]
    assert sum((row.rent_billed + row.fees_billed for row in rows), ZERO) == world.billed
    assert sum((row.rent_collected + row.fees_collected for row in rows), ZERO) == world.paid
    # Installments are never billed lines, and a paid parent of a completed plan has no
    # payment row of its own.
    for plan in InstallmentPlan.objects.filter(status=InstallmentPlanStatus.COMPLETED):
        parent = plan.parent
        parent.refresh_from_db()
        assert parent.balance == ZERO
        field = "rent_charge" if isinstance(parent, RentCharge) else "one_off_charge"
        assert not Payment.objects.filter(**{field: parent}).exists()


# --- The month of an installment payment ------------------------------------------------------


def test_an_installment_payment_counts_in_the_month_it_settled_like_any_other_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A November payment on an October rent charge is November's cash whether or not
    the charge was split: the P&L of a closed month never changes later."""
    split_lease, whole_lease = _wire_lease(), _wire_lease()
    split_parent = a_rent_charge(split_lease, OCTOBER, due="1000.00")
    whole_parent = a_rent_charge(whole_lease, OCTOBER, due="1000.00")
    plan = _plan(split_parent, 2)
    november = date(2026, 11, 10)
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: november)
    _pay(installments.installments_of(plan)[0], "500.00", november)
    _pay(whole_parent, "500.00", november)
    query = pnl.parse_query({"from": "2026-10", "to": "2026-11"}, today=november)
    rows = pnl.rows(query)

    def collected(lease: Lease) -> dict[date, Decimal]:
        return {row.month: row.rent_collected for row in rows if row.store_id == lease.store_id}

    assert collected(whole_lease) == {OCTOBER: ZERO, date(2026, 11, 1): D("500.00")}
    assert collected(split_lease) == collected(whole_lease)


# --- A lease cannot end while a plan is live --------------------------------------------------


def test_the_lease_cannot_end_while_an_installment_plan_is_live_on_one_of_its_charges() -> None:
    lease = _wire_lease()
    rent = a_rent_charge(lease, OCTOBER, due="1000.00")
    one_off = _manual_charge(lease, "300.00")
    rent_plan, one_off_plan = _plan(rent, 2), _plan(one_off, 3)
    found = lease_end.blockers(lease, TODAY)
    urls = {blocker.url for blocker in found}
    assert f"/plans/{rent_plan.pk}/" in urls and f"/plans/{one_off_plan.pk}/" in urls
    assert all(blocker.kind == lease_end.INSTALLMENT_PLAN for blocker in found), found
    # The confirm re-checks: it refuses and writes nothing.
    admin = client_for(Role.ADMIN)[1]
    draft = lease_end.start(lease, admin)
    terms = lease_end.set_terms(
        draft, TODAY, LeaseEndReason.MOVED_OUT, True, actor=admin, today=TODAY
    )
    assert terms.ok, terms.message
    audits = AuditLog.objects.count()
    outcome = lease_end.confirm(draft, admin, confirmed=True, today=TODAY)
    assert not outcome.ok
    lease.refresh_from_db()
    assert lease.ended_on is None and AuditLog.objects.count() == audits
    # Cancelling the plans clears the blockers.
    for plan in (rent_plan, one_off_plan):
        assert installments.cancel(plan, "Ending the lease.", admin).ok
    assert lease_end.blockers(lease, TODAY) == []


# --- The cancel note and the role gates -------------------------------------------------------


def test_a_cancel_note_with_payment_data_is_refused_and_reaches_no_row() -> None:
    plan = _plan(_manual_charge(_wire_lease()))
    admin, _ = client_for(Role.ADMIN)
    before = AuditLog.objects.count()
    response = admin.post(f"/plans/{plan.pk}/cancel/", {"note": f"Operator paid by card {CARD}."})
    assert response.status_code == 302
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.ACTIVE and plan.note == ""
    assert AuditLog.objects.count() == before
    assert not AuditLog.objects.filter(after__icontains=CARD[-4:]).exists()
    assert OneOffCharge.objects.filter(plan=plan, status=OneOffChargeStatus.OWED).count() == 3


def test_nobody_below_billing_reaches_a_split_or_a_plan_action_and_viewers_see_no_button() -> None:
    lease = _wire_lease()
    rent = a_rent_charge(lease, OCTOBER, due="1000.00")
    one_off = _manual_charge(lease)
    plan = _plan(one_off)
    viewer, _ = client_for(Role.VIEWER)
    for path in (f"/charges/{rent.pk}/", f"/one-off-charges/{one_off.pk}/", f"/plans/{plan.pk}/"):
        page = viewer.get(path)
        assert page.status_code == 200, path
        body = page.content.decode()
        assert "/split/" not in body and "/reschedule/" not in body and "/cancel/" not in body
    from django.test import Client

    anonymous = Client()
    for path in (
        f"/charges/{rent.pk}/split/",
        f"/one-off-charges/{one_off.pk}/split/",
        f"/plans/{plan.pk}/",
        f"/plans/{plan.pk}/reschedule/",
        f"/plans/{plan.pk}/cancel/",
    ):
        assert anonymous.get(path).status_code == 401, path
        assert anonymous.post(path, {"count": "2", "note": "x"}).status_code == 401, path
    assert viewer.post(f"/charges/{rent.pk}/split/", {"count": "2"}).status_code == 403
    billing, _ = client_for(Role.BILLING)
    assert billing.post(f"/plans/{plan.pk}/cancel/", {"note": "Stop."}).status_code == 403
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.ACTIVE
    assert not InstallmentPlan.objects.filter(rent_charge=rent).exists()
