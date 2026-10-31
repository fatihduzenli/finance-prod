"""A portal user's own payments (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md
H4): ``portal_payments`` (the user, the lease, the amount confirmed, the Pay page's one-time
``creation_key``, unique, and the state: requested, paid, failed or refused; CHECKs on the state
and on a positive amount) and ``portal_payment_charges`` (one row per charge paid: its kind and
id, the one-off charge the debit runs on, the amount, the ``portal_pay`` job and the row's own
state; one row per charge of a payment). No free-text column, so no card-number trigger.

The reverse refuses while a portal payment exists (they are the record of what an operator
asked to be debited); on empty tables it drops both.
"""

import uuid

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.models

CANNOT_REVERSE = "Portal payments exist; this migration refuses to reverse."


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    return None


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if apps.get_model("portal", "PortalPayment").objects.exists():
        raise RuntimeError(CANNOT_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0064_portal_messages"),
        ("payments", "0022_portal_pay_job"),
        ("portal", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="PortalPayment",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("amount", ledger.models.MoneyField()),
                ("creation_key", models.UUIDField(unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("requested", "Requested"),
                            ("paid", "Submitted"),
                            ("failed", "Failed"),
                            ("refused", "Refused"),
                        ],
                        default="requested",
                        max_length=9,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "lease",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.lease",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payments",
                        to="portal.portaluser",
                    ),
                ),
            ],
            options={
                "db_table": "portal_payments",
            },
        ),
        migrations.CreateModel(
            name="PortalPaymentCharge",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("rent", "Rent"), ("one_off", "One-off")], max_length=7
                    ),
                ),
                ("charge_id", models.UUIDField()),
                ("amount", ledger.models.MoneyField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("requested", "Requested"),
                            ("paid", "Submitted"),
                            ("failed", "Failed"),
                            ("refused", "Refused"),
                        ],
                        default="requested",
                        max_length=9,
                    ),
                ),
                (
                    "debit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.oneoffcharge",
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="payments.job",
                    ),
                ),
                (
                    "payment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="charges",
                        to="portal.portalpayment",
                    ),
                ),
            ],
            options={
                "db_table": "portal_payment_charges",
            },
        ),
        migrations.AddIndex(
            model_name="portalpayment",
            index=models.Index(fields=["user", "created_at"], name="portal_payments_user_idx"),
        ),
        migrations.AddConstraint(
            model_name="portalpayment",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["requested", "paid", "failed", "refused"])),
                name="portal_payments_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="portalpayment",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount__gt", 0)), name="portal_payments_amount_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="portalpaymentcharge",
            constraint=models.UniqueConstraint(
                fields=("payment", "kind", "charge_id"), name="portal_payment_charges_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="portalpaymentcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ["rent", "one_off"])),
                name="portal_payment_charges_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="portalpaymentcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ("requested", "paid", "failed"))),
                name="portal_payment_charges_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="portalpaymentcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount__gt", 0)), name="portal_payment_charges_amount_positive"
            ),
        ),
        # Last forwards, so first in reverse: the refusal runs before any table is dropped.
        migrations.RunPython(forwards, backwards, elidable=False),
    ]
