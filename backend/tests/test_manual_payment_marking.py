"""Manual payments marked paid at the payment provider (docs/qa/PLAN-009-payments-ledger.md
§4 rows 1, 2, 3 and 7; D1 to D3), the retry, the mark-paid request, and the carried
balance (D5).

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`; the mark-paid
request itself is checked against the real adapter with a mock transport. Nothing opens
a socket.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger import wording
from ledger.models import (
    AuditLog,
    Invoice,
    Lease,
    Payment,
    PaymentChannel,
    PaymentMethod,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import rent_run
from ledger.services.rent_run import MarkAction
from payments import offline_marking
from payments.errors import ProviderRejected, ProviderUnavailable, ValidationFailed
from payments.fake import FakeProvider
from payments.provider import OfflinePayment
from payments.stax.adapter import StaxProvider
from payments.stax.breaker import CircuitBreaker, reset_breaker
from payments.stax.budget import FailureBudget, reset_budget
from payments.stax.state import MemoryStateStore
from tests.job_helpers import worker_between_requests  # noqa: F401
from tests.stax_helpers import always, make_rig
from tests.test_operator_views import client_for
from tests.test_rent_run import RENT, charge_of, enrolled, invoice, lease

# PLAN-010 D3: views queue provider work; the worker is run after every request here.
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("worker_between_requests")]

D = Decimal
DAY = date(2026, 10, 15)
INVOICE_REF = "fake-inv-0001"
NOTE = "Wire from the operator's bank, received 15 October."


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


@pytest.fixture(autouse=True)
def health() -> Iterator[None]:
    reset_breaker(CircuitBreaker(MemoryStateStore(), on_open=lambda opened, reopen: None))
    reset_budget(FailureBudget(MemoryStateStore()))
    yield
    reset_breaker()
    reset_budget()


def invoiced(fake: FakeProvider, total: Decimal = RENT, status: str = "FAKE-SENT") -> RentCharge:
    """A charge of an autopay lease, linked to the provider invoice ``INVOICE_REF``."""
    row, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, ref, total=total, status=status, external_id=INVOICE_REF)
    rent_run.match_schedule_invoices(actor=None)
    fake.calls.clear()
    return charge_of(row)


def pay(charge: RentCharge, amount: str, **overrides: Any) -> rent_run.PaymentOutcome:
    values: dict[str, Any] = {
        "method": PaymentMethod.WIRE,
        "amount": D(amount),
        "paid_on": DAY,
        "note": NOTE,
        "actor": None,
        "today": DAY,
    }
    values.update(overrides)
    return rent_run.record_manual_payment(charge.pk, **values)


def call_names(fake: FakeProvider) -> list[str]:
    return [name for name, _ in fake.calls]


def marks(fake: FakeProvider) -> list[OfflinePayment]:
    return [
        args[1]  # type: ignore[misc]
        for name, args in fake.calls
        if name == "mark_invoice_paid_cash_or_check"
    ]


def actions(*names: str) -> list[str]:
    return list(
        AuditLog.objects.filter(action__in=names).order_by("id").values_list("action", flat=True)
    )


def later(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    moment = timezone.now() + timedelta(seconds=seconds)
    monkeypatch.setattr("django.utils.timezone.now", lambda: moment)


def local_invoice(charge: RentCharge) -> Invoice:
    return Invoice.objects.get(rent_charge=charge)


# --- Row 1: a full manual payment on an invoiced charge -------------------------------------


def test_row1_a_full_payment_is_marked_paid_as_cash_between_two_fetches(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake)
    outcome = pay(charge, "1500.00", reference="WIRE ref 2026/10-15 #77")
    assert outcome.ok and outcome.provider is not None
    assert (outcome.provider.action, outcome.provider.message) == (
        MarkAction.MARKED,
        rent_run.MARK_DONE,
    )
    assert call_names(fake) == ["get_invoice", "mark_invoice_paid_cash_or_check", "get_invoice"]
    assert [args[0] for _, args in fake.calls] == [INVOICE_REF] * 3
    assert marks(fake) == [
        OfflinePayment(
            method="cash",
            amount=D("1500.00"),
            check_number="WIREref202610-1577",
            note=f"Paid by wire on 10/15/2026. {NOTE}",
            email_receipt=False,
        )
    ]
    charge.refresh_from_db()
    assert (charge.status, charge.amount_paid, charge.balance) == (
        RentChargeStatus.PAID,
        RENT,
        D("0.00"),
    )
    assert (charge.needs_review, charge.review_message) == (False, "")
    payment = Payment.objects.get()
    assert (payment.method, payment.reference) == (PaymentMethod.WIRE, "WIRE ref 2026/10-15 #77")
    stored = local_invoice(charge)
    assert (stored.provider_status, stored.amount_paid) == ("PAID", RENT)
    assert stored.paid_at is not None
    assert actions("rent_charge.payment_recorded", rent_run.MARKED_ACTION) == [
        "rent_charge.payment_recorded",
        rent_run.MARKED_ACTION,
    ]
    marked = AuditLog.objects.get(action=rent_run.MARKED_ACTION)
    assert marked.after is not None
    assert marked.after["payment"] == str(payment.pk)
    assert marked.after["provider_invoice"] == "fake…0001"
    assert marked.after["provider_changes"]["provider_status"] == ["FAKE-SENT", "PAID"]
    assert INVOICE_REF not in str(marked.after)


@pytest.mark.parametrize(
    ("method", "word"),
    [(PaymentMethod.WIRE, "wire"), (PaymentMethod.ZELLE, "Zelle"), (PaymentMethod.OTHER, "other")],
)
def test_the_memo_names_the_real_method_and_date_and_the_local_method_is_kept(
    fake: FakeProvider, method: str, word: str
) -> None:
    charge = invoiced(fake)
    assert pay(charge, "1500.00", method=method, paid_on=date(2026, 10, 3)).ok
    [sent] = marks(fake)
    assert sent.method == "cash"
    assert sent.note.startswith(f"Paid by {word} on 10/03/2026. ")
    assert Payment.objects.get().method == method


def test_an_empty_reference_sends_no_check_number_and_a_long_note_is_cut_to_255(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake)
    note = "Line one\nline two\t" + "x" * 400
    assert pay(charge, "1500.00", note=note, reference=" #/ ").ok
    [sent] = marks(fake)
    assert sent.check_number == ""
    assert len(sent.note) == 255
    assert sent.note.startswith("Paid by wire on 10/15/2026. Line one line two x")
    assert "\n" not in sent.note and "\t" not in sent.note


def test_a_long_reference_is_cut_to_25_letters_digits_and_hyphens(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    assert pay(charge, "1500.00", reference="ABC-123 456 789 012 345 678 901 234").ok
    [sent] = marks(fake)
    assert sent.check_number == "ABC-123456789012345678901"


def test_a_replayed_form_marks_nothing_twice(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    key = uuid.uuid4()
    assert pay(charge, "1500.00", creation_key=key).ok
    again = pay(charge, "1500.00", creation_key=key)
    assert (again.replayed, again.provider) == (True, None)
    assert len(marks(fake)) == 1 and Payment.objects.count() == 1


# --- Row 2: a partial manual payment ---------------------------------------------------------


def test_row2_a_partial_payment_makes_no_call_and_flags_the_charge(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    outcome = pay(charge, "500.00")
    assert outcome.ok and outcome.provider is not None
    assert (outcome.provider.action, outcome.provider.message) == (
        MarkAction.NOT_UPDATED,
        "Not marked paid in Stax: part of the amount",
    )
    assert fake.calls == []
    charge.refresh_from_db()
    assert (charge.status, charge.balance) == (RentChargeStatus.PARTIAL, D("1000.00"))
    assert charge.needs_review is True
    assert charge.review_message == "Stax invoice not updated: partial amount (TODO-VERIFY-028)"
    assert rent_run.charge_review_message(charge) == (
        "Not marked paid in Stax: part of the amount."
    )
    entry = AuditLog.objects.get(action=rent_run.NOT_UPDATED_ACTION)
    assert entry.after is not None and entry.after["reason"] == offline_marking.PARTIAL_AMOUNT
    assert local_invoice(charge).provider_status == "FAKE-SENT"


def test_a_partial_payment_on_a_charge_without_an_invoice_is_not_flagged(
    fake: FakeProvider,
) -> None:
    row = lease(payment_channel=PaymentChannel.WIRE)
    rent_run.create_charges(DAY, actor=None)
    outcome = pay(charge_of(row), "500.00")
    assert outcome.provider is not None and outcome.provider.action is MarkAction.NO_INVOICE
    charge = charge_of(row)
    assert (charge.needs_review, charge.review_message) == (False, "")
    assert fake.calls == []


# --- Row 3: two partials completing the charge ------------------------------------------------


def test_row3_two_partials_pay_the_charge_without_a_mark_and_the_flag_stays(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake)
    assert pay(charge, "500.00").ok
    second = pay(charge, "1000.00", method=PaymentMethod.ZELLE)
    assert second.provider is not None and second.provider.action is MarkAction.NOT_UPDATED
    assert fake.calls == []
    charge.refresh_from_db()
    assert (charge.status, charge.amount_paid, charge.balance) == (
        RentChargeStatus.PAID,
        RENT,
        D("0.00"),
    )
    assert charge.needs_review is True
    assert charge.review_message == offline_marking.PARTIAL_AMOUNT
    assert actions(rent_run.NOT_UPDATED_ACTION) == [rent_run.NOT_UPDATED_ACTION] * 2
    assert rent_run.provider_retry_wait(charge) is None


# --- Row 7: the provider already shows the invoice paid ---------------------------------------


def test_row7_an_invoice_already_paid_is_not_marked_again_and_the_mismatch_is_audited(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake)
    fake.invoices[INVOICE_REF] = replace(fake.invoices[INVOICE_REF], raw_status="PAID")
    outcome = pay(charge, "1500.00")
    assert outcome.ok and outcome.provider is not None
    assert outcome.provider.action is MarkAction.ALREADY_PAID
    assert outcome.provider.message == (
        "Invoice already paid in Stax; not marked again. Check for a double payment"
    )
    assert call_names(fake) == ["get_invoice"]
    payment = Payment.objects.get()
    charge.refresh_from_db()
    assert (charge.status, charge.amount_paid) == (RentChargeStatus.PAID, RENT)
    assert (charge.needs_review, charge.review_message) == (True, offline_marking.ALREADY_PAID)
    entry = AuditLog.objects.get(action=rent_run.ALREADY_PAID_ACTION)
    assert (entry.entity_type, entry.entity_id) == ("payment", str(payment.pk))
    assert entry.after is not None and entry.after["rent_charge"] == str(charge.pk)
    assert local_invoice(charge).provider_status == "PAID"
    assert rent_run.provider_retry_wait(charge) is None


def test_the_paid_status_is_read_without_case_or_spaces(fake: FakeProvider) -> None:
    charge = invoiced(fake, status=" paid ")
    assert pay(charge, "1500.00").provider.action is MarkAction.ALREADY_PAID  # type: ignore[union-attr]
    assert marks(fake) == []


# --- D2: failures and the retry ----------------------------------------------------------------


def test_a_rejected_mark_keeps_the_payment_and_flags_class_and_fields(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    fake.fail_next(
        "mark_invoice_paid_cash_or_check", ProviderRejected(422, error_fields=("payment_note",))
    )
    outcome = pay(charge, "1500.00")
    assert outcome.ok and outcome.provider is not None
    assert outcome.provider.action is MarkAction.FAILED
    assert outcome.provider.message == (
        "Not marked paid in Stax: Rejected. The payment is recorded; retry from the charge page."
    )
    charge.refresh_from_db()
    assert (charge.status, charge.needs_review) == (RentChargeStatus.PAID, True)
    assert charge.review_message == (
        "Stax invoice not marked paid: ProviderRejected (fields: payment_note)"
    )
    assert Payment.objects.count() == 1
    failed = AuditLog.objects.get(action=rent_run.MARK_FAILED_ACTION)
    assert failed.after is not None
    assert (failed.after["stage"], failed.after["failure"], failed.after["fields"]) == (
        rent_run.STAGE_MARK,
        "ProviderRejected",
        "payment_note",
    )
    assert local_invoice(charge).provider_status == "FAKE-SENT"


def test_a_retry_waits_60_seconds_then_marks_and_clears_the_flag(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    charge = invoiced(fake)
    fake.fail_next("mark_invoice_paid_cash_or_check", ProviderUnavailable(503))
    assert pay(charge, "1500.00", reference="R-1").provider.action is MarkAction.FAILED  # type: ignore[union-attr]
    assert rent_run.provider_retry_wait(charge) == 60
    fake.calls.clear()

    too_soon = rent_run.retry_provider_mark(charge.pk, actor=None)
    assert (too_soon.action, too_soon.retry_in) == (MarkAction.RETRY_TOO_SOON, 60)
    assert too_soon.message == "Retry available in 60 seconds."
    assert fake.calls == []

    later(monkeypatch, 59)
    assert rent_run.retry_provider_mark(charge.pk, actor=None).retry_in == 1
    later(monkeypatch, 60)
    done = rent_run.retry_provider_mark(charge.pk, actor=None)
    assert done.action is MarkAction.MARKED
    assert call_names(fake) == ["get_invoice", "mark_invoice_paid_cash_or_check", "get_invoice"]
    assert marks(fake)[0].check_number == "R-1"
    charge.refresh_from_db()
    assert (charge.needs_review, charge.review_message) == (False, "")
    assert local_invoice(charge).provider_status == "PAID"
    assert rent_run.provider_retry_wait(charge) is None
    nothing = rent_run.retry_provider_mark(charge.pk, actor=None)
    assert nothing.action is MarkAction.NOTHING_TO_RETRY


def test_a_failed_reread_after_a_good_mark_is_confirmed_on_retry_without_a_second_mark(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    charge = invoiced(fake)
    original = fake.mark_invoice_paid_cash_or_check

    def mark_then_fail_reread(external_id: str, payment: OfflinePayment) -> Any:
        record = original(external_id, payment)
        fake.fail_next("get_invoice", ProviderUnavailable(503))
        return record

    monkeypatch.setattr(fake, "mark_invoice_paid_cash_or_check", mark_then_fail_reread)
    outcome = pay(charge, "1500.00")
    assert outcome.provider is not None and outcome.provider.action is MarkAction.FAILED
    assert outcome.provider.message.startswith(
        "Marked paid in Stax but not confirmed: Unavailable."
    )
    charge.refresh_from_db()
    assert charge.review_message == (
        "Stax invoice marked paid but not re-read: ProviderUnavailable"
    )
    assert local_invoice(charge).provider_status == "FAKE-SENT"
    fake.calls.clear()

    later(monkeypatch, 61)
    done = rent_run.retry_provider_mark(charge.pk, actor=None)
    assert done.action is MarkAction.MARKED
    assert call_names(fake) == ["get_invoice"]
    charge.refresh_from_db()
    assert (charge.needs_review, charge.review_message) == (False, "")
    assert local_invoice(charge).provider_status == "PAID"
    assert not AuditLog.objects.filter(action=rent_run.ALREADY_PAID_ACTION).exists()
    confirmed = AuditLog.objects.filter(action=rent_run.MARKED_ACTION).get()
    assert confirmed.after is not None and confirmed.after["confirmed_earlier_mark"] is True


def test_a_failed_first_fetch_makes_no_mark(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    fake.fail_next("get_invoice", ProviderUnavailable(503))
    outcome = pay(charge, "1500.00")
    assert outcome.provider is not None and outcome.provider.action is MarkAction.FAILED
    assert call_names(fake) == ["get_invoice"]
    failed = AuditLog.objects.get(action=rent_run.MARK_FAILED_ACTION)
    assert failed.after is not None and failed.after["stage"] == rent_run.STAGE_CHECK


def test_clearing_the_flag_keeps_the_charge_flagged_for_another_recorded_reason(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    charge = invoiced(fake)
    fake.fail_next("mark_invoice_paid_cash_or_check", ProviderUnavailable(503))
    pay(charge, "1500.00")
    AuditLog.objects.create(
        action="rent_charge.disputed", entity_type="rent_charge", entity_id=str(charge.pk)
    )
    later(monkeypatch, 61)
    assert rent_run.retry_provider_mark(charge.pk, actor=None).action is MarkAction.MARKED
    charge.refresh_from_db()
    assert (charge.needs_review, charge.review_message) == (True, "")


def test_a_payment_that_differs_from_the_invoice_total_is_not_sent(fake: FakeProvider) -> None:
    charge = invoiced(fake, total=D("1499.00"))
    outcome = pay(charge, "1500.00")
    assert outcome.provider is not None and outcome.provider.action is MarkAction.NOT_UPDATED
    assert fake.calls == []
    charge.refresh_from_db()
    assert charge.review_message == offline_marking.TOTAL_DIFFERS
    assert rent_run.charge_review_message(charge) == (
        f"{rent_run.TOTAL_MISMATCH} "
        "Not marked paid in Stax: the payment differs from the invoice total."
    )


# --- Through the screens ------------------------------------------------------------------------


@pytest.fixture
def billing() -> Client:
    return client_for(Role.BILLING, "billing-marking")[0]


def form(**overrides: str) -> dict[str, str]:
    data = {
        "creation_key": str(uuid.uuid4()),
        "method": "wire",
        "amount": "1500.00",
        "paid_on": "2026-10-15",
        "reference": "REF-9",
        "note": "Wire received on the 15th.",
    }
    data.update(overrides)
    return data


@pytest.fixture
def pinned_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: DAY)


@pytest.mark.usefixtures("pinned_today")
def test_the_screens_show_the_partial_flag_without_naming_the_provider(
    billing: Client, fake: FakeProvider
) -> None:
    charge = invoiced(fake)
    detail = f"/charges/{charge.pk}/"
    response = billing.post(f"{detail}payments/new/", form(amount="500.00"))
    assert (response.status_code, response["Location"]) == (302, detail)
    page = billing.get(detail).content.decode()
    assert "Payment recorded." in page
    assert "Not marked paid in Stax: part of the amount" in page
    assert "Stax invoice" not in page and "TODO-VERIFY" not in page
    assert 'id="retry-mark"' not in page
    assert "REF-9" in page


@pytest.mark.usefixtures("pinned_today")
def test_the_retry_button_appears_after_the_lock_and_retries(
    billing: Client, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    charge = invoiced(fake)
    detail = f"/charges/{charge.pk}/"
    fake.fail_next("mark_invoice_paid_cash_or_check", ProviderUnavailable(503))
    billing.post(f"{detail}payments/new/", form())
    page = billing.get(detail).content.decode()
    assert "Not marked paid in Stax: Unavailable" in page
    assert 'id="retry-mark"' not in page and 'id="retry-wait"' in page
    viewer, _ = client_for(Role.VIEWER)
    later(monkeypatch, 61)
    assert 'id="retry-mark"' in billing.get(detail).content.decode()
    assert 'id="retry-mark"' not in viewer.get(detail).content.decode()
    assert viewer.post(f"{detail}retry-mark/").status_code == 403
    fake.calls.clear()
    response = billing.post(f"{detail}retry-mark/")
    assert (response.status_code, response["Location"]) == (302, detail)
    assert len(marks(fake)) == 1
    after = billing.get(detail).content.decode()
    assert str(rent_run.MARK_DONE) in after and "Needs review" not in after


def test_the_retry_route_refuses_get_and_csrf_less_posts(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    client, _ = client_for(Role.BILLING)
    assert client.get(f"/charges/{charge.pk}/retry-mark/").status_code == 405
    client.handler.enforce_csrf_checks = True
    assert client.post(f"/charges/{charge.pk}/retry-mark/").status_code == 403
    assert fake.calls == []


# --- The mark-paid request (adapter) -----------------------------------------------------------


@pytest.mark.usefixtures("worker")
def test_the_mark_paid_request_has_no_apply_balance_and_no_body() -> None:
    rig = make_rig(always(200, {"id": "inv-1", "status": "PAID"}))
    StaxProvider(rig.transport).mark_invoice_paid_cash_or_check(
        "inv-1",
        offline_marking.offline_payment(
            method="zelle", amount=D("1500.00"), paid_on=DAY, note="ok", reference="Z-1"
        ),
    )
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("POST", "/invoice/inv-1/pay/cash")
    assert dict(request.url.params) == {
        "email_receipt": "0",
        "check_number": "Z-1",
        "payment_note": "Paid by Zelle on 10/15/2026. ok",
    }
    assert request.content == b""


@pytest.mark.parametrize(
    "payment",
    [
        OfflinePayment("cash", D("1.00"), check_number="x" * 26),
        OfflinePayment("cash", D("1.00"), check_number="12 34"),
        OfflinePayment("cash", D("1.00"), check_number="12/34"),
        OfflinePayment("cash", D("1.00"), note="n" * 256),
        OfflinePayment("cash", D("1.00"), note="line\nbreak"),
        OfflinePayment("cash", D("0.00")),
        OfflinePayment("giftcard", D("1.00")),  # type: ignore[arg-type]
    ],
)
@pytest.mark.usefixtures("worker")
def test_bad_mark_paid_requests_are_refused_before_sending(payment: OfflinePayment) -> None:
    rig = make_rig(always(200, {"id": "inv-1"}))
    with pytest.raises(ValidationFailed):
        StaxProvider(rig.transport).mark_invoice_paid_cash_or_check("inv-1", payment)
    assert rig.recorder.requests == []


@pytest.mark.usefixtures("worker")
def test_the_limits_themselves_are_accepted() -> None:
    rig = make_rig(always(200, {"id": "inv-1"}))
    StaxProvider(rig.transport).mark_invoice_paid_cash_or_check(
        "inv-1", OfflinePayment("check", D("1.00"), check_number="A" * 25, note="n" * 255)
    )
    [request] = rig.recorder.requests
    assert request.url.params["check_number"] == "A" * 25
    assert len(request.url.params["payment_note"]) == 255


def test_the_screen_text_names_the_processor_only_by_its_display_name() -> None:
    with wording.reading_from(lambda: "PayCo"):
        for stored in (
            offline_marking.PARTIAL_AMOUNT,
            offline_marking.TOTAL_DIFFERS,
            offline_marking.ALREADY_PAID,
            offline_marking.failure_text(offline_marking.MARK_FAILED, "ProviderRejected", "a,b"),
            offline_marking.failure_text(offline_marking.REREAD_FAILED, "TransportError", ""),
        ):
            assert "Stax" in stored
            shown = offline_marking.shown(stored)
            assert "PayCo" in shown
            assert "stax" not in shown.lower() and "TODO" not in shown
            assert "ProviderRejected" not in shown and "TransportError" not in shown
            assert "fields" not in shown


# --- D5: carried balance -----------------------------------------------------------------------


def _charge_on(row: Lease, day: date) -> RentCharge:
    rent_run.create_charges(day, actor=None)
    return RentCharge.objects.get(lease=row, period_month=day.replace(day=1))


def test_the_carried_balance_sums_earlier_open_balances_and_never_changes_the_amount() -> None:
    row = lease(started_on=date(2026, 7, 1))
    august = _charge_on(row, date(2026, 8, 15))
    september = _charge_on(row, date(2026, 9, 15))
    RentCharge.objects.filter(pk=august.pk).update(
        amount_paid=D("1300.00"), status=RentChargeStatus.PARTIAL
    )
    RentCharge.objects.filter(pk=september.pk).update(
        amount_paid=D("1000.00"), status=RentChargeStatus.PARTIAL
    )
    july = _charge_on(row, date(2026, 7, 15))
    RentCharge.objects.filter(pk=july.pk).update(status=RentChargeStatus.WAIVED)
    other = lease()  # no earlier charge

    run = rent_run.leases_due(DAY)
    carried = {item.lease.pk: item.carried_balance for item in run.due}
    assert carried[row.pk] == D("700.00")
    assert carried[other.pk] == D("0.00")
    rent_run.create_charges(DAY, actor=None)
    october = charge_of(row)
    assert october.amount_due == RENT
    assert rent_run.carried_balance(october) == D("700.00")
    assert rent_run.carried_balance(september) == D("200.00")


def test_a_second_debit_carries_the_first_debit_of_the_same_month() -> None:
    row = lease(debit_day=1, second_debit_day=15)
    rent_run.create_charges(date(2026, 10, 1), actor=None)
    first = RentCharge.objects.get(lease=row, seq=1)
    RentCharge.objects.filter(pk=first.pk).update(amount_paid=D("100.00"))
    [item] = rent_run.leases_due(DAY).due
    assert (item.seq, item.carried_balance) == (2, first.amount_due - D("100.00"))


@pytest.mark.usefixtures("pinned_today")
def test_the_run_and_charge_pages_show_the_carried_balance(billing: Client) -> None:
    row = lease(started_on=date(2026, 9, 1))
    september = _charge_on(row, date(2026, 9, 15))
    RentCharge.objects.filter(pk=september.pk).update(
        amount_paid=D("1000.00"), status=RentChargeStatus.PARTIAL
    )
    page = billing.get("/run/").content.decode()
    assert (
        '<td class="money carried-balance" data-role="meta" data-label="Carried balance">'
        "$500.00</td>" in page
    )
    rent_run.create_charges(DAY, actor=None)
    detail = billing.get(f"/charges/{charge_of(row).pk}/").content.decode()
    assert '<dd id="carried-balance">$500.00' in detail
    assert '<dd id="amount-due">$1,500.00</dd>' in detail
