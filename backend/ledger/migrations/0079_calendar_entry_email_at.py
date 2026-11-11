"""A calendar reminder's e-mail at a chosen time (docs/qa/15/PLAN-015h-calendar-reminder-time.md
§3.1, §4 task 1).

Schema, on ``calendar_entries``: ``email_at`` (time, default ``09:00``, so every existing row
reads 09:00, the hour of the old morning e-mail): the reminder's New York wall-clock time,
meaningful while ``email_morning`` is on; ``calendar_entries_email_at_offered`` (``email_at`` is
one of the 31 half hours from 06:00 to 21:00, both included). No data step. A time column holds
no text, so no payment-data trigger changes (an empty ``GUARDED_COLUMNS``). The reverse drops the
constraint and the column.
"""

from __future__ import annotations

import datetime

from django.db import migrations, models

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
#: The offered times as this migration writes them (``ledger.models.CALENDAR_REMINDER_TIMES``).
REMINDER_TIMES: tuple[datetime.time, ...] = tuple(
    datetime.time(hour, minute)
    for hour in range(6, 22)
    for minute in (0, 30)
    if (hour, minute) <= (21, 0)
)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0078_invoice_email_portal_link_only"),
    ]

    operations = [
        migrations.AddField(
            model_name="calendarentry",
            name="email_at",
            field=models.TimeField(default=datetime.time(9, 0)),
        ),
        migrations.AddConstraint(
            model_name="calendarentry",
            constraint=models.CheckConstraint(
                condition=models.Q(email_at__in=REMINDER_TIMES),
                name="calendar_entries_email_at_offered",
            ),
        ),
    ]
