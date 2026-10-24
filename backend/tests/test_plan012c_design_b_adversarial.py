"""Adversarial cases for PLAN-012c Batch B (components, dashboard cards, grid polish, print),
by the QA owner (plan §6 B). A flash message with markup is escaped in the toast and the toast
never removes the text from the page; the print header escapes a company name with markup; a
hostile sort or page parameter answers 200 with sane ``aria-sort`` values; the wizards' step
lists mark exactly one current step and link only to steps that answer; the grid legend's
badges carry a glyph so colour is never the only signal; empty states never show "None"."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest
from django.contrib.messages import constants
from django.contrib.messages.storage.base import Message
from django.contrib.messages.storage.cookie import CookieStorage
from django.contrib.staticfiles import finders
from django.http import HttpRequest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import CompanySettings, PaymentChannel
from tests import test_lease_end as lease_end_tests
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_lease_end import draft
from tests.test_operator_views import client_for
from tests.test_transfer import begin

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
GLYPHS = "✓◻◐◑–●○✕!"
# The lease-end file's autouse fixtures, re-exported so pytest applies them here too.
storage = lease_end_tests.storage
memory = lease_end_tests.memory


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: lease_end_tests.TODAY)
    return lease_end_tests.TODAY


def _read(static_path: str) -> str:
    found = finders.find(static_path)
    assert found, static_path
    with open(found, encoding="utf-8") as handle:  # noqa: PTH123 - finders gives a path
        return handle.read()


def _with_flash(client: Client, text: str) -> None:
    """Queue a flash message for the next request through the cookie storage."""
    storage: Any = CookieStorage(HttpRequest())
    encoded = storage._encode([Message(constants.ERROR, text)])  # noqa: SLF001
    client.cookies[CookieStorage.cookie_name] = encoded


# --- the toast ----------------------------------------------------------------------------------


def test_a_flash_message_with_markup_is_escaped_in_the_toast_and_stays_in_the_page() -> None:
    admin, _ = client_for(Role.ADMIN)
    _with_flash(admin, f"Refused: {MARKUP}")
    body = admin.get("/").content.decode()
    assert MARKUP not in body
    assert "Refused: &lt;script&gt;" in body
    toast = body[body.index("Refused:") - 600 : body.index("Refused:")]
    assert 'class="toast' in toast or "class='toast" in toast
    assert 'role="status"' in toast


def test_the_toast_styles_never_remove_the_text_from_the_page() -> None:
    """The auto-fade is opacity only: no display none, no visibility hidden, no zero height."""
    css = _read("finance/app.css")
    blocks = re.findall(r"[^{}]*\.toast[^{}]*\{[^{}]*\}", css)
    assert blocks, "no .toast rule in app.css"
    joined = "\n".join(blocks)
    assert not re.search(r"display\s*:\s*none", joined), joined
    assert not re.search(r"visibility\s*:\s*hidden", joined), joined
    assert not re.search(r"(max-)?height\s*:\s*0", joined), joined
    # The keyframes of the fade only touch opacity (and may move the box).
    for frames in re.findall(r"@keyframes[^{]*\{((?:[^{}]*\{[^{}]*\})*)\s*\}", css):
        if "opacity" in frames:
            assert "display" not in frames and "visibility" not in frames, frames


# --- the print header -------------------------------------------------------------------------


def test_the_print_header_escapes_a_company_name_with_markup() -> None:
    row = CompanySettings.current()
    row.name = f"Monvera {MARKUP}"
    row.short_name = "Monvera"
    row.save()
    lease = a_lease(a_store("2481"), an_operator("Print Co"), channel=PaymentChannel.WIRE)
    viewer, _ = client_for(Role.VIEWER)
    for path in (
        reverse("ledger:operators:statement", kwargs={"pk": lease.operator_id}),
        reverse("ledger:reports:pnl"),
    ):
        response = viewer.get(path)
        assert response.status_code == 200, path
        body = response.content.decode()
        assert MARKUP not in body, path
        assert "print.css" in body, path
        assert "Monvera &lt;script&gt;" in body, path


# --- hostile sort and page parameters ---------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"sort": MARKUP, "dir": MARKUP},
        {"sort": "' OR 1=1 --", "dir": "asc"},
        {"sort": "owed" * 500, "dir": "x"},
        {"sort": "", "dir": ""},
    ],
)
def test_a_hostile_sort_answers_200_with_sane_aria_sort_values(params: dict[str, str]) -> None:
    a_lease(a_store("2482"), an_operator("Sort Co"), channel=PaymentChannel.WIRE)
    viewer, _ = client_for(Role.VIEWER)
    response = viewer.get(reverse("ledger:arrears:page"), params)
    assert response.status_code == 200
    body = response.content.decode()
    assert MARKUP not in body and "' OR 1=1" not in body
    values = re.findall(r'aria-sort="([^"]*)"', body)
    assert set(values) <= {"ascending", "descending", "none"}, values
    assert sum(1 for value in values if value != "none") <= 1, values


@pytest.mark.parametrize("page", ["abc", "-1", "0", "99999", MARKUP, "1e3", "٣"])
def test_a_hostile_page_number_answers_200_on_every_paged_list(page: str) -> None:
    a_lease(a_store("2483"), an_operator("Page Co"), channel=PaymentChannel.WIRE)
    viewer, _ = client_for(Role.VIEWER)
    for route in (
        "ledger:payments:list",
        "ledger:leases:list",
        "ledger:returns:list",
        "ledger:operators:list",
    ):
        response = viewer.get(reverse(route), {"page": page})
        assert response.status_code == 200, (route, page)
        body = response.content.decode()
        assert MARKUP not in body, route  # one page: the pager is not shown, by design


# --- the wizards' step lists ------------------------------------------------------------------


def _step_links(body: str) -> list[str]:
    start = body.index('id="wizard-steps"')
    block = body[start : body.index("</ol>", start)]
    assert block.count('aria-current="step"') == 1, block
    return re.findall(r'href="([^"]+)"', block)


def test_the_lease_end_steps_mark_one_current_step_and_link_only_to_steps_that_answer() -> None:
    admin, user = client_for(Role.ADMIN)
    lease = a_lease(a_store("2484"), an_operator("Steps Co"), channel=PaymentChannel.WIRE)
    end = draft(lease, user)
    for name in ("terms", "blockers", "statement"):
        page = admin.get(reverse(f"ledger:lease_ends:{name}", kwargs={"pk": end.pk}))
        assert page.status_code == 200, name
        for href in _step_links(page.content.decode()):
            if "lease-ends" not in href:
                continue  # the store link above the list
            assert admin.get(href).status_code == 200, (name, href)


def test_the_transfer_steps_mark_one_current_step_and_link_only_to_steps_that_answer() -> None:
    admin, user = client_for(Role.ADMIN)
    # Started in the end month with October paid: a planned (not yet created) month blocks a
    # transfer like an existing one since PLAN-012f-a D, so the steps must answer.
    lease = a_lease(
        a_store("2485"),
        an_operator("Moving Co"),
        channel=PaymentChannel.WIRE,
        started=lease_end_tests.OCTOBER,
        rent="2000.00",
        debit_day=1,
    )
    lease_end_tests.october(lease, paid="2000.00")
    row = begin(lease, user)
    for name in ("operator", "blockers"):
        page = admin.get(reverse(f"ledger:transfers:{name}", kwargs={"pk": row.pk}))
        assert page.status_code == 200, name
        for href in _step_links(page.content.decode()):
            if "transfers" not in href:
                continue
            linked = admin.get(href)
            if linked.status_code == 302:
                # The "end" step hands over to the lease-end wizard by design; a redirect back
                # to an earlier transfer step would mean the link was offered too early.
                assert "lease-ends" in linked["Location"], (name, href, linked["Location"])
                assert admin.get(linked["Location"]).status_code == 200, (name, href)
            else:
                assert linked.status_code == 200, (name, href)


# --- badges and empty states ----------------------------------------------------------------


def test_every_badge_in_the_grid_legend_carries_a_glyph() -> None:
    a_lease(a_store("2486"), an_operator("Legend Co"), channel=PaymentChannel.WIRE)
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get(reverse("ledger:grid:page")).content.decode()
    badges = re.findall(r'<span class="badge badge-[a-z_-]+"[^>]*>(.*?)</span>', body, re.S)
    assert badges, "no status badge in the grid legend"
    for text in badges:
        plain = re.sub(r"<[^>]+>", "", text).strip()
        assert plain and plain[0] in GLYPHS, text


def test_empty_states_never_show_none_or_an_empty_link() -> None:
    admin, _ = client_for(Role.ADMIN)
    for route in (
        "ledger:stores:list",
        "ledger:operators:list",
        "ledger:payments:list",
        "ledger:returns:list",
        "ledger:disputes:page",
    ):
        body = admin.get(reverse(route)).content.decode()
        for empty in re.findall(
            r'<[a-z]+ class="empty"[^>]*>(.*?)</(?:p|div|section)>', body, re.S
        ):
            plain = re.sub(r"<[^>]+>", " ", empty)
            assert "None" not in plain.split() and ">None<" not in empty, (route, empty)
            assert 'href=""' not in empty and "href='" not in empty, (route, empty)
    # The dashboard's own "None." sentences keep their approved wording (plan §4.7, Batch D).
    body = admin.get("/").content.decode()
    assert 'href=""' not in body
