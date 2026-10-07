"""Agent/token budget shared by the children of one orchestration (``/ultracode``)."""

from __future__ import annotations

from k3code.subagents.runner import Handle


class BudgetStop(Exception):
    """The orchestration ran out of agents or tokens; remaining work is not started."""


class AgentBudget:
    def __init__(self, max_agents: int | None = None, max_tokens: int | None = None) -> None:
        self.max_agents = max_agents
        self.max_tokens = max_tokens
        self.agents = 0
        self.tokens = 0

    def take_agent(self) -> None:
        """Reserve one child before spawning it."""
        self.check()
        if self.max_agents is not None and self.agents >= self.max_agents:
            raise BudgetStop(f"agent budget exhausted ({self.agents}/{self.max_agents} agents)")
        self.agents += 1

    def charge(self, h: Handle) -> None:
        self.tokens += h.tokens_in + h.tokens_out

    def check(self) -> None:
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            raise BudgetStop(f"token budget exhausted ({self.tokens}/{self.max_tokens} tokens)")

    def summary(self) -> str:
        agents = f"{self.agents}/{self.max_agents}" if self.max_agents is not None else str(self.agents)
        tokens = f"{self.tokens}/{self.max_tokens}" if self.max_tokens is not None else str(self.tokens)
        return f"{agents} agents, {tokens} tokens"
