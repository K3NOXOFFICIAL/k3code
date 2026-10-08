"""Configuration loading with precedence: CLI > env > project config > user config > defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

K3CODE_HOME = Path(os.environ.get("K3CODE_HOME", Path.home() / ".k3code")).expanduser()


class ProviderEntry(BaseModel):
    """One provider block in the fallback chain."""

    name: str
    kind: str  # "openai", "anthropic" or "claude-cli" (the local Claude Code login, no key needed)
    base_url: str = ""  # required for "openai" / "anthropic"
    api_key_env: str = ""  # required for "openai" / "anthropic"
    api_key: str = ""  # populated by load_config() from the api_key_env var; never set this directly
    models: dict[str, str | list[str]] = Field(default_factory=dict)  # {default, cheap, ...}
    #: M4a: model list per tier, e.g. {strong: [opus], cheap: haiku}; falls back to models[<tier>], then models.default.
    tiers: dict[str, str | list[str]] = Field(default_factory=dict)
    #: claude-cli only: hidden "thinking" budget per call (MAX_THINKING_TOKENS). 0 turns it off; None keeps Claude
    #: Code's own default. Left on, Haiku 4.5 spent ~20x more output tokens than it needed on mechanical tasks, which
    #: ate most of the saving from routing unattended work to the cheap tier.
    thinking_tokens: int | None = 0
    #: ...for models whose name contains one of these (default: Haiku, the cheap tier); the others keep the CLI default
    thinking_models: list[str] = Field(default_factory=lambda: ["haiku"])

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: str) -> str:
        if v not in ("openai", "anthropic", "claude-cli"):
            raise ValueError("kind must be 'openai', 'anthropic' or 'claude-cli'")
        return v

    @model_validator(mode="before")
    @classmethod
    def require_endpoint_for_api_kinds(cls, data: Any) -> Any:
        # API providers still must name their endpoint and key variable; only claude-cli has neither.
        if isinstance(data, dict) and data.get("kind") != "claude-cli":
            for key in ("base_url", "api_key_env"):
                if key not in data:
                    raise ValueError(f"{key} is required for provider kind {data.get('kind')!r}")
        return data


class McpServerConfig(BaseModel):
    """One ``mcp.servers.<name>`` entry: stdio (command) or streamable HTTP (url)."""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class McpConfig(BaseModel):
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)


class SkillsConfig(BaseModel):
    roots: list[str] = Field(default_factory=list)


class Mem0Config(BaseModel):
    url: str = ""
    api_key_env: str = ""
    user_id: str = ""
    agent_id: str = "k3code"


class DisplayConfig(BaseModel):
    theme: str = ""
    focus_mode: bool = False


class GoalConfig(BaseModel):
    max_turns: int = 30
    judge_model: str = "cheap"


class AutomationConfig(BaseModel):
    """/loop, /schedule and automations."""

    #: Deprecated: NL→cron and the --until judge go through ModelCaller (``classification`` / ``goal_judge`` kinds,
    #: tier policy ``task_tiers``); kept so existing configs still load.
    cheap_model: str = ""
    max_concurrent: int = 2  # unattended runs at once (governor slots)
    grace_hours: float = 6.0  # missed cron runs fire once on recovery within this window
    webhook_port: int | None = None  # None = webhook triggers disabled
    git_poll_seconds: float = 15.0
    idle_poll_seconds: float = 30.0
    suggestions: bool = True


class Settings(BaseModel):
    """Runtime settings for k3code."""

    providers: list[ProviderEntry] = Field(default_factory=list)
    default_model: str = "default"  # key into provider.models
    max_turns: int = 20
    max_tokens: int = 8192
    temperature: float | None = None
    permission_mode: str = "ask"  # ask | auto-edit | yolo
    headless_permission: str | None = None  # overrides permission_mode in -p mode
    # M2 reliability layer; keys mirror reliability.ReliabilitySettings, e.g.
    #   reliability: {enabled: true, max_wait: 3600,
    #                 flags: {netwatch: true, journal: true, ...},
    #                 session_tokens: 2000000, session_usd: 5.0}
    reliability: dict[str, Any] = Field(default_factory=dict)
    # permissions: {<tool>: "allow"|"ask"|"deny" | {<pattern>: action}, hardline: [regex]}
    permissions: dict[str, Any] = Field(default_factory=dict)
    output_style: str = "default"
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    mem0: Mem0Config = Field(default_factory=Mem0Config)
    goal: GoalConfig = Field(default_factory=GoalConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)

    # router knobs: {max_inline_wait: 20, quota_cooldown: 3600}; see router.router.Router
    router: dict[str, Any] = Field(default_factory=dict)
    # M4a: task kind → tier overrides, e.g. {title: fast, review: strong}.
    task_tiers: dict[str, str] = Field(default_factory=dict)
    # M5 learning: see k3code.learning.DEFAULTS (enabled, perm_min_approvals, optimizer: {...}, ...)
    learning: dict[str, Any] = Field(default_factory=dict)
    # M4a autonomy: plan_first, gate_modes, advisor_auto, proposals, escalate_after, ...
    autonomy: dict[str, Any] = Field(default_factory=dict)
    # M4b: ultracode: {max_tokens, max_agents}; research: {searxng_url, max_subquestions, ...}
    ultracode: dict[str, Any] = Field(default_factory=dict)
    research: dict[str, Any] = Field(default_factory=dict)
    # Context management: {compact_at_tokens: 80000, keep_messages: 8}; see GatewayServer._maybe_compact
    context: dict[str, Any] = Field(default_factory=dict)


def _current_home() -> Path:
    """``K3CODE_HOME`` read at call time (the module constant is import-time only)."""
    return Path(os.environ.get("K3CODE_HOME", Path.home() / ".k3code")).expanduser()


def _load_yaml(path: Path) -> dict[str, Any]:
    if path.is_file():
        with path.open() as f:
            return yaml.safe_load(f) or {}
    return {}


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge per top-level key: a key set in ``override`` replaces the whole base value.

    Nested sections and the ``providers`` list are replaced as a whole, never merged.
    """
    result = base.copy()
    result.update(override)
    return result


def load_config(
    *,
    project_dir: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> Settings:
    """Load settings with full precedence chain.

    Order (later wins): defaults < user config < project config < env < CLI flags.
    """
    # 1. Defaults
    defaults = Settings().model_dump()

    # 2. User config (~/.k3code/config.yaml)
    user_config = _load_yaml(_current_home() / "config.yaml")

    # 3. Project config (<project_dir>/.k3code/config.yaml)
    project_config = {}
    if project_dir:
        project_config = _load_yaml(project_dir / ".k3code" / "config.yaml")

    # 4. Environment variables (only top-level keys that exist in Settings)
    env_overrides: dict[str, Any] = {}
    for key in defaults:
        env_key = f"K3CODE_{key.upper()}"
        if env_key in os.environ:
            env_overrides[key] = _parse_env_value(os.environ[env_key], defaults[key])

    # 5. CLI overrides
    cli = cli_overrides or {}

    # Merge in order
    merged = defaults
    merged = _merge_dicts(merged, user_config)
    merged = _merge_dicts(merged, project_config)
    merged = _merge_dicts(merged, env_overrides)
    merged = _merge_dicts(merged, cli)

    # Expand provider api_key_env -> api_key (api_key_env is a required field on
    # ProviderEntry, so look it up without popping it out of the dict).
    # The process environment wins; otherwise fall back to the env file the setup wizard
    # writes (~/.config/k3code/env), so a key stored there also works outside systemd.
    from k3code.setup.state import read_env_file

    file_env = read_env_file()
    for p in merged.get("providers", []):
        name = p.get("api_key_env", "")
        p["api_key"] = os.environ.get(name) or file_env.get(name, "")

    return Settings(**merged)


def _parse_env_value(value: str, default: Any) -> Any:
    """Coerce env string to the type of the default."""
    if isinstance(default, bool):
        return value.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, list):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value
