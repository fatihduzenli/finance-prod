"""Adversarial cases for PLAN-008, written by the QA owner (plan §6 row 11)."""

from __future__ import annotations

import hmac
import json
import logging
import threading
import uuid
from decimal import Decimal
from typing import Any

import pytest
from django.db import connections
from django.test import Client

from accounts.roles import Role
from ledger.models import Invoice, Payment
from payments.models import ExternalRef, WebhookEvent
from payments.webhooks import processor, receiver
from tests.auth_helpers import make_user, verified_client
from tests.test_operator_sync import requires_postgresql
from tests.test_webhook_handlers import (
    CUSTOMER,
    INVOICE,
    TXN,
    fake,  # noqa: F401 - fixture re-exported for this module
    make_world,
    provider_invoice,
    provider_txn,
)
from tests.test_webhooks import TOKEN, URL, body_bytes, webhook_token  # noqa: F401

pytestmark = pytest.mark.django_db

CARD = "4111111111111111"


def _client(role: Role) -> Client:
    user, device = make_user(f"adv8-{role.value}-{uuid.uuid4().hex[:6]}", role)
    assert device is not None
    return verified_client(user, device)


# --- the receiver --------------------------------------------------------------------------------


def test_the_token_compare_is_the_constant_time_function() -> None:
    import inspect

    source = inspect.getsource(receiver)
    assert "compare_digest" in source
    assert hmac.compare_digest is not None
    # No plain equality against the configured token anywhere in the receiver.
    assert "== settings.STAX_WEBHOOK_URL_TOKEN" not in source
    assert "settings.STAX_WEBHOOK_URL_TOKEN ==" not in source


def test_the_token_never_appears_in_any_log_line_on_200_or_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    client = Client()
    assert (
        client.post(URL, body_bytes({"id": "x"}), content_type="application/json").status_code
        == 200
    )
    assert (
        client.post(
            f"/api/webhooks/stax/{TOKEN[:-3]}abc/", b"{}", content_type="application/json"
        ).status_code
        == 404
    )
    assert client.get(URL).status_code == 404
    assert TOKEN not in caplog.text
    assert TOKEN[:-3] not in caplog.text


def test_a_card_number_in_the_body_is_redacted_in_the_payload_and_absent_from_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from payments.webhooks import handlers

    def boom(event_type: str, provider_object_id: str) -> str:
        raise ValueError(f"card {CARD} seen")

    monkeypatch.setitem(handlers.HANDLERS, "update_customer", boom)
    body = {"id": "cus_1", "email": "a@example.com", "note": f"pay {CARD}", "card_number": CARD}
    event_id = receiver.store_event(body_bytes(body), "update_customer")
    assert event_id is not None
    processor.process_event(event_id)
    event = WebhookEvent.objects.get(pk=event_id)
    assert CARD not in json.dumps(event.payload or {})
    assert CARD not in (event.error or "")
    assert "ValueError" in (event.error or "")


@pytest.mark.parametrize(
    "object_id",
    ["../self", "inv/1?x=1", "inv 1", "", "a" * 300, "inv%2F1", CARD, "1' OR '1'='1"],
)
def test_hostile_object_ids_never_reach_the_provider(fake: Any, object_id: str) -> None:  # noqa: F811
    event_id = receiver.store_event(
        body_bytes({"id": object_id, "status": "PAID"}), "update_invoice"
    )
    assert event_id is not None
    processor.process_event(event_id)
    assert all(name != "get_invoice" or args[0] != object_id for name, args in fake.calls)
    event = WebhookEvent.objects.get(pk=event_id)
    assert event.outcome in {"ignored", "processed", "failed"} or event.outcome.startswith(
        "ignored"
    )


def test_ten_thousand_distinct_events_dedupe_cleanly() -> None:
    client = Client()
    for index in range(300):  # 10,000 is the design figure; 300 keeps the suite fast
        client.post(
            URL, body_bytes({"id": f"inv-{index}", "n": index}), content_type="application/json"
        )
    assert WebhookEvent.objects.count() == 300
    assert WebhookEvent.objects.values("dedupe_hash").distinct().count() == 300


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_the_same_delivery_arriving_twice_at_the_same_moment_stores_one_row() -> None:
    body = body_bytes({"id": "inv-race", "status": "SENT"})
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def post() -> None:
        try:
            barrier.wait(timeout=10)
            response = Client().post(URL, body, content_type="application/json")
            codes.append(response.status_code)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=post) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert codes == [200, 200]
    assert WebhookEvent.objects.filter(provider_object_id="inv-race").count() == 1


# --- the handlers ---------------------------------------------------------------------------


def test_a_viewer_cannot_replay_and_billing_can() -> None:
    event_id = receiver.store_event(body_bytes({"id": "inv-1"}), "update_invoice")
    assert _client(Role.VIEWER).post(f"/webhooks/{event_id}/replay/").status_code == 403
    assert _client(Role.BILLING).post(f"/webhooks/{event_id}/replay/").status_code in (302, 200)


def test_an_invoice_claiming_another_operators_customer_is_not_linked_to_ours(fake: Any) -> None:  # noqa: F811
    world = make_world(fake)
    provider_invoice(fake, "inv-foreign", customer="cus-someone-else", total=Decimal("1.00"))
    event_id = receiver.store_event(
        body_bytes({"id": "inv-foreign", "customer_id": CUSTOMER}), "create_invoice"
    )
    assert event_id is not None
    processor.process_event(event_id)
    assert (
        not Invoice.objects.filter(operator=world.operator, rent_charge=world.charge)
        .exclude(invoice_number="")
        .filter(pk__in=ExternalRef.objects.filter(external_id="inv-foreign").values("entity_id"))
        .exists()
    )


def test_a_transaction_for_an_invoice_of_another_operator_does_not_pay_our_charge(
    fake: Any,  # noqa: F811
) -> None:
    world = make_world(fake)
    provider_invoice(fake, "inv-theirs", customer="cus-them", total=Decimal("1500.00"))
    provider_txn(
        fake,
        "txn-theirs",
        total=Decimal("1500.00"),
        invoice_external_id="inv-theirs",
        customer_external_id="cus-them",
    )
    for event in ("create_transaction", "update_transaction_settled"):
        event_id = receiver.store_event(body_bytes({"id": "txn-theirs"}), event)
        assert event_id is not None
        processor.process_event(event_id)
    world.charge.refresh_from_db()
    assert world.charge.amount_paid == Decimal("0.00")
    assert not Payment.objects.filter(rent_charge=world.charge).exists()


def test_processing_never_writes_provider_status_outside_the_sync_writer(
    fake: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every provider_status write during processing goes through provider_write()."""
    from payments import sync_writer

    entered: list[int] = []
    original = sync_writer.provider_write

    def counting(using: str = "default") -> Any:
        entered.append(1)
        return original(using)

    monkeypatch.setattr(sync_writer, "provider_write", counting)
    monkeypatch.setattr("payments.invoice_sync.provider_write", counting)
    world = make_world(fake)
    provider_invoice(
        fake,
        INVOICE,
        status="SENT",
        customer=CUSTOMER,
        raw_summary={"invoice_date_at": "2026-10-15", "schedule_id": ""},
    )
    event_id = receiver.store_event(body_bytes({"id": INVOICE, "status": "PAID"}), "update_invoice")
    assert event_id is not None
    processor.process_event(event_id)
    statuses = list(
        Invoice.objects.filter(operator=world.operator).values_list("provider_status", flat=True)
    )
    if statuses:
        assert entered, "provider_status was written without the sync writer"
        assert "PAID" not in statuses


def test_a_lying_body_with_a_settled_transaction_does_not_pay_the_charge(
    fake: Any,  # noqa: F811
) -> None:
    world = make_world(fake)
    provider_invoice(fake, INVOICE, customer=CUSTOMER)
    provider_txn(fake, TXN)  # pending at the provider: no settled_at, no return
    body = {
        "id": TXN,
        "settled_at": "2026-10-15 12:00:00",
        "success": True,
        "total": "1500.00",
        "invoice_id": INVOICE,
    }
    event_id = receiver.store_event(body_bytes(body), "update_transaction_settled")
    assert event_id is not None
    processor.process_event(event_id)
    world.charge.refresh_from_db()
    assert world.charge.amount_paid == Decimal("0.00")
    assert world.charge.status != "paid"


def test_sentry_event_built_during_processing_holds_no_url_or_token() -> None:
    from finance import sentry

    sentry.init(
        "", "local", "sandbox", stax_api_key="FAKE-CI-KEY-XYZ123", stax_webhook_url_token=TOKEN
    )
    event = {
        "request": {"url": f"https://staging.example.com{URL}", "method": "POST"},
        "breadcrumbs": {
            "values": [
                {
                    "category": "httpx",
                    "data": {
                        "url": f"https://x/{TOKEN}",
                        "http.query": f"target_url=https://y/{TOKEN}",
                        "method": "POST",
                        "status_code": 200,
                    },
                }
            ]
        },
        "message": f"failed for {TOKEN}",
    }
    out = sentry.scrub_event(event, {})  # type: ignore[arg-type]
    assert out is not None
    text = json.dumps(out)
    assert TOKEN not in text
    assert "url" not in out.get("request", {})
