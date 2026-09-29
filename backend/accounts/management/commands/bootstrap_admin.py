"""Create the first admin account on a deployed environment (docs/qa/PLAN-003c).

Run once, by the owner, in an interactive terminal. The password is typed twice at a
hidden prompt: there is no password argument and no password environment variable, and
the password is never printed or logged. Refused (exit 2) in APP_ENV=local, where
seed_local_admin is the tool, and refused (exit 2) once any user belongs to the admin
group. The account is created without an authenticator: signing in holds it at the
enrolment page until one is added.
"""

from __future__ import annotations

import logging
import sys
from getpass import getpass
from typing import Any

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q

from accounts.roles import Role

REFUSED_EXIT_CODE = 2
LOGIN_PATH = "/account/login/"

logger = logging.getLogger("accounts.bootstrap_admin")


def stdin_is_terminal() -> bool:
    """Whether standard input is an interactive terminal (a hidden prompt is possible)."""
    return sys.stdin is not None and sys.stdin.isatty()


class Command(BaseCommand):
    help = "Create the first admin (staging or production), password typed at a hidden prompt."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--email", required=True, help="The first admin's email address.")

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.APP_ENV == "local":
            raise CommandError(
                "bootstrap_admin is for staging and production; use seed_local_admin locally.",
                returncode=REFUSED_EXIT_CODE,
            )

        email = str(options["email"]).strip().lower()
        try:
            validate_email(email)
        except ValidationError as error:
            raise CommandError("--email is not a valid email address.") from error
        username_max_length = User._meta.get_field("username").max_length
        if username_max_length is not None and len(email) > username_max_length:
            raise CommandError("--email is too long to be used as a username.")

        if not stdin_is_terminal():
            raise CommandError(
                "bootstrap_admin needs an interactive terminal to read the password."
            )

        password = getpass("Password: ")
        if getpass("Password (again): ") != password:
            raise CommandError("The two passwords did not match. Nothing was created.")

        with transaction.atomic():
            admin_group, _ = Group.objects.get_or_create(name=Role.ADMIN.value)
            # Lock the admin group row so two concurrent runs cannot both pass the check.
            admin_group = Group.objects.select_for_update().get(pk=admin_group.pk)
            if admin_group.user_set.exists():
                raise CommandError(
                    "An admin already exists. bootstrap_admin creates the first admin only; "
                    "add further users in the Django admin.",
                    returncode=REFUSED_EXIT_CODE,
                )
            if User.objects.filter(Q(username__iexact=email) | Q(email__iexact=email)).exists():
                raise CommandError("A user with this email already exists. Nothing was changed.")

            user = User(
                username=email, email=email, is_active=True, is_staff=True, is_superuser=False
            )
            try:
                validate_password(password, user)
            except ValidationError as error:
                # The validator messages describe the rule, never the password.
                raise CommandError(
                    "The password was rejected: " + " ".join(error.messages)
                ) from error
            user.set_password(password)
            user.save()
            user.groups.set([admin_group])

        logger.warning("First admin created: %s", email)
        self.stdout.write(
            f"Created admin {email}. Sign in at {LOGIN_PATH} now and enrol an authenticator "
            "app immediately."
        )
