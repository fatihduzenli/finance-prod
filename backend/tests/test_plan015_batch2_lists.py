"""PLAN-015 Batch 2 (docs/qa/15/PLAN-015-batch-2-lists.md): the list pages in the dashboard's
language — the shared header (the title block with the counts as its muted sub-line, then the
actions: a segmented control where the page has one, "Download spreadsheet" with the dashboard's
icon, the page's other buttons, the primary last), the Payments totals as KPI cards v2 and its
period links, the Operators avatar tile, and the status chips of the Outbox, Bank deposits and
the Invoices tab, with the Bank deposits state as a segmented control."""

from __future__ import annotations

import re

import pytest
from django.test import Client

from accounts.roles import Role
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db


def page(client: Client, path: str, expect: int = 200) -> str:
    response = client.get(path)
    assert response.status_code == expect, (path, response.status_code)
    return response.content.decode()


def header_of(html: str) -> str:
    start = html.index('<div class="page-header">')
    return html[start : html.index("</header>", start)]


# --- §3.1 The header ------------------------------------------------------------------------------

#: The six pages, the title, the counts sentence with one lease in the data and whether the
#: page has "Download spreadsheet". The seventh list of the plan, the invoices, is the store and
#: operator pages' Invoices tab: its header is the workspace's (Batch 3).
HEADERS = [
    ("/stores/", "Stores", "1 store · 1 with a lease · 0 vacant", True),
    ("/operators/", "Operators", "1 operator", True),
    ("/leases/", "Leases", "1 lease", True),
    ("/payments/", "Payments", "0 payments", True),
    ("/admin-settings/bank-deposits/", "Bank deposits", "0 deposits", False),
    ("/admin-settings/outbox/", "Outbox", "0 messages", False),
]


@pytest.mark.parametrize("role", [Role.BILLING, Role.ADMIN])
@pytest.mark.parametrize(("path", "title", "counts", "export"), HEADERS)
def test_the_list_pages_share_the_dashboards_header(
    role: Role, path: str, title: str, counts: str, export: bool
) -> None:
    a_lease(a_store("2601 HEAD", 2601), an_operator("Head Co"))
    client = client_for(role)[0]
    if path == "/admin-settings/outbox/" and role != Role.ADMIN:
        page(client, path, expect=403)
        return
    header = header_of(page(client, path))
    assert header.startswith(
        f'<div class="page-header"><div class="page-title-block"><h1>{title}</h1>'
        f'<p class="page-sub" id="list-counts">{counts}</p></div><div class="page-actions">'
    ), header[:300]
    assert 'class="list-counts"' not in header and 'class="page-title"' not in header
    actions = header[header.index('<div class="page-actions">') :]
    if export:
        button = re.search(r'<a class="btn" id="export-csv" href="[^"]+">(.*?)</a>', actions)
        assert button is not None
        assert button[1].startswith('<svg class="icon icon-file" width="16" height="16"')
        assert button[1].endswith("</svg>Download spreadsheet")
    else:
        assert 'id="export-csv"' not in actions
    if "btn-primary" in actions:  # the primary "+ New …" comes last
        assert actions.index('id="export-csv"') < actions.index('class="btn btn-primary"')
        assert actions.count('class="btn btn-primary"') == 1
        assert actions.index('class="btn btn-primary"') > actions.rindex('class="btn"')
