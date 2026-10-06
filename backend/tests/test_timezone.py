"""New York time (docs/qa/PLAN-011b D1, §7): the project's zone, the dates taken from
provider timestamps, and ``tz_audit`` / ``tz_apply`` on rows at 23:30 and 03:30 UTC on both
sides of midnight in New York, exactly at midnight UTC, and on the DST days 2026-03-08 and
2026-11-01.
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from django.core.management import CommandError, call_command
from django.utils import timezone

from finance import env_guard
from ledger.models import (
    AuditLog,
    Invoice,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from payments import timezone_audit
from payments.webhooks.handlers import transaction as transaction_handler
from tests.receivables_helpers import an_operator

pytestmark = pytest.mark.django_db

D = Decimal


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# --- The setting --------------------------------------------------------------------------


def test_the_project_zone_is_new_york_with_utc_storage() -> None:
    assert settings.TIME_ZONE == "America/New_York" == env_guard.PROJECT_TIME_ZONE
    assert settings.USE_TZ is True
    assert timezone.now().utcoffset() == timedelta(0)


def test_the_reconciliation_zone_defaults_to_the_project_zone() -> None:
    assert env_guard.DEFAULT_RECONCILE_TIMEZONE == env_guard.PROJECT_TIME_ZONE
    config = env_guard.validate(
        {
            "APP_ENV": "local",
            "STAX_ENV": "sandbox",
            "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
            "STAX_API_KEY": "FAKE-TEST-KEY",
            "DATABASE_URL": "sqlite:///:memory:",
            "APP_BASE_URL": "http://localhost:8000",
            "DJANGO_SECRET_KEY": "test-only-not-secret",
        }
    )
    assert config.reconcile_timezone == settings.TIME_ZONE


@pytest.mark.parametrize(
    ("moment", "new_york_day"),
    [
        # 23:30 UTC is 19:30 the same day in New York (EDT).
        (utc(2026, 10, 6, 23, 30), date(2026, 10, 6)),
        # 03:30 UTC is 23:30 the day before in New York.
        (utc(2026, 10, 7, 3, 30), date(2026, 10, 6)),
        # Exactly midnight UTC is 20:00 the day before.
        (utc(2026, 10, 7, 0, 0), date(2026, 10, 6)),
        # 2026-03-08: clocks go forward at 07:00 UTC. 04:59 UTC is 23:59 EST on the 7th,
        # 05:00 UTC is midnight EST on the 8th, 07:30 UTC is 03:30 EDT on the 8th.
        (utc(2026, 3, 8, 4, 59), date(2026, 3, 7)),
        (utc(2026, 3, 8, 5, 0), date(2026, 3, 8)),
        (utc(2026, 3, 8, 7, 30), date(2026, 3, 8)),
        # 2026-11-01: clocks go back at 06:00 UTC. 03:59 UTC is 23:59 EDT on Oct 31,
        # 04:00 UTC is midnight EDT on Nov 1, 06:30 UTC is 01:30 EST on Nov 1.
        (utc(2026, 11, 1, 3, 59), date(2026, 10, 31)),
        (utc(2026, 11, 1, 4, 0), date(2026, 11, 1)),
        (utc(2026, 11, 1, 6, 30), date(2026, 11, 1)),
    ],
)
def test_a_provider_timestamp_gives_its_new_york_date(moment: datetime, new_york_day: date) -> None:
    # The date a payment is initiated or settled on (the transaction handler).
    assert transaction_handler._local_date(moment) == new_york_day
    assert timezone.localdate(moment) == new_york_day


def test_today_is_the_new_york_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timezone, "now", lambda: utc(2026, 10, 7, 2, 0))
    assert timezone.localdate() == date(2026, 10, 6)
    monkeypatch.setattr(timezone, "now", lambda: utc(2026, 10, 7, 4, 0))
    assert timezone.localdate() == date(2026, 10, 7)


# --- tz_audit / tz_apply -------------------------------------------------------------------


def ach_payment(
    *,
    created: datetime | None,
    initiated_on: date | None,
    settled_on: date | None = None,
    settled_at: str | None = None,
    method: str = PaymentMethod.ACH,
) -> Payment:
    return Payment.objects.create(
        kind=PaymentKind.RENT,
        method=method,
        amount=D("100.00"),
        status=PaymentStatus.SETTLED if settled_on else PaymentStatus.PENDING,
        initiated_on=initiated_on,
        settled_on=settled_on,
        provider_created_at=created,
        raw_summary={"settled_at": settled_at} if settled_at is not None else {},
        note="Received." if method != PaymentMethod.ACH else "",
    )


def invoice(created: datetime, issue_date: date, *, legacy: bool = False) -> Invoice:
    row = Invoice.objects.create(
        invoice_number=f"INV-2026-{uuid.uuid4().int % 100000 + 10000:05d}",
        operator=an_operator(),
        issue_date=issue_date,
        due_date=date(2026, 12, 31),
        subtotal=D("100.00"),
        total=D("100.00"),
        legacy=legacy,
    )
    Invoice.objects.filter(pk=row.pk).update(created_at=created)
    row.refresh_from_db()
    return row


def fixture_rows() -> dict[str, Any]:
    """Rows on both sides of midnight in New York; ``changes`` are the ones to realign."""
    rows: dict[str, Any] = {}
    # initiated_on, stored as the UTC date (the old setting).
    rows["evening"] = ach_payment(created=utc(2026, 10, 6, 23, 30), initiated_on=date(2026, 10, 6))
    rows["late_night"] = ach_payment(
        created=utc(2026, 10, 7, 3, 30), initiated_on=date(2026, 10, 7)
    )  # changes to 10-06
    rows["midnight"] = ach_payment(
        created=utc(2026, 10, 7, 0, 0), initiated_on=date(2026, 10, 7)
    )  # changes to 10-06
    rows["already_new_york"] = ach_payment(
        created=utc(2026, 10, 8, 3, 30), initiated_on=date(2026, 10, 7)
    )
    rows["dst_spring"] = ach_payment(
        created=utc(2026, 3, 8, 4, 30), initiated_on=date(2026, 3, 8)
    )  # changes to 03-07
    rows["dst_spring_after"] = ach_payment(
        created=utc(2026, 3, 8, 5, 30), initiated_on=date(2026, 3, 8)
    )
    rows["dst_fall"] = ach_payment(
        created=utc(2026, 11, 1, 3, 59), initiated_on=date(2026, 11, 1)
    )  # changes to 10-31
    rows["dst_fall_after"] = ach_payment(
        created=utc(2026, 11, 1, 4, 0), initiated_on=date(2026, 11, 1)
    )
    # settled_on from the settlement time in raw_summary (naive is UTC).
    rows["settled_late"] = ach_payment(
        created=utc(2026, 10, 1, 15, 0),
        initiated_on=date(2026, 10, 1),
        settled_on=date(2026, 10, 6),
        settled_at="2026-10-06 02:15:00",
    )  # settled_on changes to 10-05
    rows["settled_day"] = ach_payment(
        created=utc(2026, 10, 1, 15, 0),
        initiated_on=date(2026, 10, 1),
        settled_on=date(2026, 10, 6),
        settled_at="2026-10-06T23:30:00+00:00",
    )
    rows["settled_no_time"] = ach_payment(
        created=utc(2026, 10, 1, 15, 0),
        initiated_on=date(2026, 10, 1),
        settled_on=date(2026, 10, 6),
    )
    # A date that matches neither: not a time-zone artefact, never rewritten.
    rows["other"] = ach_payment(created=utc(2026, 10, 7, 3, 30), initiated_on=date(2026, 9, 1))
    # Manual payments: entered by staff, never audited.
    rows["zelle"] = ach_payment(
        created=None, initiated_on=date(2026, 10, 7), method=PaymentMethod.ZELLE
    )
    # Invoices.
    rows["invoice_late"] = invoice(utc(2026, 10, 7, 1, 0), date(2026, 10, 7))  # to 10-06
    rows["invoice_day"] = invoice(utc(2026, 10, 7, 12, 0), date(2026, 10, 7))
    rows["invoice_legacy"] = invoice(utc(2026, 10, 7, 1, 0), date(2026, 10, 7), legacy=True)
    rows["changes"] = {
        ("payments.initiated_on", rows["late_night"].pk): (date(2026, 10, 7), date(2026, 10, 6)),
        ("payments.initiated_on", rows["midnight"].pk): (date(2026, 10, 7), date(2026, 10, 6)),
        ("payments.initiated_on", rows["dst_spring"].pk): (date(2026, 3, 8), date(2026, 3, 7)),
        ("payments.initiated_on", rows["dst_fall"].pk): (date(2026, 11, 1), date(2026, 10, 31)),
        ("payments.settled_on", rows["settled_late"].pk): (date(2026, 10, 6), date(2026, 10, 5)),
        ("invoices.issue_date", rows["invoice_late"].pk): (date(2026, 10, 7), date(2026, 10, 6)),
    }
    return rows


def audit_output() -> str:
    out = io.StringIO()
    call_command("tz_audit", stdout=out)
    return out.getvalue()


def test_the_audit_lists_exactly_the_rows_whose_new_york_date_differs() -> None:
    rows = fixture_rows()
    found = timezone_audit.audit()
    listed = {(change.column, change.row_id): (change.old, change.new) for change in found.changes}
    assert listed == rows["changes"]
    assert found.total == 6
    assert found.counts() == {
        "payments.initiated_on": 4,
        "payments.settled_on": 1,
        "invoices.issue_date": 1,
    }
    assert [(c.column, c.row_id) for c in found.other] == [
        ("payments.initiated_on", rows["other"].pk)
    ]
    assert found.no_source["payments.settled_on"] == 1


def test_the_audit_prints_masked_ids_old_and_new_counts_and_the_columns_unchanged() -> None:
    rows = fixture_rows()
    text = audit_output()
    late = str(rows["late_night"].pk)
    masked = f"{late[:4]}…{late[-4:]}"
    assert f"payments.initiated_on {masked} 2026-10-07 -> 2026-10-06" in text
    assert "payments.settled_on " in text and "2026-10-06 -> 2026-10-05" in text
    assert "invoices.issue_date " in text
    assert "total: 6" in text
    assert "To apply: tz_apply --audited 6" in text
    for column in (
        "rent_charges.due_on",
        "one_off_charges.due_date",
        "invoices.issue_date (legacy rows)",
        "payments.initiated_on and settled_on (Zelle, wire, other)",
    ):
        assert f"  {column}: " in text, column
    # Never a full identifier.
    for name in ("late_night", "midnight", "invoice_late", "other", "zelle"):
        assert str(rows[name].pk) not in text, name
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text)


def test_the_audit_is_read_only() -> None:
    fixture_rows()
    before = list(Payment.objects.order_by("pk").values_list("pk", "initiated_on", "settled_on"))
    audits = AuditLog.objects.count()
    audit_output()
    assert list(Payment.objects.order_by("pk").values_list("pk", "initiated_on", "settled_on")) == (
        before
    )
    assert AuditLog.objects.count() == audits


@pytest.mark.parametrize("given", ["0", "5", "7", "-1"])
def test_apply_refuses_unless_the_count_matches_and_changes_nothing(given: str) -> None:
    rows = fixture_rows()
    with pytest.raises(CommandError) as raised:
        call_command("tz_apply", "--audited", given, stdout=io.StringIO())
    if given != "-1":
        assert "does not match the current audit's total of 6" in str(raised.value)
    assert "Nothing was changed" in str(raised.value)
    rows["late_night"].refresh_from_db()
    assert rows["late_night"].initiated_on == date(2026, 10, 7)
    assert not AuditLog.objects.filter(action=timezone_audit.ACTION).exists()


def test_apply_without_a_count_is_refused() -> None:
    with pytest.raises(CommandError):
        call_command("tz_apply", stdout=io.StringIO())


def test_apply_rewrites_exactly_the_listed_rows_with_an_audit_row_each() -> None:
    rows = fixture_rows()
    untouched = {
        name: (rows[name].initiated_on, rows[name].settled_on)
        for name in ("evening", "already_new_york", "other", "zelle", "settled_day")
    }
    out = io.StringIO()
    call_command("tz_apply", "--audited", "6", stdout=out)
    assert "Rows realigned: 6" in out.getvalue()
    for (column, pk), (old, new) in rows["changes"].items():
        table, field = column.split(".")
        model = Payment if table == "payments" else Invoice
        assert getattr(model.objects.get(pk=pk), field) == new, column
        entry = AuditLog.objects.get(action=timezone_audit.ACTION, entity_id=str(pk))
        assert entry.before == {field: old.isoformat()}
        assert entry.after is not None and entry.after[field] == new.isoformat()
        assert entry.after["time_zone"] == "America/New_York"
    assert AuditLog.objects.filter(action=timezone_audit.ACTION).count() == 6
    for name, dates in untouched.items():
        rows[name].refresh_from_db()
        assert (rows[name].initiated_on, rows[name].settled_on) == dates, name
    rows["invoice_legacy"].refresh_from_db()
    assert rows["invoice_legacy"].issue_date == date(2026, 10, 7)
    # Applied once, the audit is empty: a second run is refused unless told 0.
    assert timezone_audit.audit().total == 0
    with pytest.raises(CommandError):
        call_command("tz_apply", "--audited", "6", stdout=io.StringIO())
    call_command("tz_apply", "--audited", "0", stdout=io.StringIO())
    assert AuditLog.objects.filter(action=timezone_audit.ACTION).count() == 6


def test_a_row_changed_during_the_run_rolls_everything_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = fixture_rows()
    real_audit = timezone_audit.audit

    def audit_then_change(*, locked: bool = False) -> timezone_audit.Audit:
        found = real_audit(locked=locked)
        # Another writer moves one listed row after it was read.
        Payment.objects.filter(pk=rows["dst_fall"].pk).update(initiated_on=date(2026, 9, 1))
        return found

    monkeypatch.setattr(timezone_audit, "audit", audit_then_change)
    with pytest.raises(CommandError, match="Nothing was changed"):
        call_command("tz_apply", "--audited", "6", stdout=io.StringIO())
    monkeypatch.undo()
    rows["late_night"].refresh_from_db()
    assert rows["late_night"].initiated_on == date(2026, 10, 7)
    assert not AuditLog.objects.filter(action=timezone_audit.ACTION).exists()


def test_an_empty_database_audits_to_zero() -> None:
    text = audit_output()
    assert "total: 0" in text and "To apply: tz_apply --audited 0" in text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-06 02:15:00", utc(2026, 10, 6, 2, 15)),
        ("2026-10-06T02:15:00-04:00", utc(2026, 10, 6, 6, 15)),
        ("", None),
        ("not a date", None),
        (None, None),
        (12, None),
    ],
)
def test_the_settlement_time_is_read_as_the_adapter_reads_it(
    raw: object, expected: datetime | None
) -> None:
    assert timezone_audit.parse_timestamp(raw) == expected
