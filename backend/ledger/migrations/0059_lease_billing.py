"""Billing details per lease (docs/qa/PLAN-014 Batch C, C2 and C8).

Schema: the table ``lease_billing`` (model ``LeaseBilling``), one row per lease (``lease_id``
unique; a foreign key that refuses to delete a lease with billing details): ``company_name``,
``email`` (lower-cased by the model), ``phone``, the five address columns, ``bank_last4`` (""
or four digits, CHECK ``lease_billing_last4_digits``), ``shares_account_of_id`` (null; another
lease, never the row's own, CHECK ``lease_billing_not_self_shared``), ``created_at`` and
``updated_at``. ``leases`` gains the processor columns the operator had (PLAN-006 D7):
``sync_status`` (``not_applicable`` by default), ``sync_failure`` (""), ``sync_failure_fields``
(""), ``sync_attempted_at`` (null) and ``creation_key`` (null, unique); its ``review_reason``
gains ``own_customer`` ("Own processor customer needed"). ``outbound_messages`` gains
``lease_id`` (null; PLAN-014 C5). The operator's own columns stay, no longer written.

Free text is guarded: on PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), in either
direction; ``lease_billing`` gets its payment-data trigger on its text columns, and the
``leases`` trigger of ``0002_postgresql_guards`` is recreated with ``sync_failure_fields``
among its arguments (the reverse restores ``0002``'s columns). Nothing is created on other
vendors; the application validator applies there.

Data (``copy_billing``), once, in this transaction; nothing here calls the payment provider:

1. Every lease gets one ``lease_billing`` row with its operator's e-mail, phone and address,
   and ``company_name`` = the operator's ``company`` when its kind is ``company`` (else "").
   A lease without an operator gets an empty row.
2. Each operator holding a live (not superseded) ``customer`` reference picks its **primary
   lease**: of its active leases (collection ``active`` or ``paused``, not ended), the one
   with the earliest ``started_on`` (no start date sorts last), then the lowest store number
   (none sorts last), then the lease id. The operator's live ``customer`` and
   ``payment_method`` references are re-pointed to it (``entity_type = "lease"``,
   ``entity_id`` = the lease; the external ids do not change, so the provider sees
   nothing; the method snapshots stay on the customer reference), and the operator's
   ``sync_status``, ``sync_failure``, ``sync_failure_fields``, ``sync_attempted_at`` and
   ``creation_key`` are copied to that lease. Every **other** lease of the operator gets
   ``shares_account_of`` = the primary lease, so its billing keeps using the same customer
   and bank method; those not ended are also flagged ``needs_review`` with the reason
   ``own_customer`` (a reason already set is kept).
3. An operator with a live ``customer`` reference and no active lease keeps its references
   and is flagged ``needs_review``.
4. Audit, actor none: one ``lease_billing.migrated`` row per lease (entity ``lease``):
   ``before`` the lease's ``needs_review`` and ``review_reason`` until then, ``after`` the
   source operator id, ``refs_moved`` (the lease is its operator's primary lease),
   ``shares`` (the lease shares the primary's account) and the flags afterwards; one
   ``operator.refs_kept`` row per operator of case 3, ``after`` naming the reason
   (:data:`REFS_KEPT_REASON`).

The reverse (``uncopy_billing``, then the schema): every live ``customer`` or
``payment_method`` reference of a lease is re-pointed to the lease's operator when that lease
is the only lease of its operator holding references and the operator holds none of its own;
otherwise (two leases of one operator hold references, as after the per-lease customers of
C9, or the lease has no operator, or the operator kept its own) it refuses
(``CANNOT_REVERSE``, the pattern of ``0018``) before writing anything. The leases' and the
operators' ``needs_review`` and ``review_reason`` are restored from the latest audit row of
this migration (the audit rows themselves stay: ``audit_log`` is append-only). The
``lease_billing`` rows are deleted, then the table and the lease columns are dropped with the
schema (the billing details and processor state entered since are lost; the operator's
columns of PLAN-006 D7 still hold what they held before this migration).
"""

from __future__ import annotations

import importlib
import uuid
from typing import Any

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

MIGRATED_ACTION = "lease_billing.migrated"
REFS_KEPT_ACTION = "operator.refs_kept"
REFS_KEPT_REASON = "Processor customer not moved: no active lease"
ENTITY_LEASE = "lease"
ENTITY_OPERATOR = "operator"
PROVIDER_KEY = "stax_pay"
KIND_CUSTOMER = "customer"
KIND_PAYMENT_METHOD = "payment_method"
OWN_CUSTOMER = "own_customer"
COMPANY = "company"
#: The collection statuses of an active lease (with ``ended_on`` null).
ACTIVE_STATUSES: tuple[str, ...] = ("active", "paused")
ENDED = "ended"
#: The operator columns copied to its primary lease.
SYNC_COLUMNS: tuple[str, ...] = (
    "sync_status",
    "sync_failure",
    "sync_failure_fields",
    "sync_attempted_at",
    "creation_key",
)
ADDRESS_COLUMNS: tuple[str, ...] = (
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
)
CANNOT_REVERSE = (
    "Processor references of several leases of one operator (or of a lease whose operator "
    "cannot take them) exist; one operator holds one customer, so this migration refuses to "
    "reverse."
)

LEASES = "leases"
BILLING = "lease_billing"
_LEASES_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0002_postgresql_guards"
).GUARDED_COLUMNS[LEASES]
#: Table -> guarded columns after this migration: ``leases`` replaces ``0002``'s entry;
#: ``lease_billing`` is new.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    LEASES: (*_LEASES_BEFORE, "sync_failure_fields"),
    BILLING: ("company_name", "email", "phone", *ADDRESS_COLUMNS),
}


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def _replace_trigger(
    schema_editor: BaseDatabaseSchemaEditor, table: str, columns: tuple[str, ...]
) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
    )
    schema_editor.execute(
        f"CREATE TRIGGER {table}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        _replace_trigger(schema_editor, table, columns)


def drop_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {BILLING}_payment_data_guard ON {BILLING};", params=None
    )
    _replace_trigger(schema_editor, LEASES, _LEASES_BEFORE)


def _primary_key(lease: Any) -> tuple[bool, Any, bool, int, str]:
    """The primary-lease order: earliest start (none last), lowest store number (none
    last), then the lease id."""
    number = lease.store.store_number
    return (
        lease.started_on is None,
        lease.started_on,
        number is None,
        number if number is not None else 0,
        str(lease.pk),
    )


def _is_active(lease: Any) -> bool:
    return lease.collection_status in ACTIVE_STATUSES and lease.ended_on is None


def _is_ended(lease: Any) -> bool:
    return lease.collection_status == ENDED or lease.ended_on is not None


def _billing_values(operator: Any) -> dict[str, Any]:
    if operator is None:
        return {}
    return {
        "company_name": (operator.company or "")[:120] if operator.kind == COMPANY else "",
        "email": operator.email or "",
        "phone": operator.phone or "",
        **{name: getattr(operator, name) or "" for name in ADDRESS_COLUMNS},
    }


def copy_billing(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    lease_model = apps.get_model("ledger", "Lease")
    billing_model = apps.get_model("ledger", "LeaseBilling")
    operator_model = apps.get_model("ledger", "Operator")
    audit_log = apps.get_model("ledger", "AuditLog")
    external_ref = apps.get_model("payments", "ExternalRef")

    live_refs = external_ref.objects.filter(
        entity_type=ENTITY_OPERATOR,
        provider=PROVIDER_KEY,
        external_kind__in=[KIND_CUSTOMER, KIND_PAYMENT_METHOD],
        superseded_at__isnull=True,
    )
    with_customer = set(
        live_refs.filter(external_kind=KIND_CUSTOMER).values_list("entity_id", flat=True)
    )
    leases = list(lease_model.objects.select_related("store", "operator").order_by("id"))
    by_operator: dict[Any, list[Any]] = {}
    for lease in leases:
        if lease.operator_id is not None:
            by_operator.setdefault(lease.operator_id, []).append(lease)

    primary_of: dict[Any, Any] = {}
    for operator_id in sorted(with_customer, key=str):
        active = [lease for lease in by_operator.get(operator_id, []) if _is_active(lease)]
        if active:
            primary_of[operator_id] = min(active, key=_primary_key)
            continue
        operator = operator_model.objects.get(pk=operator_id)
        audit_log.objects.create(
            actor=None,
            action=REFS_KEPT_ACTION,
            entity_type=ENTITY_OPERATOR,
            entity_id=str(operator_id),
            before={"needs_review": operator.needs_review},
            after={"needs_review": True, "reason": REFS_KEPT_REASON},
        )
        operator_model.objects.filter(pk=operator_id).update(needs_review=True)

    for lease in leases:
        operator = lease.operator
        billing_model.objects.create(lease=lease, **_billing_values(operator))
        primary = primary_of.get(lease.operator_id)
        moved = primary is not None and primary.pk == lease.pk
        shares = primary is not None and not moved
        before = {"needs_review": lease.needs_review, "review_reason": lease.review_reason}
        updates: dict[str, Any] = {}
        if moved:
            live_refs.filter(entity_id=lease.operator_id).update(
                entity_type=ENTITY_LEASE, entity_id=lease.pk
            )
            updates.update({name: getattr(operator, name) for name in SYNC_COLUMNS})
        if shares:
            billing_model.objects.filter(lease=lease).update(shares_account_of=primary)
            if not _is_ended(lease):
                updates["needs_review"] = True
                if not lease.review_reason:
                    updates["review_reason"] = OWN_CUSTOMER
        if updates:
            lease_model.objects.filter(pk=lease.pk).update(**updates)
        audit_log.objects.create(
            actor=None,
            action=MIGRATED_ACTION,
            entity_type=ENTITY_LEASE,
            entity_id=str(lease.pk),
            before=before,
            after={
                "operator": str(lease.operator_id) if lease.operator_id else None,
                "refs_moved": moved,
                "shares": shares,
                "shares_account_of": str(primary.pk) if shares else None,
                "needs_review": updates.get("needs_review", lease.needs_review),
                "review_reason": updates.get("review_reason", lease.review_reason),
            },
        )


def _latest_before(audit_log: Any, action: str, entity_type: str) -> dict[str, dict[str, Any]]:
    """The ``before`` of this migration's latest audit row per entity."""
    latest: dict[str, dict[str, Any]] = {}
    rows = audit_log.objects.filter(action=action, entity_type=entity_type).order_by("at", "id")
    for entity_id, before in rows.values_list("entity_id", "before"):
        if isinstance(before, dict):
            latest[entity_id] = before
    return latest


def uncopy_billing(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    lease_model = apps.get_model("ledger", "Lease")
    billing_model = apps.get_model("ledger", "LeaseBilling")
    operator_model = apps.get_model("ledger", "Operator")
    audit_log = apps.get_model("ledger", "AuditLog")
    external_ref = apps.get_model("payments", "ExternalRef")

    lease_refs = external_ref.objects.filter(
        entity_type=ENTITY_LEASE,
        provider=PROVIDER_KEY,
        external_kind__in=[KIND_CUSTOMER, KIND_PAYMENT_METHOD],
        superseded_at__isnull=True,
    )
    holders = set(lease_refs.values_list("entity_id", flat=True))
    operator_of = dict(lease_model.objects.filter(pk__in=holders).values_list("pk", "operator_id"))
    owners = list(operator_of.values())
    operators_with_refs = set(
        external_ref.objects.filter(
            entity_type=ENTITY_OPERATOR,
            provider=PROVIDER_KEY,
            external_kind__in=[KIND_CUSTOMER, KIND_PAYMENT_METHOD],
            superseded_at__isnull=True,
        ).values_list("entity_id", flat=True)
    )
    if (
        len(operator_of) != len(holders)
        or None in owners
        or len(set(owners)) != len(owners)
        or set(owners) & operators_with_refs
    ):
        raise RuntimeError(CANNOT_REVERSE)
    for lease_id, operator_id in operator_of.items():
        lease_refs.filter(entity_id=lease_id).update(
            entity_type=ENTITY_OPERATOR, entity_id=operator_id
        )

    for entity_id, before in _latest_before(audit_log, MIGRATED_ACTION, ENTITY_LEASE).items():
        lease_model.objects.filter(pk=entity_id).update(
            needs_review=bool(before.get("needs_review")),
            review_reason=str(before.get("review_reason") or ""),
        )
    for entity_id, before in _latest_before(audit_log, REFS_KEPT_ACTION, ENTITY_OPERATOR).items():
        operator_model.objects.filter(pk=entity_id).update(
            needs_review=bool(before.get("needs_review"))
        )
    billing_model.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0058_brands_and_store_notes"),
        # The processor references re-pointed by the data step.
        ("payments", "0018_copy_labels"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.CreateModel(
            name="LeaseBilling",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "company_name",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=120,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "email",
                    models.EmailField(
                        blank=True,
                        default="",
                        max_length=254,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "phone",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=50,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "address_line1",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=200,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "address_line2",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=200,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "city",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=100,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=50,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "postal_code",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=20,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("bank_last4", models.CharField(blank=True, default="", max_length=4)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "lease",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="billing",
                        to="ledger.lease",
                    ),
                ),
                (
                    "shares_account_of",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="account_sharers",
                        to="ledger.lease",
                    ),
                ),
            ],
            options={
                "db_table": "lease_billing",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("bank_last4", ""),
                            ("bank_last4__regex", "^[0-9]{4}$"),
                            _connector="OR",
                        ),
                        name="lease_billing_last4_digits",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("shares_account_of__isnull", True),
                            models.Q(("shares_account_of", models.F("lease")), _negated=True),
                            _connector="OR",
                        ),
                        name="lease_billing_not_self_shared",
                    ),
                ],
            },
        ),
        migrations.RemoveConstraint(model_name="lease", name="leases_review_reason_valid"),
        migrations.AlterField(
            model_name="lease",
            name="review_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("wire", "Pays by wire"),
                    ("dormant", "Dormant"),
                    ("closed", "Closed"),
                    ("unknown", "Unknown"),
                    ("own_customer", "Own processor customer needed"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="lease",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "review_reason__in",
                        ["wire", "dormant", "closed", "unknown", OWN_CUSTOMER, ""],
                    )
                ),
                name="leases_review_reason_valid",
            ),
        ),
        migrations.AddField(
            model_name="lease",
            name="sync_status",
            field=models.CharField(
                choices=[
                    ("not_applicable", "Not needed"),
                    ("awaiting_link", "Waiting to connect"),
                    ("pending", "In progress"),
                    ("linked", "Connected"),
                    ("failed", "Failed"),
                ],
                default="not_applicable",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="lease",
            name="sync_failure",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ProviderError", "Error"),
                    ("ValidationFailed", "Not sent"),
                    ("ProviderNotPermittedHere", "Not allowed here"),
                    ("FeatureDisabled", "Turned off"),
                    ("CircuitOpen", "Paused after repeated errors"),
                    ("FailureBudgetExhausted", "Too many errors today"),
                    ("RateLimitWaitExceeded", "Busy, try later"),
                    ("ProviderRejected", "Rejected"),
                    ("ProviderUnavailable", "Unavailable"),
                    ("TransportError", "Could not be reached"),
                    ("IdentityMismatch", "Wrong account"),
                    ("AlreadyLinkedElsewhere", "Connected to another operator"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="lease",
            name="sync_failure_fields",
            field=models.TextField(
                blank=True,
                default="",
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="lease",
            name="sync_attempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="lease",
            name="creation_key",
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.AddConstraint(
            model_name="lease",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "sync_status__in",
                        ["not_applicable", "awaiting_link", "pending", "linked", "failed"],
                    )
                ),
                name="leases_sync_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="lease",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "sync_failure__in",
                        [
                            "ProviderError",
                            "ValidationFailed",
                            "ProviderNotPermittedHere",
                            "FeatureDisabled",
                            "CircuitOpen",
                            "FailureBudgetExhausted",
                            "RateLimitWaitExceeded",
                            "ProviderRejected",
                            "ProviderUnavailable",
                            "TransportError",
                            "IdentityMismatch",
                            "AlreadyLinkedElsewhere",
                            "",
                        ],
                    )
                ),
                name="leases_sync_failure_valid",
            ),
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="lease",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="outbound_messages",
                to="ledger.lease",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        # Reversed after the data step: the rows it rewrote are checked before the ALTER
        # TABLE statements.
        migrations.RunPython(migrations.RunPython.noop, run_pending_checks, elidable=False),
        migrations.RunPython(copy_billing, uncopy_billing, elidable=False),
    ]
