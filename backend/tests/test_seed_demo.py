"""seed_demo: local only, idempotent, and covers PLAN-004 §4.5 (§6 row 10)."""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Count
from django.test import override_settings

from ledger import models as ledger
from ledger.management.commands.seed_demo import DEMO_PROVIDER_STATUSES
from ledger.validators import validate_json_has_no_payment_data, validate_no_payment_data
from payments.models import ExternalRef

pytestmark = pytest.mark.django_db

SEEDED_APPS = ("ledger", "payments")

DEPLOYED_ENV = {
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "STAX_API_KEY": "FAKE-TEST-KEY-do-not-use-XYZ123",
    "STAX_WEBHOOK_URL_TOKEN": "t" * 64,
    # Unreachable on purpose: a refusal must happen before any database access.
    "DATABASE_URL": "postgres://finance@db.example.com:5432/finance",
    "APP_BASE_URL": "https://staging.example.com",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}


def _seed() -> str:
    out = StringIO()
    call_command("seed_demo", stdout=out)
    return out.getvalue()


def _snapshot() -> dict[str, list[dict[str, Any]]]:
    """Every seeded row, every column, ordered by primary key."""
    return {
        model._meta.db_table: list(model._default_manager.order_by("pk").values())
        for label in SEEDED_APPS
        for model in apps.get_app_config(label).get_models()
    }


def test_counts_match_the_plan() -> None:
    output = _seed()
    assert "seed_demo: demo data is in place" in output

    assert ledger.Operator.objects.count() == 10
    assert ledger.Operator.objects.filter(email__isnull=True).count() == 2
    assert ledger.Store.objects.count() == 14
    assert ledger.Lease.objects.count() == 14
    assert ledger.Lease.objects.filter(second_debit_day__isnull=False).count() == 1
    assert ledger.Lease.objects.filter(collection_status="paused").count() == 1
    assert ledger.Lease.objects.filter(payment_channel="wire").count() == 1
    assert ledger.LeaseRentHistory.objects.exists()
    premiums = dict(ledger.InsurancePremium.objects.values_list("policy_year", "amount"))
    assert premiums == {2025: Decimal("595.00"), 2026: Decimal("695.00")}
    assert ledger.InsuranceCharge.objects.filter(policy_year=2025).exists()
    assert ledger.InsuranceCharge.objects.filter(policy_year=2026).exists()
    assert ledger.Invoice.objects.count() == 25


def test_every_status_is_covered() -> None:
    _seed()
    invoices = ledger.Invoice.objects
    assert set(invoices.values_list("internal_status", flat=True)) == set(
        ledger.InvoiceStatus.values
    )
    assert set(invoices.values_list("provider_status", flat=True)) == set(DEMO_PROVIDER_STATUSES)
    charge_statuses = set(ledger.RentCharge.objects.values_list("status", flat=True))
    assert charge_statuses == set(ledger.RentChargeStatus.values)


def test_payment_scenarios_are_present() -> None:
    _seed()
    payments = ledger.Payment.objects
    partial = payments.filter(rent_charge__status="partial", is_refund=False)
    assert partial.exists()
    for payment in partial:
        assert payment.rent_charge is not None
        assert Decimal("0") < payment.amount < payment.rent_charge.amount_due

    returned = payments.filter(status="returned")
    assert returned.count() == 1
    assert returned.get().method == "ach" and returned.get().return_code

    zelle = payments.filter(method="zelle")
    assert zelle.count() == 1 and zelle.get().note.strip()

    refunds = payments.filter(is_refund=True)
    assert refunds.count() == 1
    assert refunds.get().parent_payment is not None

    adjustments = ledger.ChargeAdjustment.objects.filter(reason="cross_store_balance")
    assert adjustments.count() == 1
    adjustment = adjustments.get()
    assert adjustment.from_charge is not None
    from_lease, to_lease = adjustment.from_charge.lease, adjustment.to_charge.lease
    assert from_lease.operator_id == to_lease.operator_id
    assert from_lease.store_id != to_lease.store_id


def test_second_run_changes_nothing() -> None:
    _seed()
    before = _snapshot()
    users_before = User.objects.count()
    _seed()
    assert _snapshot() == before
    assert User.objects.count() == users_before


def test_invented_data_only() -> None:
    _seed()
    emails = [e for e in ledger.Operator.objects.values_list("email", flat=True) if e]
    assert emails and all(email.endswith("@example.com") for email in emails)
    assert all(
        u.email.endswith("@example.com") for u in User.objects.filter(username__contains="seed")
    )
    external_ids = list(ExternalRef.objects.values_list("external_id", flat=True))
    assert external_ids and all(value.startswith("demo-") for value in external_ids)
    assert not ExternalRef.objects.values("external_id").annotate(n=Count("id")).filter(n__gt=1)


def test_no_seeded_text_trips_the_guard() -> None:
    _seed()
    checked = 0
    for label in SEEDED_APPS:
        for model in apps.get_app_config(label).get_models():
            for field in model._meta.concrete_fields:
                if validate_no_payment_data in field.validators:
                    validator: Any = validate_no_payment_data
                elif validate_json_has_no_payment_data in field.validators:
                    validator = validate_json_has_no_payment_data
                else:
                    continue
                for value in model._default_manager.values_list(field.attname, flat=True):
                    validator(value)
                    checked += 1
    assert checked > 100


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_refused_in_process_outside_local(app_env: str) -> None:
    with override_settings(APP_ENV=app_env), pytest.raises(CommandError) as raised:
        call_command("seed_demo", stdout=StringIO())
    assert raised.value.returncode == 2
    assert not ledger.Operator.objects.exists()


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_refused_with_exit_2_in_a_deployed_process(repo_root: Path, app_env: str) -> None:
    result = subprocess.run(
        [sys.executable, "backend/manage.py", "seed_demo"],
        cwd=repo_root,
        env={**DEPLOYED_ENV, "APP_ENV": app_env, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "APP_ENV=local" in result.stderr
    assert "demo data is in place" not in result.stdout
