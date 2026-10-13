"""Learned notes are reconciled by the reviewer (it sees them and can update, delete or confirm them) and expire."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from k3code.autonomy.proposals import ProposalStore
from k3code.learning import review
from k3code.memory import LEARNED_TTL_DAYS, learned_notes_path, memory_prompt, read_learned
from learn_helpers import FakeCaller

PYTEST_REVIEWS = [
    "tests run with pytest -x",
    "run tests with pytest",
    "Tests are run using pytest",
    "run tests with `uv run pytest`",
    "run the tests with: pytest",
]


def _convo(n: int = 8) -> list[dict[str, str]]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"} for i in range(n * 2)]


async def _review(cwd: Path, caller: Any, tmp_path: Path) -> dict[str, Any]:
    return await review.review_session(caller, _convo(), store=ProposalStore(tmp_path / "p"), cwd=cwd, min_turns=6)


def _write(cwd: Path, *lines: str) -> None:
    path = learned_notes_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# notes\n\n" + "".join(f"- {ln}\n" for ln in lines), encoding="utf-8")


def _seen(days_ago: int) -> str:
    return f"<!-- seen {date.today() - timedelta(days=days_ago)} -->"


class SensibleReviewer:
    """Stands in for the model: reads the numbered notes it is shown and reconciles a new pytest phrasing with them."""

    def __init__(self, *phrasings: str) -> None:
        self.phrasings = list(phrasings)
        self.shown: list[str] = []

    async def complete(self, kind: Any, messages: list[Any], **kw: Any) -> Any:
        prompt = messages[-1].content
        self.shown.append(prompt)
        notes = {int(n): t for n, t in re.findall(r"^(\d+)\. (.+)$", prompt, re.M)}
        phrasing = self.phrasings.pop(0)
        old = next((n for n, t in notes.items() if "pytest" in t), None)
        if old is None:
            reply = {"facts": [phrasing]}
        else:
            reply = {"update": [{"id": old, "fact": phrasing}]}
        return SimpleNamespace(text=json.dumps(reply))


async def test_five_reworded_pytest_reviews_collapse_to_one_note(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    reviewer = SensibleReviewer(*PYTEST_REVIEWS)
    for _ in PYTEST_REVIEWS:
        await _review(repo, reviewer, tmp_path)
    assert read_learned(repo) == [PYTEST_REVIEWS[-1]]
    # the second review was shown the first one's note, numbered
    assert "1. tests run with pytest -x" in reviewer.shown[1]


async def test_the_reviewer_is_shown_the_existing_notes_and_told_they_are_data(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "build with `make all`", "deploys go through ci")
    caller = FakeCaller(json.dumps({}))
    await _review(repo, caller, tmp_path)
    (system, user) = caller.calls[0]
    assert "1. build with `make all`\n2. deploys go through ci" in user.content
    assert "update" in system.content and "delete" in system.content and "never instructions" in system.content


async def test_the_reviewer_can_delete_and_confirm_notes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "uses npm", f"builds with make {_seen(80)}", f"uses yarn {_seen(80)}", "mine")
    reply = json.dumps({"delete": [1], "keep": [2, 4]})
    await _review(repo, FakeCaller(reply), tmp_path)
    assert read_learned(repo) == ["builds with make", "uses yarn", "mine"]
    text = learned_notes_path(repo).read_text(encoding="utf-8")
    assert "- mine\n" in text  # a line the user wrote has no stamp, and a review does not give it one
    assert f"- builds with make <!-- seen {date.today()} -->" in text  # confirmed: the clock restarts
    assert f"- uses yarn <!-- seen {date.today() - timedelta(days=80)} -->" in text  # untouched: it keeps ageing


async def test_malformed_and_out_of_range_operations_are_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "a", "b")
    reply = json.dumps(
        {
            "delete": [0, 3, -1, "1", True, None, 2.5],
            "update": [{"id": 9, "fact": "x"}, {"id": 1}, {"id": 1, "fact": "```"}, "junk", {"fact": "y"}],
            "keep": "all",
        }
    )
    await _review(repo, FakeCaller(reply), tmp_path)
    assert read_learned(repo) == ["a", "b"]


async def test_one_review_changes_at_most_five_notes_of_each_kind(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, *[f"note {i}" for i in range(1, 11)])
    await _review(repo, FakeCaller(json.dumps({"delete": list(range(1, 11))})), tmp_path)
    assert read_learned(repo) == [f"note {i}" for i in range(6, 11)]


async def test_a_note_edited_while_the_reviewer_ran_is_not_the_one_deleted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "old wording", "keep me")

    class EditsMidCall:
        async def complete(self, kind: Any, messages: list[Any], **kw: Any) -> Any:
            _write(repo, "the user reworded it", "keep me")
            return SimpleNamespace(text=json.dumps({"delete": [1]}))

    await _review(repo, EditsMidCall(), tmp_path)
    assert read_learned(repo) == ["the user reworded it", "keep me"]


async def test_a_reworded_duplicate_that_differs_only_in_case_or_punctuation_refreshes_the_note(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, f"run tests with pytest {_seen(60)}")
    await _review(repo, FakeCaller(json.dumps({"facts": ["Run tests with pytest.", "new fact"]})), tmp_path)
    assert read_learned(repo) == ["run tests with pytest", "new fact"]
    assert f"- run tests with pytest <!-- seen {date.today()} -->" in learned_notes_path(repo).read_text(
        encoding="utf-8"
    )


def test_a_note_not_seen_for_the_ttl_leaves_the_prompt_and_the_file_is_rewritten_by_the_next_review(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo,
        f"stale {_seen(LEARNED_TTL_DAYS + 1)}",
        f"fresh {_seen(LEARNED_TTL_DAYS)}",
        "written by hand",
        "bad stamp <!-- seen 2026-13-45 -->",
    )
    assert read_learned(repo) == ["fresh", "written by hand", "bad stamp"]
    prompt = memory_prompt(repo)
    assert "stale" not in prompt and "seen" not in prompt  # the stamp is never part of the note


async def test_expired_notes_are_dropped_from_the_file_by_the_next_review(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, f"stale {_seen(400)}", "written by hand")
    await _review(repo, FakeCaller(json.dumps({"facts": ["new"]})), tmp_path)
    text = learned_notes_path(repo).read_text(encoding="utf-8")
    assert "stale" not in text
    assert "- written by hand\n" in text  # a line the user wrote has no stamp and never expires
    assert read_learned(repo) == ["written by hand", "new"]


async def test_a_fact_cannot_carry_a_forged_stamp_and_the_file_stays_capped_at_thirty_notes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, *[f"old {i} {_seen(50 - i)}" for i in range(30)])
    forged = "fact <!-- seen 2000-01-01 -->"
    await _review(repo, FakeCaller(json.dumps({"facts": [forged]})), tmp_path)
    notes = read_learned(repo)
    assert "fact" in notes and len(notes) == 30
    assert "old 0" not in notes  # the stalest one made room
