"""/autocompact: set when a session compacts on its own (auto, off, a token limit or a share of the window)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from k3code import confio
from k3code.autonomy.advisor import SUMMARY_SYSTEM, transcript_text
from k3code.commands.autocompact_cmd import AutoCompactError, parse_changes
from k3code.config import Settings
from k3code.context_budget import AutoCompact, autocompact_policy, compact_threshold
from k3code.paths import user_config_path
from k3code.session_ai import SUMMARY_PREFIX
from m1cmd_helpers import cmd, make_server
from test_auto_compaction import Recording, long_history, seed

# ── the setting words ─────────────────────────────────────────────────


def test_the_words_a_user_types_become_context_keys():
    auto = {"autocompact": None, "compact_at_tokens": None, "compact_at_ratio": None}
    assert parse_changes("auto") == auto
    assert parse_changes("AUTO") == auto
    assert parse_changes("off") == {"autocompact": False}
    assert parse_changes("on") == {"autocompact": None}  # on again at the limit it had
    assert parse_changes("120k")["compact_at_tokens"] == 120_000
    assert parse_changes("1.5m")["compact_at_tokens"] == 1_500_000
    assert parse_changes("150,000")["compact_at_tokens"] == 150_000
    assert parse_changes("60%") == {"autocompact": None, "compact_at_tokens": None, "compact_at_ratio": 0.6}
    assert parse_changes("reset") is None


@pytest.mark.parametrize("word", ["soon", "12x", "-5", "", "k"])
def test_an_unknown_word_is_refused(word):
    with pytest.raises(AutoCompactError):
        parse_changes(word)


def test_limits_that_would_compact_every_turn_or_never_are_refused():
    with pytest.raises(AutoCompactError, match="at least 2,000"):
        parse_changes("500")
    with pytest.raises(AutoCompactError, match="write 60%"):  # a bare small number is most likely a percentage
        parse_changes("60")
    for pct in ("5%", "99%", "0%"):
        with pytest.raises(AutoCompactError, match="between 10% and 95%"):
            parse_changes(pct)


# ── the policy ────────────────────────────────────────────────────────


def test_the_policy_reads_the_config_and_a_session_overrides_it_as_a_whole():
    cfg = SimpleNamespace(models={}, context={})
    assert autocompact_policy(cfg) == AutoCompact()
    assert compact_threshold(cfg, "claude-x") == 140_000
    cfg.context = {"autocompact": False, "compact_at_tokens": 50_000}
    assert autocompact_policy(cfg) == AutoCompact(enabled=False, tokens=50_000)
    cfg.context = {"compact_at_ratio": 0.5}
    assert compact_threshold(cfg, "claude-x") == 100_000
    session = {"enabled": True, "tokens": 30_000, "ratio": None}
    assert compact_threshold(cfg, "claude-x", autocompact_policy(cfg, session)) == 30_000
    assert autocompact_policy(cfg, {"enabled": False, "tokens": None, "ratio": None}).mode == "off"


def test_a_value_that_is_no_positive_number_counts_as_unset():
    cfg = SimpleNamespace(models={}, context={"compact_at_tokens": "lots", "compact_at_ratio": 7})
    assert autocompact_policy(cfg) == AutoCompact()


def test_the_config_refuses_bad_values_when_it_loads_not_inside_a_turn():
    Settings(context={"autocompact": False, "compact_at_tokens": 90_000})
    Settings(context={"compact_at_ratio": 0.6, "keep_messages": 8})
    Settings(context={"compact_at_tokens": 0})  # "not set", as it always was
    for bad in (
        {"autocompact": "no"},
        {"compact_at_tokens": "150k"},
        {"compact_at_tokens": -5},
        {"compact_at_ratio": 0},
        {"compact_at_ratio": 1.5},
        {"compact_at_ratio": "high"},
        {"keep_messages": 0},
        {"compact_input_chars": "60k"},
    ):
        with pytest.raises(ValueError, match="context\\."):
            Settings(context=bad)


# ── the command ───────────────────────────────────────────────────────


async def test_status_says_when_the_session_compacts(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording())
    out = (await cmd(server, "/autocompact", live.session_id))["output"]
    assert "Auto-compact is on, auto: at 70% of the window" in out
    assert "left before it compacts" in out
    await server.close()


async def test_a_limit_is_saved_in_the_user_config_and_applies_at_once(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording(), context={"keep_messages": 6})
    res = await cmd(server, "/autocompact 3k", live.session_id)
    assert "Saved in" in res["output"] and "at 3,000 tokens" in res["output"]
    assert confio.read_yaml(user_config_path())["context"] == {"compact_at_tokens": 3000}
    assert res["autocompact"]["mode"] == "tokens" and res["autocompact"]["threshold"] == 3000
    assert server.config.context["compact_at_tokens"] == 3000  # the running gateway took it
    assert await server._maybe_compact(live) > 0  # the seeded history is ~8k tokens
    await server.close()


async def test_the_other_context_keys_survive_a_change(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording())
    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_config_path().write_text("context:\n  keep_messages: 5\n  compact_at_ratio: 0.5\nmodels: {}\n")
    await cmd(server, "/autocompact 200k", live.session_id)
    assert confio.read_yaml(user_config_path())["context"] == {"keep_messages": 5, "compact_at_tokens": 200_000}
    await cmd(server, "/autocompact auto", live.session_id)
    assert confio.read_yaml(user_config_path())["context"] == {"keep_messages": 5}
    await cmd(server, "/autocompact off", live.session_id)
    assert confio.read_yaml(user_config_path())["context"] == {"keep_messages": 5, "autocompact": False}
    await server.close()


async def test_off_compacts_nothing_on_its_own_and_on_restores_the_limit(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording())
    await cmd(server, "/autocompact 3k", live.session_id)  # saved in the user config, as /autocompact off will be
    await cmd(server, "/autocompact off", live.session_id)
    before = len(live.stored.messages)
    assert await server._maybe_compact(live) == 0 and len(live.stored.messages) == before
    out = (await cmd(server, "/autocompact", live.session_id))["output"]
    assert out.startswith("Auto-compact is off")
    res = await cmd(server, "/autocompact on", live.session_id)
    assert "at 3,000 tokens" in res["output"]
    assert await server._maybe_compact(live) > 0
    await server.close()


async def test_a_session_setting_is_not_saved_and_reset_drops_it(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording(), context={"compact_at_tokens": 3000})
    res = await cmd(server, "/autocompact off --session", live.session_id)
    assert "For this session only" in res["output"] and res["autocompact"]["session_only"]
    assert not user_config_path().exists() or "autocompact" not in confio.read_yaml(user_config_path()).get(
        "context", {}
    )
    assert live.stored.meta["autocompact"]["enabled"] is False
    assert await server._maybe_compact(live) == 0
    # a session setting built on the session's own: the limit it had stays
    await cmd(server, "/autocompact 40% --session", live.session_id)
    assert live.stored.meta["autocompact"] == {"enabled": True, "tokens": None, "ratio": 0.4}
    await cmd(server, "/autocompact reset --session", live.session_id)
    assert "autocompact" not in live.stored.meta
    assert await server._maybe_compact(live) > 0  # the config's 3000 again
    await server.close()


async def test_bad_input_prints_the_usage_and_changes_nothing(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording())
    for line in (
        "/autocompact soon",
        "/autocompact 10",
        "/autocompact 1 2",
        "/autocompact --bogus",
        "/autocompact reset",
    ):
        out = (await cmd(server, line, live.session_id))["output"]
        assert "/autocompact" in out, line
    assert not user_config_path().exists()
    await server.close()
    bare, _ = make_server(tmp_path / "bare", monkeypatch)  # no session was ever created in this gateway
    assert "No active session" in (await cmd(bare, "/autocompact 60% --session"))["output"]
    await bare.close()


async def test_a_context_overflow_is_not_rescued_when_auto_compact_is_off(tmp_path, monkeypatch):
    """Off means the user compacts: the turn fails with the overflow (the TUI says to run /compress) instead of the
    gateway summarizing the conversation behind their back."""
    from k3code.providers.base import ProviderError

    class Overflowing(Recording):
        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            if "NEWPROMPT" in "\n".join(str(m.content or "") for m in messages) and self.sizes:
                raise ProviderError(
                    "maximum context length is 1000 tokens, however you requested 9000", status_code=400
                )
            async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
                yield event

    provider = Overflowing()
    provider.sizes.append(0)
    server, live = await seed(tmp_path, monkeypatch, provider, context={"autocompact": False, "keep_messages": 6})
    status, _ = await server._run_turn(live, "NEWPROMPT please")
    assert status == "error"
    assert not any(str(m["content"]).startswith(SUMMARY_PREFIX) for m in live.stored.messages)
    await server.close()


# ── /compact ──────────────────────────────────────────────────────────


async def test_compact_takes_a_focus_and_reports_the_saving(tmp_path, monkeypatch):
    seen: list[str] = []

    class Spy(Recording):
        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            seen.append(str(messages[0].content))
            async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
                yield event

    server, live = await seed(tmp_path, monkeypatch, Spy(), context={"keep_messages": 6})
    out = (await cmd(server, "/compact the failing migration", live.session_id))["output"]
    assert out.startswith("Compacted ") and "tokens, was ~" in out
    assert seen and seen[0].startswith(SUMMARY_SYSTEM) and "focus on: the failing migration" in seen[0]
    assert live.stored.meta["compactions"] == 1
    await server.close()


async def test_compact_follows_the_configured_keep_window(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording(), context={"keep_messages": 20})
    await cmd(server, "/compact", live.session_id)
    kept = [m for m in live.stored.messages if not str(m["content"]).startswith(SUMMARY_PREFIX)]
    assert len(live.stored.messages) >= 20  # /compact used a fixed 6 and ignored context.keep_messages
    assert kept[-1]["content"].startswith("answer 39")
    await server.close()


async def test_a_second_compaction_counts(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording(), context={"keep_messages": 6})
    await cmd(server, "/compact", live.session_id)
    live.stored.messages.extend(long_history(30)[1:])
    await cmd(server, "/compact", live.session_id)
    assert live.stored.meta["compactions"] == 2
    await server.close()


# ── what a summarizer reads ───────────────────────────────────────────


def test_the_transcript_names_the_tool_calls_and_their_results():
    text = transcript_text(
        [
            {"role": "user", "content": "fix the import"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1", "name": "edit", "arguments": {"path": "a.py"}}],
            },
            {"role": "tool", "name": "edit", "tool_call_id": "1", "content": "edited a.py"},
        ]
    )
    assert text.splitlines() == [
        "user: fix the import",
        'assistant called: edit({"path": "a.py"})',
        "tool edit: edited a.py",
    ]


# ── what the clients are told ─────────────────────────────────────────


async def test_session_usage_reports_the_context_the_status_bar_draws(tmp_path, monkeypatch):
    from m1cmd_helpers import rpc

    server, live = await seed(tmp_path, monkeypatch, Recording(), context={"compact_at_tokens": 5000})
    res = (await rpc(server, "session.usage", {"session_id": live.session_id}))["result"]
    assert res["context_max"] == 128_000 and res["context_estimated"] is True
    assert res["context_used"] > 8_000 and res["context_percent"] == round(100 * res["context_used"] / 128_000)
    assert res["autocompact_at"] == 5000 and res["compressions"] == 0
    assert {"cache_read", "cache_write", "cache_hit_pct"} <= set(res)
    await cmd(server, "/autocompact off --session", live.session_id)
    assert (await rpc(server, "session.usage", {"session_id": live.session_id}))["result"]["autocompact_at"] is None
    await server.close()


async def test_a_turn_reports_its_context_and_an_automatic_compaction_says_so(tmp_path, monkeypatch):
    from m1cmd_helpers import frames_of

    server, live = await seed(
        tmp_path, monkeypatch, Recording(), context={"compact_at_tokens": 2000, "keep_messages": 6}
    )
    await server._run_turn(live, "next request")
    frames = frames_of(server)
    kinds = [f["params"]["payload"]["kind"] for f in frames if f.get("params", {}).get("type") == "status.update"]
    assert "compacting" in kinds and kinds.index("compacting") < kinds.index("compacted")
    usages = [f["params"]["payload"]["usage"] for f in frames if f.get("params", {}).get("type") == "session.usage"]
    assert usages and all(u["context_max"] == 128_000 for u in usages)
    assert usages[-1]["compressions"] == 1
    await server.close()
