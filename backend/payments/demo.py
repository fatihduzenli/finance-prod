"""Local-only stand-in for the sync layer, used by the ``seed_demo`` command.

Demo invoices need a ``provider_status`` like the ones the provider returns, and that
column is written only through :mod:`payments.sync_writer`, which is private to this
package. This module is the package's single, local-only entry point for that.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from django.conf import settings

from ledger.models import Invoice
from payments.sync_writer import provider_write


def write_demo_provider_statuses(statuses: Mapping[UUID, str]) -> int:
    """Set each invoice's ``provider_status``; return how many rows changed.

    Refused outside ``APP_ENV=local``. A row that already holds its status is not
    written, so a second call with the same mapping changes nothing.
    """
    if settings.APP_ENV != "local":
        raise RuntimeError("Demo provider statuses are written only when APP_ENV=local.")
    changed = 0
    with provider_write():
        for invoice_id, status in statuses.items():
            changed += (
                Invoice.objects.filter(pk=invoice_id)
                .exclude(provider_status=status)
                .update(provider_status=status)
            )
    return changed
