"""Operator screens: list, detail, create, edit, archive (PLAN-006 §4.1).

Access: every view requires a signed-in, second-factor-verified user (middleware) with a
role; a user without one gets 403. Unsafe methods are checked a second time by
``accounts.registry`` before the view runs (viewer: none; billing: create, edit; admin:
archive as well).

Every operator in a URL is loaded from the database before anything else happens, and
every change goes through :mod:`ledger.services.operators`, which writes the audit log.
No view here reaches the payment provider: since PLAN-014 C3 the processor customer, its
bank method, the connection and the Link form belong to each lease (the LEASE card's
"Edit billing details", :func:`ledger.views.leases.lease_billing`); the operator page's
PAYMENT card lists each lease's company and bank line. Templates receive no identifier
and no setting.

Every POST ends in a redirect, whatever the outcome (PLAN-006a §3.4), so a refresh
never resubmits. A form with errors is kept in the session under
:data:`HELD_FORM_KEY` (its data and error messages, minus any value that holds a card
number) and re-rendered, once, by the next GET of the same form.

The list exports as CSV (``operators:export``, PLAN-011 D5): the same search, every page,
through :class:`ledger.services.csv_export.CsvWriter`; :func:`csv_response` streams every
export of the app. The customer statement lives in :mod:`ledger.views.reports`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterator
from functools import wraps
from typing import Any
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Q, QuerySet
from django.db.models.functions import Lower
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets, role_of
from accounts.views import forbidden
from finance.templatetags.phones import phone_text
from ledger.forms import OperatorCreateForm, OperatorForm
from ledger.models import Note, NoteEntityType, Operator
from ledger.services import (
    attention,
    contacts,
    csv_export,
    detail_cards,
    list_filters,
    messaging,
    statement_links,
    workspace,
)
from ledger.services import operators as operator_service
from ledger.services import workspace_cards as operator_cards
from ledger.services.csv_export import CsvWriter
from ledger.services.operators import OperatorArchived
from ledger.validators import validate_no_payment_data
from ledger.views import contact_log, workspace_cards
from portal import invitations

PAGE_SIZE = 25
#: Longer search input is cut to this length before it reaches a query.
SEARCH_MAX_LENGTH = 200
# C0 and C1 control characters and DEL. PostgreSQL text cannot hold NUL, and none of
# them belongs in a search term; ordinary spaces are kept.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
EMAIL_TAKEN = "An operator with this e-mail address already exists."
ARCHIVED_MESSAGE = "This operator is archived."
ALREADY_SAVED = "This operator was already saved. Nothing was changed."
#: Session key of a form with errors, held for the one GET after the redirect.
HELD_FORM_KEY = "operator_form_errors"
NON_FIELD = "__all__"

View = Callable[..., HttpResponseBase]


def role_required(view: View) -> View:
    """403 for a verified user who holds no role (docs/SCOPE.md §9)."""

    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if role_of(request.user) is None:
            return forbidden(request)
        return view(request, *args, **kwargs)

    return wrapper


def csv_response(lines: Iterator[str], filename: str) -> StreamingHttpResponse:
    """A streamed CSV attachment that no cache keeps (PLAN-011 D5)."""
    response = StreamingHttpResponse(lines, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    return response


def uuid_param(request: HttpRequest, name: str) -> uuid.UUID | None:
    """A UUID from the query string, or ``None`` when it is missing or unreadable."""
    try:
        return uuid.UUID(request.GET.get(name, "") or "")
    except ValueError:
        return None


def export_name(name: str) -> str:
    """The file name of a list's export: ``<name>-<today>.csv``."""
    return f"{name}-{timezone.localdate():%Y-%m-%d}.csv"


def _actor(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):
        raise PermissionDenied
    return user


def _role(request: HttpRequest) -> Role | None:
    return role_of(request.user)


def _by_creation_key(raw: str | None) -> Operator | None:
    try:
        key = uuid.UUID(str(raw))
    except ValueError:
        return None
    return Operator.objects.filter(creation_key=key).first()


def _load(pk: str) -> Operator:
    """The operator named in the URL, loaded from the database; 404 otherwise."""
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(Operator, pk=key)


def _holds_payment_data(value: str) -> bool:
    """Whether the forms' own guard (Rule 1) refuses ``value``."""
    try:
        validate_no_payment_data(value)
    except ValidationError:
        return True
    return False


def _hold(request: HttpRequest, target: str, form: forms.BaseForm) -> None:
    """Keep a form with errors for the next GET of ``target``.

    Only the form's own fields are kept, and a value that
    :func:`~ledger.validators.validate_no_payment_data` refuses (a card number, or a
    routing number beside an account number) is dropped (its error message is kept), so
    the session never stores one.
    """
    data = {}
    for name in form.fields:
        value = str(form.data.get(name, ""))
        data[name] = "" if _holds_payment_data(value) else value
    errors = {
        field: [str(error["message"]) for error in field_errors]
        for field, field_errors in form.errors.get_json_data().items()
    }
    request.session[HELD_FORM_KEY] = {"target": target, "data": data, "errors": errors}


def _held[F: forms.BaseForm](
    request: HttpRequest, target: str, build: Callable[[dict[str, str]], F]
) -> F | None:
    """The form held for ``target``, rebuilt with its errors; consumed by this call."""
    held = request.session.pop(HELD_FORM_KEY, None)
    if not isinstance(held, dict) or held.get("target") != target:
        return None
    form = build(dict(held.get("data") or {}))
    for field, texts in dict(held.get("errors") or {}).items():
        name = None if field == NON_FIELD else str(field)
        if name is not None and name not in form.fields:
            continue
        existing = form.errors.get(field)
        present = [str(item) for item in existing] if existing else []
        for text in texts:
            if text not in present:
                form.add_error(name, str(text))
    return form


def _refused_archived(request: HttpRequest, operator: Operator) -> HttpResponse:
    messages.error(request, ARCHIVED_MESSAGE)
    return redirect("ledger:operators:detail", pk=operator.pk)


# --- Read ---------------------------------------------------------------------------


def search_text(request: HttpRequest) -> str:
    """The ``q`` search of a list, without control characters and cut to length."""
    return CONTROL_CHARACTERS.sub("", request.GET.get("q", "")).strip()[:SEARCH_MAX_LENGTH]


def _listed(request: HttpRequest) -> tuple[QuerySet[Operator], str, bool]:
    """The operators the list shows for the request's search, in its order."""
    query = search_text(request)
    include_archived = request.GET.get("archived") == "1"
    operators = Operator.objects.all()
    if not include_archived:
        operators = operators.filter(archived_at__isnull=True)
    if query:
        operators = operators.filter(Q(display_name__icontains=query) | Q(email__icontains=query))
    return operators.order_by(Lower("display_name"), "id"), query, include_archived


@role_required
@require_safe
def operator_list(request: HttpRequest) -> HttpResponse:
    return render(request, "ledger/operators/list.html", _list_context(request))


def _list_context(request: HttpRequest) -> dict[str, Any]:
    """The Operators list page's context, for the list and for New operator's page behind
    its dialog (PLAN-012f-b Batch B)."""
    operators, query, include_archived = _listed(request)
    # PLAN-012f-a D3: the stores each operator has a lease of, in effect or to come (the
    # search's rule), counted in the page's own query.
    today = timezone.localdate()
    operators = operators.annotate(
        store_count=Count(
            "leases__store",
            distinct=True,
            filter=Q(leases__ended_on__isnull=True) | Q(leases__ended_on__gte=today),
        )
    )
    page = Paginator(operators, PAGE_SIZE).get_page(request.GET.get("page"))
    export = {"q": query, "archived": "1" if include_archived else ""}
    accepted = {k: v for k, v in export.items() if v}
    export_query = urlencode(accepted)
    return {
        "page": page,
        "query": query,
        # PLAN-014 A5: the Filters button's count, the chip and Clear.
        "active_filters": list_filters.active(
            request, {"archived": ("Archived", {"1": "Included"})}, accepted=accepted
        ),
        "include_archived": include_archived,
        "can_create": meets(_role(request), Role.BILLING),
        "export_query": export_query,
        "pager_query": export_query,
    }


EXPORT_HEADER = (
    "Operator",
    "Email",
    "Phone",
    "SMS phone",
    "SMS consent given on",
    "E-mail consent given on",
    "City",
    "State",
    "Needs review",
    "Archived on",
)


@role_required
@require_safe
def operator_export(request: HttpRequest) -> StreamingHttpResponse:
    """``ledger:operators:export`` (PLAN-011 D5): the list's rows, every page."""
    operators, _, _ = _listed(request)
    records = (
        [
            operator.display_name,
            operator.email or "",
            operator.phone,
            operator.sms_phone,
            operator.sms_consent_at,
            operator.email_consent_at,
            operator.city,
            operator.state,
            operator.needs_review,
            operator.archived_at,
        ]
        for operator in operators[: csv_export.CSV_MAX_ROWS + 1].iterator()
    )
    return csv_response(CsvWriter(EXPORT_HEADER).lines(records), export_name("operators"))


#: The entries the RECENT CONTACT card lists (PLAN-012f-a D7); the Notes & follow-ups card lists
#: more.
RECENT_CONTACTS = 2


@role_required
@require_safe
def operator_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """The operator page as the workspace (PLAN-012d D14): the operator-level items
    (contact details, SMS phone and consents, the payment-provider block, Collection
    e-mails, the notes & follow-ups, notes), then the selected store's header card and action
    bar (``?store=``, one of the operator's stores) and the open tab (``?tab=``) across
    the operator's stores (:func:`ledger.services.workspace.operator_workspace`)."""
    return render(request, "ledger/operators/detail.html", _detail_context(request, _load(pk)))


def _detail_context(
    request: HttpRequest, operator: Operator, *, forced_tab: str | None = None
) -> dict[str, Any]:
    """The operator page's context, for the page and for Edit operator's page behind its
    dialog (PLAN-012f-b Batch B). ``forced_tab`` opens that tab across the operator's stores
    whatever the query says, without its filter (PLAN-012f-b D15: the "Invoice saved"
    dialog over the Invoices tab)."""
    role = _role(request)
    today = timezone.localdate()
    tab = workspace.chosen_tab(forced_tab or request.GET.get("tab"), role)
    space = workspace.operator_workspace(
        operator,
        today,
        role,
        tab=tab,
        store=None if forced_tab else request.GET.get("store"),
        chosen="" if forced_tab else request.GET.get("filter", ""),
    )
    selected = space.space.store if space.space is not None else None
    tab_context: dict[str, Any] = {}
    if selected is not None:
        # PLAN-012f D8: the selected store's side cards (STORE COST, DOCUMENTS).
        tab_context.update(workspace_cards.side_cards(request, selected, today))
    if tab == workspace.TAB_DOCUMENTS and selected is not None:
        # Imported here: ledger.views.documents imports this module's helpers.
        from ledger.views import documents

        tab_context.update(documents.section_context(request, selected))
    active = operator.archived_at is None
    billing = meets(role, Role.BILLING)
    contact_rows = contact_log.log_rows(
        request,
        list(
            contacts.entries(operator, include_deleted=contact_log.include_deleted(request))[
                : contacts.PAGE_LIMIT + 1
            ]
        ),
    )
    store_count = len(space.stores)
    store_cards = operator_cards.store_cards(operator, space.stores, space.open_rows, today)
    return {
        **space.data,
        **tab_context,
        "operator": operator,
        "ows": space,
        "space": space.space,
        "header": space.space.header if space.space is not None else None,
        "store": selected,
        "billing": billing,
        # The tab and filter links keep the selected store (D14).
        "keep_store": urlencode({"store": str(selected.pk)}) + "&" if selected else "",
        "notes": Note.objects.filter(
            entity_type=NoteEntityType.OPERATOR, entity_id=operator.pk
        ).order_by("-created_at"),
        "can_edit": billing and active,
        "can_archive": meets(role, Role.ADMIN) and active,
        # PLAN-011b D6, D7: the operator's own attention items as banners, and the
        # latest notes & follow-ups entries.
        "attention": attention.attention(timezone.localdate(), operator=operator.pk),
        # PLAN-012c-b D2: Kept, Reschedule and Broken on a broken promise.
        "can_record_outcome": billing and active,
        "contact_rows": contact_rows,
        "contact_limit": contacts.PAGE_LIMIT,
        "can_add_contact": billing and active,
        # PLAN-011b D4: the collection e-mails sent to the operator, and the
        # "handled personally" switch (billing).
        "collection_log": messaging.operator_log(operator, messaging.LOG_LIMIT),
        "collection_log_limit": messaging.LOG_LIMIT,
        "can_set_handled": billing and active,
        # PLAN-011b D4: the signed statement links (billing revokes them).
        "can_revoke_links": billing,
        "statement_link_days": statement_links.MAX_AGE_DAYS,
        # PLAN-012f-a D7 (operator-workspace-laptop.png): the header's avatar and muted
        # line, the strip of store cards and the right column's cards; facts only.
        "operator_initials": operator_cards.initials(operator.display_name),
        "operator_line": " · ".join(
            part
            for part in (
                phone_text(operator.phone),
                operator.email or "",
                "SMS consent given" if operator.sms_consent_at else "no SMS consent",
                f"{store_count} store" if store_count == 1 else f"{store_count} stores",
            )
            if part
        ),
        "store_cards": store_cards,
        # The Charges card's title: "Charges · all stores", or the selected store's.
        "charges_scope": next(
            (card.title for card in store_cards if card.store == selected), "all stores"
        ),
        # PLAN-014 C3: the PAYMENT card's line per lease (store, company, bank).
        "lease_accounts": detail_cards.lease_accounts(operator),
        "returns_this_year": operator_cards.returns_this_year(operator, today),
        "open_promise": operator_cards.open_promise(operator),
        "recent_contacts": [row for row in contact_rows if row.entry.deleted_at is None][
            :RECENT_CONTACTS
        ],
        "statement_link_opened": operator_cards.statement_link_opened(operator),
        # PLAN-014 H2: the operator's portal account and the card's actions (billing).
        "portal": invitations.card(operator),
    }


# --- Write --------------------------------------------------------------------------

#: The New / Edit operator dialog's fields that take both columns (PLAN-012f-b Batch B).
WIDE_FIELDS: tuple[str, ...] = ("display_name", "address_line1", "address_line2", "notes")


def _form_dialog(
    form: OperatorForm, title: str, return_url: str, *, operator: Operator | None = None
) -> dict[str, Any]:
    """The New / Edit operator dialog (PLAN-012f-b Batch B; ledger/operators/_form_dialog.html),
    open over the page behind it; closing it goes to ``return_url``."""
    return {
        "form": form,
        "title": title,
        "return_url": return_url,
        "operator": operator,
        "wide": WIDE_FIELDS,
    }


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def operator_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        existing = _by_creation_key(request.POST.get("creation_key"))
        if existing is not None:
            messages.info(request, ALREADY_SAVED)
            return redirect("ledger:operators:detail", pk=existing.pk)
        form = OperatorCreateForm(request.POST)
        if form.is_valid():
            try:
                operator, created = operator_service.create_operator(
                    form.cleaned_data,
                    creation_key=form.cleaned_data["creation_key"],
                    actor=_actor(request),
                )
            except IntegrityError:
                form.add_error("email", EMAIL_TAKEN)
            else:
                if not created:
                    messages.info(request, ALREADY_SAVED)
                    return redirect("ledger:operators:detail", pk=operator.pk)
                messages.success(request, f"Operator {operator.display_name} added.")
                return redirect("ledger:operators:detail", pk=operator.pk)
        _hold(request, "create", form)
        return redirect("ledger:operators:create")
    form = _held(request, "create", OperatorCreateForm) or OperatorCreateForm(
        initial={"creation_key": uuid.uuid4()}
    )
    context = _list_context(request)
    context["form_dialog"] = _form_dialog(form, "New operator", reverse("ledger:operators:list"))
    return render(request, "ledger/operators/list.html", context)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def operator_edit(request: HttpRequest, pk: str) -> HttpResponse:
    operator = _load(pk)
    if operator.archived_at is not None:
        return _refused_archived(request, operator)
    if request.method == "POST":
        form = OperatorForm(request.POST, instance=operator)
        if form.is_valid():
            try:
                operator_service.update_operator(
                    operator.pk, form.cleaned_data, actor=_actor(request)
                )
            except OperatorArchived:
                return _refused_archived(request, operator)
            except IntegrityError:
                form.add_error("email", EMAIL_TAKEN)
            else:
                messages.success(request, "Operator saved.")
                return redirect("ledger:operators:detail", pk=operator.pk)
        _hold(request, f"edit:{operator.pk}", form)
        return redirect("ledger:operators:edit", pk=operator.pk)
    form = _held(
        request, f"edit:{operator.pk}", lambda data: OperatorForm(data, instance=operator)
    ) or OperatorForm(instance=operator)
    context = _detail_context(request, operator)
    context["form_dialog"] = _form_dialog(
        form,
        f"Edit operator {operator.display_name}",
        reverse("ledger:operators:detail", kwargs={"pk": operator.pk}),
        operator=operator,
    )
    return render(request, "ledger/operators/detail.html", context)


@role_required
@require_POST
def operator_archive(request: HttpRequest, pk: str) -> HttpResponse:
    operator = _load(pk)
    if operator.archived_at is None:
        operator_service.archive_operator(operator.pk, actor=_actor(request))
        messages.success(request, "Operator archived.")
    return redirect("ledger:operators:list")
