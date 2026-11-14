"""PLAN-015k, Fable's adversarial set (docs/qa/15/PLAN-015k-reminder-email-outbox-activity.md
§5): hostile text in a reminder, hostile filters, roles, and audit rows of the action that a
broken or older writer left behind must not break the Outbox or an Activity card."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import quote

import pytest

from accounts.roles import Role
from ledger.models import AuditLog, OutboundMessage
from ledger.services import detail_cards
from payments.models import Job
from tests import test_plan015k_reminder_outbox as base
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

OUTBOX = base.OUTBOX
HOSTILE = '<script>alert("reminder-title")</script>'
ESCAPED = "&lt;script&gt;alert(&quot;reminder-title&quot;)&lt;/script&gt;"


def admin() -> Any:
    return client_for(Role.ADMIN, f"admin-{uuid.uuid4().hex[:6]}")[0]


def body(response: Any) -> str:
    return str(response.content.decode())


def test_markup_in_a_reminders_title_is_escaped_in_the_outbox_and_the_activity_card(
    settings: Any,
) -> None:
    settings.MAIL_ENABLED = False
    item = base.entry(base.author(), title=HOSTILE)
    base.send(item)
    page = body(admin().get(OUTBOX))
    assert HOSTILE not in page and ESCAPED in page
    detail = body(admin().get(f"/calendar/{item.pk}/"))
    assert HOSTILE not in detail


@pytest.mark.parametrize(
    "after",
    [
        {"outcome": "held", "to": HOSTILE, "subject": HOSTILE, "reason": HOSTILE},
        {"outcome": "sent", "to": "a\r\nBcc: x@example.com", "subject": "line\r\nbreak"},
        {"outcome": "sent", "to": 12345, "subject": ["a", "list"]},
        {"outcome": "held", "to": None, "subject": None, "reason": None},
        {"outcome": "failed", "error": HOSTILE},
        {"outcome": "sent", "to": "x" * 5000, "subject": "y" * 5000},
    ],
)
def test_a_stored_payload_with_hostile_or_wrong_typed_values_renders_escaped(after: Any) -> None:
    item = base.entry(base.author())
    base.staff_row(item, after)
    response = admin().get(OUTBOX)
    assert response.status_code == 200
    page = body(response)
    assert HOSTILE not in page
    assert f'id="staff-{AuditLog.objects.get().pk}"' in page
    detail = admin().get(f"/calendar/{item.pk}/")
    assert detail.status_code == 200 and HOSTILE not in body(detail)


@pytest.mark.parametrize(
    "after",
    [
        None,
        [],
        ["held"],
        "held",
        7,
        {},
        {"outcome": None},
        {"outcome": ["held"]},
        {"outcome": "no_address"},
        {"outcome": "HELD"},
        {"outcome": "queued"},
    ],
)
def test_a_broken_or_unlisted_payload_is_not_a_row_and_breaks_nothing(after: Any) -> None:
    item = base.entry(base.author())
    row = base.staff_row(item, after)
    response = admin().get(OUTBOX)
    assert response.status_code == 200
    assert f'id="staff-{row.pk}"' not in body(response)
    assert admin().get(f"/calendar/{item.pk}/").status_code == 200
    words = detail_cards.morning_email_words("calendar_entry", after)
    assert words.startswith("Reminder e-mail")


def test_a_row_of_the_action_on_another_entity_type_is_not_listed() -> None:
    row = base.staff_row(None, {"outcome": "held"}, entity_type="lease")
    response = admin().get(OUTBOX)
    assert response.status_code == 200
    assert f'id="staff-{row.pk}"' not in body(response)


@pytest.mark.parametrize("name", ["status", "reason", "from", "to", "page"])
@pytest.mark.parametrize(
    "value", ["\x00", "held\x00", "' OR 1=1 --", "🙂", "‮", "a\r\nb", "9" * 400, "-1", ""]
)
def test_a_hostile_filter_value_is_ignored_and_the_page_answers(
    settings: Any, name: str, value: str
) -> None:
    settings.MAIL_ENABLED = False
    row = base.send(base.entry(base.author()))
    response = admin().get(f"{OUTBOX}?{name}={quote(value)}")
    assert response.status_code == 200
    assert f'id="staff-{row.pk}"' in body(response)


@pytest.mark.parametrize("page", ["999999999999999999999", "0", "2", "last", "1e3"])
def test_a_page_number_out_of_range_answers_with_a_page(settings: Any, page: str) -> None:
    settings.MAIL_ENABLED = False
    base.send(base.entry(base.author()))
    assert admin().get(f"{OUTBOX}?page={page}").status_code == 200


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_only_an_admin_sees_the_outbox(settings: Any, role: Any) -> None:
    settings.MAIL_ENABLED = False
    base.send(base.entry(base.author()))
    client = client_for(role, f"other-{uuid.uuid4().hex[:6]}")[0]
    assert client.get(OUTBOX).status_code == 403


def test_a_staff_row_offers_no_send_now_and_its_key_cannot_be_posted(settings: Any) -> None:
    settings.MAIL_ENABLED = False
    row = base.send(base.entry(base.author()))
    client = admin()
    page = body(client.get(OUTBOX))
    start = page.index(f'id="staff-{row.pk}"')
    assert "<form" not in page[start : page.index("</tr>", start)]
    for key in (str(row.pk), f"staff-{row.pk}"):
        assert client.post(f"{OUTBOX}{key}/send-now/").status_code == 404
    # A UUID that is no message's (the reminder's own id): the view's own refusal, back to
    # the Outbox; nothing is sent, queued or recorded.
    response = client.post(f"{OUTBOX}{row.entity_id}/send-now/")
    assert (response.status_code, response["Location"]) == (302, OUTBOX)
    assert AuditLog.objects.filter(action=base.ACTION).count() == 1
    assert not OutboundMessage.objects.exists()
    assert not Job.objects.exists()


def test_the_body_of_the_e_mail_is_in_no_page(settings: Any) -> None:
    settings.MAIL_ENABLED = False
    item = base.entry(base.author())
    base.send(item)
    client = admin()
    assert base.BODY not in body(client.get(OUTBOX))
    assert base.BODY not in body(client.get("/audit/"))
