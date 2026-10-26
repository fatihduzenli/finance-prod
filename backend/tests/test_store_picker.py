"""The "Which store?" picker and the dialog component (docs/qa/PLAN-012f-b-staging-review.md D2,
D7; §6 A, the implementer's cases; the adversarial ones are the QA owner's file).

The JSON route lists at most ten stores matching the query as the top-bar search does (number
prefix, name, operator), each with its Create invoice page; an empty query lists nothing; a closed
store says so; billing and above, signed in. The dashboard embeds the picker for billing and not
for a viewer; the search page with intent=new_invoice links each store to its builder. The
{% dialog %} tag renders the frame and keeps only a path on this site as its return URL.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from django.template import Context, Template, TemplateSyntaxError
from django.test import Client
from django.urls import reverse

from accounts import registry
from accounts.roles import Role
from ledger.models import Store, StoreStatus
from ledger.services import search
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
def billing() -> Client:
    return client_for(Role.BILLING)[0]


def builder(store: Store) -> str:
    return reverse("ledger:invoices:new_store", kwargs={"pk": store.pk})


def world() -> dict[str, Store]:
    """1991 Istanbul (Fatih Co), 1992 Izmir (Beacon Ltd), 2484 Paris (no operator)."""
    istanbul = a_store("Istanbul", 1991)
    a_lease(istanbul, an_operator("Fatih Co"))
    izmir = a_store("Izmir", 1992)
    a_lease(izmir, an_operator("Beacon Ltd"))
    return {"1991": istanbul, "1992": izmir, "2484": a_store("Paris", 2484)}


def rows(client: Client, q: str) -> list[dict[str, object]]:
    response = client.get(reverse("ledger:search_stores"), {"q": q})
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    return list(json.loads(response.content)["stores"])


# --- The JSON route (D7) ------------------------------------------------------------------------


def test_the_route_is_read_only_and_named() -> None:
    assert reverse("ledger:search_stores") == "/search/stores.json"
    assert "ledger:search_stores" in registry.READ_ONLY_ROUTE_NAMES
    assert registry.rule_for("ledger:search_stores") is None


def test_a_row_is_number_name_operator_closed_and_the_builder(billing: Client) -> None:
    stores = world()
    assert rows(billing, "1991") == [
        {
            "number": 1991,
            "name": "Istanbul",
            "operator": "Fatih Co",
            "closed": False,
            "url": builder(stores["1991"]),
        }
    ]
    assert rows(billing, "2484")[0]["operator"] == ""


def test_number_prefix_name_and_operator_match(billing: Client) -> None:
    world()
    assert [row["number"] for row in rows(billing, "199")] == [1991, 1992]
    assert [row["number"] for row in rows(billing, "#1992")] == [1992]
    assert [row["number"] for row in rows(billing, "paris")] == [2484]
    assert [row["number"] for row in rows(billing, "beacon")] == [1992]
    assert [row["number"] for row in rows(billing, "fatih istanbul")] == [1991]
    assert rows(billing, "nowhere") == []


@pytest.mark.parametrize("q", ["", "   ", "\x00"])
def test_an_empty_query_lists_nothing(billing: Client, q: str) -> None:
    world()
    assert rows(billing, q) == []
    assert billing.get(reverse("ledger:search_stores")).status_code == 200


def test_at_most_ten_rows_in_store_number_order(billing: Client) -> None:
    for number in range(3014, 2999, -1):
        a_store(f"Many {number}", number)
    found = rows(billing, "many")
    assert [row["number"] for row in found] == list(range(3000, 3010))


def test_a_long_query_is_cut_at_100_characters(billing: Client) -> None:
    a_store("x" * 100, 7)
    assert [row["number"] for row in rows(billing, "x" * 150)] == [7]
    assert search.store_rows("x" * 150) == []  # the view cuts, the service does not


@pytest.mark.parametrize("status", [StoreStatus.CLOSED, StoreStatus.TERMINATED])
def test_a_closed_store_is_listed_and_says_so(billing: Client, status: str) -> None:
    store = a_store("Rome", 5151)
    Store.objects.filter(pk=store.pk).update(status=status)
    assert rows(billing, "5151")[0]["closed"] is True


def test_markup_in_a_name_is_data(billing: Client) -> None:
    a_store('<b onmouseover="x">Bad &amp;</b>', 9)
    response = billing.get(reverse("ledger:search_stores"), {"q": "bad"})
    assert b"<" not in response.content and b"&" not in response.content
    assert json.loads(response.content)["stores"][0]["name"] == '<b onmouseover="x">Bad &amp;</b>'


def test_signed_out_a_viewer_and_a_post_are_refused() -> None:
    world()
    url = reverse("ledger:search_stores")
    anonymous = Client().get(url, {"q": "1991"})
    assert anonymous.status_code in (302, 401, 403)
    assert b"Istanbul" not in anonymous.content
    viewer = client_for(Role.VIEWER)[0].get(url, {"q": "1991"})
    assert viewer.status_code == 403 and b"Istanbul" not in viewer.content
    admin = client_for(Role.ADMIN)[0]
    assert admin.get(url, {"q": "1991"}).status_code == 200
    assert admin.post(url, {"q": "1991"}).status_code in (403, 405)


# --- The dashboard and the search page (D7) -----------------------------------------------------


def test_the_dashboard_embeds_the_picker_for_billing(billing: Client) -> None:
    page = billing.get("/").content.decode()
    assert (
        '<a class="btn btn-primary" id="new-invoice" href="/search/?intent=new_invoice" '
        'data-dialog-open="store-picker">'
    ) in page
    assert '<dialog class="dialog dialog-sm" id="store-picker"' in page
    assert '<h2 id="store-picker-title">Which store?</h2>' in page
    assert 'data-url="/search/stores.json"' in page
    assert '<input type="hidden" name="intent" value="new_invoice">' in page
    assert 'id="store-picker-results"' in page and "Type to search" in page
    assert "ledger/store_picker.js" in page and "finance/dialog.js" in page


def test_the_dashboard_has_no_picker_for_a_viewer() -> None:
    page = client_for(Role.VIEWER)[0].get("/").content.decode()
    assert 'id="new-invoice"' not in page and 'id="store-picker"' not in page
    assert "ledger/store_picker.js" not in page
    assert "finance/dialog.js" in page  # the component is on every page


def test_the_search_page_for_a_new_invoice_links_to_the_builders(billing: Client) -> None:
    stores = world()
    page = billing.get("/search/", {"q": "199", "intent": "new_invoice"}).content.decode()
    assert "<h1>New invoice — which store?</h1>" in page
    assert f'href="{builder(stores["1991"])}"' in page
    assert f'href="{builder(stores["1992"])}"' in page
    assert f'href="/stores/{stores["1991"].pk}/"' not in page
    assert 'id="search-operators-card"' not in page
    form = page[
        page.index('id="search-form"') : page.index("</form>", page.index('id="search-form"'))
    ]
    assert '<input type="hidden" name="intent" value="new_invoice">' in form


def test_one_store_for_a_new_invoice_jumps_to_its_builder(billing: Client) -> None:
    stores = world()
    response = billing.get("/search/", {"q": "1991", "intent": "new_invoice"})
    assert response.status_code == 302 and response["Location"] == builder(stores["1991"])
    # An operator alone never jumps for a new invoice: the stores card says none matched.
    an_operator("Zebra Holdings")
    page = billing.get("/search/", {"q": "zebra", "intent": "new_invoice"})
    assert page.status_code == 200 and 'id="search-no-store"' in page.content.decode()


def test_the_plain_search_page_is_unchanged_and_offers_the_picker(billing: Client) -> None:
    stores = world()
    page = billing.get("/search/", {"q": "199"}).content.decode()
    assert "<h1>Search</h1>" in page
    assert f'href="/stores/{stores["1991"].pk}/"' in page
    assert (
        'id="new-invoice-for" href="/search/?intent=new_invoice" data-dialog-open="store-picker">'
    ) in page
    assert 'id="store-picker"' in page
    viewer = client_for(Role.VIEWER)[0]
    plain = viewer.get("/search/", {"q": "199"}).content.decode()
    assert 'id="new-invoice-for"' not in plain and 'id="store-picker"' not in plain
    # A viewer's intent is ignored: the plain search, its one-store jump to the store page.
    jump = viewer.get("/search/", {"q": "1991", "intent": "new_invoice"})
    assert jump.status_code == 302 and jump["Location"] == f"/stores/{stores['1991'].pk}/"


# --- The dialog tag (D2) ------------------------------------------------------------------------


def render(source: str, **context: object) -> str:
    return Template("{% load components %}" + source).render(Context(context))


def test_the_dialog_tag_renders_the_frame() -> None:
    html = render(
        '{% dialog id="d" title=title size="lg" %}<p>Body</p>{% enddialog %}', title="<i>"
    )
    assert html == (
        '<dialog class="dialog dialog-lg" id="d" aria-labelledby="d-title">'
        '<div class="dialog-head"><h2 id="d-title">&lt;i&gt;</h2>'
        '<button type="button" class="dialog-close" aria-label="Close">&times;</button></div>'
        '<div class="dialog-body"><p>Body</p></div></dialog>'
    )


def test_an_open_dialog_keeps_only_a_local_return_url() -> None:
    opened = render('{% dialog id="d" title="T" open=True return_url="/stores/" %}{% enddialog %}')
    assert ' data-return-url="/stores/" data-open>' in opened
    for hostile in ("https://example.com/", "//example.com/", "/\\example.com", "javascript:x"):
        html = render(
            '{% dialog id="d" title="T" open=True return_url=u %}{% enddialog %}', u=hostile
        )
        assert "data-return-url" not in html and " data-open>" in html
    closed = render('{% dialog id="d" title="T" %}{% enddialog %}')
    assert "data-open" not in closed and 'class="dialog dialog-sm"' in closed


def test_a_server_opened_dialog_shows_without_the_script() -> None:
    """A1-1 (D2): ``open=True`` writes the ``open`` attribute beside ``data-open``, so the
    dialog shows with scripts off; a closed dialog has neither."""
    opened = render('{% dialog id="d" title="T" open=True %}<p>Body</p>{% enddialog %}')
    assert opened.startswith(
        '<dialog class="dialog dialog-sm" id="d" aria-labelledby="d-title" open data-open>'
    )
    closed = render('{% dialog id="d" title="T" open=False %}{% enddialog %}')
    assert closed.startswith('<dialog class="dialog dialog-sm" id="d" aria-labelledby="d-title">')
    assert " open" not in closed.split(">", 1)[0]
    script = (REPO / "finance" / "static" / "finance" / "dialog.js").read_text()
    # The script turns the attribute's non-modal dialog into a modal one, ignoring the close
    # event of that switch (no return-url navigation).
    assert "dialog.close(); }" in script and "switching" in script


@pytest.mark.parametrize(
    "source",
    [
        '{% dialog id="d" title="T" size="xl" %}{% enddialog %}',
        '{% dialog title="T" %}{% enddialog %}',
        '{% dialog id="d" colour="red" %}{% enddialog %}',
        '{% dialog "d" %}{% enddialog %}',
    ],
)
def test_a_mistaken_dialog_is_a_template_error(source: str) -> None:
    with pytest.raises(TemplateSyntaxError):
        render(source)


def test_the_scripts_set_text_never_markup() -> None:
    picker = (REPO / "ledger" / "static" / "ledger" / "store_picker.js").read_text()
    dialog = (REPO / "finance" / "static" / "finance" / "dialog.js").read_text()
    for script in (picker, dialog):
        assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "textContent" in picker and "fetch(" in picker and "fetch(" not in dialog
    assert "150" in picker and "slice(0, 10)" in picker
