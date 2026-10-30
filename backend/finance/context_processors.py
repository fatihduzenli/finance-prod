"""Template context for every page (docs/qa/PLAN-012d-store-workspace.md D5,
PLAN-012c-design-pass.md D2, D3).

:func:`navigation` names the main navigation's section of the page from the resolved
URL (its namespace, or its view name for the few sections that share one), and the group
it sits in (Reports or Admin), so ``finance/base.html`` marks that link with
``aria-current="page"`` and its group's heading with ``aria-current="true"``, and the phone's
bottom bar marks the item that holds it (PLAN-012f D4). It reads the request only; no query,
no setting.

:func:`company` exposes the company settings row's display values (``company.name``,
``short_name``, ``provider_name``, ``logo_url``, ``logo_is_override``) and the shell's theme
colours (light and dark) to every template; :func:`theme` exposes the user's stored theme
(PLAN-012g A2: ``light``, ``dark`` or ``system``) for ``<html data-theme>``. ``logo_url`` is the
brand's dark logo unless the row names a live PNG or JPEG document, then that document's preview
route: always a path of this app, never a URL taken from the row. ``logo_is_override`` is true
for such a document: the bar then shows the company name as text beside it (the brand logo
carries the wordmark already, so the bar shows the name once, as the image's text alternative).
No setting reaches the page. The row is read once per request, with one plain query (and the
logo's document with one more, only when the row names one), and kept on the request
(:func:`company_settings`), so the shell and the manifest of the same request share it.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.http import HttpRequest
from django.templatetags.static import static
from django.urls import reverse

from accounts.themes import preference_for
from finance import pwa
from ledger.models import LOGO_CONTENT_TYPES, CompanySettings, Document

#: The sidebar's and the phone bar's logo when the row has no override: the transparent
#: variant for dark backgrounds (PLAN-012f D3; the opaque monvera_logo_dark has a navy
#: rectangle that must not sit on the gradient).
BRAND_LOGO = "finance/brand/monvera_logo_dark_transparent.svg"
#: Where a request keeps its company settings row and its :class:`Company`.
_ROW_ATTRIBUTE = "_company_settings"
_COMPANY_ATTRIBUTE = "_company"

#: Sections named by a whole view name; checked before the namespaces.
_VIEW_SECTIONS: dict[str, str] = {
    "ledger:home": "home",
    "ledger:reports:pnl": "pnl",
    "ledger:reports:pnl_export": "pnl",
    # The user's own page, "Preferences" (PLAN-014 B2: Admin › Settings is "settings").
    "finance:settings": "preferences",
    "finance:help": "help",
    "finance:help_topic": "help",
}
#: Sections named by a namespace (the last one of the resolved route).
_NAMESPACE_SECTIONS: dict[str, str] = {
    "grid": "grid",
    "arrears": "arrears",
    "calendar": "calendar",
    "stores": "stores",
    "notes": "stores",
    "operators": "operators",
    "leases": "leases",
    "run": "run",
    "payments": "payments",
    "disputes": "disputes",
    "reports": "reports",
    "returns": "returns",
    "webhooks": "webhooks",
    "reconciliation": "reconciliation",
    "audit": "audit",
    "settings": "settings",
    "outbox": "outbox",
    "admin": "django_admin",
}
#: The navigation group of a section (PLAN-012c D3, copy list §4.2).
_SECTION_GROUPS: dict[str, str] = {
    "reports": "reports",
    "arrears": "reports",
    "pnl": "reports",
    "payments": "reports",
    "returns": "reports",
    "calendar": "reports",
    "leases": "reports",
    "disputes": "reports",
    "run": "admin",
    "audit": "admin",
    "webhooks": "admin",
    "reconciliation": "admin",
    "settings": "admin",
    "outbox": "admin",
    "django_admin": "admin",
}


#: The phone's bottom bar (PLAN-012f D4): the item of the sections it names; every other
#: section of the navigation (Settings included) is under More. The search page marks none.
_SECTION_TABS: dict[str, str] = {"home": "dashboard", "stores": "stores", "grid": "grid"}
_UNTABBED_SECTIONS = frozenset({""})


def navigation(request: HttpRequest) -> dict[str, str]:
    """``nav_section``: the section of the current page, or ``""``; ``nav_group``: the
    group of that section, or ``""``; ``nav_tab``: the bottom bar's item of that section
    (``dashboard``, ``stores``, ``grid``, ``more``), or ``""``."""
    match = getattr(request, "resolver_match", None)
    if match is None:
        return {"nav_section": "", "nav_group": "", "nav_tab": ""}
    section = _VIEW_SECTIONS.get(str(match.view_name))
    if section is None and match.namespaces:
        section = _NAMESPACE_SECTIONS.get(match.namespaces[-1])
    section = section or ""
    tab = _SECTION_TABS.get(section, "" if section in _UNTABBED_SECTIONS else "more")
    group = _SECTION_GROUPS.get(section, "")
    return {"nav_section": section, "nav_group": group, "nav_tab": tab}


@dataclass(frozen=True, slots=True)
class Company:
    """What a template may read of the company settings row."""

    name: str
    short_name: str
    provider_name: str
    logo_url: str
    #: The logo is the row's own document, not the brand logo (PLAN-012c D3).
    logo_is_override: bool = False


def logo_url(row: CompanySettings) -> str:
    """The override's preview route when it names a live PNG or JPEG document; the
    brand logo otherwise (blank, unknown, deleted or another type)."""
    if row.logo:
        document = (
            Document.objects.filter(
                blob_path=row.logo, deleted_at__isnull=True, content_type__in=LOGO_CONTENT_TYPES
            )
            .only("pk")
            .first()
        )
        if document is not None:
            return reverse("ledger:documents:preview", kwargs={"pk": document.pk})
    return static(BRAND_LOGO)


def company_settings(request: HttpRequest) -> CompanySettings:
    """The company settings row of this request: read on the first call
    (:meth:`CompanySettings.read`, one query), kept on the request for the next ones."""
    row: CompanySettings | None = getattr(request, _ROW_ATTRIBUTE, None)
    if row is None:
        row = CompanySettings.read()
        setattr(request, _ROW_ATTRIBUTE, row)
    return row


def company(request: HttpRequest) -> dict[str, object]:
    """``company`` (:class:`Company`), ``theme_color`` (the shell's navy) and
    ``theme_color_dark`` (the dark theme's canvas)."""
    shown: Company | None = getattr(request, _COMPANY_ATTRIBUTE, None)
    if shown is None:
        row = company_settings(request)
        url = logo_url(row)
        shown = Company(
            name=str(row.name),
            short_name=str(row.short_name),
            provider_name=str(row.processor_name),
            logo_url=url,
            logo_is_override=url != static(BRAND_LOGO),
        )
        setattr(request, _COMPANY_ATTRIBUTE, shown)
    return {
        "company": shown,
        "theme_color": pwa.THEME_COLOR,
        "theme_color_dark": pwa.THEME_COLOR_DARK,
    }


def theme(request: HttpRequest) -> dict[str, str]:
    """``theme``: the user's stored choice, ``"system"`` when signed out or never chosen.
    Read once per request (one query at most, shared with the dashboard's period:
    :func:`accounts.themes.preference_for`) and kept on the request."""
    return {"theme": preference_for(request).theme}
