"""The three staff roles (docs/SCOPE.md §9) and how a user's role is determined.

A role is membership of a Django ``Group`` of the same name; the groups are created by
``accounts/migrations/0002_role_groups.py``. The superuser flag grants nothing here: the
role registry looks at groups only.
"""

from __future__ import annotations

from enum import StrEnum

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser, User


class Role(StrEnum):
    ADMIN = "admin"
    BILLING = "billing"
    VIEWER = "viewer"


#: Higher outranks lower: admin > billing > viewer.
RANK: dict[Role, int] = {Role.VIEWER: 1, Role.BILLING: 2, Role.ADMIN: 3}

#: Group names, one per role.
GROUP_NAMES: tuple[str, ...] = tuple(role.value for role in Role)


def role_of(user: AbstractBaseUser | AnonymousUser) -> Role | None:
    """The user's highest role, or None for anonymous, inactive or group-less users."""
    if not isinstance(user, User) or not user.is_authenticated or not user.is_active:
        return None
    names = set(user.groups.filter(name__in=GROUP_NAMES).values_list("name", flat=True))
    roles = [Role(name) for name in names]
    if not roles:
        return None
    return max(roles, key=RANK.__getitem__)


def meets(role: Role | None, min_role: Role) -> bool:
    """True when ``role`` is at least ``min_role``."""
    return role is not None and RANK[role] >= RANK[min_role]
