"""Run the setup steps with resumable state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k3code.setup.prompter import Prompter
from k3code.setup.state import clear_state, load_state, save_state
from k3code.setup.steps import STEP_NAMES, STEPS, Ctx, Step, step_secrets, write_config, write_user_md


def run_setup(
    p: Prompter,
    *,
    only_step: str | None = None,
    restart: bool = False,
    do_probe: bool = True,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run pending steps in order (or just ``only_step``). State is saved after every step."""
    if restart:
        clear_state()
    state = load_state()
    if state.get("done") and not only_step:  # a finished run: start a fresh one
        state = {"completed": [], "data": {}}
    ctx = Ctx(p=p, data=state["data"], do_probe=do_probe, cwd=cwd or Path.cwd())
    if only_step == "secrets":  # extra step: not part of the numbered flow
        todo = [Step("secrets", step_secrets, "Missing API keys")]
    elif only_step:
        if only_step not in STEP_NAMES:
            raise ValueError(f"unknown step {only_step!r}; choose from {', '.join(STEP_NAMES)}")
        todo = [s for s in STEPS if s.name == only_step]
    else:
        todo = [s for s in STEPS if s.name not in state["completed"]]
        if state["completed"] and todo:
            p.say(f"Resuming at step '{todo[0].name}'.")
    for step in todo:
        pos = f" ({STEP_NAMES.index(step.name) + 1}/{len(STEPS)})" if step.name in STEP_NAMES else ""
        p.say(f"\n== {step.title}{pos} ==")
        state["data"][step.name] = step.fn(ctx)
        if step.name not in state["completed"] and step.name != "secrets":
            state["completed"].append(step.name)
        if only_step and step.name not in ("summary", "secrets"):
            write_config(state["data"], only=step.name)
            if step.name in ("about", "system", "usage"):
                write_user_md(state["data"])
        save_state(state)
    if not only_step and all(n in state["completed"] for n in STEP_NAMES):
        state["done"] = True  # keep the data so `--step X` can rewrite config from it
        save_state(state)
        p.say("\nSetup complete.")
    return state
