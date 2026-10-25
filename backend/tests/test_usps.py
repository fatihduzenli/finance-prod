"""The USPS address check (PLAN-012f-b D13): ``finance.usps``, its environment pair, the
``ledger:stores:check_address`` route and the store dialog's "Check address".

No test reaches the network: every request goes to an ``httpx.MockTransport``, and an
autouse fixture makes httpx's real transport fail the test if anything gets past it. The
credentials are obvious placeholders."""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from django.test import override_settings
from django.urls import reverse

from accounts.roles import Role
from finance import usps
from finance.env_guard import EnvGuardError, validate
from ledger.views import stores as store_views
from tests.receivables_helpers import a_store
from tests.test_lease_end_drafts import row_counts
from tests.test_operator_views import client_for

BASE = "https://usps.example.com"
CLIENT_ID = "<USPS_CLIENT_ID>"
CLIENT_SECRET = "placeholder-usps-client-secret"  # noqa: S105 - a placeholder, not a secret
TOKEN = "placeholder-usps-access-token"  # noqa: S105 - a placeholder, not a secret
CONFIGURED = {
    "USPS_CLIENT_ID": CLIENT_ID,
    "USPS_CLIENT_SECRET": CLIENT_SECRET,
    "USPS_API_BASE_URL": BASE,
}
ADDRESS = usps.Address(street="3120 m st", city="washington", state="DC", zip="20007")
STANDARDISED: dict[str, Any] = {
    "firm": "",
    "address": {
        "streetAddress": "3120 M ST NW",
        "secondaryAddress": "",
        "city": "WASHINGTON",
        "state": "DC",
        "ZIPCode": "20007",
        "ZIPPlus4": "3704",
    },
    "additionalInfo": {"DPVConfirmation": "Y", "business": "Y"},
    "corrections": [],
    "matches": [{"code": "31", "text": "Single Response - exact match"}],
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise AssertionError("a test tried to reach the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    usps.forget_tokens()
    yield
    usps.forget_tokens()


class Fake:
    """A USPS stand-in: answers the token and address paths, and records every request."""

    def __init__(
        self,
        *,
        token: Callable[[httpx.Request], httpx.Response] | None = None,
        address: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._token = token or (
            lambda _r: httpx.Response(
                200, json={"access_token": TOKEN, "token_type": "Bearer", "expires_in": "28800"}
            )
        )
        self._address = address or (lambda _r: httpx.Response(200, json=STANDARDISED))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == usps.TOKEN_PATH:
            return self._token(request)
        if request.url.path == usps.ADDRESS_PATH:
            return self._address(request)
        return httpx.Response(418)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def run(fake: Fake, address: usps.Address = ADDRESS, **kwargs: Any) -> Any:
    with override_settings(**CONFIGURED):
        return usps.check(address, transport=fake.transport, **kwargs)


# --- The client --------------------------------------------------------------------------


def test_a_standardised_answer_is_checked_with_its_zip4() -> None:
    fake = Fake()
    assert run(fake) == usps.Checked(
        street="3120 M ST NW",
        city="WASHINGTON",
        state="DC",
        zip5="20007",
        zip4="3704",
        dpv_confirmed=True,
    )
    assert fake.paths() == [usps.TOKEN_PATH, usps.ADDRESS_PATH]


def test_the_credentials_go_to_the_token_url_only_and_nothing_else() -> None:
    fake = Fake()
    run(fake)
    token_request, address_request = fake.requests
    assert str(token_request.url) == BASE + usps.TOKEN_PATH
    assert token_request.method == "POST"
    assert token_request.headers["content-type"] == "application/json"
    assert json.loads(token_request.content) == {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    assert "authorization" not in token_request.headers
    # The address call carries the token, never the client's ID or secret.
    assert address_request.method == "GET"
    assert address_request.headers["authorization"] == f"Bearer {TOKEN}"
    sent = (
        str(address_request.url) + str(address_request.headers) + address_request.content.decode()
    )
    assert CLIENT_SECRET not in sent and CLIENT_ID not in sent
    assert dict(address_request.url.params) == {
        "streetAddress": "3120 m st",
        "state": "DC",
        "city": "washington",
        "ZIPCode": "20007",
    }


def test_a_zip_plus_4_typed_by_staff_is_sent_as_its_five_digits() -> None:
    fake = Fake()
    run(fake, usps.Address(street="1 Main St", city="", state="ny", zip=" 10001-1234 "))
    params = dict(fake.requests[-1].url.params)
    assert params == {"streetAddress": "1 Main St", "state": "NY", "ZIPCode": "10001"}


def test_a_secondary_address_is_kept_on_the_street_line_and_a_null_zip4_is_empty() -> None:
    answer = json.loads(json.dumps(STANDARDISED))
    answer["address"].update(secondaryAddress="STE 200", ZIPPlus4=None)
    answer["additionalInfo"]["DPVConfirmation"] = "S"
    checked = run(Fake(address=lambda _r: httpx.Response(200, json=answer)))
    assert isinstance(checked, usps.Checked)
    assert checked.street == "3120 M ST NW STE 200"
    assert checked.zip4 == "" and checked.dpv_confirmed is False


def test_a_404_is_no_match() -> None:
    fake = Fake(address=lambda _r: httpx.Response(404, json={"error": {"message": "x"}}))
    assert run(fake) == usps.NoMatch()


@pytest.mark.parametrize(
    "address",
    [
        usps.Address(street="1 Main St", city="Springfield", state="", zip=""),
        usps.Address(street="1 Main St", city="Springfield", state="ZZ", zip=""),
        usps.Address(street="1 Main St", city="", state="IL", zip="abc"),
        usps.Address(street="   ", city="Springfield", state="IL", zip="62701"),
    ],
)
def test_an_address_usps_cannot_be_asked_about_is_no_match_without_a_request(
    address: usps.Address,
) -> None:
    fake = Fake()
    assert run(fake, address) == usps.NoMatch()
    assert fake.requests == []


def _status(code: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(code, json={"error": "x"})


def _raise(error: type[httpx.RequestError]) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        raise error("refused", request=request)

    return answer


@pytest.mark.parametrize(
    "fake",
    [
        pytest.param(Fake(token=_status(401)), id="token 401"),
        pytest.param(Fake(token=_status(500)), id="token 500"),
        pytest.param(Fake(token=_raise(httpx.ConnectTimeout)), id="token timeout"),
        pytest.param(
            Fake(token=lambda _r: httpx.Response(200, text="<html>")), id="token not json"
        ),
        pytest.param(
            Fake(token=lambda _r: httpx.Response(200, json={"x": 1})), id="token no field"
        ),
        pytest.param(
            Fake(
                token=lambda _r: httpx.Response(200, json={"access_token": TOKEN, "expires_in": 0})
            ),
            id="token no lifetime",
        ),
        pytest.param(
            Fake(token=lambda _r: httpx.Response(302, headers={"location": "https://x.example"})),
            id="token redirect",
        ),
        pytest.param(Fake(address=_status(401)), id="address 401"),
        pytest.param(Fake(address=_status(400)), id="address 400"),
        pytest.param(Fake(address=_status(429)), id="address 429"),
        pytest.param(Fake(address=_status(500)), id="address 500"),
        pytest.param(Fake(address=_status(503)), id="address 503"),
        pytest.param(Fake(address=_raise(httpx.ReadTimeout)), id="address timeout"),
        pytest.param(Fake(address=_raise(httpx.ConnectError)), id="address connect error"),
        pytest.param(
            Fake(address=lambda _r: httpx.Response(200, text="nope")), id="address not json"
        ),
        pytest.param(Fake(address=lambda _r: httpx.Response(200, json=[])), id="address a list"),
        pytest.param(Fake(address=lambda _r: httpx.Response(200, json={})), id="empty candidate"),
        pytest.param(
            Fake(address=lambda _r: httpx.Response(200, json={"address": {"streetAddress": ""}})),
            id="empty street",
        ),
        pytest.param(
            Fake(
                address=lambda _r: httpx.Response(
                    200, json={"address": {**STANDARDISED["address"], "ZIPCode": "2000"}}
                )
            ),
            id="bad zip",
        ),
        pytest.param(
            Fake(
                address=lambda _r: httpx.Response(
                    200, json={"address": {**STANDARDISED["address"], "ZIPPlus4": "37"}}
                )
            ),
            id="bad zip4",
        ),
        pytest.param(
            Fake(
                address=lambda _r: httpx.Response(
                    200, json={"address": {**STANDARDISED["address"], "state": "<b>"}}
                )
            ),
            id="bad state",
        ),
    ],
)
def test_every_failure_is_none(fake: Fake) -> None:
    assert run(fake) is None


def test_a_redirect_is_not_followed() -> None:
    fake = Fake(address=lambda _r: httpx.Response(301, headers={"location": BASE + "/elsewhere"}))
    assert run(fake) is None
    assert fake.paths() == [usps.TOKEN_PATH, usps.ADDRESS_PATH]


def test_the_timeouts_are_five_seconds() -> None:
    seen: list[dict[str, Any]] = []

    def address(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=STANDARDISED)

    run(Fake(address=address))
    assert seen == [{"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_token_is_reused_and_renewed_a_minute_before_it_expires() -> None:
    fake = Fake(
        token=lambda _r: httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
    )
    clock = Clock()
    for _ in range(3):
        assert isinstance(run(fake, clock=clock), usps.Checked)
    assert fake.paths().count(usps.TOKEN_PATH) == 1
    clock.now += 3600 - usps.TOKEN_MARGIN_SECONDS - 1
    run(fake, clock=clock)
    assert fake.paths().count(usps.TOKEN_PATH) == 1
    clock.now += 1
    run(fake, clock=clock)
    assert fake.paths().count(usps.TOKEN_PATH) == 2
    assert fake.paths().count(usps.ADDRESS_PATH) == 5


def test_a_401_on_the_address_call_forgets_the_token() -> None:
    answers = iter([httpx.Response(401), httpx.Response(200, json=STANDARDISED)])
    fake = Fake(address=lambda _r: next(answers))
    assert run(fake) is None
    assert isinstance(run(fake), usps.Checked)
    assert fake.paths().count(usps.TOKEN_PATH) == 2


def test_another_client_id_does_not_reuse_the_cached_token() -> None:
    fake = Fake()
    run(fake)
    with override_settings(**{**CONFIGURED, "USPS_CLIENT_ID": "<ANOTHER_CLIENT_ID>"}):
        usps.check(ADDRESS, transport=fake.transport)
    assert fake.paths().count(usps.TOKEN_PATH) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"USPS_CLIENT_ID": "", "USPS_CLIENT_SECRET": ""},
        {"USPS_CLIENT_ID": CLIENT_ID, "USPS_CLIENT_SECRET": ""},
        {"USPS_CLIENT_ID": "", "USPS_CLIENT_SECRET": CLIENT_SECRET},
    ],
)
def test_with_the_check_off_nothing_is_requested(overrides: dict[str, str]) -> None:
    fake = Fake()
    with override_settings(**{**CONFIGURED, **overrides}):
        assert usps.is_configured() is False
        assert usps.check(ADDRESS, transport=fake.transport) is None
    assert fake.requests == []


def test_the_log_holds_the_host_status_and_latency_only(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="finance.usps"):
        run(Fake())
        run(Fake(address=_raise(httpx.ReadTimeout)))
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "usps.example.com answered 200" in text
    assert "usps.example.com failed: ReadTimeout" in text
    for secret in (CLIENT_ID, CLIENT_SECRET, TOKEN, "3120", "washington", "WASHINGTON", "20007"):
        assert secret not in text


# --- The environment ---------------------------------------------------------------------

BASE_ENV = {
    "APP_ENV": "staging",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "STAX_API_KEY": "FAKE-CI-KEY-XYZ123",
    "STAX_WEBHOOK_URL_TOKEN": "t" * 64,
    "DATABASE_URL": "postgresql://user@db.example.com/finance",
    "APP_BASE_URL": "https://app.example.com",
    "DJANGO_SECRET_KEY": "placeholder-not-secret",
}


def test_the_pair_defaults_to_off_and_the_usps_host() -> None:
    config = validate(BASE_ENV)
    assert (config.usps_client_id, config.usps_client_secret) == ("", "")
    assert config.usps_api_base_url == "https://apis.usps.com"


def test_the_pair_is_read_and_kept_out_of_repr() -> None:
    config = validate(
        {**BASE_ENV, "USPS_CLIENT_ID": CLIENT_ID, "USPS_CLIENT_SECRET": CLIENT_SECRET}
    )
    assert (config.usps_client_id, config.usps_client_secret) == (CLIENT_ID, CLIENT_SECRET)
    assert CLIENT_ID not in repr(config) and CLIENT_SECRET not in repr(config)


@pytest.mark.parametrize(
    "pair",
    [{"USPS_CLIENT_ID": CLIENT_ID}, {"USPS_CLIENT_SECRET": CLIENT_SECRET}],
)
def test_one_without_the_other_is_refused_with_a_value_free_message(pair: dict[str, str]) -> None:
    with pytest.raises(EnvGuardError) as refused:
        validate({**BASE_ENV, **pair})
    message = str(refused.value)
    assert "USPS_CLIENT_ID" in message and "USPS_CLIENT_SECRET" in message
    assert CLIENT_ID not in message and CLIENT_SECRET not in message


def test_the_base_url_may_differ_only_locally() -> None:
    with pytest.raises(EnvGuardError) as refused:
        validate({**BASE_ENV, "USPS_API_BASE_URL": BASE})
    assert "USPS_API_BASE_URL" in str(refused.value) and BASE not in str(refused.value)
    local = {**BASE_ENV, "APP_ENV": "local", "USPS_API_BASE_URL": BASE}
    assert validate(local).usps_api_base_url == BASE
    with pytest.raises(EnvGuardError):
        validate({**local, "USPS_API_BASE_URL": "ftp://usps.example.com"})
    explicit = {**BASE_ENV, "USPS_API_BASE_URL": "https://apis.usps.com"}
    assert validate(explicit).usps_api_base_url == "https://apis.usps.com"


# --- The route ---------------------------------------------------------------------------

URL = "/stores/check-address/"
POSTED = {"street": "3120 m st", "city": "washington", "state": "DC", "zip": "20007"}


def use_fake(monkeypatch: pytest.MonkeyPatch, fake: Fake) -> None:
    monkeypatch.setattr(usps, "check", functools.partial(usps.check, transport=fake.transport))


needs_db = pytest.mark.django_db


@needs_db
def test_the_route_is_named_under_stores() -> None:
    assert reverse("ledger:stores:check_address") == URL


@needs_db
def test_billing_gets_the_standardised_address_as_json_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake()
    use_fake(monkeypatch, fake)
    billing, _ = client_for(Role.BILLING)
    before = row_counts()
    with override_settings(**CONFIGURED):
        response = billing.post(URL, POSTED)
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    assert response.json() == {
        "verified": True,
        "address": {
            "street": "3120 M ST NW",
            "city": "WASHINGTON",
            "state": "DC",
            "zip5": "20007",
            "zip4": "3704",
            "dpv_confirmed": True,
        },
    }
    assert row_counts() == before
    body = response.content.decode()
    assert CLIENT_SECRET not in body and TOKEN not in body


@needs_db
@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        (Fake(address=_status(404)), store_views.CHECK_NO_MATCH),
        (Fake(address=_status(500)), store_views.CHECK_FAILED),
        (Fake(token=_raise(httpx.ConnectTimeout)), store_views.CHECK_FAILED),
    ],
)
def test_a_failed_check_is_a_plain_reason(
    monkeypatch: pytest.MonkeyPatch, fake: Fake, reason: str
) -> None:
    use_fake(monkeypatch, fake)
    billing, _ = client_for(Role.BILLING)
    with override_settings(**CONFIGURED):
        response = billing.post(URL, POSTED)
    assert response.status_code == 200
    assert response.json() == {"verified": False, "reason": reason}


@needs_db
def test_with_the_check_off_the_route_says_so_and_asks_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake()
    use_fake(monkeypatch, fake)
    billing, _ = client_for(Role.BILLING)
    with override_settings(USPS_CLIENT_ID="", USPS_CLIENT_SECRET=""):
        response = billing.post(URL, POSTED)
    assert response.json() == {"verified": False, "reason": "The address check is not set up."}
    assert fake.requests == []


@needs_db
def test_each_field_is_cut_to_100_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Fake()
    use_fake(monkeypatch, fake)
    billing, _ = client_for(Role.BILLING)
    with override_settings(**CONFIGURED):
        billing.post(URL, {**POSTED, "street": "9" * 5000, "city": "c" * 5000})
    params = dict(fake.requests[-1].url.params)
    assert params["streetAddress"] == "9" * 100 and params["city"] == "c" * 100


@needs_db
@pytest.mark.parametrize("street", ["", "   "])
def test_an_empty_street_is_400(monkeypatch: pytest.MonkeyPatch, street: str) -> None:
    fake = Fake()
    use_fake(monkeypatch, fake)
    billing, _ = client_for(Role.BILLING)
    with override_settings(**CONFIGURED):
        response = billing.post(URL, {**POSTED, "street": street})
    assert response.status_code == 400
    assert response.json()["verified"] is False
    assert fake.requests == []


@needs_db
def test_a_viewer_is_refused_and_nothing_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Fake()
    use_fake(monkeypatch, fake)
    viewer, _ = client_for(Role.VIEWER)
    before = row_counts()
    with override_settings(**CONFIGURED):
        response = viewer.post(URL, POSTED)
    assert response.status_code == 403
    assert fake.requests == []
    assert row_counts() == before


@needs_db
@pytest.mark.parametrize(
    ("method", "status"),
    # GET reaches the view and is 405; any other method is refused by the route registry
    # (accounts.registry: the rule names POST only) before the view.
    [("get", 405), ("head", 405), ("put", 403), ("delete", 403)],
)
def test_only_post_is_allowed(monkeypatch: pytest.MonkeyPatch, method: str, status: int) -> None:
    fake = Fake()
    use_fake(monkeypatch, fake)
    billing, _ = client_for(Role.BILLING)
    with override_settings(**CONFIGURED):
        response = getattr(billing, method)(URL)
    assert response.status_code == status
    assert fake.requests == []


# --- The store dialog --------------------------------------------------------------------


@needs_db
def test_the_dialog_offers_check_address_only_when_configured() -> None:
    store = a_store("Check address store", 4242)
    admin, _ = client_for(Role.ADMIN)
    edit = reverse("ledger:stores:edit", kwargs={"pk": store.pk})
    for url in (reverse("ledger:stores:create"), edit):
        with override_settings(**CONFIGURED):
            on = admin.get(url).content.decode()
        assert 'id="check-address"' in on and 'type="button"' in on
        assert f'data-url="{URL}"' in on
        assert 'id="address-check" class="address-check" aria-live="polite"' in on
        assert "ledger/address_check.js" in on
        assert "Address not verified" not in on
        assert CLIENT_ID not in on and CLIENT_SECRET not in on
        with override_settings(USPS_CLIENT_ID="", USPS_CLIENT_SECRET=""):
            off = admin.get(url).content.decode()
        assert 'id="check-address"' not in off and "address_check.js" not in off
    # Off, a saved store's form says its address is not verified; a new store's says nothing.
    assert "Address not verified" in off
    with override_settings(USPS_CLIENT_ID="", USPS_CLIENT_SECRET=""):
        new = admin.get(reverse("ledger:stores:create")).content.decode()
    assert "Address not verified" not in new


@needs_db
def test_the_check_never_blocks_a_save(monkeypatch: pytest.MonkeyPatch) -> None:
    """The edit form saves with the service down: the check is never called on Save."""
    from tests.test_store_views import form_data as store_form_data

    calls: list[usps.Address] = []
    monkeypatch.setattr(usps, "check", lambda address, **_k: calls.append(address))
    store = a_store("Save without check", 4343)
    admin, _ = client_for(Role.ADMIN)
    edit = reverse("ledger:stores:edit", kwargs={"pk": store.pk})
    with override_settings(**CONFIGURED):
        response = admin.post(
            edit, store_form_data(store_number="4343", external_label="Saved anyway")
        )
    assert response.status_code == 302
    store.refresh_from_db()
    assert store.external_label == "Saved anyway"
    assert calls == []
