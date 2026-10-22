"""Card-number guard (PLAN-004 §6 rows 5, 6 and 7, amended by §4.6; docs/CLAUDE.md Rule 1).

Only the public test number 4111 1111 1111 1111 and numbers computed here to pass (or
fail) the Luhn check are used. Row 7 runs on PostgreSQL only: it bypasses the
application validator (``QuerySet.update``, ``bulk_create``, raw SQL) and expects the
database trigger to refuse the row with the same value-free message.
"""

from __future__ import annotations

import importlib
import json
import random
import re
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.utils import timezone

from ledger import models as ledger_models
from ledger.validators import (
    INVISIBLE_CHARACTERS,
    PAYMENT_DATA_MESSAGE,
    contains_bank_routing_pattern,
    contains_card_number,
    validate_json_has_no_payment_data,
    validate_no_payment_data,
)
from payments.sync_writer import provider_write
from tests.test_ledger_models import (
    LEDGER_FACTORIES,
    LOGO_PATH,
    make_user,
    requires_postgresql,
    saved,
)
from tests.test_payments_models import PAYMENTS_FACTORIES

TEST_CARD = "4111 1111 1111 1111"


def _luhn_valid(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        digit = int(char) * (2 if position % 2 else 1)
        total += digit - 9 if digit > 9 else digit
    return total % 10 == 0


def luhn_complete(prefix: str) -> str:
    """``prefix`` plus the check digit that makes it pass the Luhn check."""
    return next(prefix + d for d in "0123456789" if _luhn_valid(prefix + d))


def luhn_break(prefix: str) -> str:
    """``prefix`` plus a check digit that makes it fail the Luhn check."""
    return next(prefix + d for d in "0123456789" if not _luhn_valid(prefix + d))


LUHN_13 = luhn_complete("123456789012")
LUHN_19 = luhn_complete("123456789012345678")
LUHN_12 = luhn_complete("12345678901")
LUHN_20 = luhn_complete("1234567890123456789")
FAILS_LUHN_16 = luhn_break("411111111111111")

#: PLAN-004 §6 row 5.
REJECTED = [
    TEST_CARD,
    "4111-1111-1111-1111",
    "4111111111111111",
    LUHN_13,
    LUHN_19,
    f"Operator read out {TEST_CARD} on the phone; told them we cannot take cards.",
    f"Call notes:\n{LUHN_19}\nasked to use ACH instead",
    "Split over two lines: 4111 1111\n1111 1111 end",
    "Windows line break: 4111 1111\r\n1111 1111",
    # PLAN-004 §4.6 point 2: one to three separators of the listed kinds.
    "4111  1111   1111 1111",
    "4111\t1111\t1111\t1111",
    "\u00a0".join(["4111", "1111", "1111", "1111"]),
    "\u2010".join(["4111", "1111", "1111", "1111"]),
    "\u2015".join(["4111", "1111", "1111", "1111"]),
    "4111 - 1111 \u2013 1111 \u2014 1111",
    "4111 \r\n 1111 1111 1111",
    # PLAN-004 §4.6 point 3: invisible characters are removed first.
    "\u200b".join(["4111", "1111", "1111", "1111"]),
    "4\u200c1\u200d1\u20601\ufeff111111111111",
    "4111 \u200b\u200b\u200b 1111 1111 1111",
    # A UUID-shaped token becomes one letter, which ends a run but hides nothing else.
    "5f0c2d8e-9b1a-4c3e-8f7d-2a6b1c9e0d4f 4111111111111111",
]

#: PLAN-004 §6 row 6.
ACCEPTED = [
    "INV-2026-0001",
    "Invoice INV-2026-12345678 sent twice",
    FAILS_LUHN_16,
    "+1 (555) 010-0199",
    "555-010-0199",
    "+1 555 010 0199",
    LUHN_12,
    LUHN_20,
    "Rent 12581.00 due",
    "$12,581.00",
    "2026-09-29",
    "09/29/2026",
    "5f0c2d8e-9b1a-4c3e-8f7d-2a6b1c9e0d4f",
    # PLAN-004 §4.6 point 4: UUID-shaped identifiers whose tail is all digits.
    "0a1b2c3d-4e5f-1234-5678-123456789012",
    "0A1B2C3D-4E5F-1234-5678-123456789012",
    "id=0a1b2c3d-4e5f-1234-5678-123456789012;",
    "",
]


def test_computed_numbers_have_the_intended_shape() -> None:
    assert (len(LUHN_12), len(LUHN_13), len(LUHN_19), len(LUHN_20)) == (12, 13, 19, 20)
    assert all(_luhn_valid(n) for n in (LUHN_12, LUHN_13, LUHN_19, LUHN_20))
    assert _luhn_valid("4111111111111111")
    assert len(FAILS_LUHN_16) == 16 and not _luhn_valid(FAILS_LUHN_16)


# --- Rows 5 and 6: application validator -----------------------------------------------


@pytest.mark.parametrize("text", REJECTED)
def test_validator_rejects_card_numbers(text: str) -> None:
    assert contains_card_number(text)
    with pytest.raises(ValidationError) as raised:
        validate_no_payment_data(text)
    assert raised.value.messages == [PAYMENT_DATA_MESSAGE]
    assert raised.value.code == "payment_data"
    for rendered in (str(raised.value), repr(raised.value)):
        assert text not in rendered
        assert "1111" not in rendered and LUHN_13 not in rendered


@pytest.mark.parametrize("text", ACCEPTED)
def test_validator_accepts_ordinary_text(text: str) -> None:
    assert not contains_card_number(text)
    validate_no_payment_data(text)


def test_validator_accepts_none() -> None:
    validate_no_payment_data(None)


def test_full_width_and_arabic_indic_digits_count_as_digits() -> None:
    full_width = "".join(chr(0xFF10 + int(d)) for d in "4111111111111111")
    arabic_indic = "".join(chr(0x0660 + int(d)) for d in "4111111111111111")
    assert contains_card_number(full_width)
    assert contains_card_number(arabic_indic)


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111    1111 1111",  # four spaces
        "4111 1111 \t - 1111 1111",  # four separators of mixed kinds
        "4111 1111\n\n\n\n1111 1111",
        "4111 1111\r\n\r\n\r\n\r\n1111 1111",  # four Windows line breaks
        "4111 1111\r1111 1111",  # a carriage return alone is no separator
        "4111.1111.1111.1111",
        "4111/1111/1111/1111",
        "4111:1111:1111:1111",
        "4111x1111x1111x1111",
        "4111_1111_1111_1111",
    ],
)
def test_other_characters_or_four_separators_end_the_run(text: str) -> None:
    # PLAN-004 §4.6 point 2: each piece holds only 8 digits, so none is a card number.
    assert not contains_card_number(text)


def test_three_windows_line_breaks_count_as_three_separators() -> None:
    assert contains_card_number("4111 1111\r\n\r\n\r\n1111 1111")


def test_a_uuid_is_replaced_only_when_bounded_by_non_hexadecimal_characters() -> None:
    digits = LUHN_19  # 4 + 4 + 11 digits, then a letter: a UUID-shaped token
    uuid_shaped = f"0a1b2c3d-ffff-{digits[:4]}-{digits[4:8]}-{digits[8:]}a"
    assert not contains_card_number(uuid_shaped)
    assert not contains_card_number(f"id: {uuid_shaped}, next")
    # A hexadecimal character on either side: not UUID-shaped, so the digits are read.
    assert contains_card_number(f"b{uuid_shaped}")
    assert contains_card_number(f"{uuid_shaped}e")


def test_matching_is_linear_in_the_length_of_the_text() -> None:
    hostile = [
        "1   " * 50_000,
        "1    " * 50_000,
        "\u200b" * 200_000,
        "0a1b2c3d-4e5f-1234-5678-123456789012" * 5_000,
        "0a1b2c3d-4e5f-1234-5678-12345678901" * 5_000,
        "1\r\n" * 100_000,
    ]
    started = time.monotonic()
    for text in hostile:
        contains_card_number(text)
    assert time.monotonic() - started < 2.0


# ABA checksum 3-7-1: "123456780" is valid, "123456789" is not. Both are invented.
ROUTING = "123456780"


@pytest.mark.parametrize(
    "text",
    [
        f"routing {ROUTING} account 987654321012",
        f"987654 {ROUTING}",
        f"ABA {ROUTING}, acct no. 0012345678",
    ],
)
def test_routing_number_next_to_an_account_number_is_rejected(text: str) -> None:
    assert contains_bank_routing_pattern(text)
    with pytest.raises(ValidationError) as raised:
        validate_no_payment_data(text)
    assert raised.value.messages == [PAYMENT_DATA_MESSAGE]
    assert ROUTING not in str(raised.value)


@pytest.mark.parametrize(
    "text",
    [
        f"routing {ROUTING}",
        "routing 123456789 account 987654321012",
        f"routing {ROUTING}" + " " * 21 + "987654321012",
        f"routing {ROUTING} account 12345",
    ],
)
def test_routing_pattern_needs_a_valid_checksum_and_a_nearby_account(text: str) -> None:
    assert not contains_bank_routing_pattern(text)


# UUIDs whose digits once read as an ABA-valid routing number beside an account-like run
# (a 9-digit run and an 8-digit run within 20 characters), before the routing rule read
# the normalised text. They are random identifiers, not payment data.
ROUTING_SHAPED_UUIDS = [
    "96182028-8dcf-4e51-bf69-7a490278743e",
    "2863717d-b96d-4bd3-9771-659885498bb2",
    "8480340c-5e4b-4e9c-954d-703119764bff",
]


@pytest.mark.parametrize("identifier", ROUTING_SHAPED_UUIDS)
def test_a_uuid_is_not_read_as_a_routing_and_account_number(identifier: str) -> None:
    assert not contains_bank_routing_pattern(identifier)
    validate_no_payment_data(identifier)
    validate_no_payment_data(identifier.upper())
    validate_json_has_no_payment_data(
        {"lease": identifier, "year": 2026, "amount": "125.00", "kind": "insurance"}
    )


def test_a_routing_and_account_number_beside_a_uuid_is_still_rejected() -> None:
    identifier = ROUTING_SHAPED_UUIDS[0]
    text = f"lease {identifier} routing {ROUTING} account 987654321012"
    assert contains_bank_routing_pattern(text)
    with pytest.raises(ValidationError) as raised:
        validate_json_has_no_payment_data({"lease": identifier, "note": text})
    assert raised.value.messages == [PAYMENT_DATA_MESSAGE]
    assert ROUTING not in str(raised.value)


def test_routing_and_account_digits_split_by_an_invisible_character_are_rejected() -> None:
    assert contains_bank_routing_pattern(f"routing {ROUTING[:4]}\u200b{ROUTING[4:]} 987654")


def test_ten_thousand_random_uuids_are_accepted() -> None:
    # With this seed, five of the identifiers were refused before the routing rule read
    # the normalised text.
    generator = random.Random(20261009)  # noqa: S311 - reproducible test data, not security
    for _ in range(10_000):
        identifier = str(uuid.UUID(int=generator.getrandbits(128), version=4))
        validate_json_has_no_payment_data({"lease": identifier, "year": 2026})


def test_json_validator_checks_the_serialised_text() -> None:
    with pytest.raises(ValidationError) as raised:
        validate_json_has_no_payment_data({"memo": {"lines": ["x", f"card {TEST_CARD}"]}})
    assert raised.value.messages == [PAYMENT_DATA_MESSAGE]
    validate_json_has_no_payment_data({"total": "12581.00", "id": "INV-2026-0001"})
    validate_json_has_no_payment_data(None)


@pytest.mark.parametrize(
    "value",
    [
        {"note": "4111 1111\n1111 1111"},
        ["ok", {"nested": [f"x\r\n{LUHN_19}"]}],
        {TEST_CARD: "key holds it"},
        TEST_CARD,
    ],
    ids=["line-break-in-string", "nested-list", "key", "bare-string"],
)
def test_json_validator_checks_each_string_too(value: object) -> None:
    # json.dumps escapes a line break as backslash-n, which alone would split the run.
    with pytest.raises(ValidationError):
        validate_json_has_no_payment_data(value)


# --- Where the guard applies ---------------------------------------------------------

GUARD_VALIDATORS = (validate_no_payment_data, validate_json_has_no_payment_data)
FACTORIES: dict[type[models.Model], Callable[..., models.Model]] = {
    **LEDGER_FACTORIES,
    **PAYMENTS_FACTORIES,
}


def guarded_fields() -> list[tuple[type[models.Model], models.Field[Any, Any]]]:
    return [
        (model, field)
        for label in ("ledger", "payments")
        for model in apps.get_app_config(label).get_models()
        for field in model._meta.concrete_fields
        if any(validator in GUARD_VALIDATORS for validator in field.validators)
    ]


GUARDED = guarded_fields()
GUARDED_IDS = [f"{model._meta.db_table}.{field.column}" for model, field in GUARDED]


def test_guarded_columns_match_the_database_triggers() -> None:
    # A later migration's entry for a table replaces the earlier one: it drops the old
    # trigger and creates the new one (ledger 0015 reshaped ``documents``, PLAN-010c D4).
    triggers: dict[str, tuple[str, ...]] = {}
    for module in (
        "ledger.migrations.0002_postgresql_guards",
        "ledger.migrations.0004_reserved_tables",
        "ledger.migrations.0005_operator_sync_failure_fields",
        "ledger.migrations.0009_payment_returns_resolution",
        "ledger.migrations.0010_rent_charge_review_message",
        "ledger.migrations.0012_setup_charges",
        "ledger.migrations.0014_store_costs_deposit_resolutions",
        "ledger.migrations.0015_documents_reshape",
        "ledger.migrations.0016_store_cost_corrections",
        "ledger.migrations.0019_one_off_charges_constraints",
        "ledger.migrations.0023_contacts_and_calendar",
        "ledger.migrations.0024_collections_ladder",
        "ledger.migrations.0028_contact_log_maturity",
        "ledger.migrations.0029_invoice_builder",
        "ledger.migrations.0030_disputes",
        "ledger.migrations.0031_line_kind_fees_consent",
        "ledger.migrations.0032_builder_item",
        "ledger.migrations.0034_proration",
        "ledger.migrations.0035_lease_end_transfer",
        "ledger.migrations.0037_transfer_fields",
        "ledger.migrations.0038_installments",
        "ledger.migrations.0039_transfer_debt_override",
        "ledger.migrations.0040_lease_end_statement_opened",
        "ledger.migrations.0042_payment_allocation_and_credits",
        "ledger.migrations.0043_fee_type_show_in_setup",
        "ledger.migrations.0044_draft_existing_charge",
        "ledger.migrations.0045_company_settings",
        "ledger.migrations.0047_store_address",
        "payments.migrations.0002_postgresql_guards",
        "payments.migrations.0003_webhook_event_processing",
        "payments.migrations.0005_jobs",
        "payments.migrations.0007_reconciliation",
    ):
        triggers.update(importlib.import_module(module).GUARDED_COLUMNS)
    # ledger 0017 renamed ``setup_charges`` to ``one_off_charges`` and 0019 replaced its
    # trigger (PLAN-010d D1): the old table's entry names a table that no longer exists.
    del triggers["setup_charges"]
    expected = {f"{table}.{column}" for table, columns in triggers.items() for column in columns}
    assert set(GUARDED_IDS) == expected


#: PLAN-004 §4.6 point 1: identifiers, not free text, and so outside the guard.
IDENTIFIER_COLUMNS = {
    "external_refs.external_id",
    "deposits.provider_deposit_id",
    "webhook_events.provider_object_id",
    "webhook_events.dedupe_hash",
    "audit_log.entity_id",
    "import_audit.target_id",
    # A SHA-256 in hexadecimal (PLAN-010c D4), like webhook_events.dedupe_hash.
    "documents.sha256",
    # PLAN-011b D4: the sent body's SHA-256 and the mail service's message ID, each held
    # to its shape by a CHECK.
    "outbound_messages.body_sha256",
    "outbound_messages.mail_message_id",
}


def test_guard_covers_every_open_text_column_except_the_named_identifiers() -> None:
    open_text = set()
    for label in ("ledger", "payments"):
        for model in apps.get_app_config(label).get_models():
            for field in model._meta.concrete_fields:
                is_text = isinstance(field, models.CharField | models.TextField)
                can_hold_13 = field.max_length is None or field.max_length >= 13
                if isinstance(field, models.JSONField) or (
                    is_text and not field.choices and can_hold_13
                ):
                    open_text.add(f"{model._meta.db_table}.{field.column}")
    assert IDENTIFIER_COLUMNS <= open_text
    assert set(GUARDED_IDS) == open_text - IDENTIFIER_COLUMNS
    # The plan's original free-text columns (§4.3) are all still covered.
    assert {
        "operators.notes",
        "stores.open_date_note",
        "leases.end_reason",
        "charge_notes.body",
        "charge_adjustments.note",
        "notes.body",
        "invoices.memo",
        "invoice_line_items.description",
        "payments.return_reason",
        "payments.reference",
        "payments.note",
        "import_audit.raw_value",
        "payments.raw_summary",
        "provider_snapshots.payload",
        "legacy_records.raw",
        "audit_log.before",
        "audit_log.after",
        # Named in the QA cycle 1 finding.
        "operators.display_name",
        "operators.legal_name",
        "operators.company",
        "operators.address_line1",
        "operators.address_line2",
        "charge_notes.author_label",
        "import_audit.rule_applied",
        "webhook_events.error",
        "sync_state.cursor",
    } <= set(GUARDED_IDS)


#: An identifier with a shape of its own holds the Luhn-valid run inside that shape:
#: ``documents.sha256`` must be 64 lower-case hexadecimal characters (its CHECK).
IDENTIFIER_VALUES = {
    "documents.sha256": "4111111111111111" + "abcdef" * 8,
    "outbound_messages.body_sha256": "4111111111111111" + "abcdef" * 8,
}

IDENTIFIER_FIELDS = [
    (model, field)
    for label in ("ledger", "payments")
    for model in apps.get_app_config(label).get_models()
    for field in model._meta.concrete_fields
    if f"{model._meta.db_table}.{field.column}" in IDENTIFIER_COLUMNS
]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("model", "field"),
    IDENTIFIER_FIELDS,
    ids=[f"{model._meta.db_table}.{field.column}" for model, field in IDENTIFIER_FIELDS],
)
def test_identifier_columns_accept_a_luhn_valid_number(
    model: type[models.Model], field: models.Field[Any, Any]
) -> None:
    # Not free text: a provider identifier is never refused, by save() or by a trigger.
    column = f"{model._meta.db_table}.{field.column}"
    value = IDENTIFIER_VALUES.get(column, "4111111111111111")
    instance = saved(FACTORIES[model](**{field.attname: value}))
    assert model._default_manager.filter(pk=instance.pk).exists()


def _card_value(field: models.Field[Any, Any], text: str) -> Any:
    return {"note": text} if isinstance(field, models.JSONField) else text


def _fits(field: models.Field[Any, Any], text: str) -> bool:
    return field.max_length is None or len(text) <= field.max_length


@pytest.mark.django_db
@pytest.mark.parametrize(("model", "field"), GUARDED, ids=GUARDED_IDS)
def test_save_rejects_a_card_number_in_every_guarded_column(
    model: type[models.Model], field: models.Field[Any, Any]
) -> None:
    instance = FACTORIES[model](**{field.attname: _card_value(field, TEST_CARD)})
    with pytest.raises(ValidationError) as raised:
        instance.save()
    assert raised.value.messages == [PAYMENT_DATA_MESSAGE]
    assert not model._default_manager.filter(pk=instance.pk).exists()


# --- Row 7: database trigger (PostgreSQL) ---------------------------------------------

#: Written into every guarded column when the validator is bypassed; a sample longer than
#: the column is skipped (the column type refuses it before any trigger runs).
TRIGGER_REJECTED = [
    TEST_CARD,
    "4111111111111111",
    "4111\t1111\u00a01111\u20131111",
    "\u200b".join(["4111", "1111", "1111", "1111"]),
]


def _audit_log_insert(column: str, placeholder: str, parameter: Any) -> None:
    values = {"action": "'insert'", "entity_type": "'operator'", "entity_id": "'x'"}
    values[column] = placeholder
    columns = ", ".join([*values, "at"])
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO audit_log ({columns}) VALUES ({', '.join(values.values())}, now())",  # noqa: S608
            [parameter],
        )


def _raw_write(model: type[models.Model], field: models.Field[Any, Any], value: Any) -> None:
    table, column = model._meta.db_table, field.column
    is_json = isinstance(field, models.JSONField)
    parameter = json.dumps(value) if is_json else value
    placeholder = "%s::jsonb" if is_json else "%s"
    if table == "audit_log":  # append-only: every UPDATE is refused before the guard
        _audit_log_insert(str(column), placeholder, parameter)
        return
    existing = saved(FACTORIES[model]())
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table} SET {column} = {placeholder} WHERE id = %s",  # noqa: S608
            [parameter, existing.pk],
        )


def _bypass(
    path: str, model: type[models.Model], field: models.Field[Any, Any], value: Any
) -> None:
    if path == "bulk_create":
        model._default_manager.bulk_create([FACTORIES[model](**{field.attname: value})])
    elif path == "queryset_update" and model._meta.db_table != "audit_log":
        existing = saved(FACTORIES[model]())
        model._default_manager.filter(pk=existing.pk).update(**{field.attname: value})
    else:
        _raw_write(model, field, value)


@requires_postgresql
@pytest.mark.django_db
@pytest.mark.parametrize("path", ["queryset_update", "bulk_create", "raw_sql"])
@pytest.mark.parametrize(("model", "field"), GUARDED, ids=GUARDED_IDS)
def test_trigger_rejects_card_numbers_when_the_validator_is_bypassed(
    model: type[models.Model], field: models.Field[Any, Any], path: str
) -> None:
    samples = [text for text in TRIGGER_REJECTED if _fits(field, text)]
    assert samples
    for text in samples:
        with pytest.raises(IntegrityError) as raised, transaction.atomic():
            _bypass(path, model, field, _card_value(field, text))
        message = str(raised.value)
        assert PAYMENT_DATA_MESSAGE in message
        assert text not in message and "1111" not in message


@requires_postgresql
@pytest.mark.django_db
@pytest.mark.parametrize("text", REJECTED)
def test_trigger_rejects_every_rejected_sample(text: str) -> None:
    operator = saved(FACTORIES[ledger_models.Operator]())
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        ledger_models.Operator.objects.filter(pk=operator.pk).update(notes=text)
    assert PAYMENT_DATA_MESSAGE in str(raised.value)


def _accepted_samples(model: type[models.Model], field: models.Field[Any, Any]) -> list[str]:
    """ACCEPTED samples that also satisfy the column's own type and CHECK constraints."""
    samples = [text for text in ACCEPTED if text and _fits(field, text)]
    column = f"{model._meta.db_table}.{field.column}"
    if column == "invoices.invoice_number":
        return [text for text in samples if re.fullmatch(r"INV-[0-9]{4}-[0-9]{4,}", text)]
    if column == "operators.email":
        return [text.lower() for text in samples if text == text.strip()]
    # PLAN-011b D4: a template code and an error class name have a CHECK of their own.
    if column == "message_templates.code":
        return [text for text in samples if re.fullmatch(r"[a-z0-9_]+", text)] + [
            "friendly_reminder_2026"
        ]
    if column == "outbound_messages.error":
        return [text for text in samples if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text)] + [
            "ConnectTimeout"
        ]
    # PLAN-012a D3: an SMS phone is E.164 (CHECK).
    if column == "operators.sms_phone":
        return [text for text in samples if re.fullmatch(ledger_models.SMS_PHONE_PATTERN, text)] + [
            "+14045551234",
            "+447911123456",
        ]
    # PLAN-012 D2: a delete reason is not blank, and only on a deleted entry (CHECK);
    # PLAN-012e D14: a debt-override reason is not blank (CHECK).
    if column in {"contact_entries.delete_reason", "lease_transfers.debt_override_reason"}:
        return [text for text in samples if re.search(r"\S", text)]
    # PLAN-012c D2: the company's names are not blank, and a logo override is a stored
    # document's path (CHECKs); the guard reads each UUID as one letter, so a path passes.
    if column in {
        "company_settings.name",
        "company_settings.short_name",
        "company_settings.processor_name",
    }:
        return [text for text in samples if re.search(r"\S", text)]
    if column == "company_settings.logo":
        return [LOGO_PATH, LOGO_PATH.replace("store", "operator")]
    return samples


def _existing_row(model: type[models.Model], field: models.Field[Any, Any]) -> models.Model:
    """A saved row whose ``field`` may hold text: a contact entry's delete reason only
    exists on a deleted entry (PLAN-012 D2); a transfer's debt-override reason only with
    the override's amount and moment (PLAN-012e D14)."""
    column = f"{model._meta.db_table}.{field.column}"
    if column == "contact_entries.delete_reason":
        return saved(
            FACTORIES[model](
                deleted_at=timezone.now(), deleted_by=make_user(), delete_reason="Wrong shop."
            )
        )
    if column == "lease_transfers.debt_override_reason":
        return saved(
            FACTORIES[model](
                debt_override_amount=Decimal("125.00"),
                debt_override_reason="Owner agreed to carry it.",
                debt_override_by=make_user(),
                debt_override_at=timezone.now(),
            )
        )
    return saved(FACTORIES[model]())


@requires_postgresql
@pytest.mark.django_db
@pytest.mark.parametrize(("model", "field"), GUARDED, ids=GUARDED_IDS)
def test_trigger_accepts_ordinary_text_when_the_validator_is_bypassed(
    model: type[models.Model], field: models.Field[Any, Any]
) -> None:
    samples = _accepted_samples(model, field)
    assert samples
    table, column = model._meta.db_table, str(field.column)
    is_json = isinstance(field, models.JSONField)
    placeholder = "%s::jsonb" if is_json else "%s"
    existing = None if table == "audit_log" else _existing_row(model, field)
    for text in samples:
        value = _card_value(field, text)
        parameter = json.dumps(value) if is_json else value
        # provider_status may change only inside the sync writer (PLAN-004 §4.4).
        writer = provider_write() if column == "provider_status" else transaction.atomic()
        with writer:
            if existing is None:  # audit_log is append-only: insert instead
                _audit_log_insert(column, placeholder, parameter)
                continue
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {table} SET {column} = {placeholder} WHERE id = %s",  # noqa: S608
                    [parameter, existing.pk],
                )


@requires_postgresql
@pytest.mark.django_db
@pytest.mark.parametrize("text", REJECTED + ACCEPTED)
def test_database_function_agrees_with_the_validator(text: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT finance_contains_card_number(%s)", [text])
        row = cursor.fetchone()
    assert row is not None
    assert row[0] is contains_card_number(text)


@requires_postgresql
@pytest.mark.django_db
def test_database_function_handles_null_and_unicode_digits() -> None:
    full_width = "".join(chr(0xFF10 + int(d)) for d in "4111111111111111")
    extended_arabic_indic = "".join(chr(0x06F0 + int(d)) for d in "4111111111111111")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT finance_contains_card_number(NULL), finance_contains_card_number(%s), "
            "finance_contains_card_number(%s)",
            [full_width, extended_arabic_indic],
        )
        assert cursor.fetchone() == (False, True, True)


def test_digits_of_other_scripts_are_read_by_the_application() -> None:
    # PLAN-004 §4.6 point 5: the known difference. Devanagari digits are digits here; the
    # database reads only ASCII, full-width and the two Arabic-Indic scripts.
    devanagari = "".join(chr(0x0966 + int(d)) for d in "4111111111111111")
    assert contains_card_number(devanagari)


@requires_postgresql
@pytest.mark.django_db
def test_database_function_is_linear_in_the_length_of_the_text() -> None:
    hostile = [
        "1   " * 25_000,
        "1 " * 50_000,
        "\u200b" * 100_000,
        "0a1b2c3d-4e5f-1234-5678-123456789012" * 2_000,
        "1\r\n" * 50_000,
    ]
    started = time.monotonic()
    with connection.cursor() as cursor:
        for text in hostile:
            cursor.execute("SELECT finance_contains_card_number(%s)", [text])
    assert time.monotonic() - started < 5.0


@requires_postgresql
@pytest.mark.django_db
def test_database_function_agrees_with_the_validator_on_the_amended_alphabet() -> None:
    # PLAN-004 §4.6 point 5: every separator, invisible character, UUID shape and digit
    # script the plan names, mixed at random; application and database must agree.
    rng = random.Random(46)  # noqa: S311 - reproducible test data, not security
    pieces = [
        *" \t -‐‑‒–—―\n",
        "\r\n",
        "\r",
        *"​‌‍⁠﻿",
        *".:/xAf",
        "0a1b2c3d-4e5f-1234-5678-123456789012",
        "0A1B2C3D-4E5F-1234-5678-12345678901",
    ]
    scripts = (0x30, 0xFF10, 0x0660, 0x06F0)
    samples = []
    for _ in range(4_000):
        digits = [rng.randint(0, 9) for _ in range(rng.randint(10, 22))]
        if rng.random() < 0.5 and 13 <= len(digits) <= 19:
            digits = [int(d) for d in luhn_complete("".join(map(str, digits[:-1])))]
        script = rng.choice(scripts)
        text = ""
        for digit in digits:
            text += chr(script + digit)
            text += "".join(rng.choice(pieces) for _ in range(rng.choice((0, 0, 1, 2, 3, 4))))
        samples.append(text)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT finance_contains_card_number(t) FROM unnest(%s::text[]) WITH ORDINALITY "
            "AS x(t, n) ORDER BY n",
            [samples],
        )
        database = [row[0] for row in cursor.fetchall()]
    application = [contains_card_number(text) for text in samples]
    assert any(application) and not all(application)
    disagreements = [
        repr(samples[i])
        for i, (a, d) in enumerate(zip(application, database, strict=True))
        if a != d
    ]
    assert disagreements == []


# --- Bidirectional marks (PLAN-010d Batch A finding, ledger 0020) --------------------------

BIDI_MARKS = [
    chr(code) for code in (0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
]


def test_the_bidi_marks_are_invisible_to_the_guard() -> None:
    assert len(BIDI_MARKS) == 11
    for mark in BIDI_MARKS:
        assert mark in INVISIBLE_CHARACTERS
        assert contains_card_number(mark.join(["4111", "1111", "1111", "1111"])), repr(mark)
        with pytest.raises(ValidationError):
            validate_no_payment_data(f"ref {mark}4111 1111{mark} 1111 1111")


@requires_postgresql
@pytest.mark.django_db
def test_the_database_function_reads_through_the_bidi_marks_as_the_validator() -> None:
    samples = [mark.join(["4111", "1111", "1111", "1111"]) for mark in BIDI_MARKS]
    samples += [mark.join("4111111111111112") for mark in BIDI_MARKS]  # Luhn fails
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT finance_contains_card_number(t) FROM unnest(%s::text[]) WITH ORDINALITY "
            "AS x(t, n) ORDER BY n",
            [samples],
        )
        database = [row[0] for row in cursor.fetchall()]
    assert database == [contains_card_number(text) for text in samples]
    assert database == [True] * 11 + [False] * 11


@requires_postgresql
@pytest.mark.django_db
def test_the_trigger_refuses_a_card_number_split_by_bidi_marks() -> None:
    operator = saved(LEDGER_FACTORIES[ledger_models.Operator]())
    text = "‮".join(["4111", "1111", "1111", "1111"])
    with pytest.raises(IntegrityError), transaction.atomic():
        ledger_models.Operator.objects.filter(pk=operator.pk).update(notes=text)
