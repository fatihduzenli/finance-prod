"""PLAN-012f-b Batch D, slice D-1 (D15; the owner's finding 6): the invoice builder's How to
collect without a Send by e-mail tile, Save with its "Invoice saved" prompt over the Invoices
tab, and the Charge now, Schedule for a date and Set up autopay tiles greyed with one line
(never hidden) and refused when posted anyway. Nothing reaches a network.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.test import override_settings

from accounts import registry
from accounts.roles import Role
from ledger import forms as ledger_forms
from ledger.models import AuditLog, Invoice, InvoiceDraft, InvoiceDraftStatus, Lease, Operator
from ledger.services import invoice_builder, workspace
from ledger.services import one_off_charges as one_off_service
from payments.fake import FakeProvider
from payments.models import Job, JobKind
from tests.test_charge_now import store_methods
from tests.test_how_to_collect import (
    a_draft,
    a_world,
    nothing_written,
    one_line,
    page_post,
    two_lines,
)
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
APP_CSS = Path(__file__).resolve().parents[1] / "finance" / "static" / "finance" / "app.css"
ORDER = ["draft", "charge_now", "schedule", "record", "autopay", "settlement"]


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def enabled() -> Any:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


def banked(fake: FakeProvider) -> tuple[Operator, Lease]:
    """An operator with a usable bank account on file and an active lease."""
    operator, lease = a_world(fake)
    store_methods(operator)
    return operator, lease


def page(path: str, role: Role = Role.BILLING) -> str:
    response = client_for(role)[0].get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def draft_html(draft: InvoiceDraft, role: Role = Role.BILLING) -> str:
    return page(f"/invoices/drafts/{draft.pk}/", role)


def tile(html: str, value: str) -> str:
    start = html.index(f'id="tile-{value}"')
    return html[html.rfind("<div", 0, start) : html.index("</label>", start)]


def greyed(html: str, value: str) -> str:
    """The one line of the greyed tile ``value`` (its radio disabled), else ``""``."""
    found = tile(html, value)
    if "is-unavailable" not in found:
        assert " disabled" not in found, value
        return ""
    assert re.search(rf'id="collect-{value}"[^>]* disabled>', found), value
    match = re.search(rf'id="{value}-reason">([^<]*)</span>', found)
    assert match is not None, value
    return html_lib.unescape(match.group(1))


def saved_path(draft: InvoiceDraft) -> str:
    return f"/invoices/drafts/{draft.pk}/saved/"


def dialog_of(html: str) -> str:
    start = html.index('<dialog class="dialog dialog-sm" id="invoice-saved"')
    return html[start : html.index("</dialog>", start)]


def send_url_of(html: str) -> str:
    match = re.search(r'<form method="post" action="([^"]+)"[^>]*id="invoice-saved-form"', html)
    assert match is not None
    return html_lib.unescape(match.group(1))


# --- The tiles ------------------------------------------------------------------------------------


def test_the_tiles_run_save_first_and_no_tile_sends_by_e_mail(
    fake: FakeProvider, enabled: None
) -> None:
    assert [value for value, _ in ledger_forms.COLLECT_CHOICES] == ORDER
    assert dict(ledger_forms.COLLECT_CHOICES)["draft"] == "Save"
    # The e-mail stays the send's own path, never a tile.
    assert invoice_builder.CollectChoice.EMAIL.value == "email"
    operator, lease = banked(fake)
    html = draft_html(a_draft(operator, lease, one_line("175.00")), Role.ADMIN)
    places = [html.index(f'id="tile-{value}"') for value in ORDER]
    assert places == sorted(places)
    assert 'id="tile-email"' not in html and 'id="collect-email"' not in html
    assert 'value="email"' not in html and 'id="send-draft"' not in html
    assert '<span class="collect-name">Send by e-mail</span>' not in html
    assert '<span class="collect-name">Save</span>' in html
    assert 'id="save-draft" data-shown-when="collect-draft">Save</button>' in html
    assert all(greyed(html, value) == "" for value in ORDER)
    # The native radio is unseen but in the page for the keyboard.
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".collect-option input[type=radio] { position: absolute; opacity: 0;" in css
    assert ".collect-tile:has(input[type=radio]:focus-visible)" in css


def test_an_e_mail_choice_posted_to_the_builder_is_refused_and_nothing_is_saved(
    fake: FakeProvider,
) -> None:
    operator, lease = banked(fake)
    client = client_for(Role.BILLING)[0]
    for field in ("collect", "submit_collect"):
        response = client.post(
            f"/invoices/new/store/{lease.store_id}/", page_post(lease, **{field: "email"})
        )
        assert response.status_code == 302, field
        assert not InvoiceDraft.objects.exists(), field
    assert nothing_written() == (0, 0, 0, 0)


# --- Save and its prompt --------------------------------------------------------------------------


def test_save_on_a_new_draft_and_on_an_edit_opens_the_prompt(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    client = client_for(Role.BILLING)[0]
    response = client.post(f"/invoices/new/store/{lease.store_id}/", page_post(lease))
    draft = InvoiceDraft.objects.get()
    assert response["Location"] == saved_path(draft)
    edited = client.post(
        f"/invoices/drafts/{draft.pk}/edit/", page_post(lease, submit_collect="draft")
    )
    assert edited["Location"] == saved_path(draft)
    assert InvoiceDraft.objects.get().status == InvoiceDraftStatus.DRAFT
    assert nothing_written() == (0, 0, 0, 0)


def test_the_prompt_is_the_invoices_tab_with_the_dialog_open_and_writes_nothing(
    fake: FakeProvider,
) -> None:
    operator, lease = banked(fake)
    draft = a_draft(operator, lease, two_lines())
    audits = AuditLog.objects.count()
    tab = workspace.tab_url(lease.store_id, workspace.TAB_INVOICES)
    # Whatever the query says, the page behind is the Invoices tab.
    html = page(saved_path(draft) + "?tab=charges&filter=unpaid")
    assert 'id="store-drafts"' in html and f'id="draft-{draft.pk}"' in html
    dialog = dialog_of(html)
    assert dialog.startswith('<dialog class="dialog dialog-sm" id="invoice-saved"')
    assert ' open data-return-url="' in dialog and " data-open>" in dialog
    assert f'data-return-url="{html_lib.escape(tab)}"' in dialog
    assert '<h2 id="invoice-saved-title">Invoice saved</h2>' in dialog
    question = f"Send it to {html_lib.escape(operator.display_name)} by e-mail now?"
    assert f'<p id="invoice-saved-question">{question}</p>' in dialog
    assert f'<a class="btn" id="invoice-saved-later" href="{html_lib.escape(tab)}">Not now</a>' in (
        dialog
    )
    assert (
        '<button class="btn btn-primary" type="submit" id="invoice-saved-send">Send e-mail</button>'
    ) in dialog
    assert 'class="js-single-submit" id="invoice-saved-form"' in dialog
    assert 'name="csrfmiddlewaretoken"' in dialog
    assert send_url_of(html) == f"/invoices/drafts/{draft.pk}/send/?store={lease.store_id}"
    assert dialog.count("btn-primary") == 1
    assert AuditLog.objects.count() == audits and nothing_written() == (0, 0, 0, 0)
    assert InvoiceDraft.objects.get().status == InvoiceDraftStatus.DRAFT


def test_send_e_mail_sends_the_draft_due_in_fifteen_days_or_on_its_date(
    fake: FakeProvider,
) -> None:
    operator, lease = banked(fake)
    client = client_for(Role.BILLING)[0]
    for due, expected in (
        (None, TODAY + timedelta(days=invoice_builder.DUE_DAYS)),
        (date(2026, 11, 20), date(2026, 11, 20)),
    ):
        draft = a_draft(operator, lease, one_line())
        if due is not None:
            InvoiceDraft.objects.filter(pk=draft.pk).update(due_date=due)
        url = send_url_of(page(saved_path(draft)))
        response = client.post(url)
        assert response["Location"] == workspace.tab_url(lease.store_id, workspace.TAB_INVOICES)
        draft.refresh_from_db()
        assert draft.status == InvoiceDraftStatus.SENT and draft.sent_invoice is not None
        assert draft.sent_invoice.due_date == expected
        assert Job.objects.filter(kind=JobKind.INVOICE_CREATE).count() == Invoice.objects.count()
    assert invoice_builder.DUE_DAYS == 15
    assert Invoice.objects.count() == 2


def test_not_now_leaves_the_draft_under_drafts_with_its_send_by_e_mail(
    fake: FakeProvider,
) -> None:
    operator, lease = banked(fake)
    draft = a_draft(operator, lease, one_line())
    later = re.search(r'id="invoice-saved-later" href="([^"]+)"', page(saved_path(draft)))
    assert later is not None
    html = page(html_lib.unescape(later.group(1)).split("#")[0])
    row = html[
        html.index(f'id="draft-{draft.pk}"') : html.index(
            "</tr>", html.index(f'id="draft-{draft.pk}"')
        )
    ]
    assert '<button class="btn btn-primary" type="submit">Send by e-mail</button>' in row
    assert ">Edit</a>" in row and ">Delete</button>" in row
    assert 'id="invoice-saved"' not in html
    assert InvoiceDraft.objects.get().status == InvoiceDraftStatus.DRAFT


def test_a_draft_sent_meanwhile_goes_to_the_tab_with_the_refusal(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    draft = a_draft(operator, lease, one_line())
    assert invoice_builder.send(draft.pk, client_for(Role.BILLING)[1], today=TODAY).ok
    client = client_for(Role.BILLING)[0]
    response = client.get(saved_path(draft))
    tab = workspace.tab_url(lease.store_id, workspace.TAB_INVOICES)
    assert (response.status_code, response["Location"]) == (302, tab)
    number = InvoiceDraft.objects.get().sent_invoice.invoice_number  # type: ignore[union-attr]
    shown = client.get(tab).content.decode()
    assert html_lib.escape(invoice_builder.ALREADY_SENT.format(number=number)) in shown


def test_an_operator_draft_opens_the_prompt_over_the_operator_pages_invoices_tab(
    fake: FakeProvider,
) -> None:
    operator, _ = banked(fake)
    draft = a_draft(operator, None, one_line())
    html = page(saved_path(draft))
    tab = workspace.operator_tab_url(operator.pk, workspace.TAB_INVOICES)
    dialog = dialog_of(html)
    assert f'data-return-url="{html_lib.escape(tab)}#tab-invoices"' in dialog
    assert send_url_of(html).startswith(f"/invoices/drafts/{draft.pk}/send/?next=")
    response = client_for(Role.BILLING)[0].post(send_url_of(html))
    assert response["Location"] == tab
    assert InvoiceDraft.objects.get().status == InvoiceDraftStatus.SENT


def test_the_prompt_is_billings_and_get_only(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    draft = a_draft(operator, lease, one_line())
    assert client_for(Role.VIEWER)[0].get(saved_path(draft)).status_code == 403
    assert client_for(Role.BILLING)[0].post(saved_path(draft)).status_code == 403
    assert "ledger:invoices:saved" in registry.READ_ONLY_ROUTE_NAMES
    assert registry.rule_for("ledger:invoices:saved") is None
    assert InvoiceDraft.objects.get().status == InvoiceDraftStatus.DRAFT
    assert nothing_written() == (0, 0, 0, 0)


# --- Greyed, never hidden -------------------------------------------------------------------------


def test_no_bank_account_on_file_greys_every_bank_debit_tile_with_one_line(
    fake: FakeProvider, enabled: None
) -> None:
    for operator in (an_operator(fake, customer="cus-nb", bank=None), an_operator(fake)):
        # No linked method; a linked method the stored list does not show. The line is Add
        # payment's, naming the operator (PLAN-013a C3).
        no_bank = f"No bank account on file for {operator.display_name}."
        assert no_bank == one_off_service.DEBIT_NO_ACCOUNT.format(operator=operator.display_name)
        lease = make_lease(operator=operator)
        assert invoice_builder.bank_unavailable(operator) == no_bank
        assert invoice_builder.charge_now_unavailable(operator) == no_bank
        html = draft_html(a_draft(operator, lease, one_line()), Role.ADMIN)
        for value in ("charge_now", "schedule", "autopay"):
            assert greyed(html, value) == no_bank, value
        assert greyed(html, "draft") == greyed(html, "record") == ""
        for absent in ("draft-charge-now", "schedule-draft", "autopay-draft"):
            assert f'id="{absent}"' not in html, absent
        assert 'id="collect-schedule-fields"' not in html
        assert 'id="collect-autopay-fields"' not in html
        assert 'id="collect-draft" checked' in html


def test_the_pay_flag_off_greys_charge_now_only(fake: FakeProvider) -> None:
    """The pay-an-invoice flag gates Charge now's call alone; schedules and plans work
    with it off (staging runs so)."""
    operator, lease = banked(fake)
    off = str(one_off_service.DEBIT_OFF)
    assert off == "Bank debits are off at Stax."
    assert invoice_builder.charge_now_unavailable(operator) == off
    assert invoice_builder.bank_unavailable(operator) == ""
    html = draft_html(a_draft(operator, lease, one_line()), Role.ADMIN)
    assert greyed(html, "charge_now") == off and 'id="draft-charge-now"' not in html
    # Staging's case: Charge now was missing altogether; it is a greyed tile now.
    assert '<span class="collect-name">Charge now</span>' in html
    assert greyed(html, "schedule") == greyed(html, "autopay") == ""
    assert 'id="schedule-draft"' in html and 'id="autopay-draft"' in html
    # And Schedule for a date goes through with the flag off.
    client = client_for(Role.BILLING)[0]
    response = client.post(
        f"/invoices/new/store/{lease.store_id}/",
        page_post(
            lease, collect="schedule", schedule_on="2026-10-20", **{"lines-TOTAL_FORMS": "1"}
        ),
    )
    assert response["Location"] == workspace.tab_url(lease.store_id, workspace.TAB_CHARGES)
    assert Job.objects.filter(kind=JobKind.SCHEDULE_CREATE).count() == 1


def test_billing_sees_charge_now_greyed_for_an_admin(fake: FakeProvider, enabled: None) -> None:
    operator, lease = banked(fake)
    draft = a_draft(operator, lease, one_line())
    billing = draft_html(draft)
    assert greyed(billing, "charge_now") == ledger_forms.CHARGE_NOW_NEEDS_ADMIN
    assert ledger_forms.CHARGE_NOW_NEEDS_ADMIN == "Charge now needs an admin."
    assert greyed(billing, "schedule") == "" and 'id="draft-charge-now"' not in billing
    admin = draft_html(draft, Role.ADMIN)
    assert greyed(admin, "charge_now") == "" and 'id="draft-charge-now"' in admin
    # ?collect= never pre-selects a greyed tile.
    held = page(f"/invoices/drafts/{draft.pk}/?collect=charge_now")
    assert 'id="collect-draft" checked' in held


def test_no_lease_or_more_than_one_line_greys_schedule_and_autopay(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = banked(fake)
    line = invoice_builder.SCHEDULE_NOT_OFFERED
    for draft in (a_draft(operator, None, one_line()), a_draft(operator, lease, two_lines())):
        html = draft_html(draft)
        assert (greyed(html, "schedule"), greyed(html, "autopay")) == (line, line)
        assert 'id="autopay-draft"' not in html and 'id="collect-autopay-fields"' not in html
        assert 'id="collect-schedule-note"' not in html
    # The bank's line comes first where both apply; the pay flag plays no part.
    unbanked = an_operator(fake, customer="cus-nb2", bank=None)
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        html = draft_html(a_draft(unbanked, None, one_line()))
    no_bank = one_off_service.DEBIT_NO_ACCOUNT.format(operator=unbanked.display_name)
    assert greyed(html, "schedule") == greyed(html, "autopay") == no_bank
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        html = draft_html(a_draft(operator, None, one_line()))
    assert greyed(html, "schedule") == greyed(html, "autopay") == line


@pytest.mark.parametrize(
    ("role", "fields", "bank", "sentence"),
    [
        (Role.BILLING, {"submit_collect": "charge_now"}, True, "admin"),
        (Role.ADMIN, {"collect": "charge_now"}, False, "no_bank"),
        (
            Role.BILLING,
            {"collect": "schedule", "schedule_on": "2026-11-01", "lines-TOTAL_FORMS": "1"},
            False,
            "no_bank",
        ),
        (
            Role.BILLING,
            {
                "collect": "autopay",
                "autopay_cadence": "monthly",
                "autopay_first_on": "2026-11-01",
                "autopay_end_rule": "count",
                "autopay_end_count": "3",
                "lines-TOTAL_FORMS": "1",
            },
            False,
            "no_bank",
        ),
    ],
)
def test_a_posted_greyed_choice_is_refused_with_its_line_and_nothing_is_written(
    fake: FakeProvider,
    enabled: None,
    role: Role,
    fields: dict[str, str],
    bank: bool,
    sentence: str,
) -> None:
    operator = an_operator(fake) if bank else an_operator(fake, bank=None)
    # The sentences are read inside the test: the no-bank line names the operator.
    sentence = {
        "admin": ledger_forms.CHARGE_NOW_NEEDS_ADMIN,
        "no_bank": one_off_service.DEBIT_NO_ACCOUNT.format(operator=operator.display_name),
    }[sentence]
    if bank:
        store_methods(operator)
    lease = make_lease(operator=operator)
    client = client_for(role)[0]
    builder = f"/invoices/new/store/{lease.store_id}/"
    response = client.post(builder, page_post(lease, **fields))
    assert response["Location"] == builder
    assert not InvoiceDraft.objects.exists() and nothing_written() == (0, 0, 0, 0)
    assert html_lib.escape(sentence) in client.get(builder).content.decode()
    # The same on a saved draft's edit: the draft stays as it was.
    draft = a_draft(operator, lease, one_line())
    audits = AuditLog.objects.count()
    edit = client.post(f"/invoices/drafts/{draft.pk}/edit/", page_post(lease, **fields))
    assert edit["Location"] == f"/invoices/drafts/{draft.pk}/"
    assert AuditLog.objects.count() == audits and nothing_written() == (0, 0, 0, 0)
    assert InvoiceDraft.objects.get().status == InvoiceDraftStatus.DRAFT


# --- Dates on screen, the route -------------------------------------------------------------------


def test_the_debit_date_reads_nov_1_2026_and_the_field_keeps_iso(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = banked(fake)
    client = client_for(Role.BILLING)[0]
    builder = f"/invoices/new/store/{lease.store_id}/"
    # A line the form refuses: nothing is saved, the form comes back with its date.
    client.post(
        builder,
        page_post(
            lease,
            collect="schedule",
            schedule_on="2026-11-01",
            **{"lines-TOTAL_FORMS": "1", "lines-0-unit_amount": "abc"},
        ),
    )
    assert not InvoiceDraft.objects.exists()
    html = client.get(builder).content.decode()
    assert '<span class="collect-date" data-empty="the date chosen">Nov 1, 2026</span>' in html
    assert 'value="2026-11-01"' in html and ">2026-11-01<" not in html
    form = ledger_forms.CollectForm(
        {"collect": "schedule", "schedule_on": "not-a-date"}, can_charge_now=True
    )
    assert form.debit_on() is None


def test_the_prompt_route_is_a_billing_page_over_the_tab() -> None:
    from tests import test_plan012f_d_pages as pages

    assert pages.PAGES["ledger:invoices:saved"] == Role.BILLING
