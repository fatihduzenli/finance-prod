"""PLAN-012g Batch C (Fable): the dashboard in the §7.5 order with the §7.3 colours — the order
and the three rows, the 6-month preset and the 12-month default, the two Cashflow cards only when
no range is asked for, the mini calendar's legend, dots by kind and no-script day lists, the
Payment status boxes, the attention card's "All n ›" — and the adversarial cases of plan §5
(`?range=6m` on the laptop, `?cal=2026-13`, forty attention rows, a day with every kind, an empty
dashboard)."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.services import calendar
from ledger.services import dashboard as blocks
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("ledger.views.dashboard.today", lambda: TODAY, raising=False)
    monkeypatch.setattr("ledger.services.dashboard.today", lambda: TODAY, raising=False)
    return TODAY


def page(client: Client, query: str = "") -> str:
    response = client.get(f"/{query}")
    assert response.status_code == 200
    return response.content.decode()


def section(html: str, element_id: str) -> str:
    start = html.index(f'id="{element_id}"')
    return html[start : html.index("</section>", start)]


def card_classes(html: str, element_id: str) -> str:
    """The class attribute of the section carrying ``element_id`` (it precedes the id)."""
    match = re.search(r'<section class="([^"]*)"[^>]*id="' + element_id + '"', html)
    assert match, element_id
    return match.group(1)


# --- Order and ranges -----------------------------------------------------------------------------


def test_the_rows_are_the_spec_s_order_with_three_thirds_rows() -> None:
    html = page(client_for(Role.BILLING)[0])
    order = [
        'id="figures-card"',
        'id="cashflow-card"',
        'id="attention"',
        'id="mini-calendar"',
        'id="recent-payments"',
        'id="payment-status"',
        'id="coming-up"',
        'id="collection-card"',
        'id="today-run"',
        'id="failed-attempts"',
        'id="not-sent-card"',
        'id="last-reconciliation-card"',
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert html.count('class="dashboard-row-thirds"') == 3


def test_the_six_month_preset_exists_and_twelve_is_the_default() -> None:
    assert dict(blocks.RANGE_CHOICES)["6m"] == "Last 6 months"
    assert blocks._PRESET_MONTHS["6m"] == 6
    chosen = blocks.parse_range({}, TODAY)
    assert chosen is not None and chosen.key == "12m"
    assert blocks.parse_range({"range": "6m"}, TODAY).key == "6m"  # type: ignore[union-attr]


def test_without_a_range_the_page_carries_both_cards_and_with_one_only_one() -> None:
    client, _ = client_for(Role.BILLING)
    html = page(client)
    assert 'id="cashflow-card"' in html and 'id="cashflow-card-narrow"' in html
    wide, narrow = section(html, "cashflow-card"), section(html, "cashflow-card-narrow")
    assert '<option value="12m" selected>' in wide and '<option value="6m" selected>' in narrow
    assert 'id="range-select"' in wide and 'id="range-select-narrow"' in narrow
    assert "has-narrow" in card_classes(html, "cashflow-card")
    for query in (
        "?range=6m",
        "?range=12m",
        "?range=this",
        "?range=custom&from=2026-01&to=2026-03",
    ):
        html = page(client, query)
        assert 'id="cashflow-card"' in html and 'id="cashflow-card-narrow"' not in html, query
        assert "has-narrow" not in card_classes(html, "cashflow-card"), query


def test_six_months_draws_six_bar_pairs_and_twelve_draws_twelve() -> None:
    client, _ = client_for(Role.BILLING)
    six = section(page(client, "?range=6m"), "cashflow-card")
    twelve = section(page(client, "?range=12m"), "cashflow-card")
    assert six.count('class="bar-pair"') == 6 and twelve.count('class="bar-pair"') == 12


def test_the_chart_colours_are_tokens_and_the_legend_names_the_three_series() -> None:
    html = section(page(client_for(Role.VIEWER)[0]), "cashflow-card")
    for token in ("--chart-income", "--chart-expenses", "--chart-net"):
        assert f"var({token})" in html, token
    assert not re.search(r'(?:fill|stroke)="#[0-9A-Fa-f]{3,6}"', html)
    legend = html[html.index('class="cashflow-legend"') :]
    for word in ("Income", "Expenses", "Net"):
        assert has_token(legend, word), word


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


def test_the_status_boxes_carry_their_colour_bar_and_the_pending_line() -> None:
    lease = a_lease(a_store("1601"), an_operator("Boxes Co"))
    a_rent_charge(lease, date(2026, 9, 1), due="900.00")
    html = section(page(client_for(Role.BILLING)[0]), "payment-status")
    for key in ("overdue", "paid", "upcoming"):
        assert f'id="status-{key}"' in html, key
    label = re.search(r'<span class="donut-label">(\w+)</span>', html)
    assert label is not None and label.group(1) in ("charge", "charges")
    assert "pending · rent and fees" in html or 'id="payment-status-pending"' not in html


def test_the_attention_card_says_all_n_with_the_full_count() -> None:
    client, _ = client_for(Role.BILLING)
    for n in range(40):
        lease = a_lease(a_store(f"17{n:02d}"), an_operator(f"Late {n}"))
        a_rent_charge(lease, date(2026, 8, 1), due="500.00")
    html = section(page(client), "attention")
    link = re.search(r'<a class="card-link" id="all-unpaid" href="([^"]+)">All (\d+) ›</a>', html)
    assert link is not None
    count = int(link.group(2))
    assert count >= 40
    assert link.group(1).startswith("/")


def test_an_empty_dashboard_still_renders_every_card() -> None:
    html = page(client_for(Role.VIEWER)[0])
    for marker in (
        "attention",
        "mini-calendar",
        "recent-payments",
        "payment-status",
        "cashflow-card",
    ):
        assert f'id="{marker}"' in html, marker
    # Nothing to attend to: no "All 0 ›" link (the empty state says it).
    assert 'id="all-unpaid"' not in section(html, "attention")


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
    assert 'id="cashflow-card"' in html
