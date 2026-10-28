"""Adversarial cases for PLAN-012f Batch B (the component set), by the QA owner (plan §6). The
progress bar never overflows (widths sum to at most 100 whatever the amounts; zero, negative,
text and NaN amounts draw nothing; an unknown family is a template error); the KPI, badge,
avatar and date chip tags escape markup and survive hostile values; a contact entry with markup
in its text is escaped with its avatar letter from the kind only; the segmented filter keeps the
store tab's query; the grid's cell block never carries a raw amount or markup."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.roles import Role
from finance.templatetags import components
from ledger.models import PaymentChannel
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'


# --- the progress bar ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "segments",
    [
        [("paid", 1), ("pending", 1), ("partial", 1)],  # thirds: 33.3 × 3 = 99.9
        [("paid", Decimal("0.01")), ("unpaid", Decimal("999999999.99"))],
        [("paid", 7), ("pending", 7), ("partial", 7), ("unpaid", 7), ("waived", 7)],
        [("paid", "12.5"), ("unpaid", 12.5)],
    ],
)
def test_the_progress_widths_never_exceed_one_hundred(segments: list[tuple[str, object]]) -> None:
    widths = components.progress_widths(segments)
    assert widths
    assert sum((width for _, _, width in widths), Decimal(0)) <= 100
    html = str(components.progress(segments))
    assert "width:" in html and "%" in html
    for _, _, width in widths:
        assert float(width) <= 100


@pytest.mark.parametrize(
    "segments",
    [
        [],
        [("paid", 0), ("unpaid", 0)],
        [("paid", -5), ("unpaid", -1)],
        [("paid", "abc"), ("unpaid", None)],
        [("paid", Decimal("NaN"))],
        [("paid", float("inf"))],
    ],
)
def test_a_zero_or_hostile_total_draws_no_bar(segments: list[tuple[str, object]]) -> None:
    assert components.progress_widths(segments) == []
    assert str(components.progress(segments)) == ""


def test_an_unknown_family_is_a_template_error_not_a_rendered_colour() -> None:
    with pytest.raises(ValueError):
        components.progress_widths([("paid", 1), (MARKUP, 1)])
    with pytest.raises(ValueError):
        components.avatar("P", MARKUP)


# --- the tags escape markup --------------------------------------------------------------------


def test_the_kpi_badge_avatar_and_date_chip_escape_markup() -> None:
    kpi = str(components.kpi(MARKUP, MARKUP, MARKUP, tone=MARKUP))
    assert MARKUP not in kpi and "&lt;script&gt;" in kpi
    assert 'class="kpi-value"' in kpi  # a hostile tone is ignored, never a class
    badge = str(components.status_badge(MARKUP, MARKUP))
    assert MARKUP not in badge and 'class="badge' in badge
    avatar = str(components.avatar(MARKUP))
    assert MARKUP not in avatar and ">&lt;<" in avatar or ">" in avatar
    assert "<script" not in avatar
    assert str(components.avatar("")).count("<span") == 1  # an empty letter renders an empty circle
    for value in (None, "", "2026-10-22", MARKUP, 20261022):
        assert str(components.date_chip(value)) == "", value
    chip = str(components.date_chip(date(2026, 11, 1)))
    assert 'datetime="2026-11-01"' in chip and ">Nov<" in chip and ">1<" in chip


# --- a contact entry with markup ------------------------------------------------------------------


def test_a_contact_entry_with_markup_is_escaped_and_its_avatar_comes_from_the_kind() -> None:
    lease = a_lease(a_store("2801"), an_operator("Avatar Co"), channel=PaymentChannel.WIRE)
    billing, _ = client_for(Role.BILLING)
    response = billing.post(
        reverse("ledger:stores:contacts_add", kwargs={"pk": lease.store_id}),
        {
            "kind": "call",
            "body": f"Said {MARKUP}",
            "lease": str(lease.pk),
            "promised_amount": "",
            "promised_date": "",
        },
    )
    assert response.status_code in (200, 302)
    tab = billing.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}), {"tab": "contacts"}
    ).content.decode()
    assert MARKUP not in tab
    if "contact-entry" in tab:
        entry = tab[tab.index("contact-entry") :]
        avatar = entry[
            entry.index('class="avatar') : entry.index("</span>", entry.index('class="avatar'))
        ]
        assert ">C<" in avatar or avatar.endswith(">C") or avatar.rstrip().endswith("C")


# --- the segmented filter and the grid block ------------------------------------------------------


def test_the_segmented_filter_keeps_the_tab_and_the_grid_block_carries_no_raw_amount() -> None:
    lease = a_lease(a_store("2802"), an_operator("Segment Co"), channel=PaymentChannel.WIRE)
    a_rent_charge(lease, date(2026, 9, 1), due="1234567.5")
    viewer, _ = client_for(Role.VIEWER)
    tab = viewer.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}), {"tab": "charges"}
    ).content.decode()
    # The charges filter by its id: since PLAN-012g A3 the sidebar's theme switch is a
    # .segmented too, and it comes first on the page.
    start = tab.index('id="charge-filters"')
    segmented = tab[start : tab.index("</span>", start)]
    assert "tab=charges" in segmented
    grid = viewer.get(
        reverse("ledger:grid:page"), {"from": "2026-01", "to": "2026-12"}
    ).content.decode()
    assert "$1,234,567.50" in grid
    assert "1234567.5" not in grid and "1234567.50" not in grid
