"""The linked bank's access token at rest (PLAN-014 I1, §8 "Plaid access token at rest").

Plaid's access token is a long-lived credential for the business's bank account. It is kept
only encrypted, with Fernet (``cryptography``, already in the lock) and the key
``PLAID_TOKEN_KEY`` (the vault secret ``plaid-token-key``), read from the settings at the call,
never at import. :func:`encrypt` gives the text stored in
``BankConnection.access_token_encrypted``; :func:`decrypt` gives the token back for one call to
Plaid and nothing else. Neither logs, and an unreadable ciphertext (another key, a tampered
value) is :class:`TokenUnreadableError` with no detail.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class TokenKeyMissingError(Exception):
    """``PLAID_TOKEN_KEY`` is not set in this environment."""


class TokenUnreadableError(Exception):
    """The stored ciphertext cannot be read with this environment's key."""


def _fernet() -> Fernet:
    key = str(settings.PLAID_TOKEN_KEY or "")
    if not key:
        raise TokenKeyMissingError
    return Fernet(key.encode("ascii"))


def encrypt(token: str) -> str:
    """The ciphertext of ``token`` (URL-safe base64 text)."""
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """The token ``ciphertext`` holds; :class:`TokenUnreadableError` when it cannot be read."""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise TokenUnreadableError from None
