"""CLI entry point: headless -p and minimal REPL."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from k3code import __version__

if TYPE_CHECKING:  # annotations only: the engine is imported inside the functions, so --version and --help stay cheap
    from k3code.permissions import PermissionMode
    from k3code.reliability import Reliability
    from k3code.router import RouterEvent

# WARNING by default: at INFO, httpx request lines and "Turn N/20" were interleaved with the answer of every `-p` run.
# The gateway and the daemon set their own level (force=True) because this runs first, at import.
logging.basicConfig(level=os.environ.get("K3CODE_LOG_LEVEL", "WARNING").upper(), format="%(levelname)s: %(message)s")
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


def _use_model_key(config: Any, key: str) -> str | None:
    """Make ``key`` (``-m``, REPL ``/model``) the active model key; returns an error message for an unknown key.

    A key ("cheap") used to go to the router as the literal model id, so the provider got model "cheap".
    """
    known = {m for p in config.providers for m in p.models} | {config.default_model}
    if key not in known:
        return f"unknown model key: {key} (known: {', '.join(sorted(known))})"
    config.default_model = key
    return None


def _cooldown_path() -> Path:
    from k3code.config import _current_home

    return _current_home() / "cooldowns.json"


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
    from k3code.paths import home as k3code_home
    from k3code.reliability import build_reliability

    return build_reliability(config, session=session, home=k3code_home())


async def _reap_jobs(session: str) -> None:
    """Kill the run's background bash jobs: their process groups (start_new_session) would outlive the CLI."""
    from k3code.tools import jobs

    with contextlib.suppress(Exception):
        await jobs.reap(session)


async def _run_headless(
    prompt: str,
    *,
    model: str | None,
    permission_mode: PermissionMode,
    config: Any,
    json_output: bool,
    session: str = "headless",
    resume: bool = False,
) -> dict[str, Any] | None:
    """Run headless mode and return final result dict."""
    from k3code.agent.loop import AgentLoop
    from k3code.errors import AllProvidersUnreachable, ChainExhausted, ContextOverflow
    from k3code.extratools import register_skill_tool
    from k3code.prompting import build_system_prompt
    from k3code.providers import make_providers
    from k3code.reliability import BudgetExceeded, DiskGuardFull
    from k3code.reliability.persistent_retry import TurnCancelled
    from k3code.router import CooldownStore, Router, build_chain
    from k3code.routing.tiers import router_options

    if model and (err := _use_model_key(config, model)):
        return {"error": "unknown_model", "message": err}
    system_prompt = build_system_prompt(_load_system_prompt(), cwd=Path.cwd(), config=config)

    providers = make_providers(config.providers)
    chain = build_chain(providers, _resolve_model_specs(config))
    cooldowns = CooldownStore(path=_cooldown_path())
    router = Router(chain, cooldowns=cooldowns, on_event=_print_event, **router_options(config))

    # M2: reliability bundle (netwatch, persistent retry, journal, guards).
    reliability = _build_reliability(config, session=session)
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
        session=session,
    )
    register_skill_tool(loop.tools, Path.cwd(), list(config.skills.roots))

    final_text = ""
    tool_results: list[dict[str, Any]] = []

    async def on_text_delta(text: str) -> None:
        nonlocal final_text
        final_text += text
        if not json_output:
            sys.stdout.write(text)
            sys.stdout.flush()

    async def on_text_reset() -> None:
        # a retry after partial output streams the answer again: the result keeps one copy, the terminal a line break
        nonlocal final_text
        final_text = ""
        if not json_output:
            sys.stdout.write("\n")
            sys.stdout.flush()
            sys.stderr.write("[k3code] the reply was cut off; retrying (the partial text above is discarded)\n")

    loop.on_text_delta = on_text_delta
    loop.on_text_reset = on_text_reset

    try:
        await reliability.start()
        async for _ in loop.run(prompt, max_tokens=config.max_tokens, temperature=config.temperature, resume=resume):
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
        await _reap_jobs(session)  # no background bash job outlives the run
        await reliability.stop()


async def _run_repl(
    *,
    model: str | None,
    permission_mode: PermissionMode,
    config: Any,
) -> None:
    """Run minimal REPL."""
    from k3code.agent.loop import AgentLoop
    from k3code.errors import AllProvidersUnreachable, ChainExhausted, ContextOverflow
    from k3code.extratools import register_skill_tool
    from k3code.prompting import build_system_prompt
    from k3code.providers import make_providers
    from k3code.reliability import BudgetExceeded, DiskGuardFull
    from k3code.reliability.persistent_retry import TurnCancelled
    from k3code.router import CooldownStore, Router, build_chain
    from k3code.routing.tiers import router_options

    if model and (err := _use_model_key(config, model)):
        print(f"[Error] {err}; using {config.default_model}")
        model = None
    system_prompt = build_system_prompt(_load_system_prompt(), cwd=Path.cwd(), config=config)

    providers = make_providers(config.providers)
    chain = build_chain(providers, _resolve_model_specs(config))
    cooldowns = CooldownStore(path=_cooldown_path())

    def make_router() -> Router:
        return Router(
            build_chain(providers, _resolve_model_specs(config)),
            cooldowns=cooldowns,
            on_event=_print_event,
            **router_options(config),
        )

    router = make_router()

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
    register_skill_tool(loop.tools, Path.cwd(), list(config.skills.roots))

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
            if err := _use_model_key(config, user_input[7:].strip()):
                print(f"[Error] {err}")
            else:
                loop.router = make_router()  # the chain resolves the key to each provider's model id
                print(f"Model set to: {config.default_model}")
            continue

        print()  # spacing
        final_text = ""

        async def on_text_delta(text: str) -> None:
            nonlocal final_text
            final_text += text
            sys.stdout.write(text)
            sys.stdout.flush()

        async def on_text_reset() -> None:
            nonlocal final_text
            final_text = ""
            print("\n[retrying: the partial reply above was discarded]")

        loop.on_text_delta = on_text_delta
        loop.on_text_reset = on_text_reset

        try:
            async for _ in loop.run(user_input, max_tokens=config.max_tokens, temperature=config.temperature):
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
    await _reap_jobs(loop.session_id)
    await reliability.stop()


def _permission_from_config(key: str, value: str) -> PermissionMode:
    """The PermissionMode for a config string. An unknown value is a usage error, not a ValueError traceback."""
    from k3code.permissions import InvalidPermissionMode, permission_mode_from_config

    try:
        return permission_mode_from_config(key, value)
    except InvalidPermissionMode as e:
        raise click.ClickException(str(e)) from None


@click.command()
@click.option("-p", "--prompt", "prompt", help="Headless prompt; omit for REPL")
@click.option("-m", "--model", help="Model override (e.g., 'default', 'cheap')")
@click.option(
    "--permission",
    type=click.Choice(["ask", "auto-edit", "yolo"]),
    default=None,
    help="Permission mode (default: permission_mode from the config, which defaults to ask)",
)
@click.option("--json", "json_output", is_flag=True, help="Output final result as JSON (headless only)")
@click.option("--session", "session", default="headless", help="Session id (journal + transcript name)")
@click.option("--resume", is_flag=True, help="Resume the session's saved transcript after a crash (headless)")
@click.option("--config-dir", type=click.Path(path_type=Path), help="Project directory for config")
@click.option("--repl", is_flag=True, help="Force the legacy line REPL instead of the TUI")
@click.option(
    "--stdio",
    "stdio_flag",
    is_flag=True,
    help="(gateway subcommand) serve JSON-RPC 2.0 on stdin/stdout",
)
def main(
    prompt: str | None,
    model: str | None,
    permission: str | None,
    json_output: bool,
    session: str,
    resume: bool,
    config_dir: Path | None,
    repl: bool,
    stdio_flag: bool,
) -> None:
    """k3code — terminal coding agent.

    Examples:
      k3code -p "create hello.py" --permission yolo
      k3code -p "fix the bug" --json
      k3code  # starts the TUI (or the REPL with --repl / non-tty)
    """
    if stdio_flag:
        # `k3code gateway --stdio` / `k3code --stdio`: JSON-RPC on stdio.
        _run_gateway()
        return

    import asyncio

    from k3code import trust
    from k3code.config import load_config
    from k3code.permissions import PermissionMode

    # Load config. A project config is applied only once the user trusted this exact file. Interactive opens are
    # asked here, in this process (the TUI's gateway is a piped child and cannot ask); headless runs never ask.
    project_dir = config_dir or Path.cwd()
    if not prompt and _is_interactive():
        _offer_project_trust(project_dir)
    config = load_config(project_dir=project_dir)
    if trust.decision(project_dir) in (trust.UNDECIDED, trust.DECLINED):
        click.echo(
            f"k3code: ignoring {trust.config_path(project_dir)} (not trusted; `k3code trust` applies it)", err=True
        )
    if not config.providers and (prompt or not _is_interactive()):
        from k3code.setup.onboard import NO_CONFIG_HINT

        click.echo(NO_CONFIG_HINT, err=True)
        sys.exit(78)  # EX_CONFIG: never prompt in headless or piped runs

    # The flag wins; then headless_permission (-p only); then the config's permission_mode. The flag used to default
    # to "ask", which silently ignored a configured permission_mode in -p and REPL runs.
    if permission:
        permission_mode = PermissionMode(permission)  # --permission is a click.Choice, so this cannot fail
    elif prompt and config.headless_permission:
        permission_mode = _permission_from_config("headless_permission", config.headless_permission)
    else:
        permission_mode = _permission_from_config("permission_mode", config.permission_mode)

    if prompt:
        result = asyncio.run(
            _run_headless(
                prompt,
                model=model,
                permission_mode=permission_mode,
                config=config,
                json_output=json_output,
                session=session,
                resume=resume,
            )
        )
        if json_output and result:
            print(json.dumps(result, ensure_ascii=False))
        elif result:
            if result.get("text") and not result["text"].endswith("\n"):
                sys.stdout.write("\n")  # the streamed answer: end it so the shell prompt starts on its own line
            if "error" in result:
                click.echo(f"k3code: error: {result.get('message') or result['error']}", err=True)
        sys.exit(0 if result and "error" not in result else 1)
    elif repl or not _is_interactive():
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run_repl(model=model, permission_mode=permission_mode, config=config))
    else:
        if model and (err := _use_model_key(config, model)):
            raise click.ClickException(err)
        _launch_tui(model=model, permission_mode=permission_mode if permission else None, project_dir=project_dir)


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _offer_project_trust(project_dir: Path) -> None:
    """Show what the project config changes and remember the answer; asked once per version of the file."""
    from k3code import trust

    if trust.decision(project_dir) != trust.UNDECIDED:
        return
    if (why := trust.problem(project_dir)) is not None:
        click.echo(f"{trust.config_path(project_dir)} is ignored: {why}.", err=True)
        return
    click.echo(f"{trust.config_path(project_dir)} changes how k3code runs in this project:", err=True)
    for line in trust.summary(project_dir) or []:
        click.echo(f"  - {line}", err=True)
    answer = click.confirm("Trust this project config?", default=False, err=True)
    trust.record(project_dir, trusted=answer)


def _run_gateway() -> None:
    """Serve the JSON-RPC gateway on stdio (stdout = frames, stderr = logs)."""
    import asyncio

    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("K3CODE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,  # the import-time basicConfig above already installed a handler
    )
    from k3code.gateway.server import GatewayServer

    async def _serve() -> None:
        server = GatewayServer()
        try:
            await server.serve()
        finally:
            await server.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve())


def _launch_tui(
    *,
    model: str | None = None,
    permission_mode: PermissionMode | None = None,
    project_dir: Path | None = None,
    env_extra: dict[str, str] | None = None,
    require: bool = False,
) -> None:
    """Spawn the built TUI (tui/dist/entry.js) with this process as its gateway.

    ``permission_mode`` (the --permission flag) and ``project_dir`` (--config-dir, else the cwd: the directory whose
    project config the user was asked to trust) reach the spawned gateway through K3CODE_PERMISSION_MODE and
    K3CODE_PROJECT_DIR.
    """
    from k3code.paths import find_node

    node = find_node()
    repo_root = _find_repo_root()
    entry = repo_root / "tui" / "dist" / "entry.js" if repo_root else None
    if node is None or entry is None or not entry.is_file():
        if require:
            click.echo("The TUI is not available (need node + tui/dist/entry.js).", err=True)
            sys.exit(1)
        logger.warning("TUI not available (need node + tui/dist/entry.js); falling back to REPL")
        import asyncio

        from k3code.config import load_config

        config = load_config(project_dir=project_dir or Path.cwd())
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(
                _run_repl(
                    model=model,
                    permission_mode=permission_mode
                    or _permission_from_config("permission_mode", config.permission_mode),
                    config=config,
                )
            )
        return

    env = os.environ.copy()
    # The TUI spawns `K3CODE_GATEWAY_CMD` as its Python gateway.
    env["K3CODE_GATEWAY_CMD"] = f"{sys.executable} -m k3code.cli gateway --stdio"
    env.update(env_extra or {})
    env.setdefault("K3CODE_LOG_LEVEL", "INFO")
    if model:
        env["K3CODE_DEFAULT_MODEL"] = model  # load_config maps it to default_model in the spawned gateway
    if permission_mode is not None:
        env["K3CODE_PERMISSION_MODE"] = permission_mode.value  # flag > config in the gateway's load_config
    if project_dir is not None:
        env["K3CODE_PROJECT_DIR"] = str(project_dir.resolve())  # config.default_project_dir() in the gateway
    logger.info("launching TUI: %s %s", node, entry)
    result = subprocess.run([node, str(entry)], env=env, check=False)
    sys.exit(result.returncode)


def _find_repo_root() -> Path | None:
    """The k3code root holding tui/dist/entry.js: our own source tree or install, never the cwd (see install_roots)."""
    from k3code.paths import install_roots

    for candidate in install_roots():
        if (candidate / "tui" / "dist" / "entry.js").is_file():
            return candidate
    return None


@click.group(invoke_without_command=True)
@click.version_option(__version__, "--version", prog_name="k3code")
@click.pass_context
@click.option("-p", "--prompt", help="Headless prompt; omit for TUI/REPL")
@click.option("-m", "--model", help="Model override (e.g., 'default', 'cheap')")
@click.option(
    "--permission",
    type=click.Choice(["ask", "auto-edit", "yolo"]),
    default=None,
    help="Permission mode (default: permission_mode from the config, which defaults to ask)",
)
@click.option("--json", "json_output", is_flag=True, help="Output final result as JSON (headless only)")
@click.option("--session", "session", default="headless", help="Session id (journal + transcript name)")
@click.option("--resume", is_flag=True, help="Resume the session's saved transcript after a crash (headless)")
@click.option("--config-dir", type=click.Path(path_type=Path), help="Project directory for config")
@click.option("--repl", is_flag=True, help="Force the legacy line REPL instead of the TUI")
def cli(
    ctx: click.Context,
    prompt: str | None,
    model: str | None,
    permission: str | None,
    json_output: bool,
    session: str,
    resume: bool,
    config_dir: Path | None,
    repl: bool,
) -> None:
    """k3code — terminal coding agent."""
    if ctx.invoked_subcommand is None:
        if prompt is None and _is_interactive():
            from k3code.setup.onboard import first_run
            from k3code.setup.prompter import InteractivePrompter

            try:
                first_run(InteractivePrompter())  # asks fast/full once while no config exists; never starts the wizard
            except (KeyboardInterrupt, EOFError):
                click.echo("\nSetup interrupted. Run `k3code onboard` any time to set up.")
                raise SystemExit(130) from None
        ctx.invoke(
            main,
            prompt=prompt,
            model=model,
            permission=permission,
            json_output=json_output,
            session=session,
            resume=resume,
            config_dir=config_dir,
            repl=repl,
            stdio_flag=False,
        )


@cli.command("gateway")
@click.option("--stdio", "stdio_flag", is_flag=True, default=True, help="Serve JSON-RPC 2.0 on stdin/stdout")
@click.option("--attach", is_flag=True, help="Bridge stdin/stdout to a running daemon's socket")
@click.option("--socket", "socket_opt", type=click.Path(path_type=Path), help="Daemon socket path")
@click.option("--readonly", is_flag=True, help="With --attach: refuse every request that would change the session")
def gateway(stdio_flag: bool, attach: bool, socket_opt: Path | None, readonly: bool) -> None:
    """Run the JSON-RPC gateway (what the TUI spawns), or attach to the daemon with --attach."""
    if attach:
        import asyncio

        from k3code.daemon import attach_bridge

        sys.exit(asyncio.run(attach_bridge(socket_opt, readonly=readonly)))
    _run_gateway()


@cli.command("attach")
@click.argument("session_id")
@click.option("--readonly", is_flag=True, help="Watch only: prompts and approvals from this window are refused")
@click.option("--socket", "socket_opt", type=click.Path(path_type=Path), help="Daemon socket path")
def attach_cmd(session_id: str, readonly: bool, socket_opt: Path | None) -> None:
    """Open the TUI on a session that runs in the daemon (what /bg --pane and /fork --pane start in a pane)."""
    cmd = f"{sys.executable} -m k3code.cli gateway --attach" + (" --readonly" if readonly else "")
    if socket_opt:
        cmd += f" --socket {shlex.quote(str(socket_opt))}"
    # K3CODE_TUI_CWD, as `k3code agents` sends it: only sessions this TUI creates use it (/new, the fresh session when
    # <session_id> is unknown); session.resume carries no cwd and /bg prefers the attached session's own cwd, so the
    # resumed session and where it runs stay as they are. Set explicitly: an inherited value would name another shell.
    _launch_tui(
        env_extra={"K3CODE_GATEWAY_CMD": cmd, "K3CODE_TUI_RESUME": session_id, "K3CODE_TUI_CWD": os.getcwd()},
        require=True,
    )


@cli.command("agents")
@click.option("--readonly", is_flag=True, help="Watch only: prompts and approvals from this window are refused")
@click.option("--socket", "socket_opt", type=click.Path(path_type=Path), help="Daemon socket path")
def agents_cmd(readonly: bool, socket_opt: Path | None) -> None:
    """Open the TUI on the daemon with the agent view showing (every session by state)."""
    cmd = f"{sys.executable} -m k3code.cli gateway --attach" + (" --readonly" if readonly else "")
    if socket_opt:
        cmd += f" --socket {shlex.quote(str(socket_opt))}"
    # An empty K3CODE_TUI_RESUME: an inherited one would resume that session instead of starting fresh.
    # K3CODE_TUI_CWD: the session the TUI forges is this shell's project, not the daemon's launch directory.
    _launch_tui(
        env_extra={
            "K3CODE_GATEWAY_CMD": cmd,
            "K3CODE_TUI_RESUME": "",
            "K3CODE_TUI_VIEW": "agents",
            "K3CODE_TUI_CWD": os.getcwd(),
        },
        require=True,
    )


@cli.command("tail")
@click.argument("subagent_id")
@click.option("--socket", "socket_opt", type=click.Path(path_type=Path), help="Daemon socket path")
def tail_cmd(subagent_id: str, socket_opt: Path | None) -> None:
    """Follow a sub-agent of the daemon, read-only (what fan-out opens per child with autonomy.fanout.panes)."""
    import asyncio

    from k3code.daemon import tail_subagent

    sys.exit(asyncio.run(tail_subagent(subagent_id, socket_opt)))


@cli.command("daemon")
def daemon_cmd() -> None:
    """Run the long-lived host: sessions keep running while no TUI is attached (systemd: Type=notify)."""
    import asyncio

    from k3code.daemon import DaemonAlreadyRunning, run_daemon

    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("K3CODE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,  # the import-time basicConfig above already installed a handler
    )
    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run_daemon())
    except DaemonAlreadyRunning as e:
        click.echo(f"k3code daemon: {e}. Use `k3code attach`, or `k3code service restart` to restart it.", err=True)
        raise SystemExit(75) from e  # EX_TEMPFAIL


@cli.group("service")
def service_group() -> None:
    """Manage the systemd user unit for the daemon."""


@service_group.command("install")
@click.option("--dry-run", is_flag=True, help="Print what would happen; change nothing")
def service_install(dry_run: bool) -> None:
    from k3code import service

    click.echo("\n".join(service.install(dry_run=dry_run)))


@service_group.command("uninstall")
@click.option("--dry-run", is_flag=True, help="Print what would happen; change nothing")
def service_uninstall(dry_run: bool) -> None:
    from k3code import service

    click.echo("\n".join(service.uninstall(dry_run=dry_run)))


@service_group.command("status")
def service_status() -> None:
    from k3code import service

    click.echo("\n".join(service.status()))


@cli.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@click.option("--no-probe", is_flag=True, help="Skip network probes")
@click.option("--install", "install_subset", is_flag=True, help="Install-time subset: warnings only, always exit 0")
def doctor_cmd(as_json: bool, no_probe: bool, install_subset: bool) -> None:
    """Health checks with fix hints. Exit status 1 when any check fails."""
    import asyncio

    from k3code import doctor
    from k3code.config import load_config

    if install_subset:
        checks = doctor.install_subset()
        click.echo(doctor.to_json(checks) if as_json else doctor.format_report(checks))
        return
    logging.getLogger("httpx").setLevel(logging.WARNING)
    checks = asyncio.run(doctor.run_checks(load_config(project_dir=Path.cwd()), probe=not no_probe))
    click.echo(doctor.to_json(checks) if as_json else doctor.format_report(checks))
    sys.exit(1 if doctor.summary(checks)[doctor.FAIL] else 0)


@cli.command("stats")
@click.option("--by", type=click.Choice(["day", "session", "turn"]), default="day")
@click.option("--days", type=int, default=None, help="Only the last N days")
@click.option("--json", "as_json", is_flag=True)
def stats_cmd(by: str, days: int | None, as_json: bool) -> None:
    """Usage per day or session from $K3CODE_HOME/usage.db."""
    from k3code.daemon import k3_home
    from k3code.usage import UsageDB, format_stats

    rows = UsageDB(k3_home() / "usage.db").aggregate(by, days=days)
    click.echo(json.dumps(rows, indent=2) if as_json else format_stats(rows, by))


@cli.command("export")
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.option("--all", "all_sessions", is_flag=True, help="Export every session")
@click.option("--session", "session_id", help="Export one session by id")
@click.option("--settings-only", is_flag=True)
@click.option("--session-only", is_flag=True)
def export_cmd(
    path: Path | None, all_sessions: bool, session_id: str | None, settings_only: bool, session_only: bool
) -> None:
    """Write a .k3bundle (settings with secrets redacted + sessions)."""
    from k3code.bundle import BundleError
    from k3code.commands.export import run_export
    from k3code.gateway.sessions import SessionStore
    from k3code.paths import home

    store = SessionStore(home() / "sessions.db")
    try:
        recent = store.most_recent()
        out, manifest = run_export(
            store,
            Path.cwd(),
            path=str(path) if path else None,
            all_sessions=all_sessions,
            session=session_id,
            current=recent.session_id if recent else None,
            settings_only=settings_only,
            session_only=session_only,
        )
    except BundleError as e:
        raise click.ClickException(str(e)) from e
    finally:
        store.close()
    c = manifest["contents"]
    click.echo(
        f"Exported {len(c['sessions'])} session(s), {len(c['settings'])} settings file(s) to {out} (secrets redacted)."
    )


@cli.command("import")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation (headless)")
@click.option("--settings-only", is_flag=True)
@click.option("--session-only", is_flag=True)
def import_cmd(path: Path, yes: bool, settings_only: bool, session_only: bool) -> None:
    """Import a .k3bundle: merge settings (existing config backed up) and sessions."""
    from k3code.bundle import BundleError, apply_bundle, read_bundle
    from k3code.gateway.sessions import SessionStore
    from k3code.paths import home

    try:
        bundle = read_bundle(path)
    except BundleError as e:
        raise click.ClickException(str(e)) from e
    click.echo(bundle.describe())
    if not yes:
        click.confirm("Import this bundle? Existing config is backed up first.", abort=True)
    store = SessionStore(home() / "sessions.db")
    try:
        rep = apply_bundle(bundle, store=store, cwd=Path.cwd(), settings=not session_only, sessions=not settings_only)
    except (BundleError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    finally:
        store.close()
    click.echo("Imported.\n" + rep.describe())


@cli.command("setup")
@click.option("--step", "step", help="Re-run a single step (welcome, about, system, usage, providers, tiers, ...)")
@click.option("--non-interactive", is_flag=True, help="Take every answer from --answers")
@click.option("--answers", type=click.Path(path_type=Path, exists=True, dir_okay=False), help="YAML answers file")
@click.option("--restart", is_flag=True, help="Ignore saved progress and start over")
@click.option("--no-probe", is_flag=True, help="Skip live provider/integration tests")
def setup_cmd(step: str | None, non_interactive: bool, answers: Path | None, restart: bool, no_probe: bool) -> None:
    """Guided, resumable first-run setup."""
    from k3code.setup.prompter import AnswerPrompter, InteractivePrompter
    from k3code.setup.wizard import run_setup

    if non_interactive:
        if answers is None:
            raise click.UsageError("--non-interactive needs --answers FILE")
        p: Any = AnswerPrompter.from_file(answers)
    else:
        p = AnswerPrompter.from_file(answers) if answers else InteractivePrompter()
    try:
        run_setup(p, only_step=step, restart=restart, do_probe=not no_probe)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except (KeyboardInterrupt, EOFError):
        click.echo("\nInterrupted; progress saved. Re-run `k3code setup` to resume.")
        raise SystemExit(130) from None


@cli.command("onboard")
@click.option(
    "--answers", type=click.Path(path_type=Path, exists=True, dir_okay=False), help="YAML answers file (no prompts)"
)
@click.option("--no-probe", is_flag=True, help="Skip the live models call")
def onboard_cmd(answers: Path | None, no_probe: bool) -> None:
    """Guided first-run setup: fast (API endpoint + key) or full. Run it any time."""
    from k3code.setup.onboard import run_onboarding
    from k3code.setup.prompter import AnswerPrompter, InteractivePrompter, Prompter

    if answers is None and not _is_interactive():
        raise click.UsageError("`k3code onboard` needs a terminal; pass --answers FILE to run it without prompts")
    p: Prompter = AnswerPrompter.from_file(answers) if answers else InteractivePrompter()
    try:
        run_onboarding(p, do_probe=not no_probe)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except (KeyboardInterrupt, EOFError):
        click.echo("\nInterrupted. Run `k3code onboard` again to finish.")
        raise SystemExit(130) from None


@cli.command("update")
@click.option("--check", is_flag=True, help="Only report current and latest version")
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation")
@click.option("--channel", type=click.Choice(["stable", "dev"]), help="Release channel (default: update.channel)")
@click.option("--from-source", is_flag=True, help="git pull the source checkout and rebuild")
@click.option("--no-pull", is_flag=True, help="With --from-source: rebuild without git pull (you pulled the clone)")
@click.option("--rollback", "do_rollback", is_flag=True, help="Switch back to the previous version")
def update_cmd(
    check: bool, yes: bool, channel: str | None, from_source: bool, no_pull: bool, do_rollback: bool
) -> None:
    """Update to the latest release (smoke-tested, auto-rollback) or roll back.

    Whenever there is no release to fetch (none published yet, or a private repository and no token), an install
    built from a checkout (`install.sh --from-source`) updates from that checkout, and an install made with
    `install.sh --from-git` (the default) rebuilds from the newest commit of the branch or tag it was made from
    (`update.url`, default: the GitHub repository `update.repo`)."""
    from k3code import update as upd

    if do_rollback:
        res = upd.rollback()
        click.echo(res.message)
        raise SystemExit(0 if res.ok else 1)
    cfg = upd.update_settings()
    cur = upd.current_version()
    rel = None
    git_ref = None
    if not from_source:
        denied = ""
        try:
            rel = upd.fetch_latest(channel or cfg["channel"], cfg["repo"], upd.github_token())
        except PermissionError as e:
            denied = str(e)
        except Exception as e:  # noqa: BLE001
            raise click.ClickException(str(e)) from e
        if rel is None:
            src = upd.source_checkout()
            if src is not None and (src / ".git").exists():
                click.echo(
                    f"{denied or 'No release has been published yet'}.\nThis install is built from {src}: using it."
                )
                from_source = True
            elif (git_ref := upd.git_ref()) is not None:
                click.echo(
                    f"{denied or 'No release has been published yet'}.\n"
                    f"This install was made from git ({git_ref} of {cfg['url']}): checking it."
                )
            else:
                raise click.ClickException(
                    denied
                    or "no releases found on this channel, and this install keeps no checkout or git ref to update "
                    "from. Reinstall with `sh install.sh --from-git` (it can update itself from then on), or set "
                    "update.source in config.yaml to a k3code checkout"
                )
    if git_ref is not None:
        try:
            head = upd.remote_head(cfg["url"], git_ref)
        except upd.SourceUpdateError as e:
            raise click.ClickException(str(e)) from e
        if upd.is_commit_sha(git_ref):
            click.echo(
                f"current: {cur}\nThis install is pinned to commit {git_ref[:12]}: there is nothing to update. "
                f"Reinstall with `sh install.sh --from-git --ref Main` to follow a branch."
            )
            return
        click.echo(f"current: {cur}\nlatest:  {head[:7]} ({git_ref})")
        if check:
            return
        have = upd.installed_sha()
        if have and head.startswith(have):
            click.echo("Already up to date.")
            return
        if not yes:
            click.confirm(f"Update to {head[:7]} ({git_ref})?", abort=True)
        try:
            ver = upd.update_from_git(cfg["url"], git_ref)
        except upd.SourceUpdateError as e:
            raise click.ClickException(str(e)) from e
        if ver == cur:  # the installer's own fetch failed and it fell back to the installed build of the ref
            raise click.ClickException(
                f"the installer could not fetch {git_ref} and kept the installed {ver}: nothing was changed. "
                "Try again later."
            )
    elif from_source:
        src = upd.source_checkout()
        if src is None or not (src / ".git").exists():
            raise click.ClickException("no source checkout known; set update.source in config.yaml")
        click.echo(f"current: {cur}\nsource: {src}")
        if check:
            return
        if not yes:
            click.confirm("rebuild from the checkout?" if no_pull else "git pull and rebuild?", abort=True)
        try:
            ver = upd.update_from_source(src, pull=not no_pull)
        except upd.SourceUpdateError as e:
            raise click.ClickException(str(e)) from e
    else:
        assert rel is not None
        click.echo(f"current: {cur}\nlatest:  {rel.version}\n\n{rel.body.strip()[:2000]}")
        if check or not upd.is_newer(rel.version, cur):
            if not check:
                click.echo("Already up to date.")
            return
        if not yes:
            click.confirm(f"Update to {rel.version}?", abort=True)
        upd.install_release(rel, upd.github_token())
        ver = rel.version
    res = upd.activate(ver)
    click.echo(res.message)
    if res.ok:
        upd.prune()
    raise SystemExit(0 if res.ok else 1)


@cli.command("trust")
@click.argument("path", required=False, type=click.Path(path_type=Path, file_okay=False))
@click.option("--revoke", is_flag=True, help="Forget the answer: the project config is ignored again until trusted")
def trust_cmd(path: Path | None, revoke: bool) -> None:
    """Trust a project's .k3code/config.yaml (PATH, default: the current directory).

    Its MCP servers, permission rules and providers apply from the next start. Headless and piped runs ignore a
    project config that is not trusted; an interactive open asks again when the file changes.
    """
    from k3code import trust

    project_dir = path or Path.cwd()
    where = trust.config_path(project_dir)
    if revoke:
        if trust.revoke(project_dir):
            click.echo(f"trust revoked for {where}: it is ignored until you trust it again")
        else:
            click.echo(f"no trust answer is recorded for {where}")
        return
    if (why := trust.problem(project_dir)) is not None:
        click.echo(f"{where} cannot be trusted: {why}. Fix it first.", err=True)
        raise SystemExit(1)
    lines = trust.summary(project_dir)
    if lines is None:
        click.echo(f"no project settings in {where}; nothing to trust")
        return
    click.echo(f"{where} changes how k3code runs in this project:")
    for line in lines:
        click.echo(f"  - {line}")
    trust.record(project_dir, trusted=True)
    click.echo("trusted: the project config applies from the next start")


@cli.command("config-edit")
@click.option("--project", is_flag=True, help="Edit the project config instead of the user config")
def config_edit_cmd(project: bool) -> None:
    """Open the user (or project) config in $EDITOR."""
    from k3code.commands.config_cmd import open_editor, target_path

    raise SystemExit(open_editor(target_path(Path.cwd(), project)))


@cli.command("memory")
@click.argument("action", type=click.Choice(["edit", "list"]), default="list")
@click.option("--user", is_flag=True, help="User memory instead of project memory")
def memory_cmd(action: str, user: bool) -> None:
    """List memory files, or open one in $EDITOR."""
    from k3code.commands.config_cmd import open_editor
    from k3code.memory import project_memory_path, user_memory_path

    target = user_memory_path() if user else project_memory_path(Path.cwd())
    if action == "edit":
        raise SystemExit(open_editor(target))
    for scope, p in (("user", user_memory_path()), ("project", project_memory_path(Path.cwd()))):
        click.echo(f"{scope}: {p}  " + (f"{p.stat().st_size} bytes" if p.is_file() else "(missing)"))


# ── schedule (cron jobs) ──────────────────────────────────────────────


def _scheduler_db() -> Any:
    from k3code.automation.clock import SystemClock
    from k3code.automation.scheduler import JobScheduler
    from k3code.automation.store import AutomationDB
    from k3code.daemon import k3_home

    db = AutomationDB(k3_home() / "automation.db")
    return db, JobScheduler(db, None, SystemClock())  # type: ignore[arg-type]  # CRUD only; the daemon runs jobs


@cli.group("schedule")
def schedule_group() -> None:
    """Manage cron jobs (the daemon runs them; changes are picked up within seconds)."""


@schedule_group.command("add")
@click.argument("expr")
@click.argument("prompt", nargs=-1, required=True)
@click.option("--cwd", type=click.Path(path_type=Path), default=None, help="Working directory for the run")
@click.option("--model", default="", help="Model key / tier for the run")
@click.option("--name", default="", help="Job name")
def schedule_add(expr: str, prompt: tuple[str, ...], cwd: Path | None, model: str, name: str) -> None:
    """Add a job: EXPR is a cron expression, an interval (30m) or `daily 09:00`."""
    from k3code.automation.cronexpr import ScheduleError

    db, sched = _scheduler_db()
    try:
        job = sched.add(
            prompt=" ".join(prompt),
            schedule=expr,
            name=name,
            model=model,
            cwd=str((cwd or Path.cwd()).expanduser().resolve()),
        )
    except ScheduleError as e:
        hint = "For natural language ('every weekday at 9') use /schedule add inside k3code."
        raise click.ClickException(f"{e}\n{hint}") from e
    click.echo(f"Scheduled {job['id']} “{job['name']}” ({expr}).")
    db.close()


@schedule_group.command("list")
def schedule_list() -> None:
    """Show jobs with their recent run history."""
    import time

    from k3code.automation.scheduler import format_jobs

    db, _ = _scheduler_db()
    click.echo(format_jobs(db, time.time()))
    db.close()


def _schedule_action(name: str, ref: str) -> None:
    db, sched = _scheduler_db()
    fn = {"rm": sched.remove, "pause": sched.pause, "resume": sched.resume, "run": sched.run_now}[name]
    ok = fn(ref)
    db.close()
    if not ok:
        raise click.ClickException(f"No such job: {ref}")
    done = {"rm": "Removed", "pause": "Paused", "resume": "Resumed", "run": "Queued (the daemon picks it up shortly)"}
    click.echo(done[name])


for _name in ("rm", "pause", "resume", "run"):
    schedule_group.command(_name, help=f"{_name} a job by id or name")(
        click.argument("ref")(lambda ref, _n=_name: _schedule_action(_n, ref))
    )


@cli.command("slash")
@click.argument("command", nargs=-1, required=True)
@click.option("--socket", "socket_opt", type=click.Path(path_type=Path), help="Daemon socket path")
def slash_cmd(command: tuple[str, ...], socket_opt: Path | None) -> None:
    """Run a slash command against the running daemon, e.g. `k3code slash /automations list`."""
    import asyncio

    from k3code.daemon import slash_via_daemon

    try:
        out = asyncio.run(slash_via_daemon(" ".join(command), cwd=str(Path.cwd()), sock=socket_opt))
    except TimeoutError as e:
        raise click.ClickException("the daemon did not answer within 60 s") from e
    except OSError as e:
        raise click.ClickException(f"cannot reach the daemon ({e}); start it with `k3code daemon`") from e
    click.echo(out)


# Keep the guard last: every @cli.command / @cli.group above must be registered before cli() runs.
if __name__ == "__main__":
    cli()
