"""The payments ledger, its CSV export and the operator filters
(docs/qa/PLAN-009-payments-ledger.md D4, D9).

Payments are written directly, as the rent run and the webhook handlers would leave
them; nothing here calls a provider.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    ChargeNote,
    Lease,
    Operator,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
)
from ledger.services import payments_ledger as ledger
from ledger.services import rent_run
from payments.models import ExternalRef
from tests.auth_helpers import iter_routes
from tests.test_operator_views import client_for
from tests.test_rent_run import charge_of, lease
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

D = Decimal
DAY = date(2026, 10, 15)
LIST = "/payments/"
EXPORT = "/payments/export.csv"
TRANSACTION = "txn-ABCDEFGH-12345678"
TRICKY_NOTE = 'Wire, "urgent"\nsecond line, with commas'


def a_charge(**overrides: Any) -> RentCharge:
    row = lease(payment_channel=PaymentChannel.WIRE, **overrides)
    rent_run.create_charges(DAY, actor=None)
    return charge_of(row)


def payment(charge: RentCharge, amount: str, **fields: Any) -> Payment:
    values: dict[str, Any] = {
        "rent_charge": charge,
        "kind": PaymentKind.RENT,
        "method": PaymentMethod.ACH,
        "amount": D(amount),
        "status": PaymentStatus.SETTLED,
        "initiated_on": date(2026, 10, 15),
        "settled_on": date(2026, 10, 17),
    }
    values.update(fields)
    return Payment.objects.create(**values)


@pytest.fixture
def book() -> dict[str, Any]:
    """Two operators' charges with one payment of every kind."""
    first = a_charge()
    second = a_charge()
    settled = payment(first, "1500.00")
    ExternalRef.objects.create(
        entity_type="payment",
        entity_id=settled.pk,
        provider="stax_pay",
        external_kind="transaction",
        external_id=TRANSACTION,
    )
    pending = payment(second, "700.00", status=PaymentStatus.PENDING, settled_on=None)
    returned = payment(second, "300.00", status=PaymentStatus.RETURNED, return_code="R01")
    voided = payment(second, "50.00", status=PaymentStatus.VOIDED)
    refund = payment(
        first, "200.00", is_refund=True, parent_payment=settled, settled_on=date(2026, 10, 20)
    )
    wire = payment(
        second,
        "400.00",
        method=PaymentMethod.WIRE,
        initiated_on=date(2026, 9, 30),
        settled_on=date(2026, 9, 30),
        reference="=SUM(A1)",
        note=TRICKY_NOTE,
    )
    return {
        "first": first,
        "second": second,
        "settled": settled,
        "pending": pending,
        "returned": returned,
        "voided": voided,
        "refund": refund,
        "wire": wire,
    }


def ids(rows: Any) -> set[uuid.UUID]:
    return {row.pk for row in rows}


def body(response: Any) -> str:
    return b"".join(response.streaming_content).decode("utf-8")


def parse(response: Any) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(body(response), newline="")))


def operator_of(charge: RentCharge) -> Operator:
    operator = Lease.objects.get(pk=charge.lease_id).operator
    assert operator is not None
    return operator


# --- Totals ------------------------------------------------------------------------------------


def test_totals_keep_settled_pending_returned_and_refunded_apart(book: dict[str, Any]) -> None:
    totals = ledger.totals(ledger.ledger_payments(ledger.PaymentFilter()))
    assert totals == ledger.Totals(
        settled=D("1900.00"),
        pending=D("700.00"),
        returned=D("300.00"),
        refunded=D("200.00"),
        count=6,
    )


def test_pending_is_never_counted_as_collected(book: dict[str, Any]) -> None:
    only_pending = ledger.totals(
        ledger.ledger_payments(ledger.PaymentFilter(status=PaymentStatus.PENDING))
    )
    assert (only_pending.settled, only_pending.pending) == (D("0.00"), D("700.00"))


def test_a_voided_refund_is_not_counted_as_refunded(book: dict[str, Any]) -> None:
    payment(book["first"], "10.00", is_refund=True, parent_payment=book["settled"],
            status=PaymentStatus.VOIDED)  # fmt: skip
    assert ledger.totals(ledger.ledger_payments(ledger.PaymentFilter())).refunded == D("200.00")


# --- Filters -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("settled", {"settled", "wire"}),
        ("pending", {"pending"}),
        ("returned", {"returned"}),
        ("voided", {"voided"}),
        ("refunded", {"refund"}),
    ],
)
def test_the_status_filter(book: dict[str, Any], status: str, expected: set[str]) -> None:
    rows = ledger.ledger_payments(ledger.parse_filter({"status": status}))
    assert ids(rows) == {book[name].pk for name in expected}


def test_method_operator_and_store_filters(book: dict[str, Any]) -> None:
    wire = ledger.ledger_payments(ledger.parse_filter({"method": "wire"}))
    assert ids(wire) == {book["wire"].pk}
    operator = operator_of(book["first"])
    by_operator = ledger.ledger_payments(ledger.parse_filter({"operator": str(operator.pk)}))
    assert ids(by_operator) == {book["settled"].pk, book["refund"].pk}
    store = Lease.objects.get(pk=book["second"].lease_id).store_id
    by_store = ledger.ledger_payments(ledger.parse_filter({"store": str(store)}))
    assert ids(by_store) == {book[name].pk for name in ("pending", "returned", "voided", "wire")}


def test_date_range_by_paid_on_or_initiated_on(book: dict[str, Any]) -> None:
    paid = ledger.ledger_payments(
        ledger.parse_filter({"date_from": "2026-10-16", "date_to": "2026-10-17"})
    )
    assert ids(paid) == {book[name].pk for name in ("settled", "returned", "voided")}
    initiated = ledger.ledger_payments(
        ledger.parse_filter(
            {"date_by": "initiated", "date_from": "2026-10-15", "date_to": "2026-10-15"}
        )
    )
    assert book["pending"].pk in ids(initiated) and book["wire"].pk not in ids(initiated)


@pytest.mark.parametrize(
    "params",
    [
        {"status": "lost"},
        {"method": "card"},
        {"operator": "not-a-uuid"},
        {"store": "1 OR 1=1"},
        {"date_from": "2026-13-01"},
        {"date_to": "yesterday"},
        {"date_by": "created"},
    ],
)
def test_unusable_filter_values_are_ignored(book: dict[str, Any], params: dict[str, str]) -> None:
    assert ledger.parse_filter(params) == ledger.PaymentFilter()


# --- The page ----------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_every_role_reads_the_ledger_and_the_totals(book: dict[str, Any], role: Role) -> None:
    client, _ = client_for(role)
    page = client.get(LIST).content.decode()
    # PLAN-014 A6: the totals are five tiles, the figures keep their ids.
    assert '<span class="tile-value" id="total-settled">$1,900.00</span>' in page
    assert '<span class="tile-value" id="total-pending">$700.00</span>' in page
    assert '<span class="tile-value" id="total-returned">$300.00</span>' in page
    assert '<span class="tile-value" id="total-refunded">$200.00</span>' in page
    assert "txn-…5678" in page and TRANSACTION not in page


def test_the_page_filters_by_operator_and_keeps_the_filter_in_the_export_link(
    book: dict[str, Any],
) -> None:
    client, _ = client_for(Role.VIEWER)
    operator = operator_of(book["first"])
    page = client.get(LIST, {"operator": str(operator.pk)}).content.decode()
    assert f'<option value="{operator.pk}" selected>' in page
    assert "Totals of the 2 payments shown" in page
    assert f"{EXPORT}?operator={operator.pk}" in page


def test_the_note_indicator_marks_payments_whose_charge_has_a_note(book: dict[str, Any]) -> None:
    client, _ = client_for(Role.VIEWER)
    assert "note-indicator" not in client.get(LIST).content.decode()
    ChargeNote.objects.create(rent_charge=book["first"], body="Operator asked for a receipt.")
    rows = ledger.rows_for(ledger.ledger_payments(ledger.PaymentFilter()))
    flagged = {row.payment.pk for row in rows if row.has_note}
    assert flagged == {book["settled"].pk, book["refund"].pk}
    assert client.get(LIST).content.decode().count('class="badge note-indicator"') == 2


def test_the_ledger_pages_25_rows_and_keeps_the_filter() -> None:
    charge = a_charge()
    for _ in range(26):
        payment(charge, "1.00", method=PaymentMethod.WIRE, note="Wire.")
    client, _ = client_for(Role.VIEWER)
    first = client.get(LIST, {"method": "wire"}).content.decode()
    assert first.count("<tr>") == 1 + 25  # list head, 25 rows (PLAN-014 A6: totals are tiles)
    assert '<span class="pager-count">1–25 of 26</span>' in first  # PLAN-012f-a D3 footer
    assert "?method=wire&amp;page=2" in first


def test_the_ledger_routes_are_read_only_for_every_role(book: dict[str, Any]) -> None:
    names = {name for name, _, _ in iter_routes() if name and name.startswith("ledger:payments:")}
    # The payment page is read only too; refund and void are admin routes (PLAN-009 D7).
    assert names == {
        "ledger:payments:list",
        "ledger:payments:export",
        "ledger:payments:detail",
        "ledger:payments:refund",
        "ledger:payments:void",
        # PLAN-012f-c D5: billing's void of a payment recorded outside the processor.
        "ledger:payments:void_manual",
    }
    unsafe = {"ledger:payments:refund", "ledger:payments:void", "ledger:payments:void_manual"}
    assert names - unsafe <= (registry.READ_ONLY_ROUTE_NAMES)
    for role in (Role.VIEWER, Role.BILLING, Role.ADMIN):
        client, _ = client_for(role)
        for path in (LIST, EXPORT):
            assert client.post(path).status_code == 403, (role, path)


def test_a_user_without_a_role_or_a_session_reads_nothing(book: dict[str, Any]) -> None:
    nobody, _ = client_for(None)
    for path in (LIST, EXPORT):
        assert nobody.get(path).status_code == 403
        assert Client().get(path).status_code == 401


# --- The export --------------------------------------------------------------------------------


def test_the_export_streams_one_row_per_payment_with_plain_values(book: dict[str, Any]) -> None:
    client, _ = client_for(Role.VIEWER)
    response = client.get(EXPORT)
    assert response.status_code == 200 and response.streaming
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"].startswith('attachment; filename="payments-')
    rows = parse(response)
    assert len(rows) == 6
    assert list(rows[0]) == list(ledger.CSV_HEADER)
    by_id = {row["Payment ID"]: row for row in rows}
    settled = by_id[str(book["settled"].pk)]
    assert (settled["Amount (USD)"], settled["Paid date"], settled["Initiated date"]) == (
        "1500.00",
        "2026-10-17",
        "2026-10-15",
    )
    assert settled["Provider transaction (masked)"] == "txn-…5678"
    assert (settled["Status"], settled["Refund"], settled["Rent month"]) == (
        "settled",
        "no",
        "2026-10",
    )
    refund = by_id[str(book["refund"].pk)]
    assert (refund["Refund"], refund["Refund of payment ID"]) == ("yes", str(book["settled"].pk))
    wire = by_id[str(book["wire"].pk)]
    assert wire["Note"] == TRICKY_NOTE
    assert wire["Reference"] == "'=SUM(A1)"
    pending = by_id[str(book["pending"].pk)]
    assert (pending["Paid date"], pending["Status"]) == ("", "pending")


def test_the_export_never_holds_an_unmasked_provider_id(book: dict[str, Any]) -> None:
    client, _ = client_for(Role.VIEWER)
    text = body(client.get(EXPORT))
    assert TRANSACTION not in text and not has_token(text, "12345678")


def test_the_export_applies_the_same_filter_as_the_page(book: dict[str, Any]) -> None:
    client, _ = client_for(Role.BILLING)
    rows = parse(client.get(EXPORT, {"status": "refunded"}))
    assert [row["Payment ID"] for row in rows] == [str(book["refund"].pk)]
    operator = operator_of(book["second"])
    by_operator = parse(client.get(EXPORT, {"operator": str(operator.pk), "method": "ach"}))
    assert {row["Payment ID"] for row in by_operator} == {
        str(book[name].pk) for name in ("pending", "returned", "voided")
    }


def test_an_empty_export_has_the_header_only() -> None:
    client, _ = client_for(Role.VIEWER)
    text = body(client.get(EXPORT))
    assert text == ",".join(ledger.CSV_HEADER) + "\r\n"


def test_the_export_reads_in_chunks(book: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger, "EXPORT_CHUNK", 2)
    rows = list(csv.reader(io.StringIO("".join(ledger.csv_lines(ledger.PaymentFilter())))))
    assert len(rows) == 7


# --- D9 on the lease list ----------------------------------------------------------------------


def test_the_lease_list_filters_by_operator_and_marks_charge_notes() -> None:
    first = a_charge()
    second = a_charge()
    ChargeNote.objects.create(rent_charge=first, body="Promised to pay on Friday.")
    client, _ = client_for(Role.VIEWER)
    operator = operator_of(first)
    page = client.get("/leases/", {"operator": str(operator.pk)}).content.decode()
    first_lease = Lease.objects.get(pk=first.lease_id)
    second_lease = Lease.objects.get(pk=second.lease_id)
    assert f"/leases/{first_lease.pk}/" in page and f"/leases/{second_lease.pk}/" not in page
    assert 'class="badge note-indicator"' in page
    everything = client.get("/leases/", {"operator": "junk"}).content.decode()
    assert f"/leases/{second_lease.pk}/" in everything
    assert everything.count('class="badge note-indicator"') == 1
