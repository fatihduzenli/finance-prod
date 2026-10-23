"""The visual regression pages (docs/qa/PLAN-012c-design-pass.md D10, §3.7): ten pages, the
store workspace on each of its tabs, at 1280 x 800 and 390 x 844, each a full-page PNG
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
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest
from django.urls import reverse
from PIL import Image, ImageChops
from playwright.sync_api import Page, ViewportSize

from tests.visual.conftest import COMMAND, Baseline, Site, open_still

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
}

#: The lease-end wizard is for admins only.
AS_ADMIN = {"lease-end-statement"}


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


# The django_db mark makes pytest-django create the test database (it creates only the
# databases a collected test asks for); the rows are the session's (conftest.py).
@pytest.mark.django_db
@pytest.mark.parametrize("width", sorted(VIEWPORTS, reverse=True))
@pytest.mark.parametrize("page_name", list(PAGES))
def test_the_page_looks_as_its_baseline(
    page_name: str,
    width: int,
    visual_site: Site,
    visual_pages: Callable[[str], Page],
    baseline: Baseline,
) -> None:
    name = f"{page_name}-{width}"
    page = visual_pages("admin" if page_name in AS_ADMIN else "billing")
    page.set_viewport_size(VIEWPORTS[width])
    open_still(page, visual_site.url + PAGES[page_name](visual_site))
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
#: (page, width) → the reference file name under docs/design/.
REPORT_SHOTS: dict[tuple[str, int], str] = {
    ("dashboard", 1440): "dashboard-laptop",
    ("store-charges", 1440): "store-workspace-laptop",
    ("grid", 1440): "monthly-rent-overview-laptop",
    ("store-charges", 390): "store-workspace-phone",
}


@pytest.mark.django_db
@pytest.mark.parametrize(("page_name", "width"), sorted(REPORT_SHOTS))
def test_the_report_shot_is_written_when_asked(
    page_name: str,
    width: int,
    visual_site: Site,
    visual_pages: Callable[[str], Page],
    request: pytest.FixtureRequest,
) -> None:
    """With ``--report-dir``, the page at the reference's width goes into that folder as
    ``<reference>-built.png`` (the owner compares it with ``docs/design/<reference>.png``)."""
    folder = request.config.getoption("--report-dir")
    if not folder:
        pytest.skip("report shots are written only with --report-dir")
    page = visual_pages("admin" if page_name in AS_ADMIN else "billing")
    page.set_viewport_size(REPORT_VIEWPORTS[width])
    open_still(page, visual_site.url + PAGES[page_name](visual_site))
    shot = page.screenshot(full_page=True, animations="disabled", caret="hide")
    target = Path(folder) / f"{REPORT_SHOTS[(page_name, width)]}-built.png"
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
