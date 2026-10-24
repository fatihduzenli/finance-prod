"""Autopay plans: one item repeated as bank debits (docs/qa/PLAN-012f-a-design-follow-up.md
D11 to D15).

A plan repeats one item of a lease (one line, never rent) as bank debits, monthly, weekly
or yearly from a first date, ending after a number of payments, on a date, or never. The
plan is local: nothing repeating is sent to the payment provider. Each period is its own
``autopay_plan`` one-off charge, made through :func:`ledger.services.one_off_charges.create_charge`
with the collection ``autopay_due``, so the worker makes one single-occurrence schedule
for it exactly as for a charge scheduled for a date. Rules kept here, not in the views:

- **Dates** (:func:`occurrence_on`, :func:`occurrence_dates`): monthly on the first date's
  day of the month, clamped to the month's last day (the 31st gives 30 April and 28 or 29
  February, then 31 March again: always counted from the first date, as installments
  are); weekly every 7 days; yearly on the same month and day, 29 February giving 28
  February in a year that has none.
- **Setup** (:func:`create`, :func:`refusal`; local data only): an active lease with its
  operator, not archived, with a provider customer and a linked bank method; one line,
  an item (not rent); a quantity and a unit amount whose product, rounded half up to the
  cent, is the debit, at most the one-off maximum and within the confirmed provider
  limit; a large debit needs the confirmation, stored on the plan
  (``large_debit_confirmed_at``); a first date after today and at most
  :data:`FIRST_HORIZON_DAYS` days ahead; an end count of 2 to
  :data:`~ledger.models.AUTOPAY_COUNT_MAX`, or an end date on or after the first date (the
  rule "never" ignores both); at most one live plan per lease and item. Audited
  ``autopay_plan.created``; the first occurrence is made at once, in the same
  transaction (a refusal of it writes nothing).
- **Occurrences** (:func:`make_due`, :func:`run_due`): an active plan's next occurrence is
  made once ``next_on - LEAD_DAYS`` is today or earlier, under the plan's row lock, with
  the creation key ``uuid5(plan, period)`` and one charge per plan and period (a unique
  index), so two runs make one. The charge is due on ``next_on`` (today, when the run
  missed that day) and reads ``<item> · <n> of <N>`` (``<item> · <n>`` without a count),
  ``n`` being ``made_count + 1`` (:func:`occurrence_text`, :func:`made_ordinal_of`);
  ``period_no`` is the key only. Then ``next_on`` moves to the next date of the series,
  ``made_count`` grows by one and the plan is ``finished`` once its end rule is met (the
  count reached; the next date after the end date). A large debit's occurrence is audited
  ``one_off_charge.large_debit_confirmed`` naming the plan's setup confirmation. An
  occurrence the one-off rules refuse (the lease ended, the bank method unlinked) is not
  made: audited once per period as :data:`OCCURRENCE_REFUSED_ACTION`, the plan unchanged.
  A worker failure on an occurrence's schedule flags that charge for review; the plan
  carries on.
- **Pause, resume, stop, change end** (D14): pause (billing) and stop (admin, final)
  cancel each pending occurrence (owed, nothing paid or pending, no invoice made by its
  schedule) through :func:`ledger.services.one_off_charges.cancel_charge` (the worker
  deletes its schedule and voids it) and give its period back (``made_count`` falls by
  one, ``next_on`` returns to it); resume (billing) makes the plan active from the first
  date of the series after today and makes the next occurrence when it is due; change end
  (admin) rewrites the end rule and finishes a plan whose new rule is already met. Each is
  audited (``autopay_plan.paused``, ``.resumed``, ``.stopped``, ``.end_changed``,
  ``.finished``).

Money is ``Decimal`` throughout; nothing here calls the payment provider.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from accounts.roles import Role, meets, role_of
from finance.dates import day_text
from ledger.models import (
    AUTOPAY_COUNT_MAX,
    AUTOPAY_COUNT_MIN,
    DESCRIPTION_MAX_LENGTH,
    LIVE_AUTOPAY_STATES,
    NAME_MAX_LENGTH,
    AuditLog,
    AutopayCadence,
    AutopayEndRule,
    AutopayPlan,
    AutopayPlanState,
    CollectionMethod,
    CollectionStatus,
    FeeType,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Payment,
    PaymentStatus,
)
from ledger.services import leases, one_off_charges
from ledger.services.installments import _add_months
from ledger.services.one_off_charges import ChargeInput
from ledger.services.rent_run import CENT
from ledger.validators import validate_no_payment_data, visible_text
from ledger.wording import format_amount

ENTITY_PLAN = "autopay_plan"
#: Days before its date that an occurrence is made. The provider debits at 12:00 UTC on
#: the due date and the daily run is at 09:00 New York, so the occurrence must exist
#: before its day; a week ahead also lets staff see it on the Autopay tab before it
#: debits. Changing the lead is this one constant (the plan's §9 item 7).
LEAD_DAYS = 7
#: The latest first date, in days after today (the schedule-for-a-date horizon).
FIRST_HORIZON_DAYS = 365
#: Most occurrences one plan makes in one run: the daily run catches up at most this many
#: missed periods of a plan (a weekly plan after a long outage), then the next day more.
CATCH_UP_MAX = 12
QUANTITY_STEP = Decimal("0.0001")
QUANTITY_MAX = Decimal("99999999.9999")

CREATED_ACTION = "autopay_plan.created"
PAUSED_ACTION = "autopay_plan.paused"
RESUMED_ACTION = "autopay_plan.resumed"
STOPPED_ACTION = "autopay_plan.stopped"
END_CHANGED_ACTION = "autopay_plan.end_changed"
FINISHED_ACTION = "autopay_plan.finished"
OCCURRENCE_REFUSED_ACTION = "autopay_plan.occurrence_refused"

# --- Fixed sentences ------------------------------------------------------------------

ONE_LINE = (
    "Set up autopay takes one line, an item (not rent or an existing charge). Nothing was set up."
)
RENT = "Rent is collected by the lease's own autopay, not repeated as an item. Nothing was set up."
NO_LEASE = "Set up autopay needs the lease of the store. Nothing was set up."
LEASE_NOT_ACTIVE = (
    "Set up autopay needs an active lease; this lease has ended or is not active. "
    "Nothing was set up."
)
NO_OPERATOR = one_off_charges.LEASE_NO_OPERATOR
OPERATOR_ARCHIVED = one_off_charges.OPERATOR_ARCHIVED
NO_PROVIDER_CUSTOMER = one_off_charges.NO_PROVIDER_CUSTOMER
NO_BANK_METHOD = one_off_charges.NO_BANK_METHOD
ITEM_REQUIRED = "An item is required."
ITEM_TOO_LONG = f"The item can be at most {NAME_MAX_LENGTH} characters."
DESCRIPTION_TOO_LONG = f"The detail can be at most {DESCRIPTION_MAX_LENGTH} characters."
TEXT_PAYMENT_DATA = one_off_charges.TEXT_PAYMENT_DATA
QUANTITY = "The quantity must be more than zero, with at most four decimals."
UNIT_AMOUNT = "The unit amount must be more than zero, in whole cents."
AMOUNT_ZERO = "The amount rounds to zero."
AMOUNT_TOO_LARGE = one_off_charges.AMOUNT_TOO_LARGE
CHOOSE_CADENCE = "Choose monthly, weekly or yearly."
FIRST_REQUIRED = "Choose the date of the first debit. Nothing was set up."
FIRST_NOT_AFTER_TODAY = "The first debit must be after today. Nothing was set up."
FIRST_TOO_FAR = (
    f"The first debit can be at most {FIRST_HORIZON_DAYS} days from today. Nothing was set up."
)
CHOOSE_END = "Choose when the debits end: after a number of payments, on a date, or never."
END_COUNT = f"Choose between {AUTOPAY_COUNT_MIN} and {AUTOPAY_COUNT_MAX} payments."
END_DATE_REQUIRED = "Choose the date of the last debit."
END_BEFORE_FIRST = "The end date cannot be before the first debit."
ALREADY_LIVE = (
    "This lease already repeats {item} as bank debits: stop that plan first. Nothing was set up."
)
CREATED = "Autopay set up: {amount} {cadence}, the first debit on {date}."
NOT_ACTIVE = "This plan is not active. Nothing was changed."
NOT_PAUSED = "This plan is not paused. Nothing was changed."
NOT_LIVE = "This plan is stopped or finished. Nothing was changed."
BILLING_ONLY = "Only billing or an admin can pause or resume a plan. Nothing was changed."
ADMIN_ONLY = "Only an admin can stop a plan or change its end. Nothing was changed."
PAUSED = "Plan paused; the next debit is cancelled."
RESUMED = "Plan resumed; the next debit is on {date}."
STOPPED = "Plan stopped; the next debit is cancelled."
END_CHANGED = "The plan's end is changed."
FINISHED = "The plan's end is changed; the plan is finished."
UNCHANGED = "Nothing was changed."
#: The note of an occurrence's charge (stored text keeps ISO dates, the plan's §9 item 1).
OCCURRENCE_NOTE = "Debit {number} of the autopay plan set up on {date}."
#: The cancel note of a pending occurrence.
PAUSE_NOTE = "Autopay plan paused."
STOP_NOTE = "Autopay plan stopped."

CADENCE_WORDS: dict[str, str] = {
    AutopayCadence.MONTHLY: "monthly",
    AutopayCadence.WEEKLY: "weekly",
    AutopayCadence.YEARLY: "yearly",
}


# --- Values and outcomes --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanInput:
    """One plan as staff entered it (the builder's line and the autopay fields)."""

    item_name: str
    description: str
    quantity: Decimal | None
    unit_amount: Decimal | None
    cadence: str
    first_on: date | None
    end_rule: str
    end_count: int | None
    end_on: date | None
    fee_type: FeeType | None = None
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    #: The form field a refusal belongs to (``None``: the whole form).
    field: str | None = None
    plan: AutopayPlan | None = None
    charge: OneOffCharge | None = None


# --- Dates (D13) ----------------------------------------------------------------------


def occurrence_on(first_on: date, cadence: str, index: int) -> date:
    """The date of occurrence ``index`` (0 is the first) of a series from ``first_on``.
    Raises ``ValueError`` on an unknown cadence."""
    if cadence == AutopayCadence.MONTHLY:
        return _add_months(first_on, index)
    if cadence == AutopayCadence.WEEKLY:
        return first_on + timedelta(days=7 * index)
    if cadence == AutopayCadence.YEARLY:
        return _add_months(first_on, 12 * index)
    raise ValueError(CHOOSE_CADENCE)


def occurrence_dates(first_on: date, cadence: str, count: int) -> list[date]:
    """The first ``count`` dates of the series."""
    return [occurrence_on(first_on, cadence, index) for index in range(count)]


def _index_near(first_on: date, cadence: str, day: date) -> int:
    """An index whose date is within one step of ``day`` (never below 0)."""
    if cadence == AutopayCadence.WEEKLY:
        index = (day - first_on).days // 7
    elif cadence == AutopayCadence.MONTHLY:
        index = (day.year - first_on.year) * 12 + day.month - first_on.month
    else:
        index = day.year - first_on.year
    return max(index - 1, 0)


def series_on_or_after(first_on: date, cadence: str, day: date) -> date:
    """The first date of the series on or after ``day``."""
    index = _index_near(first_on, cadence, day)
    while occurrence_on(first_on, cadence, index) < day:
        index += 1
    return occurrence_on(first_on, cadence, index)


def _following(plan: AutopayPlan, day: date) -> date:
    """The date of the series after ``day``."""
    return series_on_or_after(plan.first_on, plan.cadence, day + timedelta(days=1))


def end_met(plan: AutopayPlan) -> bool:
    """Whether the plan's end rule is met: the count of debits made, or the next date
    after the end date."""
    if plan.end_rule == AutopayEndRule.COUNT:
        return plan.end_count is not None and plan.made_count >= plan.end_count
    if plan.end_rule == AutopayEndRule.DATE:
        return plan.end_on is not None and plan.next_on > plan.end_on
    return False


# --- Validation (D12, D13) ------------------------------------------------------------


def amount_of(quantity: Decimal, unit_amount: Decimal) -> Decimal:
    """Each debit: the quantity times the unit amount, rounded half up to the cent (the
    builder's line total)."""
    return (quantity * unit_amount).quantize(CENT, rounding=ROUND_HALF_UP)


def _guarded(text: str) -> bool:
    try:
        validate_no_payment_data(text)
    except ValidationError:
        return False
    return True


def _is_rent(item_name: str) -> bool:
    """A line naming rent: ``Rent``, or a builder Rent line's item (``Rent <month>, ...``)."""
    words = visible_text(item_name or "").casefold().split()
    return bool(words) and words[0].rstrip(",:") == "rent"


def lease_refusal(lease: Lease | None) -> str:
    """Why ``lease`` cannot carry a plan now, or ``""``: active, not ended, with an
    operator who is not archived."""
    if lease is None:
        return NO_LEASE
    if lease.ended_on is not None or lease.collection_status != CollectionStatus.ACTIVE:
        return LEASE_NOT_ACTIVE
    if lease.operator is None:
        return NO_OPERATOR
    if lease.operator.archived_at is not None:
        return OPERATOR_ARCHIVED
    return ""


def end_refusal(
    first_on: date, end_rule: str, end_count: int | None, end_on: date | None
) -> Outcome | None:
    """The first rule an end rule breaks, or ``None``."""
    if end_rule not in AutopayEndRule.values:
        return Outcome(False, CHOOSE_END, "end_rule")
    if end_rule == AutopayEndRule.COUNT:
        if (
            not isinstance(end_count, int)
            or isinstance(end_count, bool)
            or not AUTOPAY_COUNT_MIN <= end_count <= AUTOPAY_COUNT_MAX
        ):
            return Outcome(False, END_COUNT, "end_count")
    if end_rule == AutopayEndRule.DATE:
        if not isinstance(end_on, date):
            return Outcome(False, END_DATE_REQUIRED, "end_on")
        if end_on < first_on:
            return Outcome(False, END_BEFORE_FIRST, "end_on")
    return None


def _item_refusal(item: PlanInput) -> Outcome | None:
    name = visible_text(item.item_name or "")
    if not name:
        return Outcome(False, ITEM_REQUIRED, "item")
    if len(name) > NAME_MAX_LENGTH:
        return Outcome(False, ITEM_TOO_LONG, "item")
    if not _guarded(name):
        return Outcome(False, TEXT_PAYMENT_DATA, "item")
    detail = visible_text(item.description or "")
    if len(detail) > DESCRIPTION_MAX_LENGTH:
        return Outcome(False, DESCRIPTION_TOO_LONG, "description")
    if not _guarded(detail):
        return Outcome(False, TEXT_PAYMENT_DATA, "description")
    quantity = Decimal(1) if item.quantity is None else item.quantity
    if (
        not isinstance(quantity, Decimal)
        or not quantity.is_finite()
        or quantity <= 0
        or quantity != quantity.quantize(QUANTITY_STEP)
        or quantity > QUANTITY_MAX
    ):
        return Outcome(False, QUANTITY, "quantity")
    unit = item.unit_amount
    if (
        not isinstance(unit, Decimal)
        or not unit.is_finite()
        or unit <= 0
        or unit != unit.quantize(CENT)
        or unit > one_off_charges.AMOUNT_MAX
    ):
        return Outcome(False, UNIT_AMOUNT, "unit_amount")
    amount = amount_of(quantity, unit)
    if amount <= 0:
        return Outcome(False, AMOUNT_ZERO, "quantity")
    if amount > one_off_charges.AMOUNT_MAX:
        return Outcome(False, AMOUNT_TOO_LARGE, "quantity")
    return None


def refusal(
    lease: Lease | None,
    item: PlanInput,
    *,
    today: date,
    line_count: int = 1,
    rent_line: bool = False,
) -> Outcome | None:
    """The first rule a plan breaks before anything is written, or ``None`` (local data
    only). ``line_count`` and ``rent_line`` describe the builder's lines: one item line."""
    if line_count != 1:
        return Outcome(False, ONE_LINE)
    if rent_line or _is_rent(item.item_name):
        return Outcome(False, RENT, "item")
    reason = lease_refusal(lease)
    if reason or lease is None:
        return Outcome(False, reason or NO_LEASE)
    refused = _item_refusal(item)
    if refused is not None:
        return refused
    if item.cadence not in AutopayCadence.values:
        return Outcome(False, CHOOSE_CADENCE, "cadence")
    if not isinstance(item.first_on, date):
        return Outcome(False, FIRST_REQUIRED, "first_on")
    if item.first_on <= today:
        return Outcome(False, FIRST_NOT_AFTER_TODAY, "first_on")
    if item.first_on > today + timedelta(days=FIRST_HORIZON_DAYS):
        return Outcome(False, FIRST_TOO_FAR, "first_on")
    refused = end_refusal(item.first_on, item.end_rule, item.end_count, item.end_on)
    if refused is not None:
        return refused
    operator = lease.operator
    if not leases.customer_ref(operator):
        return Outcome(False, NO_PROVIDER_CUSTOMER, "collection")
    if not leases.linked_payment_method(operator):
        return Outcome(False, NO_BANK_METHOD, "collection")
    quantity = Decimal(1) if item.quantity is None else item.quantity
    amount = amount_of(quantity, item.unit_amount or Decimal(0))
    limit = leases.provider_limit_refusal([amount])
    if limit:
        return Outcome(False, limit, "quantity")
    if leases.is_large_debit(amount) and item.confirmed is not True:
        return Outcome(False, leases.large_debit_confirmation([amount]), "confirm")
    return None


# --- Audit ------------------------------------------------------------------------------


def plan_state(plan: AutopayPlan) -> dict[str, Any]:
    """The audited fields of an autopay plan."""
    confirmed = plan.large_debit_confirmed_at
    return {
        "lease": str(plan.lease_id),
        "operator": str(plan.operator_id),
        "store": str(plan.store_id),
        "item_name": plan.item_name,
        "description": plan.description,
        "quantity": str(plan.quantity),
        "unit_amount": str(plan.unit_amount),
        "amount": str(plan.amount),
        "fee_type": str(plan.fee_type_id) if plan.fee_type_id else None,
        "cadence": plan.cadence,
        "first_on": plan.first_on.isoformat(),
        "end_rule": plan.end_rule,
        "end_count": plan.end_count,
        "end_on": plan.end_on.isoformat() if plan.end_on is not None else None,
        "state": plan.state,
        "next_on": plan.next_on.isoformat(),
        "made_count": plan.made_count,
        "large_debit_confirmed_at": confirmed.isoformat() if confirmed is not None else None,
    }


def _audit(
    actor: User | None,
    action: str,
    plan: AutopayPlan,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_PLAN,
        entity_id=str(plan.pk),
        before=before,
        after=after,
    )


# --- Setup (D12, D13) -------------------------------------------------------------------


def create(
    lease: Lease | None,
    *,
    item_name: str,
    description: str,
    quantity: Decimal | None,
    unit_amount: Decimal | None,
    cadence: str,
    first_on: date | None,
    end_rule: str,
    end_count: int | None,
    end_on: date | None,
    actor: User | None,
    confirmed: bool,
    today: date | None = None,
    fee_type: FeeType | None = None,
    line_count: int = 1,
    rent_line: bool = False,
) -> Outcome:
    """Set up a plan on ``lease`` and make its first occurrence at once (see the module
    docstring). Every rule is checked again under the lease's row lock; a refusal, the
    first occurrence's included, writes nothing."""
    day = today or timezone.localdate()
    item = PlanInput(
        item_name=item_name,
        description=description,
        quantity=quantity,
        unit_amount=unit_amount,
        cadence=cadence,
        first_on=first_on,
        end_rule=end_rule,
        end_count=end_count,
        end_on=end_on,
        fee_type=fee_type,
        confirmed=confirmed,
    )
    refused = refusal(lease, item, today=day, line_count=line_count, rent_line=rent_line)
    if refused is not None or lease is None or not isinstance(first_on, date):
        return refused or Outcome(False, NO_LEASE)
    name = visible_text(item_name)
    count = Decimal(1) if quantity is None else quantity
    unit = unit_amount or Decimal(0)
    amount = amount_of(count, unit)
    try:
        with transaction.atomic():
            locked = (
                Lease.objects.select_for_update(of=("self",))
                .select_related("operator")
                .get(pk=lease.pk)
            )
            refused = refusal(locked, item, today=day)
            if refused is not None or locked.operator is None:
                return refused or Outcome(False, NO_OPERATOR)
            if AutopayPlan.objects.filter(
                lease=locked, item_name=name, state__in=LIVE_AUTOPAY_STATES
            ).exists():
                return Outcome(False, ALREADY_LIVE.format(item=name), "item")
            plan = AutopayPlan.objects.create(
                lease=locked,
                operator=locked.operator,
                store_id=locked.store_id,
                item_name=name,
                description=visible_text(description or ""),
                quantity=count.quantize(QUANTITY_STEP),
                unit_amount=unit,
                amount=amount,
                fee_type=fee_type,
                cadence=cadence,
                first_on=first_on,
                end_rule=end_rule,
                end_count=end_count if end_rule == AutopayEndRule.COUNT else None,
                end_on=end_on if end_rule == AutopayEndRule.DATE else None,
                state=AutopayPlanState.ACTIVE,
                next_on=first_on,
                made_count=0,
                large_debit_confirmed_at=timezone.now() if leases.is_large_debit(amount) else None,
                created_by=actor,
            )
            _audit(actor, CREATED_ACTION, plan, None, plan_state(plan))
            made = _make_next(plan, day, actor)
            if not made.ok:
                transaction.set_rollback(True)
                return Outcome(False, made.message, made.field)
    except IntegrityError:
        # A concurrent setup of the same item on the lease committed first.
        return Outcome(False, ALREADY_LIVE.format(item=name), "item")
    message = CREATED.format(
        amount=format_amount(amount),
        cadence=CADENCE_WORDS[cadence],
        date=day_text(first_on),
    )
    return Outcome(True, message, plan=plan, charge=made.charge)


# --- Occurrences (D13) ------------------------------------------------------------------


def occurrence_text(plan: AutopayPlan, ordinal: int) -> str:
    """An occurrence's words: ``Sign fee · 2 of 12`` for a plan that ends after a count,
    ``Sign fee · 2`` otherwise; the item cut so the whole fits the charge's description.
    ``ordinal`` is the debit's place among those the plan made (:func:`made_ordinal_of`),
    never ``period_no``, which counts the periods a pause gave back too."""
    suffix = f" · {ordinal}"
    if plan.end_rule == AutopayEndRule.COUNT and plan.end_count is not None:
        suffix += f" of {plan.end_count}"
    return plan.item_name[: DESCRIPTION_MAX_LENGTH - len(suffix)].rstrip() + suffix


def _given_back(plan: AutopayPlan) -> set[str]:
    """The occurrences a pause or a stop cancelled and gave back (their audit rows)."""
    rows = AuditLog.objects.filter(
        entity_type=ENTITY_PLAN,
        entity_id=str(plan.pk),
        action__in=[PAUSED_ACTION, STOPPED_ACTION],
    ).values_list("after", flat=True)
    return {str(pk) for after in rows for pk in (after or {}).get("cancelled", [])}


def made_ordinal_of(plan: AutopayPlan, charge: OneOffCharge) -> int:
    """The place of ``charge`` among the debits the plan made, as when it was made
    (``made_count + 1`` then): one more than the plan's earlier periods that were not
    given back. A pause gives back only occurrences still pending, all made before it,
    so a later occurrence's ordinal never counts them."""
    back = _given_back(plan)
    earlier = OneOffCharge.objects.filter(
        autopay_plan=plan, period_no__lt=charge.period_no
    ).values_list("pk", flat=True)
    return 1 + sum(1 for pk in earlier if str(pk) not in back)


def _next_period(plan: AutopayPlan) -> int:
    last = OneOffCharge.objects.filter(autopay_plan=plan).aggregate(last=Max("period_no"))["last"]
    return (last or 0) + 1


def _make_next(plan: AutopayPlan, today: date, actor: User | None) -> Outcome:
    """Make the plan's next occurrence and move the plan on. ``plan`` is locked by the
    caller's transaction."""
    period = _next_period(plan)
    item = ChargeInput(
        fee_type=None,
        price=plan.amount,
        cost=None,
        vendor="",
        due_date=max(plan.next_on, today),
        note=OCCURRENCE_NOTE.format(number=period, date=plan.created_at.date().isoformat()),
        category=OneOffCategory.AUTOPAY_PLAN,
        collection=CollectionMethod.AUTOPAY_DUE,
        description=occurrence_text(plan, plan.made_count + 1),
        confirmed=plan.large_debit_confirmed_at is not None,
        creation_key=uuid.uuid5(plan.pk, str(period)),
        autopay_plan=plan,
        period_no=period,
    )
    made = one_off_charges.create_charge(plan.operator, plan.lease, item, actor, today=today)
    if not made.ok or made.charge is None:
        return Outcome(False, made.message, made.field, plan=plan)
    before = plan_state(plan)
    plan.made_count += 1
    plan.next_on = _following(plan, plan.next_on)
    fields = ["made_count", "next_on", "updated_at"]
    finished = end_met(plan)
    if finished:
        plan.state = AutopayPlanState.FINISHED
        fields.append("state")
    plan.save(update_fields=fields)
    if finished:
        _audit(actor, FINISHED_ACTION, plan, before, plan_state(plan))
    return Outcome(True, made.message, plan=plan, charge=made.charge)


def _record_refusal(plan: AutopayPlan, outcome: Outcome, actor: User | None) -> None:
    """Audit an occurrence the one-off rules refused, once per plan and period."""
    period = _next_period(plan)
    if AuditLog.objects.filter(
        action=OCCURRENCE_REFUSED_ACTION,
        entity_type=ENTITY_PLAN,
        entity_id=str(plan.pk),
        after__period_no=period,
    ).exists():
        return
    _audit(
        actor,
        OCCURRENCE_REFUSED_ACTION,
        plan,
        None,
        {**plan_state(plan), "period_no": period, "reason": str(outcome.message)},
    )


def make_due(plan: AutopayPlan, today: date, actor: User | None = None) -> OneOffCharge | None:
    """Make the plan's next occurrence when it is due (``next_on - LEAD_DAYS`` is
    ``today`` or earlier) and the plan is active; ``None`` otherwise, or when the one-off
    rules refuse it (audited, the plan unchanged). Under the plan's row lock: two calls
    make one occurrence."""
    with transaction.atomic():
        locked = (
            AutopayPlan.objects.select_for_update(of=("self",))
            .select_related("lease", "operator")
            .get(pk=plan.pk)
        )
        if locked.state != AutopayPlanState.ACTIVE:
            return None
        if locked.next_on - timedelta(days=LEAD_DAYS) > today:
            return None
        made = _make_next(locked, today, actor)
        if not made.ok:
            _record_refusal(locked, made, actor)
            return None
    return made.charge


def run_due(today: date | None = None) -> list[OneOffCharge]:
    """The daily run's step (D13): every active plan's due occurrences, each plan in its
    own transaction, at most :data:`CATCH_UP_MAX` per plan."""
    day = today or timezone.localdate()
    due = (
        AutopayPlan.objects.filter(
            state=AutopayPlanState.ACTIVE, next_on__lte=day + timedelta(days=LEAD_DAYS)
        )
        .order_by("next_on", "created_at", "id")
        .values_list("pk", flat=True)
    )
    made: list[OneOffCharge] = []
    for pk in list(due):
        plan = AutopayPlan(pk=pk)
        for _ in range(CATCH_UP_MAX):
            charge = make_due(plan, day)
            if charge is None:
                break
            made.append(charge)
    return made


def made_on(day: date) -> list[OneOffCharge]:
    """The occurrences made on ``day`` (New York), oldest first: the run page's lines."""
    return list(
        OneOffCharge.objects.filter(autopay_plan__isnull=False, created_at__date=day)
        .select_related("autopay_plan", "lease__store")
        .order_by("created_at", "id")
    )


# --- Pause, resume, stop, change end (D14) ----------------------------------------------


def _is_billing(actor: User | None) -> bool:
    return actor is not None and meets(role_of(actor), Role.BILLING)


def _is_admin(actor: User | None) -> bool:
    return actor is not None and meets(role_of(actor), Role.ADMIN)


def _locked(plan: AutopayPlan) -> AutopayPlan:
    return (
        AutopayPlan.objects.select_for_update(of=("self",))
        .select_related("lease", "operator")
        .get(pk=plan.pk)
    )


def pending_occurrences(plan: AutopayPlan) -> list[OneOffCharge]:
    """The plan's occurrences not debited yet: owed, nothing paid or pending, no invoice
    made by their schedule, and no cancel requested."""
    rows = OneOffCharge.objects.filter(
        autopay_plan=plan, status=OneOffChargeStatus.OWED, amount_paid=0
    ).order_by("period_no")
    found = []
    for row in rows:
        if one_off_charges.invoices_of(row).exists():
            continue
        if Payment.objects.filter(one_off_charge=row, status=PaymentStatus.PENDING).exists():
            continue
        if AuditLog.objects.filter(
            action=one_off_charges.CANCEL_ACTION,
            entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
            entity_id=str(row.pk),
        ).exists():
            continue
        found.append(row)
    return found


def _cancel_pending(
    plan: AutopayPlan, note: str, actor: User | None
) -> tuple[list[OneOffCharge], Outcome | None]:
    """Cancel the plan's pending occurrences and give their periods back. Runs in the
    caller's transaction; on a refusal the caller rolls it back."""
    pending = pending_occurrences(plan)
    for row in pending:
        cancelled = one_off_charges.cancel_charge(row.pk, note=note, actor=actor)
        if not cancelled.ok:
            return pending, Outcome(False, cancelled.message, plan=plan)
    if pending:
        plan.made_count = max(plan.made_count - len(pending), 0)
        plan.next_on = min(plan.next_on, *(row.due_date for row in pending))
    return pending, None


def _stopping(
    plan: AutopayPlan,
    actor: User | None,
    *,
    state: str,
    action: str,
    note: str,
    message: str,
) -> Outcome:
    with transaction.atomic():
        locked = _locked(plan)
        if state == AutopayPlanState.PAUSED and locked.state != AutopayPlanState.ACTIVE:
            return Outcome(False, NOT_ACTIVE, plan=locked)
        if locked.state not in LIVE_AUTOPAY_STATES:
            return Outcome(False, NOT_LIVE, plan=locked)
        before = plan_state(locked)
        pending, refused = _cancel_pending(locked, note, actor)
        if refused is not None:
            transaction.set_rollback(True)
            return refused
        locked.state = state
        locked.save(update_fields=["state", "made_count", "next_on", "updated_at"])
        _audit(
            actor,
            action,
            locked,
            before,
            {**plan_state(locked), "cancelled": [str(row.pk) for row in pending]},
        )
    return Outcome(True, message, plan=locked)


def pause(plan: AutopayPlan, actor: User | None) -> Outcome:
    """Pause an active plan (billing): its pending occurrences are cancelled."""
    if not _is_billing(actor):
        return Outcome(False, BILLING_ONLY)
    return _stopping(
        plan,
        actor,
        state=AutopayPlanState.PAUSED,
        action=PAUSED_ACTION,
        note=PAUSE_NOTE,
        message=PAUSED,
    )


def stop(plan: AutopayPlan, actor: User | None) -> Outcome:
    """Stop an active or paused plan for good (admin): its pending occurrences are
    cancelled."""
    if not _is_admin(actor):
        return Outcome(False, ADMIN_ONLY)
    return _stopping(
        plan,
        actor,
        state=AutopayPlanState.STOPPED,
        action=STOPPED_ACTION,
        note=STOP_NOTE,
        message=STOPPED,
    )


def resume(plan: AutopayPlan, actor: User | None, *, today: date | None = None) -> Outcome:
    """Resume a paused plan (billing) from the first date of its series after today, and
    make the next occurrence when it is due; a plan whose end rule is met by then is
    finished instead."""
    if not _is_billing(actor):
        return Outcome(False, BILLING_ONLY)
    day = today or timezone.localdate()
    with transaction.atomic():
        locked = _locked(plan)
        if locked.state != AutopayPlanState.PAUSED:
            return Outcome(False, NOT_PAUSED, plan=locked)
        reason = lease_refusal(locked.lease)
        if reason:
            return Outcome(False, reason, plan=locked)
        before = plan_state(locked)
        locked.next_on = series_on_or_after(
            locked.first_on, locked.cadence, max(locked.next_on, day + timedelta(days=1))
        )
        if end_met(locked):
            locked.state = AutopayPlanState.FINISHED
            locked.save(update_fields=["state", "next_on", "updated_at"])
            _audit(actor, FINISHED_ACTION, locked, before, plan_state(locked))
            return Outcome(True, FINISHED, plan=locked)
        locked.state = AutopayPlanState.ACTIVE
        locked.save(update_fields=["state", "next_on", "updated_at"])
        _audit(actor, RESUMED_ACTION, locked, before, plan_state(locked))
        next_on = locked.next_on
        charge = make_due(locked, day, actor)
    return Outcome(True, RESUMED.format(date=day_text(next_on)), plan=locked, charge=charge)


def change_end(
    plan: AutopayPlan,
    *,
    end_rule: str,
    end_count: int | None,
    end_on: date | None,
    actor: User | None,
) -> Outcome:
    """Rewrite the end rule of an active or paused plan (admin); a plan whose new rule is
    already met is finished at once (its pending occurrence, already made, stays)."""
    if not _is_admin(actor):
        return Outcome(False, ADMIN_ONLY)
    with transaction.atomic():
        locked = _locked(plan)
        if locked.state not in LIVE_AUTOPAY_STATES:
            return Outcome(False, NOT_LIVE, plan=locked)
        refused = end_refusal(locked.first_on, end_rule, end_count, end_on)
        if refused is not None:
            return Outcome(False, refused.message, refused.field, plan=locked)
        new_count = end_count if end_rule == AutopayEndRule.COUNT else None
        new_on = end_on if end_rule == AutopayEndRule.DATE else None
        if (locked.end_rule, locked.end_count, locked.end_on) == (end_rule, new_count, new_on):
            return Outcome(True, UNCHANGED, plan=locked)
        before = plan_state(locked)
        locked.end_rule = end_rule
        locked.end_count = new_count
        locked.end_on = new_on
        fields = ["end_rule", "end_count", "end_on", "updated_at"]
        _audit(actor, END_CHANGED_ACTION, locked, before, plan_state(locked))
        if not end_met(locked):
            locked.save(update_fields=fields)
            return Outcome(True, END_CHANGED, plan=locked)
        changed = plan_state(locked)
        locked.state = AutopayPlanState.FINISHED
        locked.save(update_fields=[*fields, "state"])
        _audit(actor, FINISHED_ACTION, locked, changed, plan_state(locked))
    return Outcome(True, FINISHED, plan=locked)


def live_plans(lease: Lease) -> list[AutopayPlan]:
    """The lease's plans that are active or paused, oldest first (the lease-end and
    transfer blockers)."""
    return list(
        AutopayPlan.objects.filter(lease=lease, state__in=LIVE_AUTOPAY_STATES).order_by(
            "created_at", "id"
        )
    )
