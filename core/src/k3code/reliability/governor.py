"""Resource governor: admission control, disk guard and budget accounting.

- PSI admission: reads Linux pressure-stall info from ``/proc/pressure/{io,cpu}``
  (``some avg10``); IO-heavy work is only admitted when the IO ``some avg10`` is
  below the threshold (default 20).
- Concurrency caps: at most 2 IO-heavy and 4 total workers; a per-provider
  concurrent-stream cap (default 4). All configurable.
- Disk guard: refuse to start new work when free space on ``$K3CODE_HOME`` is
  below 2 GB.
- Budgets: tokens/USD per session, per day and per job; usage comes in from
  router events. Exceeding a budget raises :class:`BudgetExceeded` and stops
  the turn with a clear message (via the ``reliability.budget_exceeded`` event).

API: ``async with governor.slot(kind="io"|"cpu"|"llm", provider=...)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from k3code.reliability.events import EventEmitter

# ── PSI parsing ────────────────────────────────────────────────────────


def parse_pressure(text: str) -> dict[str, float]:
    """Parse one ``/proc/pressure/<resource>`` file body.

    Lines look like ``some avg10=1.23 avg60=0.00 ...`` / ``full avg10=...``.
    Returns flat keys ``some_avg10``, ``full_avg60`` etc.; skips unknown lines.
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("some", "full"):
            continue
        kind = parts[0]
        for kv in parts[1:]:
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            with contextlib.suppress(ValueError):
                out[f"{kind}_{k}"] = float(v)
    return out


def read_psi(path: str) -> dict[str, float]:
    """Read a PSI file, e.g. ``/proc/pressure/io``; {} when unavailable."""
    try:
        with open(path) as f:
            return parse_pressure(f.read())
    except OSError:
        return {}


# ── Errors ─────────────────────────────────────────────────────────────


class BudgetExceeded(Exception):
    """A configured budget (tokens/USD) would be exceeded by further work."""

    def __init__(self, message: str, scope: str = "", kind: str = "") -> None:
        super().__init__(message)
        self.scope = scope
        self.kind = kind


class DiskGuardFull(Exception):
    """Free space on $K3CODE_HOME is below the minimum."""

    def __init__(self, message: str, free_bytes: int = 0, min_bytes: int = 0) -> None:
        super().__init__(message)
        self.free_bytes = free_bytes
        self.min_bytes = min_bytes


# ── Budgets ────────────────────────────────────────────────────────────


@dataclass
class Budget:
    """Token/cost limits for one scope (session | day | job)."""

    scope: str  # "session" | "day" | "job"
    tokens: int | None = None
    usd: float | None = None
    used_tokens: int = 0
    used_usd: float = 0.0

    def would_exceed(self, tokens: int = 0, usd: float = 0.0) -> str | None:
        """Non-empty message when this usage would exceed the budget."""
        if self.tokens is not None and self.used_tokens + tokens > self.tokens:
            return f"{self.scope} token budget exceeded: {self.used_tokens + tokens} > {self.tokens}"
        if self.usd is not None and self.used_usd + usd > self.usd:
            return f"{self.scope} USD budget exceeded: {self.used_usd + usd:.4f} > {self.usd:.4f}"
        return None


class _DayLedger:
    """Process-wide spend of the current local day: ``day`` budgets are shared by every session and rolled at midnight.

    Each session has its own Governor, so a per-governor counter made the "day" cap per session and never reset.
    The ledger lives in the daemon process; a daemon restart starts the day's count from zero.
    """

    def __init__(self) -> None:
        self.date = ""
        self.tokens = 0
        self.usd = 0.0

    def roll(self, today: str) -> None:
        if self.date != today:
            self.date, self.tokens, self.usd = today, 0, 0.0


DAY_LEDGER = _DayLedger()


def _local_date() -> str:
    return time.strftime("%Y-%m-%d")


@dataclass
class GovernorConfig:
    """All governor knobs, configurable via config later."""

    psi_io_threshold: float = 20.0  # IO some avg10 admission threshold
    psi_path: str = "/proc/pressure"  # overridable in tests
    max_io_heavy: int = 2
    max_total: int = 4
    max_parallel_agents: int = 3  # sub-agent fan-out width; IO-heavy work is capped at max_io_heavy
    per_provider_streams: int = 4
    min_free_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GB
    home: Path = Path.home() / ".k3code"  # disk-guard target ($K3CODE_HOME)
    # USD costs: rough default per-token prices (input, output) for budgeting
    # when the router does not report cost directly.
    tokens_per_usd: float = 2_000_000.0  # blended; overridable per-budget


@dataclass
class _Counters:
    io: int = 0
    total: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)


class Governor:
    """Admission control + budget guard for concurrent agents and jobs."""

    def __init__(
        self,
        config: GovernorConfig | None = None,
        *,
        events: EventEmitter | None = None,
        day_ledger: _DayLedger | None = None,
    ) -> None:
        self.config = config or GovernorConfig()
        self.events = events or EventEmitter()
        #: Sessions of one daemon pass the shared ``DAY_LEDGER``; a bare Governor keeps its own day count.
        self.day_ledger = day_ledger or _DayLedger()
        self.today: Callable[[], str] = _local_date  # injectable clock for the day budget (tests)
        self._counters = _Counters()
        self._budgets: dict[str, Budget] = {}
        self._lock = asyncio.Lock()

    # ── PSI / disk ──

    def read_psi(self, resource: str = "io") -> dict[str, float]:
        """PSI stats for ``io`` or ``cpu`` ({} on systems without PSI)."""
        return read_psi(str(Path(self.config.psi_path) / resource))

    def io_pressure(self) -> float:
        """IO ``some avg10``; 0 when PSI is unavailable (do not block on unknown)."""
        psi = self.read_psi("io")
        return psi.get("some_avg10", 0.0)

    def agent_cap(self, requested: int, *, io_heavy: bool = False) -> int:
        """How many sub-agents may run at once: the request, bounded by ``max_parallel_agents`` (and the IO cap)."""
        cap = min(max(1, requested), self.config.max_parallel_agents)
        return min(cap, self.config.max_io_heavy) if io_heavy else cap

    def io_admission_ok(self) -> bool:
        return self.io_pressure() < self.config.psi_io_threshold

    def disk_stop(self) -> DiskGuardFull | None:
        """Disk guard: error to raise when free space on home is below the floor."""
        try:
            usage = shutil.disk_usage(self.config.home)
        except OSError:
            return None  # cannot stat → do not block
        if usage.free < self.config.min_free_bytes:
            msg = (
                f"Disk guard: only {usage.free / 1e9:.2f} GB free on {self.config.home} "
                f"(minimum {self.config.min_free_bytes / 1e9:.0f} GB); not starting new work."
            )
            return DiskGuardFull(msg, free_bytes=usage.free, min_bytes=self.config.min_free_bytes)
        return None

    # ── budgets ──

    def add_budget(self, budget: Budget) -> None:
        self._budgets[budget.scope] = budget

    def record_usage(self, prompt_tokens: int, completion_tokens: int, *, usd: float | None = None) -> None:
        """Apply a usage report (from router 'done' messages) to every budget."""
        cost = usd if usd is not None else (prompt_tokens + completion_tokens) / self.config.tokens_per_usd
        self.day_ledger.roll(self.today())
        self.day_ledger.tokens += prompt_tokens + completion_tokens
        self.day_ledger.usd += cost
        for b in self._budgets.values():
            if b.scope == "day":
                continue  # synced from the shared ledger in check_budgets
            b.used_tokens += prompt_tokens + completion_tokens
            b.used_usd += cost

    def check_budgets(self) -> BudgetExceeded | None:
        """First budget violation, or None."""
        self.day_ledger.roll(self.today())
        for b in self._budgets.values():
            if b.scope == "day":
                b.used_tokens, b.used_usd = self.day_ledger.tokens, self.day_ledger.usd
            msg = b.would_exceed()  # usage already applied; check without delta
            if msg is None:
                continue
            kind = "tokens" if (b.tokens is not None and "token" in msg) else "usd"
            return BudgetExceeded(msg, scope=b.scope, kind=kind)
        return None

    def apply_usage_and_check(self, prompt_tokens: int, completion_tokens: int) -> BudgetExceeded | None:
        """Record usage then report the first violated budget."""
        self.record_usage(prompt_tokens, completion_tokens)
        return self.check_budgets()

    # ── slots ──

    def _stats_snapshot(self) -> dict[str, int]:
        return {"io": self._counters.io, "total": self._counters.total}

    @contextlib.asynccontextmanager
    async def slot(self, kind: str = "cpu", provider: str | None = None) -> AsyncIterator[None]:
        """Admission-controlled slot: ``async with governor.slot("io")``.

        Kinds: ``"io"`` counts against the IO-heavy cap and the PSI threshold,
        ``"cpu"`` against the total cap only, ``"llm"`` against the total cap
        plus the per-provider concurrent-stream cap.

        Raises :class:`DiskGuardFull` before acquiring when the disk guard trips.
        """
        await self._acquire(kind, provider)
        try:
            yield
        finally:
            self._release(kind, provider)

    async def _acquire(self, kind: str, provider: str | None) -> None:
        """Block until a slot of ``kind`` is admitted (disk guard may raise)."""
        disk_err = self.disk_stop()
        if disk_err is not None:
            raise disk_err

        if kind == "io":
            # Wait for PSI to fall below the threshold before taking an IO slot.
            while not self.io_admission_ok():
                await asyncio.sleep(0.5)

        while True:
            async with self._lock:
                provider_ok = (
                    provider is None
                    or kind != "llm"
                    or self._counters.by_provider.get(provider, 0) < self.config.per_provider_streams
                )
                total_ok = self._counters.total < self.config.max_total
                io_ok = kind != "io" or self._counters.io < self.config.max_io_heavy
                if provider_ok and total_ok and io_ok:
                    self._counters.total += 1
                    if kind == "io":
                        self._counters.io += 1
                    if kind == "llm" and provider is not None:
                        self._counters.by_provider[provider] = (
                            self._counters.by_provider.get(provider, 0) + 1
                        )
                    return
            # Caps are full (or the provider stream cap): back off briefly.
            await asyncio.sleep(0.05)

    def _release(self, kind: str, provider: str | None) -> None:
        self._counters.total = max(0, self._counters.total - 1)
        if kind == "io":
            self._counters.io = max(0, self._counters.io - 1)
        if kind == "llm" and provider is not None:
            self._counters.by_provider[provider] = max(0, self._counters.by_provider.get(provider, 0) - 1)

    def stats(self) -> dict[str, int | float]:
        """Current occupancy snapshot for /stats and tests."""
        return {
            "io": self._counters.io,
            "total": self._counters.total,
            "io_pressure_avg10": self.io_pressure(),
            "free_bytes": self._free_bytes(),
        }

    def _free_bytes(self) -> int:
        with contextlib.suppress(OSError):
            return shutil.disk_usage(self.config.home).free
        return -1
