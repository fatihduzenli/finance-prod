"""Adversarial cases for PLAN-012e-2 Batch A (the builder's "How to collect"), by the QA owner
(plan §6). A hostile ``?collect=`` or ``?item=`` falls back to the draft choice and echoes
nothing; a viewer collects nothing; a second ``record`` or ``schedule`` on the same draft writes
nothing more; a card number in the payment reference, a future date or a wrong method is refused
with nothing written; the redirects never carry user text into the query string; the old
templates are gone."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import AuditLog, InvoiceDraft, OneOffCharge, Payment
from ledger.services import invoice_builder
from ledger.services.invoice_builder import PaymentInput
from payments.fake import FakeProvider
from payments.models import Job, JobKind
from tests import test_invoice_builder as builder_tests
from tests.test_invoice_builder import TODAY, a_draft, a_world, line, user
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
LUHN_CARD = "4111 1111 1111 1111"
# The builder file's fixtures, re-exported so pytest finds them here.
fixed_now = builder_tests.fixed_now
fake = builder_tests.fake
worker = builder_tests.worker


def _payment(**overrides: object) -> PaymentInput:
    values: dict[str, object] = {
        "method": "zelle",
        "paid_on": TODAY,
        "reference": "ZL-4411",
        "note": "Paid at the counter.",
    }
    values.update(overrides)
    return PaymentInput(**values)  # type: ignore[arg-type]


def _snapshot() -> tuple[int, int, int, int, int]:
    return (
        OneOffCharge.objects.count(),
        Payment.objects.count(),
        Job.objects.count(),
        AuditLog.objects.count(),
        InvoiceDraft.objects.count(),
    )


# --- hostile query strings --------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        {"collect": MARKUP},
        {"collect": "record'; DROP TABLE x; --"},
        {"collect": "x" * 3000},
        {"item": "../../etc/passwd"},
        {"item": MARKUP},
        {"collect": "schedule", "item": "<img src=x onerror=1>"},
    ],
)
def test_a_hostile_collect_or_item_falls_back_and_echoes_nothing(
    fake: FakeProvider, query: dict[str, str]
) -> None:
    operator, lease = a_world(fake)
    billing, _ = client_for(Role.BILLING)
    response = billing.get(
        reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id}), query
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert MARKUP not in body and "onerror" not in body and "etc/passwd" not in body
    assert "DROP TABLE" not in body and "x" * 200 not in body
    # The draft choice is the one checked unless a real choice was named.
    checked = [
        part.split('value="')[1].split('"')[0]
        for part in body.split("<input")
        if 'name="collect"' in part and "checked" in part
    ]
    assert checked in ([], ["draft"], ["schedule"]), checked


# --- a viewer collects nothing ----------------------------------------------------------------


@pytest.mark.parametrize("choice", ["email", "record", "schedule", "charge_now", "draft"])
def test_a_viewer_cannot_collect_a_draft_in_any_way(fake: FakeProvider, choice: str) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line()])
    before = _snapshot()
    outcome = invoice_builder.collect(
        draft.pk,
        choice,
        actor=user(Role.VIEWER),
        today=TODAY,
        due_date=TODAY + timedelta(days=7),
        payment=_payment(),
        confirmed=True,
    )
    assert not outcome.ok
    assert MARKUP not in outcome.message
    assert _snapshot() == before
    assert InvoiceDraft.objects.get(pk=draft.pk).status == "draft"


# --- a second record or schedule writes nothing more -----------------------------------------


def test_a_second_record_on_the_same_draft_writes_nothing_more(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Sign repair", "100.00"), line("Keys", "20.00")])
    first = invoice_builder.collect(
        draft.pk, "record", actor=user(Role.BILLING), today=TODAY, payment=_payment()
    )
    assert first.ok, first.message
    after_first = _snapshot()
    assert Payment.objects.count() == 2  # one payment per line, one allocation group
    assert len({payment.allocation_group for payment in Payment.objects.all()}) == 1
    second = invoice_builder.collect(
        draft.pk, "record", actor=user(Role.BILLING), today=TODAY, payment=_payment()
    )
    assert not second.ok
    assert _snapshot() == after_first
    # Every charge is paid exactly once: the sum of the payments is the invoice total.
    assert sum((p.amount for p in Payment.objects.all()), Decimal("0")) == Decimal("120.00")


def test_a_second_schedule_on_the_same_draft_makes_one_schedule(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Sign repair", "100.00")])
    due = TODAY + timedelta(days=10)
    first = invoice_builder.collect(
        draft.pk, "schedule", actor=user(Role.BILLING), today=TODAY, due_date=due, confirmed=True
    )
    assert first.ok, first.message
    after_first = _snapshot()
    second = invoice_builder.collect(
        draft.pk, "schedule", actor=user(Role.BILLING), today=TODAY, due_date=due, confirmed=True
    )
    assert not second.ok
    assert _snapshot() == after_first
    assert Job.objects.filter(kind=JobKind.SCHEDULE_CREATE).count() == 1
    assert OneOffCharge.objects.filter(collection="autopay_due").count() == 1


# --- the record path's refusals write nothing -------------------------------------------------


@pytest.mark.parametrize(
    "payment",
    [
        _payment(reference=LUHN_CARD),
        _payment(note=f"Paid with card {LUHN_CARD}"),
        _payment(paid_on=TODAY + timedelta(days=1)),
        _payment(method="ach"),
        _payment(method="credit"),
        _payment(method=MARKUP),
        _payment(method=""),
        _payment(note=""),
    ],
)
def test_a_refused_record_writes_nothing(fake: FakeProvider, payment: PaymentInput) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Sign repair", "100.00")])
    before = _snapshot()
    outcome = invoice_builder.collect(
        draft.pk, "record", actor=user(Role.BILLING), today=TODAY, payment=payment
    )
    assert not outcome.ok, payment
    assert LUHN_CARD not in outcome.message and "4111" not in outcome.message
    assert MARKUP not in outcome.message
    assert _snapshot() == before
    assert InvoiceDraft.objects.get(pk=draft.pk).status == "draft"


def test_a_schedule_in_the_past_today_or_far_away_writes_nothing(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Sign repair", "100.00")])
    before = _snapshot()
    for due in (TODAY, TODAY - timedelta(days=1), TODAY + timedelta(days=400), date(1999, 1, 1)):
        outcome = invoice_builder.collect(
            draft.pk,
            "schedule",
            actor=user(Role.BILLING),
            today=TODAY,
            due_date=due,
            confirmed=True,
        )
        assert not outcome.ok, due
        assert _snapshot() == before, due


# --- the redirects carry no user text ---------------------------------------------------------


def test_the_redirects_carry_only_known_keys_and_refuse_a_mismatch(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    billing, _ = client_for(Role.BILLING)
    admin, _ = client_for(Role.ADMIN)
    for client, name, kwargs in (
        (billing, "ledger:stores:schedule_payment", {"pk": lease.store_id}),
        (admin, "ledger:stores:charge_now_pick", {"pk": lease.store_id}),
        (billing, "ledger:leases:add_setup_charge", {"pk": lease.pk}),
    ):
        response = client.get(reverse(name, kwargs=kwargs), {"store": MARKUP, "next": MARKUP})
        assert response.status_code in (302, 404), name
        if response.status_code == 302:
            location = response["Location"]
            assert "<" not in location and "script" not in location, location
            query = location.split("?", 1)[1] if "?" in location else ""
            keys = {part.split("=")[0] for part in query.split("&") if part}
            assert keys <= {"store", "lease", "collect", "item", "next"}, keys
    # A viewer gets a refusal, not a redirect, on every one of them.
    viewer, _ = client_for(Role.VIEWER)
    for name, kwargs in (
        ("ledger:stores:schedule_payment", {"pk": lease.store_id}),
        ("ledger:stores:charge_now_pick", {"pk": lease.store_id}),
        ("ledger:leases:add_setup_charge", {"pk": lease.pk}),
    ):
        assert viewer.get(reverse(name, kwargs=kwargs)).status_code == 403, name
        assert viewer.post(reverse(name, kwargs=kwargs), {}).status_code in (403, 405), name


def test_the_old_forms_are_gone() -> None:
    from django.template import TemplateDoesNotExist
    from django.template.loader import get_template

    for name in ("ledger/stores/schedule_payment.html", "ledger/stores/charge_now_pick.html"):
        with pytest.raises(TemplateDoesNotExist):
            get_template(name)


def test_two_recorded_drafts_never_share_a_payment_creation_key(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    for text in ("Sign repair", "Keys"):
        draft = a_draft(operator, lease, [line(text, "50.00")])
        outcome = invoice_builder.collect(
            draft.pk, "record", actor=user(Role.BILLING), today=TODAY, payment=_payment()
        )
        assert outcome.ok, outcome.message
    keys = [
        entry.after.get("creation_key")
        for entry in AuditLog.objects.filter(action="one_off_charge.payment_recorded")
    ]
    assert len(keys) == 2 and None not in keys and len(set(keys)) == 2
