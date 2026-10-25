"""Adversarial cases for PLAN-012f-b Batch C (Add payment by bank debit), by the QA owner (plan
§6). Slice C-1, the services: the bank method shown to staff carries nothing beyond the last four
(Rule 1); a hostile amount or date is refused with nothing written and no job queued; a replayed key
writes once; a charge no Charge now may take (a re-debit, a later builder line) is refused; the
sentences name the processor from the company settings, never the provider (Rule 8)."""

from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.utils import timezone

from ledger.services import one_off_charges as service
from payments.fake import FakeProvider
from payments.models import Job
from tests.test_bank_debit import (  # noqa: F401 — the fixtures, under their names
    admin,
    billing,
    enabled,
    fake,
    setup_charge,
)
from tests.test_lease_end_drafts import row_counts

pytestmark = pytest.mark.django_db

BACKEND = Path(__file__).resolve().parents[1]


def test_the_bank_method_shown_to_staff_holds_nothing_beyond_the_last_four(
    fake: FakeProvider,  # noqa: F811,
) -> None:
    charge = setup_charge(fake)
    options = service.bank_debit_options(charge)
    assert options.method is not None
    assert {field.name for field in dataclasses.fields(options.method)} == {
        "bank_type",
        "last_four",
        "nickname",
    }
    assert re.fullmatch(r"\d{4}", options.method.last_four)
    text = repr(options)
    assert not re.search(r"\d{5,}", text)  # no account or routing number anywhere in it


@pytest.mark.parametrize(
    ("amount", "on_days"),
    [
        (Decimal("0.00"), 0),
        (Decimal("-1.00"), 0),
        (Decimal("999999999.99"), 0),
        (Decimal("0.01"), 7),  # a partial amount on a later date
        (Decimal("250.00"), -1),  # yesterday
        (Decimal("250.00"), 400),  # past the horizon
    ],
)
def test_a_hostile_amount_or_date_is_refused_with_nothing_written(
    fake: FakeProvider,  # noqa: F811
    amount: Decimal,
    on_days: int,
) -> None:
    charge = setup_charge(fake)
    actor = admin()
    before = (row_counts(), Job.objects.count(), fake.calls[:])
    on = timezone.localdate() + timedelta(days=on_days)
    outcome = service.debit(charge, amount, on, actor=actor, confirmed=True, key=uuid.uuid4())
    assert not outcome.ok, (amount, on_days)
    assert outcome.message and "<" not in outcome.message
    assert (row_counts(), Job.objects.count(), fake.calls[:]) == before


def test_a_nan_or_infinite_amount_never_reaches_a_write(
    fake: FakeProvider,  # noqa: F811
) -> None:
    charge = setup_charge(fake)
    actor = admin()
    before = (row_counts(), Job.objects.count())
    for raw in ("NaN", "Infinity", "-Infinity"):
        try:
            outcome = service.debit(
                charge, Decimal(raw), timezone.localdate(), actor=actor, confirmed=True
            )
        except (ValueError, ArithmeticError):
            continue
        assert not outcome.ok, raw
    assert (row_counts(), Job.objects.count()) == before


def test_a_replayed_key_on_a_later_date_writes_one_schedule(
    fake: FakeProvider,  # noqa: F811
) -> None:
    charge = setup_charge(fake)
    key = uuid.uuid4()
    on = timezone.localdate() + timedelta(days=10)
    first = service.debit(charge, Decimal("250.00"), on, actor=billing(), confirmed=True, key=key)
    assert first.ok, first.message
    jobs_after_first = Job.objects.count()
    again = service.debit(charge, Decimal("250.00"), on, actor=billing(), confirmed=True, key=key)
    assert "already requested" in again.message
    assert Job.objects.count() == jobs_after_first


def test_the_sentences_name_the_processor_from_the_settings_never_the_provider() -> None:
    source = (BACKEND / "ledger" / "services" / "one_off_charges.py").read_text(encoding="utf-8")
    debit_block = source[source.index("DEBIT_NO_METHOD") : source.index("def bank_debit_options")]
    assert not re.search(r"stax", debit_block, re.I)
    assert "provider_sentence(" in debit_block


def test_an_unconfirmed_large_debit_is_refused_before_any_write(
    fake: FakeProvider,  # noqa: F811
) -> None:
    from django.test import override_settings

    charge = setup_charge(fake, amount="250.00")
    actor = admin()
    before = (row_counts(), Job.objects.count(), fake.calls[:])
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=Decimal("100.00")):
        options = service.bank_debit_options(charge)
        assert options.large
        outcome = service.debit(
            charge, Decimal("250.00"), timezone.localdate(), actor=actor, confirmed=False
        )
    assert not outcome.ok
    assert (row_counts(), Job.objects.count(), fake.calls[:]) == before


# --- Slice C-2: the Add payment page ------------------------------------------------------------


def _page_url(charge: object) -> str:
    from django.urls import reverse

    return reverse("ledger:one_off_charges:payment", kwargs={"pk": charge.pk})  # type: ignore[attr-defined]


def test_the_page_renders_for_billing_and_writes_nothing_on_get(
    fake: FakeProvider,  # noqa: F811
) -> None:
    from accounts.roles import Role
    from tests.test_operator_views import client_for

    charge = setup_charge(fake)
    client, _ = client_for(Role.BILLING)
    before = (row_counts(), Job.objects.count(), fake.calls[:])
    response = client.get(_page_url(charge))
    assert response.status_code == 200
    body = response.content.decode()
    assert "How is this being paid?" in body
    assert re.findall(r"\d{13,19}", body) == []  # no card or account number on the page
    assert (row_counts(), Job.objects.count(), fake.calls[:]) == before


@pytest.mark.parametrize(
    "data",
    [
        {"how": "<script>alert(1)</script>", "debit_amount": "250.00", "debit_on": "2026-11-01"},
        {"how": "debit", "debit_amount": "250.00", "debit_on": "2026-11-01;drop"},
        {"how": "debit", "debit_amount": "4111111111111111", "debit_on": "2026-11-01"},
        {
            "how": "debit",
            "debit_amount": "250.00",
            "debit_on": "2026-11-01",
            "note": "4111 1111 1111 1111",
        },
        {
            "how": "record",
            "method": "cash",
            "amount": "250.00",
            "paid_on": "2026-10-20",
            "note": "ok",
            "reference": "4111111111111111",
        },
    ],
)
def test_a_hostile_post_is_refused_without_echo_and_writes_nothing(
    fake: FakeProvider,  # noqa: F811
    data: dict[str, str],
) -> None:
    from accounts.roles import Role
    from tests.test_operator_views import client_for

    charge = setup_charge(fake)
    client, _ = client_for(Role.ADMIN)
    before = (row_counts(), Job.objects.count(), fake.calls[:])
    response = client.post(_page_url(charge), {"creation_key": str(uuid.uuid4()), **data})
    assert response.status_code in (200, 302)
    shown = client.get(_page_url(charge)).content.decode()
    assert "4111111111111111" not in shown and "4111 1111" not in shown
    assert "alert(1)" not in shown  # a held date value is re-filled escaped; markup never runs
    assert (row_counts(), Job.objects.count(), fake.calls[:]) == before


def test_a_viewer_cannot_open_or_post_the_page(
    fake: FakeProvider,  # noqa: F811
) -> None:
    from accounts.roles import Role
    from tests.test_operator_views import client_for

    charge = setup_charge(fake)
    viewer, _ = client_for(Role.VIEWER)
    before = (row_counts(), Job.objects.count())
    assert viewer.get(_page_url(charge)).status_code in (302, 403)
    assert viewer.post(_page_url(charge), {"how": "debit"}).status_code in (302, 403)
    assert (row_counts(), Job.objects.count()) == before


def test_a_debit_today_from_the_page_writes_nothing_before_the_confirmation(
    fake: FakeProvider,  # noqa: F811
) -> None:
    from accounts.roles import Role
    from tests.test_operator_views import client_for

    charge = setup_charge(fake)
    client, _ = client_for(Role.ADMIN)
    before = (Job.objects.count(), fake.calls[:])
    response = client.post(
        _page_url(charge),
        {
            "creation_key": str(uuid.uuid4()),
            "how": "debit",
            "debit_amount": "250.00",
            "debit_on": str(timezone.localdate()),
        },
    )
    assert response.status_code == 302
    assert "charge-now" in response["Location"] or "charge_now" in response["Location"]
    assert (Job.objects.count(), fake.calls[:]) == before
