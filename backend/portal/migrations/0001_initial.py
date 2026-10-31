"""The operator portal's tables (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md
H1): ``portal_users`` (one per operator; the e-mail unique and lower-cased; the password a
Django hasher's value; CHECKs on the theme and on "active means accepted and not disabled"),
``portal_tokens`` (a one-time link's SHA-256, its kind, expiry and use) and
``portal_login_attempts`` (the keyed hash of the e-mail, the client address, the time, the
outcome; indexed for the lockout's counts).

On PostgreSQL the e-mail, the one free-text column, gets the card-number trigger of
``ledger.0002_postgresql_guards`` (its function is reused); the hashes and the password are
identifiers held to their shape. The reverse drops the trigger before the tables go. The
templates of the portal messages are seeded by ``ledger.0064_portal_messages``.
"""

import uuid

import django.db.models.deletion
import django.db.models.functions.text
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

#: Table -> guarded columns: every text column without choices that is not an identifier.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {"portal_users": ("email",)}


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
    initial = True

    dependencies = [
        ("ledger", "0064_portal_messages"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PortalLoginAttempt",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("email_hash", models.CharField(max_length=64)),
                ("ip", models.GenericIPAddressField(blank=True, null=True)),
                ("at", models.DateTimeField()),
                ("ok", models.BooleanField(default=False)),
            ],
            options={
                "db_table": "portal_login_attempts",
                "indexes": [
                    models.Index(fields=["email_hash", "at"], name="portal_attempts_email_idx"),
                    models.Index(fields=["ip", "at"], name="portal_attempts_ip_idx"),
                    models.Index(fields=["at"], name="portal_attempts_at_idx"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("email_hash__regex", "^[0-9a-f]{64}$")),
                        name="portal_login_attempts_email_hash_hex",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="PortalUser",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "email",
                    models.EmailField(
                        max_length=254,
                        unique=True,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("password", models.CharField(blank=True, default="", max_length=128)),
                ("is_active", models.BooleanField(default=False)),
                ("invited_at", models.DateTimeField(blank=True, null=True)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("disabled_at", models.DateTimeField(blank=True, null=True)),
                ("last_login_at", models.DateTimeField(blank=True, null=True)),
                (
                    "theme",
                    models.CharField(
                        choices=[("light", "Light"), ("dark", "Dark"), ("system", "System")],
                        default="system",
                        max_length=6,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "invited_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "operator",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="portal_users",
                        to="ledger.operator",
                    ),
                ),
            ],
            options={
                "db_table": "portal_users",
            },
        ),
        migrations.CreateModel(
            name="PortalToken",
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
                        choices=[("invite", "Invitation"), ("reset", "Password reset")],
                        max_length=6,
                    ),
                ),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tokens",
                        to="portal.portaluser",
                    ),
                ),
            ],
            options={
                "db_table": "portal_tokens",
            },
        ),
        migrations.AddConstraint(
            model_name="portaluser",
            constraint=models.UniqueConstraint(
                fields=("operator",), name="portal_users_one_per_operator"
            ),
        ),
        migrations.AddConstraint(
            model_name="portaluser",
            constraint=models.CheckConstraint(
                condition=models.Q(("email", django.db.models.functions.text.Lower("email"))),
                name="portal_users_email_lower_case",
            ),
        ),
        migrations.AddConstraint(
            model_name="portaluser",
            constraint=models.CheckConstraint(
                condition=models.Q(("theme__in", ["light", "dark", "system"])),
                name="portal_users_theme_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="portaluser",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("is_active", False),
                    models.Q(("activated_at__isnull", False), ("disabled_at__isnull", True)),
                    _connector="OR",
                ),
                name="portal_users_active_is_activated",
            ),
        ),
        migrations.AddIndex(
            model_name="portaltoken",
            index=models.Index(fields=["user", "kind"], name="portal_tokens_user_kind_idx"),
        ),
        migrations.AddConstraint(
            model_name="portaltoken",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ["invite", "reset"])),
                name="portal_tokens_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="portaltoken",
            constraint=models.CheckConstraint(
                condition=models.Q(("token_hash__regex", "^[0-9a-f]{64}$")),
                name="portal_tokens_token_hash_hex",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
    ]
