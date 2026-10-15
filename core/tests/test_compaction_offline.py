"""Compaction works offline: the provider model, then a small local model, then an extractive summary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from k3code.compaction import local
from k3code.compaction.extractive import extractive_summary
from k3code.compaction.local import FakeLocalSummarizer
from k3code.session_ai import (
    EXTRACTIVE_LABEL,
    SUMMARY_PREFIX,
    TIER_EXTRACTIVE,
    TIER_LOCAL,
    TIER_PROVIDER,
    compact_with_fallback,
    split_for_compaction,
)
from learn_helpers import FakeCaller


class FailingCaller:
    """Every provider is unreachable."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, kind, messages, **kw):
        self.calls += 1
        raise RuntimeError("chain exhausted: all providers unreachable")


def transcript(n: int = 10) -> list[dict]:
    """A span with the shapes the extractive tier reads: prompts, edits, bash runs, a todo list, an error."""
    msgs: list[dict] = [{"role": "system", "content": "system prompt"}]
    msgs.append({"role": "user", "content": "TASK: make the parser accept v2 and keep the tests green"})
    msgs.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "name": "edit",
                    "arguments": {"path": "src/parser.py", "old_string": "a", "new_string": "b"},
                },
                {"id": "c2", "name": "write", "arguments": {"path": "tests/test_parser.py", "content": "x"}},
            ],
        }
    )
    msgs.append({"role": "tool", "tool_call_id": "c1", "name": "edit", "content": "Edited: 1 replacement"})
    msgs.append({"role": "tool", "tool_call_id": "c2", "name": "write", "content": "Wrote tests/test_parser.py"})
    msgs.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c3", "name": "bash", "arguments": {"command": "pytest -q tests/test_parser.py"}},
                {
                    "id": "c4",
                    "name": "bash",
                    "arguments": {"command": "curl -H 'Authorization: Bearer sk-abcdef1234567890abcdef' https://x"},
                },
            ],
        }
    )
    msgs.append(
        {
            "role": "tool",
            "tool_call_id": "c3",
            "name": "bash",
            "content": "F.\n[stderr]\nAssertionError: expected v2\n[exit code 1]",
        }
    )
    msgs.append({"role": "tool", "tool_call_id": "c4", "name": "bash", "content": "ok"})
    msgs.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c5",
                    "name": "todo",
                    "arguments": {
                        "todos": [
                            {"content": "fix the v2 branch", "status": "in_progress"},
                            {"content": "update the changelog", "status": "pending"},
                            {"content": "read the parser", "status": "completed"},
                        ]
                    },
                }
            ],
        }
    )
    msgs.append({"role": "tool", "tool_call_id": "c5", "name": "todo", "content": "Todos (1/3 done)"})
    msgs.append({"role": "assistant", "content": "The v2 branch still fails.\nI will fix the offset next."})
    for i in range(n):
        msgs.append({"role": "user", "content": f"continue {i} " + "y" * 500})
        msgs.append({"role": "assistant", "content": f"step {i} " + "x" * 500})
    return msgs


# ---- extractive -------------------------------------------------------------------------------------------------


def test_extractive_summary_lists_the_facts_of_the_span():
    msgs = transcript(0)
    text = extractive_summary(msgs[1:])
    assert "TASK: make the parser accept v2" in text
    assert "src/parser.py" in text and "tests/test_parser.py" in text
    assert "exit 1: pytest -q tests/test_parser.py" in text
    assert "ok: curl" in text and "sk-abcdef1234567890abcdef" not in text  # the command line is redacted
    assert "AssertionError: expected v2" in text
    assert "fix the v2 branch" in text and "update the changelog" in text and "read the parser" not in text
    assert "The v2 branch still fails. … I will fix the offset next." in text


def test_extractive_summary_is_deterministic_and_bounded():
    msgs = transcript(40)
    a, b = extractive_summary(msgs[1:], budget_chars=2000), extractive_summary(msgs[1:], budget_chars=2000)
    assert a == b and len(a) <= 2000
    assert "left out (compaction budget)" in a
    assert "TASK: make the parser accept v2" in a  # the first section is never cut for a later one


def test_extractive_summary_of_an_empty_or_plain_span():
    assert "Extractive summary" in extractive_summary([])
    text = extractive_summary([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    assert "- hi" in text and "- hello" in text


# ---- the tiers --------------------------------------------------------------------------------------------------


async def test_provider_tier_is_used_when_it_answers():
    caller = FakeCaller("the parser is half migrated")
    res = await compact_with_fallback(caller, transcript(), keep=4, local=FakeLocalSummarizer())
    assert res.tier == TIER_PROVIDER and res.folded > 0 and res.note == "compacted with the provider model"
    assert any(str(m["content"]).startswith(SUMMARY_PREFIX + "the parser is half migrated") for m in res.messages)


async def test_provider_failure_falls_through_to_the_local_model():
    caller, fake = FailingCaller(), FakeLocalSummarizer("local says: parser work", label="local qwen2.5-1.5b")
    res = await compact_with_fallback(caller, transcript(), keep=4, local=fake)
    assert caller.calls == 1 and fake.calls and "TASK: make the parser" in fake.calls[0]
    assert res.tier == TIER_LOCAL and res.note == "compacted with local qwen2.5-1.5b"
    assert any(str(m["content"]).startswith(SUMMARY_PREFIX + "local says") for m in res.messages)


async def test_offline_skips_the_provider_and_prefer_local_goes_first():
    caller, fake = FakeCaller("never"), FakeLocalSummarizer("local first")
    res = await compact_with_fallback(caller, transcript(), keep=4, local=fake, offline=True)
    assert res.tier == TIER_LOCAL and not caller.calls
    res = await compact_with_fallback(caller, transcript(), keep=4, local=fake, prefer_local=True)
    assert res.tier == TIER_LOCAL and not caller.calls
    # the local model failing with prefer_local still reaches the provider
    broken = FakeLocalSummarizer(error="model file truncated")
    res = await compact_with_fallback(caller, transcript(), keep=4, local=broken, prefer_local=True)
    assert res.tier == TIER_PROVIDER and len(caller.calls) == 1


async def test_everything_down_ends_in_the_extractive_tier_and_keeps_the_tail():
    msgs = transcript()
    caller, broken = FailingCaller(), FakeLocalSummarizer(error="out of memory")
    res = await compact_with_fallback(caller, msgs, keep=4, local=broken)
    assert res.tier == TIER_EXTRACTIVE and res.label == EXTRACTIVE_LABEL
    assert res.note == "compacted extractive (no model reachable)"
    cut = split_for_compaction(msgs, 4)
    assert res.messages[-(len(msgs) - cut) :] == msgs[cut:]  # the kept tail is untouched
    assert res.messages[0]["role"] == "system" and res.messages[1]["content"].startswith("TASK:")
    summary = res.messages[2]["content"]
    assert summary.startswith(SUMMARY_PREFIX + "Extractive summary") and "src/parser.py" in summary
    # no local model at all, offline: still compacts
    res = await compact_with_fallback(caller, msgs, keep=4, local=None, offline=True)
    assert res.tier == TIER_EXTRACTIVE and caller.calls == 1


async def test_nothing_to_fold_returns_no_tier():
    res = await compact_with_fallback(FailingCaller(), [{"role": "user", "content": "hi"}], keep=4)
    assert res.folded == 0 and res.tier == "" and res.messages == [{"role": "user", "content": "hi"}]


# ---- the gateway ------------------------------------------------------------------------------------------------


async def test_a_session_at_the_limit_continues_after_an_extractive_compaction(tmp_path, monkeypatch):
    """The summary call reaches no provider; the history is folded extractively and the turn still runs."""
    from m1cmd_helpers import make_server, new_session
    from test_auto_compaction import long_history

    server, provider = make_server(
        tmp_path, monkeypatch, ["ok"], context={"compact_at_tokens": 2000, "keep_messages": 6}
    )
    server.model_caller = FailingCaller()  # type: ignore[assignment]
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    live.stored.messages = long_history()
    before = len(live.stored.messages)
    status, _ = await server._run_turn(live, "next request")
    assert status == "done" and provider.n == 1
    msgs = live.stored.messages
    assert len(msgs) < before // 2
    assert msgs[0]["role"] == "system" and msgs[1]["content"].startswith("request 0 ")
    assert msgs[2]["content"].startswith(SUMMARY_PREFIX + "Extractive summary")
    assert msgs[-2]["content"] == "next request" and msgs[-1]["content"] == "ok"
    note = next(e["payload"] for e in server.event_log if e["payload"].get("key") == "k3.compact")
    assert note["tier"] == "extractive" and "compacted extractive (no model reachable)" in note["text"]
    # the next turn runs on the compacted history as well
    status, _ = await server._run_turn(live, "and again")
    assert status == "done" and live.stored.messages[-1]["content"] == "ok"
    await server.close()


async def test_the_gateway_uses_its_local_summarizer_and_reports_the_tier(tmp_path, monkeypatch):
    from m1cmd_helpers import cmd, make_server, new_session
    from test_auto_compaction import long_history

    server, provider = make_server(tmp_path, monkeypatch, ["ok"], context={"compact_at_tokens": 2000})
    server.router = type(server.router)(server.router.chain, max_retries=0)
    server.local_summarizer = FakeLocalSummarizer("local said so", label="local qwen2.5-1.5b")
    server._local_summarizer_built = True
    server.config.compaction = {"prefer_local": True}
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    live.stored.messages = long_history()
    status, _ = await server._run_turn(live, "go on")
    assert status == "done" and provider.n == 1  # the turn only; the summary came from the local model
    assert live.stored.messages[2]["content"] == SUMMARY_PREFIX + "local said so"
    note = next(e["payload"] for e in server.event_log if e["payload"].get("key") == "k3.compact")
    assert note["tier"] == "local" and note["model"] == "local qwen2.5-1.5b"
    assert "compacted with local qwen2.5-1.5b" in note["text"]
    # /compact reports the tier too
    live.messages = long_history(12)
    res = await cmd(server, "/compact", sid)
    assert "compacted with local qwen2.5-1.5b" in res["message"]
    await server.close()


async def test_slash_compact_falls_back_when_the_provider_fails(tmp_path, monkeypatch):
    from m1cmd_helpers import cmd, make_server, new_session

    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    server.model_caller = FailingCaller()  # type: ignore[assignment]
    server.sessions.get(sid).messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"} for i in range(12)
    ]
    res = await cmd(server, "/compact", sid)
    assert "compacted extractive (no model reachable)" in res["message"]
    assert server.sessions.get(sid).messages[1]["content"].startswith(SUMMARY_PREFIX + "Extractive summary")
    await server.close()


def test_offline_reads_the_netwatch_state(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from k3code.reliability.netwatch import NetState
    from m1cmd_helpers import make_server

    server, _ = make_server(tmp_path, monkeypatch)
    assert not server._offline()  # no NetWatch built yet
    server._netwatch_built = True
    server._netwatch = SimpleNamespace(state=NetState.OFFLINE)
    assert server._offline()
    server._netwatch = SimpleNamespace(state=NetState.PROVIDER_DOWN)
    assert not server._offline()  # one provider down is a failover, not offline


# ---- the model files --------------------------------------------------------------------------------------------


def test_resolve_model_reads_the_config_and_the_models_dir(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    assert local.resolve_model(SimpleNamespace(compaction={})) is None
    folder = local.models_dir()
    folder.mkdir(parents=True)
    small = folder / local.MODELS["qwen2.5-0.5b"].file
    small.write_bytes(b"gguf")
    assert local.resolve_model(SimpleNamespace(compaction={})) == small  # the only model present
    big = folder / local.MODELS["qwen2.5-1.5b"].file
    big.write_bytes(b"gguf")
    assert local.resolve_model(SimpleNamespace(compaction={})) == big  # the default wins when fetched
    assert local.resolve_model(SimpleNamespace(compaction={"local_model": "qwen2.5-0.5b"})) == small
    assert local.resolve_model(SimpleNamespace(compaction={"local_model": small.name})) == small
    own = tmp_path / "own.gguf"
    own.write_bytes(b"gguf")
    assert local.resolve_model(SimpleNamespace(compaction={"local_model": str(own)})) == own
    assert local.resolve_model(SimpleNamespace(compaction={"local_model": "missing"})) is None
    assert local.model_name_of(big) == "qwen2.5-1.5b" and local.model_name_of(own) == "own"


def test_fetch_verifies_the_checksum_and_never_touches_the_network(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    spec = local.MODELS["qwen2.5-0.5b"]
    payload = b"x" * 100
    monkeypatch.setitem(
        local.MODELS,
        "qwen2.5-0.5b",
        local.ModelSpec(spec.name, spec.file, spec.url, len(payload), hashlib.sha256(payload).hexdigest(), spec.note),
    )
    urls: list[str] = []

    def fake_download(url: str, dest: Path, progress) -> None:
        urls.append(url)
        dest.write_bytes(payload)
        if progress:
            progress(len(payload), len(payload))

    path = local.fetch("qwen2.5-0.5b", download=fake_download)
    assert urls == [spec.url] and path.read_bytes() == payload
    assert (path.stat().st_mode & 0o777) in (0o600, 0o644) and (path.parent.stat().st_mode & 0o777) == 0o700
    cached = json.loads(path.with_name(path.name + ".sha256").read_text())
    assert cached["sha256"] == hashlib.sha256(payload).hexdigest()
    assert local.verify("qwen2.5-0.5b") == (True, "ok")
    # a verified file is not fetched again
    assert local.fetch("qwen2.5-0.5b", download=fake_download) == path and len(urls) == 1

    # a corrupt download is refused and removed
    def bad_download(url: str, dest: Path, progress) -> None:
        dest.write_bytes(b"y" * 100)

    with pytest.raises(RuntimeError, match="sha256"):
        local.fetch("qwen2.5-0.5b", download=bad_download, force=True)
    assert path.read_bytes() == payload and not list(path.parent.glob("*.part"))  # the good file stays
    with pytest.raises(ValueError, match="unknown model"):
        local.fetch("nope", download=fake_download)


def test_the_built_in_table_names_real_files():
    for name, spec in local.MODELS.items():
        assert spec.file.endswith(".gguf") and spec.url.endswith(spec.file) and len(spec.sha256) == 64
        assert spec.size > 100_000_000 and spec.name == name
    assert local.DEFAULT_MODEL in local.MODELS


def test_models_cli_lists_and_verifies_without_a_network(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from k3code.cli import cli

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    runner = CliRunner()
    out = runner.invoke(cli, ["models", "list"])
    assert out.exit_code == 0 and "qwen2.5-1.5b (default): not fetched" in out.output
    assert runner.invoke(cli, ["models", "verify"]).exit_code == 1
    out = runner.invoke(cli, ["models", "fetch", "nope"])
    assert out.exit_code != 0 and "unknown model" in out.output


def test_doctor_reports_the_local_model(tmp_path, monkeypatch):
    from k3code import doctor
    from k3code.config import Settings

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    chk = doctor.check_local_model(Settings())
    assert chk.status == doctor.WARN and "no local compaction model" in chk.detail and chk.fix == "k3code models fetch"
    folder = local.models_dir()
    folder.mkdir(parents=True)
    (folder / local.MODELS["qwen2.5-1.5b"].file).write_bytes(b"gguf")
    monkeypatch.setattr(local, "llama_available", lambda: False)
    chk = doctor.check_local_model(Settings())
    assert chk.status == doctor.WARN and "llama-cpp-python is not installed" in chk.detail
    monkeypatch.setattr(local, "llama_available", lambda: True)
    chk = doctor.check_local_model(Settings(compaction={"prefer_local": True}))
    assert chk.status == doctor.OK and "qwen2.5-1.5b" in chk.detail and "used first" in chk.detail


def test_build_summarizer_is_none_without_llama(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    folder = local.models_dir()
    folder.mkdir(parents=True)
    (folder / local.MODELS["qwen2.5-1.5b"].file).write_bytes(b"gguf")
    monkeypatch.setattr(local, "llama_available", lambda: False)
    assert local.build_summarizer(SimpleNamespace(compaction={})) is None
    monkeypatch.setattr(local, "llama_available", lambda: True)
    s = local.build_summarizer(SimpleNamespace(compaction={"local_threads": 4}))
    assert isinstance(s, local.LlamaSummarizer) and s.threads == 4 and s.label == "local qwen2.5-1.5b"
    assert s._llm is None  # nothing is loaded until the first summary call


def test_setup_offers_the_local_model_without_downloading(tmp_path, monkeypatch):
    from k3code.setup import steps
    from k3code.setup.prompter import AnswerPrompter

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    fetched: list[str] = []
    monkeypatch.setattr(local, "fetch", lambda name, **kw: fetched.append(name))
    ctx = steps.Ctx(p=AnswerPrompter({}), data={}, do_probe=False)
    assert steps.step_integrations(ctx)["local_model"] is False and not fetched  # default: no
    ctx = steps.Ctx(p=AnswerPrompter({"integrations": {"local_model": True}}), data={}, do_probe=False)
    assert steps.offer_local_model(ctx) is False and not fetched  # a yes without probes fetches nothing
    ctx = steps.Ctx(p=AnswerPrompter({"integrations": {"local_model": True}}), data={}, do_probe=True)
    assert steps.offer_local_model(ctx) is True and fetched == [local.DEFAULT_MODEL]
