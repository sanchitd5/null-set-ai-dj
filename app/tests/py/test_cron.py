"""cron.py: fields, steps, ranges, names, the day-of-month / day-of-week OR rule, month ends, leap day, DST."""
import datetime as dt
import os
import time

import pytest

from app.ui.services import cron


def ts(y, mo, d, h=0, mi=0):
    return time.mktime(dt.datetime(y, mo, d, h, mi).timetuple())


def nxt(expr, *after):
    return dt.datetime.fromtimestamp(cron.next_fire(expr, ts(*after)))


def D(*a):
    return dt.datetime(*a)


def test_every_minute_and_hourly():
    assert nxt("* * * * *", 2026, 1, 1, 10, 5) == D(2026, 1, 1, 10, 6)
    assert nxt("0 * * * *", 2026, 1, 1, 10, 0) == D(2026, 1, 1, 11, 0)      # strictly after
    assert nxt("0 * * * *", 2026, 1, 1, 23, 30) == D(2026, 1, 2, 0, 0)
    assert nxt("@hourly", 2026, 1, 1, 10, 30) == D(2026, 1, 1, 11, 0)


def test_seconds_are_ignored():
    after = ts(2026, 1, 1, 10, 0) + 59
    assert dt.datetime.fromtimestamp(cron.next_fire("* * * * *", after)) == D(2026, 1, 1, 10, 1)


def test_steps_ranges_lists():
    assert nxt("*/15 * * * *", 2026, 1, 1, 10, 16) == D(2026, 1, 1, 10, 30)
    assert nxt("10-20/5 * * * *", 2026, 1, 1, 10, 0) == D(2026, 1, 1, 10, 10)
    assert nxt("10-20/5 * * * *", 2026, 1, 1, 10, 16) == D(2026, 1, 1, 10, 20)
    assert cron.parse("10-20/5 * * * *").minutes == {10, 15, 20}
    assert cron.parse("5/20 * * * *").minutes == {5, 25, 45}
    assert cron.parse("1,2,30-32 * * * *").minutes == {1, 2, 30, 31, 32}
    assert nxt("15 4 * * *", 2026, 1, 1, 5, 0) == D(2026, 1, 2, 4, 15)
    assert nxt("30 3 * * *", 2026, 1, 1, 3, 29) == D(2026, 1, 1, 3, 30)


def test_names_and_sunday():
    assert cron.parse("0 0 * jan-mar sun").months == {1, 2, 3}
    assert cron.parse("0 0 * * 7").dows == {0}
    assert cron.parse("0 0 * * 0,7").dows == {0}
    assert nxt("0 9 * * mon", 2026, 11, 9, 9, 0) == D(2026, 11, 16, 9, 0)    # 2026-11-09 is a Monday


def test_dom_dow_or_rule():
    # both restricted: either matches (1st of the month OR a Friday)
    assert nxt("0 0 1 * fri", 2026, 11, 2) == D(2026, 11, 6)                  # Friday comes before the 1st of December
    assert nxt("0 0 1 * fri", 2026, 11, 28) == D(2026, 12, 1)
    # one restricted: only that one counts
    assert nxt("0 0 1 * *", 2026, 11, 2) == D(2026, 12, 1)
    assert nxt("0 0 * * fri", 2026, 11, 2) == D(2026, 11, 6)
    assert nxt("0 0 */2 * fri", 2026, 11, 2) == D(2026, 11, 13)               # "*/2" counts as a star: Friday AND odd day


def test_month_ends_and_leap_day():
    assert nxt("0 0 31 * *", 2026, 1, 31, 0, 0) == D(2026, 3, 31)             # February and April-style gaps are skipped
    assert nxt("0 0 31 * *", 2026, 4, 1) == D(2026, 5, 31)
    assert nxt("0 0 30 * *", 2026, 1, 31) == D(2026, 3, 30)
    assert nxt("0 0 29 2 *", 2026, 3, 1) == D(2028, 2, 29)
    assert nxt("59 23 31 12 *", 2026, 12, 31, 23, 59) == D(2027, 12, 31, 23, 59)


def test_never_fires():
    with pytest.raises(cron.CronError):
        cron.next_fire("0 0 31 2 *", ts(2026, 1, 1))


@pytest.mark.parametrize("bad", ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * 32 * *", "* * * 13 *",
                                 "* * * * 8", "5-1 * * * *", "*/0 * * * *", "a * * * *", "1,,2 * * * *", "*/x * * * *", "-1 * * * *"])
def test_invalid(bad):
    with pytest.raises(cron.CronError):
        cron.parse(bad)
    assert issubclass(cron.CronError, ValueError)


def test_validate_normalises_and_describe():
    assert cron.validate("  0   *  * * * ") == "0 * * * *"
    assert cron.describe("0 * * * *") == "every hour, on the hour"
    assert cron.describe("15 4 * * *") == "daily at 04:15"


@pytest.fixture
def tz():
    old = os.environ.get("TZ")

    def set_tz(name):
        os.environ["TZ"] = name
        time.tzset()

    yield set_tz
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_dst_spring_forward_and_fall_back(tz):
    tz("America/New_York")
    # 2026-03-08 02:30 does not exist: the daily 02:30 fire still happens once that day, never twice, never skipped
    t = ts(2026, 3, 7, 3, 0)
    fires = []
    for _ in range(4):
        t = cron.next_fire("30 2 * * *", t)
        fires.append(t)
    assert all(b > a for a, b in zip(fires, fires[1:]))
    days = [dt.datetime.fromtimestamp(f).date() for f in fires]
    assert len(set(days)) == 4
    # 2026-11-01 01:30 happens twice: an hourly fire never repeats and never goes backwards across the change
    t = ts(2026, 11, 1, 0, 0)
    seen = []
    for _ in range(6):
        t = cron.next_fire("30 * * * *", t)
        seen.append(t)
    assert all(b > a for a, b in zip(seen, seen[1:]))
    assert all(b - a <= 7200 for a, b in zip(seen, seen[1:]))
