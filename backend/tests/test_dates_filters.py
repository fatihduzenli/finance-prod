"""PLAN-012f-a B4 (docs/qa/PLAN-012f-a-design-follow-up.md §4; DESIGN-SPEC §5 "Dates on
screen"): the helpers of ``finance.dates`` and the ``dates`` template filters over them.

A day reads ``Nov 1, 2026``, a month ``Oct 2026``, a moment ``Nov 1, 2026 · 14:02`` in New
York (a naive datetime is UTC). The helpers raise ``TypeError`` on anything but a date or a
datetime; the filters render ``""`` instead, so a page never fails on a date.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from django.template import Context, Template
from django.utils import timezone as dj_timezone

from finance import dates
from finance.templatetags import dates as date_filters

NEW_YORK = ZoneInfo("America/New_York")
DAY = date(2026, 11, 1)
#: 14:02 in New York on Nov 1, 2026 (EST, UTC-5, after the clocks went back at 02:00).
AWARE = datetime(2026, 11, 1, 14, 2, tzinfo=NEW_YORK)
#: The same instant as a naive UTC datetime.
NAIVE = datetime(2026, 11, 1, 19, 2)

HELPERS: list[Callable[[date | datetime], str]] = [
    dates.day_text,
    dates.day_short_text,
    dates.day_long_text,
    dates.month_text,
    dates.moment_text,
]
FILTERS = ["day", "day_short", "day_long", "month", "moment"]


def render(source: str, **context: object) -> str:
    return Template("{% load dates %}" + source).render(Context(context))


# --- the helpers ---------------------------------------------------------------------------


def test_day_text_on_a_date_and_both_datetimes() -> None:
    assert dates.day_text(DAY) == "Nov 1, 2026"
    assert dates.day_text(AWARE) == "Nov 1, 2026"
    assert dates.day_text(NAIVE) == "Nov 1, 2026"


def test_day_short_text_drops_the_year() -> None:
    assert dates.day_short_text(DAY) == "Nov 1"
    assert dates.day_short_text(AWARE) == "Nov 1"
    assert dates.day_short_text(NAIVE) == "Nov 1"


def test_day_long_text_names_the_weekday() -> None:
    assert dates.day_long_text(date(2026, 10, 20)) == "Tuesday, Oct 20, 2026"
    assert dates.day_long_text(AWARE) == "Sunday, Nov 1, 2026"
    assert dates.day_long_text(NAIVE) == "Sunday, Nov 1, 2026"


def test_month_text_on_a_date_and_both_datetimes() -> None:
    assert dates.month_text(date(2026, 10, 1)) == "Oct 2026"
    assert dates.month_text(AWARE) == "Nov 2026"
    assert dates.month_text(NAIVE) == "Nov 2026"


def test_moment_text_on_both_datetimes() -> None:
    assert dates.moment_text(AWARE) == "Nov 1, 2026 · 14:02"
    assert dates.moment_text(NAIVE) == "Nov 1, 2026 · 14:02"


def test_moment_text_converts_any_zone_to_new_york() -> None:
    tokyo = datetime(2026, 11, 2, 4, 2, tzinfo=timezone(timedelta(hours=9)))
    assert dates.moment_text(tokyo) == "Nov 1, 2026 · 14:02"
    assert dates.moment_text(datetime(2026, 11, 1, 19, 2, tzinfo=UTC)) == "Nov 1, 2026 · 14:02"


def test_moment_text_pads_the_clock_and_keeps_24_hours() -> None:
    assert dates.moment_text(datetime(2026, 7, 4, 0, 5, tzinfo=NEW_YORK)) == "Jul 4, 2026 · 00:05"
    assert dates.moment_text(datetime(2026, 7, 4, 23, 59, tzinfo=NEW_YORK)) == (
        "Jul 4, 2026 · 23:59"
    )


def test_moment_text_refuses_a_date() -> None:
    with pytest.raises(TypeError):
        dates.moment_text(DAY)


def test_new_york_versus_utc_day_boundary() -> None:
    # 03:30 UTC on Nov 1 is still 23:30 on Oct 31 in New York (EDT, UTC-4).
    late = datetime(2026, 11, 1, 3, 30)
    assert dates.moment_text(late) == "Oct 31, 2026 · 23:30"
    assert dates.moment_text(late.replace(tzinfo=UTC)) == "Oct 31, 2026 · 23:30"
    assert dates.day_text(late) == "Oct 31, 2026"
    assert dates.day_short_text(late) == "Oct 31"
    assert dates.month_text(late) == "Oct 2026"
    # New Year's: 04:59 UTC on Jan 1 is Dec 31 in New York (EST), 05:00 is Jan 1.
    assert dates.month_text(datetime(2027, 1, 1, 4, 59)) == "Dec 2026"
    assert dates.moment_text(datetime(2027, 1, 1, 5, 0)) == "Jan 1, 2027 · 00:00"


def test_daylight_saving_end_reads_the_wall_clock() -> None:
    # The clocks go back at 02:00 EDT on Nov 1, 2026: both 01:30s read 01:30.
    assert dates.moment_text(datetime(2026, 11, 1, 5, 30)) == "Nov 1, 2026 · 01:30"
    assert dates.moment_text(datetime(2026, 11, 1, 6, 30)) == "Nov 1, 2026 · 01:30"


def test_month_ends_and_leap_day() -> None:
    assert dates.day_text(date(2026, 1, 31)) == "Jan 31, 2026"
    assert dates.day_text(date(2026, 4, 30)) == "Apr 30, 2026"
    assert dates.day_text(date(2026, 12, 31)) == "Dec 31, 2026"
    assert dates.day_text(date(2028, 2, 29)) == "Feb 29, 2028"
    assert dates.day_long_text(date(2028, 2, 29)) == "Tuesday, Feb 29, 2028"
    assert dates.month_text(date(2028, 2, 29)) == "Feb 2028"
    assert dates.day_short_text(date(2028, 2, 29)) == "Feb 29"
    assert dates.moment_text(datetime(2028, 2, 29, 12, 0, tzinfo=NEW_YORK)) == (
        "Feb 29, 2028 · 12:00"
    )


def test_every_month_name() -> None:
    names = [dates.month_text(date(2026, m, 1)) for m in range(1, 13)]
    assert names == [f"{n} 2026" for n in "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()]


def test_the_active_time_zone_does_not_change_the_text() -> None:
    with dj_timezone.override("Asia/Tokyo"):
        assert dates.moment_text(NAIVE) == "Nov 1, 2026 · 14:02"
        assert dates.day_text(datetime(2026, 11, 1, 3, 30)) == "Oct 31, 2026"


@pytest.mark.parametrize("helper", HELPERS)
@pytest.mark.parametrize("value", [None, "2026-11-01", 20261101, True, 1.5])
def test_helpers_raise_type_error_on_anything_else(
    helper: Callable[[object], str], value: object
) -> None:
    with pytest.raises(TypeError):
        helper(value)


# --- the filters ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "on_date", "on_datetime"),
    [
        ("day", "Nov 1, 2026", "Nov 1, 2026"),
        ("day_short", "Nov 1", "Nov 1"),
        ("day_long", "Sunday, Nov 1, 2026", "Sunday, Nov 1, 2026"),
        ("month", "Nov 2026", "Nov 2026"),
        ("moment", "", "Nov 1, 2026 · 14:02"),
    ],
)
def test_filters_render_dates_and_datetimes(name: str, on_date: str, on_datetime: str) -> None:
    assert render(f"{{{{ v|{name} }}}}", v=DAY) == on_date
    assert render(f"{{{{ v|{name} }}}}", v=AWARE) == on_datetime
    assert render(f"{{{{ v|{name} }}}}", v=NAIVE) == on_datetime


@pytest.mark.parametrize("name", FILTERS)
@pytest.mark.parametrize("value", [None, "", "2026-11-01", 0, True, object()])
def test_filters_render_nothing_for_anything_else(name: str, value: object) -> None:
    assert render(f"{{{{ v|{name} }}}}", v=value) == ""
    assert getattr(date_filters, name)(value) == ""


@pytest.mark.parametrize("name", FILTERS)
def test_filters_on_a_missing_variable_and_with_a_default(name: str) -> None:
    assert render(f"{{{{ missing|{name} }}}}") == ""
    assert render(f'{{{{ v|{name}|default:"—" }}}}', v=None) == "—"


def test_moment_filter_new_york_versus_utc() -> None:
    assert render("{{ v|moment }}", v=datetime(2026, 11, 1, 3, 30)) == "Oct 31, 2026 · 23:30"
    assert render("{{ v|moment }}", v=datetime(2026, 11, 1, 3, 30, tzinfo=UTC)) == (
        "Oct 31, 2026 · 23:30"
    )
    assert render("{{ v|day }}", v=datetime(2026, 11, 1, 3, 30, tzinfo=UTC)) == "Oct 31, 2026"


def test_filters_ignore_the_active_time_zone() -> None:
    with dj_timezone.override("UTC"):
        assert render("{{ v|moment }}", v=AWARE) == "Nov 1, 2026 · 14:02"
        late = datetime(2026, 11, 1, 3, 30, tzinfo=UTC)
        assert render("{{ v|day }}", v=late) == "Oct 31, 2026"


def test_filters_on_month_end_and_leap_day() -> None:
    assert render("{{ v|day }}", v=date(2026, 1, 31)) == "Jan 31, 2026"
    assert render("{{ v|day }}", v=date(2028, 2, 29)) == "Feb 29, 2028"
    assert render("{{ v|month }}", v=date(2028, 2, 29)) == "Feb 2028"
    assert render("{{ v|day_short }}", v=date(2028, 2, 29)) == "Feb 29"


def test_the_middle_dot_is_not_escaped_away() -> None:
    assert "&" not in render("{{ v|moment }}", v=AWARE)
