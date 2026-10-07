"""Line-item kind, invoice fees and surcharge, operator consent
(docs/qa/PLAN-012a-line-item-kind-and-operator-consent.md D1 to D3).

Schema: ``invoice_line_items.kind`` (default ``charge``), ``invoices.fees`` and
``invoices.surcharge`` (default 0), ``operators.sms_phone`` (default ''),
``operators.sms_consent_at`` and ``operators.email_consent_at`` (null). Adding a column
with its default fills every existing row with it, which is the whole backfill: every
existing line becomes ``charge``, every invoice gets fees and surcharge 0, so every
invoice total is unchanged and still the sum of its parts.

Constraints: the invoice total CHECK ``invoices_total_is_subtotal_plus_tax`` is replaced
by ``invoices_total_is_sum_of_parts`` (``total = subtotal + tax + fees + surcharge``
within half a cent); fees and surcharge are not negative; a line's kind is on the closed
list; an SMS phone is empty or E.164. On PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE``
runs any check this transaction queued before the ``ALTER TABLE`` statements (the
lesson of ``0018``).

Free text is guarded: on PostgreSQL the operators trigger of ``0002_postgresql_guards``
is recreated with ``sms_phone`` among its arguments; the reverse restores the columns of
``0024``. Nothing is created on other vendors; the application validator applies there.

The reverse drops the new columns (consent data in them is lost), and refuses while an
invoice has non-zero fees or surcharge or a line has a kind other than ``charge``: the
restored total CHECK could not hold the first, and the second would lose its meaning.
"""

from __future__ import annotations

import importlib
from decimal import Decimal

import django.core.validators
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import F, Q

import ledger.models
import ledger.validators

_OPERATORS_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0024_collections_ladder"
).GUARDED_COLUMNS["operators"]

#: Table -> guarded columns after this migration. Replaces the ``operators`` entry of
#: ``0024``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "operators": (*_OPERATORS_BEFORE, "sms_phone"),
}
SMS_PHONE_PATTERN = r"^\+[1-9][0-9]{7,14}$"
HALF_CENT = Decimal("0.005")
_PARTS = F("subtotal") + F("tax") + F("fees") + F("surcharge")
CANNOT_REVERSE = (
    "An invoice has fees or a surcharge, or a line has a kind other than charge; this "
    "migration refuses to reverse."
)


def _replace_trigger(schema_editor: BaseDatabaseSchemaEditor, columns: tuple[str, ...]) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS operators_payment_data_guard ON operators;", params=None
    )
    schema_editor.execute(
        "CREATE TRIGGER operators_payment_data_guard "
        "BEFORE INSERT OR UPDATE ON operators "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def guard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, GUARDED_COLUMNS["operators"])


def unguard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, _OPERATORS_BEFORE)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    invoices = apps.get_model("ledger", "Invoice").objects
    lines = apps.get_model("ledger", "InvoiceLineItem").objects
    if invoices.exclude(fees=0, surcharge=0).exists() or lines.exclude(kind="charge").exists():
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0030_disputes"),
    ]

    operations = [
        # --- Schema: the columns; their defaults fill the existing rows.
        migrations.AddField(
            model_name="invoice",
            name="fees",
            field=ledger.models.MoneyField(default=Decimal("0.00")),
        ),
        migrations.AddField(
            model_name="invoice",
            name="surcharge",
            field=ledger.models.MoneyField(default=Decimal("0.00")),
        ),
        migrations.AddField(
            model_name="invoicelineitem",
            name="kind",
            field=models.CharField(
                choices=[
                    ("charge", "Charge"),
                    ("fee", "Fee"),
                    ("surcharge", "Surcharge"),
                    ("tax", "Tax"),
                    ("discount", "Discount"),
                ],
                default="charge",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="operator",
            name="sms_phone",
            field=models.CharField(
                blank=True,
                default="",
                max_length=20,
                validators=[
                    ledger.validators.validate_no_payment_data,
                    django.core.validators.RegexValidator(
                        "^\\+[1-9][0-9]{7,14}$",
                        "Enter the SMS phone in international format: a plus sign, the "
                        "country code and the number, digits only, for example +14045551234.",
                        code="sms_phone",
                    ),
                ],
            ),
        ),
        migrations.AddField(
            model_name="operator",
            name="sms_consent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="operator",
            name="email_consent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(guard_new_column, unguard_new_column, elidable=False),
        # --- Constraints, after any queued check has run.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(
            model_name="invoice",
            name="invoices_total_is_subtotal_plus_tax",
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=Q(total__gt=_PARTS - HALF_CENT) & Q(total__lt=_PARTS + HALF_CENT),
                name="invoices_total_is_sum_of_parts",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=Q(fees__gte=0), name="invoices_fees_not_negative"
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=Q(surcharge__gte=0), name="invoices_surcharge_not_negative"
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicelineitem",
            constraint=models.CheckConstraint(
                condition=Q(kind__in=["charge", "fee", "surcharge", "tax", "discount"]),
                name="invoice_line_items_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="operator",
            constraint=models.CheckConstraint(
                condition=Q(sms_phone="") | Q(sms_phone__regex=SMS_PHONE_PATTERN),
                name="operators_sms_phone_e164",
            ),
        ),
        # --- The reverse starts here: it refuses before any constraint changes.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
