"""PLAN-015f §3.1 in a browser: an open promise's "Outcome ▾" and "⋯" menus on the store's
Notes & follow-ups tab; the arrow keys move between a menu's items (Home and End to the ends),
Escape closes it and gives the focus back to its summary, Tab leaves it; on a phone the two
summaries share one line under the entry's meta line, the text keeps the full width, and an
open list stays on the screen; a wrapped meta line keeps its line height and the store link a
44 px tap box (PLAN-015g §3D). PLAN-015f §3.2: the Lease ▾
menu reaches Pause collection from the keyboard; a menu of buttons (the operator's More ▾)
gets the menu roles and the arrow keys from the shared script."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize, expect
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import ContactEntry, ContactKind, Store
from ledger.services import contacts
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1280, "height": 800}
PHONE: ViewportSize = {"width": 390, "height": 844}


@pytest.fixture
def laptop(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(viewport=LAPTOP)
    yield context
    context.close()


@pytest.fixture
def phone(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(
        viewport=PHONE, device_scale_factor=3, is_mobile=True, has_touch=True
    )
    yield context
    context.close()


def a_world() -> tuple[Store, ContactEntry]:
    store = a_store("1515", number=1515)
    operator = an_operator("Menu Keys Co")
    lease = a_lease(store, operator, started=date(2025, 7, 1))
    a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    added = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.PROMISE,
            happened_on=date(2026, 10, 1),
            body="Pays on Friday.",
            promised_date=date(2026, 10, 30),
            promised_amount=Decimal("1000.00"),
            lease=lease.pk,
        ),
        actor=None,
    )
    assert added.entry is not None
    return store, added.entry


def signed_in(context: BrowserContext, live_server: LiveServer, username: str) -> Page:
    user, device = make_user(username, Role.ADMIN)
    assert device is not None
    page = context.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


def focused_text(page: Page) -> str:
    return str(page.evaluate("document.activeElement.textContent.trim()"))


def test_the_entry_menus_follow_the_keyboard(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    store, promise = a_world()
    page = signed_in(laptop, live_server, "e2e-menus-keys")
    page.goto(f"{live_server.url}/stores/{store.pk}/?tab=contacts")
    outcome = page.locator(f"#outcome-{promise.pk}")
    summary = outcome.locator("summary")
    summary.focus()
    page.keyboard.press("Enter")
    expect(outcome.locator("ul")).to_be_visible()
    page.keyboard.press("ArrowDown")
    assert focused_text(page) == "Kept"
    page.keyboard.press("ArrowDown")
    assert focused_text(page) == "Reschedule"
    page.keyboard.press("End")
    assert focused_text(page) == "Broken"
    page.keyboard.press("ArrowDown")  # wraps round
    assert focused_text(page) == "Kept"
    page.keyboard.press("ArrowUp")
    assert focused_text(page) == "Broken"
    page.keyboard.press("Home")
    assert focused_text(page) == "Kept"
    page.keyboard.press("Escape")
    expect(outcome.locator("ul")).to_be_hidden()
    assert focused_text(page) == "Outcome ▾"
    # The "⋯" menu: Add correction, then Delete; Tab leaves the menu (no trap).
    more = page.locator(f"#more-{promise.pk}")
    more.locator("summary").focus()
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    assert focused_text(page) == "Add correction"
    page.keyboard.press("ArrowDown")
    assert focused_text(page) == "Delete"
    page.keyboard.press("Tab")
    inside = page.evaluate(f"!!document.activeElement.closest('#more-{promise.pk}')")
    assert inside is False
    # A click outside closes it; Delete leads to the confirmation page.
    page.locator("h1").click()
    expect(more.locator("ul")).to_be_hidden()
    more.locator("summary").click()
    more.get_by_role("menuitem", name="Delete").click()
    page.wait_for_url(f"{live_server.url}/contacts/{promise.pk}/delete/")


def test_the_entry_menus_stay_on_one_line_on_the_phone(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    store, promise = a_world()
    page = signed_in(phone, live_server, "e2e-menus-phone")
    page.goto(f"{live_server.url}/stores/{store.pk}/?tab=contacts")
    outcome = page.locator(f"#outcome-{promise.pk} summary").bounding_box()
    more = page.locator(f"#more-{promise.pk} summary").bounding_box()
    assert outcome is not None and more is not None
    assert outcome["y"] == pytest.approx(more["y"], abs=1)
    assert outcome["x"] + outcome["width"] <= more["x"]
    assert more["x"] + more["width"] <= PHONE["width"]
    # The menus take their own line under the meta line; the entry's text keeps the full
    # width beside the round letter (review finding 2: not a 118 px column).
    text = page.locator(f"#contact-{promise.pk} .contact-body").bounding_box()
    assert text is not None and text["width"] >= 250
    meta = page.locator(f"#contact-{promise.pk} .contact-meta").bounding_box()
    assert meta is not None and outcome["y"] >= meta["y"] + meta["height"]
    page.locator(f"#outcome-{promise.pk} summary").click()
    listed = page.locator(f"#outcome-{promise.pk} ul").bounding_box()
    assert listed is not None
    assert listed["x"] >= 0 and listed["x"] + listed["width"] <= PHONE["width"]
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= PHONE["width"]


def test_a_wrapped_meta_line_keeps_its_line_height_and_the_store_link_a_44px_tap_box(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    # PLAN-015g §3D: a long store label wraps the meta line onto a second line; the link no
    # longer stretches the line to 44 px, and its transparent ::after box keeps the tap area.
    store, promise = a_world()
    Store.objects.filter(pk=store.pk).update(
        external_label="1515 Peachtree Corners Town Center Outparcel North"
    )
    page = signed_in(phone, live_server, "e2e-menus-meta")
    page.goto(f"{live_server.url}/stores/{store.pk}/?tab=contacts")
    meta = page.locator(f"#contact-{promise.pk} .contact-meta")
    found = meta.evaluate(
        """(p) => {
            const style = getComputedStyle(p);
            const link = p.querySelector("a");
            const after = getComputedStyle(link, "::after");
            const line = style.lineHeight === "normal"
                ? parseFloat(style.fontSize) * 1.2 : parseFloat(style.lineHeight);
            return {
                height: p.getBoundingClientRect().height,
                line: line,
                afterHeight: after.height,
                afterPosition: after.position,
                linkDisplay: getComputedStyle(link).display,
            };
        }"""
    )
    assert found["height"] > 1.5 * found["line"], found  # the line really wraps
    assert found["height"] <= 2 * found["line"] + 2, found
    assert (found["afterHeight"], found["afterPosition"]) == ("44px", "absolute"), found
    assert found["linkDisplay"] == "inline", found
    # The menus still take their own line under the meta line.
    box = meta.bounding_box()
    outcome = page.locator(f"#outcome-{promise.pk} summary").bounding_box()
    assert box is not None and outcome is not None
    assert outcome["y"] >= box["y"] + box["height"]
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= PHONE["width"]


def test_the_lease_menu_opens_pause_collection_from_the_keyboard(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    store, _ = a_world()
    page = signed_in(laptop, live_server, "e2e-menus-lease")
    page.goto(f"{live_server.url}/stores/{store.pk}/")
    expect(page.locator("#header-status a")).to_have_count(0)
    page.locator("#lease-menu summary").focus()
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    assert focused_text(page) == "Edit lease"
    page.keyboard.press("ArrowDown")
    assert focused_text(page) == "Pause collection"
    page.keyboard.press("Enter")
    page.wait_for_url("**/leases/*/pause-collection/")
    expect(page.locator("#pause-collection-dialog")).to_be_visible()


def test_a_menu_of_buttons_gets_the_roles_and_the_arrow_keys(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    _, promise = a_world()
    page = signed_in(laptop, live_server, "e2e-menus-buttons")
    page.goto(f"{live_server.url}/operators/{promise.operator_id}/")
    more = page.locator("#more-menu")
    expect(more.locator("summary")).to_have_attribute("aria-haspopup", "menu")
    expect(more.locator("ul")).to_have_attribute("role", "menu")
    expect(more.locator("#archive-operator")).to_have_attribute("role", "menuitem")
    more.locator("summary").focus()
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.id") == "archive-operator"
    page.keyboard.press("Escape")
    expect(more.locator("ul")).to_be_hidden()
