"""The shared CSV writer and every CSV route (docs/qa/PLAN-011-dashboard-reports-mobile.md D5,
D7, D10): typed cells (money as ``0.00`` from ``Decimal``, ISO dates, formula-safe text, no
float), the reviewed header list, the 50,000-row cap with its ``truncated`` row, every
export's header row in plain English, and every export a read route."""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client

from accounts import registry
from accounts.roles import Role, meets
from ledger.models import Document, DocumentContentType, DocumentKind, Operator
from ledger.services import csv_export
from ledger.services.csv_export import CsvWriter, cell
from tests.auth_helpers import iter_routes
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for

D = Decimal
BACKEND = Path(__file__).resolve().parents[1]

# --- Cells ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (D("1234.5"), "1234.50"),
        (D("-12"), "-12.00"),
        (D("0.10") + D("0.20"), "0.30"),
        (D("1470001.57"), "1470001.57"),
        (D("0.005"), "0.00"),  # banker's rounding of a half cent, never a float
        (None, ""),
        (True, "yes"),
        (False, "no"),
        (7, "7"),
        (date(2026, 8, 31), "2026-08-31"),
        (datetime(2026, 9, 1, 3, 59, tzinfo=UTC), "2026-09-01T03:59:00+00:00"),
        (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001"),
        ("Plain text", "Plain text"),
        ("=HYPERLINK(1)", "'=HYPERLINK(1)"),
        ("+1", "'+1"),
        ("-1", "'-1"),
        ("@sum", "'@sum"),
        ("\tTab", "'\tTab"),
    ],
)
def test_a_cell_is_written_from_its_type(value: object, text: str) -> None:
    assert cell(value) == text


def test_a_negative_amount_stays_a_number_while_negative_text_is_quoted() -> None:
    assert cell(D("-5.00")) == "-5.00" and cell("-5.00") == "'-5.00"


@pytest.mark.parametrize("value", [0.1, float("nan"), object(), b"bytes"])
def test_a_float_or_an_unknown_type_is_refused(value: object) -> None:
    with pytest.raises(TypeError):
        cell(value)


def test_a_float_inside_a_record_stops_the_export() -> None:
    lines = CsvWriter(["Amount (USD)"]).lines([[D("1.00")], [1.0]])
    assert next(lines) == "Amount (USD)\r\n"
    assert next(lines) == "1.00\r\n"
    with pytest.raises(TypeError):
        next(lines)


# --- Headers -------------------------------------------------------------------------------


def test_a_writer_with_an_unreviewed_header_refuses_to_start() -> None:
    with pytest.raises(ValueError, match="amount_due"):
        CsvWriter(["Operator", "amount_due"])


def test_grid_months_are_the_one_other_header_shape() -> None:
    assert csv_export.reviewed("2026-08") and csv_export.reviewed("Store number")
    for name in ("2026-8", "2026-08-01", "Aug 2026", "store_number", ""):
        assert not csv_export.reviewed(name), name


def test_every_reviewed_header_is_plain_english() -> None:
    for name in csv_export.HEADERS:
        assert name and "_" not in name and name == name.strip(), name
        assert not name[0].islower(), name  # a sentence-case heading, e.g. "Note"


# --- The cap -------------------------------------------------------------------------------


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_the_writer_streams_at_most_the_cap_then_a_truncated_row() -> None:
    read = 0

    def records() -> Any:
        nonlocal read
        for n in range(csv_export.CSV_MAX_ROWS + 10):
            read += 1
            yield [n]

    lines = list(CsvWriter(["Payments"]).lines(records()))
    assert len(lines) == csv_export.CSV_MAX_ROWS + 2
    assert lines[-2] == f"{csv_export.CSV_MAX_ROWS - 1}\r\n"
    assert _rows(lines[-1]) == [
        [
            csv_export.TRUNCATED,
            csv_export.TRUNCATED_TEXT.format(limit=csv_export.CSV_MAX_ROWS),
        ]
    ]
    assert read == csv_export.CSV_MAX_ROWS + 1  # stops reading after the one extra row


def test_exactly_the_cap_has_no_truncated_row() -> None:
    lines = list(CsvWriter(["Payments"], limit=3).lines([[1], [2], [3]]))
    assert lines == ["Payments\r\n", "1\r\n", "2\r\n", "3\r\n"]


@pytest.mark.django_db
def test_a_list_export_over_the_cap_ends_with_the_truncated_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(csv_export, "CSV_MAX_ROWS", 3)
    for n in range(5):
        an_operator(f"Operator {n}")
    rows = export_rows(client_for(Role.VIEWER)[0], "/operators/export.csv")
    assert [row[0] for row in rows[1:4]] == ["Operator 0", "Operator 1", "Operator 2"]
    assert rows[4] == [csv_export.TRUNCATED, csv_export.TRUNCATED_TEXT.format(limit=3)]
    assert len(rows) == 5


# --- Every CSV route -----------------------------------------------------------------------


def export_rows(client: Client, path: str, params: dict[str, str] | None = None) -> list[list[str]]:
    response = client.get(path, params or {})
    assert response.status_code == 200, (path, response.status_code)
    assert response["Content-Type"] == "text/csv; charset=utf-8", path
    assert response["Content-Disposition"].startswith("attachment;"), path
    assert set(response["Cache-Control"].split(", ")) == {"no-store", "private"}, path
    body = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
    return _rows(body)


def csv_routes() -> dict[str, str]:
    """Every CSV route: its name and raw pattern."""
    return {
        name: raw
        for name, raw, _ in iter_routes()
        if name and name.startswith("ledger:") and raw.endswith(".csv")
    }


EXPECTED_ROUTES = {
    "ledger:payments:export",
    "ledger:reports:pnl_export",
    "ledger:reports:aging_export",
    "ledger:reports:revenue_export",
    "ledger:reports:payments_by_method_export",
    "ledger:reports:deposits_export",
    "ledger:operators:statement_export",
    "ledger:audit:export",
    "ledger:grid:export",
    "ledger:arrears:export",
    "ledger:operators:export",
    "ledger:stores:export",
    "ledger:leases:export",
    "ledger:run:export",
    "ledger:charges:export",
    "ledger:one_off_charges:export",
    "ledger:returns:export",
    "ledger:webhooks:export",
    "ledger:reconciliation:export",
    "ledger:documents:export_store",
    "ledger:documents:export_lease",
    # PLAN-011b D7: an operator's contact log.
    "ledger:operators:contacts_export",
    # PLAN-012 D8: the dispute list.
    "ledger:disputes:export",
}
#: Exports whose page needs more than a viewer: the export needs the same.
EXPORT_ROLES = {
    "ledger:audit:export": Role.ADMIN,
    "ledger:reconciliation:export": Role.BILLING,
    "ledger:disputes:export": Role.BILLING,
}


def test_every_list_and_report_has_its_csv_route() -> None:
    assert set(csv_routes()) == EXPECTED_ROUTES


def test_every_csv_route_is_a_read_route_in_the_registry() -> None:
    for name in csv_routes():
        assert name in registry.READ_ONLY_ROUTE_NAMES, name
        assert registry.rule_for(name) is None, name


def _paths() -> dict[str, str]:
    lease = a_lease(a_store("Store 7"), an_operator("Operator 7"))
    owners = {
        "ledger:operators:statement_export": str(lease.operator_id),
        "ledger:operators:contacts_export": str(lease.operator_id),
        "ledger:documents:export_store": str(lease.store_id),
        "ledger:documents:export_lease": str(lease.pk),
    }
    paths = {}
    for name, raw in csv_routes().items():
        paths[name] = re.sub(r"<str:pk>", owners.get(name, ""), raw)
    return paths


@pytest.mark.django_db
def test_every_csv_header_is_reviewed_plain_english_and_never_a_column_name() -> None:
    client, _ = client_for(Role.ADMIN)
    seen: set[str] = set()
    for name, path in _paths().items():
        [header, *_] = export_rows(client, path)
        seen.update(header)
        for column in header:
            assert csv_export.reviewed(column), (name, column)
            assert "_" not in column, (name, column)
            assert not re.search(r"_id\b", column), (name, column)
    assert seen >= csv_export.HEADERS, sorted(csv_export.HEADERS - seen)  # no unused label


@pytest.mark.django_db
@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_every_csv_route_answers_its_roles_and_refuses_unsafe_methods(role: Role) -> None:
    client, _ = client_for(role)
    for name, path in _paths().items():
        needed = EXPORT_ROLES.get(name, Role.VIEWER)
        allowed = meets(role, needed)
        assert client.get(path).status_code == (200 if allowed else 403), (role, name)
        assert client.post(path).status_code == 403, (role, name)


@pytest.mark.django_db
def test_without_a_role_or_a_session_every_csv_route_is_refused() -> None:
    paths = _paths()
    for path in paths.values():
        assert Client().get(path).status_code == 401, path
        assert client_for(None)[0].get(path).status_code == 403, path


# --- D7: no float in the money modules ----------------------------------------------------

MONEY_MODULES = ("receivables", "grid", "reports", "csv_export", "pnl", "payments_ledger")
FLOAT_ARITHMETIC = (
    re.compile(r"\bfloat\s*\("),
    re.compile(r"/\s*100\b"),
    re.compile(r"\*\s*0?\.01\b"),
    re.compile(r"\b[0-9]+\.[0-9]+\b(?![\"'])"),
)


def _code(text: str) -> str:
    """The source without docstrings, comments and string literals."""
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    text = re.sub(r"#[^\n]*", "", text)
    return re.sub(r"\"[^\"\n]*\"|'[^'\n]*'", '""', text)


@pytest.mark.parametrize("module", MONEY_MODULES)
def test_no_money_module_converts_to_a_float_or_divides_cents(module: str) -> None:
    source = (BACKEND / "ledger" / "services" / f"{module}.py").read_text()
    code = _code(source)
    for pattern in FLOAT_ARITHMETIC:
        assert not pattern.search(code), (module, pattern.pattern, pattern.search(code))


def test_the_float_check_would_catch_each_form() -> None:
    for sample in ("x = float(total)", "cents / 100", "amount * 0.01", "rate = 1.5"):
        assert any(pattern.search(_code(sample)) for pattern in FLOAT_ARITHMETIC), sample
    assert not any(pattern.search(_code('CENT = Decimal("0.01")')) for pattern in FLOAT_ARITHMETIC)


# --- Documents export ---------------------------------------------------------------------


def _document(owner: dict[str, Any], name: str, user: User, deleted: bool = False) -> Document:
    return Document.objects.create(
        **owner,
        kind=DocumentKind.OTHER,
        original_filename=name,
        content_type=DocumentContentType.PDF,
        size_bytes=1234,
        blob_path=f"documents/test/{uuid.uuid4()}",
        sha256=uuid.uuid4().hex * 2,
        note="=cmd",
        uploaded_by=user,
        deleted_at=datetime(2026, 9, 2, tzinfo=UTC) if deleted else None,
        delete_note="Wrong file" if deleted else "",
    )


@pytest.mark.django_db
def test_a_documents_export_lists_the_section_rows_deleted_ones_for_admins_only() -> None:
    lease = a_lease()
    admin, user = client_for(Role.ADMIN)
    _document({"store": lease.store}, "plan.pdf", user)
    _document({"store": lease.store}, "old.pdf", user, deleted=True)
    _document({"lease": lease}, "agreement.pdf", user)
    other = a_lease()
    _document({"store": other.store}, "someone-else.pdf", user)
    path = f"/documents/stores/{lease.store_id}/export.csv"
    viewer_rows = export_rows(client_for(Role.VIEWER)[0], path)
    assert [row[1] for row in viewer_rows[1:]] == ["plan.pdf"]
    assert viewer_rows[1][6] == "'=cmd" and viewer_rows[1][3] == "1234"
    admin_rows = export_rows(admin, path)
    assert sorted(row[1] for row in admin_rows[1:]) == ["old.pdf", "plan.pdf"]
    lease_rows = export_rows(admin, f"/documents/leases/{lease.pk}/export.csv")
    assert [row[1] for row in lease_rows[1:]] == ["agreement.pdf"]


@pytest.mark.django_db
def test_a_documents_export_of_an_unknown_owner_is_404() -> None:
    client, _ = client_for(Role.VIEWER)
    assert client.get(f"/documents/stores/{uuid.uuid4()}/export.csv").status_code == 404
    assert client.get("/documents/leases/not-a-uuid/export.csv").status_code == 404


# --- PLAN-012a D3: the operator export's consent columns -----------------------------------


@pytest.mark.django_db
def test_the_operator_export_has_the_sms_phone_and_consent_columns() -> None:
    sms_at = datetime(2026, 10, 1, 14, 30, tzinfo=UTC)
    operator = an_operator("Consent LLC")
    Operator.objects.filter(pk=operator.pk).update(sms_phone="+14045551234", sms_consent_at=sms_at)
    an_operator("No consent LLC")

    [header, *rows] = export_rows(client_for(Role.VIEWER)[0], "/operators/export.csv")

    columns = ("SMS phone", "SMS consent given on", "E-mail consent given on")
    assert header[header.index("Phone") + 1 : header.index("Phone") + 4] == list(columns)
    found = {row[0]: dict(zip(header, row, strict=True)) for row in rows}
    # A leading "+" is written formula-safe, like every text cell.
    assert [found["Consent LLC"][name] for name in columns] == [
        "'+14045551234",
        "2026-10-01T14:30:00+00:00",
        "",
    ]
    assert [found["No consent LLC"][name] for name in columns] == ["", "", ""]
