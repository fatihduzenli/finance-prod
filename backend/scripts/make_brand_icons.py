"""Generate the app's home-screen icons and favicons (owner, 2026-11-05).

Run from the repository root (the files it writes are committed; run it again only when the
logo or the composition changes)::

    uv run python backend/scripts/make_brand_icons.py

The source is the M mark of the logo's SVG,
``finance/static/finance/brand/monvera_logo_dark_transparent.svg``, rendered by Chromium
through Playwright's sync API (a dev dependency; ``uv run playwright install chromium``): the
SVG's viewBox is narrowed to the mark's bounding box (the wordmark falls outside it) and the
SVG element is captured at 1,024 px wide on a transparent background, so even the 512 px icons
are downscaled, never upscaled. The committed files were produced this way (2026-11-05).
Fallback, when Playwright or its browser is absent: the mark is cropped from
``docs/design/monvera_logo_dark_transparent.png`` (940 x 320, RGBA) at that source's full
resolution to its alpha bounding box inside the left part of the logo; the mark there is only
212 px wide, so the 512 px icons come out softer. The script prints which path it took.

Each icon is a square filled with
the vertical gradient #0B2545 (top) to #0E4A5C (bottom), the mark centred on it, its longer
side 62 % of the icon's side (50 % for the maskable icon, inside Android's safe zone). The
PNGs are RGB: no transparency anywhere (iOS paints a transparent touch icon black and adds
its own corners).

``monvera_icon.svg`` is the same composition as vector: the gradient square and the mark's
own path from ``finance/static/finance/brand/monvera_logo_dark_transparent.svg``. It is
written to the served brand folder and to the repository's original, ``static/brand/``
(tests/test_design_shell.py keeps the two the same).
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image

REPOSITORY = Path(__file__).resolve().parents[2]
BRAND = REPOSITORY / "backend" / "finance" / "static" / "finance" / "brand"
SVG_SOURCE = BRAND / "monvera_logo_dark_transparent.svg"
PNG_SOURCE = REPOSITORY / "docs" / "design" / "monvera_logo_dark_transparent.png"
ORIGINALS = REPOSITORY / "static" / "brand"

TOP = (0x0B, 0x25, 0x45)
BOTTOM = (0x0E, 0x4A, 0x5C)
#: The logo's wordmark starts at x = 320 in the 940 px source (the SVG's x = 160, doubled);
#: the mark lies left of this.
MARK_REGION = (0, 0, 300, 320)
#: The mark's bounding box in the logo SVG's units, the stroke's round ends included
#: (x 27-133, y 35-137), and the width it is rendered at.
MARK_VIEWBOX = (27, 35, 106, 102)
RENDER_WIDTH = 1024
PLAIN = 0.62
MASKABLE = 0.50

#: (file name, side, the mark's share of the side).
PNGS: tuple[tuple[str, int, float], ...] = (
    ("monvera_touch_180.png", 180, PLAIN),
    ("monvera_icon_192.png", 192, PLAIN),
    ("monvera_icon_512.png", 512, PLAIN),
    ("monvera_icon_512_maskable.png", 512, MASKABLE),
    ("favicon-32.png", 32, PLAIN),
    ("favicon-16.png", 16, PLAIN),
)

#: The mark of the logo's SVG, its bounding box (stroke included) x 27-133, y 35-137 of the
#: 160-unit square: scaled to 62 % of the side and centred.
SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160" width="512" height="512">\n'
    '<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#0B2545"/><stop offset="1" stop-color="#0E4A5C"/>'
    "</linearGradient>\n"
    '<linearGradient id="mv" x1="0" y1="1" x2="1" y2="0">'
    '<stop offset="0" stop-color="#13A89E"/><stop offset="1" stop-color="#2EC4A6"/>'
    "</linearGradient></defs>\n"
    '<rect width="160" height="160" fill="url(#bg)"/>\n'
    '<g transform="translate(80 80) scale({scale}) translate(-80 -86)">'
    '<path d="M36 128 L36 44 L80 90 L124 44 L124 128" fill="none" stroke="url(#mv)" '
    'stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>\n'
    '<circle cx="80" cy="118" r="9" fill="#2EC4A6"/></g>\n'
    "</svg>\n"
)
SVG_MARK_SIDE = 106


def _trimmed(image: Image.Image, source: Path) -> Image.Image:
    box = image.getchannel("A").getbbox()
    if box is None:
        raise SystemExit(f"no mark found in {source}")
    return image.crop(box)


def mark_from_svg() -> Image.Image | None:
    """The M mark rendered from the logo's SVG at :data:`RENDER_WIDTH`, RGBA; ``None`` when
    Playwright or its Chromium is not installed."""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        return None
    x, y, width, height = MARK_VIEWBOX
    pixels_high = round(RENDER_WIDTH * height / width)
    svg = SVG_SOURCE.read_text(encoding="utf-8")
    svg = re.sub(r'viewBox="[^"]*"', f'viewBox="{x} {y} {width} {height}"', svg, count=1)
    svg = re.sub(
        r'width="\d+" height="\d+"', f'width="{RENDER_WIDTH}" height="{pixels_high}"', svg, count=1
    )
    page_html = (
        "<!doctype html><html><head><style>html,body{margin:0;background:transparent}"
        "svg{display:block;overflow:hidden}</style></head><body>" + svg + "</body></html>"
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": RENDER_WIDTH, "height": pixels_high},
                    device_scale_factor=1,
                )
                page.set_content(page_html)
                shot = page.locator("svg").first.screenshot(omit_background=True)
            finally:
                browser.close()
    except Error:
        return None
    return _trimmed(Image.open(io.BytesIO(shot)).convert("RGBA"), SVG_SOURCE)


def mark_from_png() -> Image.Image:
    """The M mark, cropped from the logo's PNG at full resolution, RGBA (the fallback)."""
    logo = Image.open(PNG_SOURCE).convert("RGBA")
    return _trimmed(logo.crop(MARK_REGION), PNG_SOURCE)


def mark() -> Image.Image:
    """The M mark: from the SVG when Chromium can render it, else from the PNG."""
    rendered = mark_from_svg()
    if rendered is not None:
        print(f"mark: rendered from {SVG_SOURCE.name} by Chromium, {rendered.size}")
        return rendered
    cropped = mark_from_png()
    print(f"mark: Playwright unavailable; cropped from {PNG_SOURCE.name}, {cropped.size}")
    return cropped


def gradient(side: int) -> Image.Image:
    """A ``side`` square, the vertical gradient from :data:`TOP` to :data:`BOTTOM`, RGB."""
    column = Image.new("RGB", (1, side))
    for y in range(side):
        t = y / (side - 1)
        column.putpixel(
            (0, y), tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM, strict=True))
        )
    return column.resize((side, side), Image.Resampling.NEAREST)


def icon(source: Image.Image, side: int, share: float) -> Image.Image:
    """The mark centred on the gradient square, its longer side ``share`` of ``side``."""
    scale = share * side / max(source.size)
    width = max(1, round(source.width * scale))
    height = max(1, round(source.height * scale))
    sized = source.resize((width, height), Image.Resampling.LANCZOS)
    canvas = gradient(side).convert("RGBA")
    canvas.alpha_composite(sized, ((side - width) // 2, (side - height) // 2))
    return canvas.convert("RGB")


def main() -> None:
    source = mark()
    for name, side, share in PNGS:
        image = icon(source, side, share)
        image.save(BRAND / name, optimize=True)
        if name == "monvera_icon_512.png":
            image.save(ORIGINALS / name, optimize=True)
    svg = SVG.replace("{scale}", f"{PLAIN * 160 / SVG_MARK_SIDE:.4f}")
    for folder in (BRAND, ORIGINALS):
        (folder / "monvera_icon.svg").write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    main()
