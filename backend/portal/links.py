"""The portal's links in messages (PLAN-014 H4): ``{pay_link}`` names the portal's Pay page of
the charge's store when the operator has a portal account that can sign in (accepted, not
disabled, the operator not archived); otherwise the messaging engine keeps the signed
statement link. This module reads only the portal's own table, so the messaging engine can
import it without importing the portal's pages."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.urls import reverse

from ledger.models import Operator
from portal.models import PortalUser


def has_account(operator: Operator) -> bool:
    """Whether the operator's portal account can sign in now."""
    if operator.archived_at is not None:
        return False
    return PortalUser.objects.filter(
        operator=operator,
        is_active=True,
        activated_at__isnull=False,
        disabled_at__isnull=True,
    ).exists()


def pay_link(operator: Operator, store_id: uuid.UUID) -> str:
    """The absolute URL of the portal's Pay page of the store, or ``""`` when the operator has
    no portal account that can sign in."""
    if not has_account(operator):
        return ""
    path = reverse("portal:pay", kwargs={"pk": store_id})
    return f"{str(settings.APP_BASE_URL).rstrip('/')}{path}"
