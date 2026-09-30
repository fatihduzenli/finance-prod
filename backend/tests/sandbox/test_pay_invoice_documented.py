"""Sandbox: document what pay-an-invoice does with a stored bank method (PLAN-005 §4.5).

Evidence for the owner's decision on TODO-VERIFY-019. Needs, besides the sandbox
variables, ``STAX_SANDBOX_CUSTOMER_ID``, ``STAX_SANDBOX_PAYMENT_METHOD_ID`` (a sandbox
customer whose bank method staff entered in the dashboard) and
``STAX_PAY_INVOICE_ENABLED=true``. Creates one sandbox invoice for 1.00, pays it once,
and writes the raw invoice status and the transaction status to the test log.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.conf import settings
from django.test import override_settings

from payments.provider import Amounts, InvoiceInput, LineItem, PayWithStoredMethod
from tests.sandbox.conftest import sandbox_provider, sandbox_skip_reason

logger = logging.getLogger(__name__)

_reason = sandbox_skip_reason("STAX_SANDBOX_CUSTOMER_ID", "STAX_SANDBOX_PAYMENT_METHOD_ID")
if _reason is None and not settings.STAX_PAY_INVOICE_ENABLED:
    _reason = "STAX_PAY_INVOICE_ENABLED is not true"
pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.django_db,
    pytest.mark.skipif(_reason is not None, reason=_reason or ""),
]

ONE_DOLLAR = Decimal("1.00")


def test_pay_invoice_outcome_is_documented() -> None:
    assert settings.STAX_ENV == "sandbox"
    today = date.today().isoformat()
    amounts = Amounts(
        total=ONE_DOLLAR,
        subtotal=ONE_DOLLAR,
        tax=Decimal("0.00"),
        line_items=(LineItem("PLAN-005 sandbox pay test", Decimal("1"), ONE_DOLLAR),),
    )
    with override_settings(STAX_CALLER="worker"):
        provider = sandbox_provider()
        invoice = provider.create_invoice(
            InvoiceInput(
                customer_external_id=os.environ["STAX_SANDBOX_CUSTOMER_ID"],
                amounts=amounts,
                memo=f"test_pay_invoice_outcome_is_documented {today}",
            )
        )
        transaction = provider.pay_invoice(
            invoice.external_id,
            PayWithStoredMethod(
                payment_method_external_id=os.environ["STAX_SANDBOX_PAYMENT_METHOD_ID"],
                idempotency_id=str(uuid.uuid4()),
            ),
        )
        after = provider.get_invoice(invoice.external_id)
    logger.warning(
        "documented outcome: invoice raw_status before=%s after=%s; transaction raw_status=%s",
        invoice.raw_status,
        after.raw_status,
        transaction.raw_status,
    )
    assert transaction.external_id
