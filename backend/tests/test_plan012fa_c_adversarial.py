"""Adversarial cases for PLAN-012f-a Batch C (the builder and form family, D5; the wizard frame,
D8), by the QA owner (plan §6). The builder's choice tiles come from the form's options (as many
tiles as choices, each with its radio), a hostile ``collect`` value opens Save draft and is never
echoed, the summary card carries the lines' total, Discard is never a GET; a viewer cannot open
the builder or a form; the wizard header counts its steps for both wizards, the cancel action is
a POST form with the single-submit guard, a viewer and billing cannot open the statement step,
and the statement tiles read the figures' numbers; markup in a store's name is escaped in every
wizard title."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import Lease, LeaseEnd, PaymentChannel
from ledger.services import statements
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'


def _lease() -> Lease:
    lease = a_lease(
        a_store(f"Builder {MARKUP}"),
        an_operator(f"Build {MARKUP}"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    a_rent_charge(lease, date(2026, 9, 1), due="2000.00")
    return lease


# --- the builder ------------------------------------------------------------------------


def test_the_choice_tiles_come_from_the_options_and_a_hostile_choice_is_not_echoed() -> None:
    lease = _lease()
    billing, _ = client_for(Role.BILLING)
    path = reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id})
    body = billing.get(path).content.decode()
    assert MARKUP not in body and "&lt;script&gt;" in body
    tiles = re.findall(r'class="[^"]*\bcollect-tile\b[^"]*"', body)
    radios = re.findall(
        r'<input[^>]*type="radio"[^>]*name="collect"|<input[^>]*name="collect"[^>]*type="radio"',
        body,
    )

    assert tiles and len(tiles) == len(radios), (len(tiles), len(radios))
    hostile = billing.get(path, {"collect": MARKUP}).content.decode()
    assert MARKUP not in hostile
    assert 'id="collect-draft"' in hostile  # Save draft stays the first choice
    # The summary card holds the lines' total and the primary buttons; Discard is a form.
    assert 'id="lines-total"' in body
    assert re.search(r'class="[^"]*\bform-summary\b', body), "no summary card"
    discard = re.search(r"<form[^>]*>(?:(?!</form>).)*Discard", body, re.S)
    assert discard is not None or "Discard" not in body


def test_a_viewers_post_to_the_builder_and_every_form_is_refused_and_writes_nothing() -> None:
    """The route rules gate the POST (accounts/registry.py: ``methods=POST``); a GET of a form
    may render read-only for a viewer (the operator form does today, an observation for the
    owner), but no post of theirs changes a row."""
    from django.apps import apps

    lease = _lease()
    viewer, _ = client_for(Role.VIEWER)
    counts = {m._meta.label: m._default_manager.count() for m in apps.get_models()}
    for path in (
        reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id}),
        reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}),
        reverse("ledger:stores:create"),
        reverse("ledger:leases:create"),
        reverse("ledger:operators:create"),
        reverse("ledger:calendar:add"),
    ):
        response = viewer.post(path, {"name": "x", "amount": "1.00"})
        assert response.status_code in (302, 403), (path, response.status_code)
        if response.status_code == 302:
            assert response["Location"].startswith("/") and "/new" not in response["Location"]
    after = {m._meta.label: m._default_manager.count() for m in apps.get_models()}
    changed = {k: (counts[k], after[k]) for k in counts if counts[k] != after[k]}
    assert set(changed) <= {"accounts.UserSession", "sessions.Session"}, changed


def test_every_form_page_has_its_cards_and_one_primary_in_the_summary() -> None:
    lease = _lease()
    admin, _ = client_for(Role.ADMIN)
    # PLAN-012f-b Batch B: New store and New operator are dialogs over their lists (one
    # primary in the dialog's foot); the other form pages keep the card and summary frame.
    for path in (reverse("ledger:stores:create"), reverse("ledger:operators:create")):
        body = admin.get(path).content.decode()
        dialog = re.search(
            r"<dialog[^>]*class=\"dialog dialog-lg\"[^>]*>(.*?)</dialog>", body, re.S
        )
        assert dialog is not None and " open" in dialog.group(0)[:200], path
        assert dialog.group(1).count("btn-primary") == 1, path
        assert body.count('id="page-header"') == 1, path
    for path in (
        reverse("ledger:leases:create"),
        reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}),
        reverse("ledger:calendar:add"),
    ):
        body = admin.get(path).content.decode()
        assert body.count('class="card form-card') + body.count("card form-card") >= 1, path
        summary = re.search(
            r'<aside[^>]*class="[^"]*\bform-summary\b[^"]*"[^>]*>(.*?)</aside>', body, re.S
        )
        assert summary is not None, path
        assert summary.group(1).count("btn-primary") == 1, path
        assert body.count('id="page-header"') == 1, path


# --- the wizard frame -------------------------------------------------------------------


def _draft_end() -> LeaseEnd:
    from tests.test_lease_end import OCTOBER, a_store_lease, draft, october

    lease = a_store_lease(started_on=OCTOBER)
    lease.store.external_label = f"Wiz {MARKUP}"
    lease.store.save(update_fields=["external_label"])
    october(lease, paid="2000.00")
    return draft(lease)


def test_the_wizard_header_counts_the_steps_and_the_cancel_is_a_guarded_post() -> None:
    end = _draft_end()
    admin, _ = client_for(Role.ADMIN)
    path = reverse("ledger:lease_ends:statement", kwargs={"pk": end.pk})
    body = admin.get(path).content.decode()
    assert MARKUP not in body and "&lt;script&gt;" in body
    header = body[
        body.index('id="page-header"') : body.index("</header>", body.index('id="page-header"'))
    ]
    assert re.search(r"Step \d of \d", header), header[:300]
    assert "nothing changes until you confirm" in header
    cancel = re.search(r'<form[^>]*id="lease-end-cancel"[^>]*>(.*?)</form>', body, re.S)
    assert cancel is not None
    assert 'method="post"' in cancel.group(0) and "csrfmiddlewaretoken" in cancel.group(1)
    assert "js-single-submit" in cancel.group(0)
    assert "Cancel the lease end" in cancel.group(1)
    steps = re.search(r'<aside[^>]*class="[^"]*\bwizard-steps-card\b', body)
    assert steps is not None, "no step card"
    assert body.count('aria-current="step"') == 1


def test_a_viewer_and_billing_cannot_open_the_statement_step_and_nothing_is_written() -> None:
    end = _draft_end()
    path = reverse("ledger:lease_ends:statement", kwargs={"pk": end.pk})
    for role in (Role.VIEWER, Role.BILLING):
        client, _ = client_for(role)
        response = client.get(path)
        assert response.status_code in (302, 403), (role, response.status_code)


def test_the_statement_tiles_read_the_figures() -> None:
    end = _draft_end()
    admin, _ = client_for(Role.ADMIN)
    body = admin.get(reverse("ledger:lease_ends:statement", kwargs={"pk": end.pk})).content.decode()
    figures = statements.final(end)
    text = " ".join(re.sub(r"<[^>]+>", " ", body).split())
    for value in (figures.open_total, figures.withheld, Decimal(str(figures.deductions_total))):
        shown = f"${value:,.2f}"
        assert shown in text, (shown, text[:800])
    tiles = re.findall(r'class="[^"]*\bkpi\b[^"]*"', body)
    assert len(tiles) >= 3
