"""The CSV export of every list page (docs/qa/PLAN-011-dashboard-reports-mobile.md D5, D10):
each exports the rows its page shows under the same filters, every page of them, with
readable headers, formula-safe text, masked identifiers, and the page's own role."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from accounts.roles import Role
from ledger.models import Operator, PaymentStatus, RentChargeStatus
from ledger.services import csv_export
from payments.models import (
    MismatchKind,
    ReconciliationMismatch,
    ReconciliationRun,
    ReconciliationStatus,
    ReconciliationTrigger,
)
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_operator,
)
from tests.test_operator_views import client_for
from tests.test_webhooks import stored

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 6)
SEP, OCT = date(2026, 9, 1), date(2026, 10, 1)


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def records(
    client: Client, path: str, params: dict[str, str] | None = None
) -> list[dict[str, str]]:
    response = client.get(path, params or {})
    assert response.status_code == 200, path
    assert response["Content-Disposition"].startswith('attachment; filename="'), path
    body = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
    return list(csv.DictReader(io.StringIO(body)))


def viewer() -> Client:
    return client_for(Role.VIEWER)[0]


def test_the_operators_export_follows_the_search_and_archived_filters() -> None:
    an_operator("=Formula Operator")
    an_operator("Beta Stores")
    archived = an_operator("Beta Archived")
    Operator.objects.filter(pk=archived.pk).update(archived_at=timezone.now())
    for n in range(30):  # more than one page of the list
        an_operator(f"Bulk {n:02d}")
    client = viewer()
    every = records(client, "/operators/export.csv")
    assert len(every) == 32  # every page, the archived one left out
    names = [row["Operator"] for row in every]
    assert "'=Formula Operator" in names and "Beta Archived" not in names
    assert names.index("Beta Stores") < names.index("Bulk 00")  # the list's order
    beta = records(client, "/operators/export.csv", {"q": "beta"})
    assert [row["Operator"] for row in beta] == ["Beta Stores"]
    with_archived = records(client, "/operators/export.csv", {"q": "beta", "archived": "1"})
    assert [row["Operator"] for row in with_archived] == ["Beta Archived", "Beta Stores"]
    assert with_archived[0]["Archived on"] != "" and with_archived[1]["Archived on"] == ""
    page = client.get("/operators/", {"q": "beta", "archived": "1"}).content.decode()
    assert 'href="/operators/export.csv?q=beta&amp;archived=1"' in page


def test_the_stores_export_follows_the_search() -> None:
    a_store("Alpha Mall", 101, "GA")
    a_store("Beta Plaza", 102, "FL")
    client = viewer()
    rows = records(client, "/stores/export.csv", {"q": "plaza"})
    assert [(row["Store"], row["Store number"], row["State"]) for row in rows] == [
        ("Beta Plaza", "102", "FL")
    ]
    assert len(records(client, "/stores/export.csv")) == 2
    assert 'href="/stores/export.csv?q=plaza"' in client.get("/stores/?q=plaza").content.decode()


def test_the_leases_export_follows_the_operator_filter_with_the_rent_in_effect(
    today: date,
) -> None:
    mine = an_operator("Mine")
    a_lease(a_store("Store 1", 1), mine, rent="3500.00", debit_day=5, second_debit_day=20)
    a_lease(a_store("Store 2", 2), an_operator("Theirs"))
    rows = records(viewer(), "/leases/export.csv", {"operator": str(mine.pk)})
    [row] = rows
    assert (row["Store"], row["Operator"], row["Rent now (USD)"]) == ("Store 1", "Mine", "3500.00")
    assert (row["Debit day"], row["Second debit day"], row["Started on"]) == (
        "5",
        "20",
        "2025-01-01",
    )
    assert len(records(viewer(), "/leases/export.csv", {"operator": "not-a-uuid"})) == 2


def test_the_run_export_lists_the_debits_due_today(today: date) -> None:
    created = a_lease(a_store("Due A", 1), debit_day=6)
    a_rent_charge(created, OCT, RentChargeStatus.PENDING, day=6)
    a_lease(a_store("Due B", 2), debit_day=6)
    a_lease(a_store("Tomorrow", 3), debit_day=7)
    rows = records(viewer(), "/run/export.csv")
    by_store = {row["Store"]: row for row in rows}
    assert set(by_store) == {"Due A", "Due B"}
    assert by_store["Due A"]["Status"] == "Pending"
    assert by_store["Due B"]["Status"] == "Not created"
    assert by_store["Due A"]["Amount (USD)"] == "3500.00"
    assert 'href="/run/export.csv"' in viewer().get("/run/").content.decode()


def test_the_rent_charges_export_of_one_lease(today: date) -> None:
    lease = a_lease(a_store("Charged", 9))
    a_rent_charge(lease, SEP, RentChargeStatus.PARTIAL, "1000.00", "400.00")
    a_rent_charge(lease, OCT, RentChargeStatus.UNPAID, "1000.00")
    a_rent_charge(a_lease(), OCT, RentChargeStatus.UNPAID, "1.00")
    rows = records(viewer(), "/charges/export.csv", {"lease": str(lease.pk)})
    assert [(row["Rent month"], row["Balance (USD)"], row["Status"]) for row in rows] == [
        ("2026-10", "1000.00", "Unpaid"),
        ("2026-09", "600.00", "Partially paid"),
    ]
    by_operator = records(viewer(), "/charges/export.csv", {"operator": str(lease.operator_id)})
    assert len(by_operator) == 2
    assert len(records(viewer(), "/charges/export.csv", {"lease": "x"})) == 3
    # PLAN-012d D4: the export link moved to the store page's Charges tab.
    page = viewer().get(f"/stores/{lease.store_id}/").content.decode()
    assert f'href="/charges/export.csv?store={lease.store_id}"' in page
    assert len(records(viewer(), "/charges/export.csv", {"store": str(lease.store_id)})) == 2


def test_the_one_off_export_of_an_operator_and_of_a_lease(today: date) -> None:
    operator = an_operator("One-off Operator")
    lease = a_lease(operator=operator)
    a_one_off_charge(operator, "120.00", SEP, lease=lease, description="+Broken door")
    a_one_off_charge(operator, "80.00", OCT)
    a_one_off_charge(an_operator("Other"), "5.00", OCT)
    client = viewer()
    mine = records(client, "/one-off-charges/export.csv", {"operator": str(operator.pk)})
    assert [(row["Description"], row["Price (USD)"]) for row in mine] == [
        ("Broken shelf", "80.00"),
        ("'+Broken door", "120.00"),
    ]
    on_lease = records(client, "/one-off-charges/export.csv", {"lease": str(lease.pk)})
    assert [row["Store"] for row in on_lease] == [lease.store.external_label]
    page = client.get(f"/operators/{operator.pk}/").content.decode()
    assert f'href="/one-off-charges/export.csv?operator={operator.pk}"' in page


def test_the_returns_export_lists_the_open_returns(today: date) -> None:
    charge = a_rent_charge(a_lease(a_store("Returned", 5)), SEP, RentChargeStatus.UNPAID)
    payment = a_payment(charge, "3500.00", None, PaymentStatus.RETURNED, initiated_on=SEP)
    payment.return_code = "R01"
    payment.returned_at = datetime(2026, 9, 10, 12, tzinfo=UTC)
    payment.save(update_fields=["return_code", "returned_at"])
    [row] = records(viewer(), "/returns/export.csv")
    assert (row["Store"], row["Return code"], row["Amount (USD)"]) == ("Returned", "R01", "3500.00")
    assert row["Returned at"] == "2026-09-10T12:00:00+00:00"


def test_the_webhooks_export_masks_identifiers_and_follows_the_filters() -> None:
    stored({"id": "object-identifier-0001"}, "update_invoice")
    stored({"id": "object-identifier-0002"}, "create_transaction")
    client = viewer()
    rows = records(client, "/webhooks/export.csv", {"type": "update_invoice"})
    [row] = rows
    assert row["Event type"] == "update_invoice"
    assert row["Provider object (masked)"] == "obje…0001"
    response = client.get("/webhooks/export.csv")
    assert "object-identifier" not in b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]


def _mismatch(**fields: Any) -> ReconciliationMismatch:
    run = ReconciliationRun.objects.create(
        trigger=ReconciliationTrigger.MANUAL,
        status=ReconciliationStatus.DONE,
        started_at=datetime(2026, 10, 1, 6, tzinfo=UTC),
    )
    values: dict[str, Any] = {
        "run": run,
        "kind": MismatchKind.PENDING_TOO_OLD,
        "entity_type": "payment",
        "entity_id": uuid.uuid4(),
        "detail": "=Pending for 9 business days.",
    }
    values.update(fields)
    return ReconciliationMismatch.objects.create(**values)


def test_the_reconciliation_export_needs_billing_like_its_page() -> None:
    found = _mismatch()
    assert viewer().get("/reconciliation/export.csv").status_code == 403
    [row] = records(client_for(Role.BILLING)[0], "/reconciliation/export.csv")
    assert row["Mismatch"] == "Pending too long"
    assert row["Detail"] == "'=Pending for 9 business days."
    assert str(found.entity_id) not in row["Record (masked)"]


def test_every_list_page_links_its_export(today: date) -> None:
    lease = a_lease()
    client = client_for(Role.ADMIN)[0]
    pages = {
        "/operators/": "/operators/export.csv",
        "/stores/": "/stores/export.csv",
        "/leases/": "/leases/export.csv",
        "/run/": "/run/export.csv",
        "/returns/": "/returns/export.csv",
        "/webhooks/": "/webhooks/export.csv",
        "/reconciliation/": "/reconciliation/export.csv",
        "/payments/": "/payments/export.csv",
        "/audit/": "/audit/export.csv",
        "/grid/": "/grid/export.csv",
        "/arrears/": "/arrears/export.csv",
        f"/stores/{lease.store_id}/?tab=documents": (
            f"/documents/stores/{lease.store_id}/export.csv"
        ),
        f"/leases/{lease.pk}/": f"/documents/leases/{lease.pk}/export.csv",
    }
    for page, export in pages.items():
        assert f'href="{export}' in client.get(page).content.decode(), page


def test_the_cap_applies_to_list_exports(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    monkeypatch.setattr(csv_export, "CSV_MAX_ROWS", 2)
    lease = a_lease()
    for month in (date(2026, m, 1) for m in (6, 7, 8)):
        a_rent_charge(lease, month)
    rows = records(viewer(), "/charges/export.csv", {"lease": str(lease.pk)})
    assert rows[-1]["Store"] == csv_export.TRUNCATED and len(rows) == 3
