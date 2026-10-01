"""Lease screens (docs/qa/PLAN-007-leases-and-rent-run.md §6 rows 1 to 4 and 10).

``payments.get_provider`` is replaced by a :class:`~payments.fake.FakeProvider`, so the
views run as deployed and no request leaves.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client, override_settings

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    CollectionSchedule,
    CollectionStatus,
    Lease,
    LeaseRentHistory,
    Operator,
    PaymentChannel,
    ScheduleState,
    Store,
    StoreStatus,
)
from ledger.services import leases as service
from payments.errors import ProviderRejected
from payments.fake import FakeProvider
from payments.models import ExternalRef
from payments.provider import PaymentMethodRecord
from tests.auth_helpers import iter_routes, make_user, verified_client
from tests.test_lease_services import NOW, make_lease, make_operator, method
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
LIST = "/leases/"
NEW = "/leases/new/"
CUSTOMER = "cus-1"
BANK = "pm-bank-1"
CARD_ID = "pm-card-1"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


#: The day the lease pages treat as today in tests that depend on it.
TODAY = date(2026, 10, 1)


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    """Pin ``timezone.localdate()`` so current and upcoming rent do not drift with the clock."""
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


@pytest.fixture
def admin() -> Client:
    return client_for(Role.ADMIN, "admin-lease-views")[0]


def paths(lease: Lease) -> dict[str, str]:
    base = f"/leases/{lease.pk}/"
    return {
        "list": LIST,
        "create": NEW,
        "detail": base,
        "edit": f"{base}edit/",
        "enrol": f"{base}enrol/",
        "pause": f"{base}pause/",
        "resume": f"{base}resume/",
    }


POST_ROUTES = ("create", "edit", "enrol", "pause", "resume")


def form_data(**overrides: Any) -> dict[str, Any]:
    store = Store.objects.create(external_label="Store 0202", status=StoreStatus.ACTIVE)
    operator = make_operator(email="views@example.com", customer=None)
    data: dict[str, Any] = {
        "creation_key": str(uuid.uuid4()),
        "store": str(store.pk),
        "operator": str(operator.pk),
        "debit_day": "15",
        "second_debit_day": "",
        "payment_channel": PaymentChannel.PROVIDER_ACH,
        "started_on": "2026-01-01",
        "collection_status": CollectionStatus.ACTIVE,
        "rent": "1500.00",
    }
    return {**data, **overrides}


def edit_data(lease: Lease, **overrides: Any) -> dict[str, Any]:
    rent = service.open_rent(lease)
    data = {
        "store": str(lease.store_id),
        "operator": str(lease.operator_id) if lease.operator_id else "",
        "debit_day": str(lease.debit_day or ""),
        "second_debit_day": str(lease.second_debit_day or ""),
        "payment_channel": lease.payment_channel,
        "started_on": lease.started_on.isoformat() if lease.started_on else "",
        "collection_status": lease.collection_status,
        "rent": str(rent.amount) if rent else "",
        "rent_effective_from": "",
    }
    return {**data, **overrides}


def add_methods(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, method())
    fake.add_payment_method(CUSTOMER, method(CARD_ID, kind="card"))


# --- Row 10: roles, CSRF, registry ---------------------------------------------------------------


def test_every_lease_route_is_classified_with_the_planned_roles() -> None:
    names = {name for name, _, _ in iter_routes() if name and name.startswith("ledger:leases:")}
    assert names == {f"ledger:leases:{short}" for short in ("list", "detail", *POST_ROUTES)}
    assert {"ledger:leases:list", "ledger:leases:detail"} <= registry.READ_ONLY_ROUTE_NAMES
    for short in POST_ROUTES:
        rule = registry.rule_for(f"ledger:leases:{short}")
        assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_viewer_and_billing_read_and_change_nothing(fake: FakeProvider, role: Role) -> None:
    client, _ = client_for(role)
    add_methods(fake)
    lease = make_lease()
    route = paths(lease)
    for name in ("list", "detail"):
        assert client.get(route[name]).status_code == 200, name
    for name in ("create", "edit", "enrol"):
        assert client.get(route[name]).status_code == 403, name
    data = {**form_data(), "payment_method_id": BANK}
    for name in POST_ROUTES:
        response = client.post(route[name], data)
        assert response.status_code == 403, name
    assert Lease.objects.count() == 1
    assert CollectionSchedule.objects.count() == 0
    assert fake.calls == []
    page = client.get(route["detail"]).content.decode()
    for control in ("Edit</a>", "Enable autopay", "Pause autopay", "Resume autopay"):
        assert control not in page
    assert "New lease" not in client.get(LIST).content.decode()


def test_anonymous_gets_401_and_no_role_gets_403(fake: FakeProvider) -> None:
    lease = make_lease()
    route = paths(lease)
    for name in ("list", "detail", "create", "edit", "enrol"):
        assert Client().get(route[name]).status_code == 401, name
    for name in POST_ROUTES:
        assert Client().post(route[name]).status_code == 401, name
    nobody, _ = client_for(None)
    for name in ("list", "detail"):
        assert nobody.get(route[name]).status_code == 403, name


def test_every_post_route_enforces_csrf(fake: FakeProvider) -> None:
    user, device = make_user("csrf-admin-leases", Role.ADMIN)
    assert device is not None
    client = verified_client(user, device, Client(enforce_csrf_checks=True))
    add_methods(fake)
    lease = make_lease()
    data = {**form_data(), "payment_method_id": BANK}
    for name in POST_ROUTES:
        response = client.post(paths(lease)[name], data)
        assert response.status_code == 403, name
    assert Lease.objects.count() == 1 and fake.calls == []


def test_a_malformed_or_unknown_lease_id_is_404(admin: Client) -> None:
    for path in (f"/leases/{uuid.uuid4()}/", "/leases/not-a-uuid/", "/leases/1/edit/"):
        assert admin.get(path).status_code == 404, path


# --- Row 1 through the screens -----------------------------------------------------------------


def test_admin_creates_a_lease_and_a_replay_saves_nothing(admin: Client) -> None:
    data = form_data()
    first = admin.post(NEW, data)
    second = admin.post(NEW, {**data, "rent": "1.00"})
    lease = Lease.objects.get()
    assert first.status_code == second.status_code == 302
    assert first["Location"] == second["Location"] == f"/leases/{lease.pk}/"
    [row] = LeaseRentHistory.objects.filter(lease=lease)
    assert row.amount == D("1500.00")
    page = admin.get(f"/leases/{lease.pk}/").content.decode()
    assert "$1500.00 from 2026-01-01" in page


@pytest.mark.parametrize(
    ("overrides", "field", "text"),
    [
        ({"operator": ""}, "operator", "An active lease needs an operator."),
        ({"debit_day": ""}, "debit_day", "An active lease needs a debit day."),
        ({"payment_channel": "unknown"}, "payment_channel", "An active lease needs a known"),
        ({"second_debit_day": "15"}, "second_debit_day", "must differ from the first"),
        ({"rent": "0"}, "rent", "greater than or equal to 0.01"),
        ({"rent": "-5"}, "rent", "greater than or equal to 0.01"),
        ({"rent": "10.001"}, "rent", "no more than 2 decimal places"),
        ({"debit_day": "32"}, "debit_day", "less than or equal to 31"),
    ],
)
def test_an_invalid_lease_is_held_and_shown_once_after_a_redirect(
    admin: Client, overrides: dict[str, str], field: str, text: str
) -> None:
    response = admin.post(NEW, form_data(**overrides))
    assert (response.status_code, response["Location"]) == (302, NEW)
    assert Lease.objects.count() == 0
    page = admin.get(NEW).content.decode()
    assert text in page
    assert text not in admin.get(NEW).content.decode()


def test_a_rent_edit_writes_history_and_a_too_early_date_is_refused(
    admin: Client, today: date
) -> None:
    lease = make_lease()
    route = paths(lease)
    refused = admin.post(
        route["edit"], edit_data(lease, rent="1650.00", rent_effective_from="2026-01-02")
    )
    assert (refused.status_code, refused["Location"]) == (302, route["edit"])
    assert "at least two days after the current rent started (2026-01-01)" in (
        admin.get(route["edit"]).content.decode()
    )
    assert LeaseRentHistory.objects.filter(lease=lease).count() == 1

    saved = admin.post(
        route["edit"], edit_data(lease, rent="1650.00", rent_effective_from="2026-11-01")
    )
    assert (saved.status_code, saved["Location"]) == (302, route["detail"])
    page = admin.get(route["detail"]).content.decode()
    # Future-dated: today's rent stays current and the change is shown as upcoming.
    assert "$1500.00 from 2026-01-01" in page
    assert "Rent changes to $1650.00 on 2026-11-01" in page
    assert "2026-10-31" in page  # the closed row


# --- Rows 2 and 3 through the enrolment screen ---------------------------------------------------


def test_the_enrolment_page_lists_bank_methods_and_shows_cards_as_not_allowed(
    admin: Client, fake: FakeProvider
) -> None:
    fake.add_payment_method(
        CUSTOMER,
        PaymentMethodRecord(
            external_id=BANK,
            raw_status="",
            fetched_at=NOW,
            method="bank",
            last_four="6789",
            bank_type="checking",
            nickname="Operating",
            is_default=True,
        ),
    )
    fake.add_payment_method(CUSTOMER, method(CARD_ID, kind="card"))
    lease = make_lease(D("1000.01"), debit_day=5, second_debit_day=20)
    page = admin.get(paths(lease)["enrol"]).content.decode()
    assert f'name="payment_method_id" value="{BANK}"' in page
    assert f'value="{CARD_ID}"' not in page
    assert "Card, not allowed" in page
    assert "6789" in page and "checking" in page and "Operating" in page
    assert "$500.01" in page and "$500.00" in page
    assert "UTC" in page and "Enable autopay" in page
    assert [name for name, _ in fake.calls] == ["list_payment_methods"]


def test_the_enrolment_page_with_a_blocker_makes_no_provider_call(
    admin: Client, fake: FakeProvider
) -> None:
    lease = make_lease(payment_channel=PaymentChannel.WIRE)
    page = admin.get(paths(lease)["enrol"]).content.decode()
    assert service.NOT_PROVIDER_CHANNEL in page
    assert 'id="enable-autopay"' not in page
    assert fake.calls == []


def test_the_enrolment_page_says_no_bank_method_when_only_cards_exist(
    admin: Client, fake: FakeProvider
) -> None:
    fake.add_payment_method(CUSTOMER, method(CARD_ID, kind="card"))
    page = admin.get(paths(make_lease())["enrol"]).content.decode()
    assert service.NO_BANK_METHOD in page
    assert 'id="enable-autopay"' not in page


def test_the_enrolment_page_shows_a_list_failure_by_field_names_only(
    admin: Client, fake: FakeProvider
) -> None:
    fake.fail_next(
        "list_payment_methods", ProviderRejected(422, "CANARY", error_fields=("customer_id",))
    )
    page = admin.get(paths(make_lease())["enrol"]).content.decode()
    assert service.PROVIDER_REJECTED in page and "customer_id" in page
    assert "CANARY" not in page


@pytest.mark.parametrize(
    ("threshold", "shown"), [("1500.00", False), ("1499.99", True)], ids=["at", "above"]
)
def test_the_large_debit_warning(
    admin: Client, fake: FakeProvider, threshold: str, shown: bool
) -> None:
    add_methods(fake)
    lease = make_lease(D("1500.00"))
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D(threshold)):
        page = admin.get(paths(lease)["enrol"]).content.decode()
    assert ('id="large-debit-warning"' in page) is shown
    assert ("placeholder" in page) is shown


def test_enable_through_the_screen_then_a_second_click_creates_nothing(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    lease = make_lease(debit_day=5, second_debit_day=20)
    route = paths(lease)
    first = admin.post(route["enrol"], {"payment_method_id": BANK})
    assert (first.status_code, first["Location"]) == (302, route["detail"])
    second = admin.post(route["enrol"], {"payment_method_id": BANK})
    assert (second.status_code, second["Location"]) == (302, route["detail"])
    assert [name for name, _ in fake.calls].count("create_scheduled_invoice") == 2
    assert CollectionSchedule.objects.filter(state=ScheduleState.ACTIVE).count() == 2
    page = admin.get(route["detail"]).content.decode()
    assert service.ALREADY_ENABLED in page
    assert "Active" in page and "2026-10-05" in page and "2026-10-20" in page
    for ref in ExternalRef.objects.filter(external_kind="schedule"):
        assert ref.external_id not in page  # masked: first four and last four only


def test_a_card_id_posted_directly_is_refused_with_a_redirect(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    lease = make_lease()
    route = paths(lease)
    response = admin.post(route["enrol"], {"payment_method_id": CARD_ID})
    assert (response.status_code, response["Location"]) == (302, route["enrol"])
    assert service.CARD_REFUSED in admin.get(route["enrol"]).content.decode()
    assert "create_scheduled_invoice" not in [name for name, _ in fake.calls]
    assert CollectionSchedule.objects.count() == 0


def test_a_malformed_method_id_is_held_with_its_error(admin: Client, fake: FakeProvider) -> None:
    add_methods(fake)
    lease = make_lease()
    route = paths(lease)
    response = admin.post(route["enrol"], {"payment_method_id": "../x"})
    assert (response.status_code, response["Location"]) == (302, route["enrol"])
    assert fake.calls == []
    assert "Use letters, digits" in admin.get(route["enrol"]).content.decode()


def test_a_provider_rejection_on_enrol_shows_the_field_names(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    lease = make_lease()
    fake.fail_next(
        "create_scheduled_invoice",
        ProviderRejected(422, "CANARY", error_fields=("payment_method_id", "rule")),
    )
    route = paths(lease)
    response = admin.post(route["enrol"], {"payment_method_id": BANK})
    assert (response.status_code, response["Location"]) == (302, route["enrol"])
    page = admin.get(route["enrol"]).content.decode()
    assert "Fields the provider rejected: payment_method_id, rule." in page
    assert "CANARY" not in page


# --- Row 4 through the screens -------------------------------------------------------------------


def test_pause_and_resume_buttons_redirect_and_change_state(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    lease = make_lease()
    route = paths(lease)
    admin.post(route["enrol"], {"payment_method_id": BANK})
    assert 'id="pause-autopay"' in admin.get(route["detail"]).content.decode()

    paused = admin.post(route["pause"])
    assert (paused.status_code, paused["Location"]) == (302, route["detail"])
    page = admin.get(route["detail"]).content.decode()
    assert "Autopay paused." in page and 'id="resume-autopay"' in page
    assert 'id="pause-autopay"' not in page

    resumed = admin.post(route["resume"])
    assert (resumed.status_code, resumed["Location"]) == (302, route["detail"])
    assert "Autopay resumed." in admin.get(route["detail"]).content.decode()
    again = admin.post(route["resume"])
    assert (again.status_code, again["Location"]) == (302, route["detail"])


def test_a_rent_change_shows_the_review_message_on_the_lease(
    admin: Client, fake: FakeProvider, today: date
) -> None:
    add_methods(fake)
    lease = make_lease()
    route = paths(lease)
    admin.post(route["enrol"], {"payment_method_id": BANK})
    admin.post(route["edit"], edit_data(lease, rent="1650.00", rent_effective_from="2026-10-01"))
    page = admin.get(route["detail"]).content.decode()
    assert service.SCHEDULE_DIFFERS in page
    assert "Rent changes on" not in page


def test_a_future_rent_change_shows_the_dated_review_message_on_the_lease(
    admin: Client, fake: FakeProvider, today: date
) -> None:
    add_methods(fake)
    lease = make_lease()
    route = paths(lease)
    admin.post(route["enrol"], {"payment_method_id": BANK})
    admin.post(route["edit"], edit_data(lease, rent="1650.00", rent_effective_from="2026-11-01"))
    page = admin.get(route["detail"]).content.decode()
    assert service.RENT_CHANGES.format(date="2026-11-01") in page
    assert service.SCHEDULE_DIFFERS not in page
    assert "Rent changes to $1650.00 on 2026-11-01" in page


def test_the_detail_page_lists_blockers_and_information(admin: Client, fake: FakeProvider) -> None:
    operator = make_operator(email=None, customer=None)
    lease = make_lease(operator=operator)
    page = admin.get(paths(lease)["detail"]).content.decode()
    assert service.NO_PROVIDER_CUSTOMER in page
    assert service.NO_BANK_METHOD in page
    assert service.NO_EMAIL in page
    assert fake.calls == []


def test_the_enrolment_page_names_the_receipts_email_or_says_there_is_none(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    with_email = make_lease(operator=make_operator(email="ops+rent@example.com"))
    page = admin.get(paths(with_email)["enrol"]).content.decode()
    assert '<li id="receipts-email">Receipts go to ops+rent@example.com</li>' in page
    assert service.NO_EMAIL not in page

    fake.add_payment_method("cus-2", method())
    without = make_lease(operator=make_operator(email=None, customer="cus-2"))
    page = admin.get(paths(without)["enrol"]).content.decode()
    assert "Receipts go to" not in page
    assert service.NO_EMAIL in page


def test_the_receipts_email_is_escaped_on_the_enrolment_page(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    lease = make_lease(operator=make_operator(email='"<b>x</b>"@example.com'))
    page = admin.get(paths(lease)["enrol"]).content.decode()
    assert "<b>x</b>" not in page
    assert "Receipts go to &quot;&lt;b&gt;x&lt;/b&gt;&quot;@example.com" in page


def test_no_bank_or_routing_data_reaches_any_lease_page(admin: Client, fake: FakeProvider) -> None:
    fake.add_payment_method(
        CUSTOMER,
        PaymentMethodRecord(
            external_id=BANK, raw_status="", fetched_at=NOW, method="bank", last_four="6789"
        ),
    )
    lease = make_lease()
    pages = "".join(
        admin.get(paths(lease)[name]).content.decode()
        for name in ("list", "detail", "edit", "enrol")
    )
    for forbidden in ("routing", "account_number", "000123456789", "011000015"):
        assert forbidden not in pages


def test_the_navigation_links_the_lease_list(admin: Client) -> None:
    assert 'href="/leases/"' in admin.get(LIST).content.decode()


def test_an_operator_is_not_offered_once_archived(admin: Client) -> None:
    archived = Operator.objects.create(display_name="Gone Operator", archived_at=NOW)
    assert "Gone Operator" not in admin.get(NEW).content.decode()
    lease = make_lease(operator=archived, collection_status=CollectionStatus.PROSPECTIVE)
    assert "Gone Operator" in admin.get(paths(lease)["edit"]).content.decode()
