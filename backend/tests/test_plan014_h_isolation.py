"""PLAN-014 H6 (Fable): the portal's isolation matrix, generated from the portal URL conf so
that a new route cannot escape it. For every portal route: it is classified in the registry;
signed out it redirects to the portal sign-in (or, public, answers without a staff page); as
operator A's user, a route with a store id answers 404 for operator B's store, for an unknown
id and for garbage, and 200 for A's own stores (an ended one included); a route with a token
answers 404 for a bogus token; a page without parameters answers 200 for A and never carries
B's store number, name or amounts as whole tokens; an invoice or receipt PDF route (PLAN-015c)
answers 404 for B's document, A's unlisted one, an unknown id and garbage, and A's own PDF
(audited with the portal user) for A only. Then the principals: a staff session opens no
portal page, a portal session opens no staff page, a disabled account is signed out at its next
request, and every portal POST is CSRF-protected."""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from django.test import Client
from django.urls import URLPattern
from django.utils import timezone

from accounts.registry import PORTAL_PUBLIC_ROUTE_NAMES, PORTAL_ROUTE_NAMES
from accounts.roles import Role
from portal import urls as portal_urls
from portal.models import PortalUser
from tests.test_operator_views import client_for
from tests.test_portal import World, active_user, portal_client
from tests.test_portal import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_portal import world as world  # noqa: PLC0414 (the fixture, re-exported)

pytestmark = pytest.mark.django_db

#: B's store number, name and open amount: never on A's pages, as whole tokens.
B_TOKENS = ("202", "Betaburg", "7,777.00", "9999")


#: The parameters a portal route may take: a store's id, a link's token, and (PLAN-015c) an
#: invoice's or a payment's id for its PDF.
PARAMETERS = ("pk", "token", "invoice", "payment")
PDF_PARAMETERS = ("invoice", "payment")


def routes() -> list[tuple[str, str, str]]:
    """(name, path template, parameter) for every portal route; the parameter is one of
    :data:`PARAMETERS` or ``""``."""
    found = []
    for pattern in portal_urls.urlpatterns:
        assert isinstance(pattern, URLPattern), pattern
        route = str(pattern.pattern)
        named = [name for name in PARAMETERS if f"<str:{name}>" in route]
        assert len(named) <= 1, route
        found.append((f"portal:{pattern.name}", route, named[0] if named else ""))
    return found


def url(route: str, value: str = "") -> str:
    for name in PARAMETERS:
        route = route.replace(f"<str:{name}>", value)
    return "/portal/" + route


def whole_tokens_absent(body: str) -> None:
    for token in B_TOKENS:
        assert re.search(rf"(?<![\w,.]){re.escape(token)}(?![\w,.])", body) is None, token


def test_every_portal_route_is_classified_and_the_public_set_is_exactly_the_sign_in_flow() -> None:
    names = {name for name, _, _ in routes()}
    assert names == PORTAL_ROUTE_NAMES
    assert PORTAL_PUBLIC_ROUTE_NAMES <= names
    assert {name for name in PORTAL_PUBLIC_ROUTE_NAMES if "pk" in name} == set()
    public = {name for name, route, _ in routes() if name in PORTAL_PUBLIC_ROUTE_NAMES}
    assert public == {
        "portal:login",
        "portal:logout",
        "portal:invite",
        "portal:reset_request",
        "portal:reset_confirm",
        # The portal's web app manifest (the home-screen identity, 2026-11-05): JSON with the
        # app's fixed name, colours and icon paths, no setting and no row — public by design.
        "portal:manifest",
    } | {name for name in PORTAL_PUBLIC_ROUTE_NAMES if name.endswith("not_found")}


@pytest.mark.parametrize(("name", "route", "parameter"), routes())
def test_signed_out_every_private_route_redirects_to_the_portal_sign_in(
    world: World, name: str, route: str, parameter: str
) -> None:
    value = str(world.store_a.pk) if parameter == "pk" else "bogus-token"
    response = Client().get(url(route, value))
    if name in PORTAL_PUBLIC_ROUTE_NAMES:
        assert response.status_code in (200, 404, 405), name
        assert b"Sign in to continue" not in response.content  # never the staff 401 page
    else:
        assert response.status_code == 302, name
        assert response["Location"].startswith("/portal/login/"), name


@pytest.mark.parametrize(
    ("name", "route", "parameter"), [row for row in routes() if row[2] == "pk"]
)
def test_a_store_route_answers_404_for_another_operators_store_and_200_for_ones_own(
    world: World, name: str, route: str, parameter: str
) -> None:
    client = portal_client(active_user(world.a))
    assert client.get(url(route, str(world.store_b.pk))).status_code == 404, name
    assert client.get(url(route, str(uuid.uuid4()))).status_code == 404, name
    assert client.get(url(route, "not-a-uuid")).status_code == 404, name
    for store in (world.store_a, world.store_a_old):
        response = client.get(url(route, str(store.pk)))
        assert response.status_code == 200, (name, store.external_label)
        whole_tokens_absent(response.content.decode())


def pdf_ids(world: World, parameter: str) -> tuple[str, str, list[str]]:
    """A's own document id, B's, and A's ids that are not listed (a draft invoice, a pending
    payment) for an invoice or a payment PDF route."""
    from datetime import date

    from ledger.models import Invoice, Payment
    from tests.receivables_helpers import a_payment

    if parameter == "invoice":
        mine = str(world.invoice_a.pk)
        theirs = str(Invoice.objects.get(operator=world.b).pk)
        hidden = [str(Invoice.objects.get(operator=world.a, internal_status="draft").pk)]
        return mine, theirs, hidden
    mine = str(Payment.objects.get(rent_charge=world.paid_a).pk)
    theirs = str(Payment.objects.get(rent_charge__lease=world.lease_b).pk)
    pending = a_payment(
        world.rent_a, "100.00", None, status="pending", initiated_on=date(2026, 9, 5)
    )
    return mine, theirs, [str(pending.pk)]


@pytest.mark.parametrize(
    ("name", "route", "parameter"), [row for row in routes() if row[2] in PDF_PARAMETERS]
)
def test_a_pdf_route_answers_404_for_another_operators_document_and_200_for_ones_own(
    world: World, name: str, route: str, parameter: str
) -> None:
    import io

    from pypdf import PdfReader

    from ledger.models import AuditLog

    client = portal_client(active_user(world.a))
    mine, theirs, hidden = pdf_ids(world, parameter)
    for value in (theirs, *hidden, str(uuid.uuid4()), "not-a-uuid", str(world.store_a.pk)):
        response = client.get(url(route, value))
        assert response.status_code == 404, (name, value)
        assert b"%PDF" not in response.content
    assert not AuditLog.objects.filter(entity_id=theirs).exists()
    response = client.get(url(route, mine))
    assert response.status_code == 200, name
    assert response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "private, no-store"
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages)
    whole_tokens_absent(text)
    entry = AuditLog.objects.get(entity_id=mine, action__endswith="downloaded")
    assert entry.actor_id is None and entry.after is not None
    assert entry.after["portal_user"] == str(PortalUser.objects.get(operator=world.a).pk)
    # B, signed in, gets A's as 404 too.
    other = portal_client(active_user(world.b))
    assert other.get(url(route, mine)).status_code == 404, name


@pytest.mark.parametrize(
    ("name", "route", "parameter"), [row for row in routes() if row[2] == "token"]
)
def test_a_link_route_answers_404_for_a_bogus_token(
    world: World, name: str, route: str, parameter: str
) -> None:
    for token in ("bogus", "a" * 43, "x" * 200, str(uuid.uuid4())):
        response = Client().get(url(route, token))
        assert response.status_code == 404, (name, token)
        assert "expired or was used already" in response.content.decode()


@pytest.mark.parametrize(
    ("name", "route", "parameter"),
    [row for row in routes() if row[2] == "" and row[0] not in PORTAL_PUBLIC_ROUTE_NAMES],
)
def test_a_page_without_parameters_answers_200_for_a_and_never_names_b(
    world: World, name: str, route: str, parameter: str
) -> None:
    client = portal_client(active_user(world.a))
    response = client.get(url(route))
    assert response.status_code in (200, 405), name  # a POST-only route answers 405 to GET
    if response.status_code == 200:
        body = response.content.decode()
        whole_tokens_absent(body)
        if name not in ("portal:account", "portal:statement"):
            assert (
                re.search(r"(?<![\w,.])101(?![\w,.])", body) is not None
            )  # A's store, whole token


def test_b_sees_b_and_not_a(world: World) -> None:
    client = portal_client(active_user(world.b))
    home = client.get("/portal/").content.decode()
    assert "Betaburg" in home and "Alphaville" not in home
    assert re.search(r"(?<![\w,.])1,850\.00(?![\w,.])", home) is None
    assert client.get(f"/portal/stores/{world.store_a.pk}/").status_code == 404


def test_a_staff_session_opens_no_portal_page_and_a_portal_session_no_staff_page(
    world: World,
) -> None:
    staff, _ = client_for(Role.ADMIN)
    response = staff.get("/portal/")
    assert response.status_code == 302 and response["Location"].startswith("/portal/login/")
    assert staff.get(f"/portal/stores/{world.store_a.pk}/").status_code == 302
    portal = portal_client(active_user(world.a))
    for path in ("/", "/stores/", f"/operators/{world.a.pk}/", "/admin-settings/", "/reports/"):
        response = portal.get(path)
        assert response.status_code in (302, 401, 403), path
        assert response.status_code != 200, path


def test_a_disabled_account_is_signed_out_at_its_next_request(world: World) -> None:
    user = active_user(world.a)
    client = portal_client(user)
    assert client.get("/portal/").status_code == 200
    PortalUser.objects.filter(pk=user.pk).update(is_active=False, disabled_at=timezone.now())
    response = client.get("/portal/")
    assert response.status_code == 302 and response["Location"].startswith("/portal/login/")
    assert client.get("/portal/").status_code == 302  # and it stays out


def test_an_archived_operators_account_is_signed_out_and_cannot_sign_in(world: World) -> None:
    user = active_user(world.a)
    client = portal_client(user)
    from ledger.models import Operator

    Operator.objects.filter(pk=world.a.pk).update(archived_at=timezone.now())
    response = client.get("/portal/")
    assert response.status_code == 302 and response["Location"].startswith("/portal/login/")
    fresh = Client()
    again = fresh.post("/portal/login/", {"email": user.email, "password": "correct horse battery"})
    assert again.status_code == 400
    assert "not right" in again.content.decode()


@pytest.mark.parametrize(
    "path",
    [
        "/portal/login/",
        "/portal/logout/",
        "/portal/reset/",
        "/portal/account/",
        "/portal/account/password/",
    ],
)
def test_every_portal_post_is_csrf_protected(world: World, path: str) -> None:
    strict = Client(enforce_csrf_checks=True)
    response = strict.post(path, {"email": "x@example.com", "password": "y"})
    assert response.status_code == 403, path


def test_the_portal_pages_are_not_indexed_and_the_link_pages_send_same_origin(
    world: World,
) -> None:
    client = portal_client(active_user(world.a))
    response: Any = client.get("/portal/")
    assert response["X-Robots-Tag"].startswith("noindex")
    gone: Any = Client().get("/portal/invite/bogus/")
    assert gone.status_code == 404 and gone["Referrer-Policy"] == "same-origin"
