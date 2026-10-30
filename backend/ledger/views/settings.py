"""Admin › Settings (docs/qa/PLAN-014 B2, D1, D4): the app's own settings, one card per
section.

Admin only: ``ledger:settings:index`` and ``ledger:settings:templates`` are read-only in
``accounts.registry`` and their views refuse anyone below admin (403); every action is
``RouteRule(ADMIN, POST)`` and its view refuses below admin too (the template edit page's
GET as well). The cards:

- **Brands** (:mod:`ledger.services.brands`): add, rename, make active or inactive, move up
  or down.
- **Mail** (:mod:`ledger.services.mail_settings`, D1): the sender, the Reply-To, the managers'
  list and the sandbox allowlist, and whether mail is set up here (the key's presence only).
- **Message templates** (:mod:`ledger.services.message_templates`, D4): the list, a page per
  template to edit its subject and body with the placeholders it may use, and a Preview
  with a store's real values and sample amounts (nothing queued).

Each action answers with a redirect to the page and its outcome as a message (a refusal
never repeats what was posted), except the template's save refused and the preview, which
show the edit page again with the text as typed.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from ledger.models import TEMPLATE_SUBJECT_MAX_LENGTH, CompanySettings, MessageTemplate
from ledger.services import brands, mail_settings, message_templates
from ledger.services.message_templates import BODY_MAX_LENGTH
from ledger.views.leases import _admin_only
from ledger.views.operators import _actor, role_required

INDEX = "ledger:settings:index"


def _pk_or_404(pk: str) -> uuid.UUID:
    try:
        return uuid.UUID(pk)
    except ValueError:
        raise Http404 from None


def _answer(
    request: HttpRequest,
    outcome: brands.Outcome | mail_settings.Outcome | message_templates.Outcome,
    to: str = INDEX,
) -> HttpResponse:
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(to)


@role_required
@require_safe
def settings_index(request: HttpRequest) -> HttpResponse:
    """``ledger:settings:index`` (admin): the Brands and Mail cards, and the link to the
    message templates."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    company = CompanySettings.read()
    return render(
        request,
        "ledger/settings/index.html",
        {
            "brand_rows": brands.rows(),
            "name_max": brands.NAME_MAX,
            "mail_status": mail_settings.status(),
            "mail": {name: getattr(company, name) for name in mail_settings.FIELDS},
        },
    )


@role_required
@require_POST
def brand_add(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    return _answer(request, brands.add(request.POST.get("name"), _actor(request)))


@role_required
@require_POST
def brand_save(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    return _answer(
        request, brands.rename(_pk_or_404(pk), request.POST.get("name"), _actor(request))
    )


@role_required
@require_POST
def brand_toggle(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    return _answer(request, brands.toggle(_pk_or_404(pk), _actor(request)))


@role_required
@require_POST
def brand_move(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    direction = request.POST.get("direction")
    return _answer(
        request,
        brands.move(
            _pk_or_404(pk), direction if isinstance(direction, str) else "", _actor(request)
        ),
    )


# --- Mail (D1) ------------------------------------------------------------------------------


@role_required
@require_POST
def mail_save(request: HttpRequest) -> HttpResponse:
    """``ledger:settings:mail_save`` (admin, POST): the Mail card's four fields."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    outcome = mail_settings.save(request.POST, _actor(request))
    return _answer(request, outcome, f"{reverse(INDEX)}#mail")


# --- Message templates (D4) -----------------------------------------------------------------


def _template(pk: str) -> MessageTemplate:
    template = message_templates.get(_pk_or_404(pk))
    if template is None:
        raise Http404
    return template


@role_required
@require_safe
def templates_list(request: HttpRequest) -> HttpResponse:
    """``ledger:settings:templates`` (admin): every template with its code, channel and
    state."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    return render(
        request, "ledger/settings/templates.html", {"template_rows": message_templates.rows()}
    )


def _edit_page(
    request: HttpRequest,
    template: MessageTemplate,
    *,
    subject: str,
    body: str,
    preview: message_templates.Preview | None = None,
    store_id: str = "",
    status: int = 200,
) -> HttpResponse:
    return render(
        request,
        "ledger/settings/template_edit.html",
        {
            "template": template,
            "subject": subject,
            "body": body,
            "placeholders": message_templates.placeholders(template),
            "stores": message_templates.preview_stores(),
            "store_id": store_id,
            "preview": preview,
            "subject_max": TEMPLATE_SUBJECT_MAX_LENGTH,
            "body_max": BODY_MAX_LENGTH,
        },
        status=status,
    )


def _posted(request: HttpRequest, name: str) -> str:
    value = request.POST.get(name, "")
    return value if isinstance(value, str) else ""


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def template_edit(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:settings:template_edit`` (admin): the template's subject and body; a POST
    saves them (audited) or shows the page again with the refusal and the text typed."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    template = _template(pk)
    if request.method != "POST":
        return _edit_page(request, template, subject=template.subject, body=template.body)
    subject, body = _posted(request, "subject"), _posted(request, "body")
    outcome = message_templates.save(template.pk, subject, body, _actor(request))
    if not outcome.ok:
        messages.error(request, outcome.message)
        return _edit_page(request, template, subject=subject, body=body, status=400)
    messages.success(request, outcome.message)
    return redirect("ledger:settings:template_edit", pk=template.pk)


@role_required
@require_POST
def template_preview(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:settings:template_preview`` (admin, POST): the edit page with the typed text
    rendered for the chosen store's current lease; nothing is saved or queued."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    template = _template(pk)
    subject, body = _posted(request, "subject"), _posted(request, "body")
    store_raw = _posted(request, "store")
    try:
        store_id = uuid.UUID(store_raw) if store_raw else None
    except ValueError:
        store_id = None
    found = message_templates.preview(template, store_id, subject, body, timezone.localdate())
    return _edit_page(
        request,
        template,
        subject=subject,
        body=body,
        preview=found,
        store_id=str(store_id) if store_id is not None else "",
    )
