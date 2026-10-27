"""Split into installments to the spec (docs/qa/PLAN-012f-c-staging-review-fixes.md D13; §6 C).

The live preview's JSON route (``ledger:plans:split_preview``: every parameter validated, the
refusals, 403, 401, a foreign store, nothing written), the page in the Add payment family's
frame (the header, the Plan card's stepper, date echo, segmented cadence and collection line;
the server-rendered table with scripts off, the rounding cent on the last row; the Summary's
figures and its one primary), the phone layout's rules, the help moved to the Payments topic,
and the confirm POST unchanged. Nothing reaches a network.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    InstallmentCollection,
    InstallmentPlan,
    OneOffCharge,
    RentCharge,
)
from ledger.services import autopay_plans, installments
from ledger.services import stores as store_service
from ledger.views.installments import NSF_LINE, SPLIT_LINE, parent_key
from payments.fake import FakeProvider
from payments.models import Job
from tests.test_installments import a_one_off, a_rent, plan_for
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
FIRST = date(2026, 10, 20)
BACKEND = Path(__file__).resolve().parents[1]
APP_CSS = BACKEND / "finance" / "static" / "finance" / "app.css"
SPLIT_JS = BACKEND / "ledger" / "static" / "ledger" / "split.js"
PREVIEW = "ledger:plans:split_preview"


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def billing() -> Client:
    return client_for(Role.BILLING)[0]


def preview_url(parent: RentCharge | OneOffCharge, **params: str) -> str:
    query = {"parent": parent_key(parent), "count": "3", "first": FIRST.isoformat()}
    query |= {"cadence": "monthly", **params}
    return reverse(PREVIEW) + "?" + "&".join(f"{key}={value}" for key, value in query.items())


def answer(client: Client, url: str) -> tuple[int, dict[str, Any]]:
    response = client.get(url)
    assert response["Content-Type"] == "application/json"
    return response.status_code, json.loads(response.content)


def page_url(parent: RentCharge | OneOffCharge) -> str:
    if isinstance(parent, RentCharge):
        return reverse("ledger:charges:split", kwargs={"pk": parent.pk})
    return reverse("ledger:one_off_charges:split", kwargs={"pk": parent.pk})


def page(client: Client, parent: RentCharge | OneOffCharge, **query: str) -> str:
    response = client.get(page_url(parent), query)
    assert response.status_code == 200
    return response.content.decode()


def written() -> tuple[int, int, int]:
    return (InstallmentPlan.objects.count(), AuditLog.objects.count(), Job.objects.count())


def section(body: str, start: str, end: str) -> str:
    at = body.index(start)
    return body[at : body.index(end, at)]


# --- The preview route (D13) ----------------------------------------------------------------


def test_the_preview_answers_the_lines_the_share_the_last_date_and_the_rounding_cent(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    before = written()
    status, data = answer(billing, preview_url(parent))
    assert status == 200
    assert data == {
        "ok": True,
        "lines": [
            {"number": 1, "due": "2026-10-20", "amount": "333.33"},
            {"number": 2, "due": "2026-11-20", "amount": "333.33"},
            {"number": 3, "due": "2026-12-20", "amount": "333.34"},
        ],
        "share": "333.33",
        "last_due": "2026-12-20",
        "cent_on_last": True,
    }
    assert written() == before


def test_an_even_split_carries_no_rounding_cent_and_weekly_dates_step_by_seven_days(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake), price="900.00")
    status, data = answer(billing, preview_url(parent, cadence="weekly"))
    assert status == 200 and data["cent_on_last"] is False
    assert [line["due"] for line in data["lines"]] == ["2026-10-20", "2026-10-27", "2026-11-03"]
    assert {line["amount"] for line in data["lines"]} == {"300.00"}


def test_a_rent_charge_previews_through_its_own_key(fake: FakeProvider, billing: Client) -> None:
    parent = a_rent(an_operator(fake))
    assert parent_key(parent) == f"rent:{parent.pk}"
    status, data = answer(billing, preview_url(parent, count="2"))
    assert status == 200 and [line["amount"] for line in data["lines"]] == ["500.00", "500.00"]


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"count": "1"}, installments.COUNT_RANGE),
        ({"count": "13"}, installments.COUNT_RANGE),
        ({"count": "x"}, installments.COUNT_RANGE),
        ({"count": ""}, installments.COUNT_RANGE),
        ({"count": "-3"}, installments.COUNT_RANGE),
        ({"count": "9" * 400}, installments.COUNT_RANGE),
        ({"first": ""}, installments.FIRST_REQUIRED),
        ({"first": "10/20/2026"}, installments.FIRST_REQUIRED),
        ({"first": "2026-02-30"}, installments.FIRST_REQUIRED),
        # Arabic-Indic digits are not an ISO day.
        ({"first": "٢٠٢٦-١٠-٢٠"}, installments.FIRST_REQUIRED),
        ({"first": "2026-10-05"}, installments.FIRST_IN_PAST),
        # PLAN-012f-c D13 ruling: every two weeks is no longer offered on Split.
        ({"cadence": "biweekly"}, autopay_plans.CHOOSE_CADENCE),
        ({"cadence": "MONTHLY"}, autopay_plans.CHOOSE_CADENCE),
        ({"cadence": ""}, autopay_plans.CHOOSE_CADENCE),
        ({"cadence": "<script>"}, autopay_plans.CHOOSE_CADENCE),
    ],
)
def test_every_parameter_is_validated_and_a_refusal_says_why(
    fake: FakeProvider, billing: Client, params: dict[str, str], message: str
) -> None:
    parent = a_one_off(an_operator(fake))
    before = written()
    status, data = answer(billing, preview_url(parent, **params))
    assert (status, data) == (400, {"ok": False, "message": message})
    assert written() == before


def test_today_is_a_valid_first_date(fake: FakeProvider, billing: Client) -> None:
    parent = a_one_off(an_operator(fake))
    status, data = answer(billing, preview_url(parent, first="2026-10-06"))
    assert status == 200 and data["lines"][0]["due"] == "2026-10-06"


def test_the_first_refused_parameter_is_named_in_the_forms_order(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    _, data = answer(billing, preview_url(parent, count="1", first="", cadence="biweekly"))
    assert data["message"] == installments.COUNT_RANGE
    _, data = answer(billing, preview_url(parent, first="", cadence="biweekly"))
    assert data["message"] == installments.FIRST_REQUIRED


def test_a_share_below_one_cent_is_refused(fake: FakeProvider, billing: Client) -> None:
    parent = a_one_off(an_operator(fake), price="0.05")
    status, data = answer(billing, preview_url(parent, count="6"))
    assert (status, data["message"]) == (400, installments.CENT_TOO_SMALL)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "rent",
        "rent:",
        "one_off:not-a-key",
        "invoice:00000000-0000-0000-0000-000000000000",
        "rent:00000000-0000-0000-0000-00000000000g",
        " rent:00000000-0000-0000-0000-000000000000",
    ],
)
def test_a_malformed_parent_is_refused(fake: FakeProvider, billing: Client, value: str) -> None:
    url = reverse(PREVIEW) + f"?parent={value}&count=3&first=2026-10-20&cadence=monthly"
    status, data = answer(billing, url)
    assert (status, data) == (400, {"ok": False, "message": "Choose the charge to split."})


def test_an_unknown_parent_is_a_404(fake: FakeProvider, billing: Client) -> None:
    for kind in ("rent", "one_off"):
        url = (
            reverse(PREVIEW)
            + f"?parent={kind}:00000000-0000-0000-0000-000000000000&count=3&first=2026-10-20"
        )
        assert billing.get(url).status_code == 404


def test_a_charge_that_cannot_be_split_is_refused_with_its_reason(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    plan_for(parent)
    before = written()
    status, data = answer(billing, preview_url(parent))
    assert (status, data) == (400, {"ok": False, "message": installments.HAS_PLAN})
    assert written() == before


def test_another_stores_charge_is_a_404_and_its_own_store_answers(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_rent(an_operator(fake))
    other = make_lease(operator=an_operator(fake, customer="cus-other", bank="pm-other"))
    assert other.store_id != parent.lease.store_id
    own = billing.get(preview_url(parent, store=str(parent.lease.store_id)))
    assert own.status_code == 200
    assert billing.get(preview_url(parent, store=str(other.store_id))).status_code == 404
    assert billing.get(preview_url(parent, store="not-a-key")).status_code == 404
    # A one-off charge with no lease has no store: naming one is a 404.
    loose = a_one_off(an_operator(fake, customer="cus-loose", bank="pm-loose"))
    assert billing.get(preview_url(loose, store=str(other.store_id))).status_code == 404


def test_a_viewer_is_refused_an_anonymous_request_is_401_and_nothing_is_written(
    fake: FakeProvider,
) -> None:
    parent = a_one_off(an_operator(fake))
    before = written()
    viewer = client_for(Role.VIEWER)[0]
    assert viewer.get(preview_url(parent)).status_code == 403
    assert Client().get(preview_url(parent)).status_code == 401
    # Read-only in the registry: a POST is refused for every role, even an admin.
    admin = client_for(Role.ADMIN)[0]
    assert admin.post(preview_url(parent)).status_code == 403
    assert client_for(Role.BILLING)[0].head(preview_url(parent)).status_code == 200
    assert written() == before


def test_an_admin_previews_too(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    status, data = answer(client_for(Role.ADMIN)[0], preview_url(parent))
    assert status == 200 and data["ok"] is True


# --- The page: header and Plan card (D13) ---------------------------------------------------


def test_the_header_names_the_store_the_charge_its_status_and_what_is_left(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_rent(an_operator(fake))
    store = parent.lease.store
    body = page(billing, parent)
    crumb = section(body, '<nav class="breadcrumb"', "</nav>")
    assert re.findall(r">([^<>]+)</a>", crumb) == [
        "Stores",
        store_service.crumb(store),
        "Rent Oct 2026",
    ]
    assert '<span aria-current="page">Split into installments</span>' in crumb
    title = section(body, '<h1 class="store-title">', "</h1>")
    assert title.startswith('<h1 class="store-title">Split Rent Oct 2026 into installments ')
    assert "Due · $1,000.00" in title and 'class="badge badge-' in title
    operator = parent.lease.operator
    assert operator is not None
    line = (
        f"Rent Oct 2026 · {store_service.title(store)} · {operator.display_name} · "
        "$1,000.00 left to pay"
    )
    assert f'<p class="store-line" id="split-line">{line}</p>' in body


def test_a_one_off_charge_without_a_lease_has_the_operators_breadcrumb(
    fake: FakeProvider, billing: Client
) -> None:
    operator = an_operator(fake)
    parent = a_one_off(operator)
    crumb = section(page(billing, parent), '<nav class="breadcrumb"', "</nav>")
    assert re.findall(r">([^<>]+)</a>", crumb) == [
        "Operators",
        operator.display_name,
        parent.label,
    ]


def test_the_charge_label_is_escaped_everywhere(fake: FakeProvider, billing: Client) -> None:
    parent = a_one_off(an_operator(fake))
    OneOffCharge.objects.filter(pk=parent.pk).update(description='<img src=x onerror="a()">')
    body = page(billing, OneOffCharge.objects.get(pk=parent.pk))
    assert "<img src=x" not in body and "&lt;img src=x" in body


def test_the_plan_card_has_the_stepper_the_date_echo_the_cadence_and_the_collection(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    body = page(billing, parent, count="4", first_due=FIRST.isoformat(), cadence="weekly")
    card = section(body, 'id="plan-card"', "</section>")
    assert '<h2 id="plan-card-heading">Plan</h2>' in card
    stepper = section(card, '<div class="stepper" id="count-stepper">', "</div>")
    # The buttons are the script's: hidden in the markup, the number input bounded 2 to 12.
    assert re.search(
        r'<button class="btn stepper-step" type="button" id="count-less"[^>]* hidden>', stepper
    )
    assert re.search(
        r'<button class="btn stepper-step" type="button" id="count-more"[^>]* hidden>', stepper
    )
    assert '<input type="number" name="count" id="id_count" value="4" min="2" max="12"' in stepper
    assert (
        stepper.index('id="count-less"')
        < stepper.index('id="id_count"')
        < stepper.index('id="count-more"')
    )
    assert (
        'First installment date <span class="muted" id="first-due-text">Oct 20, 2026</span>' in card
    )
    radios = re.findall(r'<input type="radio" name="cadence" value="(\w+)"[^>]*>', card)
    # The autopay control's order (PLAN-012f-c D13 ruling): Monthly, Weekly, Yearly.
    assert radios == ["monthly", "weekly", "yearly"]
    labels = re.findall(r"required(?: checked)?> (\w+)</label>", card)
    assert labels == ["Monthly", "Weekly", "Yearly"]
    assert re.search(r'value="weekly" id="id_cadence_1" required checked>', card)
    assert '<div class="radio-segmented" id="id_cadence" role="radiogroup"' in card
    manual = InstallmentCollection.MANUAL.label
    assert f'<span class="toast-text">Collected by: {manual}</span>' in card
    assert '<p class="toast info is-inline" id="split-collection">' in card
    assert (
        '<p class="actions" id="split-preview-row"><button class="btn" type="submit" '
        'id="split-preview">Preview</button></p>'
    ) in card


def test_the_collection_line_names_the_parents_collection(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake), collection=CollectionMethod.EMAIL)
    card = section(page(billing, parent), 'id="split-collection"', "</p>")
    assert f"Collected by: {InstallmentCollection.EMAIL.label}" in card


def test_the_stepper_bounds_hold_on_the_page_without_the_script(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    for count in ("1", "13"):
        body = page(billing, parent, count=count, first_due=FIRST.isoformat(), cadence="monthly")
        assert installments.COUNT_RANGE in body
        assert 'class="split-line' not in body
        assert re.search(r'id="split-confirm" disabled>', body)
    assert not InstallmentPlan.objects.exists()


def test_the_script_clamps_the_stepper_to_the_inputs_bounds() -> None:
    script = SPLIT_JS.read_text(encoding="utf-8")
    assert 'parseInt(count.getAttribute("min"), 10)' in script
    assert 'parseInt(count.getAttribute("max"), 10)' in script
    assert "Math.min(max, Math.max(min, value + delta))" in script
    # The table is built with DOM calls, never from markup in a string.
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


# --- The table: server-rendered, the rounding cent (D13) ------------------------------------


def lines_of(body: str) -> list[tuple[str, str]]:
    return re.findall(
        r'<input type="date" name="due_\d+" value="([^"]*)"[^>]*>.*?'
        r'<input type="text" inputmode="decimal" name="amount_\d+" value="([^"]*)"',
        section(body, '<tbody id="split-rows">', "</tbody>"),
        re.S,
    )


def test_with_scripts_off_the_server_renders_the_table_from_the_query_string(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    body = page(billing, parent, count="4", first_due=FIRST.isoformat(), cadence="weekly")
    assert lines_of(body) == [
        ("2026-10-20", "250.00"),
        ("2026-10-27", "250.00"),
        ("2026-11-03", "250.00"),
        ("2026-11-10", "250.00"),
    ]
    numbers = re.findall(r'<td data-label="Installment">(\d+ of \d+)</td>', body)
    assert numbers == ["1 of 4", "2 of 4", "3 of 4", "4 of 4"]
    assert 'id="split-rounding"' not in body and "is-rounding" not in body
    assert '<input type="hidden" name="count" value="4" id="confirm-count">' in body
    assert (
        '<input type="hidden" name="first_due" value="2026-10-20" id="confirm-first-due">' in body
    )
    assert '<input type="hidden" name="cadence" value="weekly" id="confirm-cadence">' in body
    assert not InstallmentPlan.objects.exists()


def test_the_page_opens_with_the_first_values_previewed(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    body = page(billing, parent)
    assert lines_of(body) == [
        ("2026-10-06", "333.33"),
        ("2026-11-06", "333.33"),
        ("2026-12-06", "333.34"),
    ]
    assert re.search(r'value="monthly" id="id_cadence_0" required checked>', body)
    assert 'id="first-due-text">Oct 6, 2026</span>' in body


def test_the_rounding_cent_is_marked_on_the_last_row_only(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake), price="100.00")
    body = page(billing, parent, count="7", first_due=FIRST.isoformat(), cadence="weekly")
    rows = re.findall(r'<tr class="(split-line[^"]*)">', body)
    assert rows == ["split-line"] * 6 + ["split-line is-rounding"]
    assert body.count('id="split-rounding"') == 1
    last = section(body, '<tr class="split-line is-rounding">', "</tr>")
    assert 'value="14.32"' in last and "Carries the rounding cent" in last


def test_edited_values_carried_back_stay_and_the_last_date_follows_them(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    edited = {"due_1": "2026-10-20", "amount_1": "500.00", "due_2": "2026-11-20"}
    edited |= {"amount_2": "250.00", "due_3": "2027-01-04", "amount_3": "200.00"}
    body = page(
        billing, parent, count="3", first_due=FIRST.isoformat(), cadence="monthly", **edited
    )
    assert lines_of(body) == [
        ("2026-10-20", "500.00"),
        ("2026-11-20", "250.00"),
        ("2027-01-04", "200.00"),
    ]
    assert '<strong id="split-last-value">Jan 4, 2027</strong>' in body


def test_no_iso_date_is_shown_as_text(fake: FakeProvider, billing: Client) -> None:
    parent = a_one_off(an_operator(fake))
    body = page(billing, parent, count="3", first_due=FIRST.isoformat(), cadence="monthly")
    text = re.sub(r"<[^>]*>", " ", body.split("<main", 1)[1])
    assert re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text) == []


# --- The Summary (D13) ----------------------------------------------------------------------


def test_the_summary_shows_the_balance_the_share_the_last_date_and_one_primary(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    body = page(billing, parent, count="3", first_due=FIRST.isoformat(), cadence="monthly")
    aside = section(body, '<aside class="form-summary" aria-label="Summary">', "</aside>")
    assert (
        '<span class="kpi-label">Balance to split</span><span class="kpi-value">$1,000.00</span>'
        in aside
    )
    assert (
        '<span class="kpi-label">Installments</span><span class="kpi-value">3 × $333.33</span>'
        in aside
    )
    assert '<strong id="split-last-value">Dec 20, 2026</strong>' in aside
    assert f'<p class="help" id="split-nsf">{NSF_LINE.replace(chr(39), "&#x27;")}</p>' in aside
    actions = re.findall(r'<div class="form-actions">(.*?)</div>', aside, re.S)
    assert actions == [
        '<button class="btn btn-primary" type="submit" form="confirm-form" id="split-confirm">'
        'Create <span class="split-count">3 </span>installments</button>'
    ]
    assert (
        f'<a class="btn summary-discard" href="{installments.parent_url(parent)}" '
        'id="split-cancel">Cancel</a>'
    ) in aside
    assert body.count("btn-primary") == 1
    # The confirm form the primary submits holds the plan's values and the lines.
    form = section(body, 'id="confirm-form"', "</form>")
    assert 'name="csrfmiddlewaretoken"' in form and form.count('name="due_') == 3


def test_without_lines_the_summary_shows_dashes_and_the_primary_is_disabled(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    body = page(billing, parent, count="3", first_due="2026-10-01", cadence="monthly")
    assert installments.FIRST_IN_PAST.replace("'", "&#x27;") in body
    aside = section(body, '<aside class="form-summary"', "</aside>")
    assert '<span class="kpi-label">Installments</span><span class="kpi-value">—</span>' in aside
    assert '<strong id="split-last-value">—</strong>' in aside
    assert (
        'id="split-confirm" disabled>Create <span class="split-count"></span>installments' in aside
    )


def test_a_default_that_cannot_be_split_says_why_without_a_field_error(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake), price="0.02")
    body = page(billing, parent)
    assert (
        '<p class="help" id="split-preview-message" aria-live="polite">'
        f"{installments.CENT_TOO_SMALL}</p>"
    ) in body
    assert 'class="split-line' not in body and "has-error" not in body


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("300.00"))
def test_the_large_debit_tick_is_shown_for_large_autopay_installments_only(
    fake: FakeProvider, billing: Client
) -> None:
    autopay = a_one_off(an_operator(fake), collection=CollectionMethod.AUTOPAY_DUE)
    body = page(billing, autopay, count="3", first_due=FIRST.isoformat(), cadence="monthly")
    assert '<p id="split-large" data-threshold="300.00"><label>' in body
    small = page(billing, autopay, count="4", first_due=FIRST.isoformat(), cadence="monthly")
    assert '<p id="split-large" data-threshold="300.00" hidden><label>' in small
    manual = a_one_off(an_operator(fake, customer="cus-manual", bank="pm-manual"))
    assert 'id="split-large"' not in page(billing, manual)


# --- The page's help, its script, a refusal, the phone (D13) ----------------------------------


def test_the_page_keeps_one_short_line_and_the_help_topic_holds_the_rest(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    body = page(billing, parent)
    topic = reverse("finance:help_topic", kwargs={"slug": "payments"})
    assert (
        f'<p class="muted" id="page-help">{SPLIT_LINE} Learn more: '
        f'<a href="{topic}#help-payments-installments">Payments</a></p>'
    ) in body
    # The three sentences that were SPLIT_HELP are on the help topic only.
    assert "Each installment is a charge of its own" not in body
    help_page = billing.get(topic).content.decode().replace("&#x27;", "'")
    assert 'id="help-payments-installments">Splitting a charge into installments</h3>' in help_page
    assert (
        "splits what is still owed on the charge into equal installments, the last one "
        "carrying the rounding cent. Each installment is a charge of its own, collected the "
        "way this charge is; while the plan runs, this charge is paid through them."
    ) in help_page


def test_the_page_loads_its_script_with_the_preview_address_and_the_charge(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_rent(an_operator(fake))
    body = page(billing, parent)
    assert '<script src="/static/ledger/split.js" defer></script>' in body
    assert f'data-preview-url="{reverse(PREVIEW)}" data-parent="rent:{parent.pk}"' in body


def test_a_charge_that_cannot_be_split_shows_why_and_no_form(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    plan_for(parent)
    body = page(billing, parent)
    assert f'<p id="split-refusal"><strong>{installments.HAS_PLAN}</strong></p>' in body
    assert 'id="split-form"' not in body and 'id="confirm-form"' not in body
    assert "ledger/split.js" not in body


def test_a_viewer_cannot_open_the_page(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    assert client_for(Role.VIEWER)[0].get(page_url(parent)).status_code == 403


def test_on_a_phone_the_page_is_one_column_with_the_summary_above_the_fixed_primary(
    fake: FakeProvider, billing: Client
) -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 768px) {", 1)[1].split("\n}\n", 1)[0]
    assert (
        ".form-layout { grid-template-columns: minmax(0, 1fr); gap: 0; padding-bottom: 72px; }"
        in phone
    )
    assert ".form-summary .form-actions { position: fixed;" in phone
    assert (
        ".stepper { display: flex;" in css
        and ".stepper-step[hidden], #split-preview-row[hidden] { display: none; }" in css
    )
    # Markup order: the Plan card, the table, then the Summary with its button last.
    body = page(billing, a_one_off(an_operator(fake)))
    order = [
        body.index(marker)
        for marker in (
            'id="plan-card"',
            'id="preview-card"',
            'id="split-summary"',
            'id="split-confirm"',
            'id="split-cancel"',
        )
    ]
    assert order == sorted(order)


# --- The confirm POST is unchanged ----------------------------------------------------------


def test_the_pages_values_posted_as_they_are_create_the_plan(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    body = page(billing, parent, count="3", first_due=FIRST.isoformat(), cadence="monthly")
    data = {"count": "3", "first_due": "2026-10-20", "cadence": "monthly"}
    for number, (due, amount) in enumerate(lines_of(body), start=1):
        data |= {f"due_{number}": due, f"amount_{number}": amount}
    response = billing.post(page_url(parent), data)
    plan = InstallmentPlan.objects.get()
    assert response.status_code == 302 and response["Location"] == f"/plans/{plan.pk}/"
    assert plan.count == 3


# --- Yearly, and every two weeks refused (PLAN-012f-c D13 ruling) ------------------------------


def test_yearly_falls_on_the_same_day_each_year_and_29_february_on_28_february(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    status, data = answer(billing, preview_url(parent, cadence="yearly", first="2027-03-15"))
    assert status == 200
    assert [line["due"] for line in data["lines"]] == ["2027-03-15", "2028-03-15", "2029-03-15"]
    status, data = answer(billing, preview_url(parent, cadence="yearly", first="2028-02-29"))
    assert status == 200
    assert [line["due"] for line in data["lines"]] == ["2028-02-29", "2029-02-28", "2030-02-28"]
    assert data["last_due"] == "2030-02-28"
    body = page(billing, parent, count="3", first_due="2028-02-29", cadence="yearly")
    assert [due for due, _ in lines_of(body)] == ["2028-02-29", "2029-02-28", "2030-02-28"]
    assert '<strong id="split-last-value">Feb 28, 2030</strong>' in body
    assert re.search(r'value="yearly" id="id_cadence_2" required checked>', body)


def test_a_yearly_split_posted_from_the_page_makes_a_yearly_plan(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    body = page(billing, parent, count="2", first_due="2027-03-15", cadence="yearly")
    data = {"count": "2", "first_due": "2027-03-15", "cadence": "yearly"}
    for number, (due, amount) in enumerate(lines_of(body), start=1):
        data |= {f"due_{number}": due, f"amount_{number}": amount}
    assert billing.post(page_url(parent), data).status_code == 302
    plan = InstallmentPlan.objects.get()
    assert plan.cadence == "yearly" and plan.count == 2


def test_every_two_weeks_is_refused_on_the_page_and_on_its_confirm(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_one_off(an_operator(fake))
    body = page(billing, parent, count="3", first_due=FIRST.isoformat(), cadence="biweekly")
    assert autopay_plans.CHOOSE_CADENCE in body and 'class="split-line' not in body
    assert 'value="biweekly"' not in body
    posted = {"count": "3", "first_due": "2026-10-20", "cadence": "biweekly"}
    posted |= {"due_1": "2026-10-20", "amount_1": "333.33", "due_2": "2026-11-03"}
    posted |= {"amount_2": "333.33", "due_3": "2026-11-17", "amount_3": "333.34"}
    response = billing.post(page_url(parent), posted)
    assert response.status_code == 302 and response["Location"] == page_url(parent)
    assert not InstallmentPlan.objects.exists()


# --- The store context (Fable's finding on Batch C) -------------------------------------------


def test_the_page_and_its_confirm_answer_404_for_another_store_as_add_payment_does(
    fake: FakeProvider, billing: Client
) -> None:
    parent = a_rent(an_operator(fake))
    other = make_lease(operator=an_operator(fake, customer="cus-other", bank="pm-other"))
    url = page_url(parent)
    own = billing.get(url, {"store": str(parent.lease.store_id)})
    assert own.status_code == 200
    assert billing.get(url, {"store": str(other.store_id)}).status_code == 404
    assert (
        billing.get(url, {"store": str(parent.lease.store_id), "lease": str(other.pk)}).status_code
        == 404
    )
    posted = {"count": "2", "first_due": "2026-10-20", "cadence": "monthly"}
    posted |= {"due_1": "2026-10-20", "amount_1": "500.00", "due_2": "2026-11-20"}
    posted |= {"amount_2": "500.00"}
    assert billing.post(f"{url}?store={other.store_id}", posted).status_code == 404
    assert not InstallmentPlan.objects.exists()
    # A one-off charge with no lease: naming any store is a 404, on the page and its confirm.
    loose = a_one_off(an_operator(fake, customer="cus-loose", bank="pm-loose"))
    loose_url = page_url(loose)
    assert billing.get(loose_url, {"store": str(other.store_id)}).status_code == 404
    assert billing.post(f"{loose_url}?store={other.store_id}", posted).status_code == 404
    assert not InstallmentPlan.objects.exists()
    # The confirm without a store still works.
    assert billing.post(url, posted).status_code == 302
    assert InstallmentPlan.objects.count() == 1
