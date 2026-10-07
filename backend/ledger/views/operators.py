"""Operator screens: list, detail, create, edit, archive, retry, link (PLAN-006 §4.1).

Access: every view requires a signed-in, second-factor-verified user (middleware) with a
role; a user without one gets 403. Unsafe methods are checked a second time by
``accounts.registry`` before the view runs (viewer: none; billing: create, edit,
retry, link; admin: archive as well).

Every operator in a URL is loaded from the database before anything else happens, and
every change goes through :mod:`payments.operator_sync`, which writes the audit log.
No view calls the payment provider: the sync, the retry and the link are queued as jobs
(:mod:`ledger.views.jobs`, PLAN-010 D3) after the local write, and the detail page shows
the latest one. Templates receive masked identifiers only (first four and last four
characters) and no setting.

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
from decimal import Decimal
from functools import wraps
from typing import Any
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Q, QuerySet, Sum
from django.db.models.functions import Lower
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets, role_of
from accounts.views import forbidden
from ledger.forms import LinkForm, OperatorCreateForm, OperatorForm
from ledger.models import ZERO, Note, NoteEntityType, Operator, RentCharge, SyncStatus
from ledger.services import (
    attention,
    collections,
    contacts,
    csv_export,
    one_off_charges,
    statement_links,
)
from ledger.services.csv_export import CsvWriter
from ledger.validators import contains_card_number
from ledger.views import contact_log
from ledger.views.jobs import busy, job_context, submit
from payments import operator_sync
from payments.models import JobKind
from payments.operator_sync import OperatorArchived
from payments.provider import mask_identifier

PAGE_SIZE = 25
#: Longer search input is cut to this length before it reaches a query.
SEARCH_MAX_LENGTH = 200
# C0 and C1 control characters and DEL. PostgreSQL text cannot hold NUL, and none of
# them belongs in a search term; ordinary spaces are kept.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
EMAIL_TAKEN = "An operator with this e-mail address already exists."
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


def _after_save(
    request: HttpRequest, operator: Operator, replay_key: uuid.UUID | None = None
) -> None:
    """Queue the sync of a row that needs it; tell staff when it waits for a manual link."""
    if operator.sync_status == SyncStatus.PENDING:
        submit(request, JobKind.OPERATOR_SYNC, operator, replay_key=replay_key)
    elif operator.sync_status == SyncStatus.AWAITING_LINK:
        messages.warning(request, operator_sync.AWAITING_LINK_MESSAGE)


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


def _hold(request: HttpRequest, target: str, form: forms.BaseForm) -> None:
    """Keep a form with errors for the next GET of ``target``.

    Only the form's own fields are kept, and a value holding a card number is dropped
    (its error message is kept), so the session never stores one.
    """
    data = {}
    for name in form.fields:
        value = form.data.get(name, "")
        data[name] = "" if contains_card_number(str(value)) else str(value)
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
    messages.error(request, operator_sync.MESSAGES[operator_sync.ARCHIVED])
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
        operators = operators.filter(
            Q(display_name__icontains=query)
            | Q(legal_name__icontains=query)
            | Q(company__icontains=query)
            | Q(email__icontains=query)
        )
    return operators.order_by(Lower("display_name"), "id"), query, include_archived


@role_required
@require_safe
def operator_list(request: HttpRequest) -> HttpResponse:
    operators, query, include_archived = _listed(request)
    page = Paginator(operators, PAGE_SIZE).get_page(request.GET.get("page"))
    export = {"q": query, "archived": "1" if include_archived else ""}
    return render(
        request,
        "ledger/operators/list.html",
        {
            "page": page,
            "query": query,
            "include_archived": include_archived,
            "can_create": meets(_role(request), Role.BILLING),
            "export_query": urlencode({k: v for k, v in export.items() if v}),
        },
    )


EXPORT_HEADER = (
    "Operator",
    "Legal name",
    "Company",
    "Email",
    "Phone",
    "SMS phone",
    "SMS consent given on",
    "E-mail consent given on",
    "City",
    "State",
    "Provider link",
    "Enrolment",
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
            operator.legal_name,
            operator.company,
            operator.email or "",
            operator.phone,
            operator.sms_phone,
            operator.sms_consent_at,
            operator.email_consent_at,
            operator.city,
            operator.state,
            operator.get_sync_status_display(),
            operator.get_enrolment_status_display(),
            operator.needs_review,
            operator.archived_at,
        ]
        for operator in operators[: csv_export.CSV_MAX_ROWS + 1].iterator()
    )
    return csv_response(CsvWriter(EXPORT_HEADER).lines(records), export_name("operators"))


@role_required
@require_safe
def operator_detail(request: HttpRequest, pk: str) -> HttpResponse:
    operator = _load(pk)
    refs = operator_sync.current_refs(operator)
    customer = refs.get(operator_sync.KIND_CUSTOMER, "")
    method = refs.get(operator_sync.KIND_PAYMENT_METHOD, "")
    outstanding: Decimal = (
        RentCharge.objects.filter(lease__operator=operator).aggregate(total=Sum("balance"))["total"]
        or ZERO
    )
    role = _role(request)
    active = operator.archived_at is None
    billing = meets(role, Role.BILLING)
    failed = operator.sync_status == SyncStatus.FAILED
    phone_ok = operator_sync.phone_is_valid(operator)
    retry_in = operator_sync.retry_wait_seconds(operator) if failed and phone_ok else 0
    jobs = job_context(operator)
    return render(
        request,
        "ledger/operators/detail.html",
        {
            **jobs,
            "operator": operator,
            "customer_masked": mask_identifier(customer) if customer else "",
            "payment_method_masked": mask_identifier(method) if method else "",
            "failure_message": (
                operator_sync.MESSAGES.get(operator.sync_failure, operator_sync.DEFAULT_MESSAGE)
                if failed
                else ""
            ),
            "failure_fields": (
                ", ".join(operator.sync_failure_fields.split(","))
                if failed and operator.sync_failure_fields
                else ""
            ),
            "leases": operator.leases.select_related("store").order_by("store__external_label"),
            "outstanding": outstanding.quantize(Decimal("0.01")),
            "invoices": operator.invoices.order_by("-issue_date", "invoice_number"),
            "notes": Note.objects.filter(
                entity_type=NoteEntityType.OPERATOR, entity_id=operator.pk
            ).order_by("-created_at"),
            "provider_updated_at": (
                operator_sync.provider_updated_at(operator)
                if operator.sync_status == SyncStatus.LINKED
                else None
            ),
            "can_edit": billing and active,
            "can_retry": billing
            and active
            and failed
            and phone_ok
            and not retry_in
            and not jobs["job_refresh"],
            "fix_phone_message": (
                operator_sync.MESSAGES[operator_sync.PHONE_INVALID]
                if billing and active and failed and not phone_ok
                else ""
            ),
            "retry_wait_message": (
                operator_sync.retry_wait_message(retry_in)
                if billing and active and retry_in
                else ""
            ),
            "can_link": billing and active,
            # PLAN-012 D6: New invoice replaces "Create one-off charge"; the drafts.
            "can_new_invoice": billing and active,
            "invoice_drafts": operator.invoice_drafts.select_related("lease__store", "sent_invoice")
            .annotate(total=Sum("lines__line_total"))
            .order_by("-created_at"),
            "one_off_charges": one_off_charges.operator_charges(operator),
            "can_archive": meets(role, Role.ADMIN) and active,
            # PLAN-011b D6, D7: the operator's own attention items as banners, and the
            # latest contact log entries.
            "attention": attention.attention(timezone.localdate(), operator=operator.pk),
            # PLAN-012c-b D2: Kept, Reschedule and Broken on a broken promise.
            "can_record_outcome": billing and active,
            "contact_rows": contact_log.log_rows(
                request,
                list(
                    contacts.entries(
                        operator, include_deleted=contact_log.include_deleted(request)
                    )[: contacts.PAGE_LIMIT + 1]
                ),
            ),
            "contact_limit": contacts.PAGE_LIMIT,
            "can_add_contact": billing and active,
            # PLAN-011b D4: the collection e-mails sent to the operator, and the
            # "handled personally" switch (billing).
            "collection_log": collections.operator_log(operator, collections.LOG_LIMIT),
            "collection_log_limit": collections.LOG_LIMIT,
            "can_set_handled": billing and active,
            # PLAN-011b D4: the signed statement links (billing revokes them).
            "can_revoke_links": billing,
            "statement_link_days": statement_links.MAX_AGE_DAYS,
        },
    )


# --- Write --------------------------------------------------------------------------


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
                operator, created = operator_sync.create_operator(
                    form.cleaned_data,
                    creation_key=form.cleaned_data["creation_key"],
                    actor=_actor(request),
                )
            except IntegrityError:
                form.add_error("email", EMAIL_TAKEN)
            else:
                if created:
                    messages.success(request, "Operator saved.")
                    _after_save(request, operator, form.cleaned_data["creation_key"])
                else:
                    messages.info(request, ALREADY_SAVED)
                return redirect("ledger:operators:detail", pk=operator.pk)
        _hold(request, "create", form)
        return redirect("ledger:operators:create")
    form = _held(request, "create", OperatorCreateForm) or OperatorCreateForm(
        initial={"creation_key": uuid.uuid4()}
    )
    return render(request, "ledger/operators/form.html", {"form": form, "operator": None})


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
                updated = operator_sync.update_operator(
                    operator.pk, form.cleaned_data, actor=_actor(request)
                )
            except OperatorArchived:
                return _refused_archived(request, operator)
            except IntegrityError:
                form.add_error("email", EMAIL_TAKEN)
            else:
                messages.success(request, "Operator saved.")
                _after_save(request, updated)
                return redirect("ledger:operators:detail", pk=operator.pk)
        _hold(request, f"edit:{operator.pk}", form)
        return redirect("ledger:operators:edit", pk=operator.pk)
    form = _held(
        request, f"edit:{operator.pk}", lambda data: OperatorForm(data, instance=operator)
    ) or OperatorForm(instance=operator)
    return render(request, "ledger/operators/form.html", {"form": form, "operator": operator})


@role_required
@require_POST
def operator_archive(request: HttpRequest, pk: str) -> HttpResponse:
    operator = _load(pk)
    if operator.archived_at is None:
        operator_sync.archive_operator(operator.pk, actor=_actor(request))
        messages.success(request, "Operator archived.")
    return redirect("ledger:operators:list")


@role_required
@require_POST
def operator_retry_sync(request: HttpRequest, pk: str) -> HttpResponse:
    operator = _load(pk)
    if operator.archived_at is not None:
        return _refused_archived(request, operator)
    if not busy(request, operator):
        submit(request, JobKind.OPERATOR_RETRY, operator)
    return redirect("ledger:operators:detail", pk=operator.pk)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def operator_link(request: HttpRequest, pk: str) -> HttpResponse:
    operator = _load(pk)
    if operator.archived_at is not None:
        return _refused_archived(request, operator)
    if request.method == "POST":
        form = LinkForm(request.POST)
        if form.is_valid():
            submit(
                request,
                JobKind.OPERATOR_LINK,
                operator,
                {
                    "customer_id": form.cleaned_data["customer_id"],
                    "payment_method_id": form.cleaned_data["payment_method_id"],
                },
            )
            return redirect("ledger:operators:detail", pk=operator.pk)
        _hold(request, f"link:{operator.pk}", form)
        return redirect("ledger:operators:link", pk=operator.pk)
    form = _held(request, f"link:{operator.pk}", LinkForm) or LinkForm()
    refs = operator_sync.current_refs(operator)
    customer = refs.get(operator_sync.KIND_CUSTOMER, "")
    return render(
        request,
        "ledger/operators/link.html",
        {
            "form": form,
            "operator": operator,
            "customer_masked": mask_identifier(customer) if customer else "",
        },
    )
