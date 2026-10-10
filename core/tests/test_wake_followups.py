"""Wake-word follow-ups: typed ultracode mode phrasings, and hook context kept out of titles and headings."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from k3code.commands.research_cmd import UltraResearchCommand
from k3code.commands.ultra_cmd import UltraCodeCommand, typed_mode_word
from k3code.wakewords import detect
from test_autonomy_gateway import call, make
from test_research import FakeTools, research_server
from test_ultra import JUDGE, PLANNERS

# what a UserPromptSubmit hook running `git status` adds: several lines
HOOK_CTX = "On branch main\nChanges not staged for commit:\n\tmodified:   HOOK-LINE.py"


# ── what a typed "ultracode ..." leaves is mode control or a task ──


@pytest.mark.parametrize(
    ("typed", "word"),
    [
        ("ultracode off", "off"),
        ("Ultracode OFF.", "off"),
        ("turn off ultracode", "off"),
        ("ultracode on", "on"),
        ("ultracode mode on", "on"),
        ("show ultracode status", "status"),
        ("show ultracode mode", "status"),  # only filler around the wake word: show the mode
        ("ultracode mode", "status"),
        ("ultracode mode?", "status"),
        ("is ultracode on?", "status"),  # a question, not a switch
        ("is ultracode off", "status"),
        ("Is ultracode mode on?", "status"),
        ("ultracode", None),  # the word alone stays the usage line
        ("ultracode on the auth module", None),
        ("ultracode off-by-one in the parser", None),
        ("ultracode status page for the API", None),
        ("ultracode is on fire, fix it", None),
        ("is ultracode on the roadmap", None),
        ("turn off the cache in ultracode", None),
    ],
)
def test_typed_mode_word_tells_mode_control_from_a_task(typed, word):
    hit = detect(typed)
    assert hit is not None and hit.mode == "ultracode"
    assert typed_mode_word(hit.task) == word


def test_other_wake_words_leave_mode_words_as_their_task():
    hit = detect("ultraresearch off")
    assert hit is not None and hit.mode == "ultraresearch" and hit.task == "off"  # the gateway asks only ultracode


# ── the hook context reaches the agents' prompts, never a title, heading or report line ──


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def ultracode(self, live: Any, task: str, *, context: str = "") -> str:
        self.calls.append(("ultracode", task, context))
        return "done"

    async def run(self, live: Any, question: str, *, n_sub: int | None = None, context: str = "") -> Any:
        self.calls.append(("ultraresearch", question, context))
        return SimpleNamespace(report="r", path="r.md")


async def test_commands_hand_the_pipeline_the_task_and_the_hook_context_apart():
    rec = _Recorder()

    async def unavailable_reason() -> str:
        return ""

    ctx = SimpleNamespace(
        ultra=rec,
        research=SimpleNamespace(
            run=rec.run, tools=lambda: SimpleNamespace(name="t", unavailable_reason=unavailable_reason)
        ),
    )
    spec = await UltraCodeCommand().prepare(ctx, object(), "fix the login form", context=HOOK_CTX)
    await spec.factory()
    spec = await UltraResearchCommand().prepare(ctx, object(), "--n 2 why is it slow?", context=HOOK_CTX)
    await spec.factory()
    assert rec.calls == [
        ("ultracode", "fix the login form", HOOK_CTX),
        ("ultraresearch", "why is it slow?", HOOK_CTX),
    ]


def _prompts(server: Any, marker: str) -> list[str]:
    return [c["text"] for p in server.providers for c in p.log if marker in c["text"]]


async def test_ultraplan_and_ultracode_keep_the_hook_context_out_of_titles(tmp_path, monkeypatch):
    # three agents: the planners run, then the budget stops ultracode before any worker
    server = make(
        tmp_path,
        monkeypatch,
        [*PLANNERS, JUDGE],
        mode="auto",
        autonomy={"plan_first": False},
        ultracode={"max_agents": 3},
    )
    await call(server, "session.create", {"cwd": str(tmp_path)})
    report = await server.ultra.ultracode(server.session, "add three files", context=HOOK_CTX)

    planners, judge = _prompts(server, "ANGLE:"), _prompts(server, "You are the plan judge")
    assert len(planners) == 3 and all("HOOK-LINE" in p for p in [*planners, *judge]) and judge
    (plan,) = server.artifacts.list(kind="plan")
    assert plan.title == "add three files"
    assert Path(plan.path).read_text(encoding="utf-8").splitlines()[0] == "# Plan: add three files"
    assert "agent budget exhausted" in report
    assert "\nTask: add three files\n" in report and "HOOK-LINE" not in report
    (review,) = server.artifacts.list(kind="review")
    assert review.title == "ultracode add three files"


async def test_ultraresearch_keeps_the_hook_context_out_of_the_heading_and_title(tmp_path, monkeypatch):
    tools = FakeTools()
    server = research_server(tmp_path, monkeypatch, tools)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    question = "What colour are the sky and grass?"
    res = await server.research.run(server.session, question, n_sub=2, context=HOOK_CTX)

    assert all("HOOK-LINE" in p for p in _prompts(server, "You are a research planner"))
    assert all("HOOK-LINE" in p for p in _prompts(server, "You are a research writer"))
    assert _prompts(server, "You are a research writer")
    assert not any("HOOK-LINE" in q for q in tools.searches)
    (art,) = server.artifacts.list(kind="research")
    assert art.title == question and res.state.question == question
    assert Path(art.path).read_text(encoding="utf-8").splitlines()[0] == f"# Research: {question}"
