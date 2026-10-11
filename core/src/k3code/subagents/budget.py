"""Agent/token budget shared by the children of one orchestration (``/ultracode``)."""

from __future__ import annotations

from k3code.subagents.runner import Handle

#: What the governor's message starts with when a child hits the orchestration's token cap (scope "ultracode").
CAP_SCOPE = "ultracode"
CAP_ERROR = f"{CAP_SCOPE} token budget exceeded"


class BudgetStop(Exception):
    """The orchestration ran out of agents or tokens; remaining work is not started.

    ``kind`` is ``agents`` or ``tokens``: a run that is out of agents can still spend its reserved tail (the review
    panel and the final tests), a run out of tokens cannot."""

    def __init__(self, message: str = "", kind: str = "agents") -> None:
        super().__init__(message)
        self.kind = kind


class AgentBudget:
    def __init__(self, max_agents: int | None = None, max_tokens: int | None = None) -> None:
        self.max_agents = max_agents
        self.max_tokens = max_tokens
        self.agents = 0
        self.tokens = 0
        #: agents held back for the end of the run (the review lenses and the fixer): everyone else sees the budget
        #: without them, so a wide fan-out cannot starve the review that follows it
        self.reserved = 0

    def reserve(self, n: int) -> None:
        """Hold ``n`` agents back from every :meth:`take_agent` that is not ``reserved=True``."""
        self.reserved = max(0, int(n))

    def trim(self, n: int) -> None:
        """Lower ``max_agents`` to ``n`` (never raise it): an ``auto`` run drops what its sized plan will not use."""
        if self.max_agents is not None:
            self.max_agents = max(self.agents, min(self.max_agents, int(n)))

    def refund(self, n: int = 1) -> None:
        """Give back ``n`` agents taken for work that did not happen (a spawn that failed, a prepaid slot not used)."""
        self.agents = max(0, self.agents - max(0, int(n)))

    def left(self, *, reserved: bool = False) -> int | None:
        """Agents that may still be taken (None = no limit); ``reserved=True`` counts the held-back ones too."""
        if self.max_agents is None:
            return None
        return max(0, self.max_agents - self.agents - (0 if reserved else self.reserved))

    def take_agent(self, *, reserved: bool = False) -> None:
        """Reserve one child before spawning it."""
        self.check()
        if self.max_agents is not None:
            ceiling = self.max_agents - (0 if reserved else self.reserved)
            if self.agents >= ceiling:
                held = f", {self.reserved} held back for the review" if self.reserved and not reserved else ""
                raise BudgetStop(f"agent budget exhausted ({self.agents}/{self.max_agents} agents{held})", "agents")
        self.agents += 1

    def charge(self, h: Handle) -> None:
        self.tokens += h.tokens_in + h.tokens_out
        if self.max_tokens is not None and CAP_ERROR in str(h.error or ""):
            # the child was stopped mid-run by the session governor's ultracode cap; the tokens it used before the stop
            # may be less than the cap in this tally, so the tally is raised to it: the run is out of tokens
            self.tokens = max(self.tokens, self.max_tokens)

    def check(self) -> None:
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            raise BudgetStop(f"token budget exhausted ({self.tokens}/{self.max_tokens} tokens)", "tokens")

    def summary(self) -> str:
        agents = f"{self.agents}/{self.max_agents}" if self.max_agents is not None else str(self.agents)
        tokens = f"{self.tokens}/{self.max_tokens}" if self.max_tokens is not None else str(self.tokens)
        return f"{agents} agents, {tokens} tokens"
