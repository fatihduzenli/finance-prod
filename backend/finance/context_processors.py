"""Template context for every page (docs/qa/PLAN-012d-store-workspace.md D5).

:func:`navigation` names the main navigation's section of the page from the resolved
URL (its namespace, or its view name for the few sections that share one), so
``finance/base.html`` marks that link with ``aria-current="page"``. It reads the
request only; no query, no setting.
"""

from __future__ import annotations

from django.http import HttpRequest

#: Sections named by a whole view name; checked before the namespaces.
_VIEW_SECTIONS: dict[str, str] = {
    "ledger:home": "home",
    "ledger:reports:pnl": "pnl",
    "ledger:reports:pnl_export": "pnl",
    "finance:settings": "settings",
}
#: Sections named by a namespace (the last one of the resolved route).
_NAMESPACE_SECTIONS: dict[str, str] = {
    "grid": "grid",
    "arrears": "arrears",
    "calendar": "calendar",
    "stores": "stores",
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
    "admin": "django_admin",
}


def navigation(request: HttpRequest) -> dict[str, str]:
    """``nav_section``: the section of the current page, or ``""``."""
    match = getattr(request, "resolver_match", None)
    if match is None:
        return {"nav_section": ""}
    section = _VIEW_SECTIONS.get(str(match.view_name))
    if section is None and match.namespaces:
        section = _NAMESPACE_SECTIONS.get(match.namespaces[-1])
    return {"nav_section": section or ""}
