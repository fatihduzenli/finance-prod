"""Adversarial cases for PLAN-009, written by the QA owner (plan §6)."""

from __future__ import annotations

import csv
import io
import time
import uuid
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import AuditLog, Payment, PaymentMethod
from ledger.services import rent_run
from payments.errors import ProviderUnavailable
from payments.fake import FakeProvider
from tests.auth_helpers import make_user, verified_client
from tests.test_manual_payment_marking import (
    DAY,
    call_names,
    fake,  # noqa: F401
    health,  # noqa: F401
    invoiced,
    local_invoice,
    marks,
    pay,
)
from tests.test_payments_ledger import a_charge, payment

pytestmark = pytest.mark.django_db

D = Decimal
CARD = "4111111111111111"


def _streamed(response: Any) -> str:
    """The full body of a streaming response as text."""
    return b"".join(response.streaming_content).decode("utf-8")


def _client(role: Role) -> Client:
    user, device = make_user(f"adv9-{role.value}-{uuid.uuid4().hex[:6]}", role)
    assert device is not None
    return verified_client(user, device)


# --- marking in Stax --------------------------------------------------------------------


def test_a_note_with_a_card_number_never_reaches_stax_nor_the_ledger(fake: FakeProvider) -> None:  # noqa: F811
    charge = invoiced(fake)
    outcome = pay(charge, "1500.00", note=f"wire ref {CARD}")
    assert not outcome.ok
    assert Payment.objects.filter(rent_charge=charge).count() == 0
    assert marks(fake) == []


@pytest.mark.parametrize(
    "reference",
    ["REF 123 456", "a" * 60, "ref/with/slashes", "ref;drop", "éè", "   "],
)
def test_odd_references_are_reduced_before_stax_and_never_refused_locally(
    fake: FakeProvider,  # noqa: F811
    reference: str,
) -> None:
    charge = invoiced(fake)
    outcome = pay(charge, "1500.00", reference=reference)
    assert outcome.ok, outcome
    sent = marks(fake)
    assert len(sent) == 1
    check_number = sent[0].check_number
    assert len(check_number) <= 25
    assert all(c.isalnum() or c == "-" for c in check_number)


def test_a_replayed_manual_payment_marks_stax_once(fake: FakeProvider) -> None:  # noqa: F811
    charge = invoiced(fake)
    key = uuid.uuid4()
    pay(charge, "1500.00", creation_key=key)
    pay(charge, "1500.00", creation_key=key)
    pay(charge, "1500.00", creation_key=key)
    assert Payment.objects.filter(rent_charge=charge).count() == 1
    assert len(marks(fake)) == 1


def test_a_failed_reread_after_a_good_mark_does_not_mark_twice_on_retry(
    fake: FakeProvider,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    charge = invoiced(fake)
    # Let the mark succeed, then fail the re-read once.
    original_get = fake.get_invoice
    state = {"fail_next_get": False, "gets": 0}

    def flaky_get(external_id: str) -> Any:
        state["gets"] += 1
        if state["gets"] == 2:  # the re-read after the mark
            raise ProviderUnavailable(503)
        return original_get(external_id)

    monkeypatch.setattr(fake, "get_invoice", flaky_get)
    outcome = pay(charge, "1500.00")
    assert outcome.ok  # the local payment stands
    assert len(marks(fake)) == 1
    charge.refresh_from_db()
    assert charge.needs_review is True
    # Retry after the lock: the pre-check now sees PAID, so no second mark.
    from tests.test_manual_payment_marking import later

    later(monkeypatch, 61)
    rent_run.retry_provider_mark(charge.pk, actor=None)
    assert len(marks(fake)) == 1
    assert local_invoice(charge).provider_status.upper() == "PAID"


# --- the ledger export ------------------------------------------------------------------


def test_export_round_trips_notes_with_commas_quotes_and_newlines() -> None:
    charge = a_charge()
    tricky = 'wire from "ACME, Inc."\nsecond line, with comma'
    payment(charge, "100.00", method=PaymentMethod.WIRE, note=tricky)
    payment(charge, "50.00", method=PaymentMethod.WIRE, note="=HYPERLINK(evil)")
    response = _client(Role.VIEWER).get("/payments/export.csv")
    assert response.status_code == 200
    text = _streamed(response)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 2
    notes = [row["note"] for row in rows]
    assert tricky in notes
    assert any(note.startswith("'=") for note in notes)  # formula injection neutralised
    assert all(CARD not in value for row in rows for value in row.values())


def test_export_of_many_payments_is_fast_and_complete() -> None:
    charge = a_charge()
    Payment.objects.bulk_create(
        [
            Payment(
                rent_charge=charge,
                kind="rent",
                method=PaymentMethod.WIRE,
                amount=D("1.00"),
                status="settled",
                initiated_on=DAY,
                settled_on=DAY,
                note="bulk",
            )
            for _ in range(5000)
        ]
    )
    started = time.monotonic()
    response = _client(Role.BILLING).get("/payments/export.csv")
    text = _streamed(response)
    elapsed = time.monotonic() - started
    assert text.count("\n") >= 5000
    assert elapsed < 10.0, elapsed  # generous bound for a laptop on sqlite


def test_export_contains_no_unmasked_provider_identifier() -> None:
    charge = a_charge()
    row = payment(charge, "100.00")
    from payments.models import ExternalRef

    ExternalRef.objects.create(
        entity_type="payment",
        entity_id=row.pk,
        provider="stax_pay",
        external_kind="transaction",
        external_id="txn-0123456789abcdef",
    )
    text = _streamed(_client(Role.VIEWER).get("/payments/export.csv"))
    assert "txn-0123456789abcdef" not in text


# --- refunds and voids ------------------------------------------------------------------


def test_every_refund_and_void_route_is_closed_to_viewer_and_billing() -> None:
    charge = a_charge()
    row = payment(charge, "100.00")
    for role in (Role.VIEWER, Role.BILLING):
        client = _client(role)
        for path in (f"/payments/{row.pk}/refund/", f"/payments/{row.pk}/void/"):
            assert client.get(path).status_code == 403, (role, path)
            assert client.post(path, {"amount": "1.00", "confirm": "true"}).status_code == 403
        page = client.get(f"/payments/{row.pk}/").content.decode()
        assert "/refund/" not in page and "/void/" not in page


@pytest.mark.parametrize("amount", ["100.001", "-1.00", "100.01", "0", "abc", "1e3"])
def test_bad_refund_amounts_make_no_call(fake: FakeProvider, amount: str) -> None:  # noqa: F811
    charge = a_charge()
    row = payment(charge, "100.00")
    from payments.models import ExternalRef

    ExternalRef.objects.create(
        entity_type="payment",
        entity_id=row.pk,
        provider="stax_pay",
        external_kind="transaction",
        external_id="txn-ok",
    )
    admin = _client(Role.ADMIN)
    page = admin.get(f"/payments/{row.pk}/refund/").content.decode()
    import re

    key = re.search(r'name="creation_key" value="([^"]+)"', page)
    data = {"amount": amount, "creation_key": key.group(1) if key else str(uuid.uuid4())}
    for field in ("confirm", "confirm_refund", "confirmed"):
        data[field] = "true"
    admin.post(f"/payments/{row.pk}/refund/", data)
    assert "refund_transaction" not in call_names(fake)
    assert Payment.objects.filter(parent_payment=row).count() == 0


def test_a_refund_equal_to_the_remaining_amount_then_one_more_cent_is_refused(
    fake: FakeProvider,  # noqa: F811
) -> None:
    from ledger.services import refunds

    charge = a_charge()
    row = payment(charge, "100.00")
    from payments.models import ExternalRef

    ExternalRef.objects.create(
        entity_type="payment",
        entity_id=row.pk,
        provider="stax_pay",
        external_kind="transaction",
        external_id="txn-ok",
    )
    fake.transactions["txn-ok"] = fake.transactions.get("txn-ok") or __import__(
        "tests.test_webhook_handlers", fromlist=["provider_txn"]
    ).provider_txn(fake, "txn-ok", total=D("100.00"))
    user, _ = make_user("adv9-admin-x", Role.ADMIN)
    first = refunds.refund_payment(
        row.pk, amount=D("100.00"), confirmed=True, actor=user, creation_key=uuid.uuid4()
    )
    assert first.ok, first
    second = refunds.refund_payment(
        row.pk, amount=D("0.01"), confirmed=True, actor=user, creation_key=uuid.uuid4()
    )
    assert not second.ok
    assert call_names(fake).count("refund_transaction") == 1
    assert AuditLog.objects.filter(action="payment.refunded").count() == 1
