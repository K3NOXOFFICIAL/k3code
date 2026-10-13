"""The background session review keeps firing after the conversation was compacted (issue #117).

Compaction replaces the stored list with the task anchor, a summary and the last messages, so a count of the list's
user messages falls from 60 to about 6 while the review's watermark stays at 55: the review never ran again, in the
long sessions that hold the most project knowledge. And it clipped the summary to 600 characters.
"""

from __future__ import annotations

import json

from k3code.autonomy.proposals import ProposalStore
from k3code.learning import review
from k3code.session_ai import SUMMARY_PREFIX, compact_messages
from learn_helpers import FakeCaller
from test_learning_gateway import call, make_server

REPLY = json.dumps({"facts": ["tests run with `uv run pytest -q`"], "skills": []})
SUMMARY_END = "END-OF-THE-SUMMARY"


def exchange(i: int) -> list[dict]:
    return [{"role": "user", "content": f"request {i}"}, {"role": "assistant", "content": f"answer {i}"}]


async def compacted(n: int = 60, keep: int = 8) -> list[dict]:
    """``n`` exchanges after what the daemon's compaction does to them (a summary longer than the old clip)."""
    messages = [m for i in range(n) for m in exchange(i)]
    summary = FakeCaller("the work so far: " + "x" * 2000 + SUMMARY_END)
    new, folded = await compact_messages(summary, messages, keep=keep)
    assert folded and len(new) < 15
    return new


async def session(tmp_path, monkeypatch, caller):
    server, _ = make_server(tmp_path, ["unused"], monkeypatch)
    server.model_caller = caller
    sess = await call(server, "session.create", {"cwd": str(tmp_path)})
    return server, server.live[sess["session_id"]]


async def finish_turns(server, live, start: int, count: int) -> None:
    """What a running session does: a user message and its answer are stored, then the turn is reported finished."""
    for i in range(start, start + count):
        live.stored.messages.extend(exchange(i))
        await server.learning.turn_finished(live, "done")


async def test_a_compacted_session_is_reviewed_again(tmp_path, monkeypatch):
    caller = FakeCaller(REPLY)
    server, live = await session(tmp_path, monkeypatch, caller)
    await finish_turns(server, live, 0, 60)
    reviews = len(caller.calls)
    assert reviews > 0  # an ordinary long session is reviewed all along

    live.stored.messages = await compacted()
    await finish_turns(server, live, 60, 6)  # review_min_turns more turns

    assert len(caller.calls) > reviews, "no review ran after the compaction"
    assert SUMMARY_END in caller.calls[-1][1].content  # the review reads the whole summary
    await server.close()


async def test_a_watermark_left_by_an_older_version_does_not_stop_the_review(tmp_path, monkeypatch):
    """A session compacted before the turn count was kept has reviewed_turns far above its list's user messages."""
    caller = FakeCaller(REPLY)
    server, live = await session(tmp_path, monkeypatch, caller)
    live.stored.messages = await compacted()
    live.stored.meta["reviewed_turns"] = 60

    await finish_turns(server, live, 60, 6)

    assert len(caller.calls) >= 1
    await server.close()


async def test_a_session_is_not_reviewed_on_every_turn(tmp_path, monkeypatch):
    caller = FakeCaller(REPLY)
    server, live = await session(tmp_path, monkeypatch, caller)
    live.stored.messages = await compacted()

    await finish_turns(server, live, 60, 12)

    assert 1 <= len(caller.calls) <= 3  # one review per review_min_turns turns, not one per turn
    await server.close()


async def test_review_session_takes_the_turn_count_of_the_whole_session(tmp_path):
    """The list the review reads is the compacted one: its own user messages are too few to pass the length gate."""
    caller = FakeCaller(REPLY)
    short = await compacted()
    assert review.user_turns(short) <= 6
    skipped = await review.review_session(caller, short, store=ProposalStore(tmp_path), cwd=tmp_path, min_turns=6)
    assert skipped["skipped"] and not caller.calls

    done = await review.review_session(
        caller, short, store=ProposalStore(tmp_path), cwd=tmp_path, min_turns=6, turns=61
    )
    assert not done["skipped"] and len(caller.calls) == 1


async def test_the_summary_is_clipped_larger_than_an_ordinary_message():
    messages = await compacted()
    text = review.transcript(messages)
    assert SUMMARY_PREFIX.strip() in text and SUMMARY_END in text
    long_answer = [{"role": "assistant", "content": "y" * 700 + "TAIL"}]
    assert "TAIL" not in review.transcript(long_answer)  # an ordinary message is still cut at 600


def test_the_session_total_follows_the_list_not_the_calls():
    """The hook is spawned as a task: it runs late, and several times in a row against the same list (a goal loop)."""
    meta: dict = {}
    messages = [m for i in range(10) for m in exchange(i)]
    assert [review.session_turns(meta, messages) for _ in range(3)] == [10, 10, 10]

    tail = messages[-6:]  # compaction left 3 of the 10 user messages
    assert review.session_turns(meta, tail) == 10
    assert review.session_turns(meta, tail + exchange(10)) == 11
