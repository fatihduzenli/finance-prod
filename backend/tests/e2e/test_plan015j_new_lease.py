"""PLAN-015j task 3 in a browser (Chromium): the Move-in charges row's When select enables once
the price holds a value; "In installments" shows the payments sentence, equal to
``installments.payments_preview`` over a table of cases; the Summary lists the row off the
first invoice in the server's words; the saved lease's charges list shows the words; at 390 px
the select sits under the price and the page does not scroll sideways
(docs/qa/15/PLAN-015j-move-in-charge-timing.md §4)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from playwright.sync_api import Browser, expect
from pytest_django.live_server_helper import LiveServer

from ledger.models import Lease
from ledger.services import installments
from tests.e2e.test_new_lease import LAPTOP, PHONE, a_world, fill_the_reference, signed_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

#: (price, count, first date, cadence): with and without a remainder, counts 2 to 12, the three
#: cadences, a first date on the 31st, a year boundary, a leap February.
CASES = [
    ("1000.00", "3", "2026-12-01", "monthly"),
    ("3000.00", "3", "2026-12-01", "monthly"),
    ("100.00", "2", "2026-11-03", "monthly"),
    ("100.00", "12", "2026-11-03", "monthly"),
    ("1000.00", "7", "2026-01-31", "monthly"),
    ("2500.00", "4", "2026-10-31", "monthly"),
    ("999.99", "5", "2027-08-31", "monthly"),
    ("1234.56", "6", "2026-12-15", "monthly"),
    ("150.00", "3", "2026-12-28", "weekly"),
    ("100.00", "3", "2026-12-20", "weekly"),
    ("500.00", "9", "2026-12-10", "biweekly"),
    ("80.00", "2", "2026-12-31", "biweekly"),
    ("0.10", "10", "2026-12-01", "monthly"),
    ("0.24", "12", "2027-02-28", "weekly"),
    ("3000", "11", "2027-12-31", "monthly"),
    ("45.5", "8", "2028-01-31", "monthly"),
    ("10000.01", "12", "2026-11-30", "biweekly"),
]


def server_sentence(price: str, count: str, first: str, cadence: str) -> str:
    return installments.payments_preview(
        Decimal(price), int(count), date.fromisoformat(first), cadence
    )


def test_the_script_payments_sentence_equals_the_servers(
    live_server: LiveServer, browser: Browser
) -> None:
    store, _operator = a_world()
    context = browser.new_context(viewport=LAPTOP)
    page = signed_in(context, live_server, "e2e-015j-preview", "light")
    page.goto(f"{live_server.url}/leases/new/?store={store.pk}")
    found = page.evaluate(
        "(cases) => cases.map((c) => window.ledgerNewLease.paymentsPreview(...c))",
        [list(case) for case in CASES],
    )
    assert found == [server_sentence(*case) for case in CASES]
    assert len({sentence for sentence in found}) == len(CASES)
    assert any(", the last " in sentence for sentence in found)
    assert any(", the last " not in sentence for sentence in found)
    # Below one cent a share: none, as the server's form shows none.
    assert (
        page.evaluate("window.ledgerNewLease.paymentsPreview('0.10', '12', '2026-12-01', '')") == ""
    )
    context.close()


def test_price_when_installments_summary_and_the_saved_words(
    live_server: LiveServer, browser: Browser
) -> None:
    store, operator = a_world()
    context = browser.new_context(viewport=LAPTOP)
    page = signed_in(context, live_server, "e2e-015j-flow", "light")
    page.goto(f"{live_server.url}/leases/new/?store={store.pk}")
    fill_the_reference(page, operator)
    # The access fee has no price: its When is disabled; a price enables it.
    access = page.locator("#id_setup_access_fee_when")
    expect(access).to_be_disabled()
    expect(page.get_by_label("When: Access fee")).to_be_disabled()
    page.fill("#id_setup_access_fee_price", "1500.00")
    expect(access).to_be_enabled()
    page.fill("#id_setup_access_fee_price", "")
    expect(access).to_be_disabled()
    # The sign fee in 3 installments from Dec 1, 2026.
    preview = page.locator("#setup-sign_fee-preview")
    expect(preview).to_be_hidden()
    page.select_option("#id_setup_sign_fee_when", "installments")
    expect(page.locator("#id_setup_sign_fee_count")).to_be_visible()
    expect(page.locator("#id_setup_sign_fee_on")).to_be_hidden()
    page.fill("#id_setup_sign_fee_count", "3")
    page.fill("#id_setup_sign_fee_first_due", "2026-12-01")
    expected = server_sentence("500.00", "3", "2026-12-01", "monthly")
    expect(preview).to_have_text(expected)
    assert expected == "3 payments of $166.66: Dec 1, 2026 … Feb 1, 2027, the last $166.68"
    # The deposit due on a date; the Summary lists both off the first invoice.
    page.select_option("#id_setup_security_deposit_when", "on_date")
    page.fill("#id_setup_security_deposit_on", "2026-12-15")
    expect(page.locator("#summary-timed li")).to_have_text(
        [
            "Security deposit · $3,000.00 · due Dec 15, 2026",
            "Sign fee · $500.00 · 3 payments from Dec 1, 2026",
        ]
    )
    expect(page.locator("#setup-total")).to_have_text("$300.00")
    page.locator("#save-lease").click()
    page.wait_for_load_state()
    lease = Lease.objects.get(store=store)
    page.goto(f"{live_server.url}/stores/{store.pk}/?tab=charges&filter=all")
    timing = page.locator(".charge-timing")
    expect(timing).to_have_count(2)
    assert sorted(timing.all_inner_texts()) == ["0 of 3 installments paid", "due Dec 15, 2026"]
    assert lease.started_on == date(2026, 11, 15)
    context.close()


def test_on_a_phone_the_select_is_under_the_price(
    live_server: LiveServer, browser: Browser
) -> None:
    store, operator = a_world()
    context = browser.new_context(
        viewport=PHONE, device_scale_factor=3, is_mobile=True, has_touch=True
    )
    page = signed_in(context, live_server, "e2e-015j-phone", "dark")
    page.goto(f"{live_server.url}/leases/new/?store={store.pk}")
    fill_the_reference(page, operator)
    page.select_option("#id_setup_sign_fee_when", "installments")
    page.fill("#id_setup_sign_fee_count", "3")
    page.fill("#id_setup_sign_fee_first_due", "2026-12-01")
    price = page.locator("#id_setup_sign_fee_price").bounding_box()
    select = page.locator("#id_setup_sign_fee_when").bounding_box()
    row = page.locator("#setup-sign_fee").bounding_box()
    assert price is not None and select is not None and row is not None
    assert select["y"] >= price["y"] + price["height"]
    assert abs(select["width"] - row["width"]) <= 1  # its own full-width line
    expect(page.locator("#setup-sign_fee-preview")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    context.close()
