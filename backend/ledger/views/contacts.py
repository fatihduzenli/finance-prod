"""The contact log (docs/qa/PLAN-011b D7; PLAN-012 D2 to D5, D9).

- ``ledger:operators:contacts`` and ``ledger:stores:contacts`` (any role): every entry of
  the operator, or about a lease of the store, newest first, paginated, filtered by a
  promise's status (``?status=open|kept|rescheduled|broken``). A deleted entry is shown
  struck through with its reason to billing and admin, and left out for a viewer.
- ``ledger:operators:contacts_add`` and ``ledger:stores:contacts_add`` (billing; the
  registry refuses the POST below billing and the views refuse the form's GET too): add an
  entry. The store's form is the form of the operator of the store's lease in effect
  today, with that lease chosen; it returns to the store page's Contact log tab, and
  opened from the store page (``?store=&lease=``, PLAN-012d D2) it hides the lease and
  shows the read-only line. When the operator has an open promise the form asks what
  happened to it (Still open, the default, Kept, Rescheduled or Broken), and warns when a
  new promise would close the open promise of its lease as Rescheduled (PLAN-012c-b D1,
  D3). A refusal shows the form again.
- ``ledger:contacts:outcome`` (billing; PLAN-012c-b D2): Kept, Reschedule or Broken on one
  open promise; the entry goes through :func:`contacts.add_entry` with that promise as
  ``previous``, then back to the validated local ``next`` at the new entry.
- ``ledger:operators:contacts_export`` (any role): every entry as CSV; a viewer's file
  leaves out the deleted entries.
- ``ledger:contacts:edit`` (billing, and only the author: anyone else gets 403) while the
  edit window is open; ``ledger:contacts:correct`` (billing): append a correction;
  ``ledger:contacts:delete`` (admin): soft delete with a reason.
"""

from __future__ import annotations

import uuid
from datetime import date

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import QuerySet
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    QueryDict,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import (
    ContactCorrectionForm,
    ContactDeleteForm,
    ContactEditForm,
    ContactEntryForm,
    ContactOutcomeForm,
)
from ledger.models import (
    ZERO,
    CollectionStatus,
    ContactEntry,
    ContactKind,
    ContactStatus,
    Lease,
    Operator,
    Store,
)
from ledger.services import contacts, workspace
from ledger.validators import contains_bank_routing_pattern, contains_card_number
from ledger.views import store_context
from ledger.views.contact_log import include_deleted, log_rows, store_add_url
from ledger.views.operators import (
    PAGE_SIZE,
    _actor,
    _load,
    _role,
    csv_response,
    role_required,
)
from ledger.views.run import _billing_only
from ledger.views.store_context import StoreContext, local_next
from ledger.wording import format_amount

NO_STORE_LEASE = "This store has no lease in effect today; add the entry on the operator's page."


def drop_payment_data(form: forms.Form) -> None:
    """Blank every submitted value that holds a card or account number before the form is
    shown again: its errors stay, the number never goes back to the browser."""
    submitted = form.data
    if not isinstance(submitted, QueryDict):
        return
    data = submitted.copy()
    for name in list(data.keys()):
        if any(
            contains_card_number(str(value)) or contains_bank_routing_pattern(str(value))
            for value in data.getlist(name)
        ):
            data[name] = ""
    form.data = data


# --- The log ------------------------------------------------------------------------------


def _status(request: HttpRequest) -> str:
    status = request.GET.get("status", "")
    return status if status in ContactStatus.values else ""


def _log_page(
    request: HttpRequest,
    rows: QuerySet[ContactEntry],
    context: dict[str, object],
    store: Store | None = None,
) -> HttpResponse:
    status = _status(request)
    page = Paginator(contacts.with_status(rows, status), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "ledger/operators/contacts.html",
        {
            **context,
            "page": page,
            "rows": log_rows(request, list(page.object_list), store=store),
            "status": status,
            "statuses": ContactStatus.choices,
        },
    )


@role_required
@require_safe
def contacts_page(request: HttpRequest, pk: str) -> HttpResponse:
    operator = _load(pk)
    can_add = meets(_role(request), Role.BILLING) and operator.archived_at is None
    return _log_page(
        request,
        contacts.entries(operator, include_deleted=include_deleted(request)),
        {
            "title": operator.display_name,
            "back_label": operator.display_name,
            "back_url": reverse("ledger:operators:detail", kwargs={"pk": operator.pk}),
            "add_url": (
                reverse("ledger:operators:contacts_add", kwargs={"pk": operator.pk})
                if can_add
                else ""
            ),
            "export_url": reverse("ledger:operators:contacts_export", kwargs={"pk": operator.pk}),
        },
    )


def _store(pk: str) -> Store:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(Store, pk=key)


@role_required
@require_safe
def store_contacts_page(request: HttpRequest, pk: str) -> HttpResponse:
    store = _store(pk)
    return _log_page(
        request,
        contacts.store_entries(store, include_deleted=include_deleted(request)),
        {
            "title": f"store {store.external_label}",
            "back_label": f"Store {store.external_label}",
            "back_url": reverse("ledger:stores:detail", kwargs={"pk": store.pk}),
            "add_url": store_add_url(request, store, timezone.localdate()),
            "export_url": "",
        },
        store,
    )


@role_required
@require_safe
def contacts_export(request: HttpRequest, pk: str) -> StreamingHttpResponse:
    operator = _load(pk)
    return csv_response(
        contacts.csv_lines(operator, include_deleted=include_deleted(request)),
        f"contacts-{operator.pk}-{timezone.localdate():%Y-%m-%d}.csv",
    )


# --- Adding an entry ------------------------------------------------------------------------


def _promise_label(promise: ContactEntry) -> str:
    label = f"{format_amount(promise.promised_amount or ZERO)} by {promise.promised_date}"
    if promise.lease is not None:
        label += f" on store {promise.lease.store.external_label}"
    return f"{label} (recorded {promise.happened_on})"


def entry_form(
    operator: Operator, data: QueryDict | None = None, *, lease: Lease | None = None
) -> tuple[ContactEntryForm, dict[str, dict[str, str]]]:
    """The entry form of ``operator`` (PLAN-012 D3, D5) with ``lease`` chosen and locked
    (the only choice, required; PLAN-012c D2), else the one lease in effect preselected
    when there is exactly one; and each open promise's date and amount for the page's
    Rescheduled preselection. The previous promise chosen is the newest open one on the
    chosen lease, else the newest open one."""
    today = timezone.localdate()
    active = list(contacts.active_leases(operator, today))
    others = (
        Lease.objects.filter(operator=operator)
        .exclude(pk__in=[row.pk for row in active])
        .select_related("store")
        .order_by("store__external_label", "id")
    )
    leases = [(str(row.pk), f"Store {row.store.external_label}") for row in active] + [
        (str(row.pk), f"Store {row.store.external_label} (not in effect today)") for row in others
    ]
    if lease is not None:
        # The store page's form (PLAN-012c D2): its lease is the only choice, and required.
        leases = [choice for choice in leases if choice[0] == str(lease.pk)]
    promises = list(contacts.open_promises(operator))
    chosen = lease or (active[0] if len(active) == 1 else None)
    previous = next(
        (row for row in promises if chosen is not None and row.lease_id == chosen.pk),
        promises[0] if promises else None,
    )
    form = ContactEntryForm(
        data,
        leases=leases,
        lease_required=lease is not None or len(active) > 1,
        promises=[(str(row.pk), _promise_label(row)) for row in promises],
        initial={
            "happened_on": today,
            "kind": ContactKind.CALL,
            "lease": str(chosen.pk) if chosen is not None else "",
            "previous": str(previous.pk) if previous is not None else "",
        },
    )
    known = {
        str(row.pk): {"date": str(row.promised_date or ""), "amount": str(row.promised_amount)}
        for row in promises
    }
    return form, known


def supersede_warnings(operator: Operator) -> dict[str, str]:
    """PLAN-012c-b D3: per lease (``""``: no lease), the sentence the add form shows when a
    new promise there would close the open promise as Rescheduled."""
    warnings: dict[str, str] = {}
    for row in contacts.open_promises(operator).order_by("-created_at", "-id"):
        where = "on this store" if row.lease_id is not None else "without a store"
        warnings.setdefault(
            str(row.lease_id or ""),
            f"Saving a promise closes the open promise of "
            f"{format_amount(row.promised_amount or ZERO)} by {row.promised_date} {where} "
            "as Rescheduled.",
        )
    return warnings


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value or "")
    except ValueError:
        return None


def _add(
    request: HttpRequest,
    operator: Operator,
    *,
    lease: Lease | None,
    back_url: str,
    title: str,
    done_url: str = "",
    context: StoreContext | None = None,
) -> HttpResponse:
    """The add form; a saved entry returns to ``done_url``, else to the log on
    ``back_url``. Opened from the store page (``context``, PLAN-012d D2) the lease field
    is hidden and the read-only line names it."""
    data = request.POST if request.method == "POST" else None
    form, known = entry_form(operator, data, lease=lease)
    if context is not None and lease is not None and "lease" in form.fields:
        form.fields["lease"].widget = forms.HiddenInput()
    if data is not None and form.is_valid():
        values = form.cleaned_data
        outcome = contacts.add_entry(
            operator.pk,
            contacts.ContactInput(
                kind=values["kind"],
                happened_on=values["happened_on"],
                body=values["body"],
                promised_date=values.get("promised_date"),
                promised_amount=values.get("promised_amount"),
                lease=_uuid(values.get("lease")),
                previous=_uuid(values.get("previous")),
                outcome=values.get("outcome") or "",
            ),
            actor=_actor(request),
        )
        if outcome.ok:
            messages.success(request, outcome.message)
            return redirect(done_url or f"{back_url}#contacts")
        form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
    if data is not None:
        drop_payment_data(form)
    warnings = supersede_warnings(operator)
    chosen = data.get("lease", "") if data is not None else form.initial.get("lease", "")
    return render(
        request,
        "ledger/operators/contact_form.html",
        {
            "operator": operator,
            "form": form,
            "promise_values": known,
            "supersede_warning": warnings.get(str(chosen), ""),
            "supersede_warnings": warnings,
            "back_url": back_url,
            "title": title,
            "store_context": context,
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def contacts_add(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    operator = _load(pk)
    if operator.archived_at is not None:
        messages.error(request, contacts.ARCHIVED)
        return redirect("ledger:operators:detail", pk=operator.pk)
    return _add(
        request,
        operator,
        lease=None,
        back_url=reverse("ledger:operators:detail", kwargs={"pk": operator.pk}),
        title=operator.display_name,
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def store_contacts_add(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    store = _store(pk)
    context = store_context.from_request(request, store=store)
    tab = workspace.tab_url(store.pk, workspace.TAB_CONTACTS)
    today = timezone.localdate()
    lease = contacts.store_contact_lease(store, today)
    if lease is None or lease.operator is None:
        messages.error(request, NO_STORE_LEASE)
        return redirect(tab)
    if context is not None:
        # The entry is about the store's contact lease whatever lease the link named.
        context = StoreContext(store=store, lease=lease)
    return _add(
        request,
        lease.operator,
        lease=lease,
        back_url=reverse("ledger:stores:detail", kwargs={"pk": store.pk}),
        title=f"{lease.operator.display_name}, store {store.external_label}"
        f"{_lease_state(lease, today)}",
        # PLAN-012d D8: back to the store page's Contact log tab.
        done_url=tab,
        context=context,
    )


def _lease_state(lease: Lease, today: date) -> str:
    """The store form's title suffix (PLAN-012c D2): ``""`` for a lease in effect on
    ``today``; else ", lease ended YYYY-MM-DD" (ended, or an end before ``today``; the date
    when known), else ", lease pending" (prospective, or a start after ``today``). A paused
    or active lease outside those is in effect, so it takes no suffix."""
    if contacts.store_leases(lease.store, today).filter(pk=lease.pk).exists():
        return ""
    if lease.collection_status == CollectionStatus.ENDED or (
        lease.ended_on is not None and lease.ended_on < today
    ):
        return f", lease ended {lease.ended_on.isoformat()}" if lease.ended_on else ", lease ended"
    return ", lease pending"


# --- Edit, correct, delete ------------------------------------------------------------------


def _entry(pk: str) -> ContactEntry:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(
        ContactEntry.objects.select_related("operator", "author", "lease__store"), pk=key
    )


def _log_url(entry: ContactEntry) -> str:
    page = reverse("ledger:operators:detail", kwargs={"pk": entry.operator_id})
    return f"{page}#contact-{entry.pk}"


def _entry_page(
    request: HttpRequest, template: str, entry: ContactEntry, form: forms.Form
) -> HttpResponse:
    if request.method == "POST":
        drop_payment_data(form)
    return render(
        request,
        template,
        {
            "entry": entry,
            "form": form,
            "back_url": _log_url(entry),
            "edit_until": contacts.edit_closes_at(entry),
        },
    )


def _refused(request: HttpRequest, entry: ContactEntry, message: str) -> HttpResponse:
    messages.error(request, message)
    return redirect(_log_url(entry))


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def contact_edit(request: HttpRequest, pk: str) -> HttpResponseBase:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    entry = _entry(pk)
    actor = _actor(request)
    if entry.author_id != actor.pk:
        return forbidden(request)
    if entry.deleted_at is not None:
        return _refused(request, entry, contacts.DELETED)
    if not contacts.can_edit(entry, actor, timezone.now()):
        return _refused(request, entry, contacts.WINDOW_CLOSED)
    if request.method != "POST":
        form = ContactEditForm(
            initial={
                "kind": entry.kind,
                "happened_on": entry.happened_on,
                "promised_date": entry.promised_date,
                "promised_amount": entry.promised_amount,
                "body": entry.body,
            }
        )
        return _entry_page(request, "ledger/contacts/edit.html", entry, form)
    form = ContactEditForm(request.POST)
    if form.is_valid():
        values = form.cleaned_data
        outcome = contacts.edit_entry(
            entry,
            contacts.EditInput(
                kind=values["kind"],
                happened_on=values["happened_on"],
                body=values["body"],
                promised_date=values.get("promised_date"),
                promised_amount=values.get("promised_amount"),
            ),
            actor=actor,
            now=timezone.now(),
        )
        if outcome.ok:
            messages.success(request, outcome.message)
            return redirect(_log_url(entry))
        if outcome.field is None:
            return _refused(request, entry, outcome.message)
        form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
    return _entry_page(request, "ledger/contacts/edit.html", entry, form)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def contact_correct(request: HttpRequest, pk: str) -> HttpResponseBase:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    entry = _entry(pk)
    if entry.deleted_at is not None:
        return _refused(request, entry, contacts.DELETED)
    if request.method != "POST":
        return _entry_page(request, "ledger/contacts/correct.html", entry, ContactCorrectionForm())
    form = ContactCorrectionForm(request.POST)
    if form.is_valid():
        outcome = contacts.add_correction(entry, form.cleaned_data["body"], actor=_actor(request))
        if outcome.ok:
            messages.success(request, outcome.message)
            return redirect(_log_url(entry))
        if outcome.field is None:
            return _refused(request, entry, outcome.message)
        form.add_error("body", outcome.message)
    return _entry_page(request, "ledger/contacts/correct.html", entry, form)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def contact_delete(request: HttpRequest, pk: str) -> HttpResponseBase:
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    entry = _entry(pk)
    if entry.deleted_at is not None:
        return _refused(request, entry, contacts.DELETED)
    if request.method != "POST":
        return _entry_page(request, "ledger/contacts/delete.html", entry, ContactDeleteForm())
    form = ContactDeleteForm(request.POST)
    if form.is_valid():
        outcome = contacts.delete_entry(entry, form.cleaned_data["reason"], actor=_actor(request))
        if outcome.ok:
            messages.success(request, outcome.message)
            return redirect(_log_url(entry))
        if outcome.field is None:
            return _refused(request, entry, outcome.message)
        form.add_error("reason", outcome.message)
    return _entry_page(request, "ledger/contacts/delete.html", entry, form)


# --- Kept, Reschedule, Broken on one promise (PLAN-012c-b D2) -------------------------------


def _outcome_leases(promise: ContactEntry) -> list[tuple[str, str]]:
    """The store choice of the outcome form: only for a promise without a store whose
    operator has more than one lease in effect (an entry must then name one)."""
    if promise.lease_id is not None:
        return []
    active = list(contacts.active_leases(promise.operator_id, timezone.localdate()))
    if len(active) < 2:
        return []
    return [(str(row.pk), f"Store {row.store.external_label}") for row in active]


OUTCOME_TITLES = {
    ContactStatus.KEPT.value: "Kept",
    ContactStatus.RESCHEDULED.value: "Reschedule",
    ContactStatus.BROKEN.value: "Broken",
}


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def contact_outcome(request: HttpRequest, pk: str, outcome: str) -> HttpResponseBase:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    if outcome not in contacts.OUTCOMES:
        raise Http404
    promise = _entry(pk)
    if promise.kind != ContactKind.PROMISE:
        raise Http404
    back = local_next(request.POST.get("next") or request.GET.get("next")) or reverse(
        "ledger:operators:detail", kwargs={"pk": promise.operator_id}
    )
    if promise.operator.archived_at is not None:
        messages.error(request, contacts.ARCHIVED)
        return redirect(f"{back}#contacts")
    if promise.deleted_at is not None:
        messages.error(request, contacts.DELETED)
        return redirect(f"{back}#contacts")
    if promise.status != ContactStatus.OPEN:
        messages.error(request, contacts.PROMISE_NOT_OPEN)
        return redirect(f"{back}#contact-{promise.pk}")
    leases = _outcome_leases(promise)
    if request.method != "POST":
        form = ContactOutcomeForm(
            outcome=outcome,
            leases=leases,
            initial={
                "kind": ContactKind.CALL,
                "happened_on": timezone.localdate(),
                "promised_date": promise.promised_date,
                "promised_amount": promise.promised_amount,
            },
        )
    else:
        form = ContactOutcomeForm(request.POST, outcome=outcome, leases=leases)
        if form.is_valid():
            values = form.cleaned_data
            added = contacts.add_entry(
                promise.operator_id,
                contacts.ContactInput(
                    kind=(
                        ContactKind.PROMISE
                        if outcome == ContactStatus.RESCHEDULED
                        else values["kind"]
                    ),
                    happened_on=values["happened_on"],
                    body=values["body"],
                    promised_date=values.get("promised_date"),
                    promised_amount=values.get("promised_amount"),
                    lease=promise.lease_id or _uuid(values.get("lease")),
                    previous=promise.pk,
                    outcome=outcome,
                ),
                actor=_actor(request),
            )
            if added.ok and added.entry is not None:
                messages.success(request, added.message)
                return redirect(f"{back}#contact-{added.entry.pk}")
            if added.field in (None, "previous"):
                messages.error(request, added.message)
                return redirect(f"{back}#contact-{promise.pk}")
            form.add_error(added.field if added.field in form.fields else None, added.message)
        drop_payment_data(form)
    return render(
        request,
        "ledger/contacts/outcome.html",
        {
            "entry": promise,
            "form": form,
            "outcome": outcome,
            "action": OUTCOME_TITLES[outcome],
            "back_url": back,
        },
    )
