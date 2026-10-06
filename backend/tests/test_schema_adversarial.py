"""Adversarial cases for PLAN-004, written by the QA owner (plan §6 rows 11 to 17)."""

from __future__ import annotations

import ast
import random
import threading
import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections, models, transaction

from ledger import models as ledger
from ledger.validators import (
    contains_card_number,
    validate_json_has_no_payment_data,
    validate_no_payment_data,
)
from tests.test_card_number_guard import luhn_break, luhn_complete
from tests.test_ledger_models import (
    new_lease,
    new_operator,
    new_payment,
    new_rent_charge,
    requires_postgresql,
    saved,
)

OUR_APPS = ("ledger", "payments")


def _rng(seed: int) -> random.Random:
    """Reproducible test data, not security."""
    return random.Random(seed)  # noqa: S311


CARD = "4111111111111111"

#: Columns that hold provider identifiers or hashes. They are not free text, and a
#: false positive there would break the sync, so the guard must not cover them.
IDENTIFIER_COLUMNS = {
    ("external_refs", "external_id"),
    ("deposits", "provider_deposit_id"),
    ("webhook_events", "provider_object_id"),
    ("webhook_events", "dedupe_hash"),
    ("audit_log", "entity_id"),
    ("import_audit", "target_id"),
    # A file's SHA-256 as 64 hex characters (PLAN-010c D4); a CHECK holds it to that shape.
    ("documents", "sha256"),
    # The sent e-mail's body digest (64 hex) and Gmail's message id (PLAN-011b D4); both
    # held to their shape by a CHECK.
    ("outbound_messages", "body_sha256"),
    ("outbound_messages", "mail_message_id"),
}


def _our_models() -> list[type[models.Model]]:
    return [model for label in OUR_APPS for model in apps.get_app_config(label).get_models()]


def _name(field: models.Field[Any, Any]) -> str:
    return str(field.column)


def _columns() -> list[tuple[str, models.Field[Any, Any]]]:
    return [
        (model._meta.db_table, field)
        for model in _our_models()
        for field in model._meta.concrete_fields
    ]


# --- 11: what the schema must never contain ---------------------------------------------


def test_no_column_names_a_provider_or_payment_data() -> None:
    forbidden = ("stax", "card_number", "cardnumber", "cvv", "cvc", "routing", "account_number")
    offenders = [
        f"{table}.{_name(field)}"
        for table, field in _columns()
        if any(word in _name(field).lower() or word in table.lower() for word in forbidden)
    ]
    assert offenders == []


def test_business_tables_hold_no_provider_identifier_column() -> None:
    allowed = {"external_refs", "deposits", "webhook_events", "provider_snapshots"}
    offenders = [
        f"{table}.{_name(field)}"
        for table, field in _columns()
        if table not in allowed
        and ("provider" in _name(field) or "external" in _name(field))
        and _name(field) not in {"provider_status", "provider_created_at", "external_label"}
    ]
    assert offenders == []


def test_every_open_text_column_is_guarded() -> None:
    """Any column that can hold 13 free characters is covered by the guard (Rule 1)."""
    unguarded = []
    for table, field in _columns():
        if not isinstance(field, models.CharField | models.TextField):
            continue
        if field.choices or (table, _name(field)) in IDENTIFIER_COLUMNS:
            continue
        if field.max_length is not None and field.max_length < 13:
            continue
        if validate_no_payment_data not in field.validators:
            unguarded.append(f"{table}.{_name(field)}")
    assert unguarded == []


def test_every_json_column_is_guarded() -> None:
    unguarded = [
        f"{table}.{_name(field)}"
        for table, field in _columns()
        if isinstance(field, models.JSONField)
        and validate_json_has_no_payment_data not in field.validators
    ]
    assert unguarded == []


# --- 12: evasion ------------------------------------------------------------------------

EVASIONS = {
    "full-width digits": "".join(chr(0xFF10 + int(d)) for d in CARD),
    "arabic-indic digits": "".join(chr(0x0660 + int(d)) for d in CARD),
    "double spaces": "4111  1111  1111  1111",
    "tabs": "4111\t1111\t1111\t1111",
    "no-break spaces": chr(0xA0).join(["4111", "1111", "1111", "1111"]),
    "en dashes": chr(0x2013).join(["4111", "1111", "1111", "1111"]),
    "space hyphen space": "4111 - 1111 - 1111 - 1111",
    "zero-width spaces": chr(0x200B).join(["4111", "1111", "1111", "1111"]),
    "zero-width joiner in every gap": chr(0x200D).join(CARD),
    "byte order mark": chr(0xFEFF).join(["4111", "1111", "1111", "1111"]),
    "inside a sentence": f"paid with card {CARD} on the phone",
    "glued to letters": f"ref{CARD}x",
    "two lines": "4111 1111\n1111 1111",
    "windows line break": "4111 1111\r\n1111 1111",
    "long padding": "x" * 5000 + " " + CARD + " " + "y" * 5000,
}


@pytest.mark.parametrize("text", EVASIONS.values(), ids=EVASIONS.keys())
def test_validator_sees_through_evasion(text: str) -> None:
    assert contains_card_number(text)
    with pytest.raises(ValidationError) as raised:
        validate_no_payment_data(text)
    assert CARD not in str(raised.value)
    assert text not in str(raised.value)


@pytest.mark.parametrize(
    "value",
    [
        {"memo": CARD},
        {CARD: "as a key"},
        {"nested": [{"deep": {"deeper": ["4111 1111", "x", f"card {CARD}"]}}]},
        {"split": "4111 1111\n1111 1111"},
        [CARD],
    ],
)
def test_json_validator_finds_a_card_number_anywhere(value: Any) -> None:
    with pytest.raises(ValidationError):
        validate_json_has_no_payment_data(value)


def test_hostile_input_is_checked_quickly() -> None:
    hostile = [
        "1 " * 100_000,
        "1-" * 100_000,
        "1" * 200_000,
        "4111 1111 1111 111" * 10_000,
        chr(0x200B) * 100_000 + CARD,
        "1\n" * 100_000,
    ]
    started = time.monotonic()
    for text in hostile:
        contains_card_number(text)
    assert time.monotonic() - started < 2.0


# --- 13: false positives ------------------------------------------------------------------


def _ordinary_texts(rng: random.Random) -> list[str]:
    texts = []
    for _ in range(2_500):
        area, mid, last = rng.randint(200, 989), rng.randint(200, 999), rng.randint(0, 9999)
        texts.append(f"call +1 ({area}) {mid}-{last:04d} ext {rng.randint(1, 9999)}")
        texts.append(f"INV-{rng.randint(2024, 2035)}-{rng.randint(1, 99999):04d} sent")
        texts.append(
            f"rent {rng.randint(500, 12581)}.{rng.randint(0, 99):02d} due "
            f"{rng.randint(2024, 2030)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        )
        texts.append(f"store {rng.randint(1, 9999)} {rng.choice(['paid', 'closed', 'wire'])}")
    return texts


def test_ordinary_staff_text_is_never_rejected() -> None:
    rejected = [text for text in _ordinary_texts(_rng(20260929)) if contains_card_number(text)]
    assert rejected == []


def test_provider_style_identifiers_are_never_rejected() -> None:
    """UUIDs appear in every provider payload; a rejected one would break the sync."""
    rng = _rng(4)
    all_digit_tail = [
        f"{rng.getrandbits(32):08x}-{rng.getrandbits(16):04x}-"
        f"{rng.randint(0, 9999):04d}-{rng.randint(0, 9999):04d}-{rng.randint(0, 10**12 - 1):012d}"
        for _ in range(3_000)
    ]
    ordinary = [str(uuid.UUID(int=rng.getrandbits(128))) for _ in range(3_000)]
    rejected = [value for value in all_digit_tail + ordinary if contains_card_number(value)]
    assert rejected == []
    for value in all_digit_tail[:200]:
        validate_json_has_no_payment_data({"id": value, "customer_id": value, "items": [value]})


@pytest.mark.parametrize(
    "text",
    [
        luhn_break("411111111111111"),
        luhn_complete("12345678901"),  # 12 digits
        luhn_complete("1234567890123456789"),  # 20 digits
        "INV-2026-0001",
        "+1 415 555 0132",
        "12581.00",
        "2026-09-29 19:00",
        "store 2475 and store 2629 and store 3409 and store 2204",
    ],
)
def test_named_false_positive_cases_are_accepted(text: str) -> None:
    validate_no_payment_data(text)


# --- 14: application and database agree (PostgreSQL) ------------------------------------


@requires_postgresql
@pytest.mark.django_db
def test_application_and_database_agree_on_random_text() -> None:
    rng = _rng(14)
    separators = ["", "", " ", "-", "  ", "\t", "\n", chr(0xA0), chr(0x200B), ".", "/", "x"]
    samples = list(EVASIONS.values())
    for _ in range(5_000):
        digits = [str(rng.randint(0, 9)) for _ in range(rng.randint(10, 22))]
        if rng.random() < 0.5 and 13 <= len(digits) <= 19:
            digits = list(luhn_complete("".join(digits[:-1])))
        samples.append("".join(d + rng.choice(separators) for d in digits))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT finance_contains_card_number(t) FROM unnest(%s::text[]) WITH ORDINALITY "
            "AS x(t, n) ORDER BY n",
            [samples],
        )
        database = [row[0] for row in cursor.fetchall()]
    application = [contains_card_number(text) for text in samples]
    disagreements = [
        i for i, pair in enumerate(zip(application, database, strict=True)) if pair[0] != pair[1]
    ]
    assert disagreements == []


@requires_postgresql
@pytest.mark.django_db
@pytest.mark.parametrize("text", EVASIONS.values(), ids=EVASIONS.keys())
def test_database_trigger_sees_through_evasion(text: str) -> None:
    operator = saved(new_operator())
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        ledger.Operator.objects.filter(pk=operator.pk).update(notes=text)
    assert CARD not in str(raised.value)


@requires_postgresql
@pytest.mark.django_db
@pytest.mark.parametrize("column", ["display_name", "company", "address_line1", "legal_name"])
def test_database_trigger_covers_short_text_columns(column: str) -> None:
    operator = saved(new_operator())
    with pytest.raises(IntegrityError), transaction.atomic():
        ledger.Operator.objects.filter(pk=operator.pk).update(**{column: CARD})


# --- 15: no inexact arithmetic --------------------------------------------------------------


def test_new_application_code_uses_no_float(repo_root: Path) -> None:
    offenders = []
    for package in OUR_APPS:
        for path in (repo_root / "backend" / package).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                is_float_literal = isinstance(node, ast.Constant) and isinstance(node.value, float)
                is_float_name = isinstance(node, ast.Name) and node.id == "float"
                is_float_field = isinstance(node, ast.Attribute) and node.attr == "FloatField"
                if is_float_literal or is_float_name or is_float_field:
                    offenders.append(f"{path.relative_to(repo_root)}:{getattr(node, 'lineno', 0)}")
    assert offenders == []


def test_every_money_column_is_numeric_12_2() -> None:
    money_names = (
        "amount",
        "subtotal",
        "tax",
        "total",
        "gross",
        "fees",
        "net",
        "unit_price",
        "balance",
    )
    wrong = []
    for table, field in _columns():
        if not any(name in _name(field) for name in money_names):
            continue
        target = getattr(field, "output_field", field)
        if not isinstance(target, models.DecimalField) or (
            target.max_digits,
            target.decimal_places,
        ) != (12, 2):
            wrong.append(f"{table}.{_name(field)}")
    assert wrong == []


@pytest.mark.django_db
def test_cent_sums_stay_exact() -> None:
    charge = saved(new_rent_charge(amount_due=Decimal("0.30"), amount_paid=Decimal("0.10")))
    charge.refresh_from_db()
    assert charge.balance == Decimal("0.20")
    assert str(charge.balance) == "0.20"


# --- 16: a lease with an unknown channel never becomes active ----------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["save", "update", "bulk_update", "bulk_create", "raw"])
def test_unknown_channel_lease_cannot_become_active(path: str) -> None:
    lease = saved(
        new_lease(
            collection_status=ledger.CollectionStatus.PAUSED,
            payment_channel=ledger.PaymentChannel.UNKNOWN,
        )
    )
    active = ledger.CollectionStatus.ACTIVE
    with pytest.raises(IntegrityError), transaction.atomic():
        if path == "save":
            lease.collection_status = active
            lease.save()
        elif path == "update":
            ledger.Lease.objects.filter(pk=lease.pk).update(collection_status=active)
        elif path == "bulk_update":
            lease.collection_status = active
            ledger.Lease.objects.bulk_update([lease], ["collection_status"])
        elif path == "bulk_create":
            ledger.Lease.objects.bulk_create(
                [
                    new_lease(
                        collection_status=active,
                        payment_channel=ledger.PaymentChannel.UNKNOWN,
                    )
                ]
            )
        else:
            with connection.cursor() as cursor:
                # sqlite stores a UUID as 32 hexadecimal characters.
                key = str(lease.pk) if connection.vendor == "postgresql" else lease.pk.hex
                cursor.execute(
                    "UPDATE leases SET collection_status = %s WHERE id = %s", [active, key]
                )
    lease.refresh_from_db()
    assert lease.collection_status == ledger.CollectionStatus.PAUSED


@pytest.mark.django_db
def test_manual_payment_without_a_note_is_refused_on_every_manual_method() -> None:
    for method in (
        ledger.PaymentMethod.ZELLE,
        ledger.PaymentMethod.WIRE,
        ledger.PaymentMethod.OTHER,
    ):
        for note in ("", "   ", "\n\t"):
            with pytest.raises(IntegrityError), transaction.atomic():
                saved(new_payment(method=method, note=note))


# --- 17: the duplicate guard under concurrency (PostgreSQL) -------------------------------


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_inserts_of_one_charge_leave_exactly_one() -> None:
    lease = saved(new_lease())
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def insert() -> None:
        try:
            barrier.wait(timeout=10)
            with transaction.atomic():
                ledger.RentCharge.objects.create(
                    lease=lease,
                    period_month=date(2026, 10, 1),
                    seq=1,
                    due_on=date(2026, 10, 5),
                    amount_due=Decimal("3800.00"),
                    status=ledger.RentChargeStatus.DUE,
                    local_ref=uuid.uuid4(),
                )
            outcomes.append("created")
        except IntegrityError:
            outcomes.append("refused")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=insert) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["created", "refused"]
    assert ledger.RentCharge.objects.filter(lease=lease).count() == 1
