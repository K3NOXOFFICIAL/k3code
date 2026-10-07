"""Regressions from the first real install: missing key, env-file keys, wizard env-var names, completion."""

from __future__ import annotations

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
