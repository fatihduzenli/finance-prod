"""The invoice builder (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md D6).

``invoice_drafts`` and ``invoice_draft_lines`` hold an invoice being prepared, local only
until it is sent. ``invoice_line_items`` gains ``one_off_charge``: on an invoice made by the
builder each line names the charge it bills, and the lines' order is the order in which a
payment on the invoice pays them. No ``kind`` column is added to ``invoice_line_items``
(schema decision S1 is the owner's). No existing row changes.

Free text is guarded: on PostgreSQL ``invoice_drafts.memo`` and
``invoice_draft_lines.description`` get the payment-data trigger of
``0002_postgresql_guards``. The reverse drops the triggers. Nothing is created on other
vendors; the application validator applies there.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import django.db.models.deletion
import django.db.models.expressions
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.models
import ledger.validators

#: Table -> guarded columns.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "invoice_drafts": ("memo",),
    "invoice_draft_lines": ("description",),
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
        ("ledger", "0028_contact_log_maturity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InvoiceDraft",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "memo",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("due_date", models.DateField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("draft", "Draft"), ("sent", "Sent"), ("cancelled", "Cancelled")],
                        default="draft",
                        max_length=32,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "invoice_drafts",
            },
        ),
        migrations.CreateModel(
            name="InvoiceDraftLine",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("position", models.IntegerField()),
                (
                    "description",
                    models.CharField(
                        max_length=120, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("quantity", ledger.models.StrictDecimalField(decimal_places=4, max_digits=12)),
                ("unit_amount", ledger.models.MoneyField()),
                ("line_total", ledger.models.MoneyField()),
            ],
            options={
                "db_table": "invoice_draft_lines",
                "ordering": ("position",),
            },
        ),
        migrations.AddField(
            model_name="invoicelineitem",
            name="one_off_charge",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invoice_lines",
                to="ledger.oneoffcharge",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicelineitem",
            constraint=models.UniqueConstraint(
                condition=models.Q(("one_off_charge__isnull", False)),
                fields=("one_off_charge",),
                name="invoice_line_items_one_per_one_off_charge",
            ),
        ),
        migrations.AddField(
            model_name="invoicedraft",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="invoicedraft",
            name="lease",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invoice_drafts",
                to="ledger.lease",
            ),
        ),
        migrations.AddField(
            model_name="invoicedraft",
            name="operator",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invoice_drafts",
                to="ledger.operator",
            ),
        ),
        migrations.AddField(
            model_name="invoicedraft",
            name="sent_invoice",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="drafts",
                to="ledger.invoice",
            ),
        ),
        migrations.AddField(
            model_name="invoicedraftline",
            name="draft",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="lines",
                to="ledger.invoicedraft",
            ),
        ),
        migrations.AddField(
            model_name="invoicedraftline",
            name="fee_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ledger.feetype",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraft",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["draft", "sent", "cancelled"])),
                name="invoice_drafts_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraft",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("sent_invoice__isnull", False), ("status", "sent")),
                    models.Q(
                        models.Q(("status", "sent"), _negated=True), ("sent_invoice__isnull", True)
                    ),
                    _connector="OR",
                ),
                name="invoice_drafts_sent_iff_invoice",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraft",
            constraint=models.UniqueConstraint(
                condition=models.Q(("sent_invoice__isnull", False)),
                fields=("sent_invoice",),
                name="invoice_drafts_one_per_invoice",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraftline",
            constraint=models.UniqueConstraint(
                fields=("draft", "position"), name="invoice_draft_lines_position_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraftline",
            constraint=models.CheckConstraint(
                condition=models.Q(("position__gte", 1)),
                name="invoice_draft_lines_position_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraftline",
            constraint=models.CheckConstraint(
                condition=models.Q(("description__regex", "\\S")),
                name="invoice_draft_lines_description_not_blank",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraftline",
            constraint=models.CheckConstraint(
                condition=models.Q(("quantity__gt", 0)),
                name="invoice_draft_lines_quantity_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraftline",
            constraint=models.CheckConstraint(
                condition=models.Q(("unit_amount__gt", 0)),
                name="invoice_draft_lines_unit_amount_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraftline",
            constraint=models.CheckConstraint(
                condition=models.Q(("line_total__gt", 0)),
                name="invoice_draft_lines_line_total_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraftline",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "line_total__gte",
                        django.db.models.expressions.CombinedExpression(
                            django.db.models.expressions.CombinedExpression(
                                models.F("quantity"), "*", models.F("unit_amount")
                            ),
                            "-",
                            models.Value(Decimal("0.005")),
                        ),
                    ),
                    (
                        "line_total__lte",
                        django.db.models.expressions.CombinedExpression(
                            django.db.models.expressions.CombinedExpression(
                                models.F("quantity"), "*", models.F("unit_amount")
                            ),
                            "+",
                            models.Value(Decimal("0.005")),
                        ),
                    ),
                ),
                name="invoice_draft_lines_total_is_quantity_times_unit",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
    ]
