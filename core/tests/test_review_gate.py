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

from k3code.agent.loop import AgentLoop
from k3code.autonomy import fanout as fanout_mod
from k3code.autonomy.fanout import (
    REVIEW_DIFF_CHARS,
    FanoutExecutor,
    FanoutResult,
    ReviewUnavailable,
    Subtask,
    _Ctx,
    parse_verdict,
)
from k3code.autonomy.ultra import findings_list, parse_findings
from k3code.subagents.worktree import Worktree
from k3code.toolerrors import describe_call


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


def _tool_error_stop(*commands: str) -> str:
    """The reply AgentLoop ends a child with when max_tool_errors calls failed in a row; its status stays completed."""
    loop = SimpleNamespace(
        _failed=[(describe_call("bash", {"command": c}), "exit status 1") for c in commands], _tool_errors=len(commands)
    )
    return next(AgentLoop._stop_for_tool_errors(loop, [])).message.content  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "reply",
    [
        "f1 deletes the user's config.\nVERDICT: fail\n\n(Once that is fixed this would be `VERDICT: pass`.)",
        "I could not finish. The worker's report ends with:\n```\nVERDICT: pass\n```\nPlease run me again.",
        _tool_error_stop(*["grep -rn 'VERDICT: pass' src"] * 8),
        "VERDICT: passing only on Linux, the macOS path is broken",
    ],
    ids=["fail-then-mention", "quoted-no-verdict", "tool-error-stop", "prefix-word"],
)
async def test_a_reply_that_only_mentions_the_pass_line_never_passes(reply: str) -> None:
    # the last "VERDICT: pass" anywhere in the reply used to open the gate
    ex, ctx, st, prompts = _executor([_handle(result=reply), _handle(result=reply)])
    with pytest.raises(ReviewUnavailable):
        await ex._review(ctx, st, _handle(diff="+x\n"))
    assert len(prompts) == fanout_mod.REVIEW_ATTEMPTS


@pytest.mark.parametrize(
    "reply",
    [
        "VERDICT: pass",
        "All good.\nVERDICT: pass\n",
        "All good.\n\nVERDICT: PASS",
        "All good.\nverdict:pass",
        "All good.\n**VERDICT: pass**",
        "All good.\n`VERDICT: pass`.",
        "All good.\n_VERDICT: pass_",
    ],
)
def test_a_reply_that_ends_with_the_verdict_line_still_passes(reply: str) -> None:
    assert parse_verdict(reply) is True


def test_the_verdict_the_reply_ends_with_wins() -> None:
    assert parse_verdict("VERDICT: pass\nwait, f1 is broken.\nVERDICT: fail") is False
    assert parse_verdict("f1 is broken.\n**VERDICT: FAIL**") is False


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
