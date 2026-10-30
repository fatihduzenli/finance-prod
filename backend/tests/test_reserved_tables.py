"""The formerly reserved table ``outbound_messages`` (docs/qa/PLAN-006-operators.md D8,
reshaped by docs/qa/PLAN-011b D4).

The table was reserved in PLAN-006 with no writer; PLAN-011b D4 reshaped it into the log
of collection e-mails (``ledger/migrations/0024_collections_ladder.py``). It starts
empty, its free-text columns are covered by the card-number guard (application
validator everywhere, trigger on PostgreSQL), its identifier columns are held to their
shape by CHECK constraints, and it has no admin page (the log is shown on the operator's
page). ``documents`` was reserved with it until PLAN-010c D4; its columns, guards and
routes are tested in ``test_documents.py``. The 0004 migration's guard list is history
and stays as written.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from ledger.models import (
    Document,
    MessageTemplate,
    Operator,
    OutboundMessage,
)
from ledger.validators import PAYMENT_DATA_MESSAGE
from tests.auth_helpers import iter_routes

CARD = "4111 1111 1111 1111"
TABLES = ("documents", "outbound_messages")
OLD_GUARDS = importlib.import_module("ledger.migrations.0004_reserved_tables").GUARDED_COLUMNS
GUARDS = importlib.import_module("ledger.migrations.0024_collections_ladder").GUARDED_COLUMNS
#: PLAN-014 D3: ledger 0061 replaced the outbound_messages and message_templates entries.
ENGINE_GUARDS = importlib.import_module("ledger.migrations.0061_messaging_engine").GUARDED_COLUMNS
LIVE_GUARDS = {**GUARDS, **ENGINE_GUARDS}

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="database triggers exist on PostgreSQL only; the test database is not PostgreSQL",
)


def operator() -> Operator:
    row = Operator(display_name="Reserved Test Operator", email="ops@example.com")
    row.save()
    return row


def message(**fields: Any) -> OutboundMessage:
    values = {
        "template": MessageTemplate.objects.get(code="reminder_due"),
        "rule": "B",
        "charge_entity_type": "rent_charge",
        "charge_entity_id": "00000000-0000-0000-0000-000000000001",
        "to_address": "ops@example.com",
        "rendered_subject": "Reminder",
        "rendered_body": "Owed.",
    }
    return OutboundMessage(operator=operator(), **{**values, **fields})


@pytest.mark.django_db
def test_the_tables_exist_and_start_empty() -> None:
    assert set(TABLES) <= set(connection.introspection.table_names())
    assert Document.objects.count() == 0
    assert OutboundMessage.objects.count() == 0


def test_columns_are_as_listed() -> None:
    columns = {str(field.column) for field in OutboundMessage._meta.concrete_fields}
    assert columns == {
        "lease_id",  # PLAN-014 C5: the lease whose billing e-mail the message goes to
        "id",
        "operator_id",
        "template_id",
        "charge_entity_type",
        "charge_entity_id",
        "to_address",
        "body_sha256",
        "status",
        "mail_message_id",
        "error",
        "manual",
        "queued_at",
        "sent_at",
        "sent_by_id",
        # PLAN-012e D8: the message's kind and a final statement's attachment.
        "kind",
        "document_id",
        # PLAN-014 C5: the lease billed; D3: the engine's columns (``subject`` became
        # ``rendered_subject``).
        "lease_id",
        "channel",
        "rule",
        "occurrence",
        "rendered_subject",
        "rendered_body",
        "held_reason",
        "scheduled_for",
    }


def test_the_guard_lists() -> None:
    assert OLD_GUARDS["outbound_messages"] == (
        "template",
        "subject",
        "document_refs",
        "delivery_status",
    )
    assert GUARDS["outbound_messages"] == ("to_address", "subject", "error")
    assert GUARDS["message_templates"] == ("code", "subject", "body")
    assert GUARDS["operators"][-1] == "handled_personally_note"
    assert ENGINE_GUARDS["outbound_messages"] == (
        "to_address",
        "rendered_subject",
        "rendered_body",
        "error",
    )
    assert ENGINE_GUARDS["message_templates"] == ("code", "subject", "body", "name")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rendered_subject", f"card {CARD}"),
        ("rendered_body", f"card {CARD}"),
        ("to_address", "4111111111111111@example.com"),
    ],
)
def test_save_refuses_a_card_number(field: str, value: Any) -> None:
    row = message(**{field: value})
    with pytest.raises(ValidationError) as raised:
        row.save()
    assert raised.value.messages == [PAYMENT_DATA_MESSAGE]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("body_sha256", "not-a-digest"),
        ("body_sha256", "A" * 64),
        ("error", "Bad error with spaces"),
        ("mail_message_id", "id with spaces"),
        ("status", "delivered"),
        ("charge_entity_type", "invoice"),
        ("rule", "Z"),
        ("channel", "fax"),
        ("held_reason", "later"),
        ("occurrence", 0),
    ],
)
def test_identifier_and_choice_columns_are_held_to_their_shape(field: str, value: str) -> None:
    row = message(**{field: value})
    with pytest.raises(IntegrityError), transaction.atomic():
        row.save()


@pytest.mark.django_db
def test_a_manual_row_needs_its_sender() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        message(manual=True).save()


@pytest.mark.django_db
def test_a_valid_row_saves() -> None:
    message(body_sha256="ab" * 32, error="ConnectTimeout", mail_message_id="18f_a-B").save()
    assert OutboundMessage.objects.count() == 1


@requires_postgresql
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("table", "column"),
    [(table, column) for table, columns in LIVE_GUARDS.items() for column in columns],
)
def test_the_database_trigger_refuses_a_card_number(table: str, column: str) -> None:
    rows = {
        "outbound_messages": lambda: message(),
        "message_templates": lambda: MessageTemplate(code="t_guard", subject="S", body="B"),
        "operators": operator,
    }
    row = rows[table]()
    row.save()
    value = "4111111111111111@example.com" if column == "to_address" else CARD
    if column == "code":
        value = "x4111111111111111"
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET {column} = %s WHERE id = %s",  # noqa: S608
                [value, str(row.pk)],
            )
    assert "4111" not in str(raised.value)


def test_no_admin_page_and_no_route_named_for_the_log() -> None:
    # Documents have routes since PLAN-010c; neither table has an admin page. The log is
    # read on the operator's and the charge's pages.
    assert Document not in admin.site._registry
    assert OutboundMessage not in admin.site._registry
    for name, raw, _ in iter_routes():
        text = f"{name or ''} {raw}".lower()
        assert "outbound" not in text, text
