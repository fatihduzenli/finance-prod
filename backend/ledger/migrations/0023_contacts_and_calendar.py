"""The contact log and the staff calendar (docs/qa/PLAN-011b D7, D8).

``contact_entries``: dated calls, e-mails, visits and promises to pay per operator; a
promise carries its date and amount and no other kind does (CHECK). ``calendar_entries``:
shared dated notes and reminders. ``calendar_feeds``: a user's feed token, stored as its
32-byte SHA-256 digest only (binary, CHECKed to 32 bytes), one live feed per user.

The free-text columns are covered by the card-number guard: on PostgreSQL each new table
with free text gets the payment-data trigger of ``0002_postgresql_guards`` (the function
it created is reused). The reverse drops the triggers before the tables go. Nothing is
created on other vendors; the application validator applies there. No existing row
changes.
"""

from __future__ import annotations

import uuid

import django.db.models.deletion
import django.db.models.functions.text
import django.db.models.lookups
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.models
import ledger.validators

#: Table -> guarded columns: every text column without choices.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "contact_entries": ("body",),
    "calendar_entries": ("title", "body"),
}


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


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0022_report_indexes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CalendarEntry",
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
                        choices=[("note", "Note"), ("reminder", "Reminder")], max_length=32
                    ),
                ),
                ("on", models.DateField()),
                (
                    "title",
                    models.CharField(
                        max_length=120, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "body",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("email_morning", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "author",
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
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="calendar_entries",
                        to="ledger.lease",
                    ),
                ),
                (
                    "operator",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="calendar_entries",
                        to="ledger.operator",
                    ),
                ),
                (
                    "store",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="calendar_entries",
                        to="ledger.store",
                    ),
                ),
            ],
            options={
                "db_table": "calendar_entries",
                "indexes": [models.Index(fields=["on"], name="calendar_entries_on_idx")],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("title__regex", "\\S")),
                        name="calendar_entries_title_not_blank",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("kind__in", ["note", "reminder"])),
                        name="calendar_entries_kind_valid",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="CalendarFeed",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("token_hash", models.BinaryField(max_length=32, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="calendar_feeds",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "calendar_feeds",
                "constraints": [
                    models.CheckConstraint(
                        condition=django.db.models.lookups.Exact(
                            django.db.models.functions.text.Length("token_hash"), 32
                        ),
                        name="calendar_feeds_token_hash_size",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("revoked_at__isnull", True)),
                        fields=("user",),
                        name="calendar_feeds_one_live_per_user",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ContactEntry",
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
                        choices=[
                            ("call", "Call"),
                            ("email", "E-mail"),
                            ("visit", "Visit"),
                            ("promise", "Promise to pay"),
                        ],
                        max_length=32,
                    ),
                ),
                ("happened_on", models.DateField()),
                ("promised_date", models.DateField(blank=True, null=True)),
                ("promised_amount", ledger.models.MoneyField(blank=True, null=True)),
                ("body", models.TextField(validators=[ledger.validators.validate_no_payment_data])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "author",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "closed_by",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="closes",
                        to="ledger.contactentry",
                    ),
                ),
                (
                    "operator",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="contact_entries",
                        to="ledger.operator",
                    ),
                ),
            ],
            options={
                "db_table": "contact_entries",
                "indexes": [
                    models.Index(
                        fields=["operator", "happened_on"], name="contact_entries_op_day_idx"
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("body__regex", "\\S")),
                        name="contact_entries_body_not_blank",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("kind__in", ["call", "email", "visit", "promise"])),
                        name="contact_entries_kind_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                models.Q(("kind", "promise"), _negated=True),
                                models.Q(
                                    ("promised_amount__isnull", False),
                                    ("promised_date__isnull", False),
                                ),
                                _connector="OR",
                            ),
                            models.Q(
                                models.Q(("kind__in", ["call", "email", "visit"]), _negated=True),
                                models.Q(
                                    ("promised_amount__isnull", True),
                                    ("promised_date__isnull", True),
                                ),
                                _connector="OR",
                            ),
                        ),
                        name="contact_entries_promise_fields",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("promised_amount__isnull", True),
                            ("promised_amount__gt", 0),
                            _connector="OR",
                        ),
                        name="contact_entries_promised_amount_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("closed_by__isnull", True),
                            models.Q(
                                ("kind", "promise"),
                                models.Q(("closed_by", models.F("id")), _negated=True),
                            ),
                            _connector="OR",
                        ),
                        name="contact_entries_closed_by_promise_only",
                    ),
                ],
            },
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
    ]
