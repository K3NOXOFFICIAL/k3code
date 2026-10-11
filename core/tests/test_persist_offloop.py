"""Persisting a turn runs off the event loop: a big transcript no longer freezes streaming, hooks and other sessions.

Mid-turn checkpoints are throttled, never overlap, and can never land after the write that ends the turn."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from k3code.providers.types import Message


def _big_messages(n: int = 60, size: int = 200_000) -> list[dict]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * size} for i in range(n)]


async def test_a_large_save_runs_on_another_thread_and_the_loop_keeps_ticking(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    sess = store.create(title="t", cwd="/p")
    sess.messages = _big_messages()
    seen: list[int] = []
    real = SessionStore.save

    def spy(self, s):
        seen.append(threading.get_ident())
        real(self, s)

    SessionStore.save = spy  # type: ignore[method-assign]
    try:
        ticks = 0
        done = False

        async def ticker() -> None:
            nonlocal ticks
            while not done:
                ticks += 1
                await asyncio.sleep(0)

        task = asyncio.create_task(ticker())
        await store.save_async(sess)
        done = True
        await task
    finally:
        SessionStore.save = real  # type: ignore[method-assign]
    assert seen and seen[0] != threading.get_ident()
    assert ticks > 5, ticks  # a save on the loop thread would have left no tick at all
    assert len(store.get(sess.session_id).messages) == 60
    store.close()


async def test_checkpoint_writes_only_the_transcript(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    sess = store.create(title="old", model="", cwd="/p")
    sess.title = "renamed"
    store.save(sess)
    await store.checkpoint_async(sess.session_id, [{"role": "user", "content": "hi"}], "m1")
    row = store.get(sess.session_id)
    assert row.title == "renamed" and row.model == "m1" and row.messages == [{"role": "user", "content": "hi"}]
    await store.checkpoint_async(sess.session_id, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}], "m2")
    assert store.get(sess.session_id).model == "m1"  # a model already on the row is kept
    store.delete(sess.session_id)
    await store.checkpoint_async(sess.session_id, [{"role": "user", "content": "zombie"}], "m")
    assert store.get(sess.session_id) is None  # a deleted session stays deleted
    store.close()


def _server(tmp_path) -> GatewayServer:
    srv = GatewayServer.__new__(GatewayServer)
    srv.store = SessionStore(tmp_path / "s.db")
    srv._chain_key = "m"
    srv.config = SimpleNamespace(default_model="m")
    srv.CHECKPOINT_EVERY_S = 0.0
    return srv


def _live(srv: GatewayServer) -> SimpleNamespace:
    stored = srv.store.create(title="t", model="m", cwd="/p")
    loop = SimpleNamespace(turn_messages=[Message(role="user", content="one")])
    return SimpleNamespace(
        session_id=stored.session_id, stored=stored, loop=loop, last_checkpoint=0.0, checkpoint_task=None
    )


async def test_checkpoints_never_overlap(tmp_path):
    srv = _server(tmp_path)
    live = _live(srv)
    gate = asyncio.Event()
    calls: list[int] = []

    async def slow(session_id, messages, model=""):
        calls.append(len(messages))
        await gate.wait()

    srv.store.checkpoint_async = slow  # type: ignore[method-assign]
    srv._checkpoint_turn(live)
    live.loop.turn_messages.append(Message(role="assistant", content="two"))
    srv._checkpoint_turn(live)  # the first write is still in flight
    await asyncio.sleep(0)
    assert calls == [1]
    gate.set()
    await live.checkpoint_task
    live.loop.turn_messages.append(Message(role="user", content="three"))
    srv._checkpoint_turn(live)
    await live.checkpoint_task
    assert calls == [1, 3]


async def test_a_stale_checkpoint_cannot_land_after_the_end_of_the_turn(tmp_path):
    srv = _server(tmp_path)
    live = _live(srv)
    real = SessionStore.checkpoint

    def slow_checkpoint(self, session_id, messages, model=""):
        threading.Event().wait(0.2)  # still writing the 1-message checkpoint when the turn ends
        real(self, session_id, messages, model)

    SessionStore.checkpoint = slow_checkpoint  # type: ignore[method-assign]
    try:
        srv._checkpoint_turn(live)
        live.loop.turn_messages.append(Message(role="assistant", content="final"))
        await srv._persist_turn(live, live.loop)
    finally:
        SessionStore.checkpoint = real  # type: ignore[method-assign]
    assert [m["content"] for m in srv.store.get(live.session_id).messages] == ["one", "final"]
    srv.store.close()


async def test_cancelling_the_turn_does_not_drop_the_final_write(tmp_path):
    srv = _server(tmp_path)
    live = _live(srv)
    live.loop.turn_messages.append(Message(role="assistant", content="before the stop"))
    task = asyncio.create_task(srv._persist_turn(live, live.loop))
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    srv.store.close()  # joins the writer thread: every queued write has landed
    again = SessionStore(tmp_path / "s.db")
    assert [m["content"] for m in again.get(live.session_id).messages] == ["one", "before the stop"]
    again.close()
