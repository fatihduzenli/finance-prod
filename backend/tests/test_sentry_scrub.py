"""The Sentry before_send scrubber (plan §3)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
import sentry_sdk
from sentry_sdk.types import BreadcrumbHint, Event, Hint

from finance import sentry as sentry_module
from finance.sentry import FILTERED, init, scrub_breadcrumb, scrub_event


def _event() -> dict[str, Any]:
    return {
        "message": "something broke",
        "level": "error",
        "request": {
            "url": "https://staging.example.com/admin/",
            "method": "POST",
            "headers": {"Authorization": "<placeholder-token>", "Host": "example.com"},
            "cookies": {"sessionid": "placeholder"},
            "data": {"username": "staff", "password": "placeholder"},
            "query_string": "next=/admin/",
        },
        "user": {
            "id": "1",
            "username": "staff",
            "email": "person@example.com",
            "ip_address": "203.0.113.7",
        },
        "extra": {
            "stax_api_key": "FAKE-TEST-KEY-do-not-use-XYZ123",
            "STAX-API-KEY": "FAKE-TEST-KEY-do-not-use-XYZ123",
            "client_secret": "placeholder",
            "card_last_four": "0000",
            "routing_number": "000000000",
            "account": "placeholder",
            "nested": [{"authorization": "<placeholder-token>"}, {"safe": "keep me"}],
            "correlation_id": "abc-123",
        },
    }


def _scrub(event: dict[str, Any]) -> dict[str, Any]:
    result = scrub_event(cast("Event", event), cast("Hint", {}))
    assert result is not None
    return cast("dict[str, Any]", result)


def test_request_detail_is_dropped() -> None:
    scrubbed = _scrub(_event())
    request = scrubbed["request"]
    for key in ("headers", "cookies", "data", "query_string"):
        assert key not in request
    # Non-sensitive request context survives, so an error is still diagnosable. The URL
    # no longer does (PLAN-008 D11); see test_request_url_is_dropped below.
    assert request["method"] == "POST"


def test_user_identifiers_are_dropped() -> None:
    user = _scrub(_event())["user"]
    assert "email" not in user
    assert "ip_address" not in user
    assert user["id"] == "1"


def test_sensitive_keys_are_filtered_at_any_depth() -> None:
    extra = _scrub(_event())["extra"]
    for key in (
        "stax_api_key",
        "STAX-API-KEY",
        "client_secret",
        "card_last_four",
        "routing_number",
        "account",
    ):
        assert extra[key] == FILTERED
    assert extra["nested"][0]["authorization"] == FILTERED
    assert extra["nested"][1]["safe"] == "keep me"
    assert extra["correlation_id"] == "abc-123"


def test_no_key_value_survives_anywhere_in_the_event() -> None:
    scrubbed = _scrub(_event())
    assert "FAKE-TEST-KEY-do-not-use-XYZ123" not in repr(scrubbed)
    assert "<placeholder-token>" not in repr(scrubbed)
    assert "person@example.com" not in repr(scrubbed)


def test_a_plain_message_survives() -> None:
    scrubbed = _scrub({"message": "something broke", "level": "error"})
    assert scrubbed["message"] == "something broke"
    assert scrubbed["level"] == "error"


def test_the_caller_event_is_not_mutated() -> None:
    event = _event()
    _scrub(event)
    assert "headers" in event["request"]
    assert event["extra"]["stax_api_key"] == "FAKE-TEST-KEY-do-not-use-XYZ123"


def test_init_without_a_dsn_does_nothing() -> None:
    init(dsn="", app_env="local", stax_env="sandbox")
    assert not sentry_sdk.get_client().is_active()


def test_request_url_is_dropped() -> None:
    """PLAN-008 D11: the webhook route carries its token in the path."""
    event = _event()
    event["request"]["url"] = "https://staging.example.com/api/webhooks/stax/" + "t" * 64 + "/"
    scrubbed = _scrub(event)
    assert "url" not in scrubbed["request"]
    assert "t" * 64 not in repr(scrubbed)


# --- Breadcrumbs and secret values (PLAN-008 finding: HTTP breadcrumbs) ----------------
# Placeholders only; neither is a real credential.
TOKEN = "placeholder-webhook-url-token-" + "0" * 40
KEY = "FAKE-SENTRY-TEST-KEY-do-not-use-ABC789"


@pytest.fixture
def secrets_configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Restore whatever settings.py configured once the test is over.
    monkeypatch.setattr(sentry_module, "_secret_pattern", sentry_module._secret_pattern)
    init(
        dsn="", app_env="local", stax_env="sandbox", stax_api_key=KEY, stax_webhook_url_token=TOKEN
    )
    yield


def _http_crumb() -> dict[str, Any]:
    target = f"https://staging.example.com/api/webhooks/stax/{TOKEN}/"
    return {
        "type": "http",
        "category": "httplib",
        "data": {
            "method": "POST",
            "url": "https://apiprod.example.com/webhook",
            "http.query": f"target_url={target}&event=create_transaction",
            "http.fragment": "frag",
            "status_code": 200,
            "reason": "OK",
        },
    }


@pytest.mark.parametrize("category", ["httplib", "http", "httpx"])
def test_before_breadcrumb_keeps_only_method_and_status(category: str) -> None:
    crumb = _http_crumb()
    crumb["category"] = category
    result = scrub_breadcrumb(crumb, cast("BreadcrumbHint", {}))
    assert result is not None
    assert result["data"] == {"method": "POST", "status_code": 200}
    assert TOKEN not in repr(result)


def test_before_breadcrumb_strips_url_data_of_any_category() -> None:
    crumb = {
        "category": "custom",
        "data": {"url": f"https://x.example.com/{TOKEN}", "method": "GET"},
    }
    result = scrub_breadcrumb(crumb, cast("BreadcrumbHint", {}))
    assert result is not None
    assert result["data"] == {"method": "GET"}


def test_before_breadcrumb_leaves_other_breadcrumbs_alone() -> None:
    crumb = {"category": "query", "message": "SELECT 1", "data": {"duration": 3}}
    result = scrub_breadcrumb(crumb, cast("BreadcrumbHint", {}))
    assert result == {"category": "query", "message": "SELECT 1", "data": {"duration": 3}}


def test_init_registers_the_breadcrumb_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda *args: None)
    monkeypatch.setattr(sentry_module, "_secret_pattern", sentry_module._secret_pattern)
    init(dsn="https://public@sentry.example.com/1", app_env="local", stax_env="sandbox")
    assert captured["before_breadcrumb"] is scrub_breadcrumb
    assert captured["before_send"] is scrub_event


@pytest.mark.usefixtures("secrets_configured")
def test_event_breadcrumbs_lose_url_detail_and_sensitive_keys() -> None:
    event = _event()
    event["breadcrumbs"] = {
        "values": [
            _http_crumb(),
            {"category": "log", "message": "ok", "data": {"authorization": "<placeholder-token>"}},
        ]
    }
    crumbs = _scrub(event)["breadcrumbs"]["values"]
    assert crumbs[0]["data"] == {"method": "POST", "status_code": 200}
    assert crumbs[1]["data"]["authorization"] == FILTERED
    assert crumbs[1]["message"] == "ok"


@pytest.mark.usefixtures("secrets_configured")
def test_secret_values_are_replaced_in_every_string() -> None:
    event = {
        "message": f"failed for {TOKEN}",
        "level": "error",
        "extra": {"note": f"key was {KEY}", "list": [f"x{TOKEN}y"]},
        "exception": {"values": [{"type": "StaxError", "value": f"bad key {KEY} and {TOKEN}"}]},
        "breadcrumbs": {
            "values": [{"category": "log", "message": f"called with {KEY}", "data": {}}]
        },
    }
    scrubbed = _scrub(event)
    assert TOKEN not in repr(scrubbed)
    assert KEY not in repr(scrubbed)
    assert scrubbed["message"] == f"failed for {FILTERED}"
    assert scrubbed["extra"]["note"] == f"key was {FILTERED}"
    assert scrubbed["extra"]["list"] == [f"x{FILTERED}y"]
    assert scrubbed["exception"]["values"][0]["value"] == f"bad key {FILTERED} and {FILTERED}"
    assert scrubbed["breadcrumbs"]["values"][0]["message"] == f"called with {FILTERED}"


def test_url_quoted_secret_values_are_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sentry_module, "_secret_pattern", sentry_module._secret_pattern)
    reserved = "FAKE/SENTRY+TEST=KEY do-not-use"
    init(dsn="", app_env="local", stax_env="sandbox", stax_api_key=reserved)
    message = "a=FAKE%2FSENTRY%2BTEST%3DKEY%20do-not-use b=FAKE%2FSENTRY%2BTEST%3DKEY+do-not-use"
    scrubbed = _scrub({"message": message, "level": "error"})
    assert scrubbed["message"] == f"a={FILTERED} b={FILTERED}"


def test_empty_secret_values_replace_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sentry_module, "_secret_pattern", sentry_module._secret_pattern)
    init(dsn="", app_env="local", stax_env="sandbox", stax_api_key="", stax_webhook_url_token="")
    scrubbed = _scrub({"message": "something broke", "level": "error"})
    assert scrubbed["message"] == "something broke"
