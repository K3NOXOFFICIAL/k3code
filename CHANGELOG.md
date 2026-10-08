# Changelog

All notable changes are listed here. Until version 1.0, any release may change the config format, the commands or the on-disk state. Versions follow [semantic versioning](https://semver.org/) from 1.0 on.

## [0.0.1] - unreleased (not tagged)

The first version meant for people other than its author. It is alpha: the core works and is tested, but live-model behaviour, long unattended runs and multi-day use are not yet verified. The [Status section of the README](README.md#status) has the details.

### What works today

- **Coding agent.** Read, edit, patch, bash, grep, glob, web fetch and search, and todo tools. Plan mode, MCP servers, skills, project and user memory, output styles, and extra working directories.
- **Terminal UI.** An agent list under the input box with live states (working, needs input, completed, failed), focus mode, input history kept per project, slash commands with completion, and permission modes cycled with Shift+Tab.
- **Permissions.** Five modes (`default`, `accept-edits`, `plan`, `auto`, `yolo`), a hardline list that is refused in every mode, and approval rules saved per project. Bash runs in a bubblewrap sandbox in `auto`, `yolo` and background sessions when `bwrap` is available.
- **Provider routing.** An ordered chain of providers and models with error classification, cooldowns and failover. A long rate limit fails over instead of sleeping. Background work goes to cheaper tiers.
- **Reliability.** A connectivity monitor that pauses a turn while offline and resumes it by itself, retry with jittered backoff, a crash-safe journal of tool calls, a resource governor, and optional token and spend caps.
- **Autonomy.** Task scope classification, a planning turn for larger tasks, fan-out of independent parts into git worktrees with review and tests, `/goal`, `/loop`, `/ultracode` and `/ultraresearch`.
- **Automation.** Cron schedules and file, git, webhook, session, network and idle triggers, run by the daemon.
- **Learning.** A decision log, permission-rule suggestions, proposals, project preparation, and proposed changes to its own configuration, which can be A/B tested.
- **Setup and operations.** An installer for Linux and macOS, `install.ps1` for Windows (installs into WSL and adds `k3code` and `k3` commands to Windows), guided and resumable setup, `k3code doctor`, export and import bundles with secrets redacted, and an update command with rollback.
- **`k3`.** A multi-window terminal (a TUIOS fork) that shows agent states and pending approvals in its inbox.

### Not yet

- Live-model behaviour. Most exit checks use a scripted fake provider. The live-model rows ran through the `claude-cli` provider (a local Claude Code login).
- The 72-hour daemon soak. A 30-minute soak passed.
- Automatic resumption of interrupted turns in the daemon. `k3code doctor` flags unresolved journal entries.
- `/artifacts publish`. It reports that publishing is not implemented.
- Platforms other than Linux on x86_64. macOS and Windows (through WSL) are untested on real machines.
- The update path against published releases. There are none yet, so the update and rollback path was only tested against local version directories.

### Changed before publication

- Hard-coded defaults that pointed at the author's own servers were replaced with neutral values. Web search has no default SearXNG instance, so the keyless DuckDuckGo fallback is used until `research.searxng_url` is set, and whenever that SearXNG is unreachable. The OmniRoute preset points at `localhost:20128`. The hardline list names placeholder hosts.
- If you relied on the old defaults, set your own values in your config. To keep protecting your own hosts from remote service restarts, add patterns under `permissions.hardline`.
- Pull requests and pushes are checked for secrets (`.github/workflows/gitleaks.yml`).

### Fixed during the long-run audit

- Redaction, install and update, and TUI and mid-turn prompt findings.
- Gateway transport and sub-agent findings, and hardening of bash, file tools, permissions and the sandbox.
- Orphan tool results that were sent to providers on the second turn of a tool-using session.
- Two daemon bugs found by an earlier soak run, and single-instance daemon handling.
- A stall in the mem0 preference writer, which blocked every session while it ran.
