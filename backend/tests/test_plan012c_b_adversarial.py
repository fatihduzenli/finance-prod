"""Adversarial cases for PLAN-012c-b (promise outcomes), by the QA owner (plan §6). An
outcome route must close only its own promise, a redirect must stay on this site, the
one-open-promise rule must touch only its own bucket, the attention list must never show two
promises for one lease, and the data migration must be safe to run twice."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from datetime import date, timedelta
from types import ModuleType

import pytest
from django.apps import apps as global_apps
from django.utils import timezone

from accounts.roles import Role
from ledger import migrations as ledger_migrations
from ledger.models import AuditLog, ContactEntry, ContactStatus
from ledger.services import attention, contacts
from tests.receivables_helpers import a_lease, an_operator
from tests.test_contact_log import deleted, promise
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    return TODAY


def _outcome_post(**overrides: str) -> dict[str, str]:
    data = {"kind": "call", "happened_on": TODAY.isoformat(), "body": "Spoke to them."}
    return {**data, **overrides}


def _fresh(row: ContactEntry) -> ContactEntry:
    return ContactEntry.objects.get(pk=row.pk)


# --- an outcome route closes its own promise only ---------------------------------------------


def test_the_outcome_route_cannot_close_another_operators_promise_by_forged_fields() -> None:
    operator_a, operator_b = an_operator("A"), an_operator("B")
    mine = promise(operator_a, by=TODAY - timedelta(days=2))
    theirs = promise(operator_b, by=TODAY - timedelta(days=2))
    billing, _ = client_for(Role.BILLING)
    response = billing.post(
        f"/contacts/{mine.pk}/outcome/kept/",
        _outcome_post(previous=str(theirs.pk), operator=str(operator_b.pk), lease=""),
    )
    assert response.status_code == 302
    assert _fresh(theirs).status == ContactStatus.OPEN
    assert _fresh(mine).status == ContactStatus.KEPT
    entry = ContactEntry.objects.exclude(kind="promise").get()
    assert entry.operator_id == operator_a.pk and _fresh(mine).closed_by_id == entry.pk
    # A second outcome on a promise already closed is refused and writes nothing.
    before = ContactEntry.objects.count()
    billing.post(f"/contacts/{mine.pk}/outcome/broken/", _outcome_post())
    assert ContactEntry.objects.count() == before and _fresh(mine).status == ContactStatus.KEPT


def test_next_cannot_send_the_user_off_site() -> None:
    operator = an_operator("A")
    billing, _ = client_for(Role.BILLING)
    for target in (
        "https://evil.example/",
        "//evil.example/x",
        "javascript:alert(1)",
        "\\\\evil.example",
    ):
        row = promise(operator, by=TODAY - timedelta(days=1))
        response = billing.post(
            f"/contacts/{row.pk}/outcome/broken/?next={target}",
            _outcome_post(next=target),
        )
        assert response.status_code == 302, target
        location = response["Location"]
        assert location.startswith("/") and not location.startswith("//"), (target, location)
        assert "evil.example" not in location and "javascript" not in location


def test_a_viewer_has_no_outcome_buttons_and_gets_403() -> None:
    operator = an_operator("A")
    row = promise(operator, by=TODAY - timedelta(days=1))
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get(f"/operators/{operator.pk}/").content.decode()
    assert "/outcome/" not in page
    assert "/outcome/" not in viewer.get("/").content.decode()
    for kind in ("kept", "rescheduled", "broken"):
        assert viewer.get(f"/contacts/{row.pk}/outcome/{kind}/").status_code == 403
        assert (
            viewer.post(f"/contacts/{row.pk}/outcome/{kind}/", _outcome_post()).status_code == 403
        )
    assert _fresh(row).status == ContactStatus.OPEN


# --- the one-open-promise rule touches its own bucket only -----------------------------------


def test_a_new_promise_closes_only_the_open_promise_of_its_own_lease() -> None:
    operator = an_operator("A")
    lease_a, lease_b = a_lease(operator=operator), a_lease(operator=operator)
    on_a = promise(operator, lease=lease_a, by=TODAY + timedelta(days=3))
    on_b = promise(operator, lease=lease_b, by=TODAY + timedelta(days=3))
    gone = deleted(promise(operator, lease=lease_a, by=TODAY + timedelta(days=4)))
    assert _fresh(on_a).status == ContactStatus.RESCHEDULED  # the deleted one superseded it
    reopened = promise(operator, lease=lease_a, by=TODAY + timedelta(days=5))
    ContactEntry.objects.filter(pk=on_a.pk).update(status=ContactStatus.OPEN, closed_by=None)
    newest = promise(operator, lease=lease_a, by=TODAY + timedelta(days=9), amount="100.00")
    assert _fresh(newest).status == ContactStatus.OPEN
    assert _fresh(reopened).status == ContactStatus.RESCHEDULED
    assert _fresh(reopened).closed_by_id == newest.pk
    assert _fresh(on_a).status == ContactStatus.RESCHEDULED
    assert _fresh(on_b).status == ContactStatus.OPEN
    assert _fresh(gone).deleted_at is not None and _fresh(gone).closed_by_id != newest.pk
    open_on_a = ContactEntry.objects.filter(
        operator=operator, lease=lease_a, kind="promise", status="open", deleted_at__isnull=True
    )
    assert list(open_on_a.values_list("pk", flat=True)) == [newest.pk]


def test_the_attention_list_never_shows_two_promises_for_one_bucket_even_when_forced() -> None:
    operator = an_operator("A")
    lease = a_lease(operator=operator)
    first = promise(operator, lease=lease, by=TODAY - timedelta(days=4))
    second = promise(operator, lease=lease, by=TODAY - timedelta(days=2))
    assert _fresh(first).status == ContactStatus.RESCHEDULED
    # Force the forbidden state behind the rule's back.
    ContactEntry.objects.filter(pk=first.pk).update(status=ContactStatus.OPEN, closed_by=None)
    assert ContactEntry.objects.filter(status="open", kind="promise", lease=lease).count() == 2
    broken = contacts.broken_promises(TODAY, operator.pk)
    assert len(broken) == 1 and broken[0].pk == second.pk
    items = [i for i in attention.attention(TODAY).items if i.kind == attention.BROKEN_PROMISE]
    assert len(items) == 1
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get("/").content.decode().count("not kept") == 1


# --- the data migration is safe to run twice ------------------------------------------------------


def _migration_0033() -> ModuleType:
    names = [m.name for m in pkgutil.iter_modules(ledger_migrations.__path__)]
    [name] = [n for n in names if n.startswith("0033_")]
    return importlib.import_module(f"ledger.migrations.{name}")


def _forward(module: ModuleType) -> list[Callable[..., object]]:
    found: list[Callable[..., object]] = []
    for name, function in inspect.getmembers(module, inspect.isfunction):
        if function.__module__ != module.__name__:
            continue
        parameters = list(inspect.signature(function).parameters)
        if parameters[:2] == ["apps", "schema_editor"] and not name.startswith(
            ("reverse", "noop", "nothing", "backwards")
        ):
            found.append(function)
    assert found, "no forward data function found in migration 0033"
    return found


def test_migration_0033_keeps_the_newest_open_per_bucket_and_is_idempotent() -> None:
    operator = an_operator("Sena")
    rows = []
    for days in (-5, -1, 1):
        row = promise(operator, by=TODAY + timedelta(days=days))
        rows.append(row)
    # Undo the rule to recreate staging's state: three open promises in one bucket.
    ContactEntry.objects.filter(pk__in=[r.pk for r in rows]).update(
        status=ContactStatus.OPEN, closed_by=None
    )
    module = _migration_0033()
    audits = AuditLog.objects.count()
    for function in _forward(module):
        function(global_apps, object())
    statuses = [_fresh(r).status for r in rows]
    assert statuses == [ContactStatus.RESCHEDULED, ContactStatus.RESCHEDULED, ContactStatus.OPEN]
    # ``closed_by`` is one-to-one, so the closers chain: oldest -> middle -> newest.
    assert [_fresh(r).closed_by_id for r in rows] == [rows[1].pk, rows[2].pk, None]
    written = AuditLog.objects.count() - audits
    assert written == 2
    for function in _forward(module):
        function(global_apps, object())
    assert [_fresh(r).status for r in rows] == statuses
    assert AuditLog.objects.count() - audits == written
    assert len(contacts.broken_promises(TODAY, operator.pk)) == 0
    assert timezone.localdate() == TODAY
