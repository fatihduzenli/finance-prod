"""The Gmail API behind :class:`~mail.base.MailSender` (docs/qa/PLAN-011b D3).

The company mailbox sends through a Google service account with domain-wide
delegation: ``google-auth`` reads the service-account key
(``Credentials.from_service_account_info`` with the ``gmail.send`` scope and the
mailbox as the delegated subject) and signs the token request's JWT assertion with
its ``jwt`` helpers; the assertion is posted to the key's ``token_uri`` and the message
to ``users/me/messages/send`` through ``httpx``, the app's one HTTP client (google-auth's
own ``requests`` transport is not used). The access token is kept in memory until a
minute before it expires.

Limits: 10 seconds to connect, 30 seconds of silence; no retry on a 4xx answer; one
retry, 5 seconds later, on a 5xx answer or a network error; redirects are not followed.
Every failure is :class:`~mail.base.MailUnavailable` carrying a class name, raised
without its cause. Nothing here logs or raises the key, the assertion, the token, a
response body or the message.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from google.auth import jwt
from google.oauth2 import service_account

from mail.base import MailUnavailable, Message, SentReceipt, mime_bytes

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/gmail.send"
#: The only token endpoint the key may name.
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - a public endpoint, not a secret
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
#: Seconds to open a connection, and of silence on the socket.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
#: Seconds before the one retry of a 5xx answer or a network error.
RETRY_DELAY = 5
#: Lifetime of the signed assertion, and how long before its expiry a token is renewed.
ASSERTION_SECONDS = 3600
TOKEN_MARGIN_SECONDS = 60
#: A Gmail message ID as stored: letters, digits, ``_`` and ``-``.
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

_STATUS_NAMES: dict[int, str] = {
    400: "BadRequest",
    401: "Unauthorized",
    403: "Forbidden",
    404: "NotFound",
    429: "RateLimited",
}


class _Unauthorized(Exception):  # noqa: N818 - internal signal, never leaves this module
    pass


def _unavailable(operation: str, name: str) -> MailUnavailable:
    error = MailUnavailable(name)
    logger.warning("Mail %s failed: %s", operation, error.cause)
    return error


def _status_name(status: int) -> str:
    if status in _STATUS_NAMES:
        return _STATUS_NAMES[status]
    if 300 <= status < 400:
        return "UnexpectedRedirect"
    return "ClientError" if status < 500 else "ServerError"


class GmailApiSender:
    """Sends as ``sender`` (the delegated mailbox: the company settings' sender address,
    PLAN-014 D1) with the service-account key ``info``, with ``Reply-To: reply_to`` when it
    is given.

    ``transport``, ``sleep`` and ``clock`` exist for tests (``httpx.MockTransport``, no
    real waits, a fixed time).
    """

    def __init__(
        self,
        info: Mapping[str, Any],
        sender: str,
        *,
        reply_to: str = "",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[int], None] = time.sleep,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        try:
            credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
                dict(info), scopes=[SCOPE], subject=sender
            )
        except Exception as error:  # any parse failure: its class name only
            raise _unavailable("setup", type(error).__name__) from None
        if info.get("token_uri") != TOKEN_URI:
            raise _unavailable("setup", "TokenUriRefused")
        self.sender = sender
        self.reply_to = reply_to
        self._signer = credentials.signer
        self._issuer = credentials.service_account_email
        self._sleep = sleep
        self._clock = clock
        self._client = httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            transport=transport,
            follow_redirects=False,
        )
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    # --- HTTP -------------------------------------------------------------------------

    def _post(self, operation: str, url: str, **kwargs: Any) -> httpx.Response:
        """One POST, retried once after :data:`RETRY_DELAY` on a 5xx or a network error."""
        failure = ""
        for attempt in (1, 2):
            try:
                response = self._client.post(url, **kwargs)
            except httpx.HTTPError as error:
                failure = type(error).__name__
            else:
                if 200 <= response.status_code < 300:
                    return response
                if response.status_code == 401:
                    raise _Unauthorized
                if response.status_code < 500:
                    raise _unavailable(operation, _status_name(response.status_code))
                failure = "ServerError"
            if attempt == 1:
                self._sleep(RETRY_DELAY)
        raise _unavailable(operation, failure)

    # --- Token ------------------------------------------------------------------------

    def _assertion(self, now: int) -> str:
        payload = {
            "iss": self._issuer,
            "sub": self.sender,
            "scope": SCOPE,
            "aud": TOKEN_URI,
            "iat": now,
            "exp": now + ASSERTION_SECONDS,
        }
        try:
            signed: bytes = jwt.encode(self._signer, payload)  # type: ignore[no-untyped-call]
            return signed.decode("ascii")
        except Exception as error:  # a signing failure: its class name only
            raise _unavailable("token", type(error).__name__) from None

    def _access_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._expires_at - TOKEN_MARGIN_SECONDS:
                return self._token
            try:
                response = self._post(
                    "token",
                    TOKEN_URI,
                    data={"grant_type": GRANT_TYPE, "assertion": self._assertion(now)},
                )
            except _Unauthorized:
                raise _unavailable("token", "Unauthorized") from None
            try:
                body = response.json()
                token = body["access_token"]
                lifetime = int(body["expires_in"])
            except Exception:  # an unreadable answer: no detail of it is kept
                raise _unavailable("token", "TokenResponseInvalid") from None
            if not isinstance(token, str) or not token or lifetime <= 0:
                raise _unavailable("token", "TokenResponseInvalid")
            self._token, self._expires_at = token, now + lifetime
            return token

    def _forget_token(self) -> None:
        with self._lock:
            self._token, self._expires_at = "", 0

    # --- Send -------------------------------------------------------------------------

    def send(self, message: Message) -> SentReceipt:
        try:
            data = mime_bytes(message, self.sender, self.reply_to)
        except ValueError:
            raise _unavailable("send", "ReplyToRefused") from None
        raw = base64.urlsafe_b64encode(data).decode("ascii")
        token = self._access_token()
        try:
            response = self._post(
                "send",
                SEND_URL,
                json={"raw": raw},
                headers={"Authorization": f"Bearer {token}"},
            )
        except _Unauthorized:
            # A revoked or expired token: the next send asks for a new one.
            self._forget_token()
            raise _unavailable("send", "Unauthorized") from None
        try:
            message_id = response.json()["id"]
        except Exception:  # an unreadable answer: no detail of it is kept
            raise _unavailable("send", "SendResponseInvalid") from None
        if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
            raise _unavailable("send", "SendResponseInvalid")
        return SentReceipt(provider_message_id=message_id, sent_at=datetime.now(UTC))
