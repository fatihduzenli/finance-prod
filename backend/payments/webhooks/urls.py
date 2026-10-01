"""The public webhook route (docs/qa/PLAN-008-webhooks.md D2). Namespace ``webhooks``.

Everything after ``stax/`` is captured, slashes and line breaks included, so every
variant of the token path (missing, truncated, extra segments, no trailing slash)
reaches the receiver and gets its silent 404, rather than an unresolved-path answer
that Django would log with the path.
"""

from __future__ import annotations

from django.urls import re_path

from payments.webhooks import receiver

app_name = "webhooks"

urlpatterns = [
    re_path(r"^stax/(?P<rest>[\s\S]*)\Z", receiver.receive, name="receive"),
]
