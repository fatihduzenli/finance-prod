"""Lease screens (docs/qa/PLAN-007-leases-and-rent-run.md §6 rows 1 to 4 and 10).

``payments.get_provider`` is replaced by a :class:`~payments.fake.FakeProvider`, so the
views run as deployed and no request leaves.
"""

from __future__ import annotations

import html
import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from accounts import registry
from accounts.roles import Role
from finance.dates import day_text
from ledger.models import (
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    Lease,
    LeaseBilling,
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
from tests.job_helpers import worker_between_requests  # noqa: F401
from tests.test_lease_services import NOW, make_lease, make_operator, method
from tests.test_operator_views import client_for
from tests.text_helpers import has_token

# PLAN-010 D3: views queue provider work; the worker is run after every request here.
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("worker_between_requests")]

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


def read_methods(lease: Lease, fake: FakeProvider) -> Lease:
    """What the worker's ``lease_methods_refresh`` job does: the snapshot the
    enrolment page reads (PLAN-010 Batch A follow-up). The read is not counted."""
    assert service.refresh_payment_methods(lease.pk).ok
    fake.calls.clear()
    return lease


# --- Row 10: roles, CSRF, registry ---------------------------------------------------------------


def test_every_lease_route_is_classified_with_the_planned_roles() -> None:
    names = {name for name, _, _ in iter_routes() if name and name.startswith("ledger:leases:")}
    # PLAN-010b D5, D7, D12: adding one setup charge from the lease page (a redirect to
    # the invoice builder since PLAN-012e-2 D4); resolving a deposit needs admin, as the
    # lease routes do. Batch C: ending a lease needs admin. PLAN-012e D5 amendment: so
    # does stopping autopay.
    assert names == {
        f"ledger:leases:{short}"
        for short in (
            "list",
            "detail",
            "export",
            "add_setup_charge",
            "new_charge",
            "resolve_deposit",
            "end",
            "stop_autopay",
            "billing",
            "bank",
            "bank_attach",
            "switch_own",
            *POST_ROUTES,
        )
    }
    assert {
        "ledger:leases:list",
        "ledger:leases:detail",
        "ledger:leases:export",
    } <= registry.READ_ONLY_ROUTE_NAMES
    for short in POST_ROUTES:
        rule = registry.rule_for(f"ledger:leases:{short}")
        assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}
    # PLAN-012e-2 D4: Add setup charge moved into the invoice builder; its route is a
    # read-only redirect there.
    assert "ledger:leases:add_setup_charge" in registry.READ_ONLY_ROUTE_NAMES
    assert registry.rule_for("ledger:leases:add_setup_charge") is None
    # PLAN-010d D9: creating a one-off charge from the lease page needs billing.
    rule = registry.rule_for("ledger:leases:new_charge")
    assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}
    rule = registry.rule_for("ledger:leases:resolve_deposit")
    assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}
    rule = registry.rule_for("ledger:leases:end")
    assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}
    # PLAN-012e D5 amendment: stopping autopay needs admin.
    rule = registry.rule_for("ledger:leases:stop_autopay")
    assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}
    # PLAN-014 C3: Edit billing details (the details, the link, the retry) needs billing.
    rule = registry.rule_for("ledger:leases:billing")
    assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}
    # PLAN-014 C7: the bank-entry page reads; its attach needs billing.
    assert "ledger:leases:bank" in registry.READ_ONLY_ROUTE_NAMES
    rule = registry.rule_for("ledger:leases:bank_attach")
    assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}
    # PLAN-014 C9: the LEASE card's "Switch to own account" needs billing.
    rule = registry.rule_for("ledger:leases:switch_own")
    assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}


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
    for control in ("Edit</a>", "Set up autopay", "Pause autopay", "Resume autopay"):
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
    assert "$1,500.00 from Jan 1, 2026" in page


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
    assert "at least two days after the current rent started (Jan 1, 2026)" in (
        admin.get(route["edit"]).content.decode()
    )
    assert LeaseRentHistory.objects.filter(lease=lease).count() == 1

    saved = admin.post(
        route["edit"], edit_data(lease, rent="1650.00", rent_effective_from="2026-11-01")
    )
    assert (saved.status_code, saved["Location"]) == (302, route["detail"])
    page = admin.get(route["detail"]).content.decode()
    # Future-dated: today's rent stays current and the change is shown as upcoming.
    assert "$1,500.00 from Jan 1, 2026" in page
    assert "Rent changes to $1,650.00 on Nov 1, 2026" in page
    assert "Oct 31, 2026" in page  # the closed row


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
    lease = read_methods(make_lease(D("1000.01"), debit_day=5, second_debit_day=20), fake)
    page = admin.get(paths(lease)["enrol"]).content.decode()
    assert 'id="methods-as-of"' in page and "Bank methods as of Oct 1, 2026 · 11:00" in page
    assert f'name="payment_method_id" value="{BANK}"' in page
    assert f'value="{CARD_ID}"' not in page
    assert "Card, not allowed" in page
    assert has_token(page, "6789") and has_token(page, "checking") and "Operating" in page
    assert "$500.01" in page and "$500.00" in page
    assert not has_token(page, "UTC") and "Set up autopay" in page
    assert fake.calls == []


def test_the_enrolment_page_with_a_blocker_makes_no_provider_call(
    admin: Client, fake: FakeProvider
) -> None:
    lease = make_lease(payment_channel=PaymentChannel.WIRE)
    page = admin.get(paths(lease)["enrol"]).content.decode()
    assert str(service.NOT_PROVIDER_CHANNEL) in page
    assert 'id="enable-autopay"' not in page
    assert fake.calls == []


def test_the_enrolment_page_says_no_bank_method_when_only_cards_exist(
    admin: Client, fake: FakeProvider
) -> None:
    fake.add_payment_method(CUSTOMER, method(CARD_ID, kind="card"))
    page = admin.get(paths(read_methods(make_lease(), fake))["enrol"]).content.decode()
    assert service.NO_BANK_METHOD in page
    assert 'id="enable-autopay"' not in page


def test_the_enrolment_page_shows_a_list_failure_by_field_names_only(
    admin: Client, fake: FakeProvider
) -> None:
    fake.fail_next(
        "list_payment_methods", ProviderRejected(422, "CANARY", error_fields=("customer_id",))
    )
    route = paths(make_lease())["enrol"]
    # The worker runs the queued read after the POST (``worker_between_requests``).
    admin.post(route, {"refresh_methods": "1"})
    page = admin.get(route).content.decode()
    assert str(service.PROVIDER_REJECTED) in page and "customer_id" in page
    assert "ProviderRejected" not in page and 'id="job-state">Failed: ' in page
    assert "(Rejected)" in page
    assert not has_token(page, "CANARY")
    assert 'id="methods-not-read"' in page


@pytest.mark.parametrize(
    ("threshold", "shown"), [("1500.00", False), ("1499.99", True)], ids=["at", "above"]
)
def test_the_large_debit_warning(
    admin: Client, fake: FakeProvider, threshold: str, shown: bool
) -> None:
    add_methods(fake)
    lease = read_methods(make_lease(D("1500.00")), fake)
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D(threshold)):
        page = admin.get(paths(lease)["enrol"]).content.decode()
    assert ('id="large-debit-warning"' in page) is shown
    assert ('id="confirm-large-debit"' in page) is shown


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
    # The first debit of each day is the next such day after now, whatever day it is.
    now = timezone.now()
    for day in (5, 20):
        assert day_text(service.next_debit_at(day, now).date()) in page
    assert '<td data-role="meta" data-label="State">On</td>' in page
    for ref in ExternalRef.objects.filter(external_kind="schedule"):
        assert ref.external_id not in page  # masked: first four and last four only


def test_a_card_id_posted_directly_is_refused_with_a_redirect(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    lease = make_lease()
    route = paths(lease)
    response = admin.post(route["enrol"], {"payment_method_id": CARD_ID})
    # PLAN-010 D3: enrolment is a job; its refusal is shown on the lease page.
    assert (response.status_code, response["Location"]) == (302, route["detail"])
    assert service.CARD_REFUSED in admin.get(route["detail"]).content.decode()
    assert "create_scheduled_invoice" not in [name for name, _ in fake.calls]
    assert CollectionSchedule.objects.count() == 0


def test_a_malformed_method_id_is_held_with_its_error(admin: Client, fake: FakeProvider) -> None:
    add_methods(fake)
    lease = read_methods(make_lease(), fake)
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
    assert (response.status_code, response["Location"]) == (302, route["detail"])
    page = admin.get(route["detail"]).content.decode()
    assert "Fields Stax rejected: payment_method_id, rule." in page
    assert not has_token(page, "CANARY")


# --- PLAN-007c: large-debit confirmation and the confirmed provider limit ------------------------

CONFIRM_2100 = (
    "I confirm this debit of $2,100.00 exceeds the warning threshold of $2,000.00 "
    "and may be enabled."
)
LIMIT_5000 = "Exceeds the Stax debit limit of $5,000.00. Autopay cannot be enabled."


def _creates(fake: FakeProvider) -> int:
    return [name for name, _ in fake.calls].count("create_scheduled_invoice")


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=None)
def test_the_enrolment_page_asks_for_the_large_debit_confirmation(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    page = admin.get(paths(read_methods(make_lease(D("2100.00")), fake))["enrol"]).content.decode()
    assert 'id="large-debit-warning"' in page and "$2,000.00" in page
    assert 'type="checkbox" name="confirm_large_debit"' in page
    assert CONFIRM_2100 in page
    assert 'id="enable-autopay"' in page
    assert 'id="provider-limit"' not in page


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=None)
def test_the_enrolment_page_below_the_warning_has_no_checkbox(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    page = admin.get(paths(read_methods(make_lease(D("2000.00")), fake))["enrol"]).content.decode()
    assert 'name="confirm_large_debit"' not in page
    assert 'id="enable-autopay"' in page


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=None)
def test_a_large_debit_posted_without_the_checkbox_is_refused_with_the_sentence(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    lease = make_lease(D("2100.00"))
    route = paths(lease)
    response = admin.post(route["enrol"], {"payment_method_id": BANK})
    assert (response.status_code, response["Location"]) == (302, route["detail"])
    assert _creates(fake) == 0 and fake.calls == []
    assert CONFIRM_2100 in admin.get(route["detail"]).content.decode()
    assert CollectionSchedule.objects.count() == 0


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=None)
def test_a_large_debit_posted_with_the_checkbox_is_enabled_and_audited(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    lease = make_lease(D("2100.00"))
    route = paths(lease)
    response = admin.post(
        route["enrol"], {"payment_method_id": BANK, "confirm_large_debit": "true"}
    )
    assert (response.status_code, response["Location"]) == (302, route["detail"])
    assert _creates(fake) == 1
    entry = AuditLog.objects.get(action="lease.autopay_large_debit_confirmed")
    assert entry.actor is not None
    assert entry.after == {
        "amount": "2100.00",
        "threshold": "2000.00",
        "confirmed_by": entry.actor.pk,
    }


@pytest.mark.parametrize(
    ("posted", "confirmed"),
    [
        ({"confirm_large_debit": "on"}, True),
        ({"confirm_large_debit": "true"}, True),
        ({"confirm_large_debit": "1"}, False),
        ({"confirm_large_debit": "yes"}, False),
        ({"confirm_large_debit": "0"}, False),
        ({"confirm_large_debit": "off"}, False),
        ({"confirm_large_debit": "false"}, False),
        ({"confirm_large_debit": "no"}, False),
        ({"confirm_large_debit": "ON"}, False),
        ({"confirm_large_debit": "True"}, False),
        ({"confirm_large_debit": " on"}, False),
        ({"confirm_large_debit": ""}, False),
        ({}, False),
    ],
)
@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=None)
def test_only_the_exact_checkbox_value_confirms_a_large_debit(
    admin: Client, fake: FakeProvider, posted: dict[str, str], confirmed: bool
) -> None:
    add_methods(fake)
    lease = make_lease(D("2100.00"))
    route = paths(lease)
    response = admin.post(route["enrol"], {"payment_method_id": BANK, **posted})
    assert (response.status_code, response["Location"]) == (302, route["detail"])
    assert _creates(fake) == (1 if confirmed else 0)
    assert CollectionSchedule.objects.exists() is confirmed
    assert (
        AuditLog.objects.filter(action="lease.autopay_large_debit_confirmed").exists() is confirmed
    )


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=D("5000.00"))
def test_above_the_confirmed_limit_the_page_offers_no_way_and_a_post_is_refused(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    lease = make_lease(D("5000.01"))
    route = paths(lease)
    page = admin.get(route["enrol"]).content.decode()
    assert LIMIT_5000 in page
    assert 'name="confirm_large_debit"' not in page
    assert 'id="enable-autopay"' not in page
    response = admin.post(
        route["enrol"], {"payment_method_id": BANK, "confirm_large_debit": "true"}
    )
    assert (response.status_code, response["Location"]) == (302, route["detail"])
    assert _creates(fake) == 0
    assert LIMIT_5000 in admin.get(route["detail"]).content.decode()
    assert CollectionSchedule.objects.count() == 0


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=D("5000.00"))
def test_between_the_warning_and_the_limit_the_screen_confirmation_works(
    admin: Client, fake: FakeProvider
) -> None:
    add_methods(fake)
    lease = read_methods(make_lease(D("5000.00")), fake)
    route = paths(lease)
    page = admin.get(route["enrol"]).content.decode()
    assert 'name="confirm_large_debit"' in page and 'id="provider-limit"' not in page
    response = admin.post(
        route["enrol"], {"payment_method_id": BANK, "confirm_large_debit": "true"}
    )
    assert (response.status_code, response["Location"]) == (302, route["detail"])
    assert _creates(fake) == 1


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
    assert service.RENT_CHANGES.format(date="Nov 1, 2026") in page
    assert service.SCHEDULE_DIFFERS not in page
    assert "Rent changes to $1,650.00 on Nov 1, 2026" in page


def test_the_detail_page_lists_blockers_and_information(admin: Client, fake: FakeProvider) -> None:
    operator = make_operator(email=None, customer=None)
    lease = make_lease(operator=operator)
    page = admin.get(paths(lease)["detail"]).content.decode()
    assert str(service.NO_PROVIDER_CUSTOMER) in page
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
        assert not has_token(pages, forbidden), forbidden


def test_the_navigation_links_the_lease_list(admin: Client) -> None:
    assert 'href="/leases/"' in admin.get(LIST).content.decode()


def test_an_operator_is_not_offered_once_archived(admin: Client) -> None:
    archived = Operator.objects.create(display_name="Gone Operator", archived_at=NOW)
    assert "Gone Operator" not in admin.get(NEW).content.decode()
    lease = make_lease(operator=archived, collection_status=CollectionStatus.PROSPECTIVE)
    assert "Gone Operator" in admin.get(paths(lease)["edit"]).content.decode()


# --- PLAN-014 C4: the Billing details of New lease ------------------------------------------------


def tag_with(page: str, *parts: str) -> bool:
    """Whether one HTML tag of the page holds every one of ``parts``."""
    return any(all(part in tag for part in parts) for tag in re.findall(r"<[^>]+>", page))


def test_new_lease_shows_empty_billing_details_and_the_copy_data_of_each_operator(
    admin: Client,
) -> None:
    from ledger.views.leases import BANK_NOT_SET_UP

    operator = make_operator(email="copy@example.com", customer=None)
    operator.phone, operator.city = "4155550132", "Austin"
    operator.save()
    page = admin.get(NEW).content.decode()
    assert 'id="billing-card"' in page and "<h2>Billing details</h2>" in page
    assert tag_with(page, 'id="copy-from-operator"', 'data-from="id_operator"')
    assert tag_with(page, f'value="{operator.pk}"', 'data-email="copy@example.com"')
    assert 'data-phone="4155550132"' in page and 'data-city="Austin"' in page
    # Nothing is copied by the server: the billing fields start empty.
    assert tag_with(page, 'name="email"', 'id="id_email"', 'data-shown-when="id_operator"')
    assert 'value="copy@example.com"' not in page
    assert tag_with(page, 'name="account"', 'value="later"', "checked")
    assert tag_with(page, 'value="enter"', "disabled")
    assert str(BANK_NOT_SET_UP) in page
    assert "leases_billing.js" in page


def test_new_lease_saves_billing_details_as_posted_empty_stays_empty(
    admin: Client, fake: FakeProvider
) -> None:
    data = form_data()
    assert admin.post(NEW, data).status_code == 302
    lease = Lease.objects.get()
    billing = lease.billing
    # The operator's e-mail is views@example.com; nothing was copied.
    assert (billing.email, billing.phone, billing.company_name) == ("", "", "")
    lease.refresh_from_db()
    assert lease.sync_status == "awaiting_link"
    assert fake.calls == []


def test_new_lease_later_with_an_email_creates_the_customer_and_no_method(
    admin: Client, fake: FakeProvider
) -> None:
    data = form_data(email="Billing@Example.com", company_name="Two LLC", account="later")
    response = admin.post(NEW, data)
    lease = Lease.objects.get()
    assert response["Location"] == f"/leases/{lease.pk}/"
    assert (lease.billing.email, lease.billing.company_name) == ("billing@example.com", "Two LLC")
    refs = dict(
        ExternalRef.objects.filter(entity_id=lease.pk).values_list("external_kind", "external_id")
    )
    assert set(refs) == {"customer"}
    lease.refresh_from_db()
    assert lease.sync_status == "linked"


def test_new_lease_refuses_enter_while_hosted_fields_are_not_set_up(admin: Client) -> None:
    from ledger.views.leases import BANK_NOT_SET_UP

    response = admin.post(NEW, form_data(account="enter", email="a@example.com"))
    assert (response.status_code, response["Location"]) == (302, NEW)
    assert Lease.objects.count() == 0
    assert str(BANK_NOT_SET_UP) in admin.get(NEW).content.decode()


@override_settings(STAX_PROVIDER="fake")
def test_new_lease_with_enter_continues_to_the_bank_entry_page(
    admin: Client, fake: FakeProvider
) -> None:
    response = admin.post(NEW, form_data(account="enter", email="a@example.com"))
    lease = Lease.objects.get()
    assert response["Location"] == f"/leases/{lease.pk}/bank/"


def _holder(fake: FakeProvider) -> Lease:
    from tests.test_lease_services import account_holder

    return account_holder(fake)


def test_new_lease_offers_and_saves_the_same_account_of_the_operators_lease(
    admin: Client, fake: FakeProvider
) -> None:
    holder = _holder(fake)
    page = admin.get(NEW).content.decode()
    assert tag_with(
        page,
        f'value="same:{holder.pk}"',
        f'data-shown-when="id_operator={holder.operator_id}"',
    )
    assert f"Use the same account as #{holder.store.external_label} (····6789)" in page
    data = form_data(operator=str(holder.operator_id), account=f"same:{holder.pk}")
    assert admin.post(NEW, data).status_code == 302
    lease = Lease.objects.exclude(pk=holder.pk).get()
    assert lease.billing.shares_account_of == holder
    assert service.customer_ref(lease) == CUSTOMER


def test_new_lease_refuses_the_account_of_another_operators_lease(
    admin: Client, fake: FakeProvider
) -> None:
    holder = _holder(fake)
    response = admin.post(NEW, form_data(account=f"same:{holder.pk}"))
    assert (response.status_code, response["Location"]) == (302, NEW)
    assert list(Lease.objects.all()) == [holder]
    assert html.escape(service.ACCOUNT_NOT_SHAREABLE) in admin.get(NEW).content.decode()


def _store_page(client: Client, lease: Lease) -> str:
    response = client.get(f"/stores/{lease.store_id}/")
    assert response.status_code == 200
    return response.content.decode()


@override_settings(STAX_PROVIDER="fake")
def test_the_lease_card_offers_the_bank_entry_and_names_a_shared_account(
    admin: Client, fake: FakeProvider
) -> None:
    holder = _holder(fake)
    LeaseBilling.objects.filter(lease=holder).update(bank_last4="6789")
    page = _store_page(admin, holder)
    assert tag_with(page, 'id="lease-bank-entry"', f'href="/leases/{holder.pk}/bank/"')
    assert ">Replace</a>" in page
    sharer = make_lease(
        operator=holder.operator,
        store=Store.objects.create(external_label="Store 0404", status=StoreStatus.ACTIVE),
    )
    LeaseBilling.objects.filter(lease=sharer).update(shares_account_of=holder)
    page = _store_page(admin, sharer)
    assert f"Shared with #{holder.store.external_label}" in page
    assert 'id="lease-bank-entry"' not in page


def test_the_lease_card_offers_no_bank_entry_without_hosted_fields(
    admin: Client, fake: FakeProvider
) -> None:
    lease = make_lease()
    page = _store_page(admin, lease)
    assert 'id="edit-billing"' in page and 'id="lease-bank-entry"' not in page


@override_settings(STAX_PROVIDER="fake")
def test_the_lease_card_says_add_a_bank_account_without_one(
    admin: Client, fake: FakeProvider
) -> None:
    lease = make_lease()
    page = _store_page(admin, lease)
    assert tag_with(page, 'id="lease-bank-entry"') and ">Add a bank account</a>" in page


# --- PLAN-014 C9: Switch to own account --------------------------------------------------------


def _sharer(fake: FakeProvider) -> tuple[Lease, Lease]:
    from tests.test_lease_services import OWN, own_method, sharer_on_autopay

    holder, sharer = sharer_on_autopay(fake)
    fake.add_payment_method(OWN, own_method())
    return holder, sharer


def _switch_path(lease: Lease) -> str:
    return f"/leases/{lease.pk}/bank/switch-own/"


def _switch_jobs() -> list[Any]:
    from payments.models import Job, JobKind

    return list(Job.objects.filter(kind=JobKind.LEASE_ACCOUNT_SWITCH))


def test_the_lease_card_offers_the_switch_to_a_sharer_with_its_own_customer(
    fake: FakeProvider,
) -> None:
    holder, sharer = _sharer(fake)
    billing, _ = client_for(Role.BILLING, "billing-switch")
    page = _store_page(billing, sharer)
    assert tag_with(page, 'id="switch-own-form"', f'action="{_switch_path(sharer)}"')
    assert tag_with(page, 'id="switch-own-account"', 'type="submit"')
    assert ">Switch to own account</button>" in page
    # Not for the account's holder, a sharer without its own customer, or a viewer.
    assert 'id="switch-own-account"' not in _store_page(billing, holder)
    viewer, _ = client_for(Role.VIEWER, "viewer-switch")
    assert 'id="switch-own-account"' not in _store_page(viewer, sharer)
    ExternalRef.objects.filter(entity_id=sharer.pk, external_kind="customer").delete()
    assert 'id="switch-own-account"' not in _store_page(billing, sharer)


def test_the_switch_queues_one_job_that_moves_the_lease_to_its_own_account(
    fake: FakeProvider,
) -> None:
    _, sharer = _sharer(fake)
    billing, user = client_for(Role.BILLING, "billing-switch")
    response = billing.post(_switch_path(sharer))
    assert (response.status_code, response["Location"]) == (302, f"/leases/{sharer.pk}/")
    [job] = _switch_jobs()
    assert (job.entity_id, job.requested_by, job.status) == (sharer.pk, user, "done")
    assert set(job.params) == {"move_key"} and uuid.UUID(job.params["move_key"])
    assert LeaseBilling.objects.get(lease=sharer).shares_account_of_id is None
    page = billing.get(f"/leases/{sharer.pk}/").content.decode()
    assert html.escape(service.SWITCH_QUEUED) in page


@pytest.mark.parametrize("why", ["not_sharing", "no_customer", "archived", "waiting"])
def test_the_switch_is_refused_up_front_with_a_sentence_and_nothing_queued(
    fake: FakeProvider, why: str
) -> None:
    from payments.models import Job, JobKind

    holder, sharer = _sharer(fake)
    target = sharer
    expected = {
        "not_sharing": service.SWITCH_NOT_SHARING,
        "no_customer": service.SWITCH_NO_CUSTOMER,
        "archived": service.SWITCH_ARCHIVED,
        "waiting": service.SWITCH_WAITING,
    }[why]
    if why == "not_sharing":
        target = holder
    elif why == "no_customer":
        ExternalRef.objects.filter(entity_id=sharer.pk, external_kind="customer").delete()
    elif why == "archived":
        Operator.objects.filter(leases=sharer).update(archived_at=timezone.now())
    else:
        # A switch the worker is running (a queued one would run after this request).
        Job.objects.create(
            kind=JobKind.LEASE_ACCOUNT_SWITCH,
            entity_type="lease",
            entity_id=sharer.pk,
            params={"move_key": str(uuid.uuid4())},
            status="running",
        )
    jobs_before = Job.objects.count()
    billing, _ = client_for(Role.BILLING, "billing-switch")
    response = billing.post(_switch_path(target), follow=True)
    assert response.redirect_chain == [(f"/leases/{target.pk}/", 302)]
    assert html.escape(service.SWITCH_MESSAGES[expected]) in response.content.decode()
    assert Job.objects.count() == jobs_before
    assert LeaseBilling.objects.get(lease=sharer).shares_account_of_id == holder.pk
    assert not [name for name, _ in fake.calls if name == "list_payment_methods"]


@pytest.mark.parametrize("role", [Role.VIEWER, None])
def test_the_switch_below_billing_is_forbidden_and_queues_nothing(
    fake: FakeProvider, role: Role | None
) -> None:
    _, sharer = _sharer(fake)
    client, _ = client_for(role, f"switch-{role}")
    assert client.post(_switch_path(sharer)).status_code == 403
    assert client.get(_switch_path(sharer)).status_code in (403, 405)
    assert _switch_jobs() == []
    assert LeaseBilling.objects.get(lease=sharer).shares_account_of_id is not None
