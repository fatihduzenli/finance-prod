"""The old ladder retired for good, the Outbox's "Preview next 30 days" and the store on every
rule entry (owner, 2026-11-01), Opus's tests.

1. Nothing queues the retired ladder or an inactive template: ``messaging.queue`` and
   ``queue_portal`` refuse with ``ValueError`` and write nothing; no source outside the
   models, migrations and tests names the ladder's rule or its three templates; the ladder's
   past rows still list on the Outbox, with no store.
2. The preview: built on the reminder job's own planner, so its "would queue" rows are what
   ``queue_due`` queues day by day; read only (rolled back even when a helper writes); each
   message once; rule F's re-debits listed with no F row invented; admin only, GET; a pinned
   query budget that does not grow with the rows; the ``outbox_preview`` command's text, with
   no address.
3. A message of each rule A to F shows its store on the Outbox; "—" is left to the ladder's and
   the portal's rows.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Lease,
    MessageRule,
    MessageTemplate,
    OneOffCharge,
    Operator,
    OutboundMessage,
    Payment,
)
from ledger.services import messaging, outbox_preview, reminders
from ledger.services.messaging import RentMonth
from payments.models import Job, JobKind
from tests.test_collections import at, billed, operator, rent_charge
from tests.test_collections import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_ledger_models import (
    new_lease,
    new_one_off_charge,
    new_outbound_message,
    new_payment,
    new_promise,
    new_rent_charge,
    new_rent_history,
    new_schedule,
    new_store,
    saved,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 11, 2)
LAST = TODAY + timedelta(days=29)
PREVIEW = "/admin-settings/outbox/preview/"
OUTBOX = "/admin-settings/outbox/"
BACKEND = Path(__file__).resolve().parents[1]
#: A start after the window's last rent debit (LAST + reminders.DAYS_BEFORE): a lease with no
#: rent month of its own in the preview.
AFTER_THE_WINDOW = date(2027, 1, 1)


def written() -> tuple[int, int, int]:
    return OutboundMessage.objects.count(), Job.objects.count(), AuditLog.objects.count()


def template(code: str) -> MessageTemplate:
    return MessageTemplate.objects.get(code=code)


def store_cell(page: str, message: OutboundMessage) -> str:
    """The Store cell (the row's second) of ``message`` on the Outbox page."""
    row = page[page.index(f'id="message-{message.pk}"') :]
    row = row[: row.index("</tr>")]
    cells: list[str] = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
    return cells[1]


# --- 1. The old ladder is retired for good ----------------------------------------------------


@pytest.mark.parametrize("code", ["friendly_reminder", "overdue_day5"])
def test_queueing_the_ladder_rule_is_refused_and_writes_nothing(clock: Any, code: str) -> None:
    charge = rent_charge()
    clock(at(TODAY))
    # Even with an active template: the rule itself is retired.
    MessageTemplate.objects.filter(code=code).update(active=True)
    context = messaging.context_for(charge.lease, charge, MessageRule.C, today=TODAY)
    before = written()
    with pytest.raises(ValueError, match="retired"):
        messaging.queue(MessageRule.LADDER, charge.lease, charge, template(code), context)
    assert written() == before


@pytest.mark.parametrize("code", ["friendly_reminder", "formal_notice", "final_notice"])
def test_the_ladders_templates_are_inactive_and_refused_under_any_rule(
    clock: Any, code: str
) -> None:
    charge = rent_charge()
    clock(at(TODAY))
    found = template(code)
    assert found.active is False
    before = written()
    with pytest.raises(ValueError, match="inactive"):
        messaging.queue(MessageRule.C, charge.lease, charge, found, {})
    assert written() == before


def test_an_inactive_template_is_refused_by_queue_and_by_queue_portal(clock: Any) -> None:
    charge = rent_charge()
    clock(at(TODAY))
    MessageTemplate.objects.filter(code__in=("overdue_day5", "portal_invite")).update(active=False)
    context = messaging.context_for(charge.lease, charge, MessageRule.C, today=TODAY)
    owner = charge.lease.operator
    assert owner is not None
    before = written()
    with pytest.raises(ValueError, match="inactive"):
        messaging.queue(MessageRule.C, charge.lease, charge, template("overdue_day5"), context)
    with pytest.raises(ValueError, match="inactive"):
        messaging.queue_portal(template("portal_invite"), uuid.uuid4(), owner, "a@example.com", {})
    assert written() == before


def test_the_job_skips_a_rule_whose_template_was_made_inactive(clock: Any) -> None:
    charge = rent_charge(due_on=TODAY - timedelta(days=5))
    MessageTemplate.objects.filter(code="overdue_day5").update(active=False)
    clock(at(TODAY))
    [item] = [item for item in reminders.plan(TODAY) if item.rule.code == MessageRule.C]
    assert item.outcome == reminders.NO_TEMPLATE
    summary = reminders.queue_due(TODAY)
    assert (summary.queued, summary.skipped) == (0, 1)
    assert not OutboundMessage.objects.filter(charge_entity_id=charge.pk).exists()


def test_no_source_outside_models_migrations_and_tests_names_the_ladder() -> None:
    names = ("MessageRule.LADDER", "friendly_reminder", "formal_notice", "final_notice")
    found = []
    for path in BACKEND.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".html", ".txt", ".js", ".json"}:
            continue
        parts = path.relative_to(BACKEND).parts
        if parts[0] == "tests" or "migrations" in parts or path == BACKEND / "ledger/models.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        found += [f"{path.relative_to(BACKEND)}: {name}" for name in names if name in text]
    assert found == []
    # The scan sees the sources it guards (a wrong root would find nothing anywhere).
    assert "MessageRule.LADDER" in (BACKEND / "ledger/models.py").read_text(encoding="utf-8")
    assert (BACKEND / "ledger/services/messaging.py").is_file()


def test_the_ladders_past_rows_still_list_on_the_outbox_with_no_store() -> None:
    old = saved(
        new_outbound_message(
            rule=MessageRule.LADDER,
            kind="ladder",
            template=template("formal_notice"),
            occurrence=10,
            status="held",
            held_reason="not_set_up",
        )
    )
    admin, _ = client_for(Role.ADMIN)
    page = admin.get(OUTBOX).content.decode()
    assert f'id="message-{old.pk}"' in page
    assert store_cell(page, old) == "—"
    row = page[page.index(f'id="message-{old.pk}"') :]
    assert "Old collections ladder" in row[: row.index("</tr>")]


# --- 3. Every rule A to F shows its store ------------------------------------------------------


def _queued(rule: str, code: str, charge: Any, **kwargs: Any) -> OutboundMessage:
    lease = charge.lease
    found = messaging.queue(
        rule,
        lease,
        charge,
        template(code),
        messaging.context_for(lease, charge, rule, today=TODAY, **kwargs),
    )
    assert found is not None
    return found


def test_a_message_of_each_rule_a_to_f_shows_its_store_on_the_outbox(clock: Any) -> None:
    clock(at(TODAY))
    charge = rent_charge(due_on=TODAY - timedelta(days=5))
    lease = charge.lease
    month = RentMonth(lease, date(2026, 12, 1), date(2026, 12, 5), D("2450.00"))
    rows = [
        _queued(MessageRule.A, "reminder_autopay", month),
        _queued(MessageRule.B, "reminder_due", charge),
        _queued(MessageRule.C, "overdue_day5", charge),
        _queued(MessageRule.D, "overdue_day10", charge),
        _queued(MessageRule.E, "internal_day30", charge),
        _queued(MessageRule.F, "returned_debit", charge, retry_date=TODAY + timedelta(days=3)),
    ]
    admin, _ = client_for(Role.ADMIN)
    page = admin.get(OUTBOX).content.decode()
    store = lease.store
    for message in rows:
        cell = store_cell(page, message)
        assert f'href="/stores/{store.pk}/"' in cell, message.rule
        assert f"#{store.store_number} {store.external_label}" in cell, message.rule
        assert cell != "—"


@pytest.mark.parametrize("rule", ["A", "B", "C", "D", "E", "F"])
def test_a_rule_message_without_a_lease_is_refused(clock: Any, rule: str) -> None:
    clock(at(TODAY))
    charge = rent_charge()
    code = {**reminders.TEMPLATES, "F": "returned_debit"}[rule]
    before = written()
    with pytest.raises(ValueError, match="lease"):
        messaging.queue(rule, None, charge, template(code), {})
    assert written() == before


# --- 2. The preview -----------------------------------------------------------------------------


def _leased(owner: Operator, number: int, *, email: str | None, **fields: Any) -> Lease:
    store = saved(new_store(store_number=number, external_label=f"Store {number} Elm"))
    lease = saved(new_lease(store=store, operator=owner, **fields))
    saved(new_rent_history(lease=lease, amount=D("1200.00")))
    return billed(lease, email)


def world() -> dict[str, Any]:
    """Two weeks of the rules: a B month (no autopay), an A month (autopay), a late one-off
    charge reaching C, D and E, and one on a lease without a billing e-mail whose B a promise
    pauses (its C goes once the promise is broken)."""
    first = operator("first-op@example.com", display_name="First Op")
    second = operator("second-op@example.com", display_name="Second Op")
    third = operator("third-op@example.com", display_name="Third Op")
    plain = _leased(first, 101, email="first-billing@example.com", debit_day=10)
    autopay = _leased(second, 102, email="second-billing@example.com", debit_day=15)
    saved(new_schedule(lease=autopay, debit_day=15, amount=D("1300.00")))
    # Re-pinned (Opus, PLAN-015b §3 B: a paused lease's charges are skipped, so it no longer
    # stands in): active, starting after the window, so no rent month of its own (A/B) in it,
    # its one-off charge only (B to E, a promise pausing its B).
    silent = _leased(third, 103, email=None, debit_day=1, started_on=AFTER_THE_WINDOW)
    late = saved(
        new_one_off_charge(
            operator=first, lease=plain, due_date=TODAY - timedelta(days=5), price=D("80.00")
        )
    )
    soon = saved(
        new_one_off_charge(
            operator=third, lease=silent, due_date=date(2026, 11, 20), price=D("45.00")
        )
    )
    saved(new_promise(operator=third, lease=silent, promised_date=date(2026, 11, 18)))
    return {"plain": plain, "autopay": autopay, "silent": silent, "late": late, "soon": soon}


def test_the_preview_is_exactly_what_the_job_queues_day_by_day(clock: Any) -> None:
    world()
    clock(at(TODAY))
    found = outbox_preview.preview(TODAY)
    planned = {(row.day, row.rule, row.store) for row in found.rows if row.queues}
    paused = {(row.day, row.rule, row.store) for row in found.rows if not row.queues}
    assert len(planned) >= 6 and paused
    queued: set[tuple[date, str, str]] = set()
    for offset in range(30):
        day = TODAY + timedelta(days=offset)
        clock(at(day))
        before = set(OutboundMessage.objects.values_list("pk", flat=True))
        summary = reminders.queue_due(day)
        new = list(OutboundMessage.objects.exclude(pk__in=before).select_related("lease__store"))
        queued |= {
            (
                day,
                message.rule,
                f"#{message.lease.store.store_number} {message.lease.store.external_label}",
            )
            for message in new
            if message.lease is not None
        }
        assert summary.queued == len(new)
        assert summary.paused == sum(
            1 for row in found.rows if row.day == day and row.outcome == "Paused by a promise"
        )
    assert queued == planned


def test_the_preview_lists_each_rule_with_its_store_operator_charge_amount_and_outcome(
    clock: Any,
) -> None:
    things = world()
    clock(at(TODAY))
    rows = {(row.rule, row.store): row for row in outbox_preview.preview(TODAY).rows}
    plain_b = rows[("B", "#101 Store 101 Elm")]
    # Re-pinned (Opus, owner 2026-11-02): on Nov 7 the lease's B goes with the late charge's D,
    # as one message.
    assert (plain_b.charge, plain_b.amount, plain_b.outcome) == (
        "Rent November 2026",
        D("1200.00"),
        "Would send combined",
    )
    assert plain_b.day == date(2026, 11, 7) and plain_b.operator == "First Op"
    auto_a = rows[("A", "#102 Store 102 Elm")]
    assert (auto_a.charge, auto_a.amount, auto_a.day) == (
        "Rent November 2026",
        D("1300.00"),
        date(2026, 11, 12),
    )
    assert auto_a.template == "A — Rent will be debited"
    late_c = rows[("C", "#101 Store 101 Elm")]
    assert (late_c.day, late_c.charge, late_c.amount) == (TODAY, "Broken shelf", D("80.00"))
    assert rows[("E", "#101 Store 101 Elm")].day == date(2026, 11, 27)
    silent_b = rows[("B", "#103 Store 103 Elm")]
    assert (silent_b.day, silent_b.outcome) == (date(2026, 11, 17), "Paused by a promise")
    silent_c = rows[("C", "#103 Store 103 Elm")]
    assert silent_c.outcome == "Would queue (held: no billing e-mail)"
    assert silent_c.store_id == things["silent"].store_id


def test_already_queued_and_settled_items_are_skipped_with_why(clock: Any) -> None:
    things = world()
    clock(at(TODAY))
    reminders.queue_due(TODAY)  # today's C of the late charge
    rows = outbox_preview.preview(TODAY).rows
    [late_c] = [row for row in rows if row.rule == "C" and row.store.startswith("#101")]
    assert late_c.outcome == "Skipped: already queued" and not late_c.queues
    # A month whose rent was paid ahead gets nothing.
    plain = things["plain"]
    saved(
        new_rent_charge(
            lease=plain,
            period_month=date(2026, 11, 1),
            due_on=date(2026, 11, 10),
            amount_due=D("1200.00"),
            amount_paid=D("1200.00"),
            status="paid",
        )
    )
    [plain_b] = [
        row
        for row in outbox_preview.preview(TODAY).rows
        if row.rule == "B" and row.store.startswith("#101")
    ]
    assert plain_b.outcome == "Skipped: the month is settled"


def test_the_preview_writes_nothing_and_rolls_back_a_helper_that_writes(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world()
    clock(at(TODAY))
    before = written()
    outbox_preview.preview(TODAY)
    assert written() == before
    original = reminders.plan

    def writing_plan(today: date, **kwargs: Any) -> list[reminders.Planned]:
        AuditLog.objects.create(action="test.write", entity_type="test", entity_id="x")
        return original(today, **kwargs)

    monkeypatch.setattr(reminders, "plan", writing_plan)
    found = outbox_preview.preview(TODAY)
    assert found.rows
    assert written() == before
    assert not AuditLog.objects.filter(action="test.write").exists()


def test_an_item_the_planner_finds_on_two_days_is_listed_once(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world()
    clock(at(TODAY))
    first_day = [item for item in reminders.plan(TODAY) if item.queues]
    assert first_day
    monkeypatch.setattr(reminders, "plan", lambda today, **kwargs: first_day)
    rows = outbox_preview.preview(TODAY).rows
    assert len(rows) == len(first_day)


def test_rule_f_is_not_invented_and_the_window_s_redebits_are_listed(clock: Any) -> None:
    things = world()
    plain = things["plain"]
    payment = saved(new_payment(status="returned"))
    redebit = saved(
        new_one_off_charge(
            operator=plain.operator,
            lease=plain,
            category="redebit",
            collection="autopay_due",
            description="Re-debit of Rent October 2026",
            price=D("1200.00"),
            due_date=date(2026, 11, 6),
            source_payment=payment,
            retry_date=date(2026, 11, 6),
            retry_state="auto_pending",
        )
    )
    saved(
        new_one_off_charge(
            operator=plain.operator,
            lease=plain,
            category="redebit",
            description="Re-debit after the window",
            due_date=LAST + timedelta(days=1),
            source_payment=saved(new_payment(status="returned")),
            retry_date=LAST + timedelta(days=1),
            retry_state="auto_pending",
        )
    )
    clock(at(TODAY))
    found = outbox_preview.preview(TODAY)
    assert [row.rule for row in found.rows if row.rule == "F"] == []
    [listed] = found.redebits
    assert (listed.day, listed.charge, listed.amount) == (
        date(2026, 11, 6),
        redebit.description,
        D("1200.00"),
    )
    admin, _ = client_for(Role.ADMIN)
    page = admin.get(PREVIEW).content.decode()
    assert "F follows a return; not predictable." in page
    assert "Re-debit of Rent October 2026" in page
    table = page[page.index('id="outbox-preview-redebits"') :]
    assert "Re-debit of Rent October 2026" in table
    # Due after the window, its B falls in it (an open charge): a reminder, not a re-debit.
    assert "Re-debit after the window" not in table
    assert Payment.objects.count() == 2


def test_the_page_is_admin_only_get_only_linked_from_the_outbox_and_shows_no_address(
    clock: Any,
) -> None:
    things = world()
    clock(at(TODAY))
    for role in (Role.VIEWER, Role.BILLING):
        client, _ = client_for(role)
        assert client.get(PREVIEW).status_code == 403
    admin, _ = client_for(Role.ADMIN)
    assert admin.post(PREVIEW).status_code in (403, 405)
    assert 'href="/admin-settings/outbox/preview/"' in admin.get(OUTBOX).content.decode()
    response = admin.get(PREVIEW)
    assert response.status_code == 200
    page = response.content.decode()
    assert "Preview next 30 days" in page
    assert outbox_preview.RENT_BEFORE_ITS_CHARGE in page
    assert f'href="/stores/{things["plain"].store_id}/"' in page
    assert "Paused by a promise" in page and "Would queue" in page
    assert "@example.com" not in page.replace(str(response.wsgi_request.user.get_username()), "")
    assert written()[0] == 0


def test_the_pages_queries_do_not_grow_with_its_rows(clock: Any) -> None:
    def leases_with_a_late_charge(count: int) -> None:
        for _ in range(count):
            owner = operator()
            # Re-pinned (Opus, PLAN-015b §3 B: a paused lease's charges are skipped): active,
            # starting after the window, so no rent month (its rule B reads the lease's rent,
            # the planner's own query per lease, rent_run.first_debit_amount); charges only.
            number = 500 + Operator.objects.count()
            lease = _leased(
                owner, number, email=owner.email, debit_day=1, started_on=AFTER_THE_WINDOW
            )
            saved(new_one_off_charge(operator=owner, lease=lease, due_date=TODAY))

    clock(at(TODAY))
    admin, _ = client_for(Role.ADMIN)
    admin.get(PREVIEW)  # the session and the company settings, read once
    leases_with_a_late_charge(1)
    with CaptureQueriesContext(connection) as one:
        assert admin.get(PREVIEW).status_code == 200
    leases_with_a_late_charge(3)
    with CaptureQueriesContext(connection) as four:
        page = admin.get(PREVIEW).content.decode()
    assert page.count("Would queue") == 4 * 2  # C and D of each charge (E is day 30: after)
    assert len(four) == len(one)
    # The budget: the planner's own reads for each of the 30 days (fewer on a day with no
    # rule) and the page's; nothing per row. 174 on sqlite on 2026-11-01.
    assert len(one) <= 180


def test_previewing_28_days_reads_the_dated_deposits_once(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fable's review of PLAN-015d: the dated deposits do not depend on the day, so the
    preview reads them once for its window; the daily job's plan reads them once too."""
    from ledger.services import receivables, reminders

    world()
    clock(at(TODAY))
    reads: list[object] = []
    original = receivables.dated_deposits

    def counted(*args: Any, **kwargs: Any) -> Any:
        reads.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(receivables, "dated_deposits", counted)
    outbox_preview.preview(TODAY, days=28)
    assert len(reads) == 1
    reminders.plan(TODAY)
    assert len(reads) == 2


def test_the_command_prints_the_same_rows_as_tab_separated_text_without_addresses(
    clock: Any,
) -> None:
    world()
    clock(at(TODAY))
    found = outbox_preview.preview(TODAY)
    out = StringIO()
    call_command("outbox_preview", "--days", "30", stdout=out)
    lines = out.getvalue().splitlines()
    assert lines[0].split("\t") == [
        "day",
        "rule",
        "template",
        "store",
        "operator",
        "charge",
        "amount",
        "outcome",
        "group",  # Re-pinned (Opus, owner 2026-11-02): the send each row goes in.
    ]
    assert len(lines) == 1 + len(found.rows) + len(found.redebits)
    assert all(len(line.split("\t")) == 9 for line in lines)
    assert "2026-11-07\tB\tB — Payment due\t#101 Store 101 Elm\tFirst Op\tRent November 2026\t" in (
        out.getvalue()
    )
    assert "@" not in out.getvalue()
    assert written()[0] == 0
    short = StringIO()
    call_command("outbox_preview", "--days", "1", stdout=short)
    assert all(line.startswith(("day\t", "2026-11-02\t")) for line in short.getvalue().splitlines())
    for days in ("0", "91"):
        with pytest.raises(CommandError):
            call_command("outbox_preview", "--days", days, stdout=StringIO())


def test_the_job_queues_only_what_the_planner_would_queue(clock: Any) -> None:
    world()
    clock(at(date(2026, 11, 17)))
    items = reminders.plan(date(2026, 11, 17))
    assert {item.outcome for item in items} >= {reminders.PAUSED}
    summary = reminders.queue_due(date(2026, 11, 17))
    assert summary.queued == sum(item.queues for item in items)
    assert summary.paused == sum(item.outcome == reminders.PAUSED for item in items)
    jobs = Job.objects.filter(kind=JobKind.MESSAGE_SEND).count()
    assert jobs == summary.queued
    assert OneOffCharge.objects.count() == 2


def test_a_paused_lease_is_listed_skipped_queues_nothing_and_counts_as_skipped(
    clock: Any,
) -> None:
    """PLAN-015b §3 B: the open charge of a lease whose collection is paused is listed with
    "Skipped: collection is paused" (outcome ``collection_paused``), never "Paused by a
    promise"; the job queues nothing for it and counts it skipped, not paused."""
    world()
    owner = operator("paused-op@example.com", display_name="Paused Op")
    lease = _leased(
        owner, 104, email="paused-billing@example.com", debit_day=1, collection_status="paused"
    )
    # Five days late today: its C is due today, its D and E later in the window.
    saved(
        new_one_off_charge(
            operator=owner, lease=lease, due_date=TODAY - timedelta(days=5), price=D("60.00")
        )
    )
    clock(at(TODAY))
    found = outbox_preview.preview(TODAY)
    mine = [row for row in found.rows if row.store == "#104 Store 104 Elm"]
    assert mine
    assert {row.outcome for row in mine} == {"Skipped: collection is paused"}
    assert not any(row.queues for row in mine)
    [item] = [
        item
        for item in reminders.plan(TODAY)
        if item.lease is not None and item.lease.pk == lease.pk
    ]
    assert (item.outcome, item.outcome_text) == (
        reminders.COLLECTION_PAUSED,
        "Skipped: collection is paused",
    )
    items = reminders.plan(TODAY)
    summary = reminders.queue_due(TODAY)
    assert summary.paused == sum(entry.outcome == reminders.PAUSED for entry in items)
    assert summary.skipped == sum(
        not entry.queues and entry.outcome != reminders.PAUSED for entry in items
    )
    # The paused lease's C is today's one collection_paused item, counted in skipped.
    assert [entry.lease for entry in items if entry.outcome == reminders.COLLECTION_PAUSED] == [
        lease
    ]
    assert not OutboundMessage.objects.filter(lease=lease).exists()
    admin, _ = client_for(Role.ADMIN)
    page = admin.get(PREVIEW).content.decode()
    assert "Skipped: collection is paused" in page
