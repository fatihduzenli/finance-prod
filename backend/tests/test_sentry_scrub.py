"""The Sentry before_send scrubber (plan §3)."""

from __future__ import annotations

from typing import Any, cast

import sentry_sdk
from sentry_sdk.types import Event, Hint

from finance.sentry import FILTERED, init, scrub_event


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
    # Non-sensitive request context survives, so an error is still diagnosable.
    assert request["url"] == "https://staging.example.com/admin/"
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
