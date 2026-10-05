"""Route-to-role registry. Fails closed (docs/qa/PLAN-003-auth-roles.md §4).

Every route is identified by its resolved view name, namespace included
(``admin:auth_user_add``). A request that is not GET, HEAD or OPTIONS is allowed only
when its route has a rule here, the method is listed in that rule, and the user's role
meets the rule's minimum. Anything else is refused with 403, including a route that
exists but was never registered.

Read-only routes need no rule for GET; they are listed in ``READ_ONLY_ROUTE_NAMES`` so
the enumeration test (backend/tests/test_route_registry.py) can prove that every route
in the URL map has been classified. A new route that is not classified fails that test.

Routes still to come: settings get ``Role.ADMIN``; invoices and send get
``Role.BILLING``. Operators (PLAN-006), leases, the rent run and rent charges
(PLAN-007), stores (PLAN-007b), webhook events and the returns queue (PLAN-008), and the
payments ledger, its export, the payment page, the marking retry, refund and void
(PLAN-009) are classified below.
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


#: Reachable without a session. Exactly the login flow, the health probe (§3) and the
#: webhook receiver, which checks its URL token itself (PLAN-008 D2).
PUBLIC_ROUTE_NAMES: frozenset[str] = frozenset({"two_factor:login", "healthz", "webhooks:receive"})

#: Public routes that also accept an unsafe method: the login form posts to itself, and
#: the payment provider posts events to the receiver (CSRF-exempt; 404 without the token).
PUBLIC_UNSAFE_ROUTE_NAMES: frozenset[str] = frozenset({"two_factor:login", "webhooks:receive"})


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
    # Stores (docs/qa/PLAN-007b-stores.md §1): admin only. There is no delete route.
    "ledger:stores:create": RouteRule(min_role=Role.ADMIN, methods=POST),
    "ledger:stores:edit": RouteRule(min_role=Role.ADMIN, methods=POST),
    # Leases and autopay (docs/qa/PLAN-007-leases-and-rent-run.md §4.1, D10): admin only.
    "ledger:leases:create": RouteRule(min_role=Role.ADMIN, methods=POST),
    "ledger:leases:edit": RouteRule(min_role=Role.ADMIN, methods=POST),
    "ledger:leases:enrol": RouteRule(min_role=Role.ADMIN, methods=POST),
    "ledger:leases:pause": RouteRule(min_role=Role.ADMIN, methods=POST),
    "ledger:leases:resume": RouteRule(min_role=Role.ADMIN, methods=POST),
    # Ending a lease (PLAN-010b Batch C): admin. The view refuses the form's GET below
    # admin too.
    "ledger:leases:end": RouteRule(min_role=Role.ADMIN, methods=POST),
    # Rent run and manual payments (PLAN-007 §4.1, D10): billing.
    "ledger:run:create_charges": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:run:match": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:charges:record_payment": RouteRule(min_role=Role.BILLING, methods=POST),
    # Linking an unlinked invoice to a rent charge (PLAN-008a §2 rule 4): admin or
    # billing. The view refuses the GET of the form below billing too.
    "ledger:charges:link_invoice": RouteRule(min_role=Role.BILLING, methods=POST),
    # Retry marking the provider invoice paid (PLAN-009 D2): billing.
    "ledger:charges:retry_mark": RouteRule(min_role=Role.BILLING, methods=POST),
    # Webhook events (docs/qa/PLAN-008-webhooks.md D8): replay needs billing.
    "ledger:webhooks:replay": RouteRule(min_role=Role.BILLING, methods=POST),
    # Returns queue (PLAN-008 D6): resolving needs billing; nothing re-debits.
    "ledger:returns:resolve": RouteRule(min_role=Role.BILLING, methods=POST),
    # Refund and void of an ACH payment (PLAN-009 D6, D7): admin. The views refuse the
    # GET of the confirmation pages below admin too.
    "ledger:payments:refund": RouteRule(min_role=Role.ADMIN, methods=POST),
    "ledger:payments:void": RouteRule(min_role=Role.ADMIN, methods=POST),
    # Reconciliation (PLAN-010 D10): "Run now" needs admin; resolving a mismatch needs
    # billing. The view refuses the page's GET below billing too.
    "ledger:reconciliation:run": RouteRule(min_role=Role.ADMIN, methods=POST),
    "ledger:reconciliation:resolve": RouteRule(min_role=Role.BILLING, methods=POST),
    # Setup charges (PLAN-010b D12): adding, editing, recording a payment, retrying the
    # marking and linking an invoice need billing; waiving needs admin. The views refuse
    # the forms' GET below the same role.
    "ledger:leases:add_setup_charge": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:setup_charges:payment": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:setup_charges:retry_mark": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:setup_charges:link": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:setup_charges:edit": RouteRule(min_role=Role.BILLING, methods=POST),
    "ledger:setup_charges:waive": RouteRule(min_role=Role.ADMIN, methods=POST),
    # Deposit resolution at lease end (PLAN-010b D7, D12): admin. The view refuses the
    # form's GET below admin too.
    "ledger:leases:resolve_deposit": RouteRule(min_role=Role.ADMIN, methods=POST),
    # Setting a store's landlord rent (PLAN-010b D8, D12): billing. The view refuses the
    # form's GET below billing too.
    "ledger:stores:set_cost": RouteRule(min_role=Role.BILLING, methods=POST),
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
    # Registered read-only as well (PLAN-007b §1).
    "admin:ledger_store_": RouteRule(min_role=Role.ADMIN, methods=POST),
    "admin:ledger_lease_": RouteRule(min_role=Role.ADMIN, methods=POST),
    # Registered read-only (PLAN-008 D8).
    "admin:payments_webhookevent_": RouteRule(min_role=Role.ADMIN, methods=POST),
    # The fee catalogue and the insurance premiums, edited in the admin (PLAN-010b D12).
    "admin:ledger_feetype_": RouteRule(min_role=Role.ADMIN, methods=POST),
    "admin:ledger_insurancepremium_": RouteRule(min_role=Role.ADMIN, methods=POST),
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
        # Store screens that only read (PLAN-007b §1).
        "ledger:stores:list",
        "ledger:stores:detail",
        # Lease screens that only read (PLAN-007 §4.1).
        "ledger:leases:list",
        "ledger:leases:detail",
        # Rent run and charge screens that only read (PLAN-007 §4.1).
        "ledger:run:today",
        "ledger:charges:detail",
        # Webhook event screens that only read (PLAN-008 D8).
        "ledger:webhooks:list",
        "ledger:webhooks:detail",
        "ledger:returns:list",
        # The payments ledger and its CSV export: any verified role (PLAN-009 D4).
        "ledger:payments:list",
        "ledger:payments:export",
        # The payment page: any verified role (PLAN-009 D8).
        "ledger:payments:detail",
        # The reconciliation page; the view itself requires billing (PLAN-010 D10).
        "ledger:reconciliation:page",
        # The setup charge page: any verified role (PLAN-010b D6).
        "ledger:setup_charges:detail",
        # The profit and loss report and its CSV export: any verified role (PLAN-010b
        # D11, D12).
        "ledger:reports:pnl",
        "ledger:reports:pnl_export",
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
