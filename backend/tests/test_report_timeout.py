"""The P&L statement timeout and the post-import ANALYZE (docs/qa/PLAN-010c D11, Batch C).

On PostgreSQL the page and the export compute inside one transaction with ``SET LOCAL
statement_timeout``; a cancelled statement answers 200 with a sentence. The export
computes its records before the response starts. ``maintenance.analyze`` issues
``ANALYZE`` on PostgreSQL only, and ``import_store_costs`` calls it after a real run that
changed rows. The forced timeout is 1 ms with a statement that sleeps longer, so the
cancel never depends on the machine's speed.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from finance.env_guard import EnvGuardError, validate
from ledger.models import Store, StoreCost
from ledger.services import maintenance, pnl
from ledger.views import reports
from tests.test_env_guard import LOCAL_ENV, _env
from tests.test_operator_views import client_for
from tests.test_store_costs import row, store, write_csv

pytestmark = pytest.mark.django_db

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="the statement timeout is PostgreSQL's"
)
requires_sqlite = pytest.mark.skipif(connection.vendor != "sqlite", reason="sqlite only")

PAGE = "/reports/pnl/"
EXPORT = "/reports/pnl/export.csv"
RANGE = {"from": "2026-01", "to": "2026-03"}


def _statements(captured: CaptureQueriesContext, prefix: str) -> list[str]:
    return [query["sql"] for query in captured.captured_queries if query["sql"].startswith(prefix)]


@pytest.fixture
def slow_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pnl.rows`` first runs a statement that sleeps 200 ms."""
    original = pnl.rows

    def slow(query: pnl.PnlQuery) -> list[pnl.PnlRow]:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_sleep(0.2)")
        return original(query)

    monkeypatch.setattr(pnl, "rows", slow)


# --- Settings and the env guard ------------------------------------------------------------


def test_the_timeout_defaults_to_15_seconds() -> None:
    assert validate(LOCAL_ENV).report_statement_timeout_ms == 15_000
    assert validate(
        _env(LOCAL_ENV, REPORT_STATEMENT_TIMEOUT_MS="")
    ).report_statement_timeout_ms == (15_000)


@pytest.mark.parametrize("value", ["1000", "15000", "120000"])
def test_the_timeout_accepts_1000_to_120000(value: str) -> None:
    config = validate(_env(LOCAL_ENV, REPORT_STATEMENT_TIMEOUT_MS=value))
    assert config.report_statement_timeout_ms == int(value)


@pytest.mark.parametrize("value", ["999", "120001", "0", "-1", "1e4", "15 000", "abc", "1000000"])
def test_the_timeout_refuses_anything_else(value: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate(_env(LOCAL_ENV, REPORT_STATEMENT_TIMEOUT_MS=value))
    message = str(raised.value)
    assert "REPORT_STATEMENT_TIMEOUT_MS must be a whole number from 1000 to 120000" in message
    assert value not in message.replace("1000 to 120000", "")


def test_the_setting_comes_from_the_guard() -> None:
    from django.conf import settings

    assert settings.REPORT_STATEMENT_TIMEOUT_MS == 15_000


# --- The timeout on PostgreSQL -------------------------------------------------------------


@requires_postgresql
@pytest.mark.parametrize("url", [PAGE, EXPORT])
def test_the_report_sets_the_timeout_for_its_queries_and_restores_it(url: str) -> None:
    client, _ = client_for(Role.VIEWER)
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        before = cursor.fetchone()[0]
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url, RANGE)
    assert response.status_code == 200
    assert _statements(captured, "SET LOCAL statement_timeout = ") == [
        "SET LOCAL statement_timeout = 15000"
    ]
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        assert cursor.fetchone()[0] == before


@requires_postgresql
@pytest.mark.parametrize("url", [PAGE, EXPORT])
def test_a_cancelled_query_answers_200_with_the_sentence(
    url: str, slow_rows: None, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = client_for(Role.VIEWER)
    with override_settings(REPORT_STATEMENT_TIMEOUT_MS=1), caplog.at_level(logging.WARNING):
        response = client.get(url, RANGE)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    assert not getattr(response, "streaming", False)
    page = response.content.decode()
    assert reports.TOO_SLOW in page
    assert 'id="pnl-too-slow"' in page
    assert 'id="pnl"' not in page
    logged = [record.getMessage() for record in caplog.records if record.name == reports.__name__]
    assert logged == ["P&L report cancelled by the statement timeout: QueryCanceled"]
    # The connection is usable afterwards: the cancelled statement was rolled back.
    assert Store.objects.count() >= 0


@requires_postgresql
def test_the_export_streams_only_after_every_record_is_computed() -> None:
    client, _ = client_for(Role.VIEWER)
    with CaptureQueriesContext(connection) as captured:
        response = client.get(EXPORT, RANGE)
        computed = len(captured.captured_queries)
        streamed: Any = response
        body = b"".join(streamed.streaming_content).decode()
        assert len(captured.captured_queries) == computed
    assert body.splitlines()[0].startswith("level,store,operator,month")
    assert body.splitlines()[-1].startswith("total,")


@requires_postgresql
def test_another_operational_error_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.db import OperationalError

    def broken(query: pnl.PnlQuery) -> list[pnl.PnlRow]:
        raise OperationalError("connection lost")

    monkeypatch.setattr(pnl, "rows", broken)
    with pytest.raises(OperationalError):
        reports.bounded(lambda: pnl.rows(pnl.parse_query({})))


# --- Other engines: unchanged -----------------------------------------------------------------


@requires_sqlite
@pytest.mark.parametrize("url", [PAGE, EXPORT])
def test_sqlite_sets_no_timeout(url: str) -> None:
    client, _ = client_for(Role.VIEWER)
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url, RANGE)
    assert response.status_code == 200
    assert not _statements(captured, "SET ")


# --- ANALYZE ----------------------------------------------------------------------------------


def _analyze(captured: CaptureQueriesContext) -> list[str]:
    return _statements(captured, "ANALYZE")


def test_analyze_issues_one_statement_per_model_on_postgresql_only() -> None:
    with CaptureQueriesContext(connection) as captured:
        maintenance.analyze(StoreCost, Store)
    expected = ['ANALYZE "store_costs"', 'ANALYZE "stores"']
    assert _analyze(captured) == (expected if connection.vendor == "postgresql" else [])


def command(path: Path, *args: str) -> tuple[str, list[str]]:
    out = io.StringIO()
    with CaptureQueriesContext(connection) as captured:
        try:
            call_command("import_store_costs", str(path), *args, stdout=out)
        except CommandError:
            pass
    return out.getvalue(), _analyze(captured)


def _expected(statements: int) -> list[str]:
    return ['ANALYZE "store_costs"'] * statements if connection.vendor == "postgresql" else []


def test_the_importer_analyzes_after_a_real_run_that_changed_rows(tmp_path: Path) -> None:
    store(5001)
    store(5002)
    path = write_csv(tmp_path / "c.csv", [row("5001", "2500.00"), row("5002", "1800.00")])

    out, analyzed = command(path, "--dry-run")
    assert "created 2" in out and analyzed == []

    out, analyzed = command(path)
    assert "created 2" in out and analyzed == _expected(1)

    out, analyzed = command(path)
    assert "unchanged 2, created 0, updated 0" in out and analyzed == []

    updated = write_csv(tmp_path / "u.csv", [row("5001", "2600.00")])
    out, analyzed = command(updated)
    assert "updated 1" in out and analyzed == _expected(1)


def test_the_importer_does_not_analyze_when_every_row_is_refused(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "c.csv", [row("9999", "2500.00")])
    out, analyzed = command(path)
    assert "refused 1" in out and analyzed == []


def test_the_importer_analyzes_when_some_rows_applied_and_others_refused(tmp_path: Path) -> None:
    store(5003)
    path = write_csv(tmp_path / "c.csv", [row("5003", "2500.00"), row("9998", "1.00")])
    out, analyzed = command(path)
    assert "created 1" in out and "refused 1" in out
    assert analyzed == _expected(1)
