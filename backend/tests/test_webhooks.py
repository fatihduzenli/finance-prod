"""Webhook receiver, storage, worker, processor framework and screens
(docs/qa/PLAN-008-webhooks.md Batch A; §6 rows 1 (dedupe), 2, 6 (storage), 7 and 9).

PLAN-010 D2: the in-process queue and its thread are gone; the receiver only stores,
and the ``worker`` command takes each stored event from the table.

Every handler is replaced by :data:`PLACEHOLDER` (no call, no write) unless a test
installs its own in ``payments.webhooks.handlers.HANDLERS``; the real handlers are
tested in ``test_webhook_handlers.py``. No test reaches the network: the one test that
goes through the real adapter uses an ``httpx.MockTransport`` and a fake clock.
"""

from __future__ import annotations

import ast
import hmac
import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.contrib.admin.sites import site as admin_site
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

import payments
from accounts import registry
from accounts.roles import Role
from payments import alerts
from payments.errors import ProviderRejected, ProviderUnavailable
from payments.management.commands import worker
from payments.models import (
    WebhookEvent,
    WebhookEventSource,
    WebhookObjectIdSource,
    WebhookOutcome,
)
from payments.redaction import redact
from payments.stax.adapter import StaxProvider
from payments.webhooks import handlers, processor, receiver
from payments.webhooks.events import EVENT_NAMES, SHAPE_TYPES, UNKNOWN_EVENT, ignored
from tests.auth_helpers import iter_routes
from tests.job_helpers import run_queued_jobs
from tests.stax_helpers import Rig, always, make_rig
from tests.test_operator_views import client_for

TOKEN = "t" * 64
URL = f"/api/webhooks/stax/{TOKEN}/"
CARD = "4111111111111111"
INVOICE_BODY = {"id": "inv-0001", "status": "SENT", "total": "125.00", "invoice_date_at": "x"}
WEBHOOKS_DIR = Path(__file__).resolve().parents[1] / "payments" / "webhooks"
PLACEHOLDER = ignored("test placeholder")


def placeholder(event_type: str, provider_object_id: str) -> str:
    """Stands in for every handler here: no call, no write."""
    return PLACEHOLDER


@pytest.fixture(autouse=True)
def placeholder_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(handlers.HANDLERS):
        monkeypatch.setitem(handlers.HANDLERS, name, placeholder)


@pytest.fixture(autouse=True)
def webhook_token(settings: Any) -> str:
    settings.STAX_WEBHOOK_URL_TOKEN = TOKEN
    return TOKEN


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def capture(message: str, **kwargs: Any) -> None:
        captured.append({"message": message, **kwargs})

    monkeypatch.setattr("sentry_sdk.capture_message", capture)
    return captured


def body_bytes(body: object) -> bytes:
    return json.dumps(body).encode()


def deliver(
    client: Client, body: object | bytes, *, event: str | None = None, url: str = URL
) -> Any:
    raw = body if isinstance(body, bytes) else body_bytes(body)
    headers = {"stax-event-name": event} if event is not None else {}
    return client.post(url, raw, content_type="application/json", headers=headers)


def stored(body: object | bytes, event: str | None = "update_invoice") -> WebhookEvent:
    raw = body if isinstance(body, bytes) else body_bytes(body)
    event_id = receiver.store_event(raw, event)
    assert event_id is not None
    return WebhookEvent.objects.get(pk=event_id)


def install(monkeypatch: pytest.MonkeyPatch, event_type: str, handler: handlers.Handler) -> None:
    monkeypatch.setitem(handlers.HANDLERS, event_type, handler)


def failing(error: Exception) -> handlers.Handler:
    def handler(event_type: str, provider_object_id: str) -> str:
        raise error

    return handler


# --- Row 2: the receiver refuses everything but a valid POST ----------------------------

REFUSED_PATHS = {
    "wrong": "/api/webhooks/stax/" + "u" * 64 + "/",
    "missing": "/api/webhooks/stax/",
    "missing, double slash": "/api/webhooks/stax//",
    "truncated": f"/api/webhooks/stax/{TOKEN[:-1]}/",
    "over-long": f"/api/webhooks/stax/{TOKEN}t/",
    "case-changed": f"/api/webhooks/stax/{TOKEN.upper()}/",
    "no trailing slash": f"/api/webhooks/stax/{TOKEN}",
    "two trailing slashes": f"/api/webhooks/stax/{TOKEN}//",
    "extra segment": f"/api/webhooks/stax/{TOKEN}/x/",
    "trailing line break": f"/api/webhooks/stax/{TOKEN}/%0A",
}


@pytest.mark.django_db
@pytest.mark.parametrize("path", REFUSED_PATHS.values(), ids=REFUSED_PATHS.keys())
def test_a_wrong_token_gets_an_empty_404_and_writes_nothing(
    client: Client, path: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        response = deliver(client, INVOICE_BODY, event="update_invoice", url=path)
    assert response.status_code == 404
    assert response.content == b""
    assert not WebhookEvent.objects.exists()
    attempted = path.removeprefix("/api/webhooks/stax/").strip("/")
    for record in caplog.records:
        line = record.getMessage()
        assert TOKEN not in line and (not attempted or attempted not in line), line
    assert [record.name for record in caplog.records] == ["payments.webhooks.receiver"]
    assert "path=/api/webhooks/stax/[token]/" in caplog.records[0].getMessage()


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "put", "patch", "delete", "head", "options"])
def test_any_method_but_post_gets_an_empty_404(client: Client, method: str) -> None:
    response = getattr(client, method)(URL)
    assert response.status_code == 404
    assert response.content == b""
    assert not WebhookEvent.objects.exists()


@pytest.mark.django_db
def test_an_unconfigured_token_refuses_everything(client: Client, settings: Any) -> None:
    settings.STAX_WEBHOOK_URL_TOKEN = ""
    for path in ("/api/webhooks/stax/", "/api/webhooks/stax//", URL):
        response = deliver(client, INVOICE_BODY, url=path)
        assert response.status_code == 404, path
        assert response.content == b""
    assert not WebhookEvent.objects.exists()


@pytest.mark.django_db
def test_an_oversize_body_gets_404_and_writes_nothing(client: Client) -> None:
    response = deliver(client, b" " * (receiver.MAX_BODY_BYTES + 1))
    assert response.status_code == 404
    assert response.content == b""
    assert not WebhookEvent.objects.exists()


@pytest.mark.django_db
def test_a_body_of_exactly_the_limit_is_accepted(client: Client) -> None:
    response = deliver(client, b" " * receiver.MAX_BODY_BYTES)
    assert response.status_code == 200
    assert WebhookEvent.objects.count() == 1


@pytest.mark.django_db
def test_the_token_is_compared_in_constant_time(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr("hmac.compare_digest", spy)
    assert deliver(client, INVOICE_BODY, url=REFUSED_PATHS["truncated"]).status_code == 404
    assert deliver(client, INVOICE_BODY).status_code == 200
    assert len(calls) == 2


@pytest.mark.django_db
def test_a_valid_delivery_needs_no_session_or_csrf_token(
    django_capture_on_commit_callbacks: Any,
) -> None:
    client = Client(enforce_csrf_checks=True)
    with django_capture_on_commit_callbacks() as callbacks:
        response = deliver(client, INVOICE_BODY, event="update_invoice")
    assert response.status_code == 200
    assert response.content == b""
    event = WebhookEvent.objects.get()
    assert event.outcome == WebhookOutcome.RECEIVED
    assert event.processed_at is None
    # PLAN-010 D2: nothing is handed to an in-process queue; the worker reads the table.
    assert callbacks == []


# --- Row 1 (dedupe): byte-identical redeliveries ------------------------------------------


@pytest.mark.django_db
def test_five_identical_deliveries_store_one_row_and_count_four_duplicates(
    client: Client,
) -> None:
    responses = [deliver(client, INVOICE_BODY, event="update_invoice") for _ in range(5)]
    assert [response.status_code for response in responses] == [200] * 5
    event = WebhookEvent.objects.get()
    assert event.duplicate_count == 4


@pytest.mark.django_db
def test_a_duplicate_leaves_the_first_rows_outcome(
    client: Client, django_capture_on_commit_callbacks: Any
) -> None:
    deliver(client, INVOICE_BODY, event="update_invoice")
    event = WebhookEvent.objects.get()
    assert processor.process_event(event.pk) == PLACEHOLDER
    with django_capture_on_commit_callbacks() as callbacks:
        assert deliver(client, INVOICE_BODY, event="update_invoice").status_code == 200
    event.refresh_from_db()
    assert event.outcome == PLACEHOLDER
    assert event.duplicate_count == 1
    assert callbacks == []  # nothing queued for a duplicate


@pytest.mark.django_db
def test_the_dedupe_hash_covers_the_event_type_and_every_byte() -> None:
    first = stored(INVOICE_BODY, "update_invoice")
    assert first.dedupe_hash == receiver.dedupe_hash("update_invoice", body_bytes(INVOICE_BODY))
    stored(INVOICE_BODY, "send_invoice")
    stored(body_bytes(INVOICE_BODY) + b" ", "update_invoice")
    assert WebhookEvent.objects.count() == 3


# --- Row 6 (storage): event source, non-JSON, unknown type, redaction ---------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("header", "body", "event_type", "source"),
    [
        ("update_invoice", {"id": "a"}, "update_invoice", WebhookEventSource.HEADER),
        (None, {"event": "update_customer", "id": "a"}, "update_customer", "body"),
        (None, {"type": "create_transaction"}, "create_transaction", "body"),
        (None, {"event_name": "create_deposit"}, "create_deposit", "body"),
        (None, {"invoice_date_at": "2026-10-01", "total": 1}, "invoice", "shape"),
        (None, {"rule": "FREQ=MONTHLY", "total": 1}, "schedule", "shape"),
        (None, {"firstname": "Pat"}, "customer", "shape"),
        (None, {"email": "pat@example.com"}, "customer", "shape"),
        # A transaction's own "type" is not an event name; its shape names it.
        (None, {"type": "charge", "total": 1, "method": "bank"}, "transaction", "shape"),
        (None, {"batched_at": "2026-10-01"}, "deposit", "shape"),
        (None, {"something": "else"}, "unknown", "unknown"),
        (None, ["a", "list"], "unknown", "unknown"),
        ("not a name!", {"id": "a"}, "unknown", "unknown"),
        (CARD, {"id": "a"}, "unknown", "unknown"),
    ],
)
def test_the_event_type_and_its_source_are_recorded(
    header: str | None, body: object, event_type: str, source: str
) -> None:
    event = stored(body, header)
    assert (event.event_type, event.event_source) == (event_type, source)


@pytest.mark.django_db
def test_a_non_json_body_is_stored_and_ignored() -> None:
    event = stored(b"this is not json", None)
    assert event.event_type == "unknown"
    assert event.event_source == WebhookEventSource.UNKNOWN
    assert event.payload is None
    assert event.provider_object_id == ""
    assert processor.process_event(event.pk) == ignored(UNKNOWN_EVENT)
    event.refresh_from_db()
    assert event.processed_at is not None


@pytest.mark.django_db
def test_an_unknown_event_type_is_stored_and_ignored() -> None:
    event = stored({"id": "obj-1"}, "rename_the_moon")
    assert event.event_type == "rename_the_moon"
    assert processor.process_event(event.pk) == ignored(UNKNOWN_EVENT)


def test_every_known_type_has_a_handler() -> None:
    assert set(handlers.HANDLERS) == {*EVENT_NAMES, *SHAPE_TYPES}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("inv-0001", "inv-0001"),
        ("0f8fad5b-d9cb-469f-a165-70867728950e", "0f8fad5b-d9cb-469f-a165-70867728950e"),
        (12345, "12345"),
        ("../../etc/passwd", ""),
        ("a/b", ""),
        ("a b", ""),
        (CARD, ""),
        (True, ""),
        ({"nested": 1}, ""),
        ("x" * 256, ""),
    ],
)
def test_only_a_plain_identifier_becomes_the_object_id(value: object, expected: str) -> None:
    assert stored({"id": value}).provider_object_id == expected


# --- PLAN-008a row 1: the object ID of an envelope, and where it was found ----------------

SEND_INVOICE_ENVELOPE = {
    "toEmail": "ops@example.com",
    "toName": "Operator",
    "ccEmails": [],
    "bccEmails": [],
    "mergeVars": {
        "user": {"name": "Staff"},
        "invoice": {"id": "inv-send-0001", "url": "https://example.com/i", "meta": {}},
    },
}


@pytest.mark.django_db
def test_a_send_invoice_envelope_takes_its_object_id_from_merge_vars_invoice_id() -> None:
    event = stored(SEND_INVOICE_ENVELOPE, "send_invoice")
    assert event.event_type == "send_invoice"
    assert event.provider_object_id == "inv-send-0001"
    assert event.object_id_source == "mergeVars.invoice.id"


@pytest.mark.django_db
def test_a_send_invoice_envelope_delivered_over_http_records_the_source(client: Client) -> None:
    assert deliver(client, SEND_INVOICE_ENVELOPE, event="send_invoice").status_code == 200
    event = WebhookEvent.objects.get()
    assert (event.provider_object_id, event.object_id_source) == (
        "inv-send-0001",
        "mergeVars.invoice.id",
    )


@pytest.mark.django_db
def test_a_top_level_id_wins_over_a_nested_one() -> None:
    body = {**SEND_INVOICE_ENVELOPE, "id": "inv-top-0001"}
    event = stored(body, "send_invoice")
    assert (event.provider_object_id, event.object_id_source) == ("inv-top-0001", "id")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("body", "expected", "source"),
    [
        ({"mergeVars": {"invoice": {"id": 42}}}, "42", "mergeVars.invoice.id"),
        ({"invoice": {"id": "inv-a"}}, "inv-a", "invoice.id"),
        ({"transaction": {"id": "txn-a"}}, "txn-a", "transaction.id"),
        ({"data": {"id": "obj-a"}}, "obj-a", "data.id"),
        ({"batch_id": "batch-a"}, "batch-a", "batch_id"),
        # The order of the paths: mergeVars before invoice before transaction ...
        (
            {"invoice": {"id": "inv-b"}, "mergeVars": {"invoice": {"id": "inv-m"}}},
            "inv-m",
            "mergeVars.invoice.id",
        ),
        ({"transaction": {"id": "txn-b"}, "invoice": {"id": "inv-c"}}, "inv-c", "invoice.id"),
        ({"data": {"id": "d"}, "batch_id": "b-1"}, "d", "data.id"),
        # Nothing usable anywhere.
        ({"mergeVars": {"invoice": "inv-not-an-object"}}, "", ""),
        ({"mergeVars": None}, "", ""),
        ({"toEmail": "ops@example.com"}, "", ""),
    ],
)
def test_the_object_id_paths_are_read_in_order(body: object, expected: str, source: str) -> None:
    event = stored(body, "send_invoice")
    assert (event.provider_object_id, event.object_id_source) == (expected, source)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value", ["../../etc/passwd", "a/b", "a b", CARD, True, {"nested": 1}, ["x"], "x" * 256]
)
def test_a_nested_value_that_is_not_a_plain_identifier_is_rejected(value: object) -> None:
    body = {"mergeVars": {"invoice": {"id": value}}, "invoice": {"id": "inv-fallback"}}
    event = stored(body, "send_invoice")
    # The first path holding a value decides; a bad value is not replaced by a later one.
    assert (event.provider_object_id, event.object_id_source) == ("", "")


# --- PLAN-009a: an event stored without an object ID gets it from its stored payload -------


def _seen_ids(monkeypatch: pytest.MonkeyPatch, event_type: str) -> list[str]:
    """Install a handler that records the object ID it is given."""
    seen: list[str] = []

    def handler(event_type: str, provider_object_id: str) -> str:
        seen.append(provider_object_id)
        if not provider_object_id:
            return ignored("no object id")
        return WebhookOutcome.PROCESSED.value

    install(monkeypatch, event_type, handler)
    return seen


def _as_stored_before_plan_008a(event: WebhookEvent, outcome: str) -> WebhookEvent:
    WebhookEvent.objects.filter(pk=event.pk).update(
        provider_object_id="",
        object_id_source=WebhookObjectIdSource.NONE,
        outcome=outcome,
        processed_at=timezone.now() if outcome != WebhookOutcome.RECEIVED else None,
    )
    event.refresh_from_db()
    return event


@pytest.mark.django_db
def test_a_replay_reads_the_object_id_from_the_stored_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _seen_ids(monkeypatch, "send_invoice")
    event = _as_stored_before_plan_008a(
        stored(SEND_INVOICE_ENVELOPE, "send_invoice"), ignored("no object id")
    )
    assert event.payload["mergeVars"]["invoice"]["id"] == "inv-send-0001"

    assert processor.process_event(event.pk, replay=True) == WebhookOutcome.PROCESSED
    event.refresh_from_db()
    assert seen == ["inv-send-0001"]
    assert event.provider_object_id == "inv-send-0001"
    assert event.object_id_source == "mergeVars.invoice.id"
    assert event.outcome == WebhookOutcome.PROCESSED


@pytest.mark.django_db
def test_process_webhooks_reads_the_object_id_of_an_unprocessed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _seen_ids(monkeypatch, "send_invoice")
    event = _as_stored_before_plan_008a(
        stored(SEND_INVOICE_ENVELOPE, "send_invoice"), WebhookOutcome.RECEIVED
    )
    call_command("process_webhooks")
    event.refresh_from_db()
    assert seen == ["inv-send-0001"]
    assert (event.provider_object_id, event.object_id_source) == (
        "inv-send-0001",
        "mergeVars.invoice.id",
    )
    assert event.outcome == WebhookOutcome.PROCESSED


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body",
    [{"toEmail": "ops@example.com", "mergeVars": {"user": {"name": "Staff"}}}, b"not json"],
    ids=["no-id-in-payload", "no-payload"],
)
def test_a_replay_of_an_event_whose_payload_holds_no_id_stays_ignored(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    seen = _seen_ids(monkeypatch, "send_invoice")
    event = stored(body, "send_invoice")
    assert event.provider_object_id == ""
    assert processor.process_event(event.pk, replay=True) == ignored("no object id")
    event.refresh_from_db()
    assert seen == [""]
    assert (event.provider_object_id, event.object_id_source) == ("", "")


@pytest.mark.django_db
def test_a_replay_never_changes_an_object_id_already_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _seen_ids(monkeypatch, "send_invoice")
    event = stored(SEND_INVOICE_ENVELOPE, "send_invoice")
    WebhookEvent.objects.filter(pk=event.pk).update(
        provider_object_id="inv-kept", object_id_source=WebhookObjectIdSource.INVOICE_ID
    )
    assert processor.process_event(event.pk, replay=True) == WebhookOutcome.PROCESSED
    event.refresh_from_db()
    assert seen == ["inv-kept"]
    assert (event.provider_object_id, event.object_id_source) == ("inv-kept", "invoice.id")


@pytest.mark.django_db
def test_an_object_id_recovered_on_a_failed_attempt_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, "send_invoice", failing(ProviderUnavailable(503)))
    event = _as_stored_before_plan_008a(
        stored(SEND_INVOICE_ENVELOPE, "send_invoice"), ignored("no object id")
    )
    assert processor.process_event(event.pk, replay=True) == WebhookOutcome.FAILED
    event.refresh_from_db()
    assert event.provider_object_id == "inv-send-0001"
    assert event.object_id_source == "mergeVars.invoice.id"


@pytest.mark.django_db
def test_the_detail_page_shows_where_the_object_id_was_found() -> None:
    event = stored(SEND_INVOICE_ENVELOPE, "send_invoice")
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get(f"/webhooks/{event.pk}/").content.decode()
    assert '<dd id="object-id-source"><code>mergeVars.invoice.id</code></dd>' in page
    assert "inv-send-0001" not in page
    missing = stored({"toEmail": "ops@example.com", "n": 1}, "send_invoice")
    page = viewer.get(f"/webhooks/{missing.pk}/").content.decode()
    assert '<dd id="object-id-source">—</dd>' in page


@pytest.mark.django_db
def test_the_stored_payload_is_redacted_at_every_depth() -> None:
    body = {
        "id": "txn-1",
        "memo": f"card {CARD}",
        "account_number": "000123456789",
        "bank_routing": "021000021",
        "api_token": "not-a-real-token",
        "customer": {"firstname": "Pat", "notes": [f"paid with {CARD}", "fine"]},
        "payment_method": {"id": "pm-1", "nickname": "Ops", "bank_account": "x", "person": "P"},
        "child_transactions": [{"meta": {"secret_answer": "x", "achDescription": "R01"}}],
        CARD: "a key that is a card number",
    }
    # A JSON number with a fraction, written as text: the test builds no float either.
    event = stored(body_bytes(body)[:-1] + b', "total": 12.5}')
    payload = event.payload
    text = json.dumps(payload)
    assert CARD not in text
    for gone in ("account_number", "bank_routing", "api_token", "memo", "secret_answer"):
        assert gone not in text
    assert payload["total"] == "12.5"  # Decimal, stored as text; never a float
    assert payload["customer"] == {"firstname": "Pat", "notes": ["fine"]}
    assert payload["payment_method"] == {"id": "pm-1", "nickname": "Ops"}
    assert payload["child_transactions"] == [{"meta": {"achDescription": "R01"}}]


@pytest.mark.django_db
def test_a_payload_that_still_matches_the_guard_is_not_kept() -> None:
    # A routing number next to an account-like run, split over two harmless values.
    event = stored({"zip": "021000021", "ref": "123456789012"})
    assert event.payload is None
    assert event.dedupe_hash


@pytest.mark.django_db
def test_a_deeply_nested_body_is_kept_as_its_hash_only() -> None:
    raw = b'{"a":' * 200 + b"1" + b"}" * 200
    event = stored(raw)
    assert event.payload is None


def test_redact_never_returns_a_float_or_a_dropped_marker_inside() -> None:
    assert redact({"a": [1, "b", None, True], "c": {}}) == {"a": [1, "b", None, True], "c": {}}
    assert redact([CARD, "ok"]) == ["ok"]


# --- Row 7: failures, attempts, dead letter ------------------------------------------------


@pytest.mark.django_db
def test_a_provider_failure_records_the_class_name_and_counts_an_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, "update_invoice", failing(ProviderUnavailable(503)))
    event = stored(INVOICE_BODY)
    assert processor.process_event(event.pk) == WebhookOutcome.FAILED
    event.refresh_from_db()
    assert (event.attempts, event.error, event.processed_at) == (1, "ProviderUnavailable", None)
    assert event.last_attempt_at is not None


@pytest.mark.django_db
def test_a_rejection_records_field_names_and_never_a_message_or_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        "update_invoice",
        failing(ProviderRejected(422, error_fields=("email", "phone", "bad name!"))),
    )
    event = stored(INVOICE_BODY)
    processor.process_event(event.pk)
    event.refresh_from_db()
    assert event.error == "ProviderRejected (fields: email, phone)"

    install(monkeypatch, "update_invoice", failing(ValueError(f"leaked {CARD}")))
    processor.process_event(event.pk)
    event.refresh_from_db()
    assert event.error == "ValueError"


@pytest.mark.django_db
def test_a_handler_failure_rolls_back_its_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    def half_done(event_type: str, provider_object_id: str) -> str:
        WebhookEvent.objects.create(event_type="side_effect", dedupe_hash="c" * 64)
        raise ProviderUnavailable(None)

    install(monkeypatch, "update_invoice", half_done)
    event = stored(INVOICE_BODY)
    processor.process_event(event.pk)
    assert not WebhookEvent.objects.filter(event_type="side_effect").exists()
    event.refresh_from_db()
    assert event.attempts == 1


@pytest.mark.django_db
def test_the_third_failure_dead_letters_once_with_a_critical_line_and_a_sentry_event(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentry_events: list[dict[str, Any]],
    django_capture_on_commit_callbacks: Any,
) -> None:
    install(monkeypatch, "update_invoice", failing(ProviderUnavailable(503)))
    event = stored(INVOICE_BODY)
    with caplog.at_level(logging.WARNING), django_capture_on_commit_callbacks(execute=True):
        outcomes = [processor.process_event(event.pk) for _ in range(3)]
        # Skipped without replay; a fourth failure by replay does not alert again.
        assert processor.process_event(event.pk) == WebhookOutcome.DEAD_LETTER
        assert processor.process_event(event.pk, replay=True) == WebhookOutcome.DEAD_LETTER
    assert outcomes == [WebhookOutcome.FAILED, WebhookOutcome.FAILED, WebhookOutcome.DEAD_LETTER]
    event.refresh_from_db()
    assert event.attempts == 4
    critical = [record for record in caplog.records if record.levelno == logging.CRITICAL]
    assert len(critical) == 1
    assert alerts.DEAD_LETTER_MESSAGE in critical[0].getMessage()
    assert str(event.pk) in critical[0].getMessage()
    assert sentry_events == [
        {
            "message": alerts.DEAD_LETTER_MESSAGE,
            "level": "fatal",
            "fingerprint": [alerts.DEAD_LETTER_FINGERPRINT],
        }
    ]


@pytest.mark.django_db
def test_the_worker_does_not_retry_a_failed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(event_type: str, provider_object_id: str) -> str:
        calls.append(provider_object_id)
        raise ProviderUnavailable(503)

    install(monkeypatch, "update_invoice", handler)
    event = stored(INVOICE_BODY)
    assert worker.tick() is True
    assert worker.tick() is False  # the failed event waits for a replay or the command
    assert calls == ["inv-0001"]
    event.refresh_from_db()
    assert (event.outcome, event.attempts) == (WebhookOutcome.FAILED, 1)


@pytest.mark.django_db
def test_the_worker_survives_an_error_outside_the_handler(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(event_id: uuid.UUID, *, replay: bool = False) -> str:
        raise RuntimeError(f"secret detail {CARD}")

    stored(INVOICE_BODY)
    monkeypatch.setattr(worker, "process_event", broken)
    with caplog.at_level(logging.INFO):
        assert worker.tick() is True
    assert "outcome=RuntimeError" in caplog.text
    assert CARD not in caplog.text


@pytest.mark.django_db
def test_process_webhooks_takes_unprocessed_events_oldest_first_and_skips_dead_letters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = timezone.now()
    old, middle, new = (stored({"id": f"o{n}"}, "update_invoice") for n in range(3))
    for offset, event in ((3, old), (2, middle), (1, new)):
        WebhookEvent.objects.filter(pk=event.pk).update(received_at=now - timedelta(minutes=offset))
    done = stored({"id": "done"}, "update_invoice")
    processor.process_event(done.pk)
    dead = stored({"id": "dead"}, "update_invoice")
    WebhookEvent.objects.filter(pk=dead.pk).update(outcome=WebhookOutcome.DEAD_LETTER, attempts=3)

    seen: list[str] = []

    def handler(event_type: str, provider_object_id: str) -> str:
        seen.append(provider_object_id)
        return WebhookOutcome.PROCESSED

    install(monkeypatch, "update_invoice", handler)
    call_command("process_webhooks", "--limit", "2")
    assert seen == ["o0", "o1"]
    assert "Events taken: 2. Processed: 2." in capsys.readouterr().out
    call_command("process_webhooks")
    assert seen == ["o0", "o1", "o2"]


@pytest.mark.django_db
def test_process_webhooks_retries_a_failed_event_on_the_next_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, "update_invoice", failing(ProviderUnavailable(503)))
    event = stored(INVOICE_BODY)
    call_command("process_webhooks")
    assert "Failed: 1." in capsys.readouterr().out
    call_command("process_webhooks")
    call_command("process_webhooks")
    assert "Dead letter: 1." in capsys.readouterr().out
    call_command("process_webhooks")
    assert "Events taken: 0." in capsys.readouterr().out
    event.refresh_from_db()
    assert event.attempts == 3


def test_process_webhooks_refuses_a_limit_below_one() -> None:
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("process_webhooks", "--limit", "0")


# --- Row 9: no call from the receiver; the queue goes through the limiter ------------------


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_nothing_in_the_webhooks_package_imports_the_adapter_package() -> None:
    files = sorted(WEBHOOKS_DIR.rglob("*.py"))
    assert WEBHOOKS_DIR / "receiver.py" in files
    for path in files:
        offenders = {name for name in _imported_modules(path) if name.startswith("payments.stax")}
        assert offenders == set(), path


@pytest.mark.django_db
def test_the_receiver_makes_no_provider_call_and_no_outbound_request(
    client: Client, monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Any
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the receiver must not reach the provider")

    monkeypatch.setattr(payments, "get_provider", refuse)
    monkeypatch.setattr("httpx.Client.send", refuse)
    monkeypatch.setattr("socket.socket.connect", refuse)
    with django_capture_on_commit_callbacks(execute=False):
        assert deliver(client, INVOICE_BODY, event="update_invoice").status_code == 200
    assert WebhookEvent.objects.count() == 1


@pytest.fixture
def stax_rig(monkeypatch: pytest.MonkeyPatch, settings: Any) -> Rig:
    """The real adapter on a mock transport and a fake clock, as ``get_provider()``.

    A handler that re-fetches is installed for ``update_invoice``.
    """
    settings.STAX_CALLER = "worker"
    rig = make_rig(always(200, {"id": "inv-x", "status": "SENT", "total": 10}))
    provider = StaxProvider(rig.transport)
    monkeypatch.setattr(payments, "get_provider", lambda: provider)

    def refetch(event_type: str, provider_object_id: str) -> str:
        payments.get_provider().get_invoice(provider_object_id)
        return WebhookOutcome.PROCESSED

    install(monkeypatch, "update_invoice", refetch)
    return rig


def _at_most_two_per_second(arrivals: list[int]) -> bool:
    """No window of one second holds a third departure."""
    return all(
        later - earlier >= 1_000 for earlier, later in zip(arrivals, arrivals[2:], strict=False)
    )


@pytest.mark.django_db
def test_a_burst_of_50_events_processed_by_the_command_departs_at_most_2_per_second(
    stax_rig: Rig,
) -> None:
    for number in range(50):
        stored({"id": f"inv-{number:04d}"}, "update_invoice")
    call_command("process_webhooks", "--limit", "50")
    assert len(stax_rig.recorder.arrivals) == 50
    assert _at_most_two_per_second(stax_rig.recorder.arrivals)
    assert stax_rig.recorder.arrivals[-1] >= 24_000
    assert WebhookEvent.objects.filter(outcome=WebhookOutcome.PROCESSED).count() == 50


@pytest.mark.django_db
def test_a_burst_of_50_deliveries_leaves_the_worker_at_most_2_per_second(
    stax_rig: Rig,
) -> None:
    client = Client()
    for number in range(50):
        response = deliver(client, {"id": f"inv-{number:04d}"}, event="update_invoice")
        assert response.status_code == 200
    assert stax_rig.recorder.arrivals == []  # the receiver called nothing
    for _ in range(50):
        call_command("worker", "--once")
    assert len(stax_rig.recorder.arrivals) == 50
    assert _at_most_two_per_second(stax_rig.recorder.arrivals)
    assert WebhookEvent.objects.filter(outcome=WebhookOutcome.PROCESSED).count() == 50


# --- Screens: list, detail, replay, banner, admin ------------------------------------------


def _events(count: int) -> list[WebhookEvent]:
    return [stored({"id": f"object-identifier-{n:04d}"}, "update_invoice") for n in range(count)]


@pytest.mark.django_db
def test_every_webhook_route_is_classified_with_the_planned_roles() -> None:
    names = {name for name, _, _ in iter_routes() if name and "webhooks" in name}
    assert names == {
        "webhooks:receive",
        "ledger:webhooks:list",
        "ledger:webhooks:detail",
        "ledger:webhooks:replay",
    }
    assert {"ledger:webhooks:list", "ledger:webhooks:detail"} <= registry.READ_ONLY_ROUTE_NAMES
    rule = registry.rule_for("ledger:webhooks:replay")
    assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}
    assert registry.rule_for("admin:payments_webhookevent_change") is not None


@pytest.mark.django_db
def test_the_list_needs_a_session(client: Client) -> None:
    assert client.get("/webhooks/").status_code == 401


@pytest.mark.django_db
def test_a_viewer_lists_25_per_page_with_masked_ids() -> None:
    _events(30)
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get("/webhooks/").content.decode()
    assert page.count("/webhooks/") >= 25
    assert "obje…0029" in page
    assert "object-identifier-0029" not in page
    assert "Page 1 of 2" in page
    second = viewer.get("/webhooks/?page=2").content.decode()
    assert "obje…0000" in second


@pytest.mark.django_db
def test_the_list_filters_by_outcome_and_type(monkeypatch: pytest.MonkeyPatch) -> None:
    ignored_event = stored({"id": "ignored-0001"}, "update_invoice")
    processor.process_event(ignored_event.pk)
    stored({"id": "received-0001"}, "send_invoice")
    install(monkeypatch, "create_deposit", failing(ProviderUnavailable(503)))
    failed = stored({"id": "failed-00001"}, "create_deposit")
    processor.process_event(failed.pk)
    viewer, _ = client_for(Role.VIEWER)

    def shown(query: str) -> set[str]:
        page = viewer.get(f"/webhooks/?{query}").content.decode()
        return {name for name in ("igno…0001", "rece…0001", "fail…0001") if name in page}

    assert shown("") == {"igno…0001", "rece…0001", "fail…0001"}
    assert shown("outcome=ignored") == {"igno…0001"}
    assert shown("outcome=failed") == {"fail…0001"}
    assert shown("outcome=received") == {"rece…0001"}
    assert shown("type=send_invoice") == {"rece…0001"}
    assert shown("outcome=failed&type=send_invoice") == set()
    # Unknown filter values are ignored, not errors.
    assert shown("outcome=nonsense&type=%3Cscript%3E") == {"igno…0001", "rece…0001", "fail…0001"}


@pytest.mark.django_db
def test_the_detail_page_shows_the_redacted_payload_as_a_table() -> None:
    event = stored(
        {
            "id": "invoice-identifier-1",
            "status": "SENT",
            "customer": {"customer_id": "customer-identifier-1", "firstname": "Pat"},
            "memo": f"card {CARD}",
        },
        "update_invoice",
    )
    WebhookEvent.objects.filter(pk=event.pk).update(duplicate_count=2, error="ProviderUnavailable")
    viewer, _ = client_for(Role.VIEWER)
    response = viewer.get(f"/webhooks/{event.pk}/")
    assert response.status_code == 200
    page = response.content.decode()
    assert '<table id="payload">' in page
    assert "<code>status</code></td><td>SENT</td>" in page
    assert "<code>customer.firstname</code></td><td>Pat</td>" in page
    assert "invoice-identifier-1" not in page and "invo…er-1" in page
    assert "customer-identifier-1" not in page and "cust…er-1" in page
    assert CARD not in page
    assert '<dd id="duplicates">2</dd>' in page
    assert '<dd id="error">ProviderUnavailable</dd>' in page
    assert 'id="replay"' not in page  # a viewer has no replay button


@pytest.mark.django_db
def test_a_detail_without_payload_says_so_and_bad_ids_are_404() -> None:
    event = stored(b"not json", None)
    viewer, _ = client_for(Role.VIEWER)
    assert 'id="no-payload"' in viewer.get(f"/webhooks/{event.pk}/").content.decode()
    assert viewer.get("/webhooks/not-a-uuid/").status_code == 404
    assert viewer.get(f"/webhooks/{uuid.uuid4()}/").status_code == 404


@pytest.mark.django_db
def test_billing_replays_a_dead_letter_and_it_is_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = stored(INVOICE_BODY)
    WebhookEvent.objects.filter(pk=event.pk).update(
        outcome=WebhookOutcome.DEAD_LETTER, attempts=3, error="ProviderUnavailable"
    )
    billing, _ = client_for(Role.BILLING)
    assert 'id="replay"' in billing.get(f"/webhooks/{event.pk}/").content.decode()
    response = billing.post(f"/webhooks/{event.pk}/replay/", follow=True)
    assert response.redirect_chain[-1][0] == f"/webhooks/{event.pk}/"
    # PLAN-010 Batch A follow-up: the replay is a job the worker runs.
    page = response.content.decode()
    assert 'id="job-state">Queued<' in page and 'http-equiv="refresh"' in page
    event.refresh_from_db()
    assert event.outcome == WebhookOutcome.DEAD_LETTER
    run_queued_jobs()
    page = billing.get(f"/webhooks/{event.pk}/").content.decode()
    assert f'id="job-state">Done: Replayed. Outcome: {PLACEHOLDER}.<' in page
    event.refresh_from_db()
    assert event.outcome == PLACEHOLDER
    assert event.processed_at is not None and event.error == ""


@pytest.mark.django_db
def test_a_replay_of_a_processed_event_runs_the_handler_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def handler(event_type: str, provider_object_id: str) -> str:
        calls.append(provider_object_id)
        return WebhookOutcome.PROCESSED

    install(monkeypatch, "update_invoice", handler)
    event = stored(INVOICE_BODY)
    processor.process_event(event.pk)
    processor.process_event(event.pk)  # not a replay: skipped
    billing, _ = client_for(Role.BILLING)
    billing.post(f"/webhooks/{event.pk}/replay/")
    assert calls == ["inv-0001"]
    run_queued_jobs()
    assert calls == ["inv-0001", "inv-0001"]


@pytest.mark.django_db
def test_a_failed_replay_reports_the_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, "update_invoice", failing(ProviderUnavailable(503)))
    event = stored(INVOICE_BODY)
    billing, _ = client_for(Role.BILLING)
    billing.post(f"/webhooks/{event.pk}/replay/")
    run_queued_jobs()
    page = billing.get(f"/webhooks/{event.pk}/").content.decode()
    assert (
        'id="job-state">Failed: Replayed. The handler failed again; outcome: failed. '
        "(ProviderUnavailable)<"
    ) in page


@pytest.mark.django_db
def test_a_viewer_cannot_replay() -> None:
    event = stored(INVOICE_BODY)
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(f"/webhooks/{event.pk}/replay/").status_code == 403
    event.refresh_from_db()
    assert event.processed_at is None and event.last_attempt_at is None


@pytest.mark.django_db
def test_the_run_page_shows_a_banner_while_events_are_dead_lettered() -> None:
    viewer, _ = client_for(Role.VIEWER)
    assert 'id="webhook-banner"' not in viewer.get("/run/").content.decode()
    first, second = _events(2)
    WebhookEvent.objects.filter(pk=first.pk).update(outcome=WebhookOutcome.DEAD_LETTER)
    page = viewer.get("/run/").content.decode()
    assert 'id="webhook-banner"' in page
    assert "1 webhook event is dead-lettered" in page
    assert 'href="/webhooks/?outcome=dead_letter"' in page
    WebhookEvent.objects.filter(pk=second.pk).update(outcome=WebhookOutcome.DEAD_LETTER)
    assert "2 webhook events are dead-lettered" in viewer.get("/run/").content.decode()


@pytest.mark.django_db
def test_the_admin_lists_webhook_events_read_only() -> None:
    assert WebhookEvent in admin_site._registry
    model_admin = admin_site._registry[WebhookEvent]
    admin_client, user = client_for(Role.ADMIN)
    user.is_superuser = True
    user.save()
    event = stored(INVOICE_BODY)
    assert admin_client.get("/admin/payments/webhookevent/").status_code == 200
    assert admin_client.get(f"/admin/payments/webhookevent/{event.pk}/change/").status_code == 200
    request = admin_client.get("/admin/").wsgi_request
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request, event)
    assert not model_admin.has_delete_permission(request, event)
    response = admin_client.post(
        f"/admin/payments/webhookevent/{event.pk}/delete/", {"post": "yes"}
    )
    assert response.status_code == 403
    assert WebhookEvent.objects.filter(pk=event.pk).exists()
