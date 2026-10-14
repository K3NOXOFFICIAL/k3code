"""Regressions from the first real install: missing key, env-file keys, wizard env-var names, completion."""

from __future__ import annotations

import asyncio

import pytest

from k3code.providers.base import ProviderError, request_headers
from k3code.router.classifier import FailoverReason, classify_api_error


def test_empty_api_key_is_a_clear_auth_error() -> None:
    with pytest.raises(ProviderError) as ei:
        request_headers("openai", "")
    err = ei.value
    assert err.status_code == 401
    assert "No API key configured" in err.message
    assert classify_api_error(err).reason is FailoverReason.auth


async def test_first_turn_missing_key_error_keeps_its_whole_fix() -> None:
    """The router caps an entry's error at 160 characters; the fix (`k3code onboard`) must fit under it."""
    from k3code.errors import ChainExhausted
    from k3code.providers.openai_compat import OpenAICompatProvider
    from k3code.providers.types import Message
    from k3code.router import CooldownStore, Router, build_chain

    provider = OpenAICompatProvider(name="p", base_url="https://api.test.com", api_key="")
    router = Router(build_chain([provider], [["m"]]), cooldowns=CooldownStore())
    try:
        with pytest.raises(ChainExhausted) as raised:
            await router.complete([Message(role="user", content="hi")], [])
    finally:
        await provider.aclose()
    message = str(raised.value)
    assert "No API key configured" in message
    assert "`k3code onboard`" in message
    assert "…" not in message  # the 160-character cap did not cut the hint


def test_omniroute_daily_quota_400_is_quota() -> None:
    err = ProviderError(
        "This API key reached its daily usage quota (100%). Resets in 13h 48m.",
        status_code=400,
        body={
            "error": {"message": "This API key reached its daily usage quota (100%).", "code": "usage_limit_exceeded"}
        },
    )
    assert classify_api_error(err).reason is FailoverReason.quota


def test_config_reads_key_from_env_file(tmp_path, monkeypatch) -> None:
    from k3code.config import load_config
    from k3code.setup.state import set_env_var

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    envfile = tmp_path / "xdg" / "k3code" / "env"
    monkeypatch.delenv("FIRSTRUN_KEY", raising=False)
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.yaml").write_text(
        "providers:\n"
        "- {name: p, kind: openai, base_url: 'http://x/v1', api_key_env: FIRSTRUN_KEY, models: {default: m}}\n"
    )
    set_env_var("FIRSTRUN_KEY", "from-file", path=envfile)
    cfg = load_config(project_dir=tmp_path)
    assert cfg.providers[0].api_key == "from-file"


def test_wizard_env_name_pattern() -> None:
    from k3code.setup.steps import _ENV_NAME

    assert _ENV_NAME.match("OMNIROUTE_API_KEY")
    assert not _ENV_NAME.match("omniroute-k3code")  # the value that broke the first install
    assert not _ENV_NAME.match("my key")


@pytest.mark.asyncio
async def test_complete_slash_and_path(tmp_path) -> None:
    from k3code.gateway import server as srv

    class FakeCmd:
        help = "do a thing"

    class FakeRegistry:
        def names(self):
            return ["model", "memory", "stats"]

        def get(self, name):
            return FakeCmd()

    class FakeServer:
        commands = FakeRegistry()
        live: dict = {}

        class config:  # noqa: N801
            class skills:  # noqa: N801
                roots = None

    got = await srv._complete_slash(FakeServer(), {"text": "/m"})
    assert [i["text"] for i in got["items"] if i["kind"] == "command"] == ["/model", "/memory"]
    assert got["replace_from"] == 1
    assert (await srv._complete_slash(FakeServer(), {"text": "plain"}))["items"] == []

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x")
    (tmp_path / "README.md").write_text("x")
    word = str(tmp_path) + "/"
    paths = await srv._complete_path(FakeServer(), {"word": word})
    assert {i["display"].rstrip("/").split("/")[-1] for i in paths["items"]} == {"src", "README.md"}
    sub = await srv._complete_path(FakeServer(), {"word": "@" + str(tmp_path / "src") + "/ma"})
    assert [i["text"] for i in sub["items"]] == ["@" + str(tmp_path / "src") + "/main.py"]


class _PathServer:
    """The bare minimum `_complete_path` needs: a session directory."""

    def __init__(self, cwd: object) -> None:
        self._cwd = cwd

    def _session_for(self, _sid: object) -> None:
        return None


async def test_complete_path_scans_off_the_loop_and_caps_the_scan(tmp_path, monkeypatch) -> None:
    """A huge directory is read on a worker thread and abandoned after the scan cap, not statted whole."""
    import os
    import threading
    import time

    from k3code.gateway import server as srv

    for i in range(srv.COMPLETE_PATH_SCAN_CAP + 500):
        (tmp_path / f"f{i:05d}").write_text("")
    seen = {"entries": 0, "threads": set()}
    real_scandir = os.scandir

    class CountingScandir:
        def __init__(self, path: object) -> None:
            self._inner = real_scandir(path)

        def __enter__(self) -> CountingScandir:
            self._inner.__enter__()
            return self

        def __exit__(self, *exc: object) -> None:
            self._inner.__exit__(*exc)

        def __iter__(self) -> CountingScandir:
            return self

        def __next__(self) -> os.DirEntry:
            seen["threads"].add(threading.get_ident())
            time.sleep(0.0005)  # a slow disk: would stall the loop if it ran there
            entry = next(self._inner)
            seen["entries"] += 1
            return entry

    monkeypatch.setattr(srv.os, "scandir", CountingScandir)
    server = _PathServer(tmp_path)
    monkeypatch.setattr(srv, "_session_dir", lambda *_a: tmp_path)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    got = await srv._complete_path(server, {"word": "./"})
    task.cancel()

    assert len(got["items"]) == 50
    assert seen["entries"] == srv.COMPLETE_PATH_SCAN_CAP
    assert threading.get_ident() not in seen["threads"]
    assert ticks >= 10  # the loop kept turning while the directory was read


async def test_complete_path_keeps_directories_first_and_hides_dotfiles(tmp_path, monkeypatch) -> None:
    from k3code.gateway import server as srv

    (tmp_path / "zdir").mkdir()
    (tmp_path / "a.txt").write_text("")
    (tmp_path / ".hidden").write_text("")
    monkeypatch.setattr(srv, "_session_dir", lambda *_a: tmp_path)
    got = await srv._complete_path(_PathServer(tmp_path), {"word": "./"})
    assert [i["display"] for i in got["items"]] == ["./zdir/", "./a.txt"]
    shown = await srv._complete_path(_PathServer(tmp_path), {"word": "./.h"})
    assert [i["display"] for i in shown["items"]] == ["./.hidden"]
