"""Adversarial cases for PLAN-010 Batch B, written by the QA owner (plan §5).

The cases here go after the edges Opus's ``test_reconciliation.py`` does not: hostile text
reaching the mismatch and resolution rows, the resolve endpoint's gates, provider objects
that vanish mid-run, a corrupted cursor, the pending threshold at its boundary, the
fixture command's argument edges, and "Run now" while the night's job waits.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from accounts.roles import Role
from ledger.models import AuditLog, Payment, PaymentStatus
from payments import invoice_sync, jobs
from payments.errors import ProviderRejected
from payments.fake import FakeProvider
from payments.models import (
    Job,
    MismatchKind,
    ReconciliationMismatch,
    ReconciliationRun,
    ReconciliationStatus,
    ReconciliationTrigger,
    SyncState,
)
from payments.reconciliation import CURSOR_ROW, mismatches, run_reconciliation, window_start
from tests.job_helpers import run_queued_jobs
from tests.test_operator_views import client_for
from tests.test_reconciliation import NOW, pending_payment, synced_world
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import INVOICE, TXN

pytestmark = pytest.mark.django_db

CARD = "4111111111111111"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


NOTE_WITH_CARD = f"Customer paid by card {CARD} instead."


def _mismatch(fake: FakeProvider) -> ReconciliationMismatch:
    """One unresolved ``pending_too_old`` mismatch from a real run."""
    pending_payment(fake, date(2026, 9, 1))
    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    assert run.status == ReconciliationStatus.DONE
    return ReconciliationMismatch.objects.get(kind=MismatchKind.PENDING_TOO_OLD)


# --- resolving ---------------------------------------------------------------------------------


def test_a_resolution_note_with_a_card_number_is_refused_everywhere(fake: FakeProvider) -> None:
    found = _mismatch(fake)
    outcome = mismatches.resolve(found.pk, note=NOTE_WITH_CARD, actor=None)
    assert outcome == mismatches.ResolveOutcome(False, mismatches.NOTE_PAYMENT_DATA)
    found.refresh_from_db()
    assert found.resolved_at is None and found.resolution_note == ""
    assert not AuditLog.objects.filter(action="reconciliation_mismatch.resolved").exists()

    billing, _ = client_for(Role.BILLING)
    response = billing.post(
        f"/reconciliation/mismatches/{found.pk}/resolve/", {"note": NOTE_WITH_CARD}
    )
    assert response.status_code == 302
    page = billing.get("/reconciliation/").content.decode()
    assert CARD not in page
    assert "card or account numbers" in page
    found.refresh_from_db()
    assert found.resolved_at is None
    assert CARD not in str(ReconciliationMismatch.objects.values_list("resolution_note"))


def test_resolving_twice_keeps_the_first_note_and_writes_one_audit_row(
    fake: FakeProvider,
) -> None:
    found = _mismatch(fake)
    first = mismatches.resolve(found.pk, note="Settled by hand on the bank statement.", actor=None)
    assert first.ok
    second = mismatches.resolve(found.pk, note="Other story.", actor=None)
    assert second == mismatches.ResolveOutcome(False, mismatches.ALREADY_RESOLVED)
    found.refresh_from_db()
    assert found.resolution_note == "Settled by hand on the bank statement."
    assert AuditLog.objects.filter(action="reconciliation_mismatch.resolved").count() == 1


@pytest.mark.parametrize("role", [Role.VIEWER, None])
def test_below_billing_cannot_resolve_and_the_row_is_untouched(
    fake: FakeProvider, role: Role | None
) -> None:
    found = _mismatch(fake)
    client, _ = client_for(role)
    response = client.post(f"/reconciliation/mismatches/{found.pk}/resolve/", {"note": "done"})
    assert response.status_code == 403
    found.refresh_from_db()
    assert found.resolved_at is None


def test_an_unknown_or_malformed_mismatch_id_is_404_not_500(fake: FakeProvider) -> None:
    billing, _ = client_for(Role.BILLING)
    for key in (str(uuid.uuid4()), "not-a-uuid", "../../etc/passwd"):
        response = billing.post(f"/reconciliation/mismatches/{key}/resolve/", {"note": "done"})
        assert response.status_code == 404, key


def test_an_empty_or_blank_note_resolves_nothing(fake: FakeProvider) -> None:
    found = _mismatch(fake)
    billing, _ = client_for(Role.BILLING)
    # Blank, whitespace, and invisible format characters only (the guard's own list).
    for note in ("", "   ", " ", "​‌⁠﻿", " ​ "):
        billing.post(f"/reconciliation/mismatches/{found.pk}/resolve/", {"note": note})
        found.refresh_from_db()
        assert found.resolved_at is None, repr(note)


# --- the run under hostile or missing provider data ----------------------------------------------


def test_an_invoice_the_provider_no_longer_has_is_counted_missing_and_the_run_goes_on(
    fake: FakeProvider,
) -> None:
    world = synced_world(fake)
    world.invoice.refresh_from_db()
    before = world.invoice.provider_status
    assert before
    fake.fail_next("get_invoice", ProviderRejected(404, "gone"))
    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    assert run.status == ReconciliationStatus.DONE
    assert run.counts["a"]["missing"] == 1
    assert run.counts["a"].get("mismatched", 0) == 0
    world.invoice.refresh_from_db()
    assert world.invoice.provider_status == before
    assert run.step_reached == "f"


def test_a_provider_status_holding_a_card_number_never_reaches_a_row(
    fake: FakeProvider,
) -> None:
    world = synced_world(fake)
    hostile = f"PAID {CARD}\n\n  <script>alert(1)</script>"
    fake.invoices[INVOICE] = replace(fake.invoices[INVOICE], raw_status=hostile)
    invoice_sync.record_provider_status(world.invoice.pk, "SENT")

    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)

    # Whatever the run's verdict, the number is in no row, and the run did not raise.
    assert run.status in (ReconciliationStatus.DONE, ReconciliationStatus.FAILED)
    stored = [
        str(ReconciliationMismatch.objects.values_list("detail")),
        str(ReconciliationRun.objects.values_list("error", "counts")),
        str(AuditLog.objects.values_list("before", "after")),
    ]
    for text in stored:
        assert CARD not in text
    if run.status == ReconciliationStatus.FAILED:
        assert run.error and CARD not in run.error
        assert SyncState.objects.filter(job_name=CURSOR_ROW).first() is None


def test_a_corrupted_cursor_falls_back_to_the_lookback(fake: FakeProvider) -> None:
    SyncState.objects.create(job_name=CURSOR_ROW, cursor="not-a-date")
    with override_settings(RECONCILE_LOOKBACK_DAYS=35):
        assert window_start(NOW) == NOW - timedelta(days=35)
    SyncState.objects.filter(job_name=CURSOR_ROW).update(cursor="")
    with override_settings(RECONCILE_LOOKBACK_DAYS=35):
        assert window_start(NOW) == NOW - timedelta(days=35)


def test_a_cursor_in_the_future_never_makes_a_backwards_window(fake: FakeProvider) -> None:
    SyncState.objects.create(job_name=CURSOR_ROW, cursor=(NOW + timedelta(days=30)).isoformat())
    assert window_start(NOW) == NOW


# --- the pending threshold at its boundary -------------------------------------------------------


@pytest.mark.parametrize(
    ("today", "flagged"),
    [
        (datetime(2026, 10, 2, 6, 30, tzinfo=UTC), False),  # Friday: 5 business days
        (datetime(2026, 10, 4, 6, 30, tzinfo=UTC), False),  # Sunday: still 5
        (datetime(2026, 10, 5, 6, 30, tzinfo=UTC), True),  # Monday: 6
    ],
)
def test_a_payment_initiated_on_a_friday_is_flagged_on_the_sixth_business_day(
    fake: FakeProvider, today: datetime, flagged: bool
) -> None:
    pending_payment(fake, date(2026, 9, 25))  # a Friday
    with override_settings(PENDING_PAYMENT_MAX_BUSINESS_DAYS=5):
        run = run_reconciliation(ReconciliationTrigger.MANUAL, now=today)
    assert run.status == ReconciliationStatus.DONE
    assert (
        ReconciliationMismatch.objects.filter(kind=MismatchKind.PENDING_TOO_OLD).exists() is flagged
    )


def test_a_settled_or_returned_payment_is_never_pending_too_old(fake: FakeProvider) -> None:
    payment = pending_payment(fake, date(2026, 1, 5))
    for status in (PaymentStatus.SETTLED, PaymentStatus.RETURNED):
        Payment.objects.filter(pk=payment.pk).update(status=status)
        run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW + timedelta(hours=1))
        assert run.status == ReconciliationStatus.DONE, status
        assert not ReconciliationMismatch.objects.filter(kind=MismatchKind.PENDING_TOO_OLD).exists()


# --- Run now against the night's job ------------------------------------------------------------


def test_run_now_while_the_scheduled_job_waits_is_busy_and_queues_nothing(
    fake: FakeProvider,
) -> None:
    jobs.queue_scheduled_reconciliation()
    admin, _ = client_for(Role.ADMIN)
    response = admin.post("/reconciliation/run/")
    assert response.status_code == 302
    assert Job.objects.count() == 1
    page = admin.get("/reconciliation/").content.decode()
    assert "still in progress" in page  # PLAN-012c §4.1: no "request waiting"
    assert fake.calls == []


def test_run_now_by_billing_is_refused_and_queues_nothing(fake: FakeProvider) -> None:
    billing, _ = client_for(Role.BILLING)
    assert billing.post("/reconciliation/run/").status_code == 403
    assert Job.objects.count() == 0


def test_a_run_now_job_records_its_requester_and_the_page_shows_no_identifier(
    fake: FakeProvider,
) -> None:
    payment = pending_payment(fake, date(2026, 9, 1))
    admin, user = client_for(Role.ADMIN)
    admin.post("/reconciliation/run/")
    with override_settings(STAX_CALLER="worker"):
        [job] = run_queued_jobs()
    assert job.status == "done"
    run = ReconciliationRun.objects.get(trigger=ReconciliationTrigger.MANUAL)
    assert run.requested_by == user
    page = admin.get("/reconciliation/").content.decode()
    assert str(payment.pk) not in page
    assert TXN not in page
    assert "<dd>Run now</dd>" in page  # the trigger label; the requester is not an identifier


# --- the fixture command's edges ----------------------------------------------------------------


@pytest.mark.parametrize("days", ["0", "-3", "abc", "100000"])
def test_age_pending_payment_refuses_bad_day_counts(fake: FakeProvider, days: str) -> None:
    payment = pending_payment(fake, date(2026, 10, 1))
    with pytest.raises((CommandError, SystemExit)):
        call_command("age_pending_payment", "--payment", str(payment.pk), "--days", days)
    payment.refresh_from_db()
    assert payment.initiated_on == date(2026, 10, 1)
    assert not AuditLog.objects.filter(action__startswith="payment.aged").exists()


@pytest.mark.parametrize("key", ["", "not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_age_pending_payment_names_no_payment_for_a_bad_id(fake: FakeProvider, key: str) -> None:
    pending_payment(fake, date(2026, 10, 1))
    out = io.StringIO()
    with pytest.raises(CommandError) as raised:
        call_command("age_pending_payment", "--payment", key, "--days", "7", stdout=out)
    assert out.getvalue() == ""
    assert CARD not in str(raised.value)
