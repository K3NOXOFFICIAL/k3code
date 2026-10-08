# Schedule math: design ported from hermes-agent cron/jobs.py::compute_next_run; first-party implementation.
"""Schedules: 5-field cron, intervals (``5m``/``every 2h``) and ``daily HH:MM``. Pure functions of an epoch time."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_INTERVAL_RE = re.compile(
    r"^(?:every\s+)?(\d+(?:\.\d+)?)\s*(s|m|h|d|sec|secs|min|mins|hr|hrs|hour|hours|day|days)$", re.I
)
_DAILY_RE = re.compile(r"^(?:daily|every\s*day)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?$", re.I)


class ScheduleError(ValueError):
    pass


def _val(tok: str, idx: int) -> int:
    t = tok.lower()
    if idx in (3, 4) and t in _NAMES:
        v = _NAMES[t]
    elif t.isdigit():
        v = int(t)
    else:
        raise ScheduleError(f"bad cron token {tok!r}")
    if idx == 4 and v == 7:
        v = 0
    lo, hi = _RANGES[idx]
    if not lo <= v <= hi:
        raise ScheduleError(f"cron field {idx + 1} value {v} out of range {lo}-{hi}")
    return v


def _parse_field(text: str, idx: int) -> set[int]:
    lo, hi = _RANGES[idx]
    out: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise ScheduleError(f"bad cron step {s!r}")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _val(x, idx), _val(y, idx)
            if idx == 4 and y == "7":
                b = 6
        else:
            a = _val(part, idx)
            b = hi if step > 1 else a
        if a > b:
            raise ScheduleError(f"bad cron range {part!r}")
        out.update(range(a, b + 1, step))
    return out


@dataclass(frozen=True)
class CronSpec:
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    dom_star: bool
    dow_star: bool

    def day_matches(self, dt: datetime) -> bool:
        if dt.month not in self.month:
            return False
        dom_ok = dt.day in self.dom
        dow_ok = ((dt.weekday() + 1) % 7) in self.dow  # cron: 0=Sunday
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok  # classic cron: both restricted → either


def parse_cron(expr: str) -> CronSpec:
    parts = expr.split()
    if len(parts) != 5:
        raise ScheduleError(f"cron expression needs 5 fields, got {len(parts)}: {expr!r}")
    sets = [frozenset(_parse_field(p, i)) for i, p in enumerate(parts)]
    return CronSpec(*sets, dom_star=parts[2].startswith("*"), dow_star=parts[4].startswith("*"))


def _first_after(wall: datetime, after: float) -> float | None:
    """Epoch of the wall-clock time ``wall`` that is strictly after ``after``, or None.

    Datetime arithmetic drops ``fold``, so inside the repeated DST fall-back hour a candidate resolved to its first
    (summer-time) occurrence even when ``after`` was already in the second pass: ``cron_next`` then returned a time
    ~55 minutes in the past and the job re-fired back to back. Try both occurrences and take the earlier one that is
    still in the future.
    """
    best: float | None = None
    for fold in (0, 1):
        ts = wall.replace(fold=fold).timestamp()
        if ts > after and (best is None or ts < best):
            best = ts
    return best


def cron_next(expr: str, after: float, tz: tzinfo | None = None) -> float:
    """First matching minute strictly after ``after`` (epoch seconds)."""
    spec = parse_cron(expr)
    dt = datetime.fromtimestamp(after, tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 5):  # five years of days covers Feb 29
        if spec.day_matches(dt):
            for h in sorted(spec.hour):
                if h < dt.hour:
                    continue
                for m in sorted(spec.minute):
                    if h == dt.hour and m < dt.minute:
                        continue
                    ts = _first_after(dt.replace(hour=h, minute=m), after)
                    if ts is not None:
                        return ts
        dt = (dt + timedelta(days=1)).replace(hour=0, minute=0)
    raise ScheduleError(f"cron expression never fires: {expr!r}")


@dataclass(frozen=True)
class Schedule:
    kind: str  # "cron" | "interval"
    expr: str = ""  # cron expression (kind=cron)
    seconds: float = 0.0  # interval length (kind=interval)

    def next_after(self, after: float, tz: tzinfo | None = None) -> float:
        if self.kind == "cron":
            return cron_next(self.expr, after, tz)
        return after + self.seconds

    def describe(self) -> str:
        return self.expr if self.kind == "cron" else f"every {format_seconds(self.seconds)}"

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "expr": self.expr, "seconds": self.seconds}

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Schedule:
        return cls(str(d.get("kind") or "cron"), str(d.get("expr") or ""), float(d.get("seconds") or 0))  # type: ignore[arg-type]


def format_seconds(s: float) -> str:
    n = int(s)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if n >= size and n % size == 0:
            return f"{n // size}{unit}"
    return f"{n}s"


def parse_interval(text: str) -> float | None:
    """``5m`` / ``every 2h`` / ``90s`` → seconds, or None when ``text`` is not an interval."""
    m = _INTERVAL_RE.match(text.strip())
    if not m:
        return None
    secs = float(m.group(1)) * _UNITS[m.group(2).lower()[0]]
    if secs <= 0:
        raise ScheduleError("interval must be positive")
    return secs


def parse_schedule(text: str) -> Schedule:
    """Parse an interval, ``daily HH:MM`` or a 5-field cron expression (raises ScheduleError)."""
    text = text.strip()
    secs = parse_interval(text)
    if secs is not None:
        return Schedule("interval", seconds=secs)
    m = _DAILY_RE.match(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if h > 23 or mi > 59:
            raise ScheduleError(f"bad time in {text!r}")
        return Schedule("cron", expr=f"{mi} {h} * * *")
    parse_cron(text)
    return Schedule("cron", expr=" ".join(text.split()))
