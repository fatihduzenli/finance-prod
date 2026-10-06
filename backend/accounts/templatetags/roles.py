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
