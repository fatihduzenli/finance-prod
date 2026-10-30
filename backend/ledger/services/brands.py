"""The brands, edited by an admin on Admin › Settings (docs/qa/PLAN-014 B2).

An admin adds a brand (a name), renames one, makes one active or inactive and moves one up or
down the list; nothing deletes a brand. Every change writes one ``audit_log`` row per brand
changed: ``brand.created`` (``after`` the new row) or ``brand.changed`` (``before`` and
``after`` the fields that changed). A name is required, at most :data:`NAME_MAX` characters,
unique without regard to case and free of card or account numbers; each refusal is a
sentence that never repeats the value. A brand in use may be made inactive: its stores keep it
and the store form stops offering it to the others (``ledger.forms.StoreForm``).

:func:`rows` reads the list with each brand's number of stores (one query).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count

from ledger.models import AuditLog, Brand
from ledger.validators import validate_no_payment_data

ENTITY_BRAND = "brand"
NAME_MAX = 60
NAME_REQUIRED = "Enter the brand's name."
NAME_TOO_LONG = f"A brand's name is at most {NAME_MAX} characters."
NAME_TAKEN = "A brand with this name exists already."
NAME_REFUSED = "A brand's name cannot hold a card or account number."
NOT_FOUND = "That brand is not in the list."
UP = "up"
DOWN = "down"
MOVES: tuple[str, ...] = (UP, DOWN)


@dataclass(frozen=True, slots=True)
class BrandRow:
    brand: Brand
    #: The stores carrying this brand.
    stores: int


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    #: What the page says: the change made, or the refusal.
    message: str


def rows() -> list[BrandRow]:
    """Every brand in the list's order (``sort_order``, then name), with its stores."""
    found = Brand.objects.annotate(store_count=Count("stores")).order_by("sort_order", "name")
    return [BrandRow(brand=brand, stores=brand.store_count) for brand in found]


def clean_name(raw: object) -> str:
    """The name as typed with its spaces collapsed; "" for anything but text."""
    return " ".join(raw.split()) if isinstance(raw, str) else ""


def name_refusal(name: str, *, exclude: uuid.UUID | None = None) -> str:
    """The sentence refusing ``name`` (already cleaned), or "" when it may be saved."""
    if not name:
        return NAME_REQUIRED
    if len(name) > NAME_MAX:
        return NAME_TOO_LONG
    try:
        validate_no_payment_data(name)
    except ValidationError:
        return NAME_REFUSED
    taken = Brand.objects.filter(name__iexact=name)
    if exclude is not None:
        taken = taken.exclude(pk=exclude)
    if taken.exists():
        return NAME_TAKEN
    return ""


def _fields(brand: Brand) -> dict[str, Any]:
    return {"name": brand.name, "active": brand.active, "sort_order": brand.sort_order}


def _audit(actor: User | None, action: str, brand: Brand, before: Any, after: Any) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_BRAND,
        entity_id=str(brand.pk),
        before=before,
        after=after,
    )


def _changed(actor: User | None, brand: Brand, before: dict[str, Any]) -> None:
    after = _fields(brand)
    names = [name for name in after if after[name] != before[name]]
    if names:
        _audit(
            actor,
            "brand.changed",
            brand,
            {name: before[name] for name in names},
            {name: after[name] for name in names},
        )


def _locked(brand_id: uuid.UUID) -> Brand | None:
    return Brand.objects.select_for_update().filter(pk=brand_id).first()


def add(raw_name: object, actor: User | None) -> Outcome:
    """A new active brand at the end of the list."""
    name = clean_name(raw_name)
    with transaction.atomic():
        # The list is locked so two adds of one name cannot both pass the check.
        last = list(Brand.objects.select_for_update().order_by("-sort_order")[:1])
        refusal = name_refusal(name)
        if refusal:
            return Outcome(False, refusal)
        brand = Brand.objects.create(
            name=name, active=True, sort_order=(last[0].sort_order + 1) if last else 1
        )
        _audit(actor, "brand.created", brand, None, _fields(brand))
    return Outcome(True, "Brand added.")


def rename(brand_id: uuid.UUID, raw_name: object, actor: User | None) -> Outcome:
    name = clean_name(raw_name)
    with transaction.atomic():
        list(Brand.objects.select_for_update().order_by("pk"))
        brand = _locked(brand_id)
        if brand is None:
            return Outcome(False, NOT_FOUND)
        refusal = name_refusal(name, exclude=brand.pk)
        if refusal:
            return Outcome(False, refusal)
        before = _fields(brand)
        brand.name = name
        brand.save(update_fields=["name", "updated_at"])
        _changed(actor, brand, before)
    return Outcome(True, "Brand saved.")


def toggle(brand_id: uuid.UUID, actor: User | None) -> Outcome:
    """Makes an active brand inactive, an inactive one active."""
    with transaction.atomic():
        brand = _locked(brand_id)
        if brand is None:
            return Outcome(False, NOT_FOUND)
        before = _fields(brand)
        brand.active = not brand.active
        brand.save(update_fields=["active", "updated_at"])
        _changed(actor, brand, before)
    return Outcome(True, "Brand made active." if brand.active else "Brand made inactive.")


def move(brand_id: uuid.UUID, direction: str, actor: User | None) -> Outcome:
    """Swaps the brand with its neighbour above (``up``) or below (``down``); the list is
    numbered 1, 2, 3... first, so the order is always the one shown. The first brand moved up
    or the last moved down stays where it is."""
    if direction not in MOVES:
        return Outcome(False, NOT_FOUND)
    with transaction.atomic():
        ordered = list(Brand.objects.select_for_update().order_by("sort_order", "name"))
        index = next((at for at, brand in enumerate(ordered) if brand.pk == brand_id), None)
        if index is None:
            return Outcome(False, NOT_FOUND)
        other = index - 1 if direction == UP else index + 1
        if 0 <= other < len(ordered):
            ordered[index], ordered[other] = ordered[other], ordered[index]
        for position, brand in enumerate(ordered, start=1):
            if brand.sort_order != position:
                before = _fields(brand)
                brand.sort_order = position
                brand.save(update_fields=["sort_order", "updated_at"])
                _changed(actor, brand, before)
    return Outcome(True, "Brand moved.")
