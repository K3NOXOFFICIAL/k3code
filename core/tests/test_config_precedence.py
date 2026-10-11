"""Precedence per top-level key: CLI flag > env > project config > user config > defaults.

Nested sections are replaced as a whole, never merged. Every file here lives under pytest's tmp_path;
HOME, XDG_CONFIG_HOME and K3CODE_HOME are redirected so a real ~/.k3code or ~/.config/k3code is never read,
and each project config is recorded as trusted there (an untrusted one is ignored).
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from k3code import trust
from k3code.config import load_config

USER_PROVIDER_A = """
providers:
  - name: user-a
    kind: openai
    base_url: https://user-a.example/v1
    api_key_env: K3_PRECEDENCE_KEY
    models: {default: model-a}
"""

PROJECT_PROVIDER_B = """
providers:
  - name: project-b
    kind: openai
    base_url: https://project-b.example/v1
    api_key_env: K3_PRECEDENCE_KEY
    models: {default: model-b}
"""


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    user_home = tmp_path / "k3home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("K3CODE_HOME", str(user_home))
    for key in list(os.environ):
        if key.startswith("K3CODE_") and key != "K3CODE_HOME":
            monkeypatch.delenv(key)
    project = tmp_path / "project"
    project.mkdir()
    return {"user": user_home, "project": project}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def _write_user(dirs: dict[str, Path], text: str) -> None:
    _write(dirs["user"] / "config.yaml", text)


def _write_project(dirs: dict[str, Path], text: str) -> None:
    _write(dirs["project"] / ".k3code" / "config.yaml", text)
    trust.record(dirs["project"], trusted=True)  # an untrusted project config is never applied (k3code.trust)


def test_project_providers_are_ignored_even_when_trusted(dirs: dict[str, Path]) -> None:
    _write_user(dirs, USER_PROVIDER_A)
    _write_project(dirs, PROJECT_PROVIDER_B)
    config = load_config(project_dir=dirs["project"])
    assert [p.name for p in config.providers] == ["user-a"]


def test_user_providers_kept_when_project_does_not_set_them(dirs: dict[str, Path]) -> None:
    _write_user(dirs, USER_PROVIDER_A)
    _write_project(dirs, "permission_mode: auto-edit\n")
    config = load_config(project_dir=dirs["project"])
    assert [p.name for p in config.providers] == ["user-a"]


def test_key_absent_from_project_keeps_user_value(dirs: dict[str, Path]) -> None:
    _write_user(
        dirs,
        USER_PROVIDER_A + "permission_mode: yolo\nautonomy: {plan_first: true}\n",
    )
    _write_project(dirs, PROJECT_PROVIDER_B)
    config = load_config(project_dir=dirs["project"])
    assert config.permission_mode == "yolo"
    assert config.autonomy == {"plan_first": True}


def test_project_scalar_replaces_user_scalar(dirs: dict[str, Path]) -> None:
    _write_user(dirs, "permission_mode: yolo\n")
    _write_project(dirs, "permission_mode: auto-edit\n")
    assert load_config(project_dir=dirs["project"]).permission_mode == "auto-edit"


def test_nested_section_is_replaced_as_a_whole(dirs: dict[str, Path]) -> None:
    _write_user(dirs, "autonomy:\n  plan_first: true\n  fanout: {max_parallel: 3}\n")
    _write_project(dirs, "autonomy:\n  plan_first: false\n")
    config = load_config(project_dir=dirs["project"])
    assert config.autonomy == {"plan_first": False}


def test_env_beats_user_and_project(dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _write_user(dirs, "permission_mode: yolo\n")
    _write_project(dirs, "permission_mode: auto-edit\n")
    monkeypatch.setenv("K3CODE_PERMISSION_MODE", "ask")
    assert load_config(project_dir=dirs["project"]).permission_mode == "ask"


def test_cli_providers_beat_project_and_user(dirs: dict[str, Path]) -> None:
    _write_user(dirs, USER_PROVIDER_A)
    _write_project(dirs, PROJECT_PROVIDER_B)
    cli_provider = {
        "name": "cli-c",
        "kind": "openai",
        "base_url": "https://cli-c.example/v1",
        "api_key_env": "K3_PRECEDENCE_KEY",
        "models": {"default": "model-c"},
    }
    config = load_config(project_dir=dirs["project"], cli_overrides={"providers": [cli_provider]})
    assert [p.name for p in config.providers] == ["cli-c"]


def test_cli_flag_beats_env(dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K3CODE_PERMISSION_MODE", "ask")
    config = load_config(project_dir=dirs["project"], cli_overrides={"permission_mode": "yolo"})
    assert config.permission_mode == "yolo"


# ── the parsed-file cache ──


def _old(path: Path, text: str) -> None:
    """Write ``text`` and date it an hour back: only a file not changed lately is cached."""
    import os
    import time

    path.write_text(text)
    past = time.time() - 3600
    os.utime(path, (past, past))


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    from k3code import config as config_mod

    seen: list[Path] = []
    real = config_mod._parse_mapping

    def counting(text: str, where: Path):
        seen.append(where)
        return real(text, where)

    monkeypatch.setattr(config_mod, "_parse_mapping", counting)
    config_mod._YAML_CACHE.clear()
    return seen


def test_a_config_file_that_did_not_change_is_parsed_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from k3code.config import _load_yaml

    f = tmp_path / "config.yaml"
    _old(f, "permission_mode: auto\nhooks: {a: 1}\n")
    seen = _count_parses(monkeypatch)
    first = _load_yaml(f)
    first["hooks"]["a"] = 99  # a caller edits what it got ...
    first["added"] = True
    for _ in range(3):
        assert _load_yaml(f) == {"permission_mode": "auto", "hooks": {"a": 1}}  # ... the next one does not see it
    assert seen == [f]


def test_a_changed_file_is_read_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import time

    from k3code.config import _load_yaml

    f = tmp_path / "config.yaml"
    _old(f, "permission_mode: auto\n")
    seen = _count_parses(monkeypatch)
    assert _load_yaml(f) == {"permission_mode": "auto"}
    f.write_text("permission_mode: yolo\n")  # same size, new mtime
    past = time.time() - 1800
    os.utime(f, (past, past))
    assert _load_yaml(f) == {"permission_mode": "yolo"}
    f.write_text("permission_mode: auto-edit\n")  # a different size, and a different time
    os.utime(f, (past - 100, past - 100))
    assert _load_yaml(f) == {"permission_mode": "auto-edit"}
    assert len(seen) == 3
    f.unlink()
    assert _load_yaml(f) == {}  # gone: empty, as before


def test_a_file_edited_just_now_is_never_served_from_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-place edit inside the filesystem's timestamp granularity keeps (mtime, size): not cached until it ages."""
    from k3code.config import _load_yaml

    f = tmp_path / "config.yaml"
    f.write_text("permission_mode: auto\n")
    seen = _count_parses(monkeypatch)
    _load_yaml(f)
    _load_yaml(f)
    assert len(seen) == 2


def test_a_broken_file_raises_every_time_and_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from k3code.config import ConfigError, _load_yaml

    f = tmp_path / "config.yaml"
    _old(f, "a: [unclosed\n")
    for _ in range(2):
        with pytest.raises(ConfigError, match="not valid YAML"):
            _load_yaml(f)
    _old(f, "- not a mapping\n")
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        _load_yaml(f)
