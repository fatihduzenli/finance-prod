"""Route-to-role registry. Fails closed (docs/qa/PLAN-003-auth-roles.md §4).

Every route is identified by its resolved view name, namespace included
(``admin:auth_user_add``). A request that is not GET, HEAD or OPTIONS is allowed only
when its route has a rule here, the method is listed in that rule, and the user's role
meets the rule's minimum. Anything else is refused with 403, including a route that
exists but was never registered.

Read-only routes need no rule for GET; they are listed in ``READ_ONLY_ROUTE_NAMES`` so
the enumeration test (backend/tests/test_route_registry.py) can prove that every route
in the URL map has been classified. A new route that is not classified fails that test.

Routes still to come: refund, void and settings get ``Role.ADMIN``; invoices, send and
record-payment get ``Role.BILLING``. Operators (PLAN-006) are classified below.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.http import HttpRequest

from accounts.roles import Role, meets

SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})
POST: frozenset[str] = frozenset({"POST"})


@dataclass(frozen=True)
class RouteRule:
    """Who may send an unsafe request to a route.

    ``min_role=None`` means any authenticated user, whatever their role (logout, MFA
    enrolment, sign out everywhere): these must work even for a user without a group.
    """

    min_role: Role | None
    methods: frozenset[str]


#: Reachable without a session. Exactly the login flow and the health probe (§3).
PUBLIC_ROUTE_NAMES: frozenset[str] = frozenset({"two_factor:login", "healthz"})

#: Public routes that also accept an unsafe method: the login form posts to itself.
PUBLIC_UNSAFE_ROUTE_NAMES: frozenset[str] = frozenset({"two_factor:login"})


def _static_prefix() -> str:
    url = settings.STATIC_URL or "static/"
    return url if url.startswith("/") else f"/{url}"


#: Path prefixes reachable without a session: static assets only.
PUBLIC_PATH_PREFIXES: tuple[str, ...] = (_static_prefix(),)

#: Views an authenticated user who has not passed the second factor may use: enrolment
#: (only while they have no device, see accounts.middleware) and logout.
MFA_SETUP_ROUTE_NAMES: frozenset[str] = frozenset(
    {
        "two_factor:setup",
        "two_factor:qr",
        "two_factor:setup_complete",
        "two_factor:backup_tokens",
    }
)
MFA_EXEMPT_ROUTE_NAMES: frozenset[str] = frozenset({"logout", "admin:logout"})

#: Exact-name rules for unsafe methods.
ROUTE_ROLES: dict[str, RouteRule] = {
    # Two-factor enrolment and backup tokens: the user's own devices.
    "two_factor:setup": RouteRule(min_role=None, methods=POST),
    "two_factor:backup_tokens": RouteRule(min_role=None, methods=POST),
    # Signing out. Django's LogoutView refuses GET itself (405).
    "logout": RouteRule(min_role=None, methods=POST),
    "admin:logout": RouteRule(min_role=None, methods=POST),
    "accounts:sign_out_everywhere": RouteRule(min_role=None, methods=POST),
    # Changing one's own password.
    "admin:password_change": RouteRule(min_role=Role.VIEWER, methods=POST),
    # Operators (docs/qa/PLAN-006-operators.md §4.1, D10). There is no delete route.
    "ledger:operators:create": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:operators:edit": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:operators:retry_sync": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:operators:link": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:operators:archive": RouteRule(min_role=Role.ADMIN, methods=POST),
}

#: Prefix rules for Django admin model routes (``admin:<app>_<model>_<action>``). Every
#: action of a listed model (add, change, delete, changelist bulk actions, extra views
#: such as auth_user_password_change) needs at least this role for an unsafe method.
ADMIN_MODEL_PREFIX_RULES: dict[str, RouteRule] = {
    "admin:auth_user_": RouteRule(min_role=Role.ADMIN, methods=POST),
    "admin:auth_group_": RouteRule(min_role=Role.ADMIN, methods=POST),
    "admin:accounts_usersession_": RouteRule(min_role=Role.ADMIN, methods=POST),
    "admin:otp_totp_": RouteRule(min_role=Role.ADMIN, methods=POST),
    "admin:otp_static_": RouteRule(min_role=Role.ADMIN, methods=POST),
    # Registered read-only (PLAN-006 D9); a POST is refused below admin as well.
    "admin:ledger_operator_": RouteRule(min_role=Role.ADMIN, methods=POST),
}

#: Routes that take no unsafe method at all. GET is open to any verified user (the view
#: itself still checks is_staff and model permissions); anything else is refused.
READ_ONLY_ROUTE_NAMES: frozenset[str] = frozenset(
    {
        "admin:index",
        "admin:login",
        "admin:app_list",
        "admin:autocomplete",
        "admin:jsi18n",
        "admin:view_on_site",
        "admin:password_change_done",
        "two_factor:qr",
        "two_factor:setup_complete",
        "two_factor:profile",
        # Disabling one's own second factor would defeat mandatory MFA: the page may be
        # viewed, its form is refused.
        "two_factor:disable",
        # Operator screens that only read (PLAN-006 §4.1).
        "ledger:operators:list",
        "ledger:operators:detail",
    }
)


def view_name(request: HttpRequest) -> str | None:
    """The namespaced name of the route the request resolved to, if any."""
    match = getattr(request, "resolver_match", None)
    if match is None or not match.url_name:
        return None
    return str(match.view_name)


def rule_for(name: str | None) -> RouteRule | None:
    """The rule for an unsafe request to ``name``, or None when there is none."""
    if name is None:
        return None
    if name in ROUTE_ROLES:
        return ROUTE_ROLES[name]
    for prefix, rule in ADMIN_MODEL_PREFIX_RULES.items():
        if name.startswith(prefix):
            return rule
    return None


def is_allowed(rule: RouteRule | None, method: str, role: Role | None) -> bool:
    """Whether a user with ``role`` may send ``method`` to a route with ``rule``.

    Only the method Django parsed from the request line is considered. Override headers
    (``X-HTTP-Method-Override``) and ``_method`` form fields are never read.
    """
    if method in SAFE_METHODS:
        return True
    if rule is None or method not in rule.methods:
        return False
    return rule.min_role is None or meets(role, rule.min_role)


def is_public_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)
