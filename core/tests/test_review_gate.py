"""The fan-out reviewer gate and the ultracode review panel fail closed (issue #97).

A reviewer that failed, was stopped, hit a budget or answered without a verdict used to count as a pass, and a dead
review lens read as "review panel found nothing".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from k3code.autonomy import fanout as fanout_mod
from k3code.autonomy.fanout import REVIEW_DIFF_CHARS, FanoutExecutor, FanoutResult, ReviewUnavailable, Subtask, _Ctx
from k3code.autonomy.ultra import findings_list, parse_findings
from k3code.subagents.worktree import Worktree


class _Session:
    session_id = "s1"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, kind: str, payload: dict[str, Any], **_: Any) -> None:
        self.events.append((kind, payload))


def _handle(status: str = "completed", result: str = "", **kw: Any) -> SimpleNamespace:
    base = {"id": f"h{id(object())}", "error": "", "diff": "", "diff_stat": "", "worktree": None, "merge": "merged"}
    return SimpleNamespace(**{**base, **kw, "status": status, "result": result})


def _executor(replies: list[SimpleNamespace]) -> tuple[FanoutExecutor, _Ctx, Subtask, list[str]]:
    """An executor whose children are scripted: workers complete with a change, reviewers answer ``replies``."""
    ex = FanoutExecutor(SimpleNamespace(config={}))
    st = Subtask("t1", "SUB-ONE add f1")
    result = FanoutResult("fo-test", [st])
    ctx = _Ctx(
        _Session(), "task", "## Steps\n1. a\n2. b", result, None, "", 60.0, asyncio.Semaphore(1), asyncio.Lock(), ex
    )
    prompts: list[str] = []
    wt = Worktree("w1", Path("/repo"), Path("/repo/.k3code/worktrees/w1"), "k3/w1", "abc123")

    async def spawn(_ctx: _Ctx, _st: Subtask, agent_type: str, prompt: str, _desc: str, **_: Any) -> Any:
        if agent_type == "worker":
            return _handle(result="worker done", worktree=wt, diff="+x\n")
        prompts.append(prompt)
        return replies.pop(0)

    ex._spawn = spawn  # type: ignore[method-assign]
    return ex, ctx, st, prompts


async def test_an_explicit_pass_opens_the_gate() -> None:
    ex, ctx, st, prompts = _executor([_handle(result="looks right. VERDICT: pass")])
    assert await ex._review(ctx, st, _handle(diff="+x\n")) == ""
    assert len(prompts) == 1


async def test_a_fail_verdict_returns_the_findings() -> None:
    ex, ctx, st, _ = _executor([_handle(result="f1 is wrong. VERDICT: fail")])
    assert "f1 is wrong" in await ex._review(ctx, st, _handle(diff="+x\n"))


@pytest.mark.parametrize(
    "replies",
    [
        [_handle("failed", error="provider outage"), _handle("failed", error="provider outage")],
        [_handle("interrupted"), _handle("completed", result="")],
        [_handle(result="I looked at it."), _handle(result="Seems fine overall.")],
    ],
    ids=["failed", "interrupted-then-empty", "no-verdict"],
)
async def test_a_review_without_a_verdict_never_passes(replies: list[SimpleNamespace]) -> None:
    ex, ctx, st, prompts = _executor(list(replies))
    with pytest.raises(ReviewUnavailable):
        await ex._review(ctx, st, _handle(diff="+x\n"))
    assert len(prompts) == fanout_mod.REVIEW_ATTEMPTS  # asked once more before giving up


async def test_a_reviewer_without_a_verdict_is_asked_once_more() -> None:
    ex, ctx, st, prompts = _executor([_handle("failed", error="429"), _handle(result="ok now. VERDICT: pass")])
    assert await ex._review(ctx, st, _handle(diff="+x\n")) == ""
    assert len(prompts) == 2


async def test_the_reviewer_is_told_where_the_change_is_and_when_the_diff_is_cut() -> None:
    ex, ctx, st, prompts = _executor([_handle(result="VERDICT: pass"), _handle(result="VERDICT: pass")])
    wt = Worktree("w1", Path("/repo"), Path("/repo/.k3code/worktrees/w1"), "k3/w1", "abc123")
    await ex._review(ctx, st, _handle(diff="+small\n", worktree=wt))
    assert "k3/w1" in prompts[0] and str(wt.path) in prompts[0] and "CUT" not in prompts[0]
    big = "+" + "x" * (REVIEW_DIFF_CHARS + 500) + "\n"
    await ex._review(ctx, st, _handle(diff=big, worktree=wt))
    assert f"CUT at {REVIEW_DIFF_CHARS} of {len(big)} characters" in prompts[1]


async def test_a_subtask_whose_review_never_answers_is_escalated_not_merged() -> None:
    ex, ctx, st, prompts = _executor([_handle("failed", error="budget"), _handle(result="no verdict here")])
    merged: list[str] = []

    async def merge(_ctx: _Ctx, s: Subtask) -> None:
        merged.append(s.id)

    ex._merge = merge  # type: ignore[method-assign]
    await ex._one(ctx, st)
    assert merged == [] and st.state == "escalated"
    assert "no review verdict" in st.detail
    assert len(prompts) == 2  # no rework of the worker: the work was never judged


def test_a_lens_reply_without_a_findings_array_is_not_an_empty_review() -> None:
    assert findings_list("[]") == [] and parse_findings("[]") == []
    assert findings_list("I could not finish the review.") is None
    assert findings_list("") is None and parse_findings("") == []
    found = findings_list('notes [{"file": "a", "line": 1, "severity": "high", "issue": "bug"}]')
    assert found is not None and found[0]["issue"] == "bug"
