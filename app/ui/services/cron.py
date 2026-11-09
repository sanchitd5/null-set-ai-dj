"""Standard 5-field cron expressions: `minute hour day-of-month month day-of-week`.

Each field takes `*`, a number, a range `a-b`, a list `a,b,c`, and steps `*/n` or `a-b/n` (`a/n` runs from a to the
field's maximum). Months accept jan..dec, weekdays sun..sat (0 or 7 = Sunday). Macros: @hourly, @daily, @midnight,
@weekly, @monthly, @yearly, @annually.

Day rule (as in Vixie cron): when both day-of-month and day-of-week are restricted (neither starts with `*`), a day
matches if EITHER matches; when one is `*`, only the other counts.

Time zone: expressions are read in the SERVER'S LOCAL TIME. Matching works on naive local wall-clock fields, so a
fire never depends on UTC offsets. Around a DST change a wall-clock time that does not exist (spring forward) fires
at the moment the clock jumps to, and one that happens twice (fall back) fires once, at its first occurrence. next_fire
always returns an epoch strictly greater than `after`, so a schedule can never fire twice for the same minute.

No dependency beyond the standard library.
"""
from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple

_MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
           "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}
_MONTHS = {n: i for i, n in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DAYS = {n: i for i, n in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
_FIELDS = (("minute", 0, 59, None), ("hour", 0, 23, None), ("day-of-month", 1, 31, None),
           ("month", 1, 12, _MONTHS), ("day-of-week", 0, 7, _DAYS))


class CronError(ValueError):
    """An invalid cron expression (the message says which field and why)."""


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: FrozenSet[int]
    hours: FrozenSet[int]
    doms: FrozenSet[int]
    months: FrozenSet[int]
    dows: FrozenSet[int]                    # 0..6, Sunday = 0
    dom_star: bool
    dow_star: bool

    def day_matches(self, d: _dt.date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok, dow_ok = d.day in self.doms, (d.isoweekday() % 7) in self.dows
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok


def _num(tok: str, name: str, lo: int, hi: int, names) -> int:
    t = tok.strip().lower()
    if names and t in names:
        return names[t]
    if not t.isdigit():
        raise CronError(f"{name}: {tok!r} is not a number")
    v = int(t)
    if not lo <= v <= hi:
        raise CronError(f"{name}: {v} is outside {lo}-{hi}")
    return v


def _field(text: str, name: str, lo: int, hi: int, names) -> Tuple[FrozenSet[int], bool]:
    out = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"{name}: empty list item")
        rng, _, step_s = part.partition("/")
        step = 1
        if "/" in part:
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"{name}: bad step {step_s!r}")
            step = int(step_s)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            x, _, y = rng.partition("-")
            a, b = _num(x, name, lo, hi, names), _num(y, name, lo, hi, names)
            if a > b:
                raise CronError(f"{name}: range {rng} runs backwards")
        else:
            a = _num(rng, name, lo, hi, names)
            b = hi if "/" in part else a
        out.update(range(a, b + 1, step))
    return frozenset(out), text.startswith("*")


def parse(expr: str) -> Cron:
    """The parsed expression, or CronError."""
    if not isinstance(expr, str):
        raise CronError("a cron expression is text")
    text = _MACROS.get(expr.strip().lower(), expr).split()
    if len(text) != 5:
        raise CronError(f"expected 5 fields (minute hour day-of-month month day-of-week), got {len(text)}")
    sets = []
    stars = []
    for tok, (name, lo, hi, names) in zip(text, _FIELDS):
        s, star = _field(tok, name, lo, hi, names)
        sets.append(s)
        stars.append(star)
    dows = frozenset(d % 7 for d in sets[4])                 # 7 is Sunday too
    return Cron(" ".join(text), sets[0], sets[1], sets[2], sets[3], dows, stars[2], stars[4])


def validate(expr: str) -> str:
    """The normalised expression (single spaces), or CronError."""
    return parse(expr).expr


def _next_naive(c: Cron, after: _dt.datetime) -> _dt.datetime:
    """The first matching minute strictly after `after` (naive local wall clock). CronError if none within 8 years."""
    start = after.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    day = start.date()
    last = day + _dt.timedelta(days=366 * 8)
    first = True
    while day <= last:
        if c.day_matches(day):
            for h in sorted(c.hours):
                if first and h < start.hour:
                    continue
                for m in sorted(c.minutes):
                    if first and h == start.hour and m < start.minute:
                        continue
                    return _dt.datetime(day.year, day.month, day.day, h, m)
        day += _dt.timedelta(days=1)
        first = False
    raise CronError(f"{c.expr}: never fires")


def next_fire(expr, after: float) -> float:
    """Epoch seconds of the first fire strictly after `after` (epoch seconds), reading the expression in local time.

    `expr` is a string or a parsed Cron. Raises CronError for an invalid or never-firing expression."""
    c = expr if isinstance(expr, Cron) else parse(expr)
    cur = _dt.datetime.fromtimestamp(after)                  # naive local
    for _ in range(4):                                       # a DST repeat can land at or before `after`: step on
        cand = _next_naive(c, cur)
        ts = time.mktime(cand.timetuple())
        if ts > after:
            return ts
        cur = cand
    raise CronError(f"{c.expr}: no fire time found after {after}")


def describe(expr: str) -> str:
    """A short human reading of the common shapes; falls back to the expression itself."""
    c = parse(expr)
    if c.expr == "0 * * * *":
        return "every hour, on the hour"
    if c.expr == "* * * * *":
        return "every minute"
    if len(c.minutes) == 1 and len(c.hours) == 1 and c.dom_star and c.dow_star and c.months == frozenset(range(1, 13)):
        return f"daily at {next(iter(c.hours)):02d}:{next(iter(c.minutes)):02d}"
    return c.expr
