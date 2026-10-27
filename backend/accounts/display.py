"""How a staff member's name is shown on a page (PLAN-012f-c D11).

:func:`staff_name` is the name: the full name when the first or the last name is set,
else the username; never the e-mail. The template filter ``staff``
(``finance/templatetags/people.py``) prints it with the e-mail as its ``title``.
"""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser


def staff_name(user: AbstractUser) -> str:
    """The user's full name, or the username when neither name is set."""
    full = f"{user.first_name.strip()} {user.last_name.strip()}".strip()
    return full or user.get_username()
