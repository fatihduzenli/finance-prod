"""The on-screen labels of the stored choices (docs/qa/PLAN-012c-design-pass.md, copy list
§4.5).

Only the ``choices`` labels change: every stored value, column, CHECK and trigger is as it
was, and no row is touched (``sqlmigrate`` prints no SQL).
"""

from __future__ import annotations

from django.db import migrations, models

import ledger.models

#: The lease's payment channels, read from the model as ``0037`` reads them, so that the
#: provider's stored value is written in one place (the provider isolation test).
CHANNELS = list(ledger.models.PaymentChannel.choices)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0045_company_settings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="collectionschedule",
            name="state",
            field=models.CharField(
                choices=[
                    ("none", "Not set up"),
                    ("active", "On"),
                    ("paused", "Paused"),
                    ("failed", "Failed"),
                    ("ended", "Off"),
                ],
                default="none",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="depositresolution",
            name="method",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ach", "Bank debit (ACH)"),
                    ("zelle", "Zelle"),
                    ("wire", "Wire"),
                    ("other", "Other"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="disputeevidence",
            name="status",
            field=models.CharField(
                choices=[("queued", "Waiting"), ("sent", "Sent"), ("failed", "Failed")],
                default="queued",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="installmentplan",
            name="collection",
            field=models.CharField(
                choices=[
                    ("autopay_due", "Autopay on the due date"),
                    ("email", "Send by e-mail"),
                    ("manual", "Manual payment"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="lease",
            name="payment_channel",
            field=models.CharField(choices=CHANNELS, max_length=32),
        ),
        migrations.AlterField(
            model_name="leasecredit",
            name="method",
            field=models.CharField(
                choices=[
                    ("ach", "Bank debit (ACH)"),
                    ("zelle", "Zelle"),
                    ("wire", "Wire"),
                    ("other", "Other"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="leaserenthistory",
            name="source",
            field=models.CharField(
                choices=[
                    ("sheet_current", "From the spreadsheet"),
                    ("sheet_inferred", "From the spreadsheet (inferred)"),
                    ("staff", "Entered by staff"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="oneoffcharge",
            name="category",
            field=models.CharField(
                choices=[
                    ("setup", "Setup"),
                    ("arrears", "Arrears"),
                    ("adjustment", "Adjustment"),
                    ("damage", "Damage"),
                    ("other", "Other"),
                    ("redebit", "Re-debit"),
                    ("nsf_fee", "Returned payment (NSF) fee"),
                    ("installment", "Installment"),
                ],
                default="setup",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="oneoffcharge",
            name="collection",
            field=models.CharField(
                choices=[
                    ("none", "None"),
                    ("autopay_due", "Autopay on the due date"),
                    ("email", "Send by e-mail"),
                    ("manual", "Manual payment"),
                    ("charge_now", "Charge now"),
                ],
                default="none",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="operator",
            name="sync_failure",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ProviderError", "Error"),
                    ("ValidationFailed", "Not sent"),
                    ("ProviderNotPermittedHere", "Not allowed here"),
                    ("FeatureDisabled", "Turned off"),
                    ("CircuitOpen", "Paused after repeated errors"),
                    ("FailureBudgetExhausted", "Too many errors today"),
                    ("RateLimitWaitExceeded", "Busy, try later"),
                    ("ProviderRejected", "Rejected"),
                    ("ProviderUnavailable", "Unavailable"),
                    ("TransportError", "Could not be reached"),
                    ("IdentityMismatch", "Wrong account"),
                    ("AlreadyLinkedElsewhere", "Connected to another operator"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="operator",
            name="sync_status",
            field=models.CharField(
                choices=[
                    ("not_applicable", "Not needed"),
                    ("awaiting_link", "Waiting to connect"),
                    ("pending", "In progress"),
                    ("linked", "Connected"),
                    ("failed", "Failed"),
                ],
                default="not_applicable",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Waiting"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                    ("suppressed", "Suppressed"),
                    ("catch_all", "Sent to the test inbox"),
                ],
                default="queued",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="payment",
            name="method",
            field=models.CharField(
                choices=[
                    ("ach", "Bank debit (ACH)"),
                    ("zelle", "Zelle"),
                    ("wire", "Wire"),
                    ("other", "Other"),
                    ("credit", "Credit"),
                ],
                max_length=32,
            ),
        ),
    ]
