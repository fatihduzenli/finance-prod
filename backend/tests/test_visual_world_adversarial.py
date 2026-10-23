"""The visual data set's state is complete at build time, by the QA owner (QA-012f, the first
CI run of 80bbe28). The lease end sits at its statement step with the charges that step
creates already written, so no page visit writes anything the later shots depend on: the
baselines render the same on SQLite (where the live server shares the test's connection) and
on PostgreSQL (where the server commits on its own connection)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

import documents_storage
from documents_storage.memory import MemoryDocumentStorage
from ledger.models import LeaseEnd, RentCharge
from ledger.services import lease_end
from payments.fake import FakeProvider
from tests.visual import world as visual_world

pytestmark = pytest.mark.django_db


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> visual_world.World:
    clock = visual_world.FrozenClock()
    monkeypatch.setattr("django.utils.timezone.now", clock)
    monkeypatch.setattr("payments.get_provider", FakeProvider)
    monkeypatch.setattr(documents_storage, "get_storage", MemoryDocumentStorage)
    world = visual_world.build()
    clock.stop()
    return world


def test_the_statement_step_is_opened_at_build_and_a_page_visit_writes_nothing_more(
    built: visual_world.World,
) -> None:
    end = LeaseEnd.objects.get(pk=built.lease_end.pk)
    assert end.statement_opened_at is not None
    october = RentCharge.objects.filter(lease=built.second_lease, period_month=date(2026, 10, 1))
    assert october.count() == 1
    assert october.get().amount_due == Decimal("1225.81")  # 19 of 31 days of 2,000.00
    before = list(RentCharge.objects.order_by("pk").values_list("pk", "amount_due", "status"))
    # What the statement page does on a visit, with the real date and the pinned one.
    assert lease_end.open_statement(end, built.user) == []
    assert lease_end.open_statement(end, built.user, today=visual_world.TODAY) == []
    after = list(RentCharge.objects.order_by("pk").values_list("pk", "amount_due", "status"))
    assert after == before
