import json

import pytest

from k3code import confio
from k3code.config import Settings
from k3code.learning import updateconfig as uc
from learn_helpers import FakeCaller


class Ctx:
    def __init__(self, reply: str, choice: str = "apply") -> None:
        self.model_caller = FakeCaller(reply)
        self.config = Settings()
        self.asked: list[str] = []
        self.choice = choice

    async def clarify(self, question, choices, session_id):
        self.asked.append(question)
        return {"choice": self.choice}


async def test_valid_patch_shows_diff_and_applies_after_confirmation(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("max_turns: 10\n")
    patch = {"task_tiers": {"background_turn": "cheap"}, "display": {"focus_mode": True}}
    ctx = Ctx(json.dumps(patch))
    out = await uc.run(ctx, "s", "always use the cheap tier for background jobs and turn focus mode on", path)
    assert "+  focus_mode: true" in ctx.asked[0] and "background_turn: cheap" in ctx.asked[0]
    assert "updated" in out
    data = confio.read_yaml(path)
    assert data["task_tiers"] == {"background_turn": "cheap"} and data["display"]["focus_mode"] is True
    assert data["max_turns"] == 10
    assert confio.latest_backup(path) is not None
    assert ctx.config.task_tiers == {"background_turn": "cheap"}


async def test_cancel_leaves_config_untouched(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("max_turns: 10\n")
    ctx = Ctx('{"max_turns": 5}', choice="cancel")
    out = await uc.run(ctx, "s", "fewer turns", path)
    assert "Cancelled" in out and confio.read_yaml(path) == {"max_turns": 10}
    assert confio.latest_backup(path) is None


@pytest.mark.parametrize(
    "patch",
    [
        {"providers": [{"name": "x"}]},
        {"mem0": {"api_key_env": "X"}},
        {"mem0": {"url": "http://m", "password": "hunter2"}},
        {"output_style": "Bearer abcdef123456"},
        {"permission_mode": "yolo"},
        {"headless_permission": "yolo"},
        {"permissions": {"hardline": []}},
        {"permissions": {"bash": "allow"}},
        {"autonomy": {"gate_modes": []}},
        {"nonsense_key": 1},
    ],
)
async def test_secrets_and_hardline_relaxations_rejected(tmp_path, patch):
    path = tmp_path / "config.yaml"
    path.write_text("permissions:\n  hardline: ['foo']\n")
    ctx = Ctx(json.dumps(patch))
    out = await uc.run(ctx, "s", "do it", path)
    assert out.startswith("Rejected") and not ctx.asked
    assert "foo" in path.read_text() and confio.latest_backup(path) is None


async def test_extending_hardline_is_allowed(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("permissions:\n  hardline: ['foo']\n")
    ctx = Ctx(json.dumps({"permissions": {"hardline": ["foo", "bar"]}}))
    assert "updated" in await uc.run(ctx, "s", "block bar", path)


async def test_invalid_value_and_garbage_reply(tmp_path):
    path = tmp_path / "config.yaml"
    assert "invalid" in await uc.run(Ctx('{"max_turns": "lots"}'), "s", "x", path)
    assert "JSON" in await uc.run(Ctx("sorry"), "s", "x", path)
    assert "could not" in await uc.run(Ctx("{}"), "s", "x", path)
    assert "Usage" in await uc.run(Ctx("{}"), "s", "", path)
