"""``{{ user|staff }}``: a staff member on a page (PLAN-012f-c D11).

The name is :func:`accounts.display.staff_name` (the full name, else the username, never
the e-mail) inside ``<span title="{e-mail}">``, both escaped; no ``title`` when the user
has no e-mail. ``None`` (no user recorded) and anything that is not a user render as
``""``, so ``|staff|default:"—"`` shows a dash.
"""

from __future__ import annotations

from django import template
from django.contrib.auth.models import AbstractUser
from django.utils.html import format_html
from django.utils.safestring import SafeString

from accounts.display import staff_name

register = template.Library()


@register.filter(name="staff")
def staff(value: object) -> SafeString | str:
    """``<span title="{e-mail}">{name}</span>``, or ``""``."""
    if not isinstance(value, AbstractUser):
        return ""
    if value.email:
        return format_html('<span title="{}">{}</span>', value.email, staff_name(value))
    return format_html("<span>{}</span>", staff_name(value))
