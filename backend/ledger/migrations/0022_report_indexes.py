"""Indexes for the dashboard, the month grid, arrears and aging
(docs/qa/PLAN-011-dashboard-reports-mobile.md D6). No row changes; each reverses.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0021_audit_log_indexes"),
    ]

    operations = [
        # The grid and the run page read a lease's rent charges by month through the left
        # prefix of the unique constraint (lease, period_month, seq): no index is added.
        # Arrears and the dashboard's overdue figure: rent charges by status and due date.
        migrations.AddIndex(
            model_name="rentcharge",
            index=models.Index(fields=["status", "due_on"], name="rent_charges_status_due_idx"),
        ),
        # The dashboard's "paid this month": settled payments by settled date.
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(fields=["settled_on", "status"], name="payments_settled_status_idx"),
        ),
        # The dashboard's failed payment attempts: invoices by provider status.
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(fields=["provider_status"], name="invoices_provider_status_idx"),
        ),
        # Arrears and aging: an operator's one-off charges by status.
        migrations.AddIndex(
            model_name="oneoffcharge",
            index=models.Index(fields=["operator", "status"], name="one_off_charges_op_status_idx"),
        ),
        # Arrears and aging: a lease's insurance charges by due date.
        migrations.AddIndex(
            model_name="insurancecharge",
            index=models.Index(fields=["lease", "due_on"], name="insurance_lease_due_idx"),
        ),
    ]
