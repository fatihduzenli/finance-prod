"""PLAN-012g Batch C (Fable): the dashboard's mini calendar — its legend, dots by kind and
no-script day lists — and the adversarial cases of plan §5 (`?cal=2026-13`, a day with every
kind, an empty dashboard, a stale `?range=`). PLAN-015 Batch 1 removed the Cashflow, Payment
status and Needs attention cards and their tests (the order, the presets, the colours, the
boxes, "All n ›"); tests/test_dashboard_v2.py covers the new page."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.services import calendar
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    # The dashboard reads django.utils.timezone.localdate (there is no ``today`` name to patch:
    # the earlier raising=False patches pinned nothing, and the tests passed only while the
    # real month was October 2026).
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def page(client: Client, query: str = "") -> str:
    response = client.get(f"/{query}")
    assert response.status_code == 200
    return response.content.decode()


def section(html: str, element_id: str) -> str:
    start = html.index(f'id="{element_id}"')
    return html[start : html.index("</section>", start)]


# --- Mini calendar --------------------------------------------------------------------------------


def a_day_with_every_kind() -> date:
    """October 15: a rent debit, a lease ending, an insurance renewal and a note on one day."""
    from tests.test_plan012fa_d_calendar import a_note

    day = date(2026, 10, 15)
    lease = a_lease(a_store("1501"), an_operator("Kinds Co"), started=date(2026, 1, 15))
    a_rent_charge(lease, date(2026, 10, 1), due="1000.00")
    a_note(day, "Promise to pay", lease=lease)
    return day


def test_a_day_shows_one_dot_per_kind_up_to_three_and_the_legend_names_them() -> None:
    a_day_with_every_kind()
    html = section(page(client_for(Role.BILLING)[0]), "mini-calendar")
    start = html.index('data-day="2026-10-15"')
    cell = html[
        start : html.find("data-day=", start + 10)
        if html.find("data-day=", start + 10) > 0
        else len(html)
    ]
    dots = re.findall(r'class="mini-dot kind-([a-z_]+)"', cell)
    assert 1 <= len(dots) <= 3 and len(dots) == len(set(dots)), cell[:400]
    legend = html[html.index('class="mini-legend"') :]
    for kind in dots:
        assert calendar.KIND_LABELS[kind] in legend
    assert (
        'class="mini-panel"' in html and "mini-day-items" in html
    )  # the phone's panel and fallback


def test_the_month_title_links_to_the_calendar_and_the_day_lists_exist_without_script() -> None:
    a_day_with_every_kind()
    html = section(page(client_for(Role.VIEWER)[0]), "mini-calendar")
    assert re.search(
        r'<a[^>]+id="mini-month"[^>]*href="/calendar/\?month=2026-10"', html
    ) or re.search(r'<a[^>]+href="/calendar/\?month=2026-10"[^>]*id="mini-month"', html)
    assert html.count('<details class="mini-day-items"') >= 1


def test_a_month_without_items_has_no_legend_and_no_day_lists() -> None:
    html = section(page(client_for(Role.VIEWER)[0], "?cal=2031-02"), "mini-calendar")
    assert 'class="mini-legend"' not in html and "mini-day-items" not in html


# --- Payment status and the attention card --------------------------------------------------------


def test_an_empty_dashboard_still_renders_every_card() -> None:
    html = page(client_for(Role.VIEWER)[0])
    # PLAN-015 Batch 1 re-pin: Design v2's cards; an empty feed says so in one sentence.
    for marker in (
        "figures-card",
        "collections-card",
        "operators-card",
        "activity-card",
        "mini-calendar",
    ):
        assert f'id="{marker}"' in html, marker
    assert 'id="activity-empty">No activity matches.<' in html


# --- Hostile parameters ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "query", ["?cal=2026-13", "?cal=99999-01", "?cal=<script>", "?range=6m&cal=2026-00"]
)
def test_a_bad_cal_falls_back_to_the_current_month(query: str) -> None:
    html = page(client_for(Role.VIEWER)[0], query)
    assert (
        'id="mini-month"' in html and "<script>" not in html.split('id="mini-calendar"')[1][:2000]
    )
    assert "Oct 2026" in section(html, "mini-calendar") or "October 2026" in section(
        html, "mini-calendar"
    )


@pytest.mark.parametrize(
    "query", ["?range=6m&from=2026-01", "?range=7m", "?range=6M", "?range=6m%00"]
)
def test_a_bad_or_mixed_range_never_breaks_the_page(query: str) -> None:
    html = page(client_for(Role.VIEWER)[0], query)
    assert 'id="collections-card"' in html  # PLAN-015 Batch 1 re-pin: ?range= is not read
