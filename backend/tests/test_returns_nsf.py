"""Returned bank debits (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md F1 to F5,
§5 F): the plain-words reasons, the business days, the NSF fee at the return, the automatic
re-debit of R01/R09 on the third business day and Nacha's limit, the eve-of-retry schedule, Stop
automatic retry (staff, a logged contact, not on day 3), Debit again after a stop, the staff void,
the attention kinds, the returns queue's retry column, the send window for F, and ledger
migration 0063 both ways. Returns arrive through real webhook deliveries to a
:class:`~payments.fake.FakeProvider`; nothing reaches a network.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

import payments
from accounts.roles import Role
from ledger.models import (
    NSF_FEE_FIRST,
    AuditLog,
    CollectionMethod,
    ContactKind,
    FeeType,
    Lease,
    MessageRule,
    MessageStatus,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    OutboundMessage,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RetryState,
    ReviewReason,
)
from ledger.services import attention, business_days, contacts, nsf, refunds
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind
from payments.provider import ScheduleInput
from payments.stax import return_codes
from tests.job_helpers import run_queued_jobs
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import (
    TXN,
    World,
    deliver,
    make_world,
    payment_for,
    provider_txn,
)

pytestmark = pytest.mark.django_db

D = Decimal
#: Friday Nov 6, 2026, 10:00 in New York: the notice of the return.
NOTICE = date(2026, 11, 6)
NOTICE_AT = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
#: The third business day after it: Mon 9, Tue 10, (Wed 11 Veterans Day), Thu 12.
RETRY = date(2026, 11, 12)
BANK = "pm-bank-returns-1"
NO_RETRY_SENTENCE = "Please contact us within 2 business days to set up a new payment method."
RETRY_SENTENCE = "will be debited again automatically on"


class Clock:
    """The patched ``timezone.now``; tests move it."""

    def __init__(self) -> None:
        self.now = NOTICE_AT

    def at(self, day: date, hour_utc: int = 15) -> None:
        self.now = datetime(day.year, day.month, day.day, hour_utc, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    found = Clock()
    monkeypatch.setattr("django.utils.timezone.now", lambda: found.now)
    return found


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


# --- Builders ------------------------------------------------------------------------------


def a_world(fake: FakeProvider, *, bank: bool = True) -> World:
    """The webhook world (a rent charge of $1,500.00 on an invoice), its lease's account
    holding a linked bank method unless ``bank`` is false."""
    world = make_world(fake)
    if bank:
        ExternalRef.objects.create(
            entity_type="lease",
            entity_id=world.lease.pk,
            provider="stax_pay",
            external_kind="payment_method",
            external_id=BANK,
        )
    return world


def returned(fake: FakeProvider, code: str = "R01", external_id: str = TXN) -> Payment:
    """Deliver the debit, then its return with ``code``; the returned payment."""
    provider_txn(fake, external_id)
    deliver("create_transaction", id=external_id)
    fake.transactions[external_id] = replace(
        fake.transactions[external_id],
        returned=True,
        return_code=code,
        return_reason="Bank text",
    )
    assert deliver("update_transaction_settled", id=external_id) == "processed"
    payment = payment_for(external_id)
    assert payment.status == PaymentStatus.RETURNED
    return payment


def fees_of(payment: Payment) -> list[OneOffCharge]:
    return list(
        OneOffCharge.objects.filter(source_payment=payment, category=OneOffCategory.NSF_FEE)
    )


def redebit_of(payment: Payment) -> OneOffCharge:
    return OneOffCharge.objects.get(source_payment=payment, category=OneOffCategory.REDEBIT)


def f_messages() -> list[OutboundMessage]:
    return list(OutboundMessage.objects.filter(rule=MessageRule.F).order_by("queued_at", "id"))


def a_user(role: Role) -> Any:
    return client_for(role)[1]


def returned_on_redebit(redebit: OneOffCharge, code: str = "R01") -> Payment:
    """The redebit's own debit, returned (as the webhook would leave it)."""
    return Payment.objects.create(
        one_off_charge=redebit,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=redebit.price,
        status=PaymentStatus.RETURNED,
        initiated_on=redebit.due_date,
        returned_at=timezone.now(),
        return_code=code,
    )


def logged(world: World, kind: str = ContactKind.CALL, *, actor: Any = None) -> str:
    data = contacts.ContactInput(
        kind=kind,
        happened_on=timezone.localdate(),
        body="Spoke to the owner about the returned debit.",
        lease=world.lease.pk,
        promised_date=timezone.localdate() + timedelta(days=5)
        if kind == ContactKind.PROMISE
        else None,
        promised_amount=D("1500.00") if kind == ContactKind.PROMISE else None,
    )
    outcome = contacts.add_entry(
        world.operator.pk, data, actor=actor if actor is not None else a_user(Role.BILLING)
    )
    assert outcome.ok, outcome.message
    return outcome.message


# --- F1: plain words --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "words"),
    [
        ("R01", "Insufficient funds"),
        ("R02", "The bank account is closed"),
        ("R03", "The account number could not be found"),
        ("R04", "The account number could not be found"),
        ("R08", "You asked your bank to stop this payment"),
        ("R09", "Uncollected funds"),
        ("R10", "Your bank reports this debit was not authorized"),
        ("R16", "The account is frozen"),
        ("R29", "Your bank reports this debit was not authorized"),
        (" r01 ", "Insufficient funds"),
        ("R05", return_codes.FALLBACK),
        ("", return_codes.FALLBACK),
        (None, return_codes.FALLBACK),
        ("R01<script>", return_codes.FALLBACK),
    ],
)
def test_each_code_reads_in_plain_words(code: str | None, words: str) -> None:
    assert payments.return_reason_words(code) == words
    assert return_codes.plain_words(code) == words


def test_only_r01_and_r09_are_retried() -> None:
    assert return_codes.RETRYABLE == {"R01", "R09"}
    assert payments.retryable_return("R01") and payments.retryable_return("r09")
    for code in ("R02", "R10", "R29", "", None, "R1"):
        assert not payments.retryable_return(code)
    assert return_codes.FALLBACK == (
        "Your bank returned the payment; please contact your bank for details"
    )


# --- Business days ----------------------------------------------------------------------------


def test_business_days_skip_weekends_and_federal_reserve_holidays() -> None:
    # Friday Oct 30 → Mon Nov 2, Tue Nov 3, Wed Nov 4: over a weekend.
    assert business_days.add(date(2026, 10, 30), 3) == date(2026, 11, 4)
    # Over Veterans Day (Wednesday Nov 11, 2026).
    assert business_days.add(NOTICE, 3) == RETRY
    assert business_days.add(NOTICE, 2) == date(2026, 11, 10)
    # Over Thanksgiving (Thursday Nov 26) and the weekend after.
    assert business_days.add(date(2026, 11, 25), 1) == date(2026, 11, 27)
    # Independence Day 2027 on a Sunday is kept on Monday July 5.
    assert business_days.add(date(2027, 7, 2), 1) == date(2027, 7, 6)
    assert business_days.add(NOTICE, 0) == NOTICE
    assert not business_days.is_business_day(date(2026, 11, 11))
    assert not business_days.is_business_day(date(2026, 11, 7))
    assert business_days.is_business_day(date(2026, 11, 12))
    # A Saturday holiday is not moved to the Friday before (the banks are open).
    assert business_days.is_business_day(date(2026, 7, 3))
    with pytest.raises(ValueError):
        business_days.add(NOTICE, -1)


def test_every_listed_holiday_is_a_weekday_of_2026_to_2028() -> None:
    days = business_days.FEDERAL_RESERVE_HOLIDAYS
    assert all(day.weekday() < 5 and 2026 <= day.year <= 2028 for day in days)
    assert len([day for day in days if day.year == 2026]) == 10


# --- F2: R01 → the fee and the automatic re-debit -----------------------------------------------


def test_an_r01_return_adds_the_fee_and_an_automatic_re_debit_on_the_third_business_day(
    fake: FakeProvider,
) -> None:
    world = a_world(fake)
    payment = returned(fake, "R01")
    [fee] = fees_of(payment)
    assert (fee.price, fee.status, fee.collection) == (
        D("100.00"),
        OneOffChargeStatus.OWED,
        CollectionMethod.MANUAL,
    )
    assert fee.companion_of_id is None and fee.lease_id == world.lease.pk
    assert fee.due_date == NOTICE
    world.operator.refresh_from_db()
    assert world.operator.nsf_return_count == 1
    redebit = redebit_of(payment)
    assert (redebit.price, redebit.retry_date, redebit.due_date, redebit.retry_state) == (
        D("1500.00"),
        RETRY,
        RETRY,
        RetryState.AUTO_PENDING,
    )
    assert redebit.collection == CollectionMethod.AUTOPAY_DUE
    # Nothing reaches the processor before the eve: no schedule job, no provider call.
    assert not Job.objects.filter(kind=JobKind.SCHEDULE_CREATE).exists()
    assert not [call for call in fake.calls if call[0] == "create_scheduled_invoice"]
    # The return stays open in the queue; the lease is not flagged.
    payment.refresh_from_db()
    assert payment.resolved_at is None
    world.lease.refresh_from_db()
    assert not world.lease.needs_review
    [message] = f_messages()
    assert message.template.code == nsf.TEMPLATE_RETRY
    assert (message.charge_entity_id, message.occurrence) == (world.charge.pk, 1)
    body = message.rendered_body
    assert "Reason: Insufficient funds" in body
    assert "made on Oct 15, 2026" in body
    assert "the payment of $1,500.00 will be debited again automatically on Nov 12, 2026" in body
    assert "for Rent October 2026" in body
    processed = AuditLog.objects.get(action=nsf.RETURN_ACTION, entity_id=str(payment.pk))
    assert processed.after is not None
    assert processed.after["retry"] == nsf.RETRY_AUTO
    assert processed.after["retry_date"] == RETRY.isoformat()


def test_an_r09_return_is_retried_too(fake: FakeProvider) -> None:
    a_world(fake)
    payment = returned(fake, "R09")
    assert redebit_of(payment).retry_state == RetryState.AUTO_PENDING
    [message] = f_messages()
    assert "Reason: Uncollected funds" in message.rendered_body


def test_a_replayed_return_adds_no_second_fee_and_no_second_message(fake: FakeProvider) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    assert deliver("update_transaction_settled", id=TXN) == "processed"
    assert deliver("update_transaction", id=TXN) == "processed"
    assert nsf.on_return(payment, NOTICE) is None
    assert len(fees_of(payment)) == 1
    assert OneOffCharge.objects.filter(category=OneOffCategory.REDEBIT).count() == 1
    assert len(f_messages()) == 1
    operator = Operator.objects.get()
    assert operator.nsf_return_count == 1
    # The database refuses a second fee for the payment whatever writes it.
    fee = fees_of(payment)[0]
    with pytest.raises(IntegrityError), transaction.atomic():
        OneOffCharge.objects.create(
            operator=fee.operator,
            category=OneOffCategory.NSF_FEE,
            collection=CollectionMethod.MANUAL,
            description="Second fee",
            price=D("100.00"),
            due_date=NOTICE,
            source_payment=payment,
        )


def test_a_refund_return_and_a_card_payment_get_nothing(fake: FakeProvider) -> None:
    world = a_world(fake)
    refund = Payment.objects.create(
        rent_charge=world.charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D("100.00"),
        status=PaymentStatus.RETURNED,
        is_refund=True,
        parent_payment=Payment.objects.create(
            rent_charge=world.charge,
            kind=PaymentKind.RENT,
            method=PaymentMethod.ACH,
            amount=D("1500.00"),
            status=PaymentStatus.SETTLED,
            settled_on=NOTICE,
        ),
        returned_at=NOTICE_AT,
        return_code="R01",
    )
    assert nsf.on_return(refund, NOTICE) is None
    zelle = Payment.objects.create(
        rent_charge=world.charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ZELLE,
        amount=D("100.00"),
        note="Zelle reference 77.",
        status=PaymentStatus.RETURNED,
        returned_at=NOTICE_AT,
        return_code="R01",
    )
    assert nsf.on_return(zelle, NOTICE) is None
    assert not OneOffCharge.objects.exists() and not f_messages()


def test_an_inactive_fee_type_makes_no_fee_but_the_retry_and_message_still_go(
    fake: FakeProvider,
) -> None:
    FeeType.objects.filter(code=NSF_FEE_FIRST).update(active=False)
    a_world(fake)
    payment = returned(fake, "R01")
    assert fees_of(payment) == []
    assert Operator.objects.get().nsf_return_count == 0
    assert redebit_of(payment).retry_state == RetryState.AUTO_PENDING
    assert len(f_messages()) == 1
    # Processed once: a replay does not make the fee later.
    FeeType.objects.filter(code=NSF_FEE_FIRST).update(active=True)
    assert nsf.on_return(payment, NOTICE) is None
    assert fees_of(payment) == []


# --- F2: codes that are not retried ------------------------------------------------------------


def test_an_r10_return_adds_the_fee_no_retry_needs_review_and_the_no_retry_message(
    fake: FakeProvider,
) -> None:
    world = a_world(fake)
    payment = returned(fake, "R10")
    assert len(fees_of(payment)) == 1
    assert not OneOffCharge.objects.filter(category=OneOffCategory.REDEBIT).exists()
    world.lease.refresh_from_db()
    assert world.lease.needs_review
    assert world.lease.review_reason == ReviewReason.RETURNED_DEBIT
    flagged = AuditLog.objects.get(action=nsf.FLAGGED_ACTION, entity_id=str(world.lease.pk))
    assert flagged.after is not None
    assert flagged.after["reason"] == "Returned debit (R10): new payment method needed"
    [message] = f_messages()
    assert message.template.code == nsf.TEMPLATE_NO_RETRY
    assert NO_RETRY_SENTENCE in message.rendered_body
    assert RETRY_SENTENCE not in message.rendered_body
    assert "Reason: Your bank reports this debit was not authorized" in message.rendered_body


def test_a_return_without_a_code_reads_the_general_sentence_and_is_not_retried(
    fake: FakeProvider,
) -> None:
    world = a_world(fake)
    payment = returned(fake, "")
    assert not OneOffCharge.objects.filter(category=OneOffCategory.REDEBIT).exists()
    [message] = f_messages()
    assert f"Reason: {return_codes.FALLBACK}" in message.rendered_body
    assert NO_RETRY_SENTENCE in message.rendered_body
    flagged = AuditLog.objects.get(action=nsf.FLAGGED_ACTION, entity_id=str(world.lease.pk))
    assert flagged.after is not None
    assert flagged.after["reason"] == "Returned debit (no code): new payment method needed"
    [fee] = fees_of(payment)
    assert fee.note == f"Returned debit (no code): {return_codes.FALLBACK}."


def test_a_code_of_another_shape_is_never_copied_into_stored_text(fake: FakeProvider) -> None:
    world = a_world(fake)
    payment = returned(fake, "R01 call 555")
    [fee] = fees_of(payment)
    assert fee.note == f"Returned debit (unlisted code): {return_codes.FALLBACK}."
    flagged = AuditLog.objects.get(action=nsf.FLAGGED_ACTION, entity_id=str(world.lease.pk))
    assert flagged.after is not None
    assert flagged.after["reason"] == "Returned debit (unlisted code): new payment method needed"
    assert nsf.queue_states([payment])[payment.pk].text == "No retry: unlisted code"


def test_an_r01_without_a_linked_bank_method_is_not_retried(fake: FakeProvider) -> None:
    world = a_world(fake, bank=False)
    payment = returned(fake, "R01")
    assert not OneOffCharge.objects.filter(category=OneOffCategory.REDEBIT).exists()
    world.lease.refresh_from_db()
    assert world.lease.review_reason == ReviewReason.RETURNED_DEBIT
    [message] = f_messages()
    assert message.template.code == nsf.TEMPLATE_NO_RETRY
    states = nsf.queue_states([payment])
    assert states[payment.pk].text == nsf.STATE_ACCOUNT


def test_a_review_reason_already_set_is_kept(fake: FakeProvider) -> None:
    world = a_world(fake)
    Lease.objects.filter(pk=world.lease.pk).update(review_reason=ReviewReason.OWN_CUSTOMER)
    returned(fake, "R02")
    world.lease.refresh_from_db()
    assert world.lease.needs_review and world.lease.review_reason == ReviewReason.OWN_CUSTOMER


# --- F2: Nacha's limit -----------------------------------------------------------------------


def test_two_re_debits_within_180_days_then_no_third(fake: FakeProvider, clock: Clock) -> None:
    world = a_world(fake)
    original = returned(fake, "R01")
    first = redebit_of(original)
    # The first re-debit's own debit is returned R01: a second re-debit, the limit counted on
    # the original; the first is done and the original's return resolved.
    clock.at(date(2026, 11, 13))
    second_return = returned_on_redebit(first)
    outcome = nsf.on_return(second_return, date(2026, 11, 13))
    assert outcome is not None and outcome.retry == nsf.RETRY_AUTO
    first.refresh_from_db()
    original.refresh_from_db()
    assert first.retry_state == RetryState.DONE
    assert original.resolved_at is not None and original.redebit_date == RETRY
    second = redebit_of(second_return)
    assert second.price == D("1500.00") and second.retry_date == date(2026, 11, 18)
    # Its debit is returned R01 too: no third re-debit, Needs review, the no-retry message.
    clock.at(date(2026, 11, 19))
    third_return = returned_on_redebit(second)
    outcome = nsf.on_return(third_return, date(2026, 11, 19))
    assert outcome is not None and outcome.retry == nsf.NO_RETRY_LIMIT
    assert not OneOffCharge.objects.filter(
        source_payment=third_return, category=OneOffCategory.REDEBIT
    ).exists()
    world.lease.refresh_from_db()
    assert world.lease.needs_review and world.lease.review_reason == ReviewReason.REDEBIT_LIMIT
    flagged = AuditLog.objects.filter(action=nsf.FLAGGED_ACTION).latest("at")
    assert flagged.after is not None and flagged.after["reason"] == "Re-debit limit reached (Nacha)"
    # Three returns, three fees, three messages about the rent charge (occurrences 1 to 3).
    assert OneOffCharge.objects.filter(category=OneOffCategory.NSF_FEE).count() == 3
    messages = f_messages()
    assert [message.occurrence for message in messages] == [1, 2, 3]
    assert {message.charge_entity_id for message in messages} == {world.charge.pk}
    assert messages[-1].template.code == nsf.TEMPLATE_NO_RETRY
    assert nsf.queue_states([third_return])[third_return.pk].text == nsf.STATE_LIMIT


def test_a_re_debit_older_than_180_days_does_not_count(fake: FakeProvider, clock: Clock) -> None:
    a_world(fake)
    original = returned(fake, "R01")
    first = redebit_of(original)
    second_return = returned_on_redebit(first)
    assert nsf.on_return(second_return, NOTICE) is not None
    second = redebit_of(second_return)
    # The first re-debit was made more than 180 days before the third return.
    OneOffCharge.objects.filter(pk=first.pk).update(created_at=NOTICE_AT - timedelta(days=200))
    third_return = returned_on_redebit(second)
    outcome = nsf.on_return(third_return, NOTICE)
    assert outcome is not None and outcome.retry == nsf.RETRY_AUTO


# --- F3: the schedule on the eve ------------------------------------------------------------


def test_the_schedule_is_made_only_on_the_eve_and_once(fake: FakeProvider, worker: None) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    redebit = redebit_of(payment)
    assert nsf.create_due_redebits(NOTICE) == 0
    assert nsf.create_due_redebits(date(2026, 11, 9)) == 0
    assert not Job.objects.filter(kind=JobKind.SCHEDULE_CREATE).exists()
    # Tuesday Nov 10: the next business day is Thursday Nov 12, the retry.
    assert nsf.create_due_redebits(date(2026, 11, 10)) == 1
    assert nsf.create_due_redebits(date(2026, 11, 10)) == 0
    redebit.refresh_from_db()
    assert redebit.retry_state == RetryState.SCHEDULED
    [job] = Job.objects.filter(kind=JobKind.SCHEDULE_CREATE)
    assert job.entity_id == redebit.pk
    assert AuditLog.objects.filter(action=nsf.SCHEDULED_ACTION, entity_id=str(redebit.pk)).exists()
    run_queued_jobs()
    [schedule] = [args[0] for name, args in fake.calls if name == "create_scheduled_invoice"]
    assert isinstance(schedule, ScheduleInput)
    # The original amount only: the fee is its own charge.
    assert schedule.amounts.total == D("1500.00")


def test_a_stopped_retry_gets_no_schedule(fake: FakeProvider) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    redebit = redebit_of(payment)
    assert nsf.stop_retry(redebit, a_user(Role.BILLING)).ok
    assert nsf.create_due_redebits(date(2026, 11, 10)) == 0
    assert nsf.create_due_redebits(RETRY) == 0
    assert not Job.objects.filter(kind=JobKind.SCHEDULE_CREATE).exists()


def test_the_daily_tick_schedules_the_eve(fake: FakeProvider, clock: Clock) -> None:
    from django.core.management import call_command

    a_world(fake)
    payment = returned(fake, "R01")
    clock.at(date(2026, 11, 10), 14)
    call_command("collections_tick", stdout=_Sink())
    assert redebit_of(payment).retry_state == RetryState.SCHEDULED


class _Sink:
    def write(self, text: str) -> None:
        self.text = text

    def flush(self) -> None:
        return None


def test_a_settled_automatic_re_debit_is_done_and_resolves_the_return(fake: FakeProvider) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    redebit = redebit_of(payment)
    OneOffCharge.objects.filter(pk=redebit.pk).update(retry_state=RetryState.SCHEDULED)
    paid = Payment.objects.create(
        one_off_charge=redebit,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("1500.00"),
        status=PaymentStatus.SETTLED,
        settled_on=RETRY,
    )
    nsf.on_settled(paid)
    redebit.refresh_from_db()
    payment.refresh_from_db()
    assert redebit.retry_state == RetryState.DONE
    assert payment.resolved_at is not None and payment.resolution_note == (
        "Re-debited automatically on Nov 12, 2026."
    )
    # A second settle changes nothing more.
    nsf.on_settled(paid)
    assert AuditLog.objects.filter(action=nsf.DONE_ACTION).count() == 1


# --- F3: Stop automatic retry ----------------------------------------------------------------


def test_staff_stop_the_retry_from_the_charge(fake: FakeProvider) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    redebit = redebit_of(payment)
    url = reverse("ledger:one_off_charges:stop_retry", kwargs={"pk": redebit.pk})
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(url).status_code == 403
    page = client_for(Role.BILLING)[0].get(
        reverse("ledger:one_off_charges:detail", kwargs={"pk": redebit.pk})
    )
    assert 'id="stop-retry"' in page.content.decode()
    assert "Automatic re-debit on Nov 12, 2026" in page.content.decode()
    client, user = client_for(Role.BILLING)
    response = client.post(url)
    assert response.status_code == 302
    redebit.refresh_from_db()
    assert (redebit.retry_state, redebit.status, redebit.void_note) == (
        RetryState.STOPPED,
        OneOffChargeStatus.VOIDED,
        nsf.STOPPED_BY_STAFF,
    )
    stopped = AuditLog.objects.get(action=nsf.STOPPED_ACTION, entity_id=str(redebit.pk))
    assert stopped.actor == user and stopped.after is not None
    assert stopped.after["reason"] == "Stopped by staff"
    # A second stop changes nothing.
    assert not nsf.stop_retry(redebit, user).ok
    assert not fake.calls or all(name != "delete_scheduled_invoice" for name, _ in fake.calls)


def test_a_logged_call_within_two_business_days_stops_the_retry(
    fake: FakeProvider, clock: Clock
) -> None:
    world = a_world(fake)
    payment = returned(fake, "R01")
    # Tuesday Nov 10 is the second business day after the Friday notice.
    clock.at(date(2026, 11, 10))
    message = logged(world, ContactKind.CALL)
    assert message.endswith(contacts.RETRY_STOPPED)
    redebit = redebit_of(payment)
    assert (redebit.retry_state, redebit.status) == (
        RetryState.STOPPED,
        OneOffChargeStatus.VOIDED,
    )
    stopped = AuditLog.objects.get(action=nsf.STOPPED_ACTION, entity_id=str(redebit.pk))
    assert stopped.after is not None and stopped.after["reason"] == "Staff contact logged"


@pytest.mark.parametrize("kind", [ContactKind.EMAIL, ContactKind.VISIT, ContactKind.PROMISE])
def test_an_e_mail_a_visit_or_a_promise_stops_it_too(fake: FakeProvider, kind: str) -> None:
    world = a_world(fake)
    payment = returned(fake, "R01")
    logged(world, kind)
    assert redebit_of(payment).retry_state == RetryState.STOPPED


def test_a_call_on_the_third_business_day_does_not_stop_it(
    fake: FakeProvider, clock: Clock
) -> None:
    world = a_world(fake)
    payment = returned(fake, "R01")
    clock.at(RETRY, 14)
    message = logged(world, ContactKind.CALL)
    assert message == contacts.ADDED
    assert redebit_of(payment).retry_state == RetryState.AUTO_PENDING
    # Nor does the holiday between, Wednesday Nov 11 (after the second business day).
    clock.at(date(2026, 11, 11))
    logged(world, ContactKind.CALL)
    assert redebit_of(payment).retry_state == RetryState.AUTO_PENDING


def test_a_contact_about_another_lease_does_not_stop_it(fake: FakeProvider) -> None:
    from tests.test_lease_services import make_lease

    world = a_world(fake)
    payment = returned(fake, "R01")
    other = make_lease(operator=world.operator)
    data = contacts.ContactInput(
        kind=ContactKind.CALL,
        happened_on=NOTICE,
        body="About the other store.",
        lease=other.pk,
    )
    assert contacts.add_entry(world.operator.pk, data, actor=a_user(Role.BILLING)).ok
    assert redebit_of(payment).retry_state == RetryState.AUTO_PENDING


def test_a_scheduled_retry_is_stopped_through_the_cancel(fake: FakeProvider, worker: None) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    redebit = redebit_of(payment)
    assert nsf.create_due_redebits(date(2026, 11, 10)) == 1
    run_queued_jobs()
    outcome = nsf.stop_retry(redebit, a_user(Role.BILLING))
    assert outcome.ok and outcome.message == nsf.STOP_CANCEL_QUEUED
    redebit.refresh_from_db()
    assert redebit.retry_state == RetryState.STOPPED
    assert Job.objects.filter(kind=JobKind.SCHEDULE_DELETE, entity_id=redebit.pk).exists()
    run_queued_jobs()
    redebit.refresh_from_db()
    assert redebit.status == OneOffChargeStatus.VOIDED


# --- F4: Debit again and staff voids ---------------------------------------------------------


def test_debit_again_waits_for_the_pending_retry(fake: FakeProvider) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    outcome = nsf.reschedule(
        payment.pk,
        redebit_date=date(2026, 11, 20),
        waive_fee=False,
        note="",
        actor=a_user(Role.BILLING),
    )
    assert not outcome.ok
    assert outcome.message == nsf.RETRY_PENDING.format(date="Nov 12, 2026")


def test_debit_again_after_a_stop_adds_no_second_fee(fake: FakeProvider) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    billing = a_user(Role.BILLING)
    assert nsf.stop_retry(redebit_of(payment), billing).ok
    admin = a_user(Role.ADMIN)
    refused = nsf.reschedule(
        payment.pk, redebit_date=date(2026, 11, 20), waive_fee=True, note="Waive.", actor=admin
    )
    assert not refused.ok and refused.message == nsf.WAIVE_AT_RETURN
    outcome = nsf.reschedule(
        payment.pk, redebit_date=date(2026, 11, 20), waive_fee=False, note="", actor=billing
    )
    assert outcome.ok, outcome.message
    assert "NSF fee added at the return" in outcome.message
    assert OneOffCharge.objects.filter(category=OneOffCategory.NSF_FEE).count() == 1
    assert Operator.objects.get().nsf_return_count == 1
    live = nsf.live_redebit(payment)
    assert live is not None and live.retry_state == "" and live.price == D("1500.00")
    assert not OneOffCharge.objects.filter(companion_of=live).exists()
    payment.refresh_from_db()
    assert payment.resolved_at is not None


def test_debit_again_on_a_non_retried_code_adds_no_second_fee(fake: FakeProvider) -> None:
    a_world(fake)
    payment = returned(fake, "R02")
    outcome = nsf.reschedule(
        payment.pk,
        redebit_date=date(2026, 11, 20),
        waive_fee=False,
        note="New account linked.",
        actor=a_user(Role.BILLING),
    )
    assert outcome.ok, outcome.message
    assert OneOffCharge.objects.filter(category=OneOffCategory.NSF_FEE).count() == 1
    [schedule_job] = Job.objects.filter(kind=JobKind.SCHEDULE_CREATE)
    assert schedule_job.entity_id == redebit_of(payment).pk


def test_a_staff_void_queues_no_message(fake: FakeProvider) -> None:
    world = a_world(fake)
    provider_txn(fake)
    deliver("create_transaction", id=TXN)
    payment = payment_for()
    assert payment.status == PaymentStatus.PENDING
    outcome = refunds.void_payment(payment.pk, confirmed=True, actor=a_user(Role.ADMIN))
    run_queued_jobs()
    payment.refresh_from_db()
    assert outcome.ok, outcome.message
    assert not f_messages()
    assert not OneOffCharge.objects.exists()
    assert not OutboundMessage.objects.filter(lease=world.lease).exists()


def test_a_manual_payment_void_queues_no_message(fake: FakeProvider) -> None:
    world = a_world(fake)
    manual = Payment.objects.create(
        rent_charge=world.charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ZELLE,
        amount=D("1500.00"),
        note="Zelle reference 78.",
        status=PaymentStatus.SETTLED,
        settled_on=NOTICE,
    )
    outcome = refunds.void_manual_payment(
        manual.pk, reason="Entered twice.", actor=a_user(Role.BILLING)
    )
    assert outcome.ok, outcome.message
    assert not OutboundMessage.objects.exists()


# --- F5: attention -------------------------------------------------------------------------


def test_a_pending_retry_is_listed_low_and_not_as_a_returned_debit(fake: FakeProvider) -> None:
    world = a_world(fake)
    payment = returned(fake, "R01")
    found = attention.attention(NOTICE, limit=None)
    kinds = [item.kind for item in found.items]
    assert attention.RETURNED_DEBIT not in kinds
    [item] = [item for item in found.items if item.kind == attention.REDEBIT_PENDING]
    redebit = redebit_of(payment)
    assert item.severity == attention.LOW
    store = world.lease.store.external_label
    assert item.text == f"Automatic re-debit: {store}, $1,500.00 on Nov 12, 2026"
    assert item.url == reverse("ledger:one_off_charges:detail", kwargs={"pk": redebit.pk})
    assert item.operator_id == world.operator.pk and item.since == NOTICE
    mine = attention.attention(NOTICE, operator=world.operator.pk, limit=None)
    assert attention.REDEBIT_PENDING in [entry.kind for entry in mine.items]
    # Stopped: the return is listed again, the pending item is gone.
    assert nsf.stop_retry(redebit, a_user(Role.BILLING)).ok
    kinds = [entry.kind for entry in attention.attention(NOTICE, limit=None).items]
    assert attention.RETURNED_DEBIT in kinds and attention.REDEBIT_PENDING not in kinds


def test_a_return_without_retry_keeps_firing_returned_debit(fake: FakeProvider) -> None:
    a_world(fake)
    returned(fake, "R10")
    kinds = [item.kind for item in attention.attention(NOTICE, limit=None).items]
    assert attention.RETURNED_DEBIT in kinds and attention.REDEBIT_PENDING not in kinds


# --- F4: the returns queue page --------------------------------------------------------------


def test_the_returns_page_shows_each_retry_state(fake: FakeProvider) -> None:
    a_world(fake)
    pending = returned(fake, "R01")
    client, _ = client_for(Role.BILLING)
    page = client.get(reverse("ledger:returns:list")).content.decode()
    assert "Automatic re-debit on Nov 12" in page
    assert f'id="stop-retry-{pending.pk}"' in page
    assert f'id="reschedule-{pending.pk}"' not in page
    response = client.post(
        reverse("ledger:one_off_charges:stop_retry", kwargs={"pk": redebit_of(pending).pk}),
        {"back": "returns"},
    )
    assert response.status_code == 302
    assert response["Location"].endswith(f"#return-{pending.pk}")
    page = client.get(reverse("ledger:returns:list")).content.decode()
    assert re.search(rf'id="retry-{pending.pk}"[^>]*>Stopped ', page)
    assert f'id="reschedule-{pending.pk}"' in page
    # The admin's waiver box is gone where the fee was made at the return.
    admin_page = client_for(Role.ADMIN)[0].get(reverse("ledger:returns:list")).content.decode()
    assert f'id="waive-fee-{pending.pk}"' not in admin_page


def test_the_returns_page_names_a_code_without_retry(fake: FakeProvider) -> None:
    a_world(fake)
    payment = returned(fake, "R10")
    page = client_for(Role.VIEWER)[0].get(reverse("ledger:returns:list")).content.decode()
    assert re.search(rf'id="retry-{payment.pk}"[^>]*>No retry: R10 ', page)


# --- D7's window applies to F ----------------------------------------------------------------


def test_a_return_in_the_evening_sends_f_at_nine_the_next_morning(
    fake: FakeProvider, clock: Clock
) -> None:
    a_world(fake)
    # 23:30 UTC is 18:30 in New York: outside the window.
    clock.now = datetime(2026, 11, 6, 23, 30, tzinfo=UTC)
    returned(fake, "R01")
    [message] = f_messages()
    assert message.status == MessageStatus.QUEUED
    assert message.scheduled_for == datetime(2026, 11, 7, 14, 0, tzinfo=UTC)
    [job] = Job.objects.filter(kind=JobKind.MESSAGE_SEND)
    assert job.run_after == message.scheduled_for


# --- Ledger migration 0063 both ways -------------------------------------------------------

BEFORE = ("ledger", "0062_rent_month_messages")
AFTER = ("ledger", "0063_returned_debits")


def _migrate(target: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    return MigrationExecutor(connection).loader.project_state(target).apps


def _columns() -> set[str]:
    with connection.cursor() as cursor:
        return {
            column.name
            for column in connection.introspection.get_table_description(cursor, "one_off_charges")
        }


@pytest.mark.django_db(transaction=True)
def test_migration_0063_activates_the_fee_and_reverses_only_without_its_rows() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = _migrate(BEFORE)
        assert "retry_state" not in _columns()
        # A flush by an earlier transactional test removes the seeded rows: seed it as 0027.
        old.get_model("ledger", "FeeType").objects.get_or_create(
            code=NSF_FEE_FIRST,
            defaults={"name": "NSF fee, first return", "default_price": D("100.00")},
        )
        old.get_model("ledger", "FeeType").objects.filter(code=NSF_FEE_FIRST).update(active=False)
        new = _migrate(AFTER)
        assert "retry_state" in _columns() and "retry_date" in _columns()
        assert new.get_model("ledger", "FeeType").objects.get(code=NSF_FEE_FIRST).active
        owner = new.get_model("ledger", "Operator").objects.create(display_name="Migrating")
        payment = new.get_model("ledger", "Payment").objects.create(
            kind="rent",
            method="ach",
            amount=D("10.00"),
            status="returned",
            returned_at=NOTICE_AT,
        )
        charges = new.get_model("ledger", "OneOffCharge").objects
        row = {
            "operator_id": owner.pk,
            "category": "redebit",
            "collection": "autopay_due",
            "description": "Re-debit",
            "price": D("10.00"),
            "due_date": RETRY,
            "source_payment_id": payment.pk,
        }
        # The new CHECKs: a retry state needs its date on a re-debit of a return; and an
        # unknown state is refused.
        for bad in (
            {"retry_state": "auto_pending"},
            {"retry_state": "later", "retry_date": RETRY},
            {"retry_date": RETRY},
        ):
            with pytest.raises(IntegrityError), transaction.atomic():
                charges.create(**{**row, **bad})
        charges.create(**row, retry_state="auto_pending", retry_date=RETRY)
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(BEFORE)
        assert "retry_state" in _columns()
        charges.all().delete()
        fee = charges.create(
            operator_id=owner.pk,
            category="nsf_fee",
            collection="manual",
            description="Fee",
            price=D("100.00"),
            due_date=NOTICE,
            source_payment_id=payment.pk,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            charges.create(
                operator_id=owner.pk,
                category="nsf_fee",
                collection="manual",
                description="Fee again",
                price=D("100.00"),
                due_date=NOTICE,
                source_payment_id=payment.pk,
            )
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(BEFORE)
        fee.delete()
        back = _migrate(BEFORE)
        assert "retry_state" not in _columns()
        assert not back.get_model("ledger", "FeeType").objects.get(code=NSF_FEE_FIRST).active
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaves)
