"""Outbound mail (docs/qa/PLAN-011b-collections-nsf-attention-calendar.md D3, §7).

- :class:`mail.Message` refuses header injection; the catch-all rewrites the envelope;
  mail off suppresses; :func:`mail.get_sender` picks the sender from the settings and
  refuses to send without a catch-all outside production.
- The Gmail sender against ``httpx.MockTransport``: the signed assertion and the token
  exchange, the send, the token cache, and the 401, 403, 4xx, 5xx and timeout paths with
  the one retry after 5 seconds.
- The environment guard: mail on needs the mailbox, the key and (outside production) the
  catch-all; a malformed key is refused by key names only, never a value.
- Fences: nothing outside ``backend/mail/`` imports ``google.*``; the key and the token
  never reach a log record or an exception message (the ladder's own fence on rows, audit
  and pages is in ``test_collections.py``).

The RSA key used to sign is generated at run time; no key is in the repository.
"""

from __future__ import annotations

import ast
import base64
import json
import logging
from collections.abc import Callable, Iterator
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.test import override_settings

import mail
from finance.env_guard import EnvGuardError, validate
from finance.log_redaction import service_account_secrets
from mail import gmail
from mail.base import mime_bytes
from mail.memory import CatchAllSender, MemoryMailSender, NullSender
from tests.test_env_guard import DEPLOYED_ENV, LOCAL_ENV, _env

BACKEND = Path(__file__).resolve().parents[1]
SENDER = "billing@example.com"
CATCH_ALL = "catchall@example.com"
TOKEN = "FAKE-ACCESS-TOKEN-MARKER-do-not-use-0001"
#: The private_key of a fake service-account key: not a key at all, a marker to grep for.
PRIVATE_KEY_MARKER = "PRIVATE-KEY-MARKER-do-not-use-Q7Z"


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def key_info(rsa_key: rsa.RSAPrivateKey, **overrides: Any) -> dict[str, Any]:
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    info = {
        "type": "service_account",
        "client_email": "mailer@finance-test.iam.example.com",
        "client_id": "000000000000000000000",
        "private_key_id": "0000000000000000000000000000000000000000",
        "private_key": pem,
        "token_uri": gmail.TOKEN_URI,
    }
    return {**info, **overrides}


def marker_info() -> dict[str, Any]:
    return {
        "type": "service_account",
        "client_email": "mailer@finance-test.iam.example.com",
        "private_key": PRIVATE_KEY_MARKER,
        "token_uri": gmail.TOKEN_URI,
    }


Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """A mock Gmail: the token endpoint and the send endpoint, each scripted."""

    def __init__(
        self,
        token: list[httpx.Response | Exception] | None = None,
        send: list[httpx.Response | Exception] | None = None,
    ) -> None:
        self.token = token or []
        self.send = send or []
        self.requests: list[httpx.Request] = []

    def _next(self, script: list[httpx.Response | Exception], ok: httpx.Response) -> httpx.Response:
        found = script.pop(0) if script else ok
        if isinstance(found, Exception):
            raise found
        return found

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == gmail.TOKEN_URI:
            ok = httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
            return self._next(self.token, ok)
        assert str(request.url) == gmail.SEND_URL
        return self._next(self.send, httpx.Response(200, json={"id": "18f0a1b2c3d4e5f6"}))

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]


class Clock:
    def __init__(self, now: int = 1_790_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def sender_for(
    rsa_key: rsa.RSAPrivateKey, recorder: Recorder, clock: Clock | None = None
) -> tuple[gmail.GmailApiSender, list[int]]:
    sleeps: list[int] = []
    sender = gmail.GmailApiSender(
        key_info(rsa_key),
        SENDER,
        transport=httpx.MockTransport(recorder),
        sleep=sleeps.append,
        clock=clock or Clock(),
    )
    return sender, sleeps


def a_message(**overrides: Any) -> mail.Message:
    values: dict[str, Any] = {
        "to": "ceo@example.com",
        "subject": "Friendly reminder",
        "text": "Hello,\nYou owe $10.00.",
        "html": "<p>Hello</p>",
    }
    return mail.Message(**{**values, **overrides})


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def sent_mime(request: httpx.Request) -> EmailMessage:
    raw = json.loads(request.content)["raw"]
    parsed = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    assert isinstance(parsed, EmailMessage)
    return parsed


# --- The message ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"to": "a@example.com, b@example.com"},
        {"to": "a@example.com\r\nBcc: x@example.com"},
        {"to": "Someone <a@example.com>"},
        {"to": ""},
        {"subject": "Hi\r\nBcc: x@example.com"},
        {"subject": "Hi\nthere"},
        {"subject": "x" * 301},
        {"headers": (("Bcc", "x@example.com"),)},
        {"headers": (("X-Note", "a\r\nBcc: x@example.com"),)},
    ],
)
def test_a_message_refuses_header_injection(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        a_message(**fields)


def test_the_mime_message_has_the_text_and_the_html_alternative() -> None:
    parsed = message_from_bytes(
        mime_bytes(a_message(headers=(("X-Note", "one"),)), SENDER), policy=policy.default
    )
    assert isinstance(parsed, EmailMessage)
    assert (parsed["From"], parsed["To"], parsed["Subject"]) == (
        SENDER,
        "ceo@example.com",
        "Friendly reminder",
    )
    assert parsed["X-Note"] == "one"
    text = parsed.get_body(("plain",))
    html = parsed.get_body(("html",))
    assert text is not None and "You owe $10.00." in text.get_content()
    assert html is not None and "<p>Hello</p>" in html.get_content()


def test_the_catch_all_rewrites_the_envelope_and_names_the_recipient() -> None:
    memory = MemoryMailSender()
    receipt = CatchAllSender(memory, CATCH_ALL).send(a_message())
    assert receipt.disposition == mail.CATCH_ALL
    [delivered] = memory.sent
    assert delivered.to == CATCH_ALL
    assert delivered.subject == "[to: ceo@example.com] Friendly reminder"
    assert ("X-Original-To", "ceo@example.com") in delivered.headers
    assert delivered.text == a_message().text


# --- Attachments (PLAN-012e D8) -----------------------------------------------------------

PDF = b"%PDF-1.4 statement"


def a_pdf(**overrides: Any) -> mail.Attachment:
    values: dict[str, Any] = {
        "filename": "final-statement-20261019.pdf",
        "content_type": "application/pdf",
        "data": PDF,
    }
    return mail.Attachment(**{**values, **overrides})


@pytest.mark.parametrize(
    "fields",
    [
        {"filename": "../etc/passwd.pdf"},
        {"filename": "statement.pdf\r\nBcc: x@example.com"},
        {"filename": "statement"},
        {"filename": ".pdf"},
        {"content_type": "text/html"},
        {"data": b""},
        {"data": b"x" * (10 * 1024 * 1024 + 1)},
    ],
)
def test_an_attachment_refuses_a_bad_name_type_or_size(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        a_pdf(**fields)


def test_a_message_takes_attachments_only() -> None:
    with pytest.raises(ValueError):
        a_message(attachments=(("statement.pdf", "application/pdf", PDF),))


def test_with_an_attachment_the_mime_message_is_multipart_mixed() -> None:
    raw = mime_bytes(a_message(attachments=(a_pdf(),)), SENDER)
    parsed = message_from_bytes(raw, policy=policy.default)
    assert isinstance(parsed, EmailMessage)
    assert parsed.get_content_type() == "multipart/mixed"
    text = parsed.get_body(("plain",))
    html = parsed.get_body(("html",))
    assert text is not None and "You owe $10.00." in text.get_content()
    assert html is not None and "<p>Hello</p>" in html.get_content()
    [attached] = list(parsed.iter_attachments())
    assert attached.get_filename() == "final-statement-20261019.pdf"
    assert attached.get_content_type() == "application/pdf"
    assert attached.get_content() == PDF


def test_without_an_attachment_the_mime_message_is_unchanged() -> None:
    parsed = message_from_bytes(mime_bytes(a_message(), SENDER), policy=policy.default)
    assert parsed.get_content_type() == "multipart/alternative"


def test_the_catch_all_keeps_the_attachment() -> None:
    memory = MemoryMailSender()
    CatchAllSender(memory, CATCH_ALL).send(a_message(attachments=(a_pdf(),)))
    [delivered] = memory.sent
    assert delivered.to == CATCH_ALL and delivered.attachments == (a_pdf(),)


def test_the_gmail_sender_posts_the_attachment(rsa_key: rsa.RSAPrivateKey) -> None:
    recorder = Recorder()
    sender, _ = sender_for(rsa_key, recorder, Clock(1_790_000_000))
    sender.send(a_message(attachments=(a_pdf(),)))
    sent = sent_mime(recorder.requests[-1])
    [attached] = list(sent.iter_attachments())
    assert attached.get_filename() == "final-statement-20261019.pdf"
    assert attached.get_content() == PDF


def test_the_null_sender_suppresses_and_reaches_nothing() -> None:
    receipt = NullSender().send(a_message())
    assert receipt.disposition == mail.SUPPRESSED
    assert receipt.provider_message_id == ""


def test_mail_unavailable_carries_a_class_name_only() -> None:
    assert str(mail.MailUnavailable("ReadTimeout")) == "ReadTimeout"
    assert mail.MailUnavailable("a message with spaces: secret").cause == "MailUnavailable"


# --- get_sender ---------------------------------------------------------------------------


@pytest.fixture
def memory_base(monkeypatch: pytest.MonkeyPatch) -> MemoryMailSender:
    memory = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: memory)
    return memory


@override_settings(MAIL_ENABLED=False, MAIL_CATCH_ALL="")
def test_mail_off_is_the_null_sender(memory_base: MemoryMailSender) -> None:
    assert isinstance(mail.get_sender(), NullSender)


@override_settings(MAIL_ENABLED=True, MAIL_CATCH_ALL=CATCH_ALL, APP_ENV="staging")
def test_a_catch_all_wraps_the_base_sender(memory_base: MemoryMailSender) -> None:
    receipt = mail.get_sender().send(a_message())
    assert receipt.disposition == mail.CATCH_ALL
    assert [message.to for message in memory_base.sent] == [CATCH_ALL]


@pytest.mark.parametrize("app_env", ["local", "staging"])
def test_outside_production_no_catch_all_means_no_mail(
    app_env: str, memory_base: MemoryMailSender
) -> None:
    with override_settings(MAIL_ENABLED=True, MAIL_CATCH_ALL="", APP_ENV=app_env):
        with pytest.raises(mail.MailUnavailable) as raised:
            mail.get_sender()
    assert raised.value.cause == "CatchAllRequired"
    assert memory_base.sent == []


@override_settings(MAIL_ENABLED=True, MAIL_CATCH_ALL="", APP_ENV="production")
def test_production_without_a_catch_all_mails_the_recipient(
    memory_base: MemoryMailSender,
) -> None:
    receipt = mail.get_sender().send(a_message())
    assert receipt.disposition == mail.SENT
    assert [message.to for message in memory_base.sent] == ["ceo@example.com"]


def test_the_real_base_sender_is_built_once_from_the_settings(rsa_key: rsa.RSAPrivateKey) -> None:
    raw = json.dumps(key_info(rsa_key))
    with override_settings(GMAIL_SENDER=SENDER, GMAIL_SERVICE_ACCOUNT_JSON=raw):
        first = mail._base_sender()
        assert isinstance(first, gmail.GmailApiSender)
        assert mail._base_sender() is first
    with override_settings(GMAIL_SENDER=SENDER, GMAIL_SERVICE_ACCOUNT_JSON="not json"):
        with pytest.raises(mail.MailUnavailable) as raised:
            mail._base_sender()
    assert raised.value.cause == "KeyInvalid"


# --- The Gmail sender ---------------------------------------------------------------------


def test_the_token_exchange_signs_a_delegated_assertion_and_the_send_posts_raw_mime(
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    recorder = Recorder()
    sender, sleeps = sender_for(rsa_key, recorder, Clock(1_790_000_000))
    receipt = sender.send(a_message())
    assert receipt.provider_message_id == "18f0a1b2c3d4e5f6"
    assert receipt.disposition == mail.SENT
    assert recorder.urls() == [gmail.TOKEN_URI, gmail.SEND_URL]
    assert sleeps == []

    token_request, send_request = recorder.requests
    form = parse_qs(token_request.content.decode())
    assert form["grant_type"] == [gmail.GRANT_TYPE]
    header, payload, signature = form["assertion"][0].split(".")
    claims = json.loads(_b64(payload))
    assert claims == {
        "iss": "mailer@finance-test.iam.example.com",
        "sub": SENDER,
        "scope": gmail.SCOPE,
        "aud": gmail.TOKEN_URI,
        "iat": 1_790_000_000,
        "exp": 1_790_000_000 + gmail.ASSERTION_SECONDS,
    }
    assert json.loads(_b64(header))["alg"] == "RS256"
    rsa_key.public_key().verify(
        _b64(signature),
        f"{header}.{payload}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    assert send_request.headers["Authorization"] == f"Bearer {TOKEN}"
    sent = sent_mime(send_request)
    assert (sent["From"], sent["To"], sent["Subject"]) == (
        SENDER,
        "ceo@example.com",
        "Friendly reminder",
    )


def test_the_token_is_kept_until_a_minute_before_it_expires(rsa_key: rsa.RSAPrivateKey) -> None:
    recorder = Recorder()
    clock = Clock()
    sender, _ = sender_for(rsa_key, recorder, clock)
    sender.send(a_message())
    clock.now += 3600 - gmail.TOKEN_MARGIN_SECONDS - 1
    sender.send(a_message())
    assert recorder.urls().count(gmail.TOKEN_URI) == 1
    clock.now += 2
    sender.send(a_message())
    assert recorder.urls().count(gmail.TOKEN_URI) == 2


def test_the_client_is_bounded() -> None:
    assert (gmail.CONNECT_TIMEOUT, gmail.READ_TIMEOUT, gmail.RETRY_DELAY) == (10, 30, 5)


def test_the_real_client_carries_the_limits_and_follows_no_redirect(
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    sender = gmail.GmailApiSender(key_info(rsa_key), SENDER)
    timeout = sender._client.timeout
    assert (timeout.connect, timeout.read) == (10, 30)
    assert sender._client.follow_redirects is False


@pytest.mark.parametrize(
    ("status", "name"),
    [(400, "BadRequest"), (403, "Forbidden"), (404, "NotFound"), (429, "RateLimited")],
)
def test_a_4xx_on_send_is_not_retried(rsa_key: rsa.RSAPrivateKey, status: int, name: str) -> None:
    recorder = Recorder(send=[httpx.Response(status, json={"error": {"message": TOKEN}})])
    sender, sleeps = sender_for(rsa_key, recorder)
    with pytest.raises(mail.MailUnavailable) as raised:
        sender.send(a_message())
    assert raised.value.cause == name
    assert recorder.urls() == [gmail.TOKEN_URI, gmail.SEND_URL]
    assert sleeps == []
    assert TOKEN not in str(raised.value)


def test_a_401_on_send_forgets_the_token_and_is_not_retried(rsa_key: rsa.RSAPrivateKey) -> None:
    recorder = Recorder(send=[httpx.Response(401)])
    sender, sleeps = sender_for(rsa_key, recorder)
    with pytest.raises(mail.MailUnavailable) as raised:
        sender.send(a_message())
    assert raised.value.cause == "Unauthorized"
    assert sleeps == []
    sender.send(a_message())  # a new token is asked for
    assert recorder.urls() == [gmail.TOKEN_URI, gmail.SEND_URL, gmail.TOKEN_URI, gmail.SEND_URL]


@pytest.mark.parametrize(("status", "name"), [(401, "Unauthorized"), (400, "BadRequest")])
def test_a_refused_token_request_sends_nothing(
    rsa_key: rsa.RSAPrivateKey, status: int, name: str
) -> None:
    recorder = Recorder(token=[httpx.Response(status, json={"error": "invalid_grant"})])
    sender, sleeps = sender_for(rsa_key, recorder)
    with pytest.raises(mail.MailUnavailable) as raised:
        sender.send(a_message())
    assert raised.value.cause == name
    assert recorder.urls() == [gmail.TOKEN_URI]
    assert sleeps == []


def test_a_5xx_is_retried_once_after_5_seconds(rsa_key: rsa.RSAPrivateKey) -> None:
    recorder = Recorder(send=[httpx.Response(503)])
    sender, sleeps = sender_for(rsa_key, recorder)
    assert sender.send(a_message()).provider_message_id == "18f0a1b2c3d4e5f6"
    assert recorder.urls() == [gmail.TOKEN_URI, gmail.SEND_URL, gmail.SEND_URL]
    assert sleeps == [5]


def test_two_5xx_answers_fail_as_a_server_error(rsa_key: rsa.RSAPrivateKey) -> None:
    recorder = Recorder(send=[httpx.Response(500), httpx.Response(502)])
    sender, sleeps = sender_for(rsa_key, recorder)
    with pytest.raises(mail.MailUnavailable) as raised:
        sender.send(a_message())
    assert raised.value.cause == "ServerError"
    assert recorder.urls().count(gmail.SEND_URL) == 2
    assert sleeps == [5]


def test_a_timeout_is_retried_once_then_named(rsa_key: rsa.RSAPrivateKey) -> None:
    timeout = httpx.ReadTimeout("timed out")
    recorder = Recorder(send=[timeout, httpx.ReadTimeout("timed out again")])
    sender, sleeps = sender_for(rsa_key, recorder)
    with pytest.raises(mail.MailUnavailable) as raised:
        sender.send(a_message())
    assert raised.value.cause == "ReadTimeout"
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert sleeps == [5]


def test_a_network_error_on_the_token_is_retried_once(rsa_key: rsa.RSAPrivateKey) -> None:
    recorder = Recorder(token=[httpx.ConnectError("refused")])
    sender, sleeps = sender_for(rsa_key, recorder)
    sender.send(a_message())
    assert recorder.urls() == [gmail.TOKEN_URI, gmail.TOKEN_URI, gmail.SEND_URL]
    assert sleeps == [5]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"id": "bad id with spaces"}),
        httpx.Response(200, json={}),
    ],
)
def test_an_unreadable_send_answer_fails(
    rsa_key: rsa.RSAPrivateKey, answer: httpx.Response
) -> None:
    sender, _ = sender_for(rsa_key, Recorder(send=[answer]))
    with pytest.raises(mail.MailUnavailable) as raised:
        sender.send(a_message())
    assert raised.value.cause == "SendResponseInvalid"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"access_token": "", "expires_in": 3600}),
        httpx.Response(200, json={"access_token": TOKEN}),
    ],
)
def test_an_unreadable_token_answer_fails(
    rsa_key: rsa.RSAPrivateKey, answer: httpx.Response
) -> None:
    recorder = Recorder(token=[answer])
    sender, _ = sender_for(rsa_key, recorder)
    with pytest.raises(mail.MailUnavailable) as raised:
        sender.send(a_message())
    assert raised.value.cause == "TokenResponseInvalid"
    assert gmail.SEND_URL not in recorder.urls()


def test_a_redirect_is_not_followed(rsa_key: rsa.RSAPrivateKey) -> None:
    recorder = Recorder(send=[httpx.Response(302, headers={"Location": "https://evil.example"})])
    sender, _ = sender_for(rsa_key, recorder)
    with pytest.raises(mail.MailUnavailable) as raised:
        sender.send(a_message())
    assert raised.value.cause == "UnexpectedRedirect"
    assert all("evil" not in url for url in recorder.urls())


def test_a_key_naming_another_token_endpoint_is_refused(rsa_key: rsa.RSAPrivateKey) -> None:
    with pytest.raises(mail.MailUnavailable) as raised:
        gmail.GmailApiSender(key_info(rsa_key, token_uri="https://token.example.com/"), SENDER)
    assert raised.value.cause == "TokenUriRefused"


def test_an_unusable_key_fails_by_class_name_without_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(mail.MailUnavailable) as raised:
        gmail.GmailApiSender(marker_info(), SENDER)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert PRIVATE_KEY_MARKER not in str(raised.value)
    assert PRIVATE_KEY_MARKER not in caplog.text


def test_no_log_record_carries_the_key_or_the_token(
    rsa_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    info = key_info(rsa_key)
    recorder = Recorder(send=[httpx.Response(500), httpx.Response(500)])
    sender, _ = sender_for(rsa_key, recorder)
    with pytest.raises(mail.MailUnavailable):
        sender.send(a_message())
    sender2, _ = sender_for(rsa_key, Recorder())
    sender2.send(a_message())
    body_lines = [line for line in info["private_key"].splitlines() if "-----" not in line]
    for record in caplog.records:
        text = record.getMessage()
        assert TOKEN not in text
        assert all(line not in text for line in body_lines)
    assert "ServerError" in caplog.text
    assert repr(sender) == "<GmailApiSender>"


def test_the_log_redaction_covers_the_key(rsa_key: rsa.RSAPrivateKey) -> None:
    info = key_info(rsa_key)
    raw = json.dumps(info)
    secrets = service_account_secrets(raw)
    assert raw in secrets and info["private_key"] in secrets
    assert info["private_key"].replace("\n", "\\n") in secrets
    assert info["private_key_id"] in secrets
    lines = [line for line in info["private_key"].splitlines() if "-----" not in line]
    assert all(line in secrets for line in lines if len(line) >= 8)
    assert service_account_secrets("") == ()


def test_the_running_formatter_redacts_a_service_account_key(rsa_key: rsa.RSAPrivateKey) -> None:
    from finance.log_redaction import RedactingFormatter

    info = key_info(rsa_key)
    formatter = RedactingFormatter("%(message)s", secrets=service_account_secrets(json.dumps(info)))
    record = logging.LogRecord(
        "x", logging.WARNING, __file__, 1, "key %s", (info["private_key"],), None
    )
    assert info["private_key"] not in formatter.format(record)
    assert "[REDACTED]" in formatter.format(record)


# --- The environment guard ----------------------------------------------------------------


def key_json(**overrides: Any) -> str:
    return json.dumps({**marker_info(), **overrides})


MAIL_ON = {
    "MAIL_ENABLED": "true",
    "GMAIL_SENDER": SENDER,
    "GMAIL_SERVICE_ACCOUNT_JSON": key_json(),
    "MAIL_CATCH_ALL": CATCH_ALL,
}


def test_mail_is_off_by_default_and_needs_nothing() -> None:
    config = validate(DEPLOYED_ENV)
    assert config.mail_enabled is False
    assert (config.gmail_sender, config.mail_catch_all) == ("", "")
    assert config.company_name == "Global Retail Groups"


def test_mail_on_with_everything_is_valid_and_keeps_the_key_out_of_repr() -> None:
    config = validate(_env(DEPLOYED_ENV, **MAIL_ON))
    assert config.mail_enabled is True
    assert config.gmail_service_account_json == MAIL_ON["GMAIL_SERVICE_ACCOUNT_JSON"]
    assert PRIVATE_KEY_MARKER not in repr(config)


@pytest.mark.parametrize("app_env", ["staging", "local"])
def test_the_guard_refuses_to_boot_without_a_catch_all_outside_production(app_env: str) -> None:
    base = DEPLOYED_ENV if app_env == "staging" else LOCAL_ENV
    with pytest.raises(EnvGuardError) as raised:
        validate(_env(base, **{**MAIL_ON, "MAIL_CATCH_ALL": None}))
    assert "MAIL_CATCH_ALL is required" in str(raised.value)


def test_production_may_run_without_a_catch_all() -> None:
    env = _env(DEPLOYED_ENV, APP_ENV="production", **{**MAIL_ON, "MAIL_CATCH_ALL": None})
    assert validate(env).mail_catch_all == ""


def test_mail_off_needs_no_catch_all_anywhere() -> None:
    assert validate(_env(DEPLOYED_ENV, MAIL_ENABLED="false")).mail_enabled is False


@pytest.mark.parametrize("name", ["GMAIL_SENDER", "GMAIL_SERVICE_ACCOUNT_JSON"])
def test_mail_on_needs_the_mailbox_and_the_key(name: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate(_env(DEPLOYED_ENV, **{**MAIL_ON, name: None}))
    assert f"{name} is required when MAIL_ENABLED is true" in str(raised.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f'{{"private_key": "{PRIVATE_KEY_MARKER}"', "is not valid JSON"),
        (json.dumps([PRIVATE_KEY_MARKER]), "must be a JSON object"),
        (key_json(client_email=None), "lacks the keys: client_email"),
        (
            json.dumps({"private_key": PRIVATE_KEY_MARKER}),
            "lacks the keys: client_email, token_uri",
        ),
        (key_json(private_key=""), "lacks the keys: private_key"),
    ],
)
def test_a_malformed_key_is_refused_without_a_value(raw: str, expected: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate(_env(DEPLOYED_ENV, **{**MAIL_ON, "GMAIL_SERVICE_ACCOUNT_JSON": raw}))
    message = str(raised.value)
    assert expected in message
    assert PRIVATE_KEY_MARKER not in message
    assert "mailer@" not in message


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAIL_ENABLED", "yes"),
        ("GMAIL_SENDER", "Billing <billing@example.com>"),
        ("MAIL_CATCH_ALL", "a@example.com,b@example.com"),
        ("COMPANY_NAME", "Line one\nBcc: x@example.com"),
        ("COMPANY_NAME", "x" * 201),
    ],
)
def test_bad_mail_values_are_refused_by_name(name: str, value: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate(_env(DEPLOYED_ENV, **{**MAIL_ON, name: value}))
    assert name in str(raised.value)
    assert value not in str(raised.value)


# --- The import fence -----------------------------------------------------------------------


def _imports_google(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "google" or alias.name.startswith("google.") for alias in node.names
        ):
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "google" or node.module.startswith("google."))
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in {"import_module", "__import__"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).startswith("google")
        ):
            return True
    return False


def test_the_fence_check_detects_each_form() -> None:
    for source in (
        "import google.auth",
        "import google",
        "from google.oauth2 import service_account",
        "from google import auth",
        "importlib.import_module('google.auth.jwt')",
        "__import__('google.oauth2')",
    ):
        assert _imports_google(ast.parse(source)), source
    assert not _imports_google(ast.parse("import mail\nfrom mail import gmail"))


def _python_files() -> Iterator[Path]:
    for path in BACKEND.rglob("*.py"):
        if "staticfiles" not in path.parts:
            yield path


def test_nothing_outside_the_mail_package_imports_google() -> None:
    offenders = [
        str(path.relative_to(BACKEND))
        for path in _python_files()
        if path.relative_to(BACKEND).parts[0] != "mail"
        and _imports_google(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []


def test_only_the_gmail_module_imports_google_inside_the_package() -> None:
    importers = sorted(
        path.name
        for path in (BACKEND / "mail").rglob("*.py")
        if _imports_google(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert importers == ["gmail.py"]
