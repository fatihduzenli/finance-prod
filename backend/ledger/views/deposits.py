"""Resolving a deposit from the lease page (docs/qa/PLAN-010b D7, D12).

``ledger:leases:resolve_deposit`` needs ``admin``, GET and POST; the registry checks the
POST first. The deposit must be a setup charge of the lease in the URL (404 otherwise).
:mod:`ledger.services.deposits` re-checks every rule under the deposit's row lock and
writes the audit row. Nothing calls the payment provider. Every POST ends in a redirect;
a form with errors is held in the session for the next GET.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import DepositResolutionForm
from ledger.models import Lease, SetupCharge
from ledger.services import deposits as service
from ledger.services import setup_charges
from ledger.views.operators import _actor, _held, _hold, _role, role_required


def _load(pk: str, charge_pk: str) -> SetupCharge:
    try:
        lease_id, charge_id = uuid.UUID(pk), uuid.UUID(charge_pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(
        SetupCharge.objects.select_related("lease", "lease__store", "lease__operator", "fee_type"),
        pk=charge_id,
        lease_id=lease_id,
    )


def _targets(charge: SetupCharge) -> list[tuple[str, str]]:
    return [(str(lease.pk), service.lease_label(lease)) for lease in service.target_leases(charge)]


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def resolve_deposit(request: HttpRequest, pk: str, charge: str) -> HttpResponse:
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    deposit = _load(pk, charge)
    lease = deposit.lease
    target = f"resolve-deposit:{deposit.pk}"
    targets = _targets(deposit)
    if request.method == "POST":
        form = DepositResolutionForm(request.POST, targets=targets)
        if form.is_valid():
            data = form.cleaned_data
            to_lease = None
            if data.get("to_lease"):
                to_lease = Lease.objects.filter(pk=data["to_lease"]).first()
                if to_lease is None:
                    form.add_error("to_lease", service.TARGET_OTHER_OPERATOR)
            if not form.errors:
                outcome = service.resolve_deposit(
                    deposit,
                    data["kind"],
                    data["amount"],
                    data["resolved_on"],
                    data.get("method") or "",
                    data["note"],
                    to_lease,
                    _actor(request),
                )
                if outcome.ok:
                    messages.success(request, outcome.message)
                    return redirect("ledger:setup_charges:detail", pk=deposit.pk)
                field = outcome.field if outcome.field in form.fields else None
                form.add_error(field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:leases:resolve_deposit", pk=lease.pk, charge=deposit.pk)
    form = _held(
        request, target, lambda data: DepositResolutionForm(data, targets=targets)
    ) or DepositResolutionForm(
        targets=targets,
        initial={"amount": setup_charges.settled(deposit), "resolved_on": timezone.localdate()},
    )
    return render(
        request,
        "ledger/deposits/resolve.html",
        {
            "form": form,
            "charge": deposit,
            "lease": lease,
            "paid": setup_charges.settled(deposit),
            "can_resolve": service.can_resolve(deposit),
        },
    )
