"""How many agents a task needs: plan steps -> independent units -> workers that fit the budget.

Pure functions, no I/O and no model call: the inputs are the plan text (its Steps, the optional ``(parallel)`` marks
the planners are asked for), the scope the classifier gave the task (when it ran) and what is left of the budget.
Nothing here can raise the limits of :mod:`k3code.autonomy.agents`; it only picks a number at or below them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from k3code.autonomy.plan_first import parse_plan

#: a run never fans out into more workers than this (the same bound ``extract_subtasks`` has always had)
MAX_SUBTASKS = 8
#: tail of an /ultracode run held back from the fan-out: two lens reviewers and one fixer
REVIEW_HOLD = 3
#: workers a scope can keep busy: bounds the serial per-merge test runs behind the merge lock
SCOPE_WORKERS = {"trivial": 1, "small": 1, "medium": 2, "large": 4, "huge": 6}
#: independent planners per scope (the judge is skipped for one)
SCOPE_PLANNERS = {"trivial": 1, "small": 1, "medium": 2, "large": 3, "huge": 3}
#: planner angles in the order a smaller panel keeps them
PLANNER_ANGLES = ("mvp-first", "risk-first", "architecture-first")
#: a worker plus its reviewer, on a guess of tokens per agent: only matters when the token budget is nearly spent
DEFAULT_TOKENS_PER_AGENT = 150_000

_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?:\d+[.)]|[-*])[ \t]+(?P<text>.+?)[ \t]*$", re.M)
_PARALLEL_RE = re.compile(r"\(\s*parallel\s*\)", re.I)
_DEP_RE = re.compile(r"\b(?:after|depends?\s+on|requires?|following|once)\s+(?:the\s+)?steps?\s+(\d+)", re.I)
_PATH_RE = re.compile(
    r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+(?![\w/-])"
    r"|\b[\w-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|go|rs|md|ya?ml|json|toml|sh|c|h|cpp|java|rb|css|html|sql)\b"
)


@dataclass(frozen=True)
class Step:
    """One top-level step of a plan."""

    title: str
    parallel: bool = False
    #: path-like tokens the step names (lower-cased)
    paths: frozenset[str] = frozenset()
    #: 1-based numbers of the steps this one says it waits for
    deps: tuple[int, ...] = ()


@dataclass(frozen=True)
class AgentPlan:
    """The sized fan-out of one run."""

    #: workers to start (0 = the budget cannot pay for one worker and its reviewer)
    workers: int
    #: how many run at once
    width: int
    #: the subtasks, packed to ``workers`` entries (empty when ``workers`` is 0)
    subtasks: list[str] = field(default_factory=list)
    #: units the plan split into before packing
    units: int = 0
    #: agents held back for the review tail (lenses and fixer)
    hold: int = 0
    #: why the count is what it is, for the progress line and the report
    reasons: list[str] = field(default_factory=list)

    def describe(self, steps: int, budget_text: str = "") -> str:
        text = f"{steps} step(s) -> {self.units} unit(s) -> {self.workers} worker(s), width {self.width}"
        return f"{text}; {budget_text}" if budget_text else text


def top_level_steps(plan: str) -> list[Step]:
    """The plan's top-level Steps: numbered or bulleted items at the outermost indent only (a sub-bullet is detail of
    its step, not a step of its own). ``(parallel)`` is removed from the title and kept as a flag."""
    steps_text = parse_plan(plan).get("Steps", "")
    items = [(len(m.group("indent").expandtabs(4)), m.group("text")) for m in _ITEM_RE.finditer(steps_text)]
    if not items:
        return []
    top = min(indent for indent, _ in items)
    out: list[Step] = []
    for indent, text in items:
        if indent != top:
            continue
        title = " ".join(_PARALLEL_RE.sub("", text).split())
        out.append(
            Step(
                title=title,
                parallel=bool(_PARALLEL_RE.search(text)),
                paths=frozenset(p.lower().strip("./") for p in _PATH_RE.findall(text)),
                deps=tuple(int(n) for n in _DEP_RE.findall(text)),
            )
        )
    return out


def group_steps(steps: list[Step]) -> list[list[Step]]:
    """The steps as independent units, in plan order: what must run one after another stays together.

    A step that says it waits for another joins it. When the planner marked some steps ``(parallel)`` they are
    separate units and the unmarked ones form ONE sequential unit; with no marks, steps that name the same file or
    directory join (they would conflict at merge time) and the others stay separate."""
    n = len(steps)
    root = list(range(n))

    def find(i: int) -> int:
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            root[max(ra, rb)] = min(ra, rb)

    for i, step in enumerate(steps):
        for dep in step.deps:
            if 1 <= dep <= n and dep - 1 != i:
                union(i, dep - 1)
    if any(s.parallel for s in steps):
        chain = [i for i, s in enumerate(steps) if not s.parallel]
        for a, b in zip(chain, chain[1:], strict=False):
            union(a, b)
    else:
        owner: dict[str, int] = {}
        for i, step in enumerate(steps):
            for path in step.paths:
                if path in owner:
                    union(i, owner[path])
                else:
                    owner[path] = i
    groups: dict[int, list[Step]] = {}
    for i, step in enumerate(steps):
        groups.setdefault(find(i), []).append(step)
    return list(groups.values())


def unit_text(unit: list[Step]) -> str:
    """One unit as the subtask a worker is given."""
    return unit[0].title if len(unit) == 1 else "; then ".join(s.title for s in unit) + " (in this order)"


def pack(units: list[str], n: int) -> list[str]:
    """``units`` as ``n`` subtasks: contiguous chunks joined in order, so no unit is dropped."""
    if n <= 0:
        return []
    if len(units) <= n:
        return list(units)
    size, extra = divmod(len(units), n)
    out: list[str] = []
    at = 0
    for i in range(n):
        take = size + (1 if i < extra else 0)
        chunk = units[at : at + take]
        at += take
        out.append(chunk[0] if len(chunk) == 1 else "; then ".join(chunk) + " (in this order)")
    return out


def pick_planners(scope: str | None) -> list[str]:
    """The planner angles to run: one for a small change, two for a medium one, all three otherwise (and when the
    task has no verdict, as with the explicit ``/ultracode <task>`` and its wake word)."""
    n = SCOPE_PLANNERS.get(scope or "", len(PLANNER_ANGLES))
    return list(PLANNER_ANGLES[:n])


def size_agents(
    units: list[str],
    *,
    whole: str,
    left: int | None,
    hold: int = REVIEW_HOLD,
    width_cap: int,
    scope: str | None = None,
    tokens_left: int | None = None,
    tokens_per_agent: int = DEFAULT_TOKENS_PER_AGENT,
) -> AgentPlan:
    """How many workers to start for ``units`` and how many at once.

    ``left`` is what remains of the agent budget after planning (None = no limit); each worker costs itself plus its
    reviewer, and ``hold`` agents stay free for the review panel and the fixer, so a wide plan can never starve the
    review that follows it. ``width_cap`` is the most children that may run at once (the governor's answer)."""
    reasons: list[str] = []
    g = len(units)
    if left is not None and left < 2:
        return AgentPlan(
            0, 0, [], g, 0, ["the agent budget has no room for a worker and its reviewer; raise it with /maxagents"]
        )
    if left is not None and (left - hold) // 2 < 1:
        hold = max(0, left - 2)
        reasons.append(f"review tail cut to {hold} agent(s): the budget is small")
    fit = (left - hold) // 2 if left is not None else MAX_SUBTASKS
    caps = {
        "plan steps": g,
        f"scope {scope}" if scope else "scope": SCOPE_WORKERS.get(scope or "", SCOPE_WORKERS["huge"]),
        "agent budget": fit,
        "limit": MAX_SUBTASKS,
    }
    if tokens_left is not None:
        caps["token budget"] = max(1, tokens_left // (2 * max(1, tokens_per_agent)))
    reason, n = min(caps.items(), key=lambda kv: kv[1])
    n = max(1, n)
    if n < g:
        reasons.append(f"{g} units packed into {n} worker(s) by the {reason}")
    subtasks = pack(units, n) if n >= 2 else [whole]
    workers = len(subtasks)
    return AgentPlan(workers, max(1, min(workers, width_cap)), subtasks, g, hold, reasons)
