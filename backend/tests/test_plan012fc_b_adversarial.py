"""Fable's adversarial cases for PLAN-012f-c Batch B (plan §6 B, the abuse and boundary cases):
the phone and staff filters against hostile values, the widened held-form guard, the two-line
reason's combinations, the partial notice's scope and the Payments help topic's access."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import Client
from django.urls import reverse
from django.utils.html import escape

from accounts.display import staff_name
from accounts.roles import Role
from finance.templatetags.phones import phone_text
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db


# --- The phone filter --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("4045551234", "(404) 555-1234"),
        ("1 404 555 1234", "(404) 555-1234"),
        ("+1 (404) 555-1234", "(404) 555-1234"),
        ("404-555-1234 x12", "404-555-1234 x12"),
        ("+44 20 7946 0958", "+44 20 7946 0958"),
        ("+447911123456", "+44 7911123456"),
        ("+90 212 555 00 00", "+90 212 555 00 00"),
        ("555-1234", "555-1234"),
        ("40455512345", "40455512345"),
        ("", ""),
        (None, ""),
        ("٠٤٠٤٥٥٥١٢٣٤", "٠٤٠٤٥٥٥١٢٣٤"),
    ],
)
def test_the_phone_filter_rewrites_only_what_it_understands(stored: str | None, shown: str) -> None:
    assert phone_text(stored) == shown


def test_the_phone_filter_output_is_escaped_in_a_template() -> None:
    hostile = '<img src=x onerror="alert(1)">4045551234'
    rendered = Template("{% load phones %}{{ value|phone }}").render(Context({"value": hostile}))
    assert "<img" not in rendered and escape(hostile) in rendered


# --- The staff filter ----------------------------------------------------------------------------


def test_the_staff_filter_escapes_the_name_and_the_title_and_never_prints_the_email_as_text() -> (
    None
):
    user = User.objects.create_user(
        "hostile", email='a"b@example.com', first_name='<b onmouseover="x()">Ann</b>', last_name=""
    )
    rendered = Template("{% load people %}{{ user|staff }}").render(Context({"user": user}))
    assert "<b onmouseover" not in rendered
    assert 'title="a&quot;b@example.com"' in rendered
    assert escape('<b onmouseover="x()">Ann</b>') in rendered
    # The e-mail is only the title: it is not in the visible text.
    visible = rendered.split(">", 1)[1].rsplit("<", 1)[0]
    assert "example.com" not in visible


def test_the_staff_name_falls_back_to_the_username_never_the_email() -> None:
    user = User.objects.create_user("jdoe", email="jdoe@example.com")
    assert staff_name(user) == "jdoe"
    user.first_name = "  "
    assert staff_name(user) == "jdoe"
    user.last_name = "Doe"
    assert staff_name(user) == "Doe"


def test_the_staff_filter_renders_nothing_for_none_and_for_a_string() -> None:
    rendered = Template('{% load people %}[{{ a|staff }}][{{ b|staff|default:"—" }}]').render(
        Context({"a": "someone@example.com", "b": None})
    )
    assert rendered == "[][—]"


# --- The held-form guard ------------------------------------------------------------------------


def test_a_routing_and_account_pair_in_a_held_note_never_reaches_the_session() -> None:
    from ledger.views.operators import _holds_payment_data

    assert _holds_payment_data("Routing 021000021 account 000123456789")
    assert _holds_payment_data("4111 1111 1111 1111")
    assert not _holds_payment_data("Wire reference WR-2026-0042")


# --- The Payments help topic ------------------------------------------------------------------


def test_the_payments_topic_is_read_by_a_viewer_and_refused_signed_out() -> None:
    viewer, _ = client_for(Role.VIEWER)
    response = viewer.get(reverse("finance:help_topic", kwargs={"slug": "payments"}))
    assert response.status_code == 200
    body = response.content.decode()
    assert "account number" in body and "Zelle" in body
    anonymous = Client()
    response = anonymous.get(reverse("finance:help_topic", kwargs={"slug": "payments"}))
    assert response.status_code in (302, 401, 403)
