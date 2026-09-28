"""Create or update the local-only admin account with a confirmed TOTP device.

Refused (exit 2) unless APP_ENV=local. The password comes from the
LOCAL_ADMIN_PASSWORD environment variable only: there is no default and none is
written anywhere. Prints the otpauth:// URI for an authenticator app and five backup
tokens, for this machine's developer only.
"""

from __future__ import annotations

import os
from typing import Any

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.roles import Role

SEED_EMAIL = "admin@example.com"
PASSWORD_ENV_NAME = "LOCAL_ADMIN_PASSWORD"  # noqa: S105 - the variable's name, not a value
BACKUP_TOKEN_COUNT = 5
#: two_factor looks for these device names (default device, backup tokens).
TOTP_DEVICE_NAME = "default"
STATIC_DEVICE_NAME = "backup"
REFUSED_EXIT_CODE = 2

_BANNER = "=" * 72


class Command(BaseCommand):
    help = "LOCAL ONLY: create the seeded admin account and print its TOTP enrolment."

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.APP_ENV != "local":
            raise CommandError(
                "seed_local_admin runs only when APP_ENV=local.", returncode=REFUSED_EXIT_CODE
            )
        password = os.environ.get(PASSWORD_ENV_NAME, "")
        if not password:
            raise CommandError(f"{PASSWORD_ENV_NAME} must be set to a non-empty value.")

        with transaction.atomic():
            user, _ = User.objects.get_or_create(
                username=SEED_EMAIL, defaults={"email": SEED_EMAIL}
            )
            try:
                validate_password(password, user)
            except ValidationError as error:
                # The validator messages describe the rule, never the password.
                raise CommandError(
                    f"{PASSWORD_ENV_NAME} was rejected: " + " ".join(error.messages)
                ) from error
            user.email = SEED_EMAIL
            user.is_active = True
            user.is_staff = True
            user.is_superuser = False
            user.set_password(password)
            user.save()
            admin_group, _ = Group.objects.get_or_create(name=Role.ADMIN.value)
            user.groups.set([admin_group])

            totp, _ = TOTPDevice.objects.get_or_create(
                user=user, name=TOTP_DEVICE_NAME, defaults={"confirmed": True}
            )
            if not totp.confirmed:
                totp.confirmed = True
                totp.save(update_fields=["confirmed"])

            static, _ = StaticDevice.objects.get_or_create(
                user=user, name=STATIC_DEVICE_NAME, defaults={"confirmed": True}
            )
            static.token_set.all().delete()
            tokens = [StaticToken.random_token() for _ in range(BACKUP_TOKEN_COUNT)]
            StaticToken.objects.bulk_create(
                StaticToken(device=static, token=token) for token in tokens
            )

        lines = [
            _BANNER,
            "LOCAL ONLY - development account. Never run this against a shared database.",
            _BANNER,
            f"User:           {SEED_EMAIL} (role: {Role.ADMIN.value}, staff, not superuser)",
            "Password:       the value of LOCAL_ADMIN_PASSWORD (not printed)",
            "Authenticator:  add this URI to your TOTP app (or paste it into a QR generator):",
            f"  {totp.config_url}",
            "Backup tokens (each works once):",
            *(f"  {token}" for token in tokens),
            _BANNER,
        ]
        self.stdout.write("\n".join(lines))
