"""PLAN-015i, Fable's adversarial cases (docs/qa/15/PLAN-015i-forms-filters-residuals.md §5): the
filter component on the Activity log and on Messages under hostile or odd input."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from tests.test_operator_views import client_for
from tests.test_plan012fa_c_builder import element
from tests.test_plan015_batch6_admin import an_event, page
from tests.test_plan015i_forms_filters import chips, clear_url, params, toggle

pytestmark = pytest.mark.django_db


def admin() -> Client:
    return client_for(Role.ADMIN)[0]


def test_messages_never_reflect_a_hostile_outcome_or_type() -> None:
    an_event("processed")
    body = page(
        admin(),
        "/webhooks/?outcome=<script>alert(1)</script>&type=%22><img src=x onerror=1>",
    )
    assert "alert(1)" not in body and "onerror" not in body
    assert chips(body) == [] and toggle(body) == "Filters"
    assert clear_url(body) == "/webhooks/"


def test_removing_one_date_chip_keeps_every_other_filter_and_drops_the_page() -> None:
    body = page(
        admin(),
        "/audit/?action=kept.*&user=somebody&from=2026-10-01&to=2026-10-20&page=4",
    )
    by_label = {words.split(":")[0]: href for href, words in chips(body)}
    assert set(by_label) == {"Action", "User", "From", "To"}
    assert params(by_label["From"]) == {
        "action": ["kept.*"],
        "user": ["somebody"],
        "to": ["2026-10-20"],
    }
    assert "page" not in params(by_label["To"]) and "to" not in params(by_label["To"])


@pytest.mark.parametrize("path", ["/audit/", "/webhooks/"])
def test_the_filter_card_holds_one_get_form_without_a_token(path: str) -> None:
    body = page(admin(), path)
    card = element(body, '<div class="card filter-card">', "div")
    assert card.count("<form") == 1 and 'method="get"' in card
    # A GET form must not carry the CSRF token into the address bar.
    assert "csrfmiddlewaretoken" not in card
    assert 'id="filters-apply"' in card and "style=" not in card


def test_a_repeated_parameter_gives_one_chip_and_no_error() -> None:
    body = page(admin(), "/audit/?action=first&action=second")
    words = [text for _, text in chips(body)]
    assert len(words) == 1 and words[0].startswith("Action: ")
    assert toggle(body).endswith("1")


@pytest.mark.parametrize("value", ["%00", "%F0%9F%92%B8", "%E2%80%AE", "%0D%0ASet-Cookie:x=1"])
def test_odd_bytes_in_a_filter_do_not_break_the_page(value: str) -> None:
    response = admin().get(f"/audit/?user={value}&entity_id={value}")
    assert response.status_code == 200
    assert "Set-Cookie" not in response.headers.get("Location", "")
    assert "x" not in response.cookies
    for href, _ in chips(response.content.decode()):
        assert href.startswith("/audit/?") and "\r" not in href and "\n" not in href


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
@pytest.mark.parametrize("name", ["ledger:audit:page", "ledger:audit:export"])
def test_a_filter_does_not_open_the_activity_log_to_other_roles(role: Role, name: str) -> None:
    assert client_for(role)[0].get(reverse(name) + "?action=x").status_code == 403
