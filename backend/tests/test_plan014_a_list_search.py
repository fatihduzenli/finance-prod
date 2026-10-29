"""PLAN-014 Batch A rework (Opus): the search field (``q``) on Payments, Leases and Unpaid
balances — what each page's words match, every word having to match, the spreadsheet keeping
the search, ``q`` never a chip and kept by Clear — and the links of the filters component never
carrying a value the page's parser dropped."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.test import Client

from accounts.roles import Role
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def section(html: str, element_id: str, end: str) -> str:
    start = html.index(f'id="{element_id}"')
    return html[start : html.index(end, start)]


def export(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]


def seeded() -> None:
    """Two stores, each with its operator, a September charge (behind on TODAY) and a payment."""
    north = a_lease(a_store("North Plaza", number=2477), an_operator("Harbor Foods"))
    south = a_lease(a_store("South Mall", number=1991), an_operator("Lime Kitchen"))
    for lease, reference in ((north, "CHK-7781"), (south, "WIRE-0042")):
        charge = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
        payment = a_payment(charge, "100.00", date(2026, 9, 5))
        payment.reference = reference
        payment.save(update_fields=["reference"])


# --- Payments -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "kept"),
    [
        ("24", "North Plaza"),  # the store number as a prefix
        ("#1991", "South Mall"),
        ("plaza", "North Plaza"),  # the store's name, any case
        ("lime", "South Mall"),  # the operator's display name
        ("chk-77", "North Plaza"),  # the payment's reference
        ("harbor 2477", "North Plaza"),  # every word matches something
    ],
)
def test_the_payments_search_matches_store_operator_and_reference(query: str, kept: str) -> None:
    seeded()
    rows = section(
        page(client_for(Role.BILLING)[0], f"/payments/?q={query.replace('#', '%23')}"),
        "payments",
        "</table>",
    )
    other = "South Mall" if kept == "North Plaza" else "North Plaza"
    assert kept in rows and other not in rows, query


@pytest.mark.parametrize("query", ["harbor 1991", "99", "nothing"])
def test_the_payments_search_needs_every_word_to_match(query: str) -> None:
    seeded()
    rows = section(
        page(client_for(Role.BILLING)[0], f"/payments/?q={query}"), "payments", "</table>"
    )
    assert "North Plaza" not in rows and "South Mall" not in rows and "No payments match" in rows


def test_the_payments_search_field_chip_clear_and_spreadsheet() -> None:
    seeded()
    client, _ = client_for(Role.BILLING)
    html = page(client, "/payments/?q=plaza&method=ach")
    form = section(html, "filters", "</form>")
    search = re.search(r'<input type="search" name="q" value="plaza" maxlength="100"', form)
    assert search is not None
    assert "Filters · 1" in form  # the method; the search never counts
    chips = section(html, "filters-chips", "</div>")
    assert chips.count('class="chip"') == 1 and ">Method: " in chips  # no chip for the search
    assert "q=plaza" in chips  # removing the method keeps the search
    clear = re.search(r'id="filters-clear" href="([^"]+)"', html)
    assert clear is not None and "q=plaza" in clear.group(1) and "method" not in clear.group(1)
    link = re.search(r'id="export-csv" href="([^"]+)"', html)
    assert link is not None and "q=plaza" in link.group(1)
    sheet = export(client, link.group(1).replace("&amp;", "&"))
    assert "North Plaza" in sheet and "South Mall" not in sheet


def test_a_long_or_hostile_payments_search_answers_200() -> None:
    client, _ = client_for(Role.VIEWER)
    assert client.get("/payments/", {"q": "a" * 500}).status_code == 200
    html = page(client, "/payments/?q=%3Cscript%3Ealert(1)%3C/script%3E%25_")
    assert "<script>alert" not in html


# --- Leases ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "kept"),
    [
        ("2477", "North Plaza"),
        ("mall", "South Mall"),
        ("harbor", "North Plaza"),
        ("19 lime", "South Mall"),
    ],
)
def test_the_leases_search_matches_store_and_operator(query: str, kept: str) -> None:
    seeded()
    rows = section(page(client_for(Role.BILLING)[0], f"/leases/?q={query}"), "leases", "</table>")
    other = "South Mall" if kept == "North Plaza" else "North Plaza"
    assert kept in rows and other not in rows, query


def test_the_leases_search_is_kept_by_clear_and_the_spreadsheet() -> None:
    seeded()
    client, _ = client_for(Role.BILLING)
    html = page(client, "/leases/?q=harbor")
    assert re.search(r'<input type="search" name="q" value="harbor" maxlength="100"', html)
    assert 'class="chip"' not in html
    clear = re.search(r'id="filters-clear" href="([^"]+)"', html)
    assert clear is not None and "q=harbor" in clear.group(1)
    link = re.search(r'id="export-csv" href="([^"]+)"', html)
    assert link is not None and "q=harbor" in link.group(1)
    sheet = export(client, link.group(1))
    assert "North Plaza" in sheet and "South Mall" not in sheet
    rows = section(page(client, "/leases/?q=harbor+mall"), "leases", "</table>")
    assert "North Plaza" not in rows and "South Mall" not in rows


# --- Unpaid balances ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "kept"),
    [
        ("harbor", "Harbor Foods"),  # the operator's name
        ("south", "Lime Kitchen"),  # one of its stores' names
        ("247", "Harbor Foods"),  # one of its stores' numbers, as a prefix
        ("lime 1991", "Lime Kitchen"),
    ],
)
def test_the_unpaid_balances_search_matches_operator_and_its_stores(query: str, kept: str) -> None:
    seeded()
    rows = section(page(client_for(Role.BILLING)[0], f"/arrears/?q={query}"), "arrears", "</table>")
    other = "Lime Kitchen" if kept == "Harbor Foods" else "Harbor Foods"
    assert kept in rows and other not in rows, query


def test_the_unpaid_balances_search_is_kept_by_clear_sort_and_spreadsheet() -> None:
    seeded()
    client, _ = client_for(Role.BILLING)
    html = page(client, "/arrears/?q=harbor&bucket=1")
    form = section(html, "filters", "</form>")
    assert re.search(r'<input type="search" name="q" value="harbor" maxlength="100"', form)
    assert "Filters · 1" in form
    clear = re.search(r'id="filters-clear" href="([^"]+)"', html)
    assert clear is not None and "q=harbor" in clear.group(1) and "bucket" not in clear.group(1)
    sort = section(html, "filters-sort", "</details>")
    assert sort.count("q=harbor") == 4
    link = re.search(r'id="export-csv" href="([^"]+)"', html)
    assert link is not None and "q=harbor" in link.group(1)
    sheet = export(client, link.group(1).replace("&amp;", "&"))
    assert "Harbor Foods" in sheet and "Lime Kitchen" not in sheet
    rows = section(page(client, "/arrears/?q=harbor+1991"), "arrears", "</table>")
    assert "Harbor Foods" not in rows and "Lime Kitchen" not in rows


# --- No link carries a value the parser dropped ------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/arrears/?state=%3Cscript%3E&bucket=zz&sort=evil&operator=00000000-0000-0000-0000-000000000000",
        "/payments/?status=%3Cscript%3E&method=evil&date_from=2026-13-01&store=nope",
        "/leases/?operator=%3Cscript%3E",
        "/stores/?state=ZZ&status=%3Cscript%3E&sort=evil",
    ],
)
def test_an_unknown_value_is_in_no_link_of_the_filters(path: str) -> None:
    html = page(client_for(Role.BILLING)[0], path)
    form = section(html, "filters", "</form>")
    links = " ".join(re.findall(r'href="([^"]*)"', form))
    for unknown in ("script", "evil", "zz", "2026-13", "nope", "00000000"):
        assert unknown not in links.lower(), (path, unknown)
    assert 'class="chip"' not in form
