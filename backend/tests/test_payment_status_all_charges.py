"""The dashboard's Payment status card over every charge (docs/qa/PLAN-012f-b-staging-review.md
§10 D20, slice A5).

The card counts the one-off charges (fees) due in the range beside the rent charges: a paid
fee in Paid by its amount paid, an open fee in Overdue or Upcoming by its balance, a fee with
a pending payment (or partly paid and not yet due) in the pending count, a waived or voided
fee nowhere. An installment, an autopay-plan occurrence and a re-debit count through their
parent or source, never on their own. The range is by due date, both ends included; the
card reads one query more than before.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from ledger.models import (
    AutopayPlan,
    CollectionMethod,
    InstallmentPlan,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
)
from ledger.services import dashboard as blocks
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    an_operator,
)

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 6)
OCT = date(2026, 10, 1)
SEP = date(2026, 9, 1)
NOV = date(2026, 11, 1)
THIS_MONTH = blocks.Range("this", "This month", OCT, OCT)
LAST_WEEK = date(2026, 9, 29)
NEXT_WEEK = date(2026, 10, 13)


def card(selected: blocks.Range = THIS_MONTH) -> blocks.PaymentStatusCard:
    return blocks.payment_status(selected, TODAY)


def boxes(result: blocks.PaymentStatusCard) -> dict[str, tuple[int, Decimal]]:
    return {part.key: (part.count, part.amount) for part in result.parts}


def a_fee(price: str, due: date, **fields: Any) -> OneOffCharge:
    return a_one_off_charge(an_operator(), price, due, **fields)


def a_one_off_payment(
    charge: OneOffCharge, amount: str, status: str = PaymentStatus.PENDING
) -> Payment:
    return Payment.objects.create(
        one_off_charge=charge,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D(amount),
        status=status,
        initiated_on=TODAY,
        settled_on=TODAY if status == PaymentStatus.SETTLED else None,
    )


EMPTY = {"overdue": (0, D("0")), "paid": (0, D("0")), "upcoming": (0, D("0"))}


# --- The families ----------------------------------------------------------------------------


def test_a_fee_due_before_today_unpaid_is_overdue_by_its_price() -> None:
    a_fee("150.00", date(2026, 10, 2))
    result = card()
    assert boxes(result)["overdue"] == (1, D("150.00"))
    assert result.pending == 0 and result.total == D("150.00")


def test_a_fee_due_last_week_is_overdue_in_the_range_that_holds_its_due_date() -> None:
    a_fee("150.00", LAST_WEEK)
    assert boxes(card()) == EMPTY  # due in September: not in October's card
    wider = card(blocks.Range("3m", "Last 3 months", date(2026, 8, 1), OCT))
    assert boxes(wider)["overdue"] == (1, D("150.00"))


def test_a_paid_fee_is_paid_by_its_amount_paid() -> None:
    fee = a_fee("200.00", date(2026, 10, 3), status=OneOffChargeStatus.PAID, paid="200.00")
    a_one_off_payment(fee, "200.00", PaymentStatus.SETTLED)
    result = card()
    assert boxes(result)["paid"] == (1, D("200.00"))
    assert boxes(result)["overdue"] == (0, D("0"))


def test_a_paid_fee_due_next_week_is_paid_not_upcoming() -> None:
    a_fee("80.00", NEXT_WEEK, status=OneOffChargeStatus.PAID, paid="80.00")
    assert boxes(card())["paid"] == (1, D("80.00"))
    assert boxes(card())["upcoming"] == (0, D("0"))


def test_a_fee_due_next_week_is_upcoming_by_its_price() -> None:
    a_fee("300.00", NEXT_WEEK)
    result = card()
    assert boxes(result)["upcoming"] == (1, D("300.00"))
    assert result.pending == 0


def test_a_fee_due_today_is_upcoming() -> None:
    a_fee("75.00", TODAY)
    assert boxes(card())["upcoming"] == (1, D("75.00"))


def test_a_partly_paid_fee_is_overdue_by_its_balance_when_past_due_else_pending() -> None:
    a_fee("500.00", date(2026, 10, 2), status=OneOffChargeStatus.PARTIAL, paid="120.00")
    a_fee("500.00", NEXT_WEEK, status=OneOffChargeStatus.PARTIAL, paid="100.00")
    result = card()
    assert boxes(result)["overdue"] == (1, D("380.00"))
    assert boxes(result)["upcoming"] == (0, D("0"))
    assert result.pending == 1


@pytest.mark.parametrize("due", [date(2026, 10, 2), NEXT_WEEK])
def test_a_fee_with_a_pending_payment_is_counted_pending_whatever_its_due_date(due: date) -> None:
    fee = a_fee("250.00", due)
    a_one_off_payment(fee, "250.00")
    result = card()
    assert boxes(result) == EMPTY
    assert result.pending == 1


def test_a_partly_paid_fee_past_due_with_a_pending_payment_is_pending() -> None:
    fee = a_fee("400.00", date(2026, 10, 1), status=OneOffChargeStatus.PARTIAL, paid="100.00")
    a_one_off_payment(fee, "300.00")
    result = card()
    assert boxes(result) == EMPTY and result.pending == 1


def test_a_settled_or_returned_payment_does_not_make_a_fee_pending() -> None:
    fee = a_fee("250.00", date(2026, 10, 2))
    a_one_off_payment(fee, "250.00", PaymentStatus.RETURNED)
    result = card()
    assert boxes(result)["overdue"] == (1, D("250.00"))
    assert result.pending == 0


def test_the_pending_payment_of_another_fee_leaves_this_one_alone() -> None:
    other = a_fee("90.00", SEP)  # outside the range
    a_one_off_payment(other, "90.00")
    a_fee("250.00", date(2026, 10, 2))
    result = card()
    assert boxes(result)["overdue"] == (1, D("250.00"))
    assert result.pending == 0


@pytest.mark.parametrize("status", [OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED])
@pytest.mark.parametrize("due", [date(2026, 10, 2), NEXT_WEEK])
def test_a_waived_or_voided_fee_is_nowhere(status: str, due: date) -> None:
    fields: dict[str, Any] = {"status": status}
    if status == OneOffChargeStatus.WAIVED:
        fields.update(waived_at=timezone.now(), waive_note="Goodwill")
    else:
        fields.update(voided_at=timezone.now(), void_note="Entered twice")
    a_fee("120.00", due, **fields)
    result = card()
    assert boxes(result) == EMPTY and result.pending == 0


def test_an_nsf_fee_counts_as_a_fee_and_its_redebit_does_not() -> None:
    operator = an_operator()
    lease = a_lease(operator=operator)
    rent = a_rent_charge(lease, SEP, RentChargeStatus.UNPAID, "1000.00", day=5)
    returned = a_payment(rent, "1000.00", None, PaymentStatus.RETURNED, initiated_on=SEP)
    redebit = a_one_off_charge(
        operator,
        "1000.00",
        date(2026, 10, 2),
        lease=lease,
        category=OneOffCategory.REDEBIT,
        collection=CollectionMethod.AUTOPAY_DUE,
        description="Re-debit of Rent Sep 2026",
        source_payment=returned,
    )
    a_one_off_charge(
        operator,
        "35.00",
        date(2026, 10, 2),
        lease=lease,
        category=OneOffCategory.NSF_FEE,
        collection=CollectionMethod.AUTOPAY_DUE,
        description="Returned payment (NSF) fee",
        companion_of=redebit,
    )
    result = card()
    assert boxes(result)["overdue"] == (1, D("35.00"))  # the fee alone; the rent is September's


# --- Counted through their parent or source ---------------------------------------------------


def test_a_rent_charge_bank_debit_counts_nothing_beside_its_source() -> None:
    operator = an_operator()
    lease = a_lease(operator=operator)
    rent = a_rent_charge(lease, OCT, RentChargeStatus.UNPAID, "1500.00", day=2)
    a_one_off_charge(
        operator,
        "1500.00",
        date(2026, 10, 3),
        lease=lease,
        category=OneOffCategory.REDEBIT,
        collection=CollectionMethod.AUTOPAY_DUE,
        description="Bank debit of Rent Oct 2026",
        source_rent_charge=rent,
    )
    result = card()
    assert boxes(result)["overdue"] == (1, D("1500.00"))  # the rent charge, once
    assert boxes(result)["upcoming"] == (0, D("0")) and result.pending == 0


def test_a_paid_redebit_of_a_returned_payment_counts_nothing_beside_its_source() -> None:
    operator = an_operator()
    lease = a_lease(operator=operator)
    rent = a_rent_charge(lease, OCT, RentChargeStatus.PAID, "1200.00", "1200.00", day=1)
    returned = a_payment(rent, "1200.00", None, PaymentStatus.RETURNED, initiated_on=OCT)
    a_one_off_charge(
        operator,
        "1200.00",
        date(2026, 10, 4),
        lease=lease,
        category=OneOffCategory.REDEBIT,
        collection=CollectionMethod.AUTOPAY_DUE,
        description="Re-debit of Rent Oct 2026",
        source_payment=returned,
        status=OneOffChargeStatus.PAID,
        amount_paid=D("1200.00"),
    )
    result = card()
    assert boxes(result)["paid"] == (1, D("1200.00"))


def test_installments_due_in_range_count_nothing_beside_their_parent_fee() -> None:
    operator = an_operator()
    parent = a_one_off_charge(operator, "600.00", date(2026, 10, 1))
    plan = InstallmentPlan.objects.create(
        one_off_charge=parent,
        operator=operator,
        count=2,
        first_due=date(2026, 10, 2),
        cadence="monthly",
        collection="manual",
        confirmed_at=timezone.now(),
    )
    for number, due in ((1, date(2026, 10, 2)), (2, NEXT_WEEK)):
        a_one_off_charge(
            operator,
            "300.00",
            due,
            category=OneOffCategory.INSTALLMENT,
            description=f"Installment {number} of 2",
            plan=plan,
            installment_no=number,
        )
    result = card()
    assert boxes(result) == {
        "overdue": (1, D("600.00")),  # the parent, once
        "paid": (0, D("0")),
        "upcoming": (0, D("0")),
    }
    assert result.pending == 0


def test_installments_of_a_rent_charge_count_nothing_beside_it() -> None:
    operator = an_operator()
    lease = a_lease(operator=operator)
    rent = a_rent_charge(lease, OCT, RentChargeStatus.UNPAID, "1000.00", day=1)
    plan = InstallmentPlan.objects.create(
        rent_charge=rent,
        operator=operator,
        lease=lease,
        count=2,
        first_due=date(2026, 10, 2),
        cadence="monthly",
        collection="manual",
        confirmed_at=timezone.now(),
    )
    a_one_off_charge(
        operator,
        "500.00",
        date(2026, 10, 2),
        lease=lease,
        category=OneOffCategory.INSTALLMENT,
        description="Installment 1 of 2",
        plan=plan,
        installment_no=1,
        status=OneOffChargeStatus.PAID,
        amount_paid=D("500.00"),
    )
    result = card()
    assert boxes(result)["overdue"] == (1, D("1000.00"))
    assert boxes(result)["paid"] == (0, D("0"))


def test_an_autopay_plan_occurrence_counts_nothing() -> None:
    operator = an_operator()
    lease = a_lease(operator=operator)
    plan = AutopayPlan.objects.create(
        lease=lease,
        operator=operator,
        store=lease.store,
        item_name="Sign fee",
        description="",
        quantity=D("1"),
        unit_amount=D("100.00"),
        amount=D("100.00"),
        cadence="monthly",
        first_on=date(2026, 10, 1),
        end_rule="never",
        next_on=NOV,
    )
    for period, due, status, paid in (
        (1, date(2026, 10, 1), OneOffChargeStatus.PAID, "100.00"),
        (2, date(2026, 10, 20), OneOffChargeStatus.OWED, "0.00"),
    ):
        a_one_off_charge(
            operator,
            "100.00",
            due,
            lease=lease,
            category=OneOffCategory.AUTOPAY_PLAN,
            collection=CollectionMethod.AUTOPAY_DUE,
            description="Sign fee",
            autopay_plan=plan,
            period_no=period,
            status=status,
            paid=paid,
        )
    result = card()
    assert boxes(result) == EMPTY and result.pending == 0


# --- The range edges -------------------------------------------------------------------------


def test_the_range_holds_its_first_and_last_day_and_nothing_beyond() -> None:
    a_fee("10.00", date(2026, 9, 30))  # the day before
    a_fee("20.00", date(2026, 10, 1))  # the first day: overdue
    a_fee("30.00", date(2026, 10, 31))  # the last day: upcoming
    a_fee("40.00", date(2026, 11, 1))  # the day after
    result = card()
    assert boxes(result)["overdue"] == (1, D("20.00"))
    assert boxes(result)["upcoming"] == (1, D("30.00"))
    custom = card(blocks.Range("custom", "Sep 2026 to Nov 2026", SEP, NOV))
    assert boxes(custom)["overdue"] == (2, D("30.00"))
    assert boxes(custom)["upcoming"] == (2, D("70.00"))


# --- The whole card --------------------------------------------------------------------------


def test_the_three_boxes_equal_the_hand_computed_figures_of_rent_and_fees() -> None:
    """Rent: overdue 1000, paid 1500, upcoming 1200, one pending, one waived. Fees: overdue
    150 and 380 (500 less 120), paid 200, upcoming 300, one pending payment, one partly paid
    not yet due, one waived, one voided; an installment and a bank debit beside their parent
    and source."""
    a_rent_charge(a_lease(), OCT, RentChargeStatus.UNPAID, "1000.00", day=1)
    a_rent_charge(a_lease(), OCT, RentChargeStatus.PAID, "1500.00", "1500.00", day=3)
    a_rent_charge(a_lease(), OCT, RentChargeStatus.DUE, "1200.00", day=20)
    a_rent_charge(a_lease(), OCT, RentChargeStatus.PENDING, "900.00", day=5)
    a_rent_charge(a_lease(), OCT, RentChargeStatus.WAIVED, "800.00", day=8)
    a_fee("150.00", date(2026, 10, 2))
    a_fee("500.00", date(2026, 10, 3), status=OneOffChargeStatus.PARTIAL, paid="120.00")
    a_fee("200.00", date(2026, 10, 4), status=OneOffChargeStatus.PAID, paid="200.00")
    a_fee("300.00", NEXT_WEEK)
    a_one_off_payment(a_fee("250.00", date(2026, 10, 5)), "250.00")
    a_fee("400.00", NEXT_WEEK, status=OneOffChargeStatus.PARTIAL, paid="50.00")
    a_fee(
        "60.00",
        date(2026, 10, 2),
        status=OneOffChargeStatus.WAIVED,
        waived_at=timezone.now(),
        waive_note="Goodwill",
    )
    a_fee(
        "70.00",
        NEXT_WEEK,
        status=OneOffChargeStatus.VOIDED,
        voided_at=timezone.now(),
        void_note="Entered twice",
    )
    operator = an_operator()
    lease = a_lease(operator=operator)
    rent = a_rent_charge(lease, SEP, RentChargeStatus.UNPAID, "999.00", day=5)
    a_one_off_charge(
        operator,
        "999.00",
        date(2026, 10, 2),
        lease=lease,
        category=OneOffCategory.REDEBIT,
        collection=CollectionMethod.AUTOPAY_DUE,
        description="Bank debit of Rent Sep 2026",
        source_rent_charge=rent,
    )
    result = card()
    assert boxes(result) == {
        "overdue": (3, D("1530.00")),
        "paid": (2, D("1700.00")),
        "upcoming": (2, D("1500.00")),
    }
    assert result.pending == 3
    assert result.total == D("4730.00")


def test_the_card_reads_two_queries_whatever_the_charges(
    django_assert_num_queries: Any,
) -> None:
    for day in (2, 3, 4):
        fee = a_fee("100.00", date(2026, 10, day))
        a_one_off_payment(fee, "100.00")
        a_rent_charge(a_lease(), OCT, RentChargeStatus.UNPAID, "1000.00", day=day)
    with django_assert_num_queries(2):
        result = card()
    assert result.pending == 3
