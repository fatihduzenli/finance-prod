"""Keep UserSession rows and the session's sign-in time in step with login/logout."""

from __future__ import annotations

import time
from typing import Any

from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.http import HttpRequest
from django.utils import timezone

from accounts.models import USER_AGENT_MAX_LENGTH, UserSession

#: Session key holding the Unix time of sign-in (absolute ceiling, PLAN-003 §4).
LOGIN_AT_SESSION_KEY = "accounts_login_at"
#: Session key holding the Unix time of the last request (idle timeout).
LAST_SEEN_SESSION_KEY = "accounts_last_seen_at"


def now() -> float:
    """Current Unix time. A function so tests can move the clock."""
    return time.time()


@receiver(user_logged_in, dispatch_uid="accounts_record_login")
def record_login(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    if request is None or not hasattr(request, "session"):
        return
    stamp = int(now())
    request.session[LOGIN_AT_SESSION_KEY] = stamp
    request.session[LAST_SEEN_SESSION_KEY] = stamp
    if request.session.session_key is None:
        request.session.save()
    user_agent = request.headers.get("User-Agent", "")[:USER_AGENT_MAX_LENGTH]
    UserSession.objects.update_or_create(
        session_key=request.session.session_key,
        defaults={"user": user, "last_seen_at": timezone.now(), "user_agent": user_agent},
    )


@receiver(user_logged_out, dispatch_uid="accounts_record_logout")
def record_logout(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    if request is None or not hasattr(request, "session"):
        return
    key = request.session.session_key
    if key:
        UserSession.objects.filter(session_key=key).delete()
