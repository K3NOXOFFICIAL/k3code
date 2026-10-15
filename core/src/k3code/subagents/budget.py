"""Agent/token budget shared by the children of one orchestration (``/ultracode``)."""

from __future__ import annotations

from k3code.subagents.runner import Handle


class BudgetStop(Exception):
    """The orchestration ran out of agents or tokens; remaining work is not started."""


class AgentBudget:
    """Children are reserved (:meth:`take_agent`) before they start and charged (:meth:`charge`) when they end. A
    child registered with :meth:`track` while it runs counts with its live use (the handle's ``tokens_in +
    tokens_out``, updated after every model call), so the cap holds for children already running: the caller
    watches :meth:`over` and stops them (charging only at the end let up to ``max_parallel`` children
    overshoot the cap by a whole run each)."""

    def __init__(self, max_agents: int | None = None, max_tokens: int | None = None) -> None:
        self.max_agents = max_agents
        self.max_tokens = max_tokens
        self.agents = 0
        self.tokens = 0
        #: children running now, by handle id (see :meth:`track`)
        self.running: dict[str, Handle] = {}

    def take_agent(self) -> None:
        """Reserve one child before spawning it."""
        self.check()
        if self.max_agents is not None and self.agents >= self.max_agents:
            raise BudgetStop(f"agent budget exhausted ({self.agents}/{self.max_agents} agents)")
        self.agents += 1

    def track(self, h: Handle) -> None:
        """Count ``h``'s tokens while it runs, not only once it is charged."""
        self.running[h.id] = h

    def charge(self, h: Handle) -> None:
        self.running.pop(h.id, None)
        self.tokens += h.tokens_in + h.tokens_out

    @property
    def used(self) -> int:
        """Tokens of the finished children plus what the running ones have used so far."""
        return self.tokens + sum(h.tokens_in + h.tokens_out for h in self.running.values())

    def over(self) -> str:
        """Why the token budget is spent (running children included), or "" while it is not."""
        used = self.used
        if self.max_tokens is not None and used >= self.max_tokens:
            return f"token budget exhausted ({used}/{self.max_tokens} tokens)"
        return ""

    def check(self) -> None:
        if why := self.over():
            raise BudgetStop(why)

    def summary(self) -> str:
        agents = f"{self.agents}/{self.max_agents}" if self.max_agents is not None else str(self.agents)
        tokens = f"{self.tokens}/{self.max_tokens}" if self.max_tokens is not None else str(self.tokens)
        return f"{agents} agents, {tokens} tokens"
