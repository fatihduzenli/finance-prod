"""The operator portal, slice 1 (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md
H1, H2, H3, H6; §5 H).

The invitation (sent through the engine to the operator's e-mail; its link made at the send,
never stored; 7 days; used once; a second voids the first; refused without the operator's
e-mail or with another account's), the sign-in (wrong password, disabled, unknown e-mail, the
lockout by e-mail and by address, the count reset by a success, the cookie, rotation, idle and
absolute timeouts), the reset (1 hour, the same answer for any e-mail, the old password and the
other sessions end), the staff chain untouched, the pages and the store switcher, isolation (a
foreign id is the portal's 404), CSRF, the PORTAL card's actions and audits, and the
migrations both ways. Fable adds the generated isolation matrix.
"""

from __future__ import annotations

import importlib
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.hashers import make_password
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

import mail
from accounts import registry
from accounts.roles import Role
from ledger import merge
from ledger.models import (
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    InvoiceStatus,
    Lease,
    LeaseBilling,
    MessageKind,
    MessageStatus,
    MessageTemplate,
    Operator,
    OutboundMessage,
    PaymentStatus,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import messaging
from mail.memory import MemoryMailSender
from portal import auth, invitations, tokens
from portal.middleware import COOKIE_NAME
from portal.models import PortalLoginAttempt, PortalToken, PortalUser, TokenKind
from tests.auth_helpers import iter_routes, sample_path
from tests.job_helpers import run_queued_jobs
from tests.receivables_helpers import (
    a_lease,
    a_payment,
    a_rent_charge,
    a_store,
    an_invoice,
)
from tests.test_collections import mail_set_up
from tests.test_ledger_models import new_operator, saved
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

PASSWORD = "portal-test-only-Pass-91"
NEW_PASSWORD = "portal-test-only-New-73"
#: 10:00 New York: in the send window.
START = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
BASE = "http://localhost:8000"
LINK = re.compile(r"http://localhost:8000(/portal/(?:invite|reset)/[A-Za-z0-9_-]+/)")


# --- Factories (also the card-number guard's, tests/test_card_number_guard.py) ---------------


def new_portal_user(**fields: Any) -> PortalUser:
    values: dict[str, Any] = {"email": f"portal-{uuid.uuid4().hex[:10]}@example.com"}
    values.update(fields)
    if "operator" not in values:
        values["operator"] = saved(new_operator())
    return PortalUser(**values)


def new_portal_token(**fields: Any) -> PortalToken:
    values: dict[str, Any] = {
        "kind": TokenKind.INVITE,
        "token_hash": tokens.digest(uuid.uuid4().hex),
        "expires_at": START + timedelta(days=7),
    }
    values.update(fields)
    if "user" not in values:
        values["user"] = saved(new_portal_user())
    return PortalToken(**values)


def new_login_attempt(**fields: Any) -> PortalLoginAttempt:
    values: dict[str, Any] = {"email_hash": auth.email_hash("x@example.com"), "at": START}
    values.update(fields)
    return PortalLoginAttempt(**values)


PORTAL_FACTORIES: dict[type[models.Model], Callable[..., models.Model]] = {
    PortalUser: new_portal_user,
    PortalToken: new_portal_token,
    PortalLoginAttempt: new_login_attempt,
}


# --- The clock and the world ------------------------------------------------------------------


class Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = START
        monkeypatch.setattr(timezone, "now", lambda: self.now)

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch, settings: Any) -> MemoryMailSender:
    mail_set_up(settings)
    sender = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: sender)
    return sender


class World:
    """Operator A with two stores (#101 current, #102 ended), operator B with #202."""

    def __init__(self) -> None:
        self.a = saved(new_operator(display_name="Alpha Owner", email="Alpha.Owner@Example.com"))
        self.b = saved(new_operator(display_name="Beta Owner", email="beta.owner@example.com"))
        self.store_a = a_store("Alphaville", 101)
        self.store_a_old = a_store("Oldtown", 102)
        self.store_b = a_store("Betaburg", 202)
        self.lease_a = a_lease(self.store_a, self.a)
        LeaseBilling.objects.create(
            lease=self.lease_a, email="billing-a@example.com", bank_last4="4821"
        )
        self.lease_a_old = a_lease(self.store_a_old, self.a, status=CollectionStatus.ENDED)
        Lease.objects.filter(pk=self.lease_a_old.pk).update(ended_on=date(2026, 3, 31))
        self.lease_b = a_lease(self.store_b, self.b)
        LeaseBilling.objects.create(lease=self.lease_b, bank_last4="9999")
        self.rent_a = a_rent_charge(self.lease_a, date(2026, 9, 1), due="1850.00")
        self.rent_a_old = a_rent_charge(self.lease_a_old, date(2026, 3, 1), due="400.00")
        self.paid_a = a_rent_charge(
            self.lease_a, date(2026, 8, 1), RentChargeStatus.PAID, "1850.00", "1850.00"
        )
        a_payment(self.paid_a, "1850.00", date(2026, 8, 5))
        self.rent_b = a_rent_charge(self.lease_b, date(2026, 9, 1), due="7777.00")
        paid_b = a_rent_charge(
            self.lease_b, date(2026, 8, 1), RentChargeStatus.PAID, "6666.00", "6666.00"
        )
        a_payment(paid_b, "6666.00", date(2026, 8, 5))
        self.invoice_a = an_invoice(
            self.a,
            1,
            "1850.00",
            rent_charge=self.rent_a,
            internal_status=InvoiceStatus.SENT,
        )
        an_invoice(self.a, 2, "50.00", rent_charge=self.paid_a, internal_status=InvoiceStatus.DRAFT)
        an_invoice(
            self.b, 3, "7777.00", rent_charge=self.rent_b, internal_status=InvoiceStatus.SENT
        )


@pytest.fixture
def world() -> World:
    return World()


def active_user(operator: Operator, password: str = PASSWORD) -> PortalUser:
    return saved(
        PortalUser(
            operator=operator,
            email=str(operator.email),
            password=make_password(password),
            is_active=True,
            invited_at=START,
            activated_at=START,
        )
    )


def sign_in(
    email: str, password: str = PASSWORD, client: Client | None = None, ip: str = "198.51.100.7"
) -> tuple[Client, Any]:
    client = client or Client()
    return client, client.post(
        "/portal/login/", {"email": email, "password": password}, REMOTE_ADDR=ip
    )


def portal_client(user: PortalUser) -> Client:
    client, response = sign_in(user.email)
    assert response.status_code == 302, response.content.decode()
    return client


def text(response: Any) -> str:
    return str(response.content.decode())


def billing_client() -> Client:
    return client_for(Role.BILLING)[0]


def last_link(memory: MemoryMailSender) -> str:
    found = LINK.search(memory.sent[-1].text)
    assert found is not None, memory.sent[-1].text
    return found.group(1)


def invite(staff: Client, operator: Operator) -> Any:
    return staff.post(f"/operators/{operator.pk}/portal/invite/")


# --- H2: the invitation ----------------------------------------------------------------------


def test_the_invitation_creates_an_inactive_account_and_queues_to_the_operators_email(
    world: World,
) -> None:
    staff, actor = client_for(Role.BILLING)
    response = invite(staff, world.a)
    assert response.status_code == 302
    assert response["Location"].endswith(f"/operators/{world.a.pk}/#portal-card")
    user = PortalUser.objects.get(operator=world.a)
    assert (user.email, user.is_active, user.activated_at, user.password) == (
        "alpha.owner@example.com",
        False,
        None,
        "",
    )
    assert user.invited_by == actor and user.invited_at == START
    message = OutboundMessage.objects.get(kind=MessageKind.PORTAL)
    # The operator's e-mail, not the lease's billing e-mail; no lease; sent by hand.
    assert message.to_address == "alpha.owner@example.com"
    assert message.lease is None and message.manual and message.sent_by == actor
    assert (message.rule, message.charge_entity_type) == ("portal", "portal_user")
    assert message.charge_entity_id == user.pk
    assert message.template.code == merge.PORTAL_INVITE_CODE
    assert messaging.LINK_ADDED_AT_SEND in message.rendered_body
    assert "/portal/invite/" not in message.rendered_body
    assert "Alpha Owner" in message.rendered_body
    assert not PortalToken.objects.exists()  # the link is made when the e-mail leaves
    audit = AuditLog.objects.get(action=invitations.INVITED)
    assert audit.actor == actor and audit.entity_id == str(user.pk)
    assert audit.after == {
        "operator": str(world.a.pk),
        "to": "a***@example.com",
        "resend": False,
        "message": str(message.pk),
    }


def test_the_sent_invitation_carries_a_one_time_link_that_is_never_stored(
    world: World, memory: MemoryMailSender
) -> None:
    invite(billing_client(), world.a)
    run_queued_jobs()
    message = OutboundMessage.objects.get(kind=MessageKind.PORTAL)
    assert message.status == MessageStatus.SENT
    assert memory.sent[0].to == "alpha.owner@example.com"
    path = last_link(memory)
    raw = path.rstrip("/").rsplit("/", 1)[1]
    # Stored: the hash only, never the token or the link.
    assert raw not in message.rendered_body
    token = PortalToken.objects.get()
    assert token.token_hash == tokens.digest(raw) and raw not in token.token_hash
    assert token.expires_at == START + timedelta(days=7) and token.kind == TokenKind.INVITE
    assert not AuditLog.objects.filter(after__icontains=raw).exists()
    page = Client().get(path)
    assert page.status_code == 200 and "alpha.owner@example.com" in text(page)
    assert page["Referrer-Policy"] == "no-referrer"
    client = Client()
    done = client.post(path, {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert done.status_code == 302 and done["Location"] == "/portal/"
    user = PortalUser.objects.get(operator=world.a)
    assert user.is_active and user.activated_at == START and user.can_sign_in
    assert client.get("/portal/").status_code == 200
    assert AuditLog.objects.filter(action="portal_user.activated", entity_id=str(user.pk)).exists()
    # Used: the link is refused, with the same sentence as an unknown one.
    again = Client().get(path)
    assert again.status_code == 404 and "expired or was used already" in text(again)
    assert text(Client().get("/portal/invite/not-a-token/")) == text(again)


def test_the_invitation_link_expires_after_seven_days(world: World, clock: Clock) -> None:
    user = saved(new_portal_user(operator=world.a, email="alpha.owner@example.com"))
    _, raw = tokens.issue(user, TokenKind.INVITE)
    clock.advance(days=7, seconds=-1)
    assert Client().get(f"/portal/invite/{raw}/").status_code == 200
    clock.advance(seconds=1)
    assert tokens.find(raw, TokenKind.INVITE) is None
    response = Client().post(
        f"/portal/invite/{raw}/", {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    )
    assert response.status_code == 404
    assert not PortalUser.objects.get(pk=user.pk).is_active


def test_a_second_invitation_voids_the_first(world: World, memory: MemoryMailSender) -> None:
    staff = billing_client()
    invite(staff, world.a)
    run_queued_jobs()
    first = last_link(memory)
    resend = invite(staff, world.a)
    assert resend.status_code == 302
    run_queued_jobs()
    second = last_link(memory)
    assert first != second
    assert Client().get(first).status_code == 404
    assert Client().get(second).status_code == 200
    assert PortalUser.objects.filter(operator=world.a).count() == 1
    audits = AuditLog.objects.filter(action=invitations.INVITED).order_by("id")
    assert [row.after["resend"] for row in audits] == [False, True]


def test_an_operator_without_an_email_cannot_be_invited(world: World) -> None:
    Operator.objects.filter(pk=world.a.pk).update(email=None)
    staff = billing_client()
    response = invite(staff, world.a)
    assert response.status_code == 302
    assert not PortalUser.objects.exists() and not OutboundMessage.objects.exists()
    page = text(staff.get(f"/operators/{world.a.pk}/"))
    assert invitations.NO_EMAIL.replace("'", "&#x27;") in page
    assert 'id="portal-needs-email"' in page and 'id="portal-invite-form"' not in page


def test_the_email_must_be_the_operators_and_no_other_accounts(
    world: World, memory: MemoryMailSender
) -> None:
    staff = billing_client()
    invite(staff, world.a)
    # The operator's e-mail changes before the message leaves: the old message is not sent.
    Operator.objects.filter(pk=world.a.pk).update(email="new.alpha@example.com")
    invite(staff, world.a)
    run_queued_jobs()
    first = OutboundMessage.objects.get(to_address="alpha.owner@example.com")
    second = OutboundMessage.objects.get(to_address="new.alpha@example.com")
    assert (first.status, first.error) == (MessageStatus.SUPPRESSED, invitations.EMAIL_CHANGED)
    assert second.status == MessageStatus.SENT and second.to_address == "new.alpha@example.com"
    assert [sent.to for sent in memory.sent] == ["new.alpha@example.com"]
    assert PortalUser.objects.get(operator=world.a).email == "new.alpha@example.com"
    # Another operator's account holds the e-mail (A's operator moved on to another
    # address since): refused, nothing written.
    Operator.objects.filter(pk=world.a.pk).update(email="third.alpha@example.com")
    Operator.objects.filter(pk=world.b.pk).update(email="new.alpha@example.com")
    assert invitations.invite(world.b.pk, client_for(Role.BILLING)[1]).message == (
        invitations.TAKEN
    )
    assert not PortalUser.objects.filter(operator=world.b).exists()


def test_a_held_invitation_makes_no_link(world: World) -> None:
    # Mail not set up here: the message is held in the Outbox and no token exists.
    invite(billing_client(), world.a)
    run_queued_jobs()
    message = OutboundMessage.objects.get(kind=MessageKind.PORTAL)
    assert message.status == MessageStatus.HELD
    assert not PortalToken.objects.filter(used_at__isnull=True).exists()


def test_an_invitation_ignores_handled_personally_and_a_missing_template(
    world: World, memory: MemoryMailSender
) -> None:
    Operator.objects.filter(pk=world.a.pk).update(handled_personally=True)
    invite(billing_client(), world.a)
    run_queued_jobs()
    assert OutboundMessage.objects.get().status == MessageStatus.SENT
    MessageTemplate.objects.filter(code=merge.PORTAL_INVITE_CODE).update(active=False)
    outcome = invitations.invite(world.b.pk, client_for(Role.BILLING)[1])
    assert not outcome.ok and "portal_invite" in outcome.message
    assert not PortalUser.objects.filter(operator=world.b).exists()


def test_a_viewer_cannot_use_the_portal_card(world: World) -> None:
    viewer = client_for(Role.VIEWER)[0]
    for action in ("invite", "disable", "enable", "reset"):
        assert viewer.post(f"/operators/{world.a.pk}/portal/{action}/").status_code == 403
    assert not PortalUser.objects.exists()
    page = text(viewer.get(f"/operators/{world.a.pk}/"))
    assert 'id="portal-state"' in page and "Not invited" in page
    assert 'id="portal-invite-form"' not in page


def test_the_card_disables_enables_and_resets_with_audits(
    world: World, memory: MemoryMailSender
) -> None:
    user = active_user(world.a)
    signed_in = portal_client(user)
    staff, actor = client_for(Role.BILLING)
    page = text(staff.get(f"/operators/{world.a.pk}/"))
    assert "Active" in page and 'id="portal-reset-form"' in page
    assert 'id="portal-disable-form"' in page and 'id="portal-last-sign-in"' in page
    assert staff.post(f"/operators/{world.a.pk}/portal/disable/").status_code == 302
    user.refresh_from_db()
    assert not user.is_active and user.disabled_at == START
    # Signed out on the next request.
    assert signed_in.get("/portal/").status_code == 302
    _, refused = sign_in(user.email)
    assert auth.WRONG in text(refused)
    assert 'id="portal-enable-form"' in text(staff.get(f"/operators/{world.a.pk}/"))
    assert staff.post(f"/operators/{world.a.pk}/portal/enable/").status_code == 302
    assert PortalUser.objects.get(pk=user.pk).can_sign_in
    assert staff.post(f"/operators/{world.a.pk}/portal/reset/").status_code == 302
    run_queued_jobs()
    assert "/portal/reset/" in last_link(memory)
    actions = list(
        AuditLog.objects.filter(entity_type="portal_user", actor=actor)
        .order_by("at", "id")
        .values_list("action", flat=True)
    )
    assert actions == [invitations.DISABLED, invitations.ENABLED, invitations.RESET_SENT]


def test_portal_templates_are_seeded_with_their_own_placeholder_only() -> None:
    seeded = {
        row.code: row.name
        for row in MessageTemplate.objects.filter(code__in=sorted(merge.PORTAL_CODES))
    }
    assert seeded == {
        "portal_invite": "H — Portal invitation",
        "portal_reset": "H — Portal password reset",
    }
    for code in merge.PORTAL_CODES:
        row = MessageTemplate.objects.get(code=code)
        assert merge.problems(row.body, merge.fields_for(code)) == []
    assert merge.problems("{set_password_link}", merge.fields_for("reminder_due")) != []
    assert merge.problems("{reset_link}", merge.fields_for("portal_invite")) != []
    assert merge.problems("{set_password_link}", merge.fields_for("portal_reset")) != []
    # "Send a message now" on a charge never offers them.
    assert not {row.code for row in messaging.manual_templates()} & merge.PORTAL_CODES


# --- H1: signing in ----------------------------------------------------------------------------


def test_a_wrong_password_an_unknown_email_and_a_disabled_account_read_the_same(
    world: World,
) -> None:
    user = active_user(world.a)
    answers = []
    for email, password in (
        (user.email, "not-the-password"),
        ("nobody@example.com", PASSWORD),
        ("", ""),
    ):
        _, response = sign_in(email, password, ip="198.51.100.20")
        assert response.status_code == 400
        assert COOKIE_NAME not in response.cookies
        answers.append(re.sub(r'value="[^"]*"', "", text(response)))
    PortalUser.objects.filter(pk=user.pk).update(is_active=False, disabled_at=START)
    _, disabled = sign_in(user.email, ip="198.51.100.21")
    assert disabled.status_code == 400
    answers.append(re.sub(r'value="[^"]*"', "", text(disabled)))
    assert all(auth.WRONG in answer for answer in answers)
    assert len(set(answers)) == 1
    assert not PortalLoginAttempt.objects.filter(ok=True).exists()
    # No attempt row holds the e-mail itself.
    assert not PortalLoginAttempt.objects.filter(email_hash__icontains="@").exists()


def test_five_failures_lock_the_email_for_fifteen_minutes(world: World, clock: Clock) -> None:
    user = active_user(world.a)
    for number in range(4):
        _, response = sign_in(user.email, "wrong", ip=f"198.51.100.{30 + number}")
        assert auth.WRONG in text(response)
    _, fifth = sign_in(user.email, "wrong", ip="198.51.100.40")
    assert "Too many attempts. Try again in 15 minutes." in text(fifth)
    # The right password from another address is not even checked.
    _, locked = sign_in(user.email, ip="198.51.100.41")
    assert locked.status_code == 400 and "Try again in 15 minutes" in text(locked)
    count = PortalLoginAttempt.objects.count()
    clock.advance(minutes=14, seconds=1)
    _, still = sign_in(user.email, ip="198.51.100.42")
    assert "Try again in 1 minute." in text(still)
    assert PortalLoginAttempt.objects.count() == count  # not recorded while locked
    clock.advance(seconds=59)
    _, freed = sign_in(user.email, ip="198.51.100.43")
    assert freed.status_code == 302


def test_an_unknown_email_locks_with_the_same_sentence(world: World) -> None:
    active_user(world.a)
    answers = []
    for index, email in enumerate(("ghost@example.com", str(world.a.email))):
        for number in range(5):
            _, response = sign_in(email, "wrong", ip=f"203.0.113.{index * 10 + number}")
        answers.append(text(response))
    assert all("Too many attempts. Try again in 15 minutes." in answer for answer in answers)
    blank = [re.sub(r'value="[^"]*"', "", answer) for answer in answers]
    assert blank[0] == blank[1]


def test_five_failures_from_one_address_lock_that_address(world: World) -> None:
    user = active_user(world.a)
    for number in range(5):
        sign_in(f"guess{number}@example.com", "wrong", ip="192.0.2.9")
    _, locked = sign_in(user.email, ip="192.0.2.9")
    assert "Try again in 15 minutes" in text(locked)
    _, elsewhere = sign_in(user.email, ip="192.0.2.10")
    assert elsewhere.status_code == 302


def test_a_success_resets_the_emails_count(world: World) -> None:
    user = active_user(world.a)
    for number in range(4):
        sign_in(user.email, "wrong", ip=f"198.51.100.{60 + number}")
    _, ok = sign_in(user.email, ip="198.51.100.70")
    assert ok.status_code == 302
    for number in range(4):
        _, response = sign_in(user.email, "wrong", ip=f"198.51.100.{80 + number}")
        assert auth.WRONG in text(response)
    assert PortalUser.objects.get(pk=user.pk).last_login_at == START


def test_the_session_cookie_is_the_portals_own(
    world: World, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    user = active_user(world.a)
    _, response = sign_in(user.email)
    assert response.status_code == 302 and response["Location"] == "/portal/"
    cookie = response.cookies[COOKIE_NAME]
    assert cookie["path"] == "/portal/"
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "Strict"
    assert not cookie["secure"]  # local
    assert int(cookie["max-age"]) == 30 * 60
    # The staff session is never set by the portal.
    assert settings.SESSION_COOKIE_NAME not in response.cookies
    settings.APP_ENV = "staging"
    _, outside_local = sign_in(user.email)
    assert outside_local.cookies[COOKIE_NAME]["secure"] is True


def test_the_session_key_rotates_at_sign_in(world: World) -> None:
    user = active_user(world.a)
    client = portal_client(user)
    first = client.cookies[COOKIE_NAME].value
    sign_in(user.email, client=client)
    second = client.cookies[COOKIE_NAME].value
    assert first != second
    assert not SessionStore().exists(first)
    # A cookie naming a session the portal did not make is never trusted nor deleted.
    staff_like = SessionStore()
    staff_like["_auth_user_id"] = "1"
    staff_like.create()
    planted = Client()
    planted.cookies[COOKIE_NAME] = str(staff_like.session_key)
    assert planted.get("/portal/").status_code == 302
    sign_in(user.email, client=planted)
    assert planted.cookies[COOKIE_NAME].value != staff_like.session_key
    assert SessionStore().exists(str(staff_like.session_key))


def test_the_session_ends_after_thirty_idle_minutes(world: World, clock: Clock) -> None:
    client = portal_client(active_user(world.a))
    clock.advance(minutes=29)
    assert client.get("/portal/").status_code == 200
    clock.advance(minutes=29)
    assert client.get("/portal/").status_code == 200
    clock.advance(minutes=30)
    response = client.get("/portal/")
    assert response.status_code == 302 and response["Location"].startswith("/portal/login/")


def test_the_session_ends_twelve_hours_after_sign_in(world: World, clock: Clock) -> None:
    client = portal_client(active_user(world.a))
    for _ in range(int(12 * 60 / 20) - 1):
        clock.advance(minutes=20)
        assert client.get("/portal/").status_code == 200
    clock.advance(minutes=20)
    assert client.get("/portal/").status_code == 302


def test_signing_out_deletes_the_session_and_takes_post_only(world: World) -> None:
    client = portal_client(active_user(world.a))
    key = client.cookies[COOKIE_NAME].value
    assert client.get("/portal/logout/").status_code == 405
    response = client.post("/portal/logout/")
    assert response.status_code == 302 and response["Location"] == "/portal/login/"
    assert not SessionStore().exists(key)
    assert client.get("/portal/").status_code == 302


def test_next_is_kept_for_portal_paths_only(world: World) -> None:
    user = active_user(world.a)
    path = f"/portal/stores/{world.store_a.pk}/"
    response = Client().get(path)
    assert response["Location"] == f"/portal/login/?next=%2Fportal%2Fstores%2F{world.store_a.pk}%2F"
    client = Client()
    signed = client.post(
        "/portal/login/", {"email": user.email, "password": PASSWORD, "next": path}
    )
    assert signed["Location"] == path
    for hostile in ("https://evil.example/", "//evil.example/", "/operators/", "/portal/\\x"):
        away = Client().post(
            "/portal/login/", {"email": user.email, "password": PASSWORD, "next": hostile}
        )
        assert away["Location"] == "/portal/", hostile


# --- H1: the reset ------------------------------------------------------------------------------


def test_the_reset_form_answers_the_same_for_any_email(
    world: World, memory: MemoryMailSender
) -> None:
    user = active_user(world.a)
    pending = saved(new_portal_user(operator=world.b, email="pending@example.com"))
    answers = []
    for email in (user.email, "nobody@example.com", pending.email, "not an e-mail"):
        response = Client().post("/portal/reset/", {"email": email})
        assert response.status_code == 200
        answers.append(text(response))
    assert len(set(answers)) == 1 and "If this e-mail has a portal account" in answers[0]
    run_queued_jobs()
    assert [sent.to for sent in memory.sent] == [user.email]
    # A second request within five minutes queues nothing more.
    Client().post("/portal/reset/", {"email": user.email})
    assert OutboundMessage.objects.filter(kind=MessageKind.PORTAL).count() == 1


def test_a_reset_link_works_once_for_an_hour_and_ends_the_old_password_and_sessions(
    world: World, memory: MemoryMailSender, clock: Clock
) -> None:
    user = active_user(world.a)
    other_browser = portal_client(user)
    Client().post("/portal/reset/", {"email": user.email})
    run_queued_jobs()
    path = last_link(memory)
    token = PortalToken.objects.get(kind=TokenKind.RESET)
    assert token.expires_at == START + timedelta(hours=1)
    mismatch = Client().post(path, {"password": NEW_PASSWORD, "confirm": "something-else-1"})
    assert mismatch.status_code == 200 and "not the same" in text(mismatch)
    weak = Client().post(path, {"password": "12345678", "confirm": "12345678"})
    assert "too common" in text(weak) or "entirely numeric" in text(weak)
    done = Client().post(path, {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert done.status_code == 302
    assert Client().get(path).status_code == 404
    _, old = sign_in(user.email, PASSWORD, ip="198.51.100.90")
    assert auth.WRONG in text(old)
    _, new = sign_in(user.email, NEW_PASSWORD, ip="198.51.100.91")
    assert new.status_code == 302
    assert other_browser.get("/portal/").status_code == 302
    # A second link, an hour later, is refused.
    Client().post("/portal/reset/", {"email": user.email})
    clock.advance(minutes=6)
    Client().post("/portal/reset/", {"email": user.email})
    run_queued_jobs()
    late = last_link(memory)
    clock.advance(hours=1)
    assert Client().get(late).status_code == 404


def test_an_invitation_link_cannot_reset_and_a_reset_link_cannot_invite(world: World) -> None:
    user = active_user(world.a)
    _, raw = tokens.issue(user, TokenKind.RESET)
    assert Client().get(f"/portal/invite/{raw}/").status_code == 404
    assert Client().get(f"/portal/reset/{raw}/").status_code == 200
    _, invite_raw = tokens.issue(user, TokenKind.INVITE)
    # An account that accepted already cannot use an invitation.
    assert Client().get(f"/portal/invite/{invite_raw}/").status_code == 404
    assert Client().get(f"/portal/reset/{'x' * 200}/").status_code == 404


# --- The staff chain is untouched ----------------------------------------------------------------


def test_a_portal_cookie_on_a_staff_url_is_ignored(world: World) -> None:
    client = portal_client(active_user(world.a))
    key = client.cookies[COOKIE_NAME].value
    staff_path = Client()
    staff_path.cookies[COOKIE_NAME] = key
    for path in ("/", "/operators/", f"/operators/{world.a.pk}/"):
        assert staff_path.get(path).status_code == 401, path
    # Even as the staff cookie: the portal session holds no staff user.
    as_staff = Client()
    as_staff.cookies["sessionid"] = key
    assert as_staff.get("/operators/").status_code == 401


def test_a_staff_session_opens_no_portal_page(world: World) -> None:
    staff = client_for(Role.ADMIN)[0]
    for name, raw, _ in iter_routes():
        if not registry.is_portal_route(name) or name in registry.PORTAL_PUBLIC_ROUTE_NAMES:
            continue
        path = sample_path(raw).replace("/1/", f"/{world.store_a.pk}/")
        response = staff.get(path)
        assert response.status_code == 302, (name, response.status_code)
        assert response["Location"].startswith("/portal/login/"), name


def test_every_portal_route_needs_the_portal_sign_in_but_the_public_five() -> None:
    routes = [
        (name, sample_path(raw)) for name, raw, _ in iter_routes() if registry.is_portal_route(name)
    ]
    assert {name for name, _ in routes} == registry.PORTAL_ROUTE_NAMES
    for name, path in routes:
        response = Client().get(path)
        if name in registry.PORTAL_PUBLIC_ROUTE_NAMES:
            assert response.status_code in (200, 404, 405), (name, response.status_code)
        else:
            assert response.status_code == 302, name
            assert response["Location"].startswith("/portal/login/"), name


def test_csrf_is_enforced_on_every_portal_post(world: World) -> None:
    user = active_user(world.a)
    strict = Client(enforce_csrf_checks=True)
    for path in ("/portal/login/", "/portal/reset/", "/portal/logout/"):
        assert strict.post(path, {"email": user.email}).status_code == 403, path
    page = strict.get("/portal/login/")
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', text(page))
    assert token is not None
    signed = strict.post(
        "/portal/login/",
        {"email": user.email, "password": PASSWORD, "csrfmiddlewaretoken": token.group(1)},
    )
    assert signed.status_code == 302
    for path in ("/portal/account/", "/portal/account/password/", "/portal/logout/"):
        assert strict.post(path, {"theme": "dark"}).status_code == 403, path


# --- H3 and H6: the pages and isolation ------------------------------------------------------


def _store_numbers(html: str) -> set[str]:
    return set(re.findall(r"#(\d+)\b", html))


def test_home_lists_the_operators_stores_only_with_the_ended_one_marked(world: World) -> None:
    client = portal_client(active_user(world.a))
    page = text(client.get("/portal/"))
    assert _store_numbers(page) <= {"101", "102"}
    assert "#101 Alphaville" in page and "#102 Oldtown" in page
    assert "Betaburg" not in page and "#202" not in page and "7,777.00" not in page
    assert f'id="ended-{world.store_a_old.pk}"' in page
    assert f'id="ended-{world.store_a.pk}"' not in page
    assert f'id="balance-{world.store_a.pk}">$1,850.00<' in page
    assert f'id="balance-{world.store_a_old.pk}">$400.00<' in page
    # The store switcher lists the same stores, and no staff navigation is on the page.
    switcher = page.split('id="store-switcher"', 1)[1].split("</details>", 1)[0]
    assert "#101 Alphaville" in switcher and "#102 Oldtown" in switcher and "#202" not in switcher
    assert "manifest" not in page and 'id="sidebar"' not in page and "/operators/" not in page


def test_the_store_page_shows_charges_autopay_and_the_bank(world: World) -> None:
    client = portal_client(active_user(world.a))
    page = text(client.get(f"/portal/stores/{world.store_a.pk}/"))
    assert "Rent Sep 2026" in page and "$1,850.00" in page
    assert 'id="store-autopay">off<' in page
    assert 'id="store-bank">····4821<' in page
    CollectionSchedule.objects.create(
        lease=world.lease_a,
        kind=ScheduleKind.RENT,
        seq=1,
        debit_day=5,
        amount=Decimal("1850.00"),
        state=ScheduleState.ACTIVE,
    )
    assert 'id="store-autopay">on<' in text(client.get(f"/portal/stores/{world.store_a.pk}/"))
    old = text(client.get(f"/portal/stores/{world.store_a_old.pk}/"))
    assert 'id="store-ended"' in old and 'id="store-bank">none<' in old
    assert "#202" not in page and "9999" not in page and "7,777.00" not in page


def test_invoices_and_payments_are_the_operators_on_that_store(world: World) -> None:
    client = portal_client(active_user(world.a))
    invoices = text(client.get(f"/portal/stores/{world.store_a.pk}/invoices/"))
    assert "INV-2026-0001" in invoices and "$1,850.00" in invoices and ">Open<" in invoices
    assert "INV-2026-0002" not in invoices  # a draft
    assert "INV-2026-0003" not in invoices
    payments = text(client.get(f"/portal/stores/{world.store_a.pk}/payments/"))
    assert "Rent Aug 2026" in payments and "Bank debit (ACH) ····4821" in payments
    assert "Settled" in payments and "6,666.00" not in payments
    a_payment(world.rent_a, "100.00", None, PaymentStatus.RETURNED, date(2026, 9, 5))
    a_payment(world.rent_a, "50.00", None, PaymentStatus.VOIDED, date(2026, 9, 6))
    again = text(client.get(f"/portal/stores/{world.store_a.pk}/payments/"))
    assert "Returned" in again and "$100.00" in again and "$50.00" not in again


def test_a_foreign_or_unreadable_id_is_the_portals_404(world: World) -> None:
    client = portal_client(active_user(world.a))
    for suffix in ("", "invoices/", "payments/", "pay/", "bank/"):
        for key in (world.store_b.pk, uuid.uuid4(), "not-a-uuid", world.lease_a.pk):
            response = client.get(f"/portal/stores/{key}/{suffix}")
            assert response.status_code == 404, (suffix, key)
            html = text(response)
            assert 'id="not-found"' in html and "Betaburg" not in html and "#202" not in html


def test_the_statement_is_scoped_to_the_portal_users_operator(world: World) -> None:
    client = portal_client(active_user(world.a))
    page = text(client.get("/portal/statement/"))
    assert "Statement: Alpha Owner" in page
    assert 'id="open-total">$2,250.00<' in page
    assert "Betaburg" not in page and "7,777.00" not in page and "6,666.00" not in page


def test_pay_and_bank_say_they_are_not_available_yet(world: World) -> None:
    client = portal_client(active_user(world.a))
    pay = text(client.get(f"/portal/stores/{world.store_a.pk}/pay/"))
    assert "Online payment is not available yet." in pay
    bank = text(client.get(f"/portal/stores/{world.store_a.pk}/bank/"))
    assert "Bank account changes are not available yet." in bank


def test_the_account_page_changes_the_theme_and_the_password(world: World) -> None:
    user = active_user(world.a)
    client = portal_client(user)
    other = portal_client(user)
    page = text(client.get("/portal/account/"))
    assert 'id="account-email">alpha.owner@example.com<' in page and 'data-theme="system"' in page
    themed = client.post("/portal/account/", {"theme": "dark", "next": "/portal/statement/"})
    assert themed["Location"] == "/portal/statement/"
    assert 'data-theme="dark"' in text(client.get("/portal/"))
    assert client.post("/portal/account/", {"theme": "neon"}).status_code == 400
    away = client.post("/portal/account/", {"theme": "light", "next": "https://evil.example/"})
    assert away["Location"] == "/portal/account/"
    wrong = client.post(
        "/portal/account/password/",
        {"current": "nope", "password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
    )
    assert wrong.status_code == 400 and "current password is not right" in text(wrong)
    changed = client.post(
        "/portal/account/password/",
        {"current": PASSWORD, "password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
    )
    assert changed["Location"] == "/portal/account/?password=changed"
    assert client.get("/portal/").status_code == 200  # this session stays
    assert other.get("/portal/").status_code == 302  # the other one ends
    _, old = sign_in(user.email, PASSWORD, ip="198.51.100.120")
    assert auth.WRONG in text(old)


# --- Migrations -----------------------------------------------------------------------------------


def _migrate(targets: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


@pytest.fixture
def leaves() -> Iterator[list[tuple[str, str]]]:
    found = MigrationExecutor(connection).loader.graph.leaf_nodes()
    yield found
    _migrate(found)


@pytest.mark.django_db(transaction=True)
def test_the_portal_migrations_go_both_ways(leaves: list[tuple[str, str]]) -> None:
    seed = importlib.import_module("ledger.migrations.0064_portal_messages")
    _migrate([("portal", None)])  # type: ignore[list-item]
    tables = connection.introspection.table_names()
    assert not {"portal_users", "portal_tokens", "portal_login_attempts"} & set(tables)
    _migrate([("ledger", "0063_returned_debits")])
    assert not MessageTemplate.objects.filter(code__in=seed.SEEDED_CODES).exists()
    _migrate(leaves)
    assert {"portal_users", "portal_tokens", "portal_login_attempts"} <= set(
        connection.introspection.table_names()
    )
    assert set(
        MessageTemplate.objects.filter(code__in=seed.SEEDED_CODES).values_list("code", flat=True)
    ) == set(seed.SEEDED_CODES)
    # A portal message exists: the ledger step refuses to reverse.
    operator = saved(new_operator(email="mig@example.com"))
    user = saved(new_portal_user(operator=operator, email="mig@example.com"))
    messaging.queue_portal(
        MessageTemplate.objects.get(code=merge.PORTAL_INVITE_CODE),
        user.pk,
        operator,
        user.email,
        {
            "operator_name": "x",
            "company_name": "x",
            "support_email": "",
            "set_password_link": messaging.LINK_ADDED_AT_SEND,
        },
    )
    with pytest.raises(RuntimeError, match="refuses to reverse"):
        _migrate([("ledger", "0063_returned_debits")])
    OutboundMessage.objects.all().delete()
