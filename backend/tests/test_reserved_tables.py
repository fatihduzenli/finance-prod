"""The reserved table (docs/qa/PLAN-006-operators.md §6 row 10, D8, §4.4).

``outbound_messages`` exists, starts empty, is covered by the card-number guard
(application validator everywhere, trigger on PostgreSQL), and has no URL and no admin
page. ``documents`` was reserved with it until PLAN-010c D4 reshaped it for uploads; its
columns, guards and routes are tested in ``test_documents.py``. The 0004 migration's
guard list is history and stays as written.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from ledger.models import Document, Operator, OutboundMessage
from ledger.validators import PAYMENT_DATA_MESSAGE
from tests.auth_helpers import iter_routes

CARD = "4111 1111 1111 1111"
TABLES = ("documents", "outbound_messages")
#: Reserved tables still without a writer.
RESERVED = ("outbound_messages",)
GUARDS = importlib.import_module("ledger.migrations.0004_reserved_tables").GUARDED_COLUMNS

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="database triggers exist on PostgreSQL only; the test database is not PostgreSQL",
)


def operator() -> Operator:
    row = Operator(display_name="Reserved Test Operator")
    row.save()
    return row


def message(**fields: Any) -> OutboundMessage:
    values = {"template": "welcome", "subject": "Welcome", "delivery_status": "queued"}
    return OutboundMessage(operator=operator(), **{**values, **fields})


@pytest.mark.django_db
def test_the_tables_exist_and_are_empty() -> None:
    assert set(TABLES) <= set(connection.introspection.table_names())
    assert Document.objects.count() == 0
    assert OutboundMessage.objects.count() == 0


def test_columns_are_as_listed() -> None:
    def columns(model: type[OutboundMessage]) -> set[str]:
        return {str(field.column) for field in model._meta.concrete_fields}

    assert columns(OutboundMessage) == {
        "id",
        "operator_id",
        "template",
        "subject",
        "document_refs",
        "sent_by_id",
        "sent_at",
        "delivery_status",
    }
    assert OutboundMessage._meta.get_field("document_refs").get_default() == []


def test_every_text_and_json_column_has_a_trigger() -> None:
    assert GUARDS == {
        "documents": ("kind", "file_reference"),
        "outbound_messages": ("template", "subject", "document_refs", "delivery_status"),
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (message, "template", CARD),
        (message, "subject", f"card {CARD}"),
        (message, "delivery_status", CARD),
        (message, "document_refs", [CARD]),
    ],
)
def test_save_refuses_a_card_number(factory: Any, field: str, value: Any) -> None:
    row = factory(**{field: value})
    with pytest.raises(ValidationError) as raised:
        row.save()
    assert raised.value.messages == [PAYMENT_DATA_MESSAGE]


@pytest.mark.django_db
def test_a_valid_row_saves() -> None:
    message(document_refs=["00000000-0000-0000-0000-000000000000"]).save()
    assert OutboundMessage.objects.count() == 1


@requires_postgresql
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("table", "column"),
    [
        (table, column)
        for table, columns in GUARDS.items()
        if table in RESERVED
        for column in columns
    ],
)
def test_the_database_trigger_refuses_a_card_number(table: str, column: str) -> None:
    row = message()
    row.save()
    is_json = column == "document_refs"
    value = json.dumps([CARD]) if is_json else CARD
    placeholder = "%s::jsonb" if is_json else "%s"
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET {column} = {placeholder} WHERE id = %s",  # noqa: S608
                [value, str(row.pk)],
            )
    assert "4111" not in str(raised.value)


def test_no_url_and_no_admin() -> None:
    # Documents have routes since PLAN-010c, but still no admin page.
    assert Document not in admin.site._registry
    assert OutboundMessage not in admin.site._registry
    for name, raw, _ in iter_routes():
        text = f"{name or ''} {raw}".lower()
        assert "outbound" not in text and "message" not in text, text
