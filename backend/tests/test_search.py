"""The Stores list and Grid filter and the top-bar search (docs/qa/PLAN-012d-store-workspace.md
D16, D17; §6 F, the implementer's cases; the adversarial ones are the QA owner's file).

The filter matches the store number as a prefix, the name, the city and the current
operator's name, every word; an empty query is no filter; it is kept by the list's paging
and export links and by the grid's From/To form and export, which it filters. The search
jumps on one store, on one operator, lists otherwise and says "No match"; an archived
operator is listed, never jumped to. Fixed query counts. Nothing reaches a network.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from pathlib import Path

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts import registry
from accounts.roles import Role
from ledger.models import Lease, Operator, Store
from ledger.services import grid, search
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 16)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


@pytest.fixture
def viewer() -> Client:
    return client_for(Role.VIEWER)[0]


def a_city_store(label: str, number: int | None, city: str) -> Store:
    store = a_store(label, number)
    store.city = city
    store.save(update_fields=["city"])
    return store


def world() -> dict[str, Lease]:
    """Three stores: 2477 Acme in Austin, 2478 Beacon in Dallas, 3100 Acme in Dallas."""
    acme = an_operator("Acme Stores")
    return {
        "2477": a_lease(a_city_store("North Plaza", 2477, "Austin"), acme),
        "2478": a_lease(a_city_store("South Plaza", 2478, "Dallas"), an_operator("Beacon Ltd")),
        "3100": a_lease(a_city_store("Lake Mall", 3100, "Dallas"), acme),
    }


def numbers(stores: object) -> set[int | None]:
    return {store.store_number for store in stores}  # type: ignore[attr-defined]


def matched(query: str) -> set[int | None]:
    return numbers(search.filter_stores(Store.objects.all(), query))


def text(response: object) -> str:
    content = getattr(response, "streaming_content", None)
    if content is not None:
        return b"".join(content).decode()
    return str(response.content.decode())  # type: ignore[attr-defined]


def csv_numbers(body: str, column: str) -> set[str]:
    return {row[column] for row in csv.DictReader(io.StringIO(body.lstrip("﻿")))}


# --- The filter (D16) ---------------------------------------------------------------------------


def test_the_filter_matches_number_prefix_name_city_and_operator() -> None:
    world()
    assert matched("2477") == {2477}
    assert matched("247") == {2477, 2478}
    assert matched("#3100") == {3100}
    assert matched("77") == set()  # a number matches as a prefix only
    assert matched("plaza") == {2477, 2478}
    assert matched("DALLAS") == {2478, 3100}
    assert matched("acme") == {2477, 3100}
    assert matched("beacon") == {2478}


def test_every_word_must_match_one_field() -> None:
    world()
    assert matched("acme dallas") == {3100}
    assert matched("Dallas  plaza") == {2478}
    assert matched("acme beacon") == set()
    assert matched("acme acme ACME") == {2477, 3100}


def test_an_empty_or_blank_query_is_no_filter_and_no_match_is_empty() -> None:
    world()
    assert matched("") == matched("   \t ") == {2477, 2478, 3100}
    assert matched("zzz") == set()


def test_an_ended_leases_operator_does_not_match_the_store() -> None:
    leases = world()
    old = leases["2478"]
    old.ended_on = date(2026, 9, 30)
    old.save(update_fields=["ended_on"])
    a_lease(old.store, an_operator("Comet Co"), started=date(2026, 10, 1))
    assert matched("beacon") == set()
    assert matched("comet") == {2478}


def test_the_query_is_cut_at_100_characters_and_cleaned() -> None:
    assert search.clean_query("a" * 500) == "a" * 100
    assert search.clean_query("  ac\x00me \x7f ") == "acme"
    assert search.clean_query(None) == ""
    assert search.words("Acme  acme\tDallas") == ["Acme", "Dallas"]


# --- The Stores list (D16) ------------------------------------------------------------------------


def test_the_stores_list_filters_and_says_so_with_a_clear_link(viewer: Client) -> None:
    world()
    page = text(viewer.get("/stores/", {"q": "acme dallas"}))
    assert "Lake Mall" in page and "North Plaza" not in page and "South Plaza" not in page
    assert "Filtered by 'acme dallas' · " in page
    assert '<a href="/stores/" id="stores-clear">Clear</a>' in page
    assert 'name="q" value="acme dallas"' in page
    assert 'href="/stores/export.csv?q=acme+dallas"' in page
    plain = text(viewer.get("/stores/", {"q": "  "}))
    assert "Filtered by" not in plain and plain.count("Plaza") >= 2
    assert "No stores match." in text(viewer.get("/stores/", {"q": "zzz"}))


def test_the_list_paging_and_export_keep_the_filter(viewer: Client) -> None:
    for n in range(30):
        a_city_store(f"Depot {n:02d}", 5000 + n, "Waco")
    a_city_store("Elsewhere", 6000, "Tyler")
    page = text(viewer.get("/stores/", {"q": "waco"}))
    assert '<span class="pager-count">1–25 of 30 · filtered from 31</span>' in page
    assert 'href="?q=waco&amp;page=2"' in page
    second = text(viewer.get("/stores/", {"q": "waco", "page": "2"}))
    assert 'href="?q=waco&amp;page=1"' in second and "Elsewhere" not in second
    export = text(viewer.get("/stores/export.csv", {"q": "waco"}))
    found = csv_numbers(export, "Store number")
    assert len(found) == 30 and "6000" not in found


# --- The Grid (D16) -----------------------------------------------------------------------------


def test_the_grid_filters_its_rows_and_keeps_the_filter_in_the_form_and_export(
    viewer: Client,
) -> None:
    world()
    params = {"from": "2026-01", "to": "2026-12", "q": "dallas"}
    page = text(viewer.get("/grid/", params))
    rows = re.findall(r'class="grid-row" data-store="([^"]+)"', page)
    assert len(rows) == 2 and "North Plaza" not in page
    assert "2 stores." in page
    filters = page[
        page.index('id="grid-filters"') : page.index("</form>", page.index('id="grid-filters"'))
    ]
    assert '<input type="hidden" name="q" value="dallas">' in filters
    box = page[
        page.index('id="grid-search"') : page.index("</form>", page.index('id="grid-search"'))
    ]
    assert 'name="q" id="grid-q" value="dallas"' in box
    assert '<input type="hidden" name="from" value="2026-01">' in box
    assert '<input type="hidden" name="to" value="2026-12">' in box
    assert 'href="/grid/export.csv?from=2026-01&amp;to=2026-12&amp;q=dallas"' in page
    assert "Filtered by 'dallas' · " in page
    assert 'href="/grid/?from=2026-01&amp;to=2026-12" id="grid-clear">Clear</a>' in page
    export = text(viewer.get("/grid/export.csv", params))
    assert csv_numbers(export, "Store number") == {"2478", "3100"}
    plain = text(viewer.get("/grid/", {"from": "2026-01", "to": "2026-12"}))
    assert "3 stores." in plain and "Filtered by" not in plain
    assert 'type="hidden" name="q"' not in plain


def test_the_grid_query_reads_q_and_writes_it_back() -> None:
    query = grid.parse_query({"q": "  Acme  "}, TODAY)
    assert query.q == "Acme" and query.as_query()["q"] == "Acme"
    assert "q" not in grid.parse_query({"q": " "}, TODAY).as_query()
    assert len(grid.parse_query({"q": "x" * 300}, TODAY).q) == 100


def _grid_queries(**params: str) -> int:
    with CaptureQueriesContext(connection) as captured:
        grid.build(grid.parse_query({"from": "2026-01", "to": "2026-12", **params}, TODAY), TODAY)
    return len(captured.captured_queries)


def test_the_grid_with_a_filter_keeps_its_query_count() -> None:
    world()
    small = _grid_queries(q="dallas plaza")
    assert small == _grid_queries()
    for n in range(20):
        a_lease(a_city_store(f"Depot {n}", 7000 + n, "Dallas"), an_operator(f"Op {n}"))
    assert _grid_queries(q="dallas plaza") == small == _grid_queries()


def _page_queries(client: Client, path: str, params: dict[str, str]) -> int:
    with CaptureQueriesContext(connection) as captured:
        response = client.get(path, params)
    assert response.status_code in (200, 302)
    return len(captured.captured_queries)


def test_the_stores_list_and_the_search_keep_a_fixed_query_count(viewer: Client) -> None:
    world()
    viewer.get("/stores/")  # warm the session
    small_list = _page_queries(viewer, "/stores/", {"q": "dallas"})
    small_search = _page_queries(viewer, "/search/", {"q": "dallas"})
    for n in range(20):
        a_lease(a_city_store(f"Depot {n}", 7000 + n, "Dallas"), an_operator(f"Dallas Op {n}"))
    assert _page_queries(viewer, "/stores/", {"q": "dallas"}) == small_list
    assert _page_queries(viewer, "/search/", {"q": "dallas"}) == small_search


# --- The top-bar search (D17) -------------------------------------------------------------------


def test_one_store_and_no_operator_jumps_to_the_store(viewer: Client) -> None:
    leases = world()
    response = viewer.get("/search/", {"q": "north"})
    assert response.status_code == 302
    assert response["Location"] == f"/stores/{leases['2477'].store_id}/"
    assert (
        viewer.get("/search/", {"q": "2477"})["Location"] == f"/stores/{leases['2477'].store_id}/"
    )


def test_one_operator_and_no_store_jumps_to_the_operator(viewer: Client) -> None:
    world()
    lone = an_operator("Zenith Holdings")
    response = viewer.get("/search/", {"q": "zenith"})
    assert response.status_code == 302 and response["Location"] == f"/operators/{lone.pk}/"


def test_several_or_mixed_matches_are_listed(viewer: Client) -> None:
    leases = world()
    Operator.objects.filter(display_name="Acme Stores").update(email="billing@example.com")
    # "acme": two stores and one operator.
    page = text(viewer.get("/search/", {"q": "acme"}))
    assert 'name="q" value="acme"' in page
    stores = page[
        page.index('id="search-stores"') : page.index("</table>", page.index('id="search-stores"'))
    ]
    assert "North Plaza" in stores and "Lake Mall" in stores and "South Plaza" not in stores
    assert "2477" in stores and "Austin" in stores and "Acme Stores" in stores
    assert f'href="/stores/{leases["2477"].store_id}/"' in stores
    operators = page[page.index('id="search-operators"') :]
    assert "Acme Stores" in operators and "b***@example.com" in operators
    assert "billing@example.com" not in page
    # Several stores, no operator: listed.
    page = text(viewer.get("/search/", {"q": "plaza"}))
    assert "North Plaza" in page and "South Plaza" in page and "No operator matches." in page


def test_no_match_and_an_empty_query(viewer: Client) -> None:
    world()
    page = text(viewer.get("/search/", {"q": "<b>zzz</b>"}))
    # PLAN-012f-b E-4: the line of the empty state, escaped whole (its quotes too).
    assert "No match for &#x27;&lt;b&gt;zzz&lt;/b&gt;&#x27;." in page
    assert 'value="&lt;b&gt;zzz&lt;/b&gt;"' in page and "<b>zzz" not in page
    empty = viewer.get("/search/", {"q": "   "})
    assert empty.status_code == 200 and "No match" not in text(empty)


def test_an_archived_operator_is_listed_and_never_jumped_to(viewer: Client) -> None:
    world()
    gone = Operator.objects.create(display_name="Zebra Holdings")
    Operator.objects.filter(pk=gone.pk).update(archived_at="2026-09-01T00:00:00Z")
    response = viewer.get("/search/", {"q": "zebra"})
    assert response.status_code == 200
    page = text(response)
    assert (
        f'href="/operators/{gone.pk}/">Zebra Holdings</a> <span class="badge">Archived</span>'
        in page
    )


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_every_role_searches_and_nothing_is_written(role: Role) -> None:
    world()
    client, _ = client_for(role)
    assert client.get("/search/", {"q": "plaza"}).status_code == 200
    assert client.post("/search/", {"q": "plaza"}).status_code in (403, 405)
    assert Client().get("/search/", {"q": "plaza"}).status_code == 401


def test_the_route_is_read_only_and_the_box_is_in_the_nav(viewer: Client) -> None:
    assert reverse("ledger:search") == "/search/"
    assert "ledger:search" in registry.READ_ONLY_ROUTE_NAMES
    assert registry.rule_for("ledger:search") is None
    page = text(viewer.get("/"))
    # PLAN-012f D3: the box sits in the dashboard's page header (and the stores list's and
    # the grid's); the phone bar's search opens the same GET form on every page.
    start = page.index('<header class="page-header-bar" id="page-header">')
    nav = page[start : page.index("</header>", start)]
    assert (
        '<form method="get" action="/search/" class="nav-search" id="nav-search" role="search">'
        '<input type="search" name="q" id="nav-search-q" maxlength="100" '
        'placeholder="Store, city or operator"'
    ) in nav
    assert (
        '<form method="get" action="/search/" class="phone-search" id="phone-search" role="search">'
        '<input type="search" name="q" id="phone-search-q" maxlength="100" '
    ) in page
    template = (REPO / "ledger" / "templates" / "ledger" / "search" / "results.html").read_text()
    assert "\n{% load responsive %}\n" in template
