"""The page every invoice and receipt PDF shares (docs/qa/15/PLAN-015c-invoice-pdfs.md §3, the
reference mockups ``docs/design/invoice-template-*.png``).

:class:`Frame` draws Letter pages with reportlab's ``Canvas`` (``invariant=1``,
``pageCompression=0``, as ``statements.render_pdf``): on the first page the band, a vertical
gradient from :data:`BAND_TOP` to :data:`BAND_BOTTOM` :data:`BAND_HEIGHT` points tall, with the
logo (``CompanySettings.logo_png``, else the brand PNG), the company's name and address, the
title ("INVOICE", "RECEIPT") and the number at the right and the :class:`Stamp` under it as a
rounded outline in its colour; on the pages after it a narrow band
(:data:`NARROW_BAND_HEIGHT`: the logo, the title and the number); on every page the footer,
"<company> · <support e-mail>" at the left and "Page x of y" at the right (the pages are
kept until :meth:`Frame.finish`, which knows *y*).
The body is drawn by :mod:`.invoice` and :mod:`.receipt` between :data:`MARGIN` point margins,
from :attr:`Frame.top` down to :data:`BODY_BOTTOM`.

Fonts: Inter Regular and Bold (OFL, ``finance/static/finance/fonts``), registered once; when a
TTF is missing, Helvetica. Every colour has a meaning that survives black-and-white printing:
the band prints dark with white text, the body black on white, and the stamp's word says what
its colour says. The same data gives the same bytes.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from django.conf import settings
from reportlab.lib.colors import HexColor  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.lib.utils import ImageReader  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.ttfonts import TTFError, TTFont  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from ledger.models import CompanySettings

logger = logging.getLogger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = 612.0, 792.0  # US Letter, in points
MARGIN = 72.0
LEFT, RIGHT = MARGIN, PAGE_WIDTH - MARGIN
BAND_HEIGHT = 150.0
#: The band of the pages after the first.
NARROW_BAND_HEIGHT = 64.0
#: The lowest point the body may draw at (the footer sits below it).
BODY_BOTTOM = MARGIN
FOOTER_Y = 40.0

BAND_TOP = "#0B2545"
BAND_BOTTOM = "#0E4A5C"
INK = "#0B1F33"
MUTED = "#6B7280"
RULE = "#E5E7EB"
PANEL = "#F3F5F8"
PAY_PANEL = "#E3F4F1"
TEAL = "#0F8A7A"
BAND_TEXT = "#FFFFFF"
BAND_MUTED = "#C9D6E3"
BAND_LABEL = "#8FD3C9"
BAND_RULE = "#3D5F7A"

STATIC = Path(settings.BASE_DIR) / "finance" / "static" / "finance"
FONT_DIR = STATIC / "fonts"
#: The brand logo the band shows when the company has none (940 × 320, transparent).
BRAND_LOGO = STATIC / "brand" / "monvera_logo_dark_transparent.png"
LOGO_HEIGHT = 40.0
LOGO_MAX_WIDTH = 150.0
ADDRESS_LINES = 2

REGULAR, BOLD = "Inter", "Inter-Bold"
FALLBACK_REGULAR, FALLBACK_BOLD = "Helvetica", "Helvetica-Bold"
_FONT_FILES = {REGULAR: "Inter-Regular.ttf", BOLD: "Inter-Bold.ttf"}
_fonts: tuple[str, str] | None = None

ELLIPSIS = "…"


class Stamp(Enum):
    """The status stamp under the number: (word, colour)."""

    UNPAID = ("UNPAID", "#D14343")
    PARTLY_PAID = ("PARTLY PAID", "#C8741A")
    PAID = ("PAID", "#1E8E5A")
    VOID = ("VOID", "#6B7280")

    @property
    def word(self) -> str:
        return self.value[0]

    @property
    def colour(self) -> str:
        return self.value[1]


@dataclass(frozen=True, slots=True)
class CompanyFacts:
    """What the band and the footer print of the company."""

    name: str
    address: str
    support_email: str
    logo_png: bytes


def company_facts() -> CompanyFacts:
    """The company settings row's name, address (the lines that are set, joined ", "),
    support e-mail and logo copy (``b""``: the brand logo)."""
    row = CompanySettings.current()
    place = " ".join(part for part in (row.state, row.postal_code) if part)
    parts = (row.address_line1, row.address_line2, row.city, place)
    return CompanyFacts(
        name=str(row.name),
        address=", ".join(" ".join(str(part).split()) for part in parts if part),
        support_email=str(row.support_email or ""),
        logo_png=bytes(row.logo_png or b""),
    )


def fonts() -> tuple[str, str]:
    """The (regular, bold) font names: Inter, registered once, else Helvetica."""
    global _fonts
    if _fonts is None:
        _fonts = _register()
    return _fonts


def _register() -> tuple[str, str]:

    try:
        for name, filename in _FONT_FILES.items():
            pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / filename)))
    except (OSError, TTFError):
        logger.warning("The Inter TTF files are missing; the PDFs use Helvetica.")
        return FALLBACK_REGULAR, FALLBACK_BOLD
    return REGULAR, BOLD


def text_width(text: str, font: str, size: float) -> float:
    return float(pdfmetrics.stringWidth(text, font, size))


def fit(text: str, font: str, size: float, width: float) -> str:
    """``text`` cut to ``width`` points with an ellipsis when it is wider."""
    if text_width(text, font, size) <= width:
        return text
    while text and text_width(text + ELLIPSIS, font, size) > width:
        text = text[:-1]
    return text.rstrip() + ELLIPSIS


def wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """``text`` broken at spaces into lines of at most ``width`` points; a word wider than
    a line is cut with an ellipsis."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if text_width(candidate, font, size) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = fit(word, font, size, width)
    if current:
        lines.append(current)
    return lines


def _image(data: bytes) -> Any:
    return ImageReader(io.BytesIO(data))


def _logo(company: CompanyFacts) -> Any:
    """The company's logo copy, else the brand PNG; ``None`` when neither can be read."""
    for source in (company.logo_png, None):
        try:
            data = source if source is not None else BRAND_LOGO.read_bytes()
            if not data:
                continue
            image = _image(data)
            image.getSize()
        except (OSError, ValueError, SyntaxError):
            logger.warning("A logo could not be read for the PDF band.")
            continue
        return image
    return None


class Frame:
    """The pages of one document. Draw the body with :attr:`canvas` between :data:`LEFT`
    and :data:`RIGHT`, from :attr:`top` down to :data:`BODY_BOTTOM`; call :meth:`new_page`
    for the next page and :meth:`finish` for the bytes."""

    def __init__(
        self,
        company: CompanyFacts,
        title: str,
        number: str,
        stamp: Stamp | None,
    ) -> None:

        self.company = company
        self.title = title
        self.number = number
        self.stamp = stamp
        self.regular, self.bold = fonts()
        self._buffer = io.BytesIO()
        self.canvas = Canvas(self._buffer, pagesize=letter, invariant=1, pageCompression=0)
        self.canvas.setTitle(f"{title.title()} {number}")
        self.canvas.setAuthor(company.name)
        self._logo = _logo(company)
        self._pages: list[dict[str, Any]] = []
        self.top = 0.0
        self._start_page()

    # --- Pages ---------------------------------------------------------------------------

    def _start_page(self) -> None:
        self._band()
        self.top = PAGE_HEIGHT - BAND_HEIGHT - 30

    def new_page(self) -> None:
        """Keep this page and start the next, under the narrow band (the logo, the title and
        the number)."""
        self._pages.append(dict(self.canvas.__dict__))
        self.canvas._startPage()
        self._narrow_band()
        self.top = PAGE_HEIGHT - NARROW_BAND_HEIGHT - 24

    def finish(self) -> bytes:
        """The document's bytes: each page gets its footer with "Page x of y"."""
        self._pages.append(dict(self.canvas.__dict__))
        total = len(self._pages)
        for number, state in enumerate(self._pages, start=1):
            self.canvas.__dict__.update(state)
            self._footer(number, total)
            self.canvas.showPage()
        self.canvas.save()
        return self._buffer.getvalue()

    # --- The band and the footer ---------------------------------------------------------

    def _gradient(self, height: float) -> None:
        """The band's gradient across the top ``height`` points of the page."""
        c = self.canvas
        bottom = PAGE_HEIGHT - height
        c.saveState()
        path = c.beginPath()
        path.rect(0, bottom, PAGE_WIDTH, height)
        c.clipPath(path, stroke=0, fill=0)
        c.linearGradient(
            0,
            PAGE_HEIGHT,
            0,
            bottom,
            (HexColor(BAND_TOP), HexColor(BAND_BOTTOM)),
            extend=True,
        )
        c.restoreState()

    def _draw_logo(self, x: float, middle: float, height: float) -> float:
        """The logo at ``x``, centred on ``middle``, ``height`` points tall at most; the x
        after it (``x`` without a logo)."""
        if self._logo is None:
            return x
        width, tall = self._logo.getSize()
        shown_w = min(LOGO_MAX_WIDTH * height / LOGO_HEIGHT, height * width / max(tall, 1))
        shown_h = shown_w * tall / max(width, 1)
        self.canvas.drawImage(self._logo, x, middle - shown_h / 2, shown_w, shown_h, mask="auto")
        return x + float(shown_w)

    def _narrow_band(self) -> None:
        """The band of the pages after the first: the logo, the title and the number."""
        self._gradient(NARROW_BAND_HEIGHT)
        middle = PAGE_HEIGHT - NARROW_BAND_HEIGHT / 2
        self._draw_logo(LEFT, middle, 24)
        c = self.canvas
        c.setFillColor(HexColor(BAND_TEXT))
        c.setFont(self.bold, 13)
        c.drawRightString(RIGHT, middle - 5, fit(self.number, self.bold, 13, RIGHT / 3))
        c.setFillColor(HexColor(BAND_LABEL))
        shift = text_width(fit(self.number, self.bold, 13, RIGHT / 3), self.bold, 13) + 10
        self.spaced(RIGHT - shift, middle - 4, self.title.upper(), self.bold, 8.5, 1.5, "right")

    def _band(self) -> None:
        c = self.canvas
        self._gradient(BAND_HEIGHT)
        middle = PAGE_HEIGHT - BAND_HEIGHT / 2
        x = LEFT
        if self._logo is not None:
            x = self._draw_logo(x, middle, LOGO_HEIGHT) + 14
            c.setStrokeColor(HexColor(BAND_RULE))
            c.setLineWidth(0.75)
            c.line(x, middle - 18, x, middle + 18)
            x += 14
        # The number's width (at most a third of the page) and a gap; the company's lines
        # have the rest, the address on at most two lines.
        right_block = min(text_width(self.number, self.bold, 20), RIGHT / 3) + 24
        room = RIGHT - right_block - x
        address = wrap(self.company.address, self.regular, 9.5, room)[:ADDRESS_LINES]
        y = middle + 4 + 6 * len(address)
        c.setFillColor(HexColor(BAND_TEXT))
        c.setFont(self.bold, 11)
        c.drawString(x, y, fit(self.company.name, self.bold, 11, room))
        c.setFillColor(HexColor(BAND_MUTED))
        c.setFont(self.regular, 9.5)
        for line in address:
            y -= 14
            c.drawString(x, y, line)
        # The title, the number and the stamp, right-aligned.
        c.setFillColor(HexColor(BAND_LABEL))
        self.spaced(RIGHT, middle + 30, self.title.upper(), self.bold, 8.5, 1.5, align="right")
        c.setFillColor(HexColor(BAND_TEXT))
        c.setFont(self.bold, 20)
        c.drawRightString(RIGHT, middle + 6, fit(self.number, self.bold, 20, RIGHT / 3))
        if self.stamp is not None:
            self._stamp(self.stamp, RIGHT, middle - 26)

    def _stamp(self, stamp: Stamp, right: float, baseline: float) -> None:
        c = self.canvas
        size, spacing = 8.5, 1.2
        word_width = text_width(stamp.word, self.bold, size) + spacing * (len(stamp.word) - 1)
        width, height = word_width + 22, 17.0
        c.setStrokeColor(HexColor(stamp.colour))
        c.setLineWidth(1)
        c.roundRect(right - width, baseline - 5.5, width, height, height / 2, stroke=1, fill=0)
        c.setFillColor(HexColor(stamp.colour))
        self.spaced(right - 11, baseline, stamp.word, self.bold, size, spacing, align="right")

    def _footer(self, number: int, total: int) -> None:
        c = self.canvas
        c.setFillColor(HexColor(MUTED))
        c.setFont(self.regular, 8)
        left = " · ".join(part for part in (self.company.name, self.company.support_email) if part)
        c.drawString(LEFT, FOOTER_Y, fit(left, self.regular, 8, RIGHT - LEFT - 80))
        c.drawRightString(RIGHT, FOOTER_Y, f"Page {number} of {total}")

    # --- Drawing helpers the bodies share -------------------------------------------------

    def spaced(
        self,
        x: float,
        y: float,
        text: str,
        font: str,
        size: float,
        spacing: float,
        align: str = "left",
    ) -> None:
        """``text`` with ``spacing`` points between letters (the small-caps labels)."""
        width = text_width(text, font, size) + spacing * max(len(text) - 1, 0)
        start = x - width if align == "right" else x
        line = self.canvas.beginText(start, y)
        line.setFont(font, size)
        line.setCharSpace(spacing)
        line.textOut(text)
        # The spacing is part of the graphics state: kept to this text alone.
        self.canvas.saveState()
        self.canvas.drawText(line)
        self.canvas.restoreState()

    def label(self, x: float, y: float, text: str, align: str = "left") -> None:
        """A muted small-caps label ("BILL TO", "ITEM")."""
        self.canvas.setFillColor(HexColor(MUTED))
        self.spaced(x, y, text.upper(), self.bold, 7.5, 1.0, align=align)

    def text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        bold: bool = False,
        size: float = 10,
        colour: str = INK,
        align: str = "left",
        width: float | None = None,
    ) -> None:
        font = self.bold if bold else self.regular
        if width is not None:
            text = fit(text, font, size, width)
        c = self.canvas
        c.setFillColor(HexColor(colour))
        c.setFont(font, size)
        if align == "right":
            c.drawRightString(x, y, text)
        else:
            c.drawString(x, y, text)

    def rule(self, y: float, left: float = LEFT, right: float = RIGHT) -> None:
        self.canvas.setStrokeColor(HexColor(RULE))
        self.canvas.setLineWidth(0.75)
        self.canvas.line(left, y, right, y)

    def panel(self, x: float, y: float, width: float, height: float, colour: str) -> None:
        """A filled rounded rectangle whose bottom-left corner is (``x``, ``y``)."""
        self.canvas.setFillColor(HexColor(colour))
        self.canvas.roundRect(x, y, width, height, 8, stroke=0, fill=1)

    def rich_line(
        self, x: float, y: float, parts: Sequence[tuple[str, bool]], size: float = 9.5
    ) -> None:
        """One line of (text, bold) runs, in :data:`INK`."""
        c = self.canvas
        c.setFillColor(HexColor(INK))
        for text, bold in parts:
            font = self.bold if bold else self.regular
            c.setFont(font, size)
            c.drawString(x, y, text)
            x += text_width(text, font, size)
