"""The contact log grows up (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md D2 to
D5, §6 for Batch A): the author's 15-minute edit window, appended corrections, the admin's
soft delete with a reason (struck through for billing and admin, hidden from viewers and
their CSV), a lease on an entry with the store page's section, the automatic close of a
promise by a settled payment (handler, manual payment and the daily sweep), the outcome of
the previous promise, the status filter and the broken-promise rule reading ``status``."""

from __future__ import annotations

import csv
import io
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
    page = page_of(client, f"/stores/{store.pk}/")
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
    assert response["Location"] == f"/stores/{store.pk}/#contacts"
    added = ContactEntry.objects.get(body="Nope.")
    assert added.lease == lease and added.operator == operator
    viewer, _ = client_for(Role.VIEWER)
    assert "/contacts/new/" not in page_of(viewer, f"/stores/{store.pk}/")
    assert viewer.get(f"/stores/{store.pk}/contacts/new/").status_code == 403


def test_a_store_without_a_lease_in_effect_offers_no_add_entry() -> None:
    store: Store = a_store("3000")
    client, _ = client_for(Role.BILLING)
    page = page_of(client, f"/stores/{store.pk}/")
    assert "No contact entries." in page and f"/stores/{store.pk}/contacts/new/" not in page
    response = client.get(f"/stores/{store.pk}/contacts/new/")
    assert response.status_code == 302 and response["Location"] == f"/stores/{store.pk}/#contacts"


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
        assert add_link(store) in page_of(client, f"/stores/{store.pk}/")
    client, _ = client_for(Role.BILLING)
    form = page_of(client, f"/stores/{store.pk}/contacts/new/")
    assert f"<h1>Add contact: Acme, store 2477{suffix}</h1>" in form
    assert f'<option value="{lease.pk}" selected>Store 2477{label}</option>' in form
    response = client.post(
        f"/stores/{store.pk}/contacts/new/", contact_post(lease=str(lease.pk), body="Old lease.")
    )
    assert response.status_code == 302
    assert response["Location"] == f"/stores/{store.pk}/#contacts"
    added = ContactEntry.objects.get()
    assert (added.operator, added.lease, added.body) == (operator, lease, "Old lease.")
    assert "Old lease." in table_of(page_of(client, f"/stores/{store.pk}/"))
    assert "Old lease." in table_of(page_of(client, f"/operators/{operator.pk}/"))
    viewer, _ = client_for(Role.VIEWER)
    assert "/contacts/new/" not in page_of(viewer, f"/stores/{store.pk}/")
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
    assert "<h1>Add contact: New, store 2477</h1>" in form


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
    assert "<h1>Add contact: Second, store 2477, lease ended 2024-06-30</h1>" in form
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
    assert add_link(store) in page_of(client, f"/stores/{store.pk}/")
    response = client.post(
        f"/stores/{store.pk}/contacts/new/", contact_post(lease=str(older.pk), body="Live.")
    )
    assert response.status_code == 302
    added = ContactEntry.objects.get()
    assert added.lease == older and added.operator.display_name == "Live"
    # With only archived or operator-less leases left, no button and the route refuses.
    archived(older.operator)  # type: ignore[arg-type]
    assert contacts.store_contact_lease(store, TODAY) is None
    assert add_link(store) not in page_of(client, f"/stores/{store.pk}/")
    response = client.get(f"/stores/{store.pk}/contacts/new/", follow=True)
    assert response.redirect_chain == [(f"/stores/{store.pk}/#contacts", 302)]
    assert escape(NO_STORE_LEASE) in response.content.decode()
    response = client.post(f"/stores/{store.pk}/contacts/new/", contact_post(body="Gone."))
    assert response.status_code == 302 and ContactEntry.objects.count() == 1


def test_a_store_with_no_lease_shows_the_existing_message(today: date) -> None:
    store = a_store("3000")
    client, _ = client_for(Role.BILLING)
    assert add_link(store) not in page_of(client, f"/stores/{store.pk}/")
    response = client.get(f"/stores/{store.pk}/contacts/new/", follow=True)
    assert response.redirect_chain == [(f"/stores/{store.pk}/#contacts", 302)]
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
    response = client.post(f"/operators/{operator.pk}/contacts/new/", contact_post())
    assert response.status_code == 200
    assert "Choose kept, rescheduled or broken." in response.content.decode()
    assert ContactEntry.objects.count() == 1


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
    for previous, outcome in ((None, "kept"), (old.pk, ""), (old.pk, "forgotten")):
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
