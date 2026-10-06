"""The contact log (docs/qa/PLAN-011b D7, §7): billing adds entries, a promise carries its
date and amount and nothing else does, a later entry closes a promise, the operator page
lists the entries newest first with a CSV export, and the exact broken-promise rule."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AuditLog,
    ContactEntry,
    ContactKind,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
)
from ledger.services import attention, contacts
from tests.auth_helpers import make_user
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 6)
YESTERDAY = TODAY - timedelta(days=1)
CARD = "4111 1111 1111 1111"


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def an_actor() -> User:
    user, _ = make_user(f"contacts-{uuid.uuid4().hex[:8]}", Role.BILLING)
    return user


def promise(
    operator: Operator,
    *,
    on: date = date(2026, 10, 1),
    by: date = YESTERDAY,
    amount: str = "1500.00",
    actor: User | None = None,
) -> ContactEntry:
    outcome = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.PROMISE,
            happened_on=on,
            body="Will pay by Monday.",
            promised_date=by,
            promised_amount=D(amount),
        ),
        actor=actor,
    )
    assert outcome.ok and outcome.entry is not None, outcome.message
    return outcome.entry


def owing_operator(name: str = "Acme") -> Operator:
    operator = an_operator(name)
    lease = a_lease(a_store(f"Store {name}"), operator)
    a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.UNPAID, "3500.00")
    return operator


# --- The service --------------------------------------------------------------------------


def test_each_kind_is_recorded_with_an_audit_row() -> None:
    operator = an_operator()
    actor = an_actor()
    for kind in (ContactKind.CALL, ContactKind.EMAIL, ContactKind.VISIT):
        outcome = contacts.add_entry(
            operator.pk,
            contacts.ContactInput(kind=kind, happened_on=TODAY, body=f"A {kind}."),
            actor=actor,
        )
        assert outcome.ok, outcome.message
    entry = promise(operator, actor=actor)
    assert (entry.promised_date, entry.promised_amount, entry.author) == (
        YESTERDAY,
        D("1500.00"),
        actor,
    )
    assert AuditLog.objects.filter(action="contact_entry.created").count() == 4


@pytest.mark.parametrize(
    ("data", "message", "field"),
    [
        (
            {"kind": ContactKind.PROMISE, "promised_amount": D("10.00")},
            contacts.PROMISE_FIELDS_REQUIRED,
            "promised_date",
        ),
        (
            {"kind": ContactKind.PROMISE, "promised_date": TODAY},
            contacts.PROMISE_FIELDS_REQUIRED,
            "promised_amount",
        ),
        ({"kind": ContactKind.CALL, "promised_date": TODAY}, contacts.PROMISE_FIELDS_ONLY, None),
        (
            {"kind": ContactKind.VISIT, "promised_amount": D("5.00")},
            contacts.PROMISE_FIELDS_ONLY,
            None,
        ),
        (
            {"kind": ContactKind.PROMISE, "promised_date": TODAY, "promised_amount": D("0")},
            contacts.AMOUNT_POSITIVE,
            "promised_amount",
        ),
        (
            {
                "kind": ContactKind.PROMISE,
                "promised_date": TODAY,
                "promised_amount": D("-1.00"),
            },
            contacts.AMOUNT_POSITIVE,
            "promised_amount",
        ),
        ({"kind": ContactKind.CALL, "body": " ​\n"}, contacts.BODY_REQUIRED, "body"),
        ({"kind": ContactKind.CALL, "body": f"Card {CARD}"}, contacts.BODY_PAYMENT_DATA, "body"),
        ({"kind": "letter"}, "Choose what kind of contact this was.", "kind"),
    ],
)
def test_refusals_change_nothing(data: dict[str, object], message: str, field: str | None) -> None:
    operator = an_operator()
    values: dict[str, object] = {"happened_on": TODAY, "body": "Note.", **data}
    outcome = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(**values),  # type: ignore[arg-type]
        actor=None,
    )
    assert not outcome.ok and outcome.message == message
    if field is not None:
        assert outcome.field == field
    assert not ContactEntry.objects.exists()


def test_the_database_holds_the_promise_rule_too() -> None:
    operator = an_operator()
    with pytest.raises(IntegrityError), transaction.atomic():
        ContactEntry.objects.bulk_create(
            [ContactEntry(operator=operator, kind="promise", happened_on=TODAY, body="x")]
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        ContactEntry.objects.bulk_create(
            [
                ContactEntry(
                    operator=operator,
                    kind="call",
                    happened_on=TODAY,
                    body="x",
                    promised_date=TODAY,
                    promised_amount=D("1.00"),
                )
            ]
        )


def test_a_later_entry_closes_an_open_promise_of_the_same_operator_only() -> None:
    operator = an_operator()
    other = an_operator()
    open_promise = promise(operator)
    others_promise = promise(other)
    refused = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.CALL, happened_on=TODAY, body="Paid?", closes=others_promise.pk
        ),
        actor=None,
    )
    assert not refused.ok and refused.message == contacts.CLOSES_UNKNOWN
    closing = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.CALL,
            happened_on=TODAY,
            body="Agreed new plan.",
            closes=open_promise.pk,
        ),
        actor=None,
    )
    assert closing.ok and closing.entry is not None
    open_promise.refresh_from_db()
    assert open_promise.closed_by == closing.entry
    assert AuditLog.objects.filter(
        action="contact_entry.promise_closed", entity_id=str(open_promise.pk)
    ).exists()
    # A closed promise cannot be closed twice.
    again = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.CALL, happened_on=TODAY, body="Again.", closes=open_promise.pk
        ),
        actor=None,
    )
    assert not again.ok and again.message == contacts.CLOSES_UNKNOWN


def test_an_archived_operator_takes_no_entry() -> None:
    operator = an_operator()
    Operator.objects.filter(pk=operator.pk).update(archived_at=timezone.now())
    outcome = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(kind=ContactKind.CALL, happened_on=TODAY, body="Hello."),
        actor=None,
    )
    assert not outcome.ok and outcome.message == contacts.ARCHIVED


# --- Broken promises -----------------------------------------------------------------------


def test_a_promise_dated_yesterday_with_an_open_balance_is_broken_and_on_the_list() -> None:
    operator = owing_operator()
    entry = promise(operator, by=YESTERDAY)
    assert contacts.broken_promises(TODAY) == [entry]
    items = [item for item in attention.attention(TODAY).items if item.kind == "broken_promise"]
    assert len(items) == 1
    assert items[0].text == f"Promise to pay $1,500.00 by {YESTERDAY}: Acme, not kept"
    assert items[0].url == f"/operators/{operator.pk}/#contacts"
    assert items[0].severity == attention.HIGH
    # The operator page shows it as a banner.
    banners = attention.attention(TODAY, operator=operator.pk).items
    assert any(item.kind == "broken_promise" for item in banners)


def test_a_promise_due_today_or_later_is_not_broken() -> None:
    operator = owing_operator()
    promise(operator, by=TODAY)
    promise(operator, by=TODAY + timedelta(days=3))
    assert contacts.broken_promises(TODAY) == []


def test_without_an_open_balance_a_past_promise_is_not_broken() -> None:
    operator = an_operator()
    promise(operator, by=YESTERDAY)
    assert contacts.broken_promises(TODAY) == []


def test_a_payment_settled_on_or_after_the_promised_date_keeps_the_promise() -> None:
    operator = owing_operator()
    lease = operator.leases.get()
    entry = promise(operator, by=date(2026, 10, 3))
    paid = a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.PAID, "100.00", "100.00")
    # Settled the day before the promised date: does not count.
    a_payment(paid, "100.00", date(2026, 10, 2))
    assert contacts.broken_promises(TODAY) == [entry]
    # A refund on the date does not count either.
    refund = a_payment(paid, "50.00", date(2026, 10, 3))
    Payment.objects.filter(pk=refund.pk).update(
        is_refund=True, parent_payment=Payment.objects.exclude(pk=refund.pk).first()
    )
    assert contacts.broken_promises(TODAY) == [entry]
    # A pending payment does not count.
    a_payment(paid, "100.00", None, PaymentStatus.PENDING, initiated_on=date(2026, 10, 4))
    assert contacts.broken_promises(TODAY) == [entry]
    # Settled on the promised date: kept.
    a_payment(paid, "100.00", date(2026, 10, 3))
    assert contacts.broken_promises(TODAY) == []


def test_a_settled_one_off_payment_billed_to_the_operator_counts() -> None:
    from tests.receivables_helpers import a_one_off_charge

    operator = owing_operator()
    entry = promise(operator, by=date(2026, 10, 3))
    charge = a_one_off_charge(operator, "50.00", date(2026, 9, 1))
    assert contacts.broken_promises(TODAY) == [entry]
    Payment.objects.create(
        one_off_charge=charge,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("50.00"),
        status=PaymentStatus.SETTLED,
        settled_on=date(2026, 10, 5),
    )
    assert contacts.broken_promises(TODAY) == []


def test_another_operators_payment_does_not_keep_the_promise() -> None:
    operator = owing_operator("Acme")
    other = owing_operator("Beta")
    entry = promise(operator, by=date(2026, 10, 3))
    charge = a_rent_charge(other.leases.get(), date(2026, 9, 1), RentChargeStatus.PAID)
    a_payment(charge, "100.00", date(2026, 10, 5))
    assert contacts.broken_promises(TODAY) == [entry]
    assert contacts.broken_promises(TODAY, operator=other.pk) == []


def test_a_closed_promise_is_not_broken() -> None:
    operator = owing_operator()
    entry = promise(operator, by=YESTERDAY)
    contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.VISIT, happened_on=TODAY, body="New plan.", closes=entry.pk
        ),
        actor=None,
    )
    assert contacts.broken_promises(TODAY) == []


def test_broken_promises_read_a_fixed_number_of_queries() -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def count() -> int:
        with CaptureQueriesContext(connection) as queries:
            contacts.broken_promises(TODAY)
        return len(queries)

    promise(owing_operator("First"))
    small = count()
    for n in range(6):
        promise(owing_operator(f"Many {n}"))
    assert count() == small


# --- The pages -----------------------------------------------------------------------------


def contact_post(**overrides: str) -> dict[str, str]:
    data = {
        "kind": "call",
        "happened_on": "2026-10-06",
        "promised_date": "",
        "promised_amount": "",
        "closes": "",
        "body": "Called; will pay Friday.",
    }
    return {**data, **overrides}


def test_billing_adds_from_the_operator_page_and_the_page_lists_newest_first(today: date) -> None:
    client, user = client_for(Role.BILLING)
    operator = an_operator("Acme")
    page = client.get(f"/operators/{operator.pk}/").content.decode()
    assert 'id="contacts"' in page and f"/operators/{operator.pk}/contacts/new/" in page
    form = client.get(f"/operators/{operator.pk}/contacts/new/")
    assert form.status_code == 200
    response = client.post(f"/operators/{operator.pk}/contacts/new/", contact_post())
    assert response.status_code == 302
    assert response["Location"] == f"/operators/{operator.pk}/#contacts"
    client.post(
        f"/operators/{operator.pk}/contacts/new/",
        contact_post(
            kind="promise",
            happened_on="2026-10-06",
            promised_date="2026-10-09",
            promised_amount="1500.00",
            body="Promised the full balance.",
        ),
    )
    client.post(
        f"/operators/{operator.pk}/contacts/new/",
        contact_post(happened_on="2026-10-01", body="First visit."),
    )
    entries = list(ContactEntry.objects.filter(operator=operator))
    assert len(entries) == 3 and all(entry.author == user for entry in entries)
    page = client.get(f"/operators/{operator.pk}/").content.decode()
    table = page.split('id="contact-entries"')[1].split("</table>")[0]
    assert table.index("Promised the full balance.") < table.index("Called; will pay Friday.")
    assert table.index("Called; will pay Friday.") < table.index("First visit.")
    assert "$1500.00 by 2026-10-09" in table
    full = client.get(f"/operators/{operator.pk}/contacts/").content.decode()
    assert "First visit." in full


def test_a_refused_entry_shows_the_form_with_the_reason(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    operator = an_operator()
    response = client.post(
        f"/operators/{operator.pk}/contacts/new/",
        contact_post(kind="promise", promised_amount="10.00"),
    )
    assert response.status_code == 200
    assert contacts.PROMISE_FIELDS_REQUIRED in response.content.decode()
    response = client.post(f"/operators/{operator.pk}/contacts/new/", contact_post(body=CARD))
    assert response.status_code == 200
    assert CARD not in response.content.decode()
    assert not ContactEntry.objects.exists()


def test_a_viewer_reads_but_cannot_add() -> None:
    client, _ = client_for(Role.VIEWER)
    operator = an_operator()
    assert client.get(f"/operators/{operator.pk}/contacts/").status_code == 200
    assert client.get(f"/operators/{operator.pk}/contacts/new/").status_code == 403
    assert client.post(f"/operators/{operator.pk}/contacts/new/", contact_post()).status_code == (
        403
    )
    page = client.get(f"/operators/{operator.pk}/").content.decode()
    assert 'id="add-contact"' not in page
    assert not ContactEntry.objects.exists()


def test_the_routes_are_registered_with_their_roles() -> None:
    rule = registry.rule_for("ledger:operators:contacts_add")
    assert rule is not None and rule.min_role is Role.BILLING
    assert {"ledger:operators:contacts", "ledger:operators:contacts_export"} <= (
        registry.READ_ONLY_ROUTE_NAMES
    )


def test_unknown_operator_is_404() -> None:
    client, _ = client_for(Role.BILLING)
    for path in (
        f"/operators/{uuid.uuid4()}/contacts/",
        f"/operators/{uuid.uuid4()}/contacts/new/",
        "/operators/not-a-uuid/contacts/export.csv",
    ):
        assert client.get(path).status_code == 404, path


def test_the_csv_has_readable_headers_and_every_entry_newest_first(today: date) -> None:
    client, user = client_for(Role.VIEWER)
    operator = an_operator()
    contacts.add_entry(
        operator.pk,
        contacts.ContactInput(kind=ContactKind.CALL, happened_on=date(2026, 9, 1), body="=SUM(A1)"),
        actor=user,
    )
    promise(operator, on=date(2026, 10, 2), by=date(2026, 10, 9), actor=user)
    response = client.get(f"/operators/{operator.pk}/contacts/export.csv")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    body = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == list(contacts.CSV_HEADER)
    assert rows[1][:6] == [
        "2026-10-02",
        "Promise to pay",
        "Will pay by Monday.",
        "2026-10-09",
        "1500.00",
        "no",
    ]
    # Formula-safe text; no promise fields on a call.
    assert rows[2][:6] == ["2026-09-01", "Call", "'=SUM(A1)", "", "", ""]
    assert rows[2][6] == user.get_username()
