"""Reliability layer: netwatch, persistent retry, crash journal, governor, loop guard.

Public entry points for the agent loop:

- :class:`NetWatch` — connectivity state machine (``net.state`` events).
- :class:`PersistentRetry` — pause/park/resume wrapper around ``Router.stream``.
- :class:`ToolJournal` — fsync'd intent/done journal for crash-safe tool runs.
- :class:`Governor` — PSI admission, concurrency caps, disk guard, budgets.
- :class:`LoopGuard` — doom-loop detection with corrective note / turn stop.
- :class:`EventEmitter` — fan-out for ``reliability.*`` and ``net.state`` events.
"""

from k3code.reliability.events import (
    BUDGET_EXCEEDED,
    INTERRUPTED_TOOL,
    LOOP_DETECTED,
    LOOP_NOTE_INJECTED,
    NEEDS_INPUT,
    NET_STATE,
    PARKED,
    PAUSED,
    RESUMED,
    UNPARKED,
    EventEmitter,
    ReliabilityEvent,
)
from k3code.reliability.governor import (
    Budget,
    BudgetExceeded,
    DiskGuardFull,
    Governor,
    GovernorConfig,
    parse_pressure,
    read_psi,
)
from k3code.reliability.journal import (
    INTERRUPTED_TEMPLATE,
    PendingIntent,
    ToolJournal,
    args_digest,
    interrupted_result,
    result_digest,
)
from k3code.reliability.loopguard import GuardOutcome, LoopGuard, Verdict, normalize_args
from k3code.reliability.netwatch import NetState, NetWatch, NetWatchConfig
from k3code.reliability.persistent_retry import (
    RATE_LIMIT_REASONS,
    CancelToken,
    PersistentRetry,
    RetryConfig,
    TurnCancelled,
)

__all__ = [
    "BUDGET_EXCEEDED",
    "INTERRUPTED_TEMPLATE",
    "INTERRUPTED_TOOL",
    "LOOP_DETECTED",
    "LOOP_NOTE_INJECTED",
    "NEEDS_INPUT",
    "NET_STATE",
    "PARKED",
    "PAUSED",
    "RATE_LIMIT_REASONS",
    "RESUMED",
    "UNPARKED",
    "Budget",
    "BudgetExceeded",
    "CancelToken",
    "DiskGuardFull",
    "EventEmitter",
    "Governor",
    "GovernorConfig",
    "GuardOutcome",
    "LoopGuard",
    "NetState",
    "NetWatch",
    "NetWatchConfig",
    "PendingIntent",
    "PersistentRetry",
    "ReliabilityEvent",
    "RetryConfig",
    "ToolJournal",
    "TurnCancelled",
    "Verdict",
    "args_digest",
    "interrupted_result",
    "normalize_args",
    "parse_pressure",
    "read_psi",
    "result_digest",
]
