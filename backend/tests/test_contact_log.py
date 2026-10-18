"""The contact log grows up (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md D2 to
D5, §6 for Batch A): the author's 15-minute edit window, appended corrections, the admin's
soft delete with a reason (struck through for billing and admin, hidden from viewers and
their CSV), a lease on an entry with the store page's section, the automatic close of a
promise by a settled payment (handler, manual payment and the daily sweep), the outcome of
the previous promise, the status filter and the broken-promise rule reading ``status``."""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from django.utils.html import escape

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AuditLog,
    ContactCorrection,
    ContactEntry,
    ContactKind,
    ContactStatus,
    Lease,
    Operator,
    PaymentMethod,
    RentChargeStatus,
    Store,
)
from ledger.services import attention, calendar, contacts, rent_run
from ledger.views.contacts import NO_STORE_LEASE
from payments.fake import FakeProvider
from tests.auth_helpers import make_user
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_operator,
)
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture: the webhook path)
from tests.test_webhook_handlers import (
    deliver,
    fake,  # noqa: F401 (fixture)
    make_world,
    payment_for,
    provider_txn,
    settle,
)

pytestmark = pytest.mark.django_db

D = Decimal
NEW_YORK = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 6)
CARD = "4111 1111 1111 1111"


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def billing_user() -> User:
    user, _ = make_user(f"billing-{uuid.uuid4().hex[:8]}", Role.BILLING)
    return user


def entry(
    operator: Operator,
    *,
    kind: str = ContactKind.CALL,
    actor: User | None = None,
    lease: Lease | None = None,
    on: date = date(2026, 10, 1),
    by: date | None = None,
    amount: str | None = None,
    body: str = "Called about rent.",
) -> ContactEntry:
    promise = kind == ContactKind.PROMISE
    outcome = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=kind,
            happened_on=on,
            body=body,
            promised_date=(by or date(2026, 10, 5)) if promise else None,
            promised_amount=D(amount or "1500.00") if promise else None,
            lease=lease.pk if lease is not None else None,
        ),
        actor=actor,
    )
    assert outcome.ok and outcome.entry is not None, outcome.message
    return outcome.entry


def promise(operator: Operator, **fields: object) -> ContactEntry:
    return entry(operator, kind=ContactKind.PROMISE, **fields)  # type: ignore[arg-type]


def recorded(row: ContactEntry, on: date, hour: int = 12) -> ContactEntry:
    """Set ``created_at`` to ``hour`` o'clock New York time on ``on``."""
    ContactEntry.objects.filter(pk=row.pk).update(
        created_at=datetime.combine(on, time(hour), tzinfo=NEW_YORK)
    )
    row.refresh_from_db()
    return row


def deleted(row: ContactEntry, reason: str = "Recorded on the wrong operator.") -> ContactEntry:
    admin, _ = make_user(f"admin-{uuid.uuid4().hex[:8]}", Role.ADMIN)
    outcome = contacts.delete_entry(row, reason, actor=admin)
    assert outcome.ok, outcome.message
    row.refresh_from_db()
    return row


def page_of(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return response.content.decode()


def table_of(page: str) -> str:
    return page.split('id="contact-entries"')[1].split("</table>")[0]


# --- D2: the edit window ---------------------------------------------------------------------


def edit_input(row: ContactEntry, **changes: object) -> contacts.EditInput:
    values: dict[str, object] = {
        "kind": row.kind,
        "happened_on": row.happened_on,
        "body": row.body,
        "promised_date": row.promised_date,
        "promised_amount": row.promised_amount,
        **changes,
    }
    return contacts.EditInput(**values)  # type: ignore[arg-type]


def test_the_author_edits_at_14_59_and_is_refused_at_15_01_with_a_pinned_clock() -> None:
    author = billing_user()
    row = entry(an_operator(), actor=author)
    at = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
    ContactEntry.objects.filter(pk=row.pk).update(created_at=at)
    row.refresh_from_db()
    inside = contacts.edit_entry(
        row,
        edit_input(row, body="Called; agreed Friday."),
        actor=author,
        now=at + timedelta(minutes=14, seconds=59),
    )
    assert inside.ok and inside.message == contacts.EDITED
    row.refresh_from_db()
    assert row.body == "Called; agreed Friday."
    audit = AuditLog.objects.get(action="contact_entry.edited", entity_id=str(row.pk))
    assert audit.before is not None and audit.before["body"] == "Called about rent."
    assert audit.after["body"] == "Called; agreed Friday."
    outside = contacts.edit_entry(
        row,
        edit_input(row, body="Too late."),
        actor=author,
        now=at + timedelta(minutes=15, seconds=1),
    )
    assert not outside.ok and outside.message == contacts.WINDOW_CLOSED
    row.refresh_from_db()
    assert row.body == "Called; agreed Friday."
    # The window closes at exactly 15 minutes.
    exactly = contacts.edit_entry(
        row, edit_input(row, body="On the minute."), actor=author, now=at + timedelta(minutes=15)
    )
    assert not exactly.ok and exactly.message == contacts.WINDOW_CLOSED


def pin_clock(monkeypatch: pytest.MonkeyPatch, row: ContactEntry, after: timedelta) -> None:
    """Pin ``timezone.now`` to ``after`` past the entry's ``created_at``."""
    pinned = row.created_at + after
    monkeypatch.setattr("django.utils.timezone.now", lambda: pinned)


def edit_post(row: ContactEntry, **changes: str) -> dict[str, str]:
    data = {
        "kind": row.kind,
        "happened_on": row.happened_on.isoformat(),
        "promised_date": row.promised_date.isoformat() if row.promised_date else "",
        "promised_amount": str(row.promised_amount) if row.promised_amount else "",
        "body": row.body,
    }
    return {**data, **changes}


def test_the_edit_page_inside_and_outside_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    client, author = client_for(Role.BILLING)
    operator = an_operator()
    row = entry(operator, actor=author)
    pin_clock(monkeypatch, row, timedelta(minutes=14, seconds=59))
    log = table_of(page_of(client, f"/operators/{operator.pk}/"))
    assert f"/contacts/{row.pk}/edit/" in log and "contact-edit-until" in log
    closes = timezone.localtime(row.created_at + contacts.EDIT_WINDOW)
    assert f"until {closes:%H:%M}" in log
    assert f"/contacts/{row.pk}/correct/" not in log
    form = page_of(client, f"/contacts/{row.pk}/edit/")
    assert f"until {closes:%Y-%m-%d %H:%M}" in form
    response = client.post(f"/contacts/{row.pk}/edit/", edit_post(row, body="Agreed Friday."))
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.body == "Agreed Friday."
    pin_clock(monkeypatch, row, timedelta(minutes=15, seconds=1))
    log = table_of(page_of(client, f"/operators/{operator.pk}/"))
    assert f"/contacts/{row.pk}/edit/" not in log
    assert f"/contacts/{row.pk}/correct/" in log
    response = client.post(f"/contacts/{row.pk}/edit/", edit_post(row, body="Too late."))
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.body == "Agreed Friday."
    assert client.get(f"/contacts/{row.pk}/edit/").status_code == 302


def test_another_author_gets_403_even_inside_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    author = billing_user()
    row = entry(an_operator(), actor=author)
    other, _ = client_for(Role.BILLING)
    admin, _ = client_for(Role.ADMIN)
    pin_clock(monkeypatch, row, timedelta(minutes=1))
    for client in (other, admin):
        assert client.get(f"/contacts/{row.pk}/edit/").status_code == 403
        response = client.post(f"/contacts/{row.pk}/edit/", edit_post(row, body="Not mine."))
        assert response.status_code == 403
    row.refresh_from_db()
    assert row.body == "Called about rent."
    refused = contacts.edit_entry(
        row, edit_input(row, body="Not mine."), actor=billing_user(), now=row.created_at
    )
    assert not refused.ok and refused.message == contacts.NOT_AUTHOR
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get(f"/contacts/{row.pk}/edit/").status_code == 403
    assert viewer.post(f"/contacts/{row.pk}/edit/", edit_post(row)).status_code == 403


def test_an_edit_reruns_the_promise_rules_and_keeps_an_outcome() -> None:
    author = billing_user()
    operator = an_operator()
    row = promise(operator, actor=author)
    now = row.created_at + timedelta(minutes=1)
    for changes, message in (
        ({"promised_amount": D("0.00")}, contacts.AMOUNT_POSITIVE),
        ({"promised_date": None}, contacts.PROMISE_FIELDS_REQUIRED),
        ({"kind": ContactKind.CALL}, contacts.PROMISE_FIELDS_ONLY),
        ({"body": f"Card {CARD}"}, contacts.BODY_PAYMENT_DATA),
    ):
        outcome = contacts.edit_entry(row, edit_input(row, **changes), actor=author, now=now)
        assert not outcome.ok and outcome.message == message, changes
    moved = contacts.edit_entry(
        row, edit_input(row, promised_amount=D("900.00")), actor=author, now=now
    )
    assert moved.ok
    row.refresh_from_db()
    assert row.promised_amount == D("900.00")
    # Once its outcome is recorded, a promise keeps its kind, date and amount.
    entry(operator, actor=author)  # unrelated
    contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.CALL, happened_on=TODAY, body="Paid.", previous=row.pk, outcome="kept"
        ),
        actor=author,
    )
    row.refresh_from_db()
    closed = contacts.edit_entry(
        row, edit_input(row, promised_amount=D("100.00")), actor=author, now=now
    )
    assert not closed.ok and closed.message == contacts.PROMISE_CLOSED
    text_only = contacts.edit_entry(
        row, edit_input(row, body="Paid in cash."), actor=author, now=now
    )
    assert text_only.ok


# --- D2: corrections and the soft delete ----------------------------------------------------


def test_any_billing_user_appends_a_correction_listed_under_the_entry(today: date) -> None:
    author = billing_user()
    operator = an_operator()
    row = entry(operator, actor=author)
    client, corrector = client_for(Role.BILLING)
    assert page_of(client, f"/contacts/{row.pk}/correct/")
    response = client.post(f"/contacts/{row.pk}/correct/", {"body": "It was a visit, not a call."})
    assert response.status_code == 302
    assert response["Location"] == f"/operators/{operator.pk}/#contact-{row.pk}"
    correction = ContactCorrection.objects.get(entry=row)
    assert (correction.author, correction.body) == (corrector, "It was a visit, not a call.")
    row.refresh_from_db()
    assert row.body == "Called about rent."
    audit = AuditLog.objects.get(action="contact_entry.corrected", entity_id=str(row.pk))
    assert audit.before == {"corrections": 0}
    assert audit.after["body"] == "It was a visit, not a call."
    viewer, _ = client_for(Role.VIEWER)
    log = table_of(page_of(viewer, f"/operators/{operator.pk}/"))
    assert "It was a visit, not a call." in log and 'class="contact-correction"' in log
    assert f"/contacts/{row.pk}/correct/" not in log
    assert viewer.post(f"/contacts/{row.pk}/correct/", {"body": "x"}).status_code == 403
    refused = client.post(f"/contacts/{row.pk}/correct/", {"body": f"Card {CARD}"})
    assert refused.status_code == 200 and CARD not in refused.content.decode()
    assert ContactCorrection.objects.count() == 1


def test_a_correction_on_a_deleted_entry_is_refused() -> None:
    row = deleted(entry(an_operator(), actor=billing_user()))
    outcome = contacts.add_correction(row, "Late correction.", actor=billing_user())
    assert not outcome.ok and outcome.message == contacts.DELETED
    client, _ = client_for(Role.BILLING)
    response = client.post(f"/contacts/{row.pk}/correct/", {"body": "Late correction."})
    assert response.status_code == 302
    assert not ContactCorrection.objects.exists()
    edit = contacts.edit_entry(row, edit_input(row), actor=row.author, now=row.created_at)
    assert not edit.ok and edit.message == contacts.DELETED


def test_delete_by_billing_is_403_and_by_admin_needs_a_reason() -> None:
    operator = an_operator()
    row = entry(operator, actor=billing_user())
    billing, _ = client_for(Role.BILLING)
    assert billing.get(f"/contacts/{row.pk}/delete/").status_code == 403
    assert billing.post(f"/contacts/{row.pk}/delete/", {"reason": "No."}).status_code == 403
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(f"/contacts/{row.pk}/delete/", {"reason": "No."}).status_code == 403
    admin, admin_user = client_for(Role.ADMIN)
    assert admin.post(f"/contacts/{row.pk}/delete/", {"reason": " \u200b "}).status_code == 200
    assert ContactEntry.objects.get(pk=row.pk).deleted_at is None
    response = admin.post(f"/contacts/{row.pk}/delete/", {"reason": "Wrong operator."})
    assert response.status_code == 302
    row.refresh_from_db()
    assert (row.deleted_by, row.delete_reason) == (admin_user, "Wrong operator.")
    assert row.deleted_at is not None
    audit = AuditLog.objects.get(action="contact_entry.deleted", entity_id=str(row.pk))
    assert audit.before == {"deleted_at": None, "deleted_by": None, "delete_reason": ""}
    assert audit.after["delete_reason"] == "Wrong operator."
    again = contacts.delete_entry(row, "Twice.", actor=admin_user)
    assert not again.ok and again.message == contacts.DELETED


def test_the_deleted_entry_is_struck_through_for_billing_and_hidden_from_viewers(
    today: date,
) -> None:
    operator = an_operator()
    author = billing_user()
    kept = entry(operator, actor=author, body="Visible note.")
    gone = deleted(entry(operator, actor=author, body="Secret note."), "Typed on the wrong shop.")
    for role in (Role.BILLING, Role.ADMIN):
        client, _ = client_for(role)
        for path in (f"/operators/{operator.pk}/", f"/operators/{operator.pk}/contacts/"):
            log = table_of(page_of(client, path))
            assert f'id="contact-{gone.pk}"' in log
            row = log.split(f'id="contact-{gone.pk}"')[1].split("</tr>")[0]
            assert "<s>" in row and "Typed on the wrong shop." in row
            assert f"/contacts/{gone.pk}/" not in row  # no action on a deleted entry
    viewer, _ = client_for(Role.VIEWER)
    for path in (f"/operators/{operator.pk}/", f"/operators/{operator.pk}/contacts/"):
        page = page_of(viewer, path)
        assert "Secret note." not in page and "Typed on the wrong shop." not in page
        assert "Visible note." in page
    body = b"".join(
        viewer.get(f"/operators/{operator.pk}/contacts/export.csv").streaming_content  # type: ignore[attr-defined]
    ).decode()
    assert "Secret note." not in body and "Typed on the wrong shop." not in body
    rows = list(csv.reader(io.StringIO(body)))
    assert len(rows) == 2 and rows[1][3] == "Visible note."
    billing, _ = client_for(Role.BILLING)
    body = b"".join(
        billing.get(f"/operators/{operator.pk}/contacts/export.csv").streaming_content  # type: ignore[attr-defined]
    ).decode()
    rows = list(csv.reader(io.StringIO(body)))
    by_note = {row[3]: row for row in rows[1:]}
    assert by_note["Secret note."][-1] == "Typed on the wrong shop."
    assert by_note["Visible note."][-1] == "" and kept.pk


def test_a_deleted_promise_counts_for_nothing() -> None:
    operator = an_operator()
    lease = a_lease(a_store("2477"), operator)
    gone = deleted(promise(operator, by=date(2026, 10, 5), amount="500.00"))
    assert contacts.broken_promises(TODAY) == []
    assert list(contacts.open_promises(operator)) == []
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.PAID, "3500.00", "3500.00")
    a_payment(charge, "500.00", date(2026, 10, 3))
    assert contacts.close_kept() == 0
    gone.refresh_from_db()
    assert gone.status == ContactStatus.OPEN and gone.kept_by_payment is None
    refused = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.CALL, happened_on=TODAY, body="x", previous=gone.pk, outcome="kept"
        ),
        actor=None,
    )
    assert not refused.ok and refused.message == contacts.PREVIOUS_UNKNOWN
    ContactEntry.objects.filter(pk=gone.pk).update(promised_date=TODAY)
    ContactEntry.objects.filter(pk=gone.pk).update(author=billing_user())
    assert calendar.queue_morning_emails(TODAY).promises == 0


# --- D3: a lease on an entry ------------------------------------------------------------------


def form_page(client: Client, operator: Operator) -> str:
    return page_of(client, f"/operators/{operator.pk}/contacts/new/")


def contact_post(**overrides: str) -> dict[str, str]:
    data = {
        "kind": "call",
        "happened_on": "2026-10-06",
        "promised_date": "",
        "promised_amount": "",
        "body": "Called.",
    }
    return {**data, **overrides}


def test_an_operator_without_a_lease_gets_no_store_choice(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    assert 'name="lease"' not in form_page(client, operator)
    response = client.post(f"/operators/{operator.pk}/contacts/new/", contact_post())
    assert response.status_code == 302
    assert ContactEntry.objects.get().lease is None


def test_one_active_lease_is_preselected_and_optional(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    lease = a_lease(a_store("2477"), operator)
    ended = a_lease(a_store("1999"), operator)
    Lease.objects.filter(pk=ended.pk).update(ended_on=date(2026, 1, 31), collection_status="ended")
    page = form_page(client, operator)
    assert f'<option value="{lease.pk}" selected>Store 2477</option>' in page
    assert "No particular store" in page
    assert "Store 1999 (not in effect today)" in page
    client.post(f"/operators/{operator.pk}/contacts/new/", contact_post(lease=str(lease.pk)))
    client.post(f"/operators/{operator.pk}/contacts/new/", contact_post(lease="", body="Any."))
    assert {row.body: row.lease for row in ContactEntry.objects.all()} == {
        "Called.": lease,
        "Any.": None,
    }


def test_two_active_leases_make_the_store_required(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    first = a_lease(a_store("2476"), operator)
    second = a_lease(a_store("2477"), operator)
    page = form_page(client, operator)
    assert "No particular store" not in page and " selected>Store" not in page
    response = client.post(f"/operators/{operator.pk}/contacts/new/", contact_post())
    assert response.status_code == 200
    assert "Choose the store this is about." in response.content.decode()
    assert not ContactEntry.objects.exists()
    refused = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(kind=ContactKind.CALL, happened_on=TODAY, body="x"),
        actor=None,
    )
    assert not refused.ok and refused.message == contacts.LEASE_REQUIRED
    response = client.post(
        f"/operators/{operator.pk}/contacts/new/", contact_post(lease=str(second.pk))
    )
    assert response.status_code == 302
    assert ContactEntry.objects.get().lease == second and first.pk


def test_another_operators_lease_is_refused(today: date) -> None:
    operator = an_operator("Acme")
    other_lease = a_lease(a_store("9999"), an_operator("Beta"))
    refused = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.CALL, happened_on=TODAY, body="x", lease=other_lease.pk
        ),
        actor=None,
    )
    assert not refused.ok and refused.message == contacts.LEASE_UNKNOWN
    assert refused.field == "lease"
    # On the page, the choice offers the operator's own leases only.
    a_lease(a_store("2477"), operator)
    client, _ = client_for(Role.BILLING)
    response = client.post(
        f"/operators/{operator.pk}/contacts/new/", contact_post(lease=str(other_lease.pk))
    )
    assert response.status_code == 200
    assert not ContactEntry.objects.exists()


def test_the_store_page_lists_its_entries_and_add_entry_preselects_its_lease(
    today: date,
) -> None:
    operator = an_operator("Acme")
    store = a_store("2477")
    lease = a_lease(store, operator)
    other = a_lease(a_store("2476"), operator)
    author = billing_user()
    mine = promise(operator, lease=lease, actor=author, body="Pays 2477 Friday.")
    entry(operator, lease=other, actor=author, body="About 2476.")
    client, _ = client_for(Role.BILLING)
    page = page_of(client, f"/stores/{store.pk}/?tab=contacts")
    assert 'id="contacts"' in page and f'href="/stores/{store.pk}/contacts/new/"' in page
    log = table_of(page)
    assert "Pays 2477 Friday." in log and "About 2476." not in log
    # The operator page shows the same entry, with its store.
    operator_log = table_of(page_of(client, f"/operators/{operator.pk}/"))
    assert "Pays 2477 Friday." in operator_log and f'href="/stores/{store.pk}/"' in operator_log
    full = page_of(client, f"/stores/{store.pk}/contacts/")
    assert "Pays 2477 Friday." in full and "About 2476." not in full
    form = page_of(client, f"/stores/{store.pk}/contacts/new/")
    assert f'<option value="{lease.pk}" selected>Store 2477</option>' in form
    # The open promise on this store's lease is the previous promise chosen.
    assert f'<option value="{mine.pk}" selected>' in form
    response = client.post(
        f"/stores/{store.pk}/contacts/new/",
        contact_post(lease=str(lease.pk), previous=str(mine.pk), outcome="broken", body="Nope."),
    )
    assert response.status_code == 302
    assert response["Location"] == f"/stores/{store.pk}/?tab=contacts#tab-contacts"
    added = ContactEntry.objects.get(body="Nope.")
    assert added.lease == lease and added.operator == operator
    viewer, _ = client_for(Role.VIEWER)
    assert "/contacts/new/" not in page_of(viewer, f"/stores/{store.pk}/?tab=contacts")
    assert viewer.get(f"/stores/{store.pk}/contacts/new/").status_code == 403


def test_a_store_without_a_lease_in_effect_offers_no_add_entry() -> None:
    store: Store = a_store("3000")
    client, _ = client_for(Role.BILLING)
    page = page_of(client, f"/stores/{store.pk}/?tab=contacts")
    assert "No entries yet. Add one." in page and f"/stores/{store.pk}/contacts/new/" not in page
    response = client.get(f"/stores/{store.pk}/contacts/new/")
    assert (
        response.status_code == 302
        and response["Location"] == f"/stores/{store.pk}/?tab=contacts#tab-contacts"
    )


# --- PLAN-012c: Add entry on a store whose lease is not in effect today -------------------


def ended(lease: Lease, on: date) -> Lease:
    Lease.objects.filter(pk=lease.pk).update(collection_status="ended", ended_on=on)
    lease.refresh_from_db()
    return lease


def archived(operator: Operator) -> Operator:
    Operator.objects.filter(pk=operator.pk).update(archived_at=timezone.now())
    operator.refresh_from_db()
    return operator


def without_operator(lease: Lease) -> Lease:
    Lease.objects.filter(pk=lease.pk).update(operator=None, collection_status="ended")
    lease.refresh_from_db()
    return lease


def add_link(store: Store) -> str:
    return f'href="/stores/{store.pk}/contacts/new/"'


@pytest.mark.parametrize(
    ("status", "started", "ended_on", "label", "suffix"),
    [
        ("active", date(2026, 11, 1), None, " (not in effect today)", ", lease pending"),
        ("prospective", None, None, " (not in effect today)", ", lease pending"),
        ("paused", date(2026, 10, 7), None, " (not in effect today)", ", lease pending"),
        ("ended", date(2025, 1, 1), date(2026, 9, 30), " (not in effect today)", ""),
        ("active", date(2025, 1, 1), date(2026, 10, 5), " (not in effect today)", ""),
        ("paused", date(2025, 1, 1), None, "", ""),
    ],
)
def test_add_entry_on_a_store_whose_only_lease_is_not_in_effect(
    today: date,
    status: str,
    started: date | None,
    ended_on: date | None,
    label: str,
    suffix: str,
) -> None:
    operator = an_operator("Acme")
    store = a_store("2477")
    lease = a_lease(store, operator, started=started, status=status)
    if ended_on is not None:
        Lease.objects.filter(pk=lease.pk).update(ended_on=ended_on)
        suffix = f", lease ended {ended_on.isoformat()}"
    for role in (Role.BILLING, Role.ADMIN):
        client, _ = client_for(role)
        assert add_link(store) in page_of(client, f"/stores/{store.pk}/?tab=contacts")
    client, _ = client_for(Role.BILLING)
    form = page_of(client, f"/stores/{store.pk}/contacts/new/")
    assert f"<h1>Add entry: Acme, store 2477{suffix}</h1>" in form
    assert f'<option value="{lease.pk}" selected>Store 2477{label}</option>' in form
    response = client.post(
        f"/stores/{store.pk}/contacts/new/", contact_post(lease=str(lease.pk), body="Old lease.")
    )
    assert response.status_code == 302
    assert response["Location"] == f"/stores/{store.pk}/?tab=contacts#tab-contacts"
    added = ContactEntry.objects.get()
    assert (added.operator, added.lease, added.body) == (operator, lease, "Old lease.")
    assert "Old lease." in table_of(page_of(client, f"/stores/{store.pk}/?tab=contacts"))
    assert "Old lease." in table_of(page_of(client, f"/operators/{operator.pk}/"))
    viewer, _ = client_for(Role.VIEWER)
    assert "/contacts/new/" not in page_of(viewer, f"/stores/{store.pk}/?tab=contacts")
    assert viewer.get(f"/stores/{store.pk}/contacts/new/").status_code == 403
    assert viewer.post(f"/stores/{store.pk}/contacts/new/", contact_post()).status_code == 403
    assert ContactEntry.objects.count() == 1


def test_the_lease_in_effect_wins_over_an_older_ended_one(today: date) -> None:
    store = a_store("2477")
    ended(a_lease(store, an_operator("Old"), started=date(2024, 1, 1)), date(2025, 12, 31))
    current = a_lease(store, an_operator("New"), started=date(2026, 1, 1))
    assert contacts.store_contact_lease(store, TODAY) == current
    client, _ = client_for(Role.BILLING)
    form = page_of(client, f"/stores/{store.pk}/contacts/new/")
    assert "<h1>Add entry: New, store 2477</h1>" in form


def test_of_two_ended_leases_the_later_start_wins_then_the_later_creation(
    today: date,
) -> None:
    store = a_store("2477")
    ended(a_lease(store, an_operator("First"), started=date(2023, 1, 1)), date(2023, 12, 31))
    later = ended(
        a_lease(store, an_operator("Second"), started=date(2024, 1, 1)), date(2024, 6, 30)
    )
    undated = a_lease(store, an_operator("Undated"), started=None, status="prospective")
    assert contacts.store_contact_lease(store, TODAY) == later
    client, _ = client_for(Role.BILLING)
    form = page_of(client, f"/stores/{store.pk}/contacts/new/")
    assert "<h1>Add entry: Second, store 2477, lease ended 2024-06-30</h1>" in form
    # A lease without a start comes last; between two, the later creation wins.
    other = a_store("2478")
    Lease.objects.filter(pk=undated.pk).update(store=other)
    newer = a_lease(other, an_operator("Newer"), started=None, status="prospective")
    assert contacts.store_contact_lease(other, TODAY) == newer


def test_a_lease_of_an_archived_operator_or_without_an_operator_is_skipped(
    today: date,
) -> None:
    store = a_store("2477")
    older = ended(a_lease(store, an_operator("Live"), started=date(2023, 1, 1)), date(2023, 12, 31))
    without_operator(a_lease(store, an_operator(), started=date(2025, 1, 1)))
    gone = archived(an_operator("Gone"))
    a_lease(store, gone, started=date(2026, 1, 1))
    # The lease in effect belongs to an archived operator: the next one is chosen.
    assert contacts.store_contact_lease(store, TODAY) == older
    client, _ = client_for(Role.BILLING)
    assert add_link(store) in page_of(client, f"/stores/{store.pk}/?tab=contacts")
    response = client.post(
        f"/stores/{store.pk}/contacts/new/", contact_post(lease=str(older.pk), body="Live.")
    )
    assert response.status_code == 302
    added = ContactEntry.objects.get()
    assert added.lease == older and added.operator.display_name == "Live"
    # With only archived or operator-less leases left, no button and the route refuses.
    archived(older.operator)  # type: ignore[arg-type]
    assert contacts.store_contact_lease(store, TODAY) is None
    assert add_link(store) not in page_of(client, f"/stores/{store.pk}/?tab=contacts")
    response = client.get(f"/stores/{store.pk}/contacts/new/", follow=True)
    assert response.redirect_chain == [(f"/stores/{store.pk}/?tab=contacts#tab-contacts", 302)]
    assert escape(NO_STORE_LEASE) in response.content.decode()
    response = client.post(f"/stores/{store.pk}/contacts/new/", contact_post(body="Gone."))
    assert response.status_code == 302 and ContactEntry.objects.count() == 1


def test_a_store_with_no_lease_shows_the_existing_message(today: date) -> None:
    store = a_store("3000")
    client, _ = client_for(Role.BILLING)
    assert add_link(store) not in page_of(client, f"/stores/{store.pk}/?tab=contacts")
    response = client.get(f"/stores/{store.pk}/contacts/new/", follow=True)
    assert response.redirect_chain == [(f"/stores/{store.pk}/?tab=contacts#tab-contacts", 302)]
    assert escape(NO_STORE_LEASE) in response.content.decode()


def test_the_store_route_records_nothing_on_another_stores_lease_or_operator(
    today: date,
) -> None:
    store = a_store("2477")
    lease = ended(a_lease(store, an_operator("Acme"), started=date(2024, 1, 1)), date(2025, 6, 30))
    beta = an_operator("Beta")
    foreign = a_lease(a_store("9999"), beta)
    client, _ = client_for(Role.BILLING)
    response = client.post(
        f"/stores/{store.pk}/contacts/new/",
        contact_post(lease=str(foreign.pk), operator=str(beta.pk), body="Sneaky."),
    )
    assert response.status_code == 200
    # The store's lease is locked: no blank choice, and the operator's lease at another
    # store is not offered either.
    acme_elsewhere = a_lease(a_store("2476"), lease.operator)
    form = page_of(client, f"/stores/{store.pk}/contacts/new/")
    assert "No particular store" not in form and str(acme_elsewhere.pk) not in form
    for forged in ({"lease": ""}, {"lease": str(acme_elsewhere.pk)}):
        response = client.post(
            f"/stores/{store.pk}/contacts/new/",
            contact_post(operator=str(beta.pk), body="Forged.", **forged),
        )
        assert response.status_code == 200
    assert not ContactEntry.objects.exists()
    response = client.post(
        f"/stores/{store.pk}/contacts/new/",
        contact_post(lease=str(lease.pk), operator=str(beta.pk), body="Named."),
    )
    assert response.status_code == 302
    added = ContactEntry.objects.get()
    assert (added.operator, added.lease) == (lease.operator, lease)


def test_the_attention_text_names_the_store_and_the_leases_balance() -> None:
    operator = an_operator("Acme")
    lease = a_lease(a_store("2477"), operator)
    other = a_lease(a_store("2476"), operator)
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.PARTIAL, "3500.00", "2250.00")
    a_rent_charge(other, date(2026, 9, 1), RentChargeStatus.UNPAID, "3500.00")
    promise(operator, lease=lease, by=date(2026, 10, 5), amount="500.00")
    promise(operator, lease=other, by=date(2026, 10, 4), amount="100.00")
    items = [i.text for i in attention.attention(TODAY).items if i.kind == "broken_promise"]
    assert items == [
        "Promise to pay $100.00 by 2026-10-04: Acme on store 2476: owes $3,500.00, not kept",
        "Promise to pay $500.00 by 2026-10-05: Acme on store 2477: owes $1,250.00, not kept",
    ]
    banners = attention.attention(TODAY, operator=operator.pk).items
    assert "on store 2477: owes $1,250.00" in " ".join(item.text for item in banners)


# --- D4: the automatic close ----------------------------------------------------------------


def keeping_world(
    *, amount: str = "500.00", promised: date = date(2026, 10, 5)
) -> tuple[Operator, Lease, Lease, ContactEntry]:
    operator = an_operator("Acme")
    lease = a_lease(a_store("2477"), operator)
    other = a_lease(a_store("2476"), operator)
    row = recorded(promise(operator, lease=lease, by=promised, amount=amount), date(2026, 10, 2))
    return operator, lease, other, row


def settled_on(lease: Lease, amount: str, on: date) -> None:
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.PAID, "3500.00", amount)
    payment = a_payment(charge, amount, on)
    contacts.close_kept_by_payment(payment)


def test_exactly_the_amount_on_the_promised_date_on_that_lease_keeps_it() -> None:
    _, lease, _, row = keeping_world()
    settled_on(lease, "500.00", date(2026, 10, 5))
    row.refresh_from_db()
    assert row.status == ContactStatus.KEPT and row.closed_by is None
    assert row.kept_by_payment is not None and row.kept_by_payment.amount == D("500.00")
    assert AuditLog.objects.filter(
        action="contact_entry.kept_by_payment", entity_id=str(row.pk)
    ).exists()
    assert contacts.broken_promises(TODAY) == []


@pytest.mark.parametrize(
    ("amount", "on", "which"),
    [
        ("499.99", date(2026, 10, 4), "lease"),  # smaller
        ("500.00", date(2026, 10, 6), "lease"),  # after the promised date
        ("500.00", date(2026, 10, 1), "lease"),  # before the promise was recorded
        ("500.00", date(2026, 10, 4), "other"),  # on another lease
    ],
    ids=["smaller", "after-the-date", "before-recording", "another-lease"],
)
def test_these_payments_do_not_keep_it(amount: str, on: date, which: str) -> None:
    _, lease, other, row = keeping_world()
    settled_on(lease if which == "lease" else other, amount, on)
    row.refresh_from_db()
    assert row.status == ContactStatus.OPEN and row.kept_by_payment is None
    assert contacts.close_kept() == 0
    assert contacts.broken_promises(TODAY) == [row]


def test_a_promise_without_a_lease_is_kept_by_a_payment_on_any_lease() -> None:
    operator = an_operator("Acme")
    a_lease(a_store("2477"), operator)
    ended = a_lease(a_store("2476"), operator)
    Lease.objects.filter(pk=ended.pk).update(ended_on=date(2026, 6, 30), collection_status="ended")
    loose = recorded(promise(operator, by=date(2026, 10, 5), amount="500.00"), date(2026, 10, 2))
    assert loose.lease is None
    settled_on(ended, "600.00", date(2026, 10, 3))
    loose.refresh_from_db()
    assert loose.status == ContactStatus.KEPT


def test_a_refund_or_a_payment_not_settled_keeps_nothing() -> None:
    _, lease, _, row = keeping_world()
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.PAID, "3500.00", "500.00")
    pending = a_payment(charge, "500.00", None, status="pending", initiated_on=date(2026, 10, 3))
    assert contacts.close_kept_by_payment(pending) == 0
    row.refresh_from_db()
    assert row.status == ContactStatus.OPEN


def test_the_tick_sweeps_and_a_second_run_changes_nothing() -> None:
    _, lease, _, row = keeping_world()
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.PAID, "3500.00", "500.00")
    a_payment(charge, "500.00", date(2026, 10, 4))  # written directly: no settlement path ran
    row.refresh_from_db()
    assert row.status == ContactStatus.OPEN
    out = StringIO()
    call_command("collections_tick", stdout=out)
    assert "Promises kept by a payment: 1." in out.getvalue()
    row.refresh_from_db()
    assert row.status == ContactStatus.KEPT
    audits = AuditLog.objects.filter(action="contact_entry.kept_by_payment").count()
    out = StringIO()
    call_command("collections_tick", stdout=out)
    assert "Promises kept by a payment: 0." in out.getvalue()
    assert contacts.close_kept() == 0
    assert contacts.close_kept_by_payment(row.kept_by_payment) == 0  # type: ignore[arg-type]
    assert AuditLog.objects.filter(action="contact_entry.kept_by_payment").count() == audits


def test_a_manual_payment_keeps_the_promise() -> None:
    _, lease, _, row = keeping_world()
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.UNPAID, "3500.00")
    outcome = rent_run.record_manual_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=D("500.00"),
        paid_on=date(2026, 10, 5),
        note="Zelle from the operator.",
        actor=None,
        today=TODAY,
    )
    assert outcome.ok, outcome.message
    row.refresh_from_db()
    assert row.status == ContactStatus.KEPT and row.kept_by_payment == outcome.payment


def test_a_manual_one_off_payment_keeps_a_promise_without_a_lease() -> None:
    from ledger.services import one_off_charges

    operator = an_operator("Acme")
    row = recorded(promise(operator, by=date(2026, 10, 5), amount="50.00"), date(2026, 10, 2))
    charge = a_one_off_charge(operator, "50.00", date(2026, 10, 1))
    outcome = one_off_charges.record_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=D("50.00"),
        paid_on=date(2026, 10, 4),
        note="Zelle.",
        actor=None,
        today=TODAY,
    )
    assert outcome.ok, outcome.message
    row.refresh_from_db()
    assert row.status == ContactStatus.KEPT


def test_a_settlement_by_webhook_keeps_the_promise(fake: FakeProvider) -> None:  # noqa: F811
    world = make_world(fake)
    row = recorded(
        promise(world.operator, lease=world.lease, by=date(2026, 10, 20), amount="1500.00"),
        date(2026, 10, 10),
    )
    provider_txn(fake)
    deliver("create_transaction", id="txn-0001")
    row.refresh_from_db()
    assert row.status == ContactStatus.OPEN  # pending keeps nothing
    settle(fake)
    assert deliver("update_transaction_settled", id="txn-0001") == "processed"
    row.refresh_from_db()
    assert row.status == ContactStatus.KEPT and row.kept_by_payment == payment_for()
    # A replay of the settlement changes nothing.
    assert deliver("update_transaction_settled", id="txn-0001") == "processed"
    assert AuditLog.objects.filter(action="contact_entry.kept_by_payment").count() == 1


# --- D5: the outcome of the previous promise --------------------------------------------------


def test_the_form_asks_for_the_outcome_when_a_promise_is_open(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    assert 'name="outcome"' not in form_page(client, operator)
    old = promise(operator, by=date(2026, 10, 9), amount="700.00")
    page = form_page(client, operator)
    assert 'name="outcome"' in page and 'name="closes"' not in page
    assert f'<option value="{old.pk}" selected>$700.00 by 2026-10-09' in page
    assert '"amount": "700.00"' in page and '"date": "2026-10-09"' in page
    assert '<input type="radio" name="outcome" value="open"' in page
    assert 'value="open" id="id_outcome_0" checked' in page
    assert "Still open (no change)" in page


def outcome_post(old: ContactEntry, outcome: str, **overrides: str) -> dict[str, str]:
    return contact_post(previous=str(old.pk), outcome=outcome, **overrides)


def test_kept_closes_the_old_promise_with_the_new_entry(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    old = promise(operator)
    client.post(f"/operators/{operator.pk}/contacts/new/", outcome_post(old, "kept"))
    old.refresh_from_db()
    new = ContactEntry.objects.get(body="Called.")
    assert (old.status, old.closed_by, old.kept_by_payment) == ("kept", new, None)


def test_rescheduled_needs_a_promise_preselected_from_the_old_one(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    old = promise(operator, by=date(2026, 10, 9), amount="700.00")
    response = client.post(
        f"/operators/{operator.pk}/contacts/new/", outcome_post(old, "rescheduled")
    )
    assert response.status_code == 200
    assert contacts.RESCHEDULE_NEEDS_PROMISE in response.content.decode()
    old.refresh_from_db()
    assert old.status == ContactStatus.OPEN
    # Date and amount left blank: the old promise's; edited: the new values.
    client.post(
        f"/operators/{operator.pk}/contacts/new/",
        outcome_post(old, "rescheduled", kind="promise", body="Same again."),
    )
    old.refresh_from_db()
    new = ContactEntry.objects.get(body="Same again.")
    assert old.status == ContactStatus.RESCHEDULED and old.closed_by == new
    assert (new.kind, new.promised_date, new.promised_amount, new.status) == (
        "promise",
        date(2026, 10, 9),
        D("700.00"),
        "open",
    )
    client.post(
        f"/operators/{operator.pk}/contacts/new/",
        outcome_post(
            new,
            "rescheduled",
            kind="promise",
            promised_date="2026-10-20",
            promised_amount="350.00",
            body="Half on the 20th.",
        ),
    )
    newer = ContactEntry.objects.get(body="Half on the 20th.")
    assert (newer.promised_date, newer.promised_amount) == (date(2026, 10, 20), D("350.00"))
    new.refresh_from_db()
    assert new.status == ContactStatus.RESCHEDULED
    # The entry that rescheduled a promise stays a promise.
    author_edit = contacts.edit_entry(
        newer,
        edit_input(newer, kind=ContactKind.CALL, promised_date=None, promised_amount=None),
        actor=newer.author,
        now=newer.created_at,
    )
    assert not author_edit.ok


def test_broken_closes_the_old_promise_and_the_new_entry_is_any_kind(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    old = promise(operator, by=date(2026, 10, 1))
    assert contacts.broken_promises(TODAY) == [old]
    client.post(
        f"/operators/{operator.pk}/contacts/new/",
        outcome_post(old, "broken", kind="visit", body="Visited; nothing paid."),
    )
    old.refresh_from_db()
    assert old.status == ContactStatus.BROKEN
    assert old.closed_by == ContactEntry.objects.get(kind="visit")
    # A promise with an outcome is no longer an attention item.
    assert contacts.broken_promises(TODAY) == []
    audit = AuditLog.objects.get(action="contact_entry.outcome_recorded", entity_id=str(old.pk))
    assert audit.before == {"status": "open", "closed_by": None, "kept_by_payment": None}
    assert audit.after["status"] == "broken"


def test_an_outcome_without_a_promise_or_an_unknown_outcome_is_refused() -> None:
    operator = an_operator()
    old = promise(operator)
    for previous, outcome in ((None, "kept"), (old.pk, "forgotten"), (None, "pending")):
        refused = contacts.add_entry(
            operator.pk,
            contacts.ContactInput(
                kind=ContactKind.CALL,
                happened_on=TODAY,
                body="x",
                previous=previous,
                outcome=outcome,
            ),
            actor=None,
        )
        assert not refused.ok and refused.message == contacts.OUTCOME_UNKNOWN
    old.refresh_from_db()
    assert old.status == ContactStatus.OPEN


# --- The log: statuses, the filter, the broken list ------------------------------------------


def test_the_full_list_filters_by_status_and_shows_badges(today: date) -> None:
    operator = an_operator()
    kept = promise(operator, body="Kept one.")
    contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.CALL,
            happened_on=TODAY,
            body="Closing call.",
            previous=kept.pk,
            outcome="kept",
        ),
        actor=None,
    )
    promise(operator, body="Still open.")
    client, _ = client_for(Role.VIEWER)
    full = table_of(page_of(client, f"/operators/{operator.pk}/contacts/"))
    assert "Kept one." in full and "Still open." in full and "Closing call." in full
    assert 'promise-kept">Kept<' in full and 'promise-open">Open<' in full
    only_kept = table_of(page_of(client, f"/operators/{operator.pk}/contacts/?status=kept"))
    assert "Kept one." in only_kept
    assert "Still open." not in only_kept and "Closing call." not in only_kept
    only_open = table_of(page_of(client, f"/operators/{operator.pk}/contacts/?status=open"))
    assert "Still open." in only_open and "Kept one." not in only_open
    assert "Closing call." not in only_open  # a call is never listed as an open promise
    unknown = table_of(page_of(client, f"/operators/{operator.pk}/contacts/?status=bogus"))
    assert "Closing call." in unknown


def test_the_broken_list_reads_the_status_not_closed_by() -> None:
    operator = an_operator()
    row = promise(operator, by=date(2026, 10, 1))
    assert contacts.broken_promises(TODAY) == [row]
    for status in ("kept", "rescheduled", "broken"):
        ContactEntry.objects.filter(pk=row.pk).update(status=status)
        assert contacts.broken_promises(TODAY) == [], status
    ContactEntry.objects.filter(pk=row.pk).update(status="open")
    assert contacts.broken_promises(TODAY) == [row]


def test_the_new_routes_are_registered_with_their_roles() -> None:
    for name, role in (
        ("ledger:contacts:edit", Role.BILLING),
        ("ledger:contacts:correct", Role.BILLING),
        ("ledger:contacts:delete", Role.ADMIN),
        ("ledger:stores:contacts_add", Role.BILLING),
    ):
        rule = registry.rule_for(name)
        assert rule is not None and rule.min_role is role and rule.methods == {"POST"}, name
    assert "ledger:stores:contacts" in registry.READ_ONLY_ROUTE_NAMES


def test_unknown_entries_are_404() -> None:
    client, _ = client_for(Role.ADMIN)
    for path in (
        f"/contacts/{uuid.uuid4()}/edit/",
        f"/contacts/{uuid.uuid4()}/correct/",
        "/contacts/not-a-uuid/delete/",
        f"/stores/{uuid.uuid4()}/contacts/",
        "/stores/not-a-uuid/contacts/new/",
    ):
        assert client.get(path).status_code == 404, path


# --- PLAN-012c-b: Still open, outcome buttons, one open promise per lease, Add entry -------


def outcome_url(row: ContactEntry, outcome: str, next_path: str = "") -> str:
    url = f"/contacts/{row.pk}/outcome/{outcome}/"
    return f"{url}?next={next_path}" if next_path else url


def outcome_form(**overrides: str) -> dict[str, str]:
    return {"kind": "call", "happened_on": "2026-10-06", "body": "Spoke to them.", **overrides}


def outcome_buttons(page: str, row: ContactEntry) -> list[str]:
    return [
        outcome
        for outcome in ("kept", "rescheduled", "broken")
        if f'href="/contacts/{row.pk}/outcome/{outcome}/?next=' in page
    ]


def test_still_open_records_the_entry_and_leaves_the_promise(today: date) -> None:
    client, user = client_for(Role.BILLING)
    operator = an_operator()
    old = promise(operator, by=date(2026, 10, 9))
    for body, extra in (("No news.", {"outcome": "open"}), ("Still waiting.", {})):
        response = client.post(
            f"/operators/{operator.pk}/contacts/new/",
            contact_post(previous=str(old.pk), body=body, **extra),
        )
        assert response.status_code == 302, body
        new = ContactEntry.objects.get(body=body)
        old.refresh_from_db()
        assert (old.status, old.closed_by) == (ContactStatus.OPEN, None)
        audit = AuditLog.objects.get(action="contact_entry.created", entity_id=str(new.pk))
        assert audit.after["previous"] == str(old.pk) and audit.after["outcome"] == "open"
        assert audit.actor == user
    assert not AuditLog.objects.filter(action="contact_entry.outcome_recorded").exists()


@pytest.mark.parametrize("outcome", ["open", "kept", "rescheduled", "broken"])
def test_the_previous_promise_block_takes_each_of_the_four_outcomes(
    today: date, outcome: str
) -> None:
    operator = an_operator()
    old = promise(operator, by=date(2026, 10, 9))
    kind = ContactKind.PROMISE if outcome == "rescheduled" else ContactKind.CALL
    added = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=kind, happened_on=TODAY, body="Spoke.", previous=old.pk, outcome=outcome
        ),
        actor=None,
    )
    assert added.ok and added.message == contacts.ADDED, added.message
    old.refresh_from_db()
    assert old.status == outcome
    assert old.closed_by == (None if outcome == "open" else added.entry)


def test_the_buttons_are_on_every_open_promise_row_for_billing_only(today: date) -> None:
    operator = an_operator()
    store = a_store("2477")
    lease = a_lease(store, operator)
    open_row = promise(operator, lease=lease, by=date(2026, 10, 9))
    kept = promise(operator, by=date(2026, 10, 9))
    contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.CALL, happened_on=TODAY, body="Paid.", previous=kept.pk, outcome="kept"
        ),
        actor=None,
    )
    gone = deleted(promise(operator, by=date(2026, 10, 12)))
    billing, _ = client_for(Role.BILLING)
    viewer, _ = client_for(Role.VIEWER)
    for path in (
        f"/operators/{operator.pk}/",
        f"/operators/{operator.pk}/contacts/",
        f"/stores/{store.pk}/?tab=contacts",
        f"/stores/{store.pk}/contacts/",
    ):
        page = table_of(page_of(billing, path))
        assert outcome_buttons(page, open_row) == ["kept", "rescheduled", "broken"], path
        assert outcome_buttons(page, kept) == [] and outcome_buttons(page, gone) == [], path
        assert "/outcome/" not in table_of(page_of(viewer, path)), path
    # The link comes back to the page it was on.
    page = page_of(billing, f"/stores/{store.pk}/?tab=contacts")
    assert f"/contacts/{open_row.pk}/outcome/kept/?next=%2Fstores%2F{store.pk}%2F" in page
    # An archived operator's promise has none.
    archived(operator)
    assert outcome_buttons(page_of(billing, f"/operators/{operator.pk}/"), open_row) == []


def test_kept_from_the_promise_closes_it_as_the_previous_promise_path_does(today: date) -> None:
    client, user = client_for(Role.BILLING)
    operator = an_operator()
    lease = a_lease(operator=operator)
    old = promise(operator, lease=lease, by=date(2026, 10, 9))
    form = page_of(client, outcome_url(old, "kept", f"/operators/{operator.pk}/contacts/"))
    assert "<h1>Kept: promise of" in form and 'name="promised_date"' not in form
    assert '<option value="call" selected>Call</option>' in form
    assert '<option value="promise"' not in form
    assert 'value="2026-10-06"' in form
    response = client.post(
        outcome_url(old, "kept"),
        outcome_form(kind="email", next=f"/operators/{operator.pk}/contacts/"),
    )
    new = ContactEntry.objects.get(body="Spoke to them.")
    assert response.status_code == 302
    assert response["Location"] == f"/operators/{operator.pk}/contacts/#contact-{new.pk}"
    old.refresh_from_db()
    assert (old.status, old.closed_by, old.kept_by_payment) == ("kept", new, None)
    assert (new.kind, new.lease, new.author, new.happened_on) == ("email", lease, user, TODAY)
    audit = AuditLog.objects.get(action="contact_entry.outcome_recorded", entity_id=str(old.pk))
    assert audit.after == {"status": "kept", "closed_by": str(new.pk), "kept_by_payment": None}


def test_reschedule_prefills_the_old_promise_and_takes_new_values(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    old = promise(operator, by=date(2026, 10, 9), amount="700.00")
    form = page_of(client, outcome_url(old, "rescheduled"))
    assert 'name="promised_date" value="2026-10-09"' in form
    assert 'name="promised_amount" value="700.00"' in form
    assert 'name="kind"' not in form
    # Without a new date: refused, nothing written.
    response = client.post(
        outcome_url(old, "rescheduled"), outcome_form(promised_date="", promised_amount="700.00")
    )
    assert response.status_code == 200 and "This field is required." in response.content.decode()
    assert ContactEntry.objects.count() == 1
    response = client.post(
        outcome_url(old, "rescheduled"),
        outcome_form(promised_date="2026-10-20", promised_amount="350.00", body="Half later."),
    )
    new = ContactEntry.objects.get(body="Half later.")
    assert response["Location"] == f"/operators/{operator.pk}/#contact-{new.pk}"
    old.refresh_from_db()
    assert (old.status, old.closed_by) == (ContactStatus.RESCHEDULED, new)
    assert (new.kind, new.promised_date, new.promised_amount, new.status) == (
        "promise",
        date(2026, 10, 20),
        D("350.00"),
        "open",
    )
    # The rule's automatic close did not run: the only outcome is the reschedule's.
    assert AuditLog.objects.filter(action="contact_entry.outcome_recorded").count() == 1


def test_broken_from_the_attention_list_comes_back_to_the_dashboard(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    old = promise(operator, by=date(2026, 10, 1))
    dashboard = page_of(client, "/")
    assert outcome_buttons(dashboard, old) == ["kept", "rescheduled", "broken"]
    assert f"/contacts/{old.pk}/outcome/broken/?next=%2F" in dashboard
    banners = page_of(client, f"/operators/{operator.pk}/")
    banners = banners[banners.index('id="attention"') : banners.index("</section>")]
    assert outcome_buttons(banners, old) == ["kept", "rescheduled", "broken"]
    response = client.post(
        outcome_url(old, "broken", "/"), outcome_form(kind="visit", body="Nothing paid.")
    )
    new = ContactEntry.objects.get(body="Nothing paid.")
    assert response["Location"] == f"/#contact-{new.pk}"
    old.refresh_from_db()
    assert (old.status, old.closed_by, new.kind) == (ContactStatus.BROKEN, new, "visit")
    assert contacts.broken_promises(TODAY) == []
    assert "/outcome/" not in page_of(client, "/")


def test_an_outcome_on_a_promise_no_longer_open_is_refused_with_the_sentence(
    today: date,
) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    old = promise(operator, by=date(2026, 10, 9))
    form = page_of(client, outcome_url(old, "kept"))  # opened, then closed meanwhile
    assert 'id="contact-outcome-form"' in form
    ContactEntry.objects.filter(pk=old.pk).update(status=ContactStatus.BROKEN)
    for method in (client.get, client.post):
        response = method(outcome_url(old, "kept"), outcome_form())
        assert response.status_code == 302
        assert response["Location"] == f"/operators/{operator.pk}/#contact-{old.pk}"
    page = page_of(client, f"/operators/{operator.pk}/")
    assert contacts.PROMISE_NOT_OPEN in page
    assert ContactEntry.objects.count() == 1
    gone = deleted(promise(operator, by=date(2026, 10, 10)))
    response = client.post(outcome_url(gone, "kept"), outcome_form())
    assert response.status_code == 302 and ContactEntry.objects.count() == 2
    assert contacts.DELETED in page_of(client, f"/operators/{operator.pk}/")
    live = promise(operator, by=date(2026, 10, 11))
    archived(operator)
    response = client.post(outcome_url(live, "kept"), outcome_form())
    assert response.status_code == 302 and ContactEntry.objects.count() == 3
    live.refresh_from_db()
    assert live.status == ContactStatus.OPEN


def test_the_outcome_form_refuses_a_card_number_a_promise_kind_and_unknown_routes(
    today: date,
) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    old = promise(operator, by=date(2026, 10, 9))
    response = client.post(outcome_url(old, "kept"), outcome_form(body=f"Card {CARD}"))
    assert response.status_code == 200
    assert CARD not in response.content.decode()
    response = client.post(outcome_url(old, "broken"), outcome_form(kind="promise"))
    assert response.status_code == 200 and "Select a valid choice." in response.content.decode()
    assert ContactEntry.objects.count() == 1
    old.refresh_from_db()
    assert old.status == ContactStatus.OPEN
    call = entry(operator)
    for path in (
        f"/contacts/{uuid.uuid4()}/outcome/kept/",
        "/contacts/not-a-uuid/outcome/kept/",
        f"/contacts/{call.pk}/outcome/kept/",
        f"/contacts/{old.pk}/outcome/open/",
        f"/contacts/{old.pk}/outcome/forgotten/",
    ):
        assert client.get(path).status_code == 404, path


def test_an_outcome_on_a_promise_without_a_store_asks_the_store_when_two_are_in_effect(
    today: date,
) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    old = promise(operator, by=date(2026, 10, 9))
    first, second = a_lease(operator=operator), a_lease(operator=operator)
    form = page_of(client, outcome_url(old, "kept"))
    assert 'name="lease"' in form
    response = client.post(outcome_url(old, "kept"), outcome_form())
    assert response.status_code == 200 and ContactEntry.objects.count() == 1
    client.post(outcome_url(old, "kept"), outcome_form(lease=str(second.pk)))
    new = ContactEntry.objects.get(body="Spoke to them.")
    assert new.lease == second
    old.refresh_from_db()
    assert old.status == ContactStatus.KEPT
    # A promise with a store keeps it; no choice is offered.
    on_first = promise(operator, lease=first, by=date(2026, 10, 9))
    assert 'name="lease"' not in page_of(client, outcome_url(on_first, "kept"))


def test_a_new_promise_closes_the_open_promise_of_its_lease_as_rescheduled(today: date) -> None:
    client, user = client_for(Role.BILLING)
    operator = an_operator()
    store = a_store("2477")
    # One lease in effect, so an entry may name no store; the other lease is pending.
    lease, other = a_lease(store, operator), a_lease(operator=operator, status="prospective")
    old = promise(operator, lease=lease, by=date(2026, 10, 9), amount="700.00")
    elsewhere = promise(operator, lease=other, by=date(2026, 10, 9))
    no_store = promise(operator, by=date(2026, 10, 9))
    gone = deleted(promise(operator, lease=lease, by=date(2026, 10, 12)))
    old.refresh_from_db()
    assert old.status == ContactStatus.RESCHEDULED and old.closed_by == gone
    ContactEntry.objects.filter(pk=old.pk).update(status=ContactStatus.OPEN, closed_by=None)
    # The store form warns beforehand.
    form = page_of(client, f"/stores/{store.pk}/contacts/new/")
    warning = (
        "Saving a promise closes the open promise of $700.00 by 2026-10-09 on this store "
        "as Rescheduled."
    )
    assert warning in form
    assert form.index('id="promise-supersede"') < form.index('name="promised_date"')
    response = client.post(
        f"/stores/{store.pk}/contacts/new/",
        contact_post(
            kind="promise",
            promised_date="2026-10-20",
            promised_amount="500.00",
            lease=str(lease.pk),
            previous=str(no_store.pk),
            outcome="open",
            body="New date.",
        ),
    )
    assert response.status_code == 302
    new = ContactEntry.objects.get(body="New date.")
    old.refresh_from_db()
    assert (old.status, old.closed_by) == (ContactStatus.RESCHEDULED, new)
    message = (
        "Promise recorded; the earlier promise of $700.00 by 2026-10-09 is closed as Rescheduled."
    )
    assert message in page_of(client, f"/stores/{store.pk}/?tab=contacts")
    for untouched in (elsewhere, no_store, gone):
        untouched.refresh_from_db()
        assert untouched.closed_by != new
    assert (elsewhere.status, no_store.status) == (ContactStatus.OPEN, ContactStatus.OPEN)
    audit = AuditLog.objects.filter(
        action="contact_entry.outcome_recorded", entity_id=str(old.pk)
    ).latest("at")
    assert audit.actor == user
    assert audit.after == {
        "status": "rescheduled",
        "closed_by": str(new.pk),
        "kept_by_payment": None,
        "auto": True,
    }
    # The no-store bucket is its own: a promise without a store closes only no_store.
    added = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.PROMISE,
            happened_on=TODAY,
            body="No store.",
            promised_date=date(2026, 10, 25),
            promised_amount=D("100.00"),
        ),
        actor=None,
    )
    no_store.refresh_from_db()
    assert no_store.status == ContactStatus.RESCHEDULED and no_store.closed_by == added.entry
    elsewhere.refresh_from_db()
    new.refresh_from_db()
    assert (elsewhere.status, new.status) == (ContactStatus.OPEN, ContactStatus.OPEN)


def test_an_entry_closing_another_promise_leaves_the_auto_closed_one_without_closed_by(
    today: date,
) -> None:
    operator = an_operator()
    lease_a, lease_b = a_lease(operator=operator), a_lease(operator=operator)
    on_a = promise(operator, lease=lease_a, by=date(2026, 10, 9))
    on_b = promise(operator, lease=lease_b, by=date(2026, 10, 9), amount="300.00")
    # Reschedule the promise of lease A onto lease B: it closes A by the outcome and B by
    # the rule; closed_by is one to one, so B's names nothing and its audit says auto.
    added = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.PROMISE,
            happened_on=TODAY,
            body="Moved.",
            lease=lease_b.pk,
            previous=on_a.pk,
            outcome="rescheduled",
        ),
        actor=None,
    )
    assert added.ok and added.message.startswith("Promise recorded; the earlier promise of $300")
    on_a.refresh_from_db()
    on_b.refresh_from_db()
    assert (on_a.status, on_a.closed_by) == (ContactStatus.RESCHEDULED, added.entry)
    assert (on_b.status, on_b.closed_by) == (ContactStatus.RESCHEDULED, None)


def test_the_add_buttons_and_the_form_say_add_entry(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator("Acme")
    for path in (f"/operators/{operator.pk}/", f"/operators/{operator.pk}/contacts/"):
        page = page_of(client, path)
        assert '<a class="btn" id="add-contact"' in page and ">Add entry</a>" in page, path
        assert "Add contact" not in page, path
    form = form_page(client, operator)
    assert "Monvera · Add entry: Acme</title>" in form and "<h1>Add entry: Acme</h1>" in form


def test_the_outcome_route_is_registered_for_billing() -> None:
    rule = registry.rule_for("ledger:contacts:outcome")
    assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}


# --- PLAN-012c-b D5: the closing entry shows what it closed -----------------------------------


def closing(
    operator: Operator,
    previous: ContactEntry,
    outcome: str,
    *,
    lease: Lease | None = None,
    kind: str = ContactKind.CALL,
    body: str = "Spoke to them.",
    by: date | None = None,
    amount: str | None = None,
) -> ContactEntry:
    added = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=kind,
            happened_on=TODAY,
            body=body,
            promised_date=by,
            promised_amount=D(amount) if amount is not None else None,
            lease=lease.pk if lease is not None else None,
            previous=previous.pk,
            outcome=outcome,
        ),
        actor=None,
    )
    assert added.ok and added.entry is not None, added.message
    return added.entry


def promise_cell(log: str, row: ContactEntry) -> str:
    """The Promise column (the fifth cell) of ``row`` in the table ``log``."""
    tr = log.split(f'id="contact-{row.pk}"')[1].split("</tr>")[0]
    cells: list[str] = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)
    return cells[4]


def closes_link(cell: str) -> tuple[str, str]:
    """The href and the text of the cell's "Closed ..." link."""
    [_, link] = cell.split('<div class="contact-closes">')
    href = link.split('href="')[1].split('"')[0]
    text = link.split('">', 1)[1].split("</a>")[0]
    return href, text


def three_closings(
    operator: Operator, lease: Lease
) -> dict[str, tuple[ContactEntry, ContactEntry]]:
    """A Kept, a Broken and a Rescheduled promise on ``lease``, each with its closing entry."""
    kept = promise(operator, lease=lease, by=date(2026, 10, 1), amount="2000.00")
    kept_by = closing(operator, kept, "kept", lease=lease, body="Paid in the office.")
    broken = promise(operator, lease=lease, by=date(2026, 10, 3), amount="300.00")
    broken_by = closing(operator, broken, "broken", lease=lease, body="Will not pay.")
    moved = promise(operator, lease=lease, by=date(2026, 10, 4), amount="400.00")
    moved_by = closing(
        operator,
        moved,
        "rescheduled",
        lease=lease,
        kind=ContactKind.PROMISE,
        by=date(2026, 10, 20),
        amount="450.00",
        body="New date.",
    )
    return {
        "Closed $2,000.00 by 2026-10-01 as Kept": (kept, kept_by),
        "Closed $300.00 by 2026-10-03 as Broken": (broken, broken_by),
        "Closed $400.00 by 2026-10-04 as Rescheduled": (moved, moved_by),
    }


def test_the_closing_entries_name_their_promise_on_every_page_and_in_the_csv(
    today: date,
) -> None:
    operator = an_operator("Acme")
    store = a_store("2477")
    lease = a_lease(store, operator)
    closings = three_closings(operator, lease)
    plain = entry(operator, lease=lease, body="Just a call.")
    client, _ = client_for(Role.BILLING)
    for path in (
        f"/operators/{operator.pk}/",
        f"/operators/{operator.pk}/contacts/",
        f"/operators/{operator.pk}/contacts/?status=all",
        f"/stores/{store.pk}/?tab=contacts",
        f"/stores/{store.pk}/contacts/",
    ):
        log = table_of(page_of(client, path))
        for sentence, (closed, closer) in closings.items():
            href, text = closes_link(promise_cell(log, closer))
            assert text == sentence, path
            # The original promise is on this page: the link stays here.
            assert href == f"{escape(path)}#contact-{closed.pk}", path
            # The original promise itself names nothing it closed.
            assert "contact-closes" not in promise_cell(log, closed), path
        assert promise_cell(log, plain) == "—", path
    # The Rescheduled closing entry is a promise: its own promise first, then the sentence.
    _, moved_by = closings["Closed $400.00 by 2026-10-04 as Rescheduled"]
    cell = promise_cell(table_of(page_of(client, f"/operators/{operator.pk}/")), moved_by)
    own = cell.index("$450.00 by 2026-10-20")
    assert own < cell.index('<div class="contact-closes">') < cell.index("as Rescheduled")
    assert 'promise-status promise-open">Open<' in cell
    # The call closings show the sentence alone, without "—".
    _, kept_by = closings["Closed $2,000.00 by 2026-10-01 as Kept"]
    kept_cell = promise_cell(table_of(page_of(client, f"/operators/{operator.pk}/")), kept_by)
    assert kept_cell.startswith('<div class="contact-closes">') and "—" not in kept_cell
    # The CSV column reads the same sentence without the link.
    body = b"".join(
        client.get(f"/operators/{operator.pk}/contacts/export.csv").streaming_content  # type: ignore[attr-defined]
    ).decode()
    [header, *records] = list(csv.reader(io.StringIO(body)))
    column = header.index("Closes promise")
    assert header[column - 1] == "Promise status"
    by_note = {record[3]: record[column] for record in records}
    assert by_note["Paid in the office."] == "Closed $2,000.00 by 2026-10-01 as Kept"
    assert by_note["Will not pay."] == "Closed $300.00 by 2026-10-03 as Broken"
    assert by_note["New date."] == "Closed $400.00 by 2026-10-04 as Rescheduled"
    assert by_note["Just a call."] == "" and "href" not in body


def test_the_store_page_links_to_the_operator_page_for_a_promise_not_on_that_store(
    today: date,
) -> None:
    operator = an_operator("Acme")
    here, there = a_store("2477"), a_store("2476")
    # One lease in effect, so a promise may name no store; this store's lease is pending.
    their_lease = a_lease(there, operator)
    my_lease = a_lease(here, operator, status="prospective")
    on_there = promise(operator, lease=their_lease, by=date(2026, 10, 1), amount="2000.00")
    moved = closing(operator, on_there, "broken", lease=my_lease, body="Moved here.")
    no_store = promise(operator, by=date(2026, 10, 2), amount="100.00")
    kept = closing(operator, no_store, "kept", lease=my_lease, body="Paid here.")
    client, _ = client_for(Role.BILLING)
    operator_page = f"/operators/{operator.pk}/"
    for path in (f"/stores/{here.pk}/?tab=contacts", f"/stores/{here.pk}/contacts/"):
        log = table_of(page_of(client, path))
        assert closes_link(promise_cell(log, moved)) == (
            f"{operator_page}#contact-{on_there.pk}",
            "Closed $2,000.00 by 2026-10-01 as Broken",
        )
        assert closes_link(promise_cell(log, kept)) == (
            f"{operator_page}#contact-{no_store.pk}",
            "Closed $100.00 by 2026-10-02 as Kept",
        )
    # On the operator page both promises are listed: the links stay there.
    log = table_of(page_of(client, operator_page))
    assert closes_link(promise_cell(log, moved))[0] == f"{operator_page}#contact-{on_there.pk}"
    assert closes_link(promise_cell(log, kept))[0] == f"{operator_page}#contact-{no_store.pk}"


def test_an_entry_whose_closed_by_names_nothing_shows_nothing(today: date) -> None:
    operator = an_operator()
    lease_a, lease_b = a_lease(operator=operator), a_lease(operator=operator)
    on_a = promise(operator, lease=lease_a, by=date(2026, 10, 9), amount="200.00")
    on_b = promise(operator, lease=lease_b, by=date(2026, 10, 9), amount="300.00")
    # The closing entry closes A by its outcome and B by the one-open rule; B's closed_by
    # stays null (one to one), so only A is named.
    moved = closing(
        operator,
        on_a,
        "rescheduled",
        lease=lease_b,
        kind=ContactKind.PROMISE,
        by=date(2026, 10, 20),
        body="Moved.",
    )
    on_b.refresh_from_db()
    assert on_b.status == ContactStatus.RESCHEDULED and on_b.closed_by is None
    client, _ = client_for(Role.BILLING)
    log = table_of(page_of(client, f"/operators/{operator.pk}/"))
    cell = promise_cell(log, moved)
    assert cell.count("contact-closes") == 1 and "$200.00 by 2026-10-09 as Rescheduled" in cell
    assert "$300.00" not in cell
    for row in (on_a, on_b):
        assert "contact-closes" not in promise_cell(log, row)
    assert contacts.closed_promise(ContactEntry.objects.get(pk=on_b.pk)) is None


def test_a_deleted_promise_or_closing_entry_keeps_the_sentence(today: date) -> None:
    operator = an_operator()
    lease = a_lease(operator=operator)
    original = promise(operator, lease=lease, by=date(2026, 10, 1), amount="2000.00")
    closer = closing(operator, original, "broken", lease=lease, body="Will not pay.")
    sentence = "Closed $2,000.00 by 2026-10-01 as Broken"
    deleted(original, "Entered twice.")
    path = f"/operators/{operator.pk}/"
    for role in (Role.BILLING, Role.VIEWER):
        client, _ = client_for(role)
        cell = promise_cell(table_of(page_of(client, path)), closer)
        assert closes_link(cell) == (f"{path}#contact-{original.pk}", sentence), role
    # The deleted closing entry's row shows it too, struck through like the rest of the row.
    deleted(closer, "Wrong operator.")
    admin, _ = client_for(Role.ADMIN)
    cell = promise_cell(table_of(page_of(admin, path)), closer)
    assert cell == (
        f'<div class="contact-closes"><s><a href="{path}#contact-{original.pk}">'
        f"{sentence}</a></s></div>"
    )
    body = b"".join(
        admin.get(f"{path}contacts/export.csv").streaming_content  # type: ignore[attr-defined]
    ).decode()
    assert sentence in body


def test_the_pages_read_the_closed_promises_in_their_own_queries(today: date) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    operator = an_operator()
    lease = a_lease(operator=operator)
    client, _ = client_for(Role.BILLING)

    def count(path: str) -> int:
        with CaptureQueriesContext(connection) as queries:
            assert client.get(path).status_code == 200
        return len(queries)

    def closed_pair(n: int) -> None:
        old = promise(operator, lease=lease, by=date(2026, 10, 1), amount=f"{100 + n}.00")
        closing(operator, old, ("kept", "broken")[n % 2], lease=lease, body=f"Closer {n}.")

    paths = (
        f"/operators/{operator.pk}/",
        f"/operators/{operator.pk}/contacts/",
        f"/stores/{lease.store_id}/?tab=contacts",
        f"/stores/{lease.store_id}/contacts/",
    )
    entry(operator, lease=lease, body="A call.")
    plain = {path: count(path) for path in paths}
    closed_pair(0)
    one = {path: count(path) for path in paths}
    for n in range(1, 6):
        closed_pair(n)
    many = {path: count(path) for path in paths}
    assert plain == one == many
    log = table_of(page_of(client, paths[0]))
    assert log.count('<div class="contact-closes">') == 6
