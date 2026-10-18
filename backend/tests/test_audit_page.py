"""The audit log page, its CSV export and the Activity lists of the Documents sections
(docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D8, D9; §6 test plan items for the
audit page, happy paths, boundaries and error paths; the adversarial cases are the QA
owner's file).
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from ledger.models import AuditLog, Lease, Operator, Store
from ledger.services import audit, csv_export
from ledger.services import documents as documents_service
from ledger.views import audit as audit_views
from ledger.views.reports import ReportTooSlow
from tests.document_files import EXE, PNG
from tests.test_documents import do_upload
from tests.test_ledger_models import new_lease, new_store, saved
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

#: Wednesday 2026-10-07 16:30 UTC is 12:30 EDT in New York.
NOW = datetime(2026, 10, 7, 16, 30, tzinfo=UTC)
TODAY = date(2026, 10, 7)
PROVIDER_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
#: The query-string range that holds NOW.
WEEK = {"from": "2026-10-01", "to": "2026-10-07"}


@contextmanager
def at(moment: datetime) -> Iterator[None]:
    """Rows written in the block carry ``moment`` as their time."""
    with mock.patch("django.utils.timezone.now", return_value=moment):
        yield


def log(
    moment: datetime = NOW,
    *,
    action: str = "operator.updated",
    entity_type: str = "operator",
    entity_id: str = "",
    actor: User | None = None,
    before: Any = None,
    after: Any = None,
) -> AuditLog:
    with at(moment):
        return AuditLog.objects.create(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id or str(uuid.uuid4()),
            actor=actor,
            before=before,
            after=after,
        )


def admin() -> tuple[Client, User]:
    return client_for(Role.ADMIN)


def page(client: Client, **params: str) -> str:
    response = client.get("/audit/", {**WEEK, **params})
    assert response.status_code == 200
    return str(response.content.decode())


def export(client: Client, **params: str) -> list[list[str]]:
    response = client.get("/audit/export.csv", {**WEEK, **params})
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    body = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
    return list(csv.reader(io.StringIO(body)))


def query(**params: str) -> audit.AuditQuery:
    return audit.parse_query(params, today=TODAY)


# --- Roles ----------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_billing_and_viewer_get_403_on_the_page_and_the_export(role: Role) -> None:
    client, _ = client_for(role)
    assert client.get("/audit/").status_code == 403
    assert client.get("/audit/export.csv").status_code == 403


def test_an_admin_gets_the_page_and_no_unsafe_method() -> None:
    client, _ = admin()
    assert client.get("/audit/").status_code == 200
    assert client.post("/audit/").status_code == 403
    assert client.post("/audit/export.csv").status_code == 403


def test_the_nav_link_is_shown_to_admins_only() -> None:
    assert 'id="nav-audit"' in admin()[0].get("/operators/").content.decode()
    for role in (Role.BILLING, Role.VIEWER):
        assert 'id="nav-audit"' not in client_for(role)[0].get("/operators/").content.decode()


# --- The query ------------------------------------------------------------------------------


def test_the_default_range_is_the_last_seven_new_york_days() -> None:
    found = query()
    assert (found.date_from, found.date_to, found.messages) == (
        date(2026, 10, 1),
        TODAY,
        (),
    )
    assert found.start == datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    assert found.end == datetime(2026, 10, 8, 4, 0, tzinfo=UTC)


def test_366_days_are_allowed_and_more_are_capped() -> None:
    full = query(**{"from": "2025-10-07", "to": "2026-10-07"})
    assert (full.date_from, full.messages) == (date(2025, 10, 7), ())
    ten_years = query(**{"from": "2016-10-07", "to": "2026-10-07"})
    assert ten_years.date_from == date(2025, 10, 7)
    assert ten_years.messages == (audit.CAPPED.format(days=366, start="2025-10-07"),)


def test_swapped_and_unreadable_dates_are_replaced_with_a_message() -> None:
    swapped = query(**{"from": "2026-10-07", "to": "2026-10-01"})
    assert (swapped.date_from, swapped.date_to) == (date(2026, 10, 1), TODAY)
    assert swapped.messages == (audit.SWAPPED,)
    for raw in ("2026-13-01", "yesterday", "2026-10-07T00:00", "٢٠٢٦-١٠-٠٧"):
        bad = query(**{"from": raw})
        assert (bad.date_from, bad.date_to) == (date(2026, 10, 1), TODAY), raw
        assert bad.messages == (audit.UNREADABLE_DATES.format(days=7),)


@pytest.mark.parametrize(
    "dates",
    [
        {"from": "0001-01-01"},
        {"to": "0001-01-01"},
        {"from": "9999-12-31"},
        {"to": "9999-12-31"},
        {"from": "0000-00-00"},
        {"from": "2026-10-01", "to": "9999-12-31"},
        {"from": "0001-01-01", "to": "9999-12-31"},
        {"from": "1999-12-31", "to": "2026-10-07"},
        {"from": "2026-10-01", "to": "2101-01-01"},
    ],
)
def test_years_outside_2000_to_2100_are_unreadable(dates: dict[str, str]) -> None:
    found = query(**dates)
    assert (found.date_from, found.date_to) == (date(2026, 10, 1), TODAY)
    assert found.messages == (audit.UNREADABLE_DATES.format(days=7),)
    assert found.start < found.end


def test_the_first_and_last_readable_years_never_overflow() -> None:
    first = query(**{"from": "2000-01-01", "to": "2000-01-01"})
    assert (first.date_from, first.date_to, first.messages) == (
        date(2000, 1, 1),
        date(2000, 1, 1),
        (),
    )
    assert first.start == datetime(2000, 1, 1, 5, 0, tzinfo=UTC)
    early = query(**{"to": "2000-01-01"})
    assert (early.date_from, early.date_to) == (date(1999, 12, 26), date(2000, 1, 1))
    assert early.start < early.end
    last = query(**{"from": "2100-12-31", "to": "2100-12-31"})
    assert last.end == datetime(2101, 1, 1, 5, 0, tzinfo=UTC)
    wide = query(**{"from": "2000-01-01", "to": "2100-12-31"})
    assert wide.date_from == date(2099, 12, 31)
    assert wide.messages == (audit.CAPPED.format(days=366, start="2099-12-31"),)


@pytest.mark.parametrize(
    "dates",
    [
        {"from": "0001-01-01"},
        {"to": "9999-12-31"},
        {"from": "0000-00-00", "to": "0000-00-00"},
        {"from": "2026-10-01", "to": "9999-12-31"},
    ],
)
def test_extreme_years_answer_200_on_the_page_and_the_export(dates: dict[str, str]) -> None:
    client, _ = admin()
    response = client.get("/audit/", dates)
    assert response.status_code == 200
    assert "The dates could not be read" in response.content.decode()
    exported = client.get("/audit/export.csv", dates)
    assert exported.status_code == 200
    b"".join(exported.streaming_content)  # type: ignore[attr-defined]


def test_a_filter_over_200_characters_is_ignored_with_a_message() -> None:
    long = query(action="a" * 201, entity_id="x" * 10_000)
    assert (long.action, long.entity_id) == ("", "")
    assert long.messages == (
        audit.TOO_LONG.format(limit=200, name="action"),
        audit.TOO_LONG.format(limit=200, name="entity_id"),
    )
    assert query(action="a" * 200).action == "a" * 200


def test_the_page_number_is_bounded() -> None:
    assert query(page="0").page == 1
    assert query(page="-3").page == 1
    assert query(page="２").page == 1
    capped = query(page="999999")
    assert capped.page == audit.MAX_PAGE
    assert capped.messages == (audit.PAGE_CAPPED.format(limit=audit.MAX_PAGE),)


# --- Filters --------------------------------------------------------------------------------


def actions_on(**params: str) -> list[str]:
    found = audit.parse_query(params, today=TODAY)
    return [row.action for row in audit.rows(found).rows]


def test_filters_by_action_exact_or_prefix_user_type_and_id() -> None:
    _, user = admin()
    operator_id = str(uuid.uuid4())
    log(action="document.uploaded", entity_type="document", actor=user)
    log(action="document.downloaded", entity_type="document")
    log(action="operator.created", entity_id=operator_id)
    log(action="documents.other", entity_type="x")
    assert actions_on(action="document.uploaded") == ["document.uploaded"]
    assert actions_on(action="document.*") == [
        "document.downloaded",
        "document.uploaded",
    ]
    assert actions_on(action="document.") == []  # exact without the star
    assert actions_on(user=user.get_username()) == ["document.uploaded"]
    assert actions_on(entity_type="x") == ["documents.other"]
    assert actions_on(entity_id=operator_id) == ["operator.created"]
    assert actions_on(entity_id=f"{operator_id[:8]}*") == ["operator.created"]


def test_percent_and_underscore_in_entity_id_are_plain_characters() -> None:
    log(action="a.one", entity_id="ab%cd")
    log(action="a.two", entity_id="abXcd")
    log(action="a.three", entity_id="ab_cd")
    assert actions_on(entity_id="ab%*") == ["a.one"]
    assert actions_on(entity_id="ab_cd") == ["a.three"]
    assert actions_on(entity_id="ab_*") == ["a.three"]


def test_an_unknown_user_answers_200_with_a_message_and_no_rows() -> None:
    log()
    client, _ = admin()
    body = page(client, user="nobody-here")
    assert audit.UNKNOWN_USER in body
    assert 'class="audit-row"' not in body


def test_the_range_includes_both_new_york_days() -> None:
    # 2026-10-01 00:30 New York is 04:30 UTC; 2026-09-30 23:30 New York is 03:30 UTC.
    log(datetime(2026, 10, 1, 4, 30, tzinfo=UTC), action="in.first")
    log(datetime(2026, 10, 1, 3, 30, tzinfo=UTC), action="out.before")
    log(datetime(2026, 10, 8, 3, 59, tzinfo=UTC), action="in.last")
    log(datetime(2026, 10, 8, 4, 0, tzinfo=UTC), action="out.after")
    assert actions_on() == ["in.last", "in.first"]


def test_rows_are_newest_first_100_a_page() -> None:
    for n in range(101):
        log(NOW - timedelta(minutes=n), action=f"step.{n:03d}")
    first = audit.rows(query())
    assert len(first.rows) == 100 and first.has_next
    assert first.rows[0].action == "step.000" and first.rows[-1].action == "step.099"
    second = audit.rows(query(page="2"))
    assert [row.action for row in second.rows] == ["step.100"] and not second.has_next
    client, _ = admin()
    body = page(client)
    assert 'id="audit-next"' in body and 'id="audit-previous"' not in body
    body = page(client, page="2")
    assert 'id="audit-previous"' in body and 'id="audit-next"' not in body


def test_the_page_reads_a_bounded_number_of_queries() -> None:
    for _ in range(30):
        log(action="document.uploaded", entity_type="document")
    with CaptureQueriesContext(connection) as captured:
        audit.rows(query(user="nobody"))
    small = len(captured)
    with CaptureQueriesContext(connection) as captured:
        audit.rows(query())
    assert len(captured) <= 3
    assert small <= 2


# --- What a row shows -----------------------------------------------------------------------


def test_a_row_shows_the_new_york_time_masks_identifiers_and_keeps_money_as_text() -> None:
    operator = Operator.objects.create(display_name="Northwind Stores")
    _, user = admin()
    log(
        action="invoice.provider_created",
        entity_type="operator",
        entity_id=str(operator.pk),
        actor=user,
        before={"status": "draft"},
        after={
            "provider_invoice": "fake-invoice-123456",
            "customer": PROVIDER_ID,
            "total": "3000.00",
            "invoice_number": "INV-2026-0042",
            "due": "2026-10-15",
            "sent": True,
            "masked_already": "abcd…wxyz",
        },
    )
    client, _ = admin()
    body = page(client)
    # One time, New York's (copy list §4.6: "Time (UTC)" → "Time"); the export keeps both.
    assert "2026-10-07 16:30:00" not in body
    assert "2026-10-07 12:30:00 EDT" in body
    assert user.get_username() in body
    assert str(operator.pk) not in body.replace(f"/operators/{operator.pk}/", "")
    assert f'href="/operators/{operator.pk}/"' in body
    assert "fake-invoice-123456" not in body and "fake…3456" in body
    assert PROVIDER_ID not in body and "0f8f…950e" in body
    assert "3000.00" in body and "INV-2026-0042" in body and "2026-10-15" in body
    assert "abcd…wxyz" in body


def test_identifier_shapes() -> None:
    for value in (PROVIDER_ID, "fake-invoice-12", "cus_9f8e7d6c5b", "pm-bank-one-off-1"):
        assert audit.looks_like_identifier(value), value
    for value in ("2026-10-15", "INV-2026-0042", "autopay_due", "3000.00", "abc", "pm-1", "ok"):
        assert not audit.looks_like_identifier(value), value


def test_a_document_row_links_to_its_owner() -> None:
    store = saved(new_store())
    memory = MemoryDocumentStorage()
    with mock.patch.object(documents_storage, "get_storage", return_value=memory):
        _, user = client_for(Role.BILLING)
        assert do_upload(store, user, "plan.png", PNG).ok
    client, _ = admin()
    today = audit.today_in_new_york()
    body = page(
        client,
        action="document.uploaded",
        **{"from": (today - timedelta(days=1)).isoformat(), "to": today.isoformat()},
    )
    assert f'href="/stores/{store.pk}/"' in body


# --- CSV ------------------------------------------------------------------------------------


def test_the_csv_has_the_filtered_rows_masked_and_compact() -> None:
    log(action="kept.row", after={"provider_invoice": "fake-invoice-123456", "total": "5.00"})
    log(action="other.row")
    client, _ = admin()
    lines = export(client, action="kept.*")
    assert lines[0] == list(audit.CSV_HEADER)
    [row] = lines[1:]
    assert row[3] == "kept.row"
    assert json.loads(row[8]) == {"provider_invoice": "fake…3456", "total": "5.00"}
    assert row[8] == '{"provider_invoice":"fake…3456","total":"5.00"}'
    assert row[7] == ""
    assert "…" in row[6]
    assert row[1] == "2026-10-07T16:30:00+00:00" and row[2] == "2026-10-07T12:30:00-04:00"


def test_csv_cells_are_formula_safe() -> None:
    log(action="=HYPERLINK(1)", entity_type="+sum", after={"a": "-1"})
    lines = export(admin()[0])
    [row] = lines[1:]
    assert row[3] == "'=HYPERLINK(1)" and row[5] == "'+sum"
    assert row[8] == '{"a":"-1"}'


def test_the_csv_is_capped_with_a_last_line_saying_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit, "CSV_MAX_ROWS", 3)
    for n in range(5):
        log(NOW - timedelta(minutes=n), action=f"step.{n}")
    lines = export(admin()[0])
    assert [line[3] for line in lines[1:4]] == ["step.0", "step.1", "step.2"]
    assert lines[4] == [csv_export.TRUNCATED, csv_export.TRUNCATED_TEXT.format(limit=3)]
    assert len(lines) == 5
    monkeypatch.setattr(audit, "CSV_MAX_ROWS", 5)
    assert len(export(admin()[0])) == 6


def test_the_csv_of_an_unknown_user_has_the_header_only() -> None:
    log()
    assert export(admin()[0], user="nobody") == [list(audit.CSV_HEADER)]


# --- Statement timeout ----------------------------------------------------------------------


def test_a_cancelled_query_answers_200_with_the_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    def too_slow(compute: Any) -> Any:
        raise ReportTooSlow

    monkeypatch.setattr(audit_views, "bounded", too_slow)
    client, _ = admin()
    for url in ("/audit/", "/audit/export.csv"):
        response = client.get(url)
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/html")
        assert 'id="audit-too-slow"' in response.content.decode()


@pytest.mark.skipif(connection.vendor != "postgresql", reason="the timeout is PostgreSQL's")
@pytest.mark.parametrize("url", ["/audit/", "/audit/export.csv"])
def test_the_queries_run_under_the_report_timeout(url: str) -> None:
    client, _ = admin()
    with (
        override_settings(REPORT_STATEMENT_TIMEOUT_MS=15000),
        CaptureQueriesContext(connection) as captured,
    ):
        response = client.get(url)
    assert response.status_code == 200
    statements = [q["sql"] for q in captured.captured_queries]
    assert "SET LOCAL statement_timeout = 15000" in statements


# --- Activity lists ---------------------------------------------------------------------------


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> MemoryDocumentStorage:
    memory = MemoryDocumentStorage()
    monkeypatch.setattr(documents_storage, "get_storage", lambda: memory)
    return memory


def test_the_activity_list_shows_uploads_downloads_and_refusals_of_that_record_only(
    storage: MemoryDocumentStorage,
) -> None:
    store, other = saved(new_store()), saved(new_store())
    lease: Lease = saved(new_lease())
    _, billing = client_for(Role.BILLING)
    mine = do_upload(store, billing, "plan.png", PNG)
    assert mine.ok and mine.document is not None
    documents_service.open_for(mine.document, billing, inline=False)
    assert not do_upload(store, billing, "virus.exe", EXE).ok
    assert do_upload(other, billing, "other.png", PNG).ok
    assert not do_upload(other, billing, "other.exe", EXE).ok
    assert do_upload(lease, billing, "lease.png", PNG).ok
    entries = audit.activity_for(store)
    assert [entry.action for entry in entries] == ["upload_refused", "downloaded", "uploaded"]
    assert entries[0].outcome == documents_service.TYPE_NOT_ACCEPTED
    assert entries[1].outcome == "ok"
    assert all(entry.user == billing.get_username() for entry in entries)
    assert [entry.action for entry in audit.activity_for(lease)] == ["uploaded"]
    assert [entry.action for entry in audit.activity_for(other)] == [
        "upload_refused",
        "uploaded",
    ]
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get(f"/stores/{store.pk}/?tab=documents").content.decode()
    assert body.count('class="document-activity"') == 3
    assert 'id="documents-activity-link"' not in body
    admin_client, _ = admin()
    body = admin_client.get(f"/stores/{store.pk}/?tab=documents").content.decode()
    assert 'id="documents-activity-link"' in body
    assert f"documents_of=store%3A{store.pk}" in body
    lease_body = admin_client.get(f"/leases/{lease.pk}/").content.decode()
    assert lease_body.count('class="document-activity"') == 1


def test_the_activity_list_keeps_the_last_ten(storage: MemoryDocumentStorage) -> None:
    store = saved(new_store())
    _, billing = client_for(Role.BILLING)
    upload = do_upload(store, billing, "plan.png", PNG)
    assert upload.document is not None
    for _ in range(12):
        documents_service.open_for(upload.document, billing, inline=False)
    entries = audit.activity_for(store)
    assert len(entries) == 10
    assert {entry.action for entry in entries} == {"downloaded"}


def test_the_audit_link_lists_the_same_rows(storage: MemoryDocumentStorage) -> None:
    store, other = saved(new_store()), saved(new_store())
    _, billing = client_for(Role.BILLING)
    assert do_upload(store, billing, "plan.png", PNG).ok
    assert not do_upload(store, billing, "virus.exe", EXE).ok
    assert do_upload(other, billing, "other.png", PNG).ok
    linked = audit.parse_query(audit.activity_query(store))
    assert [row.action for row in audit.rows(linked).rows] == [
        "document.upload_refused",
        "document.uploaded",
    ]


def test_a_new_record_shows_no_activity(storage: MemoryDocumentStorage) -> None:
    store: Store = saved(new_store())
    assert audit.activity_for(store) == []
    body = client_for(Role.VIEWER)[0].get(f"/stores/{store.pk}/?tab=documents").content.decode()
    assert "No document activity yet." in body
