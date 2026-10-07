"""Sub-agents: agent types, worktree isolation, the child runner and the ``task`` tools."""

from __future__ import annotations

from k3code.subagents.runner import MAX_DEPTH, DepthLimit, Handle, SubagentManager
from k3code.subagents.types import AgentType, load_agent_types

__all__ = ["MAX_DEPTH", "AgentType", "DepthLimit", "Handle", "SubagentManager", "load_agent_types"]
