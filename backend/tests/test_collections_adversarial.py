"""Adversarial cases for PLAN-011b (mail, ladder, NSF, calendar feed, time zone, statement
links), by the QA owner (plan §7). Opus's files prove each feature; these cases try to make
the system mail a real address, leak the key, charge a fee by itself, or hand a link to the
wrong reader. Re-pinned to the messaging engine (PLAN-014 D2): the catch-all is retired,
the Outbox and the sandbox allowlist keep a sandbox from mailing a real address."""

from __future__ import annotations

import io
import json
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger import merge
from ledger.models import AuditLog, CalendarEntry, OneOffCharge, OutboundMessage
from ledger.services import statement_links
from tests import test_collections as ladder
from tests.test_calendar import feed_path, make_feed
from tests.test_collections import (
    CEO,
    at,
    mail_set_up,
    operator,
    queue_rule,
    rent_charge,
)
from tests.test_mail import PRIVATE_KEY_MARKER, marker_info
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

# The ladder file's clock fixture, re-exported under its own name so pytest finds it here.
clock = ladder.clock

D = Decimal
CARD = "4111 1111 1111 1111"


# --- the key never leaves the mail package ----------------------------------------------


def test_the_service_account_key_is_in_no_log_row_page_or_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, settings: Any
) -> None:
    """A marker inside the key must be absent from everything observable after a send that
    fails at the token exchange (the worst case: the exception path)."""
    import httpx

    from mail import gmail

    info = marker_info()
    mail_set_up(settings)
    settings.GMAIL_SERVICE_ACCOUNT_JSON = json.dumps(info)

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom " + PRIVATE_KEY_MARKER})

    monkeypatch.setattr(gmail, "_transport", lambda: httpx.MockTransport(refuse), raising=False)
    monkeypatch.setattr(gmail, "RETRY_DELAY_SECONDS", 0, raising=False)
    caplog.set_level(logging.DEBUG)
    charge = rent_charge(owner=operator(email=CEO))
    with override_settings(STAX_CALLER="worker"):
        try:
            queue_rule(charge)
            from tests.job_helpers import run_queued_jobs

            run_queued_jobs()
        except Exception as error:  # the fence is about what leaks, not whether it fails
            assert PRIVATE_KEY_MARKER not in str(error)
    dump = caplog.text
    for row in AuditLog.objects.all():
        dump += str(row.before) + str(row.after)
    for message in OutboundMessage.objects.all():
        dump += (message.error or "") + message.rendered_subject + str(message.status)
    admin, _ = client_for(Role.ADMIN)
    dump += admin.get(f"/operators/{charge.lease.operator_id}/").content.decode()
    dump += admin.get("/audit/").content.decode()
    assert PRIVATE_KEY_MARKER not in dump
    assert info["private_key"] not in dump


# --- a sandbox can never reach a real address (PLAN-014 D2) -------------------------------


def test_on_the_sandbox_nothing_reaches_an_address_off_the_allowlist(
    clock: Any, settings: Any
) -> None:
    from mail.memory import MemoryMailSender

    memory = MemoryMailSender()
    mail_set_up(settings, sandbox=True, allowlist="")
    clock(at(date(2026, 10, 8)))
    with override_settings(STAX_CALLER="worker"), pytest.MonkeyPatch.context() as mp:
        import mail

        # Only the base sender is replaced: get_sender() itself must wrap it in the
        # allowlist because STAX_ENV is sandbox.
        mp.setattr(mail, "_base_sender", lambda: memory)
        for address in (CEO, "someone@realcompany.example", "x+tag@example.org"):
            queue_rule(rent_charge(due_on=date(2026, 9, 1), owner=operator(email=address)))
        from tests.job_helpers import run_queued_jobs

        run_queued_jobs()
    assert memory.sent == []
    messages = list(OutboundMessage.objects.all())
    assert len(messages) == 3
    assert {(m.status, m.held_reason) for m in messages} == {("held", "sandbox")}


def test_mail_on_boots_without_the_retired_variables_and_sends_nothing_without_a_sender(
    settings: Any,
) -> None:
    from finance.env_guard import validate
    from mail.outbox import OutboxSender
    from tests.test_env_guard import DEPLOYED_ENV, _env

    env = _env(
        DEPLOYED_ENV,
        MAIL_ENABLED="true",
        GMAIL_SERVICE_ACCOUNT_JSON=json.dumps(marker_info()),
    )
    config = validate(env)
    assert config.mail_enabled is True
    assert PRIVATE_KEY_MARKER not in repr(config)
    import mail

    settings.MAIL_ENABLED = True
    settings.GMAIL_SERVICE_ACCOUNT_JSON = json.dumps(marker_info())
    assert isinstance(mail.get_sender(), OutboxSender)  # no sender address in the settings


# --- templates cannot reach into objects ------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "{operator.__class__}",
        "{operator.__dict__}",
        "{operator[0]}",
        "{amount!r}",
        "{amount:>{operator}}",
        "{0}",
        "{}",
        "{settings}",
        "{company.__init__.__globals__}",
    ],
)
def test_a_template_cannot_reach_attributes_indexes_or_unknown_names(body: str) -> None:
    assert merge.problems(body), body
    values = {name: "x" for name in merge.MERGE_FIELDS}
    with pytest.raises((merge.TemplateRefused, KeyError, ValueError, AttributeError, IndexError)):
        merge.render(body, values)


# --- NSF fee only by hand ---------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Any:
    from payments.fake import FakeProvider

    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def test_reschedule_is_closed_to_viewers_and_the_waiver_to_billing(fake: Any) -> None:
    from tests.test_nsf import a_return

    payment = a_return(fake)
    viewer, _ = client_for(Role.VIEWER)
    assert (
        viewer.post(
            f"/returns/{payment.pk}/reschedule/",
            {"redebit_date": (timezone.localdate() + timedelta(days=3)).isoformat(), "note": "n"},
        ).status_code
        == 403
    )
    billing, _ = client_for(Role.BILLING)
    response = billing.post(
        f"/returns/{payment.pk}/reschedule/",
        {
            "redebit_date": (timezone.localdate() + timedelta(days=3)).isoformat(),
            "note": "n",
            "waive_fee": "on",
            "waive_note": "goodwill",
        },
    )
    assert response.status_code in (302, 403)
    fee = OneOffCharge.objects.filter(category="nsf_fee").first()
    if fee is not None:
        assert fee.status != "waived", "billing must not waive the NSF fee"


# --- the calendar feed token ------------------------------------------------------------


def test_feed_tokens_are_not_guessable_and_a_revoked_one_is_dead(settings: Any) -> None:
    _, user = client_for(Role.BILLING)
    token = make_feed(user)
    assert len(token) >= 43  # 32 random bytes, urlsafe
    anonymous = Client()
    assert anonymous.get(feed_path(token)).status_code == 200
    near_misses = [token[:-1] + ("A" if token[-1] != "A" else "B"), token.upper(), token[:-2]]
    for wrong in near_misses:
        assert anonymous.get(feed_path(wrong)).status_code == 404
    from ledger.models import CalendarFeed

    CalendarFeed.objects.filter(user=user).update(revoked_at=timezone.now())
    assert anonymous.get(feed_path(token)).status_code == 404
    assert token not in "".join(str(r.after) + str(r.before) for r in AuditLog.objects.all())


def test_the_feed_never_carries_a_card_number_or_a_private_marker(settings: Any) -> None:
    _, user = client_for(Role.BILLING)
    token = make_feed(user)
    from django.core.exceptions import ValidationError

    for title in (f"Pay {CARD}", "Routing 123456780 Account 987654321012"):
        with pytest.raises(ValidationError):
            CalendarEntry.objects.create(
                kind="note", on=timezone.localdate(), title=title, body="", author=user
            )
    CalendarEntry.objects.create(
        kind="note", on=timezone.localdate(), title="Call Sam", body="about rent", author=user
    )
    body = Client().get(feed_path(token)).content.decode()
    # The card number as typed and without its spaces: the feed's UIDs are random hex, so a
    # bare "4111" matched one UID in a thousand (the once-failed PostgreSQL run of 12f-a,
    # closed by PLAN-013a C8).
    assert "Call Sam" in body
    assert CARD not in body and CARD.replace(" ", "") not in body


# --- statement links --------------------------------------------------------------------


def test_a_statement_link_cannot_be_forged_bent_or_reused_after_revocation() -> None:
    from tests.test_lease_services import make_operator

    first = make_operator(email="a-link@example.com", customer=None)
    second = make_operator(email="b-link@example.com", customer=None)
    token = statement_links.make_token(first)
    anonymous = Client()
    assert anonymous.get(f"/statement/{token}/").status_code == 200
    forged = token.replace(str(first.pk)[:8], str(second.pk)[:8])
    assert anonymous.get(f"/statement/{forged}/").status_code == 404
    assert anonymous.get(f"/statement/{token[:-3]}/").status_code == 404
    assert anonymous.get(f"/statement/{token}x/").status_code == 404
    page = anonymous.get(f"/statement/{token}/").content.decode()
    assert "b-link@example.com" not in page
    assert "/operators/" not in page and "<script" not in page.lower()
    billing, _ = client_for(Role.BILLING)
    billing.post(f"/operators/{first.pk}/statement-links/revoke/", {})
    assert anonymous.get(f"/statement/{token}/").status_code == 404


# --- tz_apply refuses without the matching audit ----------------------------------------


def test_tz_apply_refuses_a_wrong_count_and_writes_nothing() -> None:
    out = io.StringIO()
    before = AuditLog.objects.count()
    with pytest.raises(CommandError):
        call_command("tz_apply", "--audited", "999999", stdout=out)
    assert AuditLog.objects.count() == before


# --- the owner's broken-promise scenario (staging defect of 2026-10-06; one open promise per
# lease since PLAN-012c-b, so the three promises sit on three leases) ------------------------


def test_past_promises_show_at_once_whatever_the_balance_and_future_ones_do_not() -> None:
    """Operator with nothing owed; three promises recorded today on three stores: two past,
    one future. The two past ones are attention items immediately; the future one is not."""
    from datetime import date as _date

    from ledger.services import attention, contacts
    from tests.receivables_helpers import a_lease
    from tests.test_lease_services import make_operator

    sena = make_operator(email="sena-adv@example.com", customer=None)
    today = timezone.localdate()
    for promised in (
        today - timedelta(days=5),
        today - timedelta(days=1),
        today + timedelta(days=1),
    ):
        lease = a_lease(operator=sena)
        outcome = contacts.add_entry(
            sena.pk,
            contacts.ContactInput(
                kind="promise",
                happened_on=today,
                body="will pay",
                promised_date=promised,
                promised_amount=D("250.00"),
                lease=lease.pk,
            ),
            actor=None,
        )
        assert outcome.ok, outcome.message
    items = [i for i in attention.attention(today).items if i.kind == attention.BROKEN_PROMISE]
    assert len(items) == 2
    assert [i.since for i in items] == [today - timedelta(days=5), today - timedelta(days=1)]
    assert all(isinstance(i.since, _date) and i.since < today for i in items)
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get("/").content.decode()
    assert page.count("not kept") == 2


def test_three_promises_in_one_bucket_leave_one_open_and_at_most_one_attention_item() -> None:
    """PLAN-012c-b D3: the same three promises on one operator without a store close each
    other as Rescheduled; only the newest (future) one stays open, so nothing is broken."""
    from ledger.models import ContactEntry, ContactStatus
    from ledger.services import attention, contacts
    from tests.test_lease_services import make_operator

    sena = make_operator(email="sena-one@example.com", customer=None)
    today = timezone.localdate()
    for promised in (
        today - timedelta(days=5),
        today - timedelta(days=1),
        today + timedelta(days=1),
    ):
        outcome = contacts.add_entry(
            sena.pk,
            contacts.ContactInput(
                kind="promise",
                happened_on=today,
                body="will pay",
                promised_date=promised,
                promised_amount=D("250.00"),
            ),
            actor=None,
        )
        assert outcome.ok, outcome.message
    rows = ContactEntry.objects.filter(operator=sena, kind="promise").order_by("created_at")
    assert [r.status for r in rows] == [
        ContactStatus.RESCHEDULED,
        ContactStatus.RESCHEDULED,
        ContactStatus.OPEN,
    ]
    assert [r.closed_by_id for r in rows][:2] == [rows[1].pk, rows[2].pk]
    items = [i for i in attention.attention(today).items if i.kind == attention.BROKEN_PROMISE]
    assert items == []
    assert len(contacts.broken_promises(today, sena.pk)) <= 1
