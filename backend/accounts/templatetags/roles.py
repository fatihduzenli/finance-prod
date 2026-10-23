"""Role checks for templates: ``{% load roles %}`` then ``user|is_admin`` (the main
navigation's admin-only links, PLAN-010d D8). The views check the role themselves."""

from __future__ import annotations

from django import template
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser

from accounts.roles import Role, meets, role_of

register = template.Library()


@register.filter
def is_admin(user: AbstractBaseUser | AnonymousUser | None) -> bool:
    return user is not None and meets(role_of(user), Role.ADMIN)


@register.filter
def is_billing(user: AbstractBaseUser | AnonymousUser | None) -> bool:
    """Billing or admin: the navigation's Rent run under Admin (PLAN-012d D5)."""
    return user is not None and meets(role_of(user), Role.BILLING)


@register.filter
def role_label(user: AbstractBaseUser | AnonymousUser | None) -> str:
    """The user's role as the sidebar's user card writes it ("Admin", "Billing", "Viewer"),
    or ``""`` for a user without one (PLAN-012f D3)."""
    role = role_of(user) if user is not None else None
    return role.value.capitalize() if role is not None else ""
