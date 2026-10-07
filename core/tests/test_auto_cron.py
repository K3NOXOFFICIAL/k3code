from datetime import UTC, datetime

import pytest

from k3code.automation.cronexpr import ScheduleError, cron_next, parse_interval, parse_schedule

UTC = UTC


def ts(*a):
    return datetime(*a, tzinfo=UTC).timestamp()


def test_interval_parsing():
    assert parse_interval("5m") == 300
    assert parse_interval("1h") == 3600
    assert parse_interval("every 2h") == 7200
    assert parse_interval("90s") == 90
    assert parse_interval("daily 09:00") is None
    assert parse_schedule("daily 09:00").expr == "0 9 * * *"
    assert parse_schedule("daily 9").expr == "0 9 * * *"
    assert parse_schedule("5m").seconds == 300


def test_cron_every_minute_and_step():
    base = ts(2026, 1, 5, 10, 7, 30)
    assert cron_next("* * * * *", base, UTC) == ts(2026, 1, 5, 10, 8)
    assert cron_next("*/15 * * * *", base, UTC) == ts(2026, 1, 5, 10, 15)
    assert cron_next("*/15 * * * *", ts(2026, 1, 5, 10, 45), UTC) == ts(2026, 1, 5, 11, 0)


def test_cron_weekday_nine():
    fri = ts(2026, 1, 9, 9, 0)  # Friday 09:00 exactly → strictly after
    assert cron_next("0 9 * * 1-5", fri, UTC) == ts(2026, 1, 12, 9, 0)  # Monday
    assert cron_next("0 9 * * mon-fri", ts(2026, 1, 9, 8, 0), UTC) == fri


def test_cron_month_boundaries_and_leap():
    assert cron_next("0 0 1 * *", ts(2026, 1, 31, 12, 0), UTC) == ts(2026, 2, 1)
    assert cron_next("0 0 29 2 *", ts(2026, 1, 1), UTC) == ts(2028, 2, 29)
    assert cron_next("30 23 31 * *", ts(2026, 4, 1), UTC) == ts(2026, 5, 31, 23, 30)


def test_cron_dom_or_dow():
    # both restricted → either matches (1st of month OR Monday)
    assert cron_next("0 0 1 * 1", ts(2026, 1, 2), UTC) == ts(2026, 1, 5)  # Monday Jan 5


def test_sunday_zero_and_seven():
    assert cron_next("0 0 * * 0", ts(2026, 1, 5), UTC) == ts(2026, 1, 11)
    assert cron_next("0 0 * * 7", ts(2026, 1, 5), UTC) == ts(2026, 1, 11)


@pytest.mark.parametrize("bad", ["", "* * * *", "61 * * * *", "* 25 * * *", "a b c d e", "*/0 * * * *", "5x"])
def test_bad_expressions(bad):
    with pytest.raises(ScheduleError):
        parse_schedule(bad)
