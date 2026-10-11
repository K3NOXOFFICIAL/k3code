"""Configuration loading with precedence: CLI > env > project config > user config > defaults."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from k3code import trust

logger = logging.getLogger(__name__)

K3CODE_HOME = Path(os.environ.get("K3CODE_HOME", Path.home() / ".k3code")).expanduser()


class ProviderEntry(BaseModel):
    """One provider block in the fallback chain."""

    name: str
    kind: str  # "openai", "anthropic" or "claude-cli" (the local Claude Code login, no key needed)
    base_url: str = ""  # required for "openai" / "anthropic"
    api_key_env: str = ""  # required for "openai" / "anthropic"
    api_key: str = ""  # populated by load_config() from the api_key_env var; never set this directly
    models: dict[str, str | list[str]] = Field(default_factory=dict)  # {default, cheap, ...}
    #: Optional one-line text per model key ({default: "Everyday coding", cheap: "Fast and cheap"}), shown by /tune.
    descriptions: dict[str, str] = Field(default_factory=dict)
    #: M4a: model list per tier, e.g. {strong: [opus], cheap: haiku}; falls back to models[<tier>], then models.default.
    tiers: dict[str, str | list[str]] = Field(default_factory=dict)
    #: claude-cli only: hidden "thinking" budget per call (MAX_THINKING_TOKENS). 0 turns it off; None keeps Claude
    #: Code's own default. Left on, Haiku 4.5 spent ~20x more output tokens than it needed on mechanical tasks, which
    #: ate most of the saving from routing unattended work to the cheap tier.
    thinking_tokens: int | None = 0
    #: ...for models whose name contains one of these (default: Haiku, the cheap tier); the others keep the CLI default
    thinking_models: list[str] = Field(default_factory=lambda: ["haiku"])
    #: claude-cli only: ``--effort`` (low|medium|high|xhigh|max) for turns where /effort sets none; None = the CLI's
    #: own default. /effort always wins.
    effort: str | None = None
    #: claude-cli only: keep a live ``claude -p`` process per conversation and send each step only what is new (the
    #: CLI start is paid once, and the prompt cache covers the earlier conversation). false = one stateless call per
    #: step. At most ``max_sessions`` processes (~270 MB each) live at once; one idle ``idle_seconds`` is stopped.
    persistent: bool = True
    max_sessions: int = 4
    idle_seconds: float = 600.0
    #: Prompt-cache breakpoints (cache_control): auto = on for kind anthropic, off for openai; on for an openai entry
    #: marks messages Anthropic-style only when the model id looks like Claude (a relay to Anthropic passes it on).
    prompt_cache: str = "auto"
    #: How long a cache entry lives after its last use: "5m" (the default) or "1h" (written at twice the price of
    #: a 5-minute entry, read at the same discount). A /loop or cron job that runs every 5 minutes or more finds a
    #: 5-minute entry expired on every tick; "1h" keeps its prompt cached between ticks. Anthropic's API only: a relay
    #: that rejects unknown cache_control fields needs "5m".
    cache_ttl: str = "5m"

    @field_validator("cache_ttl")
    @classmethod
    def validate_cache_ttl(cls, v: str) -> str:
        if v not in ("5m", "1h"):
            raise ValueError("cache_ttl must be '5m' or '1h'")
        return v

    @field_validator("prompt_cache")
    @classmethod
    def validate_prompt_cache(cls, v: str) -> str:
        if v not in ("auto", "on", "off"):
            raise ValueError("prompt_cache must be 'auto', 'on' or 'off'")
        return v

    @field_validator("effort")
    @classmethod
    def validate_effort(cls, v: str | None) -> str | None:
        from k3code.providers.effort import LEVELS  # not at module level: k3code.providers imports this module

        if v is not None and v not in LEVELS:
            raise ValueError(f"effort must be one of {', '.join(LEVELS)}")
        return v

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
    #: name of an environment variable (or of a key in ~/.config/k3code/env) whose value is sent as a bearer token;
    #: read when the server connects, so the secret itself never appears in config.yaml
    bearer_env: str = ""
    enabled: bool = True


def env_value(name: str) -> str:
    """A variable from the process environment, else from the env file the setup wizard writes. Never logged."""
    from k3code.setup.state import read_env_file

    return os.environ.get(name) or read_env_file().get(name, "")


class McpConfig(BaseModel):
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)


class SkillsConfig(BaseModel):
    roots: list[str] = Field(default_factory=list)
    #: also load ~/.claude/skills; read from the user's config only (skills.import_claude_enabled)
    import_claude: bool = True


class Mem0Config(BaseModel):
    url: str = ""
    api_key_env: str = ""
    user_id: str = ""
    agent_id: str = "k3code"


class DisplayConfig(BaseModel):
    # TUI-only settings (tui_theme, tui_status_indicator, tui_statusbar, battery, pet, ...) are kept as extra
    # fields: the gateway stores them for the TUI (gateway/tui_display.py) and config.full hands them back.
    model_config = ConfigDict(extra="allow")

    theme: str = ""
    focus_mode: bool = False


class GoalConfig(BaseModel):
    max_turns: int = 0  # judged turns before a goal pauses; 0 = no limit (the goal runs until done or blocked)
    #: failed --check runs in a row before a goal pauses; 0 = no limit. A check that can never pass would otherwise
    #: keep the goal (and the token spend) going until someone looks; /goal resume starts the count again.
    gate_max_retries: int = 20
    judge_model: str = "cheap"


class SubagentsConfig(BaseModel):
    #: model calls a sub-agent may make when the global ``max_turns`` is 0 (no cap). Nobody watches a sub-agent while
    #: its parent waits, so it never runs unbounded; a positive global ``max_turns`` wins when it is smaller.
    max_turns: int = 200


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
    max_turns: int = 0  # model calls per prompt; 0 = no cap (a reached cap stops the turn as needs_input)
    max_tokens: int = 8192
    temperature: float | None = None
    permission_mode: str = "ask"  # ask | auto-edit | plan | auto | yolo
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
    subagents: SubagentsConfig = Field(default_factory=SubagentsConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)

    # router knobs: {max_inline_wait: 20, quota_cooldown: 3600}; see router.router.Router
    router: dict[str, Any] = Field(default_factory=dict)
    # M4a: task kind → tier overrides, e.g. {title: fast, review: strong}.
    task_tiers: dict[str, str] = Field(default_factory=dict)
    # M5 learning: see k3code.learning.DEFAULTS (enabled, perm_min_approvals, optimizer: {...}, ...)
    learning: dict[str, Any] = Field(default_factory=dict)
    # M4a autonomy: plan_first, gate_modes, advisor_auto, proposals, escalate_after, ...
    autonomy: dict[str, Any] = Field(default_factory=dict)
    # M4b: ultracode: {max_tokens, max_agents, min_scope}; research: {searxng_url, max_subquestions, ...}
    # min_scope: with the ultracode mode on (/ultracode on), a prompt rated at least this runs the pipeline.
    ultracode: dict[str, Any] = Field(default_factory=dict)
    research: dict[str, Any] = Field(default_factory=dict)
    # Wake words (ultracode, ultraplan, ultraresearch typed in a prompt run that mode once): {enabled: true,
    # ultracode: true, ultraplan: true, ultraresearch: true}; see k3code.wakewords
    wake_words: dict[str, Any] = Field(
        default_factory=lambda: {"enabled": True, "ultracode": True, "ultraplan": True, "ultraresearch": True}
    )
    # Web tools SSRF guard: {allow_private: false}. true lets web_fetch/web_browse reach loopback/private addresses;
    # the exact origin of research.searxng_url is always allowed. See k3code.net_guard.
    web: dict[str, Any] = Field(default_factory=dict)
    # Context management: {autocompact: true, compact_at_ratio: 0.7, compact_at_tokens: <absolute override>,
    # keep_messages: 8}; /autocompact edits the first three. See GatewayServer._maybe_compact and
    # k3code.context_budget. decision_model: {enabled: false, provider: "", model: "", at_ratio: 0.5}: a small model
    # picks which old tool results the main model still needs (k3code.context_select)
    context: dict[str, Any] = Field(default_factory=dict)
    # Per model id: {<model id>: {context_window: 200000}}; ids without an entry use k3code.context_budget's defaults
    models: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Browser for web tools: {cdp_url: ""} (empty = off, never attach to a running browser by default)
    browser: dict[str, Any] = Field(default_factory=dict)
    # /artifacts publish: {publish_dir: "" (default <home>/published), publish_url: "" (template, e.g.
    # https://example.com/{name}; nothing is uploaded, the link is only printed)}
    artifacts: dict[str, Any] = Field(default_factory=dict)
    # User hooks: {PreToolUse: [{matcher, command, timeout}], ...}. Run from the user's config and, once the project
    # is trusted, the project's: both apply (k3code.userhooks.load reads each file; this merged value is not used).
    hooks: dict[str, Any] = Field(default_factory=dict)
    # What the daemon prunes at start: {usage_days: 180, journal_days: 30, debug_bundles: 10, decisions_days: 365};
    # see retention()
    retention: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_searxng(cls, data: Any) -> Any:
        """The setup wizard used to write a top-level ``searxng: {url}``, which no code read. Move it to
        ``research.searxng_url`` so existing configs keep working; an explicit research value wins."""
        if isinstance(data, dict) and isinstance(data.get("searxng"), dict):
            data = dict(data)
            legacy = data.pop("searxng")
            research = dict(data.get("research") or {})
            if legacy.get("url") and not research.get("searxng_url"):
                research["searxng_url"] = legacy["url"]
            data["research"] = research
        return data

    @field_validator("ultracode")
    @classmethod
    def _validate_ultracode(cls, v: dict[str, Any]) -> dict[str, Any]:
        if "min_scope" in v:
            from k3code.autonomy.scope import SCOPES

            level = str(v["min_scope"]).strip().lower()
            if level not in SCOPES:
                raise ValueError(f"ultracode.min_scope must be one of {', '.join(SCOPES)} (got {v['min_scope']!r})")
            v = {**v, "min_scope": level}
        return v

    @field_validator("context")
    @classmethod
    def _validate_context(cls, v: dict[str, Any]) -> dict[str, Any]:
        """The automatic-compaction keys, checked when the config loads: a bad value used to raise inside every turn."""
        flag = v.get("autocompact")
        if flag is not None and not isinstance(flag, bool):
            raise ValueError(f"context.autocompact must be true or false (got {flag!r})")
        tokens = v.get("compact_at_tokens")
        if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0):
            # 0 has always meant "not set" here: configs that carry it keep loading
            raise ValueError(f"context.compact_at_tokens must be a whole number of tokens (got {tokens!r})")
        for key in ("keep_messages", "compact_input_chars"):
            n = v.get(key)
            if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
                raise ValueError(f"context.{key} must be a whole number, 1 or more (got {n!r})")
        ratio = v.get("compact_at_ratio")
        if ratio is not None and (isinstance(ratio, bool) or not isinstance(ratio, int | float) or not 0 < ratio <= 1):
            raise ValueError(f"context.compact_at_ratio must be a share of the window above 0 up to 1 (got {ratio!r})")
        return v

    @field_validator("wake_words")
    @classmethod
    def _validate_wake_words(cls, v: dict[str, Any]) -> dict[str, Any]:
        from k3code.wakewords import CONFIG_KEYS

        for key, value in v.items():
            if key in CONFIG_KEYS and not isinstance(value, bool):
                raise ValueError(f"wake_words.{key} must be true or false (got {value!r})")
        # a section that sets only some keys (the merge replaces the default as a whole) keeps the others on
        return {**dict.fromkeys(CONFIG_KEYS, True), **v}


#: ``retention`` defaults: usage.db rows (days), closed sessions' journal files (days), debug bundles kept (count),
#: learning decisions (days).
RETENTION_DEFAULTS: dict[str, int] = {"usage_days": 180, "journal_days": 30, "debug_bundles": 10, "decisions_days": 365}


def retention(config: Any) -> dict[str, int]:
    """``config.retention`` over :data:`RETENTION_DEFAULTS`; a value that is not a positive number keeps its default."""
    raw = getattr(config, "retention", None) or {}
    out = dict(RETENTION_DEFAULTS)
    for key in out:
        value = raw.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            out[key] = int(value)
    return out


def _current_home() -> Path:
    """``K3CODE_HOME`` read at call time (the module constant is import-time only)."""
    return Path(os.environ.get("K3CODE_HOME", Path.home() / ".k3code")).expanduser()


def _load_yaml(path: Path) -> dict[str, Any]:
    if path.is_file():
        with path.open() as f:
            return yaml.safe_load(f) or {}
    return {}


def load_user_section(name: str) -> Any:
    """One top-level section of the *user's* ``config.yaml`` only: never the project's, never the environment.

    For settings that widen what a sandboxed command can see, which a repository must not be able to do.
    """
    return _load_yaml(_current_home() / "config.yaml").get(name)


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge per top-level key: a key set in ``override`` replaces the whole base value.

    Nested sections and the ``providers`` list are replaced as a whole, never merged (a project config never sets
    ``providers``: load_config drops it).
    """
    result = base.copy()
    result.update(override)
    return result


def default_project_dir() -> Path:
    """The project whose config the gateway applies: K3CODE_PROJECT_DIR (set by `k3code --config-dir` for the TUI's
    gateway), else the cwd."""
    return Path(os.environ.get("K3CODE_PROJECT_DIR") or Path.cwd())


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

    # 3. Project config (<project_dir>/.k3code/config.yaml): applied only as the user trusted it (see k3code.trust)
    project_config = {}
    if project_dir:
        text = trust.trusted_text(project_dir)
        if text is None:
            logger.debug("project config %s not applied: not trusted (k3code trust applies it)", project_dir)
        else:
            project_config = yaml.safe_load(text) or {}
        # Providers come from the user config only: a repo's base_url plus api_key_env would send the user's key to
        # the repo author's host (trust.describe and doctor say the section is ignored).
        if isinstance(project_config, dict) and "providers" in project_config:
            project_config = {k: v for k, v in project_config.items() if k != "providers"}
            logger.warning(
                "%s: providers ignored (only the user config can set providers)", trust.config_path(project_dir)
            )
        # mem0 likewise: its url plus api_key_env is a second way to send a user key to a foreign host.
        if isinstance(project_config, dict) and "mem0" in project_config:
            project_config = {k: v for k, v in project_config.items() if k != "mem0"}
            logger.warning("%s: mem0 ignored (only the user config can set mem0)", trust.config_path(project_dir))

    # 4. Environment variables (only scalar top-level keys that exist in Settings; an empty value counts as unset)
    env_overrides: dict[str, Any] = {}
    for key in defaults:
        env_key = f"K3CODE_{key.upper()}"
        raw = os.environ.get(env_key, "")
        if not raw.strip():
            continue
        if not _is_scalar(defaults[key]):
            # Sections only come from a YAML file. The value is not echoed: env values can hold secrets.
            logger.warning("ignoring %s: %s is a config section, not a scalar; set it in config.yaml", env_key, key)
            continue
        env_overrides[key] = _parse_env_value(raw, defaults[key])

    # 5. CLI overrides
    cli = cli_overrides or {}

    # Merge in order
    merged = defaults
    merged = _merge_dicts(merged, user_config)
    merged = _merge_dicts(merged, project_config)
    merged = _merge_dicts(merged, env_overrides)
    merged = _merge_dicts(merged, cli)

    _warn_unknown_escalate_keys(merged)
    _warn_unknown_mode_keys(merged)

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


def _warn_unknown_escalate_keys(merged: dict[str, Any]) -> None:
    """A key under ``autonomy.escalate`` that nothing reads would be ignored without a word: say so."""
    from k3code.autonomy import ESCALATE_KEYS

    section = (merged.get("autonomy") or {}).get("escalate") or {}
    for key in sorted(set(section) - ESCALATE_KEYS):
        logger.warning(
            "autonomy.escalate.%s is not used and is ignored (known keys: %s)", key, ", ".join(sorted(ESCALATE_KEYS))
        )


def _warn_unknown_mode_keys(merged: dict[str, Any]) -> None:
    """Keys under ``ultracode`` and ``wake_words`` that nothing reads (a typo) would be ignored without a word."""
    from k3code.autonomy import ULTRACODE_KEYS
    from k3code.wakewords import CONFIG_KEYS

    for name, known in (("ultracode", set(ULTRACODE_KEYS)), ("wake_words", set(CONFIG_KEYS))):
        section = merged.get(name)
        for key in sorted(set(section) - known if isinstance(section, dict) else (), key=str):
            logger.warning("%s.%s is not used and is ignored (known keys: %s)", name, key, ", ".join(sorted(known)))


def _is_scalar(default: Any) -> bool:
    """A plain value (str, bool, int, float or None). Mappings and lists are sections of the config."""
    return default is None or isinstance(default, (str, bool, int, float))


def _parse_env_value(value: str, default: Any) -> Any:
    """Coerce env string to the type of the default. A number that does not parse is passed on as text,
    so pydantic reports it with the field name, the same as a bad value in config.yaml."""
    if isinstance(default, bool):
        return value.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(value)
        except ValueError:
            return value
    if isinstance(default, float):
        try:
            return float(value)
        except ValueError:
            return value
    return value
