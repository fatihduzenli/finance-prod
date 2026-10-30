"""A store's notes on the Notes & follow-ups tab (docs/qa/PLAN-014 B5).

``ledger:notes:add`` (POST, billing) saves a note from the tab's form; ``ledger:notes:pin``
(POST, billing) pins (``pinned=1``) or unpins it; ``ledger:notes:edit`` and
``ledger:notes:delete`` open their dialog over the store page (GET) and save it (POST,
billing in ``accounts.registry``; the author or an admin, :func:`ledger.services.notes.can_change`).
Each answer goes back to the tab with its outcome as a message; a refused text is shown again
in its form, unless it was refused for holding payment data (the field comes back empty).
"""

from __future__ import annotations

import uuid
from typing import Any

from django import forms
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import NoteDeleteForm, NoteForm
from ledger.models import Note, NoteEntityType, Store
from ledger.services import notes, workspace
from ledger.validators import PAYMENT_DATA_MESSAGE
from ledger.views.operators import _actor, _role, role_required
from ledger.views.stores import _detail_context


def _key(pk: str) -> uuid.UUID:
    try:
        return uuid.UUID(pk)
    except ValueError:
        raise Http404 from None


def _note(pk: str) -> tuple[Note, Store]:
    """A live store note and its store; 404 for any other."""
    note = get_object_or_404(
        Note, pk=_key(pk), entity_type=NoteEntityType.STORE, deleted_at__isnull=True
    )
    return note, get_object_or_404(Store, pk=note.entity_id)


def _tab_url(store: Store, note: Note | None = None) -> str:
    """The store page's Notes & follow-ups tab, at the note when there is one."""
    url = workspace.tab_url(store.pk, workspace.TAB_CONTACTS)
    if note is not None:
        url = url.replace(f"#tab-{workspace.TAB_CONTACTS}", f"#note-{note.pk}")
    return url


def _tab(store: Store, note: Note | None = None) -> HttpResponse:
    return redirect(_tab_url(store, note))


def _emptied(request: HttpRequest, form: forms.Form, name: str) -> None:
    """A text refused for payment data is not shown again."""
    errors = [str(error) for error in form.errors.get(name) or ()]
    if PAYMENT_DATA_MESSAGE in errors or notes.PAYMENT_DATA in errors:
        data = request.POST.copy()
        data[name] = ""
        form.data = data


def _page(request: HttpRequest, store: Store, template: str, extra: dict[str, Any]) -> HttpResponse:
    context = _detail_context(request, store, forced_tab=workspace.TAB_CONTACTS)
    return render(request, template, {**context, **extra})


@role_required
@require_POST
def note_add(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:notes:add`` (billing): a note about the store."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    store = get_object_or_404(Store, pk=_key(pk))
    form = NoteForm(request.POST)
    if form.is_valid():
        try:
            note = notes.add(store, form.cleaned_data["body"], _actor(request))
        except notes.NoteRefused as refused:
            form.add_error("body", refused.message)
        else:
            messages.success(request, notes.ADDED)
            return _tab(store, note)
    _emptied(request, form, "body")
    return _page(request, store, "ledger/stores/detail.html", {"note_form": form})


@role_required
@require_POST
def note_pin(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:notes:pin`` (billing): ``pinned=1`` pins the note, anything else unpins it."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    note, store = _note(pk)
    try:
        if request.POST.get("pinned") == "1":
            notes.pin(note, _actor(request))
            messages.success(request, notes.PINNED)
        else:
            notes.unpin(note, _actor(request))
            messages.success(request, notes.UNPINNED)
    except notes.NoteRefused as refused:
        messages.error(request, refused.message)
    return _tab(store, note)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def note_edit(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:notes:edit``: the Edit note dialog over the store page (the author or an
    admin)."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    note, store = _note(pk)
    if not notes.can_change(note, _actor(request)):
        return forbidden(request)
    if request.method != "POST":
        form = NoteForm(initial={"body": note.body})
    else:
        form = NoteForm(request.POST)
        if form.is_valid():
            body = form.cleaned_data["body"]
            try:
                notes.edit(note, body, _actor(request))
            except notes.NoteRefused as refused:
                form.add_error("body", refused.message)
            else:
                messages.success(request, notes.UNCHANGED if body == note.body else notes.EDITED)
                return _tab(store, note)
        _emptied(request, form, "body")
    return _page(
        request,
        store,
        "ledger/notes/edit.html",
        {"edited_note": note, "edit_form": form, "note_back_url": _tab_url(store, note)},
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def note_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:notes:delete``: the Delete note dialog over the store page (the author or an
    admin); the reason is required."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    note, store = _note(pk)
    if not notes.can_change(note, _actor(request)):
        return forbidden(request)
    if request.method != "POST":
        form = NoteDeleteForm()
    else:
        form = NoteDeleteForm(request.POST)
        if form.is_valid():
            try:
                notes.delete(note, form.cleaned_data["reason"], _actor(request))
            except notes.NoteRefused as refused:
                form.add_error("reason", refused.message)
            else:
                messages.success(request, notes.DELETED)
                return _tab(store)
        _emptied(request, form, "reason")
    return _page(
        request,
        store,
        "ledger/notes/delete.html",
        {"deleted_note": note, "delete_form": form, "note_back_url": _tab_url(store, note)},
    )
