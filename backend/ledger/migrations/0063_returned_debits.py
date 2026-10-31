"""Returned bank debits (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md F2):
the NSF fee at the return and the automatic re-debit of R01/R09 returns.

Data: the fee type ``nsf_fee_first`` (100.00, seeded inactive by ``0027``) is made active: the
return's fee is charged at that price (``ledger.services.nsf.on_return``). It stays off the
setup checklist and the invoice builder (``NSF_FEE_CODES`` are never offered).

Schema, ``one_off_charges``:

- ``retry_date`` (date, null) and ``retry_state`` (``""``, ``auto_pending``, ``scheduled``,
  ``stopped``, ``done``; CHECK ``one_off_charges_retry_state_valid``): an automatic
  re-debit's date and state. ``one_off_charges_retry_iff_redebit``: both are set together,
  and only on a ``redebit`` charge with a returned payment.
- ``one_off_charges_redebit_one_source`` and ``one_off_charges_companion_iff_nsf_fee`` are
  rebuilt: an ``nsf_fee`` charge may name its returned payment (``source_payment``) instead
  of a re-debit (``companion_of``).
- ``one_off_charges_one_redebit_per_payment`` now reads live re-debits only (a re-debit
  stopped by staff is voided, and staff may then debit again); and
  ``one_off_charges_one_fee_per_returned_payment``: one ``nsf_fee`` charge per returned
  payment, so a replayed return or a later "Debit again" never adds a second fee.

``leases.review_reason`` gains ``returned_debit`` ("Returned debit: new payment method
needed") and ``redebit_limit`` ("Re-debit limit reached (Nacha)"); CHECK
``leases_review_reason_valid`` rebuilt. No text column is added, so no payment-data trigger
changes. On PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this transaction
queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), in either direction.

The reverse refuses (``CANNOT_REVERSE``) while a charge has a retry state, a fee names its
returned payment, a returned payment has more than one re-debit, or a lease has one of the
new review reasons: the schema before it has no place for them. Otherwise it makes
``nsf_fee_first`` inactive again and restores the constraints of ``0062``.
"""

from __future__ import annotations

from typing import Any

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Count

NSF_FEE_FIRST = "nsf_fee_first"
NEW_REVIEW_REASONS = ("returned_debit", "redebit_limit")
CANNOT_REVERSE = (
    "Returned debits of PLAN-014 F exist (a retry state, a fee naming its returned payment, a "
    "second re-debit of a returned payment, or a lease flagged for a returned debit); this "
    "migration refuses to reverse (the schema before it has no place for them)."
)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def activate_fee(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    fee_type: Any = apps.get_model("ledger", "FeeType")
    fee_type.objects.filter(code=NSF_FEE_FIRST).update(active=True)


def deactivate_fee(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    fee_type: Any = apps.get_model("ledger", "FeeType")
    fee_type.objects.filter(code=NSF_FEE_FIRST).update(active=False)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    charge: Any = apps.get_model("ledger", "OneOffCharge")
    lease: Any = apps.get_model("ledger", "Lease")
    found = (
        charge.objects.exclude(retry_state="").exists()
        or charge.objects.filter(category="nsf_fee", source_payment__isnull=False).exists()
        or charge.objects.filter(category="redebit", source_payment__isnull=False)
        .values("source_payment")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
        .exists()
        or lease.objects.filter(review_reason__in=NEW_REVIEW_REASONS).exists()
    )
    if found:
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0062_rent_month_messages"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(
            model_name="lease",
            name="leases_review_reason_valid",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_companion_iff_nsf_fee",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_one_redebit_per_payment",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_redebit_one_source",
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="retry_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="retry_state",
            field=models.CharField(
                blank=True,
                choices=[
                    ("auto_pending", "Automatic re-debit pending"),
                    ("scheduled", "Automatic re-debit scheduled"),
                    ("stopped", "Stopped"),
                    ("done", "Done"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="lease",
            name="review_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("wire", "Pays by wire"),
                    ("dormant", "Dormant"),
                    ("closed", "Closed"),
                    ("unknown", "Unknown"),
                    ("own_customer", "Own processor customer needed"),
                    ("returned_debit", "Returned debit: new payment method needed"),
                    ("redebit_limit", "Re-debit limit reached (Nacha)"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="lease",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "review_reason__in",
                        [
                            "wire",
                            "dormant",
                            "closed",
                            "unknown",
                            "own_customer",
                            "returned_debit",
                            "redebit_limit",
                            "",
                        ],
                    )
                ),
                name="leases_review_reason_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("category", "redebit"),
                        ("source_payment__isnull", False),
                        ("source_rent_charge__isnull", True),
                    ),
                    models.Q(
                        ("category", "redebit"),
                        ("source_payment__isnull", True),
                        ("source_rent_charge__isnull", False),
                    ),
                    models.Q(
                        models.Q(("category", "redebit"), _negated=True),
                        ("source_rent_charge__isnull", True),
                        models.Q(
                            ("source_payment__isnull", True),
                            ("category", "nsf_fee"),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="one_off_charges_redebit_one_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("category", "nsf_fee"),
                        models.Q(
                            ("companion_of__isnull", False),
                            ("source_payment__isnull", False),
                            _connector="OR",
                        ),
                    ),
                    models.Q(
                        models.Q(("category", "nsf_fee"), _negated=True),
                        ("companion_of__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="one_off_charges_companion_iff_nsf_fee",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("category", "redebit"), models.Q(("status", "voided"), _negated=True)
                ),
                fields=("source_payment",),
                name="one_off_charges_one_redebit_per_payment",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.UniqueConstraint(
                condition=models.Q(("category", "nsf_fee")),
                fields=("source_payment",),
                name="one_off_charges_one_fee_per_returned_payment",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("retry_date__isnull", True), ("retry_state", "")),
                    models.Q(
                        models.Q(("retry_state", ""), _negated=True),
                        ("category", "redebit"),
                        ("retry_date__isnull", False),
                        ("source_payment__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="one_off_charges_retry_iff_redebit",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("retry_state__in", ["auto_pending", "scheduled", "stopped", "done", ""])
                ),
                name="one_off_charges_retry_state_valid",
            ),
        ),
        migrations.RunPython(activate_fee, deactivate_fee, elidable=False),
        # Reversed first: refused while a row of this step exists, then any check queued
        # runs before the ALTER TABLE statements.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
