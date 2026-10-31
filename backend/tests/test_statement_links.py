"""The signed statement link of the collection e-mails (docs/qa/PLAN-011b D4, Batch C).

``/statement/<token>/`` opens without a session: the token is a ``TimestampSigner`` value
with its own salt, valid 30 days, revocable per operator. Tamper, expiry, revocation,
the rate limit, what the page leaves out, the audit row, the log lines, and that the
staff statement is unchanged.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.core import signing
from django.test import Client

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Lease,
    Note,
    NoteEntityType,
    OneOffCategory,
    OneOffCharge,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
)
from ledger.services import statement_links
from ledger.views import statement_links as views
from payments.models import ExternalRef
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
BANK = "pm-secret-bank-ref-0001"
INTERNAL = "Internal: call the owner's brother first"


@pytest.fixture(autouse=True)
def fresh_limiter() -> Iterator[None]:
    views.STATEMENT_LIMITER.reset()
    yield
    views.STATEMENT_LIMITER.reset()


@pytest.fixture
def operator() -> Operator:
    """An operator with an overdue rent charge, a paid one, a one-off charge with an
    internal note, an operator note and a linked bank method."""
    owner = an_operator("Acme Stores LLC")
    Operator.objects.filter(pk=owner.pk).update(notes=INTERNAL)
    lease = a_lease(a_store("Store 77 Springfield"), owner)
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, "1850.00")
    paid = a_rent_charge(lease, date(2026, 8, 1), RentChargeStatus.PAID, "1850.00", "1850.00")
    Payment.objects.create(
        rent_charge=paid,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D("1850.00"),
        status=PaymentStatus.SETTLED,
        settled_on=date(2026, 8, 5),
        initiated_on=date(2026, 8, 3),
    )
    OneOffCharge.objects.create(
        operator=owner,
        lease=lease,
        category=OneOffCategory.DAMAGE,
        collection="manual",
        description="Broken shelf",
        note=INTERNAL,
        price=D("120.00"),
        due_date=date(2026, 10, 1),
    )
    Note.objects.create(entity_type=NoteEntityType.OPERATOR, entity_id=owner.pk, body=INTERNAL)
    ExternalRef.objects.create(
        entity_type="operator",
        entity_id=owner.pk,
        provider="stax_pay",
        external_kind="payment_method",
        external_id=BANK,
    )
    return Operator.objects.get(pk=owner.pk)


def path(token: str) -> str:
    return f"/statement/{token}/"


def token_at(operator: Operator, moment: float, monkeypatch: pytest.MonkeyPatch) -> str:
    with monkeypatch.context() as patched:
        patched.setattr("django.core.signing.time.time", lambda: moment)
        return statement_links.make_token(operator)


# --- The page --------------------------------------------------------------------------------


def test_the_link_opens_the_statement_without_a_session(operator: Operator) -> None:
    response = Client().get(path(statement_links.make_token(operator)))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Statement: Acme Stores LLC" in body
    assert "Rent Sep 2026" in body and "1850.00" in body
    assert "Damage: Broken shelf" in body
    assert 'id="open-total">1970.00<' in body
    assert "Rent Aug 2026" in body  # the payment history of the last 12 months
    assert "Global Retail Groups" in body
    assert response["Cache-Control"] == "private, no-store"
    assert response["Referrer-Policy"] == "no-referrer"


def test_the_page_carries_no_bank_data_no_internal_note_and_no_link_into_the_app(
    operator: Operator,
) -> None:
    body = Client().get(path(statement_links.make_token(operator))).content.decode()
    assert BANK not in body and BANK[-4:] not in body
    assert INTERNAL not in body and "call the owner" not in body
    assert "<a " not in body and not has_token(body, "href")
    assert "<script" not in body and "<form" not in body
    for internal in ("/operators/", "/returns/", "/admin/", "/static/", "csrf"):
        assert not has_token(body, internal), internal


def test_every_open_is_audited_with_the_operator_only(operator: Operator) -> None:
    token = statement_links.make_token(operator)
    Client().get(path(token))
    Client().get(path(token))
    rows = AuditLog.objects.filter(action=statement_links.OPENED_ACTION)
    assert rows.count() == 2
    for row in rows:
        assert row.entity_id == str(operator.pk) and row.actor is None
        assert row.after == {"operator": str(operator.pk)}
    signature = token.rsplit(":", 1)[1]
    for row in AuditLog.objects.all():
        assert signature not in str(row.before) + str(row.after)


def test_history_is_the_last_twelve_months() -> None:
    selected = statement_links.history_range(TODAY)
    assert (selected.from_month, selected.to_month) == (date(2025, 11, 1), date(2026, 10, 1))


# --- Refusals --------------------------------------------------------------------------------


def _tampered(token: str) -> list[str]:
    value, stamp, signature = token.split(":")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    other = "00000000-0000-0000-0000-000000000000"
    return [
        f"{value}:{stamp}:{flipped}",
        f"{other}:{stamp}:{signature}",
        f"{value}:{signing.b62_encode(signing.b62_decode(stamp) + 86400)}:{signature}",
        f"{value}:{stamp}",
        value,
        f"{value}:{stamp}:{signature}x",
        "x" * 300,
    ]


def test_a_tampered_token_is_an_empty_404_and_never_logged(
    operator: Operator, caplog: pytest.LogCaptureFixture
) -> None:
    token = statement_links.make_token(operator)
    with caplog.at_level(logging.DEBUG):
        for bad in _tampered(token):
            response = Client().get(path(bad))
            assert response.status_code == 404, bad
            assert response.content == b""
            assert response["Cache-Control"] == "private, no-store"
    assert token.rsplit(":", 1)[1] not in caplog.text
    assert str(operator.pk) not in caplog.text
    assert "/statement/[token]/" in caplog.text
    assert not AuditLog.objects.filter(action=statement_links.OPENED_ACTION).exists()


def test_a_token_signed_with_another_salt_is_refused(operator: Operator) -> None:
    other = signing.TimestampSigner(salt="ledger.something-else").sign(str(operator.pk))
    plain = signing.TimestampSigner().sign(str(operator.pk))
    for token in (other, plain):
        assert statement_links.operator_for(token) is None
        assert Client().get(path(token)).status_code == 404


def test_a_token_is_valid_for_30_days(operator: Operator, monkeypatch: pytest.MonkeyPatch) -> None:
    issued = time.time() - 30 * 86400 + 60
    fresh = token_at(operator, issued, monkeypatch)
    assert statement_links.operator_for(fresh) == operator
    expired = token_at(operator, time.time() - 30 * 86400 - 60, monkeypatch)
    assert statement_links.operator_for(expired) is None
    assert Client().get(path(expired)).status_code == 404


def test_revocation_refuses_every_earlier_link_and_not_later_ones(
    operator: Operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = token_at(operator, time.time() - 3600, monkeypatch)
    client, _ = client_for(Role.BILLING)
    response = client.post(f"/operators/{operator.pk}/statement-links/revoke/")
    assert response.status_code == 302
    operator.refresh_from_db()
    assert operator.statement_links_revoked_at is not None
    assert Client().get(path(before)).status_code == 404
    after = token_at(operator, time.time() + 5, monkeypatch)
    assert Client().get(path(after)).status_code == 200
    row = AuditLog.objects.get(action=statement_links.REVOKED_ACTION)
    assert row.entity_id == str(operator.pk) and row.actor is not None


def test_a_viewer_cannot_revoke(operator: Operator) -> None:
    client, _ = client_for(Role.VIEWER)
    assert client.post(f"/operators/{operator.pk}/statement-links/revoke/").status_code == 403
    operator.refresh_from_db()
    assert operator.statement_links_revoked_at is None


def test_the_operator_page_offers_the_revoke_to_billing(operator: Operator) -> None:
    billing = client_for(Role.BILLING)[0].get(f"/operators/{operator.pk}/").content.decode()
    viewer = client_for(Role.VIEWER)[0].get(f"/operators/{operator.pk}/").content.decode()
    assert 'id="statement-links-revoke-form"' in billing
    assert 'id="statement-links-revoke-form"' not in viewer
    assert "30 days" in viewer


def test_a_deleted_operator_id_is_refused() -> None:
    owner = an_operator("Gone")
    token = statement_links.make_token(owner)
    Operator.objects.filter(pk=owner.pk).delete()
    assert Client().get(path(token)).status_code == 404


def test_the_page_takes_get_and_head_only(operator: Operator) -> None:
    token = statement_links.make_token(operator)
    for method in ("post", "put", "delete"):
        assert getattr(Client(), method)(path(token)).status_code in (403, 404)
    assert Client().head(path(token)).status_code == 200
    assert "ledger:statement_link" in registry.PUBLIC_ROUTE_NAMES
    assert "ledger:statement_link" not in registry.PUBLIC_UNSAFE_ROUTE_NAMES


def test_the_page_is_rate_limited_per_address(operator: Operator) -> None:
    token = statement_links.make_token(operator)
    client = Client(REMOTE_ADDR="203.0.113.9")
    for _ in range(views.STATEMENT_LIMIT):
        assert client.get(path(token)).status_code == 200
    limited = client.get(path(token))
    assert limited.status_code == 429 and limited["Retry-After"] == "60"
    # Guesses count too.
    assert client.get(path("x:y:z")).status_code == 429
    assert Client(REMOTE_ADDR="203.0.113.10").get(path(token)).status_code == 200


# --- The merge field and the staff statement -------------------------------------------------


def test_the_merge_field_renders_a_signed_link(operator: Operator) -> None:
    # PLAN-014 D4: {pay_link} is this link until the portal exists.
    link = statement_links.link(operator)
    match = re.fullmatch(r"http://localhost:8000/statement/([^/]+)/", link)
    assert match is not None
    assert statement_links.operator_for(match.group(1)) == operator


def test_the_staff_statement_is_unchanged(operator: Operator) -> None:
    staff = f"/operators/{operator.pk}/statement/"
    assert Client().get(staff).status_code in (302, 401)
    page = client_for(Role.VIEWER)[0].get(staff)
    assert page.status_code == 200
    body = page.content.decode()
    assert f'href="/operators/{operator.pk}/"' in body
    assert "Export CSV" in body or has_token(body, "export")
    assert 'id="open-total">$1,970.00<' in body
    assert Lease.objects.filter(operator=operator).exists()
