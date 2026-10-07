"""CLI entry point: headless -p and minimal REPL."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

import click

from k3code.agent.loop import AgentLoop
from k3code.config import K3CODE_HOME, load_config
from k3code.errors import AllProvidersUnreachable, ChainExhausted, ContextOverflow
from k3code.permissions import PermissionMode
from k3code.providers import make_providers
from k3code.reliability import (
    BudgetExceeded,
    DiskGuardFull,
    Reliability,
    ReliabilityFlags,
    ReliabilitySettings,
)
from k3code.reliability.persistent_retry import TurnCancelled
from k3code.router import CooldownStore, Router, RouterEvent, build_chain

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _load_system_prompt() -> str:
    prompt_path = Path(__file__).parent / "prompts" / "system.md"
    if prompt_path.is_file():
        return prompt_path.read_text(encoding="utf-8")
    return "You are a helpful coding assistant."


def _resolve_model_specs(config: Any) -> list[str | list[str]]:
    """Pick each provider's configured model(s) for ``config.default_model``.

    ``provider.models`` maps keys (e.g. "default", "cheap") to a model id or a
    per-provider fallback list; this resolves the active key (falling back to
    "default", then any entry) into the ``models`` argument ``build_chain()``
    expects.
    """
    key = config.default_model
    resolved: list[str | list[str]] = []
    for p in config.providers:
        spec = p.models.get(key)
        if spec is None:
            spec = p.models.get("default")
        if spec is None:
            spec = next(iter(p.models.values()), "")
        resolved.append(spec)
    return resolved


def _print_event(event: RouterEvent) -> None:
    if event.kind == "router.attempt":
        logger.debug("Attempt %d: %s/%s", event.attempt, event.provider, event.model)
    elif event.kind == "router.failover":
        logger.info("Failover: %s/%s (reason: %s) — %s", event.provider, event.model, event.reason, event.detail)
    elif event.kind == "router.exhausted":
        logger.warning("Chain exhausted: %s", event.detail)
    elif event.kind.startswith(("reliability.", "net.state")):
        # M2: reliability events surface as info lines.
        logger.info("%s %s", event.kind, event.detail)


def _build_reliability(config: Any, session: str) -> Reliability:
    """M2: build the reliability bundle from the config's reliability dict."""
    raw = dict(getattr(config, "reliability", None) or {})
    flags_raw = raw.pop("flags", None)
    flags = ReliabilityFlags(**flags_raw) if isinstance(flags_raw, dict) else None
    known = {"enabled", "flags", "max_wait", "max_park_seconds",
             "session_tokens", "session_usd", "day_tokens", "day_usd"}
    settings = ReliabilitySettings(flags=flags) if flags else ReliabilitySettings()
    for key in known & set(raw):
        setattr(settings, key, raw[key])
    return Reliability.from_settings(settings, session=session, home=K3CODE_HOME)


async def _run_headless(
    prompt: str,
    *,
    model: str | None,
    permission_mode: PermissionMode,
    config: Any,
    json_output: bool,
) -> dict[str, Any] | None:
    """Run headless mode and return final result dict."""
    system_prompt = _load_system_prompt()

    providers = make_providers(config.providers)
    chain = build_chain(providers, _resolve_model_specs(config))
    cooldowns = CooldownStore()
    router = Router(chain, cooldowns=cooldowns, on_event=_print_event)

    # M2: reliability bundle (netwatch, persistent retry, journal, guards).
    reliability = _build_reliability(config, session="headless")
    reliability.register_providers(chain)

    loop = AgentLoop(
        router,
        system_prompt=system_prompt,
        max_turns=config.max_turns,
        permission_mode=permission_mode.value,
        headless=True,
        on_event=_print_event,
        cwd=Path.cwd(),
        reliability=reliability,
    )

    final_text = ""
    tool_results: list[dict[str, Any]] = []

    async def on_text_delta(text: str) -> None:
        nonlocal final_text
        final_text += text
        if not json_output:
            sys.stdout.write(text)
            sys.stdout.flush()

    loop.on_text_delta = on_text_delta

    try:
        await reliability.start()
        async for _ in loop.run(prompt, model=model, max_tokens=config.max_tokens, temperature=config.temperature):
            pass
        return {"text": final_text, "tools": tool_results}
    except AllProvidersUnreachable as e:
        return {"error": "all_providers_unreachable", "message": str(e), "attempts": e.attempts}
    except ChainExhausted as e:
        return {"error": "chain_exhausted", "message": str(e), "last_reason": e.last_reason}
    except ContextOverflow as e:
        return {"error": "context_overflow", "message": str(e)}
    except TurnCancelled as e:
        return {"error": "cancelled", "message": str(e)}
    except BudgetExceeded as e:
        return {"error": "budget_exceeded", "message": str(e), "scope": e.scope, "kind": e.kind}
    except DiskGuardFull as e:
        return {"error": "disk_guard", "message": str(e)}
    except Exception as e:
        logger.exception("Agent failed")
        return {"error": "agent_error", "message": str(e)}
    finally:
        await reliability.stop()


async def _run_repl(
    *,
    model: str | None,
    permission_mode: PermissionMode,
    config: Any,
) -> None:
    """Run minimal REPL."""
    system_prompt = _load_system_prompt()

    providers = make_providers(config.providers)
    chain = build_chain(providers, _resolve_model_specs(config))
    cooldowns = CooldownStore()
    router = Router(chain, cooldowns=cooldowns, on_event=_print_event)

    # M2: reliability bundle; one journal/session per REPL process.
    reliability = _build_reliability(config, session="repl")
    reliability.register_providers(chain)

    loop = AgentLoop(
        router,
        system_prompt=system_prompt,
        max_turns=config.max_turns,
        permission_mode=permission_mode.value,
        headless=False,
        on_event=_print_event,
        cwd=Path.cwd(),
        reliability=reliability,
    )

    print("k3code REPL (type /exit to quit, /model <name> to switch, /stop to cancel a stuck turn)")
    print(f"Permission mode: {permission_mode.value}")
    if model:
        print(f"Model override: {model}")

    try:
        await reliability.start()
    except Exception:
        logger.debug("netwatch start failed; continuing without it")

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input == "/exit":
            break
        if user_input == "/stop":
            # M2: abort a paused/parked wait in another turn (best effort here:
            # the REPL is single-threaded, so /stop mainly guards the next turn).
            reliability.cancel()
            print("Stop requested.")
            continue
        if user_input.startswith("/model "):
            model = user_input[7:].strip()
            print(f"Model set to: {model}")
            continue

        print()  # spacing
        final_text = ""

        async def on_text_delta(text: str) -> None:
            nonlocal final_text
            final_text += text
            sys.stdout.write(text)
            sys.stdout.flush()

        loop.on_text_delta = on_text_delta

        try:
            async for _ in loop.run(
                user_input, model=model, max_tokens=config.max_tokens, temperature=config.temperature
            ):
                pass
            print()  # newline after streaming
        except AllProvidersUnreachable as e:
            print(f"\n[Error] All providers unreachable: {e}")
        except ChainExhausted as e:
            print(f"\n[Error] Chain exhausted: {e}")
        except ContextOverflow as e:
            print(f"\n[Error] Context overflow: {e}")
        except TurnCancelled as e:
            print(f"\n[Stopped] {e}")
        except BudgetExceeded as e:
            print(f"\n[Budget] {e} — raise the limit in config (reliability.session_*) to continue.")
        except DiskGuardFull as e:
            print(f"\n[Disk] {e}")
        except KeyboardInterrupt:
            # M2: Ctrl-C mid-turn cancels any parked/paused wait.
            reliability.cancel()
            print("\n[Interrupted] turn cancelled (/stop semantics).")
        except Exception as e:
            logger.exception("REPL turn failed")
            print(f"\n[Error] {e}")
    await reliability.stop()


@click.command()
@click.option("-p", "--prompt", "prompt", help="Headless prompt; omit for REPL")
@click.option("-m", "--model", help="Model override (e.g., 'default', 'cheap')")
@click.option(
    "--permission",
    type=click.Choice(["ask", "auto-edit", "yolo"]),
    default="ask",
    help="Permission mode (default: ask)",
)
@click.option("--json", "json_output", is_flag=True, help="Output final result as JSON (headless only)")
@click.option("--config-dir", type=click.Path(path_type=Path), help="Project directory for config")
def main(
    prompt: str | None,
    model: str | None,
    permission: str,
    json_output: bool,
    config_dir: Path | None,
) -> None:
    """k3code — terminal coding agent.

    Examples:
      k3code -p "create hello.py" --permission yolo
      k3code -p "fix the bug" --json
      k3code  # starts REPL
    """
    permission_mode = PermissionMode(permission)

    # Load config
    config = load_config(project_dir=config_dir or Path.cwd())

    # Headless permission override
    if config.headless_permission and prompt:
        permission_mode = PermissionMode(config.headless_permission)

    if prompt:
        result = asyncio.run(
            _run_headless(
                prompt, model=model, permission_mode=permission_mode, config=config, json_output=json_output
            )
        )
        if json_output and result:
            print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result and "error" not in result else 1)
    else:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run_repl(model=model, permission_mode=permission_mode, config=config))


if __name__ == "__main__":
    main()
