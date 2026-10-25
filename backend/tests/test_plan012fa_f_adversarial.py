"""Adversarial cases for PLAN-012f-a slice F (owner, 2026-10-24), by the QA owner (plan §10): both
wizards refuse Confirm unless the statement step was saved in this wizard. A direct POST to either
confirm URL on a draft whose statement was never saved writes nothing (no charge, no balance
change, no status change) and returns to the statement step with a message; a GET of the confirm
page goes the same way; the service refuses with the field "statement"; the marker cannot be
forged from a form field; a save and a confirm racing on PostgreSQL never leave a confirmed row
without the marker; the renamed column's old name is gone from the code."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import pytest
from django.db import connection, connections
from django.urls import reverse

from accounts.roles import Role
from ledger.models import Lease, LeaseEnd, LeaseEndStatus, LeaseTransfer, RentCharge
from ledger.services import lease_end, transfer
from tests.test_lease_end import TODAY, draft, fresh, settle
from tests.test_lease_end_drafts import an_october_lease, row_counts, save
from tests.test_operator_views import client_for
from tests.test_transfer import a_clean_lease, begin, incoming, snapshot

pytestmark = pytest.mark.django_db

BACKEND = Path(__file__).resolve().parents[1]
FORGED = {"statement_saved_at": "2026-10-01T00:00:00+00:00", "statement_opened_at": "2026-10-01"}


@pytest.fixture(autouse=True)
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)


def statement_of(end: LeaseEnd) -> str:
    return reverse("ledger:lease_ends:statement", kwargs={"pk": end.pk})


def state_of(lease: Lease, end: LeaseEnd) -> tuple[Any, ...]:
    """Everything a confirm would change: the lease's end, the draft's status and stamp,
    the charges, the marker."""
    lease = Lease.objects.get(pk=lease.pk)
    row = fresh(end)
    return (
        lease.ended_on,
        row.status,
        row.updated_at,
        row.statement_saved_at,
        list(RentCharge.objects.filter(lease=lease).values_list("period_month", "amount_due")),
        row_counts(),
    )


# --- the lease-end wizard --------------------------------------------------------------------


def test_a_direct_post_to_the_lease_end_confirm_without_a_saved_statement_writes_nothing() -> None:
    lease = an_october_lease()
    end = draft(lease)
    assert fresh(end).statement_saved_at is None
    admin, _ = client_for(Role.ADMIN)
    before = state_of(lease, end)
    url = reverse("ledger:lease_ends:confirm", kwargs={"pk": end.pk})
    for response in (admin.get(url), admin.post(url, {"confirmed": "on", **FORGED})):
        assert response.status_code == 302, response.status_code
        assert response["Location"] == statement_of(end)
        assert state_of(lease, end) == before
    assert fresh(end).status == LeaseEndStatus.DRAFT
    assert not RentCharge.objects.filter(lease=lease).exists()
    page = admin.get(statement_of(end))
    assert page.status_code == 200
    assert lease_end.STATEMENT_REQUIRED in page.content.decode()


def test_the_lease_end_service_refuses_an_unsaved_statement_with_nothing_written() -> None:
    lease = an_october_lease()
    end = draft(lease)
    before = state_of(lease, end)
    outcome = lease_end.confirm(end, None, confirmed=True, today=TODAY)
    assert not outcome.ok
    assert outcome.field == "statement"
    assert outcome.message == lease_end.STATEMENT_REQUIRED
    assert state_of(lease, end) == before


def test_a_saved_statement_confirms_as_before_and_a_forged_marker_is_ignored() -> None:
    lease = an_october_lease()
    end = draft(lease)
    admin, _ = client_for(Role.ADMIN)
    # A form field of the marker's name on the terms step sets nothing.
    terms = reverse("ledger:lease_ends:terms", kwargs={"pk": end.pk})
    admin.post(terms, {"ended_on": "2026-10-19", "reason": "moved_out", **FORGED})
    assert fresh(end).statement_saved_at is None
    assert save(end).ok
    assert fresh(end).statement_saved_at is not None
    url = reverse("ledger:lease_ends:confirm", kwargs={"pk": end.pk})
    assert admin.get(url).status_code == 200
    response = admin.post(url, {"confirmed": "on"})
    assert response.status_code == 302
    assert fresh(end).status == LeaseEndStatus.CONFIRMED
    assert Lease.objects.get(pk=lease.pk).ended_on is not None


# --- the transfer wizard ---------------------------------------------------------------------


def a_transfer_up_to_confirm(user: Any) -> LeaseTransfer:
    """Operator, end date, settlement and incoming lease set; the statement never saved."""
    row = begin(a_clean_lease(), user)
    end = fresh(row).lease_end
    assert end is not None
    assert settle(end, actor=user).ok
    outcome = incoming(row, user)
    assert outcome.ok, outcome.message
    row = LeaseTransfer.objects.get(pk=row.pk)
    assert fresh(end).statement_saved_at is None
    return row


def test_a_direct_post_to_the_transfer_confirm_without_a_saved_statement_writes_nothing() -> None:
    admin, user = client_for(Role.ADMIN)
    row = a_transfer_up_to_confirm(user)
    end = row.lease_end
    assert end is not None
    before = (snapshot(), state_of(row.outgoing_lease, end))
    url = reverse("ledger:transfers:confirm", kwargs={"pk": row.pk})
    for response in (admin.get(url), admin.post(url, {"confirmed": "on", **FORGED})):
        assert response.status_code == 302, response.status_code
        assert response["Location"] == statement_of(end)
        assert (snapshot(), state_of(row.outgoing_lease, end)) == before
    assert LeaseTransfer.objects.get(pk=row.pk).status == row.status
    assert Lease.objects.filter(store=row.store).count() == 1  # no incoming lease
    assert lease_end.STATEMENT_REQUIRED in admin.get(statement_of(end)).content.decode()


def test_the_transfer_service_refuses_an_unsaved_statement_with_nothing_written() -> None:
    _, user = client_for(Role.ADMIN)
    row = a_transfer_up_to_confirm(user)
    end = row.lease_end
    assert end is not None
    before = (snapshot(), state_of(row.outgoing_lease, end))
    outcome = transfer.confirm(fresh(row), user, confirmed=True, today=TODAY)
    assert not outcome.ok
    assert outcome.field == "statement"
    assert (snapshot(), state_of(row.outgoing_lease, end)) == before


def test_a_transfer_with_a_saved_statement_still_confirms() -> None:
    admin, user = client_for(Role.ADMIN)
    row = a_transfer_up_to_confirm(user)
    end = row.lease_end
    assert end is not None
    assert save(end).ok
    url = reverse("ledger:transfers:confirm", kwargs={"pk": row.pk})
    assert admin.get(url).status_code == 200
    assert admin.post(url, {"confirmed": "on"}).status_code == 302
    assert fresh(end).status == LeaseEndStatus.CONFIRMED
    assert Lease.objects.filter(store=row.store).count() == 2


# --- the race, on PostgreSQL ------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_save_and_a_confirm_racing_never_confirm_without_the_marker() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("row locks and concurrent transactions need PostgreSQL")
    lease = an_october_lease()
    end = draft(lease)
    results: dict[str, Any] = {}

    def run(name: str, action: Any) -> None:
        try:
            results[name] = action()
        except Exception as error:  # noqa: BLE001 — recorded, asserted below
            results[name] = error
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=run, args=("save", lambda: save(end))),
        threading.Thread(
            target=run,
            args=("confirm", lambda: lease_end.confirm(end, None, confirmed=True, today=TODAY)),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(not isinstance(found, Exception) for found in results.values()), results
    row = fresh(end)
    confirmed = LeaseEnd.objects.filter(lease=lease, status=LeaseEndStatus.CONFIRMED).count()
    assert confirmed <= 1
    if row.status == LeaseEndStatus.CONFIRMED:
        assert row.statement_saved_at is not None
    else:
        assert not results["confirm"].ok and results["confirm"].field == "statement"
        assert Lease.objects.get(pk=lease.pk).ended_on is None


# --- the rename ------------------------------------------------------------------------------


def test_the_old_column_name_is_gone_from_the_code() -> None:
    keep = {"0040_lease_end_statement_opened.py", "0050_rename_statement_saved_at.py"}
    offenders = []
    for root in ("ledger", "finance", "accounts", "payments"):
        for path in sorted((BACKEND / root).rglob("*")):
            if path.suffix not in (".py", ".html", ".js") or path.name in keep:
                continue
            if "statement_opened_at" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []
    ownership = (BACKEND.parent / "docs" / "DATA_OWNERSHIP.md").read_text(encoding="utf-8")
    assert "lease_ends.statement_saved_at" in ownership
    assert not re.search(r"lease_ends\.statement_opened_at", ownership)
