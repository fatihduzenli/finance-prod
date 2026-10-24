"""Adversarial guard for PLAN-012f-a B4 (dates on screen), by the QA owner (plan §6). No ISO
day ("2026-11-01") or month ("2026-11") renders as page text on any main page for any role:
the visual data set's pages, the lists, the reports, the calendar, the daily run, the help pages.
Attributes, ``<time datetime>``, inputs, options' values, scripts and styles are not page text;
the audit page's and the webhook pages' JSON cells keep ISO by the owner's rule and are left
out; stored descriptions written by earlier steps ("Re-debit of Rent 2026-10") keep their text
and are allowed by pattern. A second case reads the templates: no ``|date:"Y-m-d"``,
``|date:"Y-m"`` or ``|date:"F Y"`` outside an attribute value."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from django.urls import reverse

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from payments.fake import FakeProvider
from tests.test_operator_views import client_for
from tests.visual import world as visual_world
from tests.visual.test_pages import AS_ADMIN, PAGES

pytestmark = pytest.mark.django_db

ISO = re.compile(r"(?<![\w-])20\d{2}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?(?![\w-])")
#: Stored text from earlier steps that keeps ISO (PLAN-012f-a §3.2 B4, the owner's rule).
STORED = re.compile(
    r"Re-debit of Rent 20\d{2}-\d{2}"
    r"|returned debit of Rent 20\d{2}-\d{2}"
    r"|Installment \d+ of \d+: Rent 20\d{2}-\d{2}"
    r"|re-debit on 20\d{2}-\d{2}-\d{2}"
    r"|plan made 20\d{2}-\d{2}-\d{2}"
)
BACKEND = Path(__file__).resolve().parents[1]
TEMPLATES = sorted(BACKEND.glob("*/templates/**/*.html"))
#: Pages outside the visual suite that render dates (path, admin-only).
MORE_PAGES = (
    ("/operators/", False),
    ("/leases/", False),
    ("/payments/", False),
    ("/returns/", False),
    ("/disputes/", False),
    ("/arrears/", False),
    ("/calendar/", False),
    ("/reports/pnl/", False),
    ("/reports/deposits/", False),
    ("/reports/revenue/", False),
    ("/reports/payments-by-method/", False),
    ("/reports/aging/", False),
    ("/search/?q=Istanbul", False),
    ("/help/", False),
    ("/run/", True),
    ("/reconciliation/", True),
)


def page_text(html: str) -> str:
    """The text a reader sees: no scripts, styles, tags (so no attributes), no ``<time>``."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<time\b[^>]*>.*?</time>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = STORED.sub(" ", text)
    return text


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> visual_world.World:
    clock = visual_world.FrozenClock()
    monkeypatch.setattr("django.utils.timezone.now", clock)
    monkeypatch.setattr("payments.get_provider", FakeProvider)
    monkeypatch.setattr(documents_storage, "get_storage", MemoryDocumentStorage)
    world = visual_world.build()
    clock.stop()
    return world


class _Site:
    def __init__(self, world: visual_world.World) -> None:
        self.world = world


def test_no_iso_date_renders_as_text_on_any_main_page_for_any_role(
    built: visual_world.World,
) -> None:
    site: Any = _Site(built)
    pages: list[tuple[str, bool]] = [
        (path_of(site), name in AS_ADMIN) for name, path_of in PAGES.items()
    ]
    pages += list(MORE_PAGES)
    for charge in built.lease.rent_charges.all()[:2]:
        pages.append((reverse("ledger:charges:detail", kwargs={"pk": charge.pk}), False))
        pages.append((reverse("ledger:charges:split", kwargs={"pk": charge.pk}), False))
        pages.append((reverse("ledger:charges:record_payment", kwargs={"pk": charge.pk}), False))
    findings: list[str] = []
    for role in (Role.VIEWER, Role.BILLING, Role.ADMIN):
        client, _ = client_for(role)
        for path, admin_only in pages:
            if admin_only and role != Role.ADMIN:
                continue
            response = client.get(path)
            if response.status_code != 200:
                continue  # a role that may not open the page is another test's business
            text = page_text(response.content.decode())
            for match in ISO.finditer(text):
                start = max(0, match.start() - 40)
                findings.append(f"{role.value} {path}: …{text[start : match.end() + 20]!r}")
    assert findings == [], "ISO dates on screen:\n" + "\n".join(findings[:40])


ATTRIBUTE_DATE = re.compile(r'=\s*"[^"]*\|date:"(?:Y-m-d|Y-m|F Y)"')
TEXT_DATE = re.compile(r'\|date:"(?:Y-m-d|Y-m|F Y|Y-m-d H:i|Y-m-d H:i:s|F|l, F j, Y)"')


def test_no_template_formats_a_page_date_as_iso_outside_an_attribute() -> None:
    offenders: list[str] = []
    for path in TEMPLATES:
        relative = path.relative_to(BACKEND).as_posix()
        if "/templates/finance/sw.js" in relative:
            continue
        text = path.read_text(encoding="utf-8")
        stripped = re.sub(r'="[^"]*"', '=""', text)  # attribute values out
        stripped = re.sub(r"\{%\s*now\s+\"Y-m-d\"[^%]*%\}", "", stripped)
        # A grouping key is never printed ({% ifchanged line.on|date:"Y-m" %}).
        stripped = re.sub(r"\{%\s*ifchanged\b[^%]*%\}", "", stripped)
        for match in TEXT_DATE.finditer(stripped):
            line = stripped.count("\n", 0, match.start()) + 1
            offenders.append(f"{relative}:{line}")
    assert offenders == [], "ISO date filters on page text:\n" + "\n".join(offenders)
