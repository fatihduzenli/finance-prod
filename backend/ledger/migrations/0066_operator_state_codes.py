"""Operator state codes (PLAN-014 I-b, the owner's checkpoint 1 review, fix 4).

The operator form's State becomes the store form's select of the 50 states and DC, so each
operator's free-text ``state`` becomes its two-letter code, as ``0051`` did for the stores:
the value, stripped, matched without regard to case on a code or a full name of
:data:`finance.us_states.US_STATES`; anything else becomes "" and the operator is flagged
``needs_review`` (the operator has no review-reason column: the list's "Needs review" badge),
with the raw value kept in an ``audit_log`` row (``operator.state_unmapped``, before
``{"state": raw}``).

The reverse writes the full names back from the codes; a value that was unmapped stays ""
(its raw value is in the audit row) and its ``needs_review`` flag is left as it is (the flag
also marks a provider duplicate, PLAN-006 D3, which this migration cannot tell apart). No
column is added, so no payment-data trigger changes (an empty ``GUARDED_COLUMNS``).
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from finance.us_states import US_STATES

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
UNMAPPED_ACTION = "operator.state_unmapped"
_BY_KEY: dict[str, str] = {
    **{code.casefold(): code for code, _ in US_STATES},
    **{name.casefold(): code for code, name in US_STATES},
}
_NAMES: dict[str, str] = dict(US_STATES)


def state_code(raw: str) -> str:
    """The two-letter code of a free-text state ("Texas", "tx", " TX " are "TX"); "" when
    it matches no code and no full name (the rule of ``0051``)."""
    return _BY_KEY.get(" ".join(raw.split()).casefold(), "")


def map_states(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    operator = apps.get_model("ledger", "Operator")
    audit_log = apps.get_model("ledger", "AuditLog")
    for pk, raw in list(operator.objects.exclude(state="").values_list("pk", "state")):
        code = state_code(raw)
        if code == raw:
            continue
        if code:
            operator.objects.filter(pk=pk).update(state=code)
            continue
        operator.objects.filter(pk=pk).update(state="", needs_review=True)
        audit_log.objects.create(
            actor=None,
            action=UNMAPPED_ACTION,
            entity_type="operator",
            entity_id=str(pk),
            before={"state": raw},
            after={"state": "", "needs_review": True},
        )


def unmap_states(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    operator = apps.get_model("ledger", "Operator")
    for code, name in _NAMES.items():
        operator.objects.filter(state=code).update(state=name)


class Migration(migrations.Migration):
    dependencies = [("ledger", "0065_bank_deposit_settings")]

    operations = [migrations.RunPython(map_states, unmap_states, elidable=False)]
