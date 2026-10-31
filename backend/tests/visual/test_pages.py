"""The visual regression pages (docs/qa/PLAN-012c-design-pass.md D10, §3.7; the family pages
of PLAN-012f-a Batch D): the main pages, the store workspace on each of its tabs, one page of
each family (lists, detail, forms, the wizard, the calendar) and the sign-in page (seen signed
out), at 1280 x 800 and 390 x 844, each a full-page PNG
compared pixel by pixel with ``baseline/<page>-<width>.png``.

A pixel differs when one of its channels moves by more than :data:`CHANNEL_THRESHOLD`; the
page fails when more than :data:`TOLERANCE` of its pixels differ, or when its size changed.
A failure names the page and writes ``<page>-<width>-actual.png``, ``-expected.png`` and
``-diff.png`` (the differing pixels in red over a faded copy of the page) under ``out/``.
``--update-baseline`` writes the baseline instead; a missing baseline fails and names the
command that writes it.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path

import pytest
from django.urls import reverse
from PIL import Image, ImageChops
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize

from ledger.models import OneOffCharge, Payment, PaymentStatus
from tests.visual import world
from tests.visual.conftest import COMMAND, DESKTOP, Baseline, Site, open_still

pytestmark = pytest.mark.visual

#: Per channel, out of 255: below this a pixel is the same (antialiasing, rounding).
CHANNEL_THRESHOLD = 16
#: The share of the image's pixels allowed to differ.
TOLERANCE = 0.005
VIEWPORTS: dict[int, ViewportSize] = {
    1280: {"width": 1280, "height": 800},
    390: {"width": 390, "height": 844},
}
STORE_TABS = ("charges", "invoices", "payments", "schedules", "documents", "contacts")


def store_tab(site: Site, tab: str) -> str:
    return f"/stores/{site.world.store.pk}/?tab={tab}"


#: The page's path in the data set; every page is seen as the billing user except
#: :data:`AS_ADMIN`.
PAGES: dict[str, Callable[[Site], str]] = {
    "dashboard": lambda site: "/",
    # PLAN-012f-b Batch A: the picker's scripts-off page (the search results for a new invoice).
    "search-new-invoice": lambda site: "/search/?q=0&intent=new_invoice",
    "stores": lambda site: "/stores/",
    **{f"store-{tab}": partial(store_tab, tab=tab) for tab in STORE_TABS},
    "operator": lambda site: f"/operators/{site.world.operator.pk}/",
    "grid": lambda site: "/grid/",
    "invoice-builder": lambda site: reverse(
        "ledger:invoices:draft", kwargs={"pk": site.world.draft.pk}
    ),
    "record-payment": lambda site: reverse(
        "ledger:stores:record_payment", kwargs={"pk": site.world.store.pk}
    ),
    "lease-end-statement": lambda site: reverse(
        "ledger:lease_ends:statement", kwargs={"pk": site.world.lease_end.pk}
    ),
    "pnl": lambda site: "/reports/pnl/",
    "daily-charges": lambda site: "/run/",
    # PLAN-012f-a Batch D: one page of each family beside the references.
    "operators": lambda site: "/operators/",
    "leases": lambda site: "/leases/",
    "payments": lambda site: "/payments/",
    "calendar": lambda site: "/calendar/",
    # PLAN-012f-b Batch C: Add payment on a one-off charge (the two radio cards).
    "add-payment": lambda site: reverse(
        "ledger:one_off_charges:payment", kwargs={"pk": one_off_of(site).pk}
    ),
    "charge-detail": lambda site: reverse(
        "ledger:one_off_charges:detail", kwargs={"pk": one_off_of(site).pk}
    ),
    "payment-detail": lambda site: reverse(
        "ledger:payments:detail", kwargs={"pk": settled_payment_of(site).pk}
    ),
    # PLAN-012f-b Batch E: a confirmation as a dialog over its parent, the Help pages, the
    # not-found page in the shell.
    "void-payment": lambda site: reverse(
        "ledger:payments:void", kwargs={"pk": settled_payment_of(site).pk}
    ),
    "help": lambda site: "/help/",
    "help-topic": lambda site: "/help/collecting-an-invoice/",
    "not-found": lambda site: "/no-such-page/",
    "new-store": lambda site: reverse("ledger:stores:create"),
    "new-lease": lambda site: reverse("ledger:leases:create"),
    "operator-form": lambda site: reverse(
        "ledger:operators:edit", kwargs={"pk": site.world.operator.pk}
    ),
    "login": lambda site: "/account/login/",
    # PLAN-014 B2: Admin › Settings (the Brands card), admins only.
    "settings": lambda site: "/admin-settings/",
    # PLAN-014 D4, D5: the message templates and the Outbox (one held message), admins only.
    "message-templates": lambda site: "/admin-settings/templates/",
    "outbox": lambda site: "/admin-settings/outbox/",
    # PLAN-014 I4: the Bank deposits page (billing): one deposit with a suggestion, one without.
    "bank-deposits": lambda site: "/admin-settings/bank-deposits/",
}


def one_off_of(site: Site) -> OneOffCharge:
    """The data set's damage charge ("Broken shelf", $250.00): the latest due without a plan."""
    found = (
        OneOffCharge.objects.filter(lease=site.world.lease, plan__isnull=True)
        .order_by("-due_date")
        .first()
    )
    assert found is not None
    return found


def settled_payment_of(site: Site) -> Payment:
    found = (
        Payment.objects.filter(rent_charge__lease=site.world.lease, status=PaymentStatus.SETTLED)
        .order_by("settled_on", "pk")
        .first()
    )
    assert found is not None
    return found


#: The lease-end wizard and the New store and New lease forms are for admins only.
AS_ADMIN = {
    "lease-end-statement",
    "new-store",
    "new-lease",
    "void-payment",
    "settings",
    "message-templates",
    "outbox",
}
#: Seen signed out, in a context of its own (the sign-in page, PLAN-012f-a D9).
SIGNED_OUT = {"login"}
#: Pages that answer something other than 200 by design (PLAN-012f-b Batch E: the not-found page).
STATUS: dict[str, int] = {"not-found": 404}


def differing_pixels(actual: Image.Image, expected: Image.Image) -> Image.Image:
    """A mask (mode "L"): 255 where a channel moved by more than the threshold."""
    bands = ImageChops.difference(actual, expected).split()
    masks = [band.point(lambda value: 255 if value > CHANNEL_THRESHOLD else 0) for band in bands]
    mask = masks[0]
    for other in masks[1:]:
        mask = ImageChops.lighter(mask, other)
    return mask


def diff_picture(expected: Image.Image, mask: Image.Image) -> Image.Image:
    faded = Image.blend(expected, Image.new("RGB", expected.size, "white"), 0.7)
    return Image.composite(Image.new("RGB", expected.size, (255, 0, 0)), faded, mask)


def compare(name: str, shot: bytes, baseline: Baseline) -> str | None:
    """``None`` when the shot matches its baseline, else why not (the images written)."""
    expected_path = baseline.folder / f"{name}.png"
    actual = Image.open(io.BytesIO(shot)).convert("RGB")
    expected = Image.open(expected_path).convert("RGB")
    if actual.size != expected.size:
        reason = f"{name}: the page is {actual.size}, its baseline {expected.size}"
        write_out(name, actual, expected, None, baseline.out)
        return reason
    mask = differing_pixels(actual, expected)
    changed = mask.histogram()[255]
    share = changed / (actual.width * actual.height)
    if share <= TOLERANCE:
        return None
    write_out(name, actual, expected, mask, baseline.out)
    return (
        f"{name}: {share:.2%} of the pixels differ ({changed} of "
        f"{actual.width * actual.height}; at most {TOLERANCE:.1%}); see {baseline.out}"
    )


def write_out(
    name: str,
    actual: Image.Image,
    expected: Image.Image,
    mask: Image.Image | None,
    out: Path,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    actual.save(out / f"{name}-actual.png")
    expected.save(out / f"{name}-expected.png")
    if mask is not None:
        diff_picture(expected, mask).save(out / f"{name}-diff.png")


@pytest.fixture
def anonymous_page(visual_browser: Browser) -> Iterator[Callable[[str], Page]]:
    """``anonymous(scheme)``: a page in a fresh context with the suite's settings and no sign-in
    (the login page); the browser's colour scheme is ``light`` or ``dark`` (the page follows
    System)."""
    opened: list[BrowserContext] = []

    def anonymous(scheme: str) -> Page:
        context: BrowserContext = visual_browser.new_context(
            viewport=DESKTOP,
            device_scale_factor=1,
            locale="en-US",
            timezone_id="America/New_York",
            reduced_motion="reduce",
            color_scheme=scheme,  # type: ignore[arg-type]
            service_workers="block",
        )
        context.clock.set_fixed_time(world.NOON)
        opened.append(context)
        return context.new_page()

    yield anonymous
    for context in opened:
        context.close()


THEMES = ("light", "dark")


def page_for(
    page_name: str,
    signed_in: Callable[[str], Page],
    anonymous: Callable[[str], Page],
    theme: str = "light",
) -> Page:
    """The page to shoot: the signed-out context for the sign-in page (its theme is System, so
    the browser's colour scheme sets it), else the role's user — the one with Dark stored for the
    dark set (PLAN-012g D1)."""
    if page_name in SIGNED_OUT:
        return anonymous(theme)
    role = "admin" if page_name in AS_ADMIN else "billing"
    return signed_in(role if theme == "light" else f"{role}-dark")


# The django_db mark makes pytest-django create the test database (it creates only the
# databases a collected test asks for); the rows are the session's (conftest.py).
@pytest.mark.django_db
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", sorted(VIEWPORTS, reverse=True))
@pytest.mark.parametrize("page_name", list(PAGES))
def test_the_page_looks_as_its_baseline(
    page_name: str,
    width: int,
    theme: str,
    visual_site: Site,
    visual_pages: Callable[[str], Page],
    anonymous_page: Callable[[str], Page],
    baseline: Baseline,
) -> None:
    # The light set keeps its names (<page>-<width>.png); the dark set adds "-dark" (PLAN-012g D1).
    name = f"{page_name}-{width}" + ("" if theme == "light" else "-dark")
    page = page_for(page_name, visual_pages, anonymous_page, theme)
    page.set_viewport_size(VIEWPORTS[width])
    open_still(page, visual_site.url + PAGES[page_name](visual_site), STATUS.get(page_name, 200))
    shot = page.screenshot(full_page=True, animations="disabled", caret="hide")
    target = baseline.folder / f"{name}.png"
    if baseline.update:
        baseline.folder.mkdir(parents=True, exist_ok=True)
        target.write_bytes(shot)
        return
    if not target.exists():
        pytest.fail(f"No baseline for {name} in {baseline.folder}: run `{COMMAND}` to write it.")
    reason = compare(name, shot, baseline)
    if reason is not None:
        pytest.fail(reason)


# --- The report shots (PLAN-012f D12): beside the references, not baselines ------------------

REPORT_VIEWPORTS: dict[int, ViewportSize] = {**VIEWPORTS, 1440: {"width": 1440, "height": 900}}
#: (page, width, theme) → the reference file name under docs/design/.
REPORT_SHOTS: dict[tuple[str, int, str], str] = {
    # PLAN-012g (the four dark references, DESIGN-SPEC §7).
    ("dashboard", 1440, "dark"): "dashboard-dark-laptop",
    ("dashboard", 390, "dark"): "dashboard-dark-phone",
    ("store-charges", 1440, "dark"): "store-workspace-dark-laptop",
    ("grid", 1440, "dark"): "monthly-rent-overview-dark-laptop",
    ("dashboard", 1440, "light"): "dashboard-laptop",
    ("store-charges", 1440, "light"): "store-workspace-laptop",
    ("grid", 1440, "light"): "monthly-rent-overview-laptop",
    ("store-charges", 390, "light"): "store-workspace-phone",
    # PLAN-012f-a (the seven references of Step 12f-a; the lease-end statement stands for the
    # transfer wizard's frame, the same frame with five steps).
    ("operator", 1440, "light"): "operator-workspace-laptop",
    ("lease-end-statement", 1440, "light"): "transfer-wizard-laptop",
    ("stores", 1440, "light"): "stores-list-laptop",
    ("calendar", 1440, "light"): "calendar-laptop",
    ("invoice-builder", 1440, "light"): "create-invoice-laptop",
    ("charge-detail", 1440, "light"): "charge-detail-laptop",
    ("login", 1440, "light"): "login-laptop",
}


@pytest.mark.django_db
@pytest.mark.parametrize(("page_name", "width", "theme"), sorted(REPORT_SHOTS))
def test_the_report_shot_is_written_when_asked(
    page_name: str,
    width: int,
    theme: str,
    visual_site: Site,
    visual_pages: Callable[[str], Page],
    anonymous_page: Callable[[str], Page],
    request: pytest.FixtureRequest,
) -> None:
    """With ``--report-dir``, the page at the reference's width goes into that folder as
    ``<reference>-built.png`` (the owner compares it with ``docs/design/<reference>.png``)."""
    folder = request.config.getoption("--report-dir")
    if not folder:
        pytest.skip("report shots are written only with --report-dir")
    page = page_for(page_name, visual_pages, anonymous_page, theme)
    page.set_viewport_size(REPORT_VIEWPORTS[width])
    open_still(page, visual_site.url + PAGES[page_name](visual_site))
    shot = page.screenshot(full_page=True, animations="disabled", caret="hide")
    target = Path(folder) / f"{REPORT_SHOTS[(page_name, width, theme)]}-built.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(shot)
    assert target.stat().st_size > 10_000


# --- The comparison itself (no browser) ----------------------------------------------------


def a_picture(size: tuple[int, int] = (100, 100), colour: str = "white") -> Image.Image:
    return Image.new("RGB", size, colour)


def png(picture: Image.Image) -> bytes:
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    return buffer.getvalue()


def baseline_of(tmp_path: Path, picture: Image.Image) -> Baseline:
    folder = tmp_path / "baseline"
    folder.mkdir()
    picture.save(folder / "page-100.png")
    return Baseline(folder=folder, update=False, out=tmp_path / "out")


def test_an_identical_shot_matches(tmp_path: Path) -> None:
    assert compare("page-100", png(a_picture()), baseline_of(tmp_path, a_picture())) is None
    assert not (tmp_path / "out").exists()


def test_a_change_within_the_channel_threshold_is_the_same_pixel(tmp_path: Path) -> None:
    near = a_picture(colour="#F0F0F0")  # 15 below white on each channel
    assert compare("page-100", png(near), baseline_of(tmp_path, a_picture())) is None


def test_up_to_half_a_percent_of_the_pixels_may_differ_and_more_fails(tmp_path: Path) -> None:
    expected = a_picture()
    within = a_picture()
    for x in range(50):  # 50 of 10,000 pixels: 0.5 %
        within.putpixel((x, 0), (0, 0, 0))
    found = baseline_of(tmp_path, expected)
    assert compare("page-100", png(within), found) is None
    beyond = within.copy()
    beyond.putpixel((0, 1), (0, 0, 0))  # 51: over
    reason = compare("page-100", png(beyond), found)
    assert reason is not None and reason.startswith("page-100: 0.51% of the pixels differ")
    for suffix in ("actual", "expected", "diff"):
        assert (tmp_path / "out" / f"page-100-{suffix}.png").exists(), suffix
    diff = Image.open(tmp_path / "out" / "page-100-diff.png").convert("RGB")
    assert diff.getpixel((0, 1)) == (255, 0, 0) and diff.getpixel((99, 99)) != (255, 0, 0)


def test_a_different_size_fails_and_names_both_sizes(tmp_path: Path) -> None:
    reason = compare("page-100", png(a_picture((100, 101))), baseline_of(tmp_path, a_picture()))
    assert reason == "page-100: the page is (100, 101), its baseline (100, 100)"
    assert (tmp_path / "out" / "page-100-actual.png").exists()
