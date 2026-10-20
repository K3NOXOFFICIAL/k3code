"""The rows the gateway sends for the TUI's agent view name a session by its first prompt (not "Session" for every row)
and show the model the user recognises (not the config key ``default``)."""

from __future__ import annotations

from types import SimpleNamespace

from k3code.config import ProviderEntry, Settings
from k3code.gateway.server import GatewayServer


def _config() -> Settings:
    entry = ProviderEntry(
        name="stubby",
        kind="openai",
        base_url="http://127.0.0.1:1/v1",
        api_key_env="",
        models={"default": "stub-small"},
    )
    return Settings(providers=[entry], default_model="default")


def _session(session_id: str, messages: list[dict], title: str = "", model: str = "", provider: str = ""):
    stored = SimpleNamespace(
        title=title,
        messages=messages,
        model=model,
        provider=provider,
        updated_at=2.0,
        created_at=1.0,
        meta={},
    )
    return SimpleNamespace(
        session_id=session_id,
        stored=stored,
        state="idle",
        paused=False,
        streaming=False,
        turn_started_wall=0.0,
        offline_protection_error=None,
        background=False,
    )


def _rows(*sessions, config=None):
    fake = SimpleNamespace(
        live={s.session_id: s for s in sessions},
        subagents=SimpleNamespace(handles={}),
        config=config or _config(),
    )
    return GatewayServer._active_rows(fake, None)


def test_a_session_without_a_title_is_named_by_the_start_of_its_first_prompt() -> None:
    row = _rows(_session("a", [{"role": "user", "content": "fix the flaky login test in auth/"}]))[0]
    assert row["title"] == "fix the flaky login test in auth/"
    assert row["preview"] == "fix the flaky login test in auth/"


def test_a_generated_title_wins_over_the_first_prompt() -> None:
    row = _rows(_session("a", [{"role": "user", "content": "hello"}], title="Login test fix"))[0]
    assert row["title"] == "Login test fix"


def test_a_session_with_no_prompt_yet_is_called_session() -> None:
    assert _rows(_session("a", []))[0]["title"] == "Session"


def test_the_title_is_one_line_without_control_characters_and_is_cut() -> None:
    nasty = "\x1b[31mred\x1b[0m\n\nsecond   line\x07 " + "x" * 200
    title = _rows(_session("a", [{"role": "user", "content": nasty}]))[0]["title"]
    assert "\x1b" not in title and "\x07" not in title and "\n" not in title
    assert title.startswith("[31mred [0m second line")
    assert len(title) <= 80


def test_the_model_column_names_the_model_not_the_config_key() -> None:
    assert _rows(_session("a", [], model="default"))[0]["model"] == "stub-small"
    assert _rows(_session("a", []))[0]["model"] == "stub-small"  # no pinned model: the default's


def test_without_a_provider_block_the_key_is_shown_as_it_is() -> None:
    assert _rows(_session("a", [], model="mystery"), config=Settings())[0]["model"] == "mystery"
