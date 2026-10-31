"""PLAN-014 Batch F (Fable): returned bank debits — adversarial cases beside Opus's
``test_returns_nsf.py``. The return event replayed twice makes one fee, one re-debit, one message
and one send job; the F body names the plain words, the fee, the retry date, the original debit's
date and the returned amount, and no account data; every code's words and retry flag through the
webhook; the third business day over Thanksgiving, Christmas and MLK Day; the retry date from a
notice before Thanksgiving; the stop route refuses a viewer, a GET and a bogus id and returns to
the returns queue when asked; a replayed return with the fee type inactive still makes one
message; the F row on the charge's log and the store timeline; the fee's own page names the
return."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    FeeType,
    MessageRule,
    OneOffCharge,
    OneOffChargeStatus,
    OutboundMessage,
    RetryState,
)
from ledger.services import business_days
from payments.models import Job, JobKind
from tests.test_operator_views import client_for
from tests.test_returns_nsf import (
    NOTICE,
    RETRY,
    a_world,
    f_messages,
    fees_of,
    redebit_of,
    returned,
)
from tests.test_returns_nsf import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_returns_nsf import fake as fake  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_webhook_handlers import TXN, deliver

pytestmark = pytest.mark.django_db


# --- The return event, once and replayed ----------------------------------------------------


def test_the_return_event_replayed_twice_makes_one_of_everything(fake: Any) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    for _ in range(2):
        assert deliver("update_transaction_settled", id=TXN) in (
            "processed",
            "ignored",
            "duplicate",
        )
    assert len(fees_of(payment)) == 1
    redebit = redebit_of(payment)
    assert redebit.retry_state == RetryState.AUTO_PENDING and redebit.retry_date == RETRY
    assert len(f_messages()) == 1
    assert Job.objects.filter(kind=JobKind.MESSAGE_SEND).count() == 1
    assert AuditLog.objects.filter(action="payment.return_processed").count() == 1


def test_a_replayed_return_with_the_fee_type_inactive_still_makes_one_message(fake: Any) -> None:
    FeeType.objects.filter(code="nsf_fee_first").update(active=False)
    a_world(fake)
    payment = returned(fake, "R01")
    deliver("update_transaction_settled", id=TXN)
    assert fees_of(payment) == []
    assert len(f_messages()) == 1
    assert redebit_of(payment).retry_state == RetryState.AUTO_PENDING


# --- The F body -------------------------------------------------------------------------------


def test_the_f_body_names_the_words_the_fee_the_dates_and_the_returned_amount(fake: Any) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    message = f_messages()[0]
    body = message.rendered_body
    assert payment.initiated_on is not None
    debited = payment.initiated_on
    assert message.template.code == "returned_debit" and message.rule == MessageRule.F
    assert "Reason: Insufficient funds" in body
    assert "A returned-payment (NSF) fee of $100.00 has been added" in body
    assert "will be debited again automatically on Nov 12, 2026" in body
    assert f"made on {debited:%b} {debited.day}, {debited.year}" in body
    assert "$1,500.00" in body  # the returned amount
    assert "4111" not in body and "routing" not in body.lower() and "Bank text" not in body


@pytest.mark.parametrize(
    ("code", "words", "retries"),
    [
        ("R09", "Uncollected funds", True),
        ("R02", "The bank account is closed", False),
        ("R29", "Your bank reports this debit was not authorized", False),
        ("R16", "The account is frozen", False),
        ("R99", "please contact your bank for details", False),
    ],
)
def test_each_code_through_the_webhook_gets_its_words_and_only_r01_and_r09_a_retry(
    fake: Any, code: str, words: str, retries: bool
) -> None:
    a_world(fake)
    payment = returned(fake, code)
    message = f_messages()[0]
    assert words in message.rendered_body
    assert (message.template.code == "returned_debit") is retries
    has_redebit = OneOffCharge.objects.filter(source_payment=payment, category="redebit").exists()
    assert has_redebit is retries


# --- Business days ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("notice", "third"),
    [
        (date(2026, 11, 2), date(2026, 11, 5)),  # Mon → Thu
        (date(2026, 11, 24), date(2026, 11, 30)),  # Tue → Mon: Thanksgiving Nov 26 and the weekend
        (date(2026, 12, 23), date(2026, 12, 29)),  # Wed → Tue: Christmas Fri 25 and the weekend
        (date(2027, 1, 14), date(2027, 1, 20)),  # Thu → Wed: the weekend and MLK Day Mon 18
        (date(2027, 7, 1), date(2027, 7, 7)),  # Thu → Wed: the weekend and July 4 kept on Mon 5
        (
            date(2026, 7, 2),
            date(2026, 7, 7),
        ),  # Thu → Tue: Sat July 4 2026 not observed, Fri 3 counts
    ],
)
def test_the_third_business_day_over_holidays(notice: date, third: date) -> None:
    assert business_days.add(notice, 3) == third
    assert business_days.is_business_day(third)
    assert business_days.add(notice, 0) == notice


def test_the_retry_date_counts_from_the_notice_over_thanksgiving(fake: Any, clock: Any) -> None:
    clock.at(date(2026, 11, 24))
    a_world(fake)
    payment = returned(fake, "R01")
    assert redebit_of(payment).retry_date == date(2026, 11, 30)
    assert NOTICE < date(2026, 11, 24)


# --- The stop route -------------------------------------------------------------------------------


def test_the_stop_route_refuses_a_viewer_a_get_and_a_bogus_id(fake: Any) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    redebit = redebit_of(payment)
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(f"/one-off-charges/{redebit.pk}/stop-retry/").status_code in (302, 403)
    billing, _ = client_for(Role.BILLING)
    assert billing.get(f"/one-off-charges/{redebit.pk}/stop-retry/").status_code == 405
    assert billing.post("/one-off-charges/not-a-uuid/stop-retry/").status_code == 404
    redebit.refresh_from_db()
    assert redebit.retry_state == RetryState.AUTO_PENDING


def test_the_stop_from_the_returns_queue_returns_there_and_stops_once(fake: Any) -> None:
    a_world(fake)
    payment = returned(fake, "R01")
    redebit = redebit_of(payment)
    billing, _ = client_for(Role.BILLING)
    response = billing.post(f"/one-off-charges/{redebit.pk}/stop-retry/", {"back": "returns"})
    assert response.status_code == 302 and response["Location"].startswith("/returns/")
    redebit.refresh_from_db()
    assert redebit.retry_state == RetryState.STOPPED and redebit.status == OneOffChargeStatus.VOIDED
    again = billing.post(f"/one-off-charges/{redebit.pk}/stop-retry/", follow=True)
    assert "no automatic re-debit to stop" in again.content.decode()
    assert AuditLog.objects.filter(action="redebit.stopped").count() == 1


# --- The pages ----------------------------------------------------------------------------------


def test_the_f_row_is_on_the_charges_log_and_the_store_timeline(fake: Any) -> None:
    world = a_world(fake)
    payment = returned(fake, "R01")
    message = f_messages()[0]
    billing, _ = client_for(Role.BILLING)
    charge_page = billing.get(f"/charges/{payment.rent_charge_id}/").content.decode()
    assert "F — payment returned" in charge_page
    timeline = billing.get(
        f"/stores/{world.lease.store_id}/?tab=contacts&show=emails"
    ).content.decode()
    assert f'id="message-{message.pk}"' in timeline
    assert "Your payment for #" in timeline  # F's subject, in the details summary


def test_the_fees_own_page_names_the_return_and_offers_no_retry_line(fake: Any) -> None:
    a_world(fake)
    payment = returned(fake, "R10")
    fee = fees_of(payment)[0]
    billing, _ = client_for(Role.BILLING)
    page = billing.get(f"/one-off-charges/{fee.pk}/").content.decode()
    assert "Returned debit (R10)" in page
    assert "Your bank reports this debit was not authorized" in page
    assert 'id="stop-retry"' not in page
    assert OutboundMessage.objects.filter(rule=MessageRule.F).count() == 1
