# Changelog

All notable changes are listed here. Until version 1.0, any release may change the config format, the commands or the on-disk state. Versions follow [semantic versioning](https://semver.org/) from 1.0 on.

## Unreleased

### Changed

- **Look.** The default theme is hot pink (deep pink, hot pink, violet-red rules, plum fills; darker raspberry on a light terminal). Errors, success, warnings and shell mode keep their own colours. Turns are separated by a full-width rule, the status line is one continuous rule between the conversation and the input, and the agent list has a labelled header (`agents (N) · ↓ to select`).
- **Agents.** ↓ from an empty prompt moves into the agent list; Enter on a sub-agent opens its live view, Esc goes back. Finished agents leave the list after a minute.
- **Tool output.** `bash`, `grep` and `glob` results reach the model and the screen as plain text (stdout as it is; stderr, an error note and a non-zero exit code only when there is something to say), not as the Python repr of a dict with every newline and quote escaped. `write`, `edit` and plain errors are one line too (`Wrote /path`, `Edited: 1 replacement`, `Error: File not found: x`).
- **Stored sessions.** At start the daemon deletes stored sessions that never got a message, have no title or per-session setting, are not live or referenced by any loop or automation (paused ones included), and were last touched more than 30 days ago. Every `k3code agents`, `n` and abandoned TUI start used to leave one behind for good.
- **`/help`** lists the gateway's commands by category (it showed only the TUI's own commands).
- **Logging.** `k3code -p` prints only warnings by default, ends the answer with a newline and says why on stderr when a run fails. `K3CODE_LOG_LEVEL=info` brings the log back.
- **Setup.** Fast setup over an existing config puts the new provider first and keeps the others as fallbacks. `k3code doctor` reports a provider without a key as a warning while another provider can still answer.

### Added

- **Agent view.** `←` on an empty input, `/agents` or `k3code agents` (on the daemon) opens a full-screen list of every session and sub-agent by state, plus earlier sessions of the current project; `session.list` rows now carry the session's `cwd`, and a `cwd` param filters the list to that project on the gateway.
- **`sandbox.home_readonly`** (user config only): `$HOME` entries that stay visible, read-only, inside the bash sandbox, for example `[".myapp"]`. The sandbox hid all of `$HOME`, so a model that checks a marker file in the home folder could never see it and repeated its first-run question in every `auto`/`yolo` session.

### Fixed

- Automations: `pause`, `resume`, `remove` and `test` by name could act on a different automation whose random id began with the same letters (`remove cafe` hit id `cafe1234`). A ref now resolves as exact id, then exact name, then unique id prefix; a name shared by several automations or an ambiguous prefix matches nothing.
- The TUI called 29 gateway methods that did not exist, and each printed "the terminal UI and the k3code backend are out of sync": window resize, `!cmd` shell mode (and `{!cmd}` inside a prompt), `/undo` and `/retry`, `/usage`, `/status`, `/save`, `/reload`, `/reload-mcp`, `/reload-skills`, pausing and steering sub-agents, and the command catalog. They exist now. `/rollback`, `/journey`, `/plugins`, `/tools`, `/btw`, `/skin`, `/personality`, `/fast`, `/verbose` and `/replay list|load` had no backend and are removed.
- Sub-agents: finished ones showed as working, elapsed times of about 56 years, and the live view said "unavailable" for every agent.
- A loop tick could start while a user turn was still preparing, and a prompt typed behind a tick ran unattended on the cheap tier.
- Reading key files, `.env`, `~/.config/k3code` and `/proc/*/environ` is refused in every mode, and "always allow" no longer saves a command's inline `VAR=secret` in the rule.
- The setup answer "focus mode on by default" is applied.

## [0.1.0] - 2026-10-08 (alpha, not tagged)

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
- Platforms other than Linux on x86_64. macOS and Windows (through WSL) are untested on real machines. The installer is untested on Debian and on aarch64.
- The browser tool needs Playwright, which `presetup` installs. That path is untested on a clean machine.
- The update path against published releases. There are none yet, so the update and rollback path was only tested against local version directories.

### Changed before publication

- Hard-coded defaults that pointed at the author's own servers were replaced with neutral values. Web search has no default SearXNG instance, so the keyless DuckDuckGo fallback is used until `research.searxng_url` is set, and whenever that SearXNG is unreachable. The OmniRoute preset is no longer a default: the wizard offers Claude Code's own login when `claude` is installed, otherwise a custom endpoint. Existing configs that name it keep working. The hardline list names placeholder hosts.
- If you relied on the old defaults, set your own values in your config. To keep protecting your own hosts from remote service restarts, add patterns under `permissions.hardline`.
- Pull requests and pushes are checked for secrets (`.github/workflows/gitleaks.yml`).

### Fixed during the long-run audit

- Redaction, install and update, and TUI and mid-turn prompt findings.
- Gateway transport and sub-agent findings, and hardening of bash, file tools, permissions and the sandbox.
- Orphan tool results that were sent to providers on the second turn of a tool-using session.
- Two daemon bugs found by an earlier soak run, and single-instance daemon handling.
- A stall in the mem0 preference writer, which blocked every session while it ran.
