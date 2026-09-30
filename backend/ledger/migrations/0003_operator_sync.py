"""Operator sync columns (docs/qa/PLAN-006-operators.md D3, D6, D7).

``sync_status`` and ``sync_failure`` are closed lists with a CHECK each; the failure
column holds an error class name, never a message. ``creation_key`` is the create form's
one-time key (unique; NULL for rows not created by the form). No data is changed:
existing rows start as ``not_applicable`` until staff save them through the screens.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0002_postgresql_guards"),
    ]

    operations = [
        migrations.AddField(
            model_name="operator",
            name="creation_key",
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="operator",
            name="needs_review",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="operator",
            name="sync_attempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="operator",
            name="sync_failure",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ProviderError", "Provider error"),
                    ("ValidationFailed", "Refused before sending"),
                    ("ProviderNotPermittedHere", "Not permitted from this process"),
                    ("FeatureDisabled", "Feature disabled"),
                    ("CircuitOpen", "Calls paused after repeated failures"),
                    ("FailureBudgetExhausted", "Failure budget exhausted"),
                    ("RateLimitWaitExceeded", "Rate limit wait exceeded"),
                    ("ProviderRejected", "Rejected by the provider"),
                    ("ProviderUnavailable", "Provider unavailable"),
                    ("TransportError", "Provider unreachable"),
                    ("IdentityMismatch", "Wrong merchant"),
                    ("AlreadyLinkedElsewhere", "Linked to another operator"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="operator",
            name="sync_status",
            field=models.CharField(
                choices=[
                    ("not_applicable", "Not applicable"),
                    ("awaiting_link", "Awaiting link"),
                    ("pending", "Pending"),
                    ("linked", "Linked"),
                    ("failed", "Failed"),
                ],
                default="not_applicable",
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="operator",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "sync_status__in",
                        ["not_applicable", "awaiting_link", "pending", "linked", "failed"],
                    )
                ),
                name="operators_sync_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="operator",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "sync_failure__in",
                        [
                            "ProviderError",
                            "ValidationFailed",
                            "ProviderNotPermittedHere",
                            "FeatureDisabled",
                            "CircuitOpen",
                            "FailureBudgetExhausted",
                            "RateLimitWaitExceeded",
                            "ProviderRejected",
                            "ProviderUnavailable",
                            "TransportError",
                            "IdentityMismatch",
                            "AlreadyLinkedElsewhere",
                            "",
                        ],
                    )
                ),
                name="operators_sync_failure_valid",
            ),
        ),
    ]
