"""PostgreSQL guards for the ledger tables (docs/qa/PLAN-004-schema.md §4.3, §4.6, §4.4, §4.1).

- ``finance_contains_card_number(text)``: the Luhn rule of ``ledger.validators``, with the
  same normalisation (§4.6 points 2 to 4). Digits of scripts other than ASCII,
  full-width, Arabic-Indic and extended Arabic-Indic are read by the application only.
- One ``BEFORE INSERT OR UPDATE`` trigger per table with free text or JSON, rejecting a
  row whose guarded column holds a card number, with the value-free message below.
- ``invoices.provider_status`` may change only while ``finance.sync_writer = 'on'``
  (set by ``payments.sync_writer.provider_write``).
- ``audit_log`` rejects every UPDATE and DELETE.
- ``invoices.provider_status`` carries a column comment saying who writes it.

Nothing is created on any other database vendor (sqlite is local development only);
the application validator still applies there. The reverse operation drops everything
this migration created.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

PAYMENT_DATA_MESSAGE = "This text may not contain card or account numbers."

#: Table -> guarded columns (PLAN-004 §4.6 point 1): every text column without choices
#: that can hold 13 characters, and every JSON column, except the identifier columns
#: ``audit_log.entity_id`` and ``import_audit.target_id``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "operators": (
        "display_name",
        "legal_name",
        "company",
        "email",
        "phone",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
        "notes",
    ),
    "stores": ("external_label", "city", "state", "open_date_note"),
    "leases": ("end_reason",),
    "charge_notes": ("body", "author_label"),
    "charge_adjustments": ("note",),
    "notes": ("body", "author_label"),
    "invoices": ("invoice_number", "memo", "provider_status"),
    "invoice_line_items": ("description",),
    "payments": ("return_code", "return_reason", "reference", "note", "raw_summary"),
    "audit_log": ("action", "entity_type", "before", "after"),
    "legacy_records": ("source_tab", "raw"),
    "import_audit": ("sheet_col", "raw_value", "target_table", "rule_applied"),
}

# Decimal digits of other scripts that the application validator also reads as digits:
# full-width, Arabic-Indic and extended Arabic-Indic. Mapped to ASCII before matching.
_OTHER_DIGITS = "".join(
    chr(start + offset) for start in (0xFF10, 0x0660, 0x06F0) for offset in range(10)
)
_ASCII_DIGITS = "0123456789" * 3

# The normalisation of ``ledger.validators.normalise_for_matching``, in the same order:
# invisible characters removed, UUID-shaped tokens replaced by one letter, a Windows line
# break turned into one line break. Then other-script digits become ASCII digits.
_INVISIBLE_SQL = " || ".join(f"chr({ord(char)})" for char in "\u200b\u200c\u200d\u2060\ufeff")
_HEX = "[0-9A-Fa-f]"
_UUID_PATTERN = f"(?<!{_HEX}){_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}(?!{_HEX})"
#: A run of digits with up to three separators between neighbours (PLAN-004 §4.6 point 2).
_DIGIT_RUN_PATTERN = r"([0-9](?:[ \t\u00A0\u2010-\u2015\n-]{0,3}[0-9])*)"

CONTAINS_CARD_NUMBER_SQL = f"""
CREATE OR REPLACE FUNCTION finance_contains_card_number(input text)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $function$
DECLARE
    normalised text;
    run text;
    digits text;
    digit_count integer;
    total integer;
    digit integer;
    idx integer;
BEGIN
    IF input IS NULL THEN
        RETURN false;
    END IF;
    normalised := translate(input, {_INVISIBLE_SQL}, '');
    normalised := regexp_replace(normalised, '{_UUID_PATTERN}', 'u', 'g');
    normalised := replace(normalised, chr(13) || chr(10), chr(10));
    normalised := translate(normalised, '{_OTHER_DIGITS}', '{_ASCII_DIGITS}');
    FOR run IN
        SELECT matched[1]
        FROM regexp_matches(normalised, '{_DIGIT_RUN_PATTERN}', 'g') AS matched
    LOOP
        digits := regexp_replace(run, '[^0-9]', '', 'g');
        digit_count := length(digits);
        IF digit_count BETWEEN 13 AND 19 THEN
            total := 0;
            FOR idx IN 1..digit_count LOOP
                digit := ascii(substr(digits, digit_count - idx + 1, 1)) - 48;
                IF mod(idx, 2) = 0 THEN
                    digit := digit * 2;
                    IF digit > 9 THEN
                        digit := digit - 9;
                    END IF;
                END IF;
                total := total + digit;
            END LOOP;
            IF mod(total, 10) = 0 THEN
                RETURN true;
            END IF;
        END IF;
    END LOOP;
    RETURN false;
END;
$function$;
"""  # noqa: S608 - constant text, no input

REJECT_PAYMENT_DATA_SQL = f"""
CREATE OR REPLACE FUNCTION finance_reject_payment_data()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    new_row jsonb := to_jsonb(NEW);
    guarded_column text;
    guarded_value jsonb;
    leaf jsonb;
    rejected boolean;
BEGIN
    -- The trigger arguments name the guarded columns. A text column arrives as a jsonb
    -- string (#>> '{{}}' unwraps it); a jsonb column is checked on its text form and, as
    -- that form escapes line breaks, also string by string. The message never includes
    -- the value.
    FOREACH guarded_column IN ARRAY TG_ARGV LOOP
        guarded_value := new_row -> guarded_column;
        CONTINUE WHEN guarded_value IS NULL OR jsonb_typeof(guarded_value) = 'null';
        rejected := finance_contains_card_number(guarded_value #>> '{{}}');
        IF NOT rejected AND jsonb_typeof(guarded_value) IN ('object', 'array') THEN
            FOR leaf IN SELECT jsonb_path_query(guarded_value, 'strict $.**') LOOP
                IF jsonb_typeof(leaf) = 'string'
                    AND finance_contains_card_number(leaf #>> '{{}}') THEN
                    rejected := true;
                    EXIT;
                END IF;
            END LOOP;
        END IF;
        IF rejected THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                MESSAGE = '{PAYMENT_DATA_MESSAGE}';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$function$;
"""

PROVIDER_STATUS_GUARD_SQL = """
CREATE OR REPLACE FUNCTION finance_guard_provider_status()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF coalesce(current_setting('finance.sync_writer', true), '') = 'on' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.provider_status <> '' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'provider_status is written only by the sync layer.';
        END IF;
    ELSIF NEW.provider_status IS DISTINCT FROM OLD.provider_status THEN
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = 'provider_status is written only by the sync layer.';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE TRIGGER invoices_provider_status_guard
BEFORE INSERT OR UPDATE ON invoices
FOR EACH ROW EXECUTE FUNCTION finance_guard_provider_status();
"""

AUDIT_LOG_APPEND_ONLY_SQL = """
CREATE OR REPLACE FUNCTION finance_audit_log_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'restrict_violation',
        MESSAGE = 'audit_log is append-only.';
END;
$function$;

CREATE TRIGGER audit_log_append_only
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION finance_audit_log_append_only();
"""

PROVIDER_STATUS_COMMENT_SQL = (
    "COMMENT ON COLUMN invoices.provider_status IS "
    "'Copied from the payment provider. Written only by the sync layer, never by staff "
    "or views.';"
)

DROP_SQL = """
COMMENT ON COLUMN invoices.provider_status IS NULL;
DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;
DROP FUNCTION IF EXISTS finance_audit_log_append_only();
DROP TRIGGER IF EXISTS invoices_provider_status_guard ON invoices;
DROP FUNCTION IF EXISTS finance_guard_provider_status();
"""


def _payment_data_trigger_sql(table: str, columns: tuple[str, ...]) -> str:
    arguments = ", ".join(f"'{column}'" for column in columns)
    return (
        f"CREATE TRIGGER {table}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});"
    )


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CONTAINS_CARD_NUMBER_SQL, params=None)
    schema_editor.execute(REJECT_PAYMENT_DATA_SQL, params=None)
    for table, columns in GUARDED_COLUMNS.items():
        schema_editor.execute(_payment_data_trigger_sql(table, columns), params=None)
    schema_editor.execute(PROVIDER_STATUS_GUARD_SQL, params=None)
    schema_editor.execute(PROVIDER_STATUS_COMMENT_SQL, params=None)
    schema_editor.execute(AUDIT_LOG_APPEND_ONLY_SQL, params=None)


def drop_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DROP_SQL, params=None)
    for table in GUARDED_COLUMNS:
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
        )
    schema_editor.execute("DROP FUNCTION IF EXISTS finance_reject_payment_data();", params=None)
    schema_editor.execute(
        "DROP FUNCTION IF EXISTS finance_contains_card_number(text);", params=None
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_guards, drop_guards, elidable=False),
    ]
