"""The Bank deposits tables (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md
I1): ``bank_connections`` (the linked business account, one active row: Plaid's ids, the
institution, the mask, the Fernet-encrypted access token, the cursor; CHECKs on the mask's
shape and "active has a token"), ``bank_transactions`` (one credit per ``external_id``, its
kind, state, confidence and suggestion; the ``allocation_group`` unique while set, so a
deposit is applied once; CHECKs tying the state to its stamps and reasons) and
``sender_aliases`` (a sender's name for a lease, unique per lease once normalised).

On PostgreSQL the free-text columns get the card-number trigger of
``ledger.0002_postgresql_guards`` (its function is reused); the identifiers (Plaid's ids, the
cursor, the ciphertext) are plain columns, as PLAN-004 §4.6 point 1 has them.

The reverse refuses (``CANNOT_REVERSE``) while a connection, a transaction or an alias exists:
the bank's history and the record of what each deposit paid would be lost with the tables.
Otherwise it drops the triggers, then the tables.
"""

import uuid
from typing import Any

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.models
import ledger.validators

#: Table -> guarded columns: every text column without choices that is not an identifier.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "bank_connections": ("institution", "last_error"),
    "bank_transactions": ("description", "counterparty", "ignore_note"),
    "sender_aliases": ("text", "normalised"),
}
CANNOT_REVERSE = (
    "Bank deposits of PLAN-014 I exist (a bank connection, a bank transaction or a sender "
    "alias); this migration refuses to reverse (the bank's history and what each deposit paid "
    "would be lost)."
)


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        arguments = ", ".join(f"'{column}'" for column in columns)
        schema_editor.execute(
            f"CREATE TRIGGER {table}_payment_data_guard "
            f"BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
            params=None,
        )


def drop_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in GUARDED_COLUMNS:
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
        )


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    names = ("BankConnection", "BankTransaction", "SenderAlias")
    models_: list[Any] = [apps.get_model("banking", name) for name in names]
    if any(model.objects.exists() for model in models_):
        raise RuntimeError(CANNOT_REVERSE)


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("ledger", "0065_bank_deposit_settings"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BankConnection",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("item_id", models.CharField(max_length=128)),
                ("account_id", models.CharField(max_length=128)),
                (
                    "institution",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=200,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("mask", models.CharField(blank=True, default="", max_length=4)),
                ("access_token_encrypted", models.TextField(blank=True, default="")),
                ("cursor", models.TextField(blank=True, default="")),
                (
                    "source",
                    models.CharField(
                        choices=[("plaid", "Plaid"), ("fake", "Fake")],
                        default="plaid",
                        max_length=32,
                    ),
                ),
                ("linked_at", models.DateTimeField()),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                (
                    "last_error",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=100,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("active", models.BooleanField(default=True)),
                ("disconnected_at", models.DateTimeField(blank=True, null=True)),
                (
                    "linked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "bank_connections",
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("active", True)),
                        fields=("active",),
                        name="bank_connections_one_active",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("mask__regex", "^[0-9A-Za-z]{0,4}$")),
                        name="bank_connections_mask_shape",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("active", False),
                            ("access_token_encrypted__regex", "\\S"),
                            _connector="OR",
                        ),
                        name="bank_connections_active_has_token",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("source__in", ["plaid", "fake"])),
                        name="bank_connections_source_valid",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="BankTransaction",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "source",
                    models.CharField(choices=[("plaid", "Plaid"), ("fake", "Fake")], max_length=32),
                ),
                ("external_id", models.CharField(max_length=128, unique=True)),
                ("posted_on", models.DateField()),
                ("amount", ledger.models.MoneyField()),
                (
                    "description",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=500,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "counterparty",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=200,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("wire", "Wire"),
                            ("zelle", "Zelle"),
                            ("ach_credit", "ACH credit"),
                            ("check", "Check"),
                            ("other", "Other"),
                        ],
                        default="other",
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("new", "New"),
                            ("confirmed", "Confirmed"),
                            ("split", "Split"),
                            ("ignored", "Ignored"),
                        ],
                        default="new",
                        max_length=32,
                    ),
                ),
                (
                    "confidence",
                    models.CharField(
                        choices=[
                            ("exact", "Exact"),
                            ("strong", "Strong"),
                            ("weak", "Weak"),
                            ("none", "None"),
                        ],
                        default="none",
                        max_length=32,
                    ),
                ),
                (
                    "suggested_charge_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("rent", "Rent"),
                            ("insurance", "Insurance"),
                            ("one_off", "One-off"),
                        ],
                        default="",
                        max_length=32,
                    ),
                ),
                ("suggested_charge_id", models.UUIDField(blank=True, null=True)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("ignored_at", models.DateTimeField(blank=True, null=True)),
                (
                    "ignore_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("internal_transfer", "Internal transfer"),
                            ("not_rent", "Not rent"),
                            ("other", "Other"),
                        ],
                        default="",
                        max_length=32,
                    ),
                ),
                (
                    "ignore_note",
                    models.TextField(
                        blank=True,
                        default="",
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("allocation_group", models.UUIDField(blank=True, null=True)),
                ("undone_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "confirmed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "ignored_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "suggested_lease",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.lease",
                    ),
                ),
            ],
            options={
                "db_table": "bank_transactions",
                "indexes": [
                    models.Index(
                        fields=["status", "posted_on"], name="bank_transactions_status_idx"
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("amount__gt", 0)),
                        name="bank_transactions_amount_positive",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("allocation_group__isnull", False)),
                        fields=("allocation_group",),
                        name="bank_transactions_allocation_group_once",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("allocation_group__isnull", False),
                                ("confirmed_at__isnull", False),
                                ("status__in", ("confirmed", "split")),
                            ),
                            models.Q(
                                models.Q(("status__in", ("confirmed", "split")), _negated=True),
                                ("allocation_group__isnull", True),
                                ("confirmed_at__isnull", True),
                            ),
                            _connector="OR",
                        ),
                        name="bank_transactions_applied_has_group",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("status", "ignored"),
                                ("ignored_at__isnull", False),
                                models.Q(("ignore_reason", ""), _negated=True),
                            ),
                            models.Q(
                                models.Q(("status", "ignored"), _negated=True),
                                ("ignore_reason", ""),
                                ("ignored_at__isnull", True),
                            ),
                            _connector="OR",
                        ),
                        name="bank_transactions_ignored_has_reason",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("ignore_reason", "other"), _negated=True),
                            ("ignore_note__regex", "\\S"),
                            _connector="OR",
                        ),
                        name="bank_transactions_other_reason_has_note",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("suggested_charge_id__isnull", True), ("suggested_charge_type", "")
                            ),
                            models.Q(
                                models.Q(("suggested_charge_type", ""), _negated=True),
                                ("suggested_charge_id__isnull", False),
                            ),
                            _connector="OR",
                        ),
                        name="bank_transactions_suggested_charge_pair",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("source__in", ["plaid", "fake"])),
                        name="bank_transactions_source_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("kind__in", ["wire", "zelle", "ach_credit", "check", "other"])
                        ),
                        name="bank_transactions_kind_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("status__in", ["new", "confirmed", "split", "ignored"])
                        ),
                        name="bank_transactions_status_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("confidence__in", ["exact", "strong", "weak", "none"])),
                        name="bank_transactions_confidence_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("suggested_charge_type__in", ["rent", "insurance", "one_off", ""])
                        ),
                        name="bank_transactions_suggested_charge_type_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("ignore_reason__in", ["internal_transfer", "not_rent", "other", ""])
                        ),
                        name="bank_transactions_ignore_reason_valid",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="SenderAlias",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "text",
                    models.CharField(
                        max_length=200, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "normalised",
                    models.CharField(
                        max_length=200, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "lease",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sender_aliases",
                        to="ledger.lease",
                    ),
                ),
            ],
            options={
                "db_table": "sender_aliases",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("lease", "normalised"), name="sender_aliases_unique_per_lease"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("normalised__regex", "\\S")),
                        name="sender_aliases_normalised_not_blank",
                    ),
                ],
            },
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
