"""NSF re-debit and the signed statement link (docs/qa/PLAN-011b D5, D4).

One-off charge categories ``redebit`` (``source_payment``, one per returned payment) and
``nsf_fee`` (``companion_of``, one per re-debit); ``operators.nsf_return_count`` and
``operators.statement_links_revoked_at``; ``payments.resolution_kind`` and
``payments.redebit_date``. Every return already resolved was resolved with a note: its
``resolution_kind`` is set to ``note`` before the CHECK that requires it is added.
"""

from typing import Any

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def resolved_with_a_note(apps: Any, schema_editor: Any) -> None:
    apps.get_model("ledger", "Payment").objects.filter(resolved_at__isnull=False).update(
        resolution_kind="note"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0025_seed_collections_ladder"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_fee_type_iff_setup",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_description_required",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_collection_none_only_setup",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_has_operator",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_category_valid",
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="companion_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="companions",
                to="ledger.oneoffcharge",
            ),
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="source_payment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="redebits",
                to="ledger.payment",
            ),
        ),
        migrations.AddField(
            model_name="operator",
            name="nsf_return_count",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="operator",
            name="statement_links_revoked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="redebit_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="resolution_kind",
            field=models.CharField(
                blank=True,
                choices=[("note", "Resolved with a note"), ("rescheduled", "Rescheduled")],
                default="",
                max_length=32,
            ),
        ),
        migrations.RunPython(resolved_with_a_note, migrations.RunPython.noop, elidable=False),
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
                    ("nsf_fee", "NSF fee"),
                ],
                default="setup",
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        models.Q(("category", "setup"), _negated=True),
                        ("fee_type__isnull", False),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(
                            (
                                "category__in",
                                ["arrears", "adjustment", "damage", "other", "redebit", "nsf_fee"],
                            ),
                            _negated=True,
                        ),
                        ("fee_type__isnull", True),
                        _connector="OR",
                    ),
                ),
                name="one_off_charges_fee_type_iff_setup",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        (
                            "category__in",
                            ["arrears", "adjustment", "damage", "other", "redebit", "nsf_fee"],
                        ),
                        _negated=True,
                    ),
                    ("description__regex", "\\S"),
                    _connector="OR",
                ),
                name="one_off_charges_description_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "category__in",
                        ["arrears", "adjustment", "damage", "other", "redebit", "nsf_fee"],
                    ),
                    ("collection", "none"),
                    _negated=True,
                ),
                name="one_off_charges_collection_none_only_setup",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("operator__isnull", False),
                    models.Q(
                        ("lease__isnull", False),
                        models.Q(
                            (
                                "category__in",
                                ["arrears", "adjustment", "damage", "other", "redebit", "nsf_fee"],
                            ),
                            _negated=True,
                        ),
                    ),
                    _connector="OR",
                ),
                name="one_off_charges_has_operator",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("category", "redebit"), ("source_payment__isnull", False)),
                    models.Q(
                        models.Q(("category", "redebit"), _negated=True),
                        ("source_payment__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="one_off_charges_source_payment_iff_redebit",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("category", "nsf_fee"), ("companion_of__isnull", False)),
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
                fields=("source_payment",), name="one_off_charges_one_redebit_per_payment"
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.UniqueConstraint(
                fields=("companion_of",), name="one_off_charges_one_fee_per_redebit"
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "category__in",
                        ["setup", "arrears", "adjustment", "damage", "other", "redebit", "nsf_fee"],
                    )
                ),
                name="one_off_charges_category_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="operator",
            constraint=models.CheckConstraint(
                condition=models.Q(("nsf_return_count__gte", 0)),
                name="operators_nsf_return_count_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("resolution_kind", ""), ("resolved_at__isnull", True)),
                    models.Q(
                        ("resolved_at__isnull", False),
                        models.Q(("resolution_kind", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="payments_resolution_kind_iff_resolved",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("redebit_date__isnull", False), ("resolution_kind", "rescheduled")),
                    models.Q(
                        models.Q(("resolution_kind", "rescheduled"), _negated=True),
                        ("redebit_date__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="payments_redebit_date_iff_rescheduled",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(("resolution_kind__in", ["note", "rescheduled", ""])),
                name="payments_resolution_kind_valid",
            ),
        ),
    ]
