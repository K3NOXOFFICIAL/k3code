# k3code

**A terminal coding agent that keeps working when you are not looking.**

k3code plans before it acts, can run several agents in parallel, retries through dropped connections and provider outages, and sends cheap background work to cheaper models. It has a Python core, a terminal UI (TypeScript, Ink) and an optional multi-window terminal (`k3`, a fork of TUIOS). It works with OpenAI-compatible and Anthropic providers, and with a local Claude Code login.

> **Status: alpha, version 0.1.0 (not tagged).** The core is built and its test suite passes, and most exit criteria have scripted evidence. Live-model behaviour, the 72-hour daemon soak and multi-day use are not verified yet. Read [Safety](#safety) before you add a provider key or let it run unattended, and [Status](#status) for what has been tested.
> The repository is private for now. Install with the command in [Install](#install): it uses the latest `v*` tag, or `Main` until a tag exists. There is no tagged release yet. The steps for making it public are in [docs/PUBLIC-RELEASE.md](docs/PUBLIC-RELEASE.md).

---

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Using k3code](#using-k3code)
- [Running 24/7](#running-247)
- [Safety](#safety)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Development](#development)
- [Documentation](#documentation)
- [Status](#status)
- [Credits and licenses](#credits-and-licenses)

---

## What it does

| Area | What you get |
|---|---|
| **Coding agent** | Read, edit, patch, bash, grep, glob, web fetch/search and todo tools; plan mode; MCP servers; skills; project and user memory (`K3CODE.md`, mem0); output styles. |
| **Autonomy** | In auto mode it classifies the task (trivial → huge), plans first, and fans out parallel sub-agents in git worktrees with a reviewer gate. `/goal` and `/loop` keep going until a condition holds. Cron jobs and event-triggered automations run unattended. |
| **Reliability** | If the network or a provider drops, it retries; when you are offline it pauses and resumes by itself. A crash-safe journal never re-runs a command that may already have executed. Concurrency limits, optional budgets and a loop guard keep unattended runs bounded. |
| **Provider routing** | An ordered chain of providers and models (primary, secondary, tertiary, as many as you like). Errors are classified, failing entries cool down, and a long rate limit fails over instead of sleeping. Background work is routed to cheaper tiers automatically. |
| **Multi-session UI** | An agent list under the input box with live states (working, needs input, completed, failed), navigable with the arrow keys. Background sessions and sub-agents appear there too. ↑ walks your input history. |
| **Focus mode** | One key hides tool calls, thinking and chatter, and shows only questions, approvals, errors and final answers. |
| **Multiple windows** | `k3`: a terminal window manager (a fork of [TUIOS](https://github.com/Gaurav-Gosain/tuios)) with a zellij-style keymap: typing passes through by default, one leader key opens a mode, Esc always returns, and the hint bar is always visible. A pane running k3code reports its agent state to it. |
| **Learning** | It logs your decisions, proposes permission rules ("you always allow `npm test` here: add a rule?"), distills preferences, prepares new projects, and can propose (and A/B test) changes to its own configuration. |
| **Setup** | An installer, a guided and resumable first-run setup, `k3code doctor` health checks, and updates with automatic rollback. |

---

## Quick start

### Requirements

- **Linux** on x86_64 is the tested platform: a clean install was tested in a Fedora 44 container. aarch64 (arm64) is accepted but untested.
- **macOS** (Intel and Apple silicon) uses the same installer. It is untested; the 24/7 service and the bubblewrap sandbox are Linux-only.
- **Windows** runs k3code inside [WSL 2](https://learn.microsoft.com/windows/wsl/install). `install\install.ps1` sets up WSL with Ubuntu if needed, runs the Linux installer there and adds `k3code` and `k3` commands to Windows. Native Windows, Git Bash and Cygwin are not supported. Untested on a real Windows machine so far.
- You need `git`, `curl` or `wget`, a POSIX shell and network access during install. Everything else is fetched into your home directory without root, when it is not already installed:
  - [`uv`](https://docs.astral.sh/uv/), which provides Python ≥ 3.12;
  - a private Node 22, from nodejs.org and checked against its published checksums, to build and run the TUI;
  - the Go toolchain `panes/go.mod` asks for, from the Go module proxy, to build the `k3` binary. Its module cache is deleted after the build.
- `bubblewrap` (`bwrap`), which sandboxes unattended runs on Linux, comes from your package manager. The installer installs it only when that needs no password (root or passwordless sudo); otherwise it prints the command to run (presetup repeats it after the install). `--no-install-deps` turns all fetching off.
- Optional: `systemd --user` for the 24/7 service.

### Install

The repository is private, so authenticate once with `gh auth login` and `gh auth setup-git` (on Windows, do this inside WSL, where the install runs). A Windows clone made with Windows git also installs without that: when WSL cannot reach the repository and nothing is installed yet, the installer builds the checkout it runs from (as `--from-source` does).

**Linux and macOS**

```sh
git clone https://github.com/K3NOXOFFICIAL/k3code.git && cd k3code
sh install/install.sh          # the latest v* tag, or Main until a tag exists
```

On macOS, `xcode-select --install` provides `git`; the installer fetches the rest.

**Windows** (PowerShell, from a clone on Windows)

```powershell
git clone https://github.com/K3NOXOFFICIAL/k3code.git; cd k3code
powershell -ExecutionPolicy Bypass -File install\install.ps1
```

If WSL has no Linux distribution yet, `install.ps1` first runs `wsl --install -d Ubuntu`. Windows asks for administrator approval, and Ubuntu opens once in the same window so you can choose a Linux user name. If Windows asks for a restart, restart and run the installer again. After that, `install.ps1` runs `install/install.sh` in your default WSL distribution (`-Distro NAME` picks another), so the install lives in WSL under `~/.local/share/k3code/`. It then writes `k3code.cmd` and `k3.cmd` to `%LOCALAPPDATA%\k3code\bin` and adds that folder to your user `PATH` (`-NoModifyPath` skips that). Open a new terminal and `k3code` starts in WSL, in the folder you ran it from. Every other argument goes to `install.sh` unchanged, so `--from-source`, `--ref`, `--yes`, `--minimal` and `--check` work as below. Presetup runs inside WSL too, since that is where the Linux install happens. Building from a checkout on the Windows drive is slow; cloning inside WSL and running `sh install/install.sh` there works too, but then you get no Windows commands. Uninstall with `install\uninstall.ps1` (`-Purge` also deletes your k3code data in WSL).

**All platforms**

To install from the checkout you already have, use `sh install/install.sh --from-source` (on Windows: `install\install.ps1 --from-source`).

The installer creates a versioned install under `~/.local/share/k3code/` and links `k3code` into `~/.local/bin/` (put that on your `PATH`), and links `k3` too. It never runs onboarding; it ends by telling you to run `k3code onboard`. Re-running it upgrades in place and keeps the previous version for rollback.

Useful flags: `--ref REF` (a tag, branch or commit), `--prefix DIR`, `--no-install-deps` (fetch nothing; fail or skip with hints instead), `--check` (reports the platform and missing dependencies, changes nothing), `--from-bundle FILE` (imports a `k3code export`), `--minimal` (skips presetup, below). Uninstall with `sh install/uninstall.sh`; it removes the versions, the links and the Chromium location under `~/.local/share/k3code`, and keeps your data (`~/.k3code`, `~/.config/k3code`) unless you pass `--purge`.

**Presetup** runs after the install and is on by default; it never fails the install. It checks the sandbox (`bwrap`): when it is missing, it prints the install command for your distribution and runs nothing (it asks for a `sudo` run only on an interactive terminal, never with `--yes`). It installs Chromium for the browser tool (the headless shell, about 115 MiB download and about 266 MB on disk; `K3CODE_SKIP_CHROMIUM=1` skips only this), and prints a health subset (`k3code doctor --install`, warnings only). `--minimal` skips all of it.

### First run

```sh
k3code onboard    # first-time setup: fast (endpoint and key) or full (every step)
k3code doctor     # health checks with fix hints
k3code            # start the TUI
```

A plain interactive start with no provider configured asks once, "fast or full setup?", and does not ask again (the answer is kept in `$K3CODE_HOME/onboarding.json`). Fast asks only for an API endpoint and key (or the `claude-cli` provider). Full runs the whole setup wizard. Nothing is forced: `k3code onboard` works at any time, and a headless `-p` or piped run without a provider prints one hint and exits with code 78 instead of prompting.

The full wizard has 12 steps: `welcome`, `about`, `system`, `usage`, `providers`, `tiers`, `permissions`, `integrations`, `theme`, `service`, `tour`, `summary`. It asks about you, your system, what you mainly use k3code for, your provider chain, model tiers, permissions, optional integrations (MCP, mem0, skills), the theme, and whether to install the 24/7 service. When asked for a provider key, the input is hidden and the key is saved in `~/.config/k3code/env` (mode 0600); the config only names the variable. Run one step again with `k3code setup --step NAME`.

### Maintain

```sh
k3code doctor                                    # health checks with fix hints
k3code setup --step providers                    # re-run one step (any step name above)
k3code setup --restart                           # ignore saved progress and start over
k3code setup --non-interactive --answers FILE    # unattended setup (add --no-probe to skip live provider tests)
k3code export my.k3bundle                        # settings (secrets redacted) and sessions
k3code import my.k3bundle                        # merge them on another machine (existing config is backed up)
k3code update --check                            # show the current and latest version
k3code update                                    # smoke-tested update; rolls back automatically if it fails
k3code update --rollback                         # switch back to the previous version
```

There is no release yet, so `update --check` has nothing to find; `k3code update --from-source` pulls your clone and rebuilds.

### Three ways to run it

```sh
k3code                                   # interactive TUI
k3code -p "fix the failing test" --json  # headless: one prompt, JSON result
k3code daemon                            # a long-running host: sessions, schedules and automations keep running
```

With a daemon running, `k3code attach <session-id>` opens the TUI on one of its sessions (find ids with `/resume`; add `--readonly` to only watch). Detach any time; the session keeps working.

---

## Using k3code

### The TUI

| Key | Action |
|---|---|
| `Shift+Tab` | Cycle permission mode: default → accept-edits → plan → auto |
| `Ctrl+F` | Focus mode on/off (also `/focus`) |
| `Ctrl+C` | Interrupt the running turn (clears the draft first if there is one) |
| `↓` (empty input) | Move into the agent list under the input; `↑`/`↓` select, `Enter` opens that session, `x` stops it (asks first), `Esc` returns |
| `↑` / `↓` (in the input) | Walk through your previous inputs (kept per project) |
| `Ctrl+B` | Send the running turn to the background |
| `Alt+Y` / `Alt+N` | Accept / dismiss the top proposal card (a bare letter would steal the first character of your message) |
| `/` | Command completion; `@` completes file paths |

### Slash commands

Type `/` to browse the live list (completion shows each command's help), or run `k3code slash /help` from a shell. Everything below exists today.

| Group | Commands |
|---|---|
| **Session and context** | `/clear` · `/compact` · `/resume` · `/rename` · `/fork` · `/branch` · `/stop` · `/exit` · `/add-dir` |
| **Models and effort** | `/model` (opens the picker; `/model <key>` switches; `/model chain` shows the fallback chain and its health, and `add`, `remove` and `move` edit it) · `/effort` · `/output-style` |
| **Planning and agents** | `/goal` · `/loop` · `/bg` · `/preview` (fast sketch of the result, no changes) · `/go` (run the previewed task) · `/scope` · `/ultraplan` · `/ultracode` · `/ultraresearch` · `/advisor` |
| **Automation** | `/schedule` (cron) · `/automations` (file, git, webhook, session, network and idle triggers) |
| **Review and learning** | `/review` · `/proposals` · `/learn` · `/optimizer` · `/self-improve` |
| **Config and memory** | `/settings` · `/config` · `/update-config` (change settings in plain words) · `/permissions` · `/memory` · `/skills` · `/mcp` · `/export` · `/import` · `/artifacts` |
| **Operations** | `/doctor` · `/stats` · `/debug` · `/daemon` · `/update` · `/help` |
| **Look and feel** (TUI) | `/pet` (`on`, `off`, `random` or a pet name: blob, cat, crab, duck, ghost, hamster, owl, robot; shown at 100+ columns) · `/indicator` (`ascii` for terminals without Unicode glyphs) · `/theme` · `/statusbar` · `/focus`. These choices are saved to `display` in `~/.k3code/config.yaml`. Set `K3_NO_ANIMATION=1` to stop the spinner, messages and pet from moving. |

### Permission modes

| Mode | Behaviour |
|---|---|
| `default` | Asks before edits and before any shell command that is not on the allowlist. |
| `accept-edits` | File edits are allowed; shell commands still ask. |
| `plan` | Read-only. The agent presents a plan (`exit_plan`) and you approve it before anything changes. |
| `auto` | Everything that is not denied runs, and every auto-approved side effect is logged. This is also the mode where the plan-first gate applies (`autonomy.gate_modes`). |
| `yolo` | Skips every approval prompt. Only the hardline list and explicit `deny` rules for file and other non-shell tools still block; `deny` rules for shell commands are ignored. `Shift+Tab` never cycles into it; set it deliberately. |

Approvals are *once*, *for this session*, *always* (writes a narrow rule such as `git commit *` to the project's `.k3code/config.yaml`; for file edits, the exact path) or *deny*.
A short **hardline list** is refused in every mode, `yolo` included: `rm -rf /` and `rm -rf ~`, `mkfs`, `dd of=/dev/…`, `curl … | sh`, `env`/`printenv`, `cat` of `~/.ssh/` or `.env` files, `git push --force` to `main` or `master`, and stopping or restarting services over ssh on the hosts named in `_REMOTE_HOSTS` (`protected-host-a` and `protected-host-b`, placeholders in this release). These are pattern matches, not a sandbox, and they match specific spellings only; extend them with `permissions.hardline` in your config.
The command-line flag `--permission` takes `ask`, `auto-edit` or `yolo`.

### Plan-first and fan-out

In `auto` mode every new task is classified first (`trivial`, `small`, `medium`, `large`, `huge`; override with `/scope`). Small, low-risk tasks run directly. Anything bigger gets a read-only planning turn on the strong tier, then runs.

`large` and `huge` tasks with independent parts are split into parallel workers in separate git worktrees (this needs a git repository with at least one commit). Each result is reviewed and merged, and tested before it is kept when the project has a test command: set `autonomy.fanout.test_command`, or it is detected for pytest, npm, Go and Cargo projects.

`/ultracode <task>` is the full pipeline: three independent plans and a judge, parallel implementation, a two-reviewer adversarial panel where a finding counts only if both agree, fixes, and a final test run, all under a token and agent budget.

### Multiple windows: `k3`

```sh
k3        # the multi-window terminal (built from panes/ by the installer)
```

| Key | Action |
|---|---|
| (typing) | Everything goes to the focused pane, like a normal terminal |
| `Ctrl+G` | Leader: then `p` panes, `t` tabs, `s` sessions, `r` resize, `/` search, `a` agents, `?` help |
| `Esc` | Always back to typing |
| `Alt+←↑↓→` | Focus a neighbouring pane (works in every mode) |
| `Alt+n` / `Alt+1…9` / `Alt+z` | New pane / jump to workspace / zoom |
| `Ctrl+P` | Command palette (a searchable list of actions; its right-hand column shows the underlying TUIOS keys, not the k3 keys above) |

A mode runs one action and returns; press the mode's letter again to stay in it. The bottom bar always shows the current mode and its keys. Full sheet: [`panes/docs/k3-keymap.md`](panes/docs/k3-keymap.md).

A pane running k3code reports its state (working, needs input, done) to `k3` automatically. To answer its approvals from the Inbox while the pane is out of sight, add `[agents.approvals] enabled = ["k3code"]` to the tuios config (it is off by default). With a daemon running, `/bg --pane <prompt>` and `/fork --pane` open a new pane attached to the session. Setup and manual test steps: [`docs/k3-panes-test.md`](docs/k3-panes-test.md).

---

## Running 24/7

```sh
k3code daemon                  # long-lived host: sessions, scheduler, automations
k3code service install         # systemd user unit (--dry-run previews it); then: loginctl enable-linger $USER
k3code attach <session-id>     # open the TUI on a daemon session; detach any time, work continues
```

What keeps it alive and safe when nobody is watching:

- **Offline handling.** A connectivity monitor distinguishes *online*, *degraded*, *provider down*, *captive portal* and *offline*. Offline: the turn pauses (no error) and resumes by itself once the network is back, usually within a minute (while offline the probe backs off from every 5 s to every 60 s). A single provider being down is a failover, not a pause.
- **Retry until it works.** Network errors and server errors retry with jittered backoff, then fail over to the next chain entry. If every entry is cooling down, the call parks until the earliest reset (or until connectivity recovers, for network failures) and the status line says when.
- **Crash safety.** Tool calls are journaled (fsync'd intent and done records). When a crashed headless session is resumed with `k3code -p … --session ID --resume`, side-effect calls that had no `done` record are *not* re-run: the model gets an `INTERRUPTED` result and inspects the state first. The daemon does not resume interrupted turns automatically yet; `k3code doctor` flags unresolved journal intents.
- **Bounded by design.** A resource governor caps concurrent agents with fixed limits and refuses to start new work when free disk space drops below 2 GB (`k3code doctor` also reports I/O and CPU pressure). Optional token and estimated-spend budgets (`reliability.session_tokens`, `session_usd`, `day_tokens`, `day_usd`; all off by default) stop a turn when exceeded. A loop guard stops repeating agents. Unattended runs execute shell commands in a [bubblewrap](https://github.com/containers/bubblewrap) sandbox when it is installed.
- **Self-supervision.** The unit uses a systemd watchdog, restarts on failure, and enters a safe mode after a restart storm.
- **Missed jobs.** A cron job that was missed while the machine was off or offline fires once on recovery, within a grace window.

---

## Safety

k3code edits files and runs shell commands on your machine, as your user. Some modes do that without asking. Read this section before you choose a mode and before you add a provider key.

- **Permission modes** are described under [Permission modes](#permission-modes). In short: `default` asks before edits and before shell commands that are not allowlisted; `plan` is read-only; `auto` runs everything that is not denied and logs each auto-approved side effect; `yolo` skips approval prompts. The config key `permission_mode` accepts `ask` (the same as `default`), `auto-edit` (the same as `accept-edits`), `yolo`, `plan` and `auto`; the `--permission` flag takes `ask`, `auto-edit` or `yolo`.
- **`yolo` does not ask.** Use it for scratch projects and for scripted runs you can throw away. Anything the agent reads (a file, a web page, a tool result) can try to steer it, and in `yolo` nothing stops it from acting on that. Start in `default` on anything you care about.
- **Hardline list.** Refused in every mode, `yolo` included: `rm -rf /` and `rm -rf ~`, `mkfs`, `dd` to a device, `curl … | sh`, `env` and `printenv`, `cat` of `~/.ssh/` or `.env` files, `git push --force` to `main` or `master`, and stopping or restarting services over ssh on the hosts listed in `_REMOTE_HOSTS` (`core/src/k3code/permissions/hardline.py`). These are pattern matches, not a sandbox, and they only match the spellings they list. The host names in `_REMOTE_HOSTS` are placeholders in this release. Add your own patterns under `permissions.hardline` in your config.
- **Sandbox, and where it fails open.** In `auto` and `yolo` modes, and in every background, cron and loop session, bash runs inside [bubblewrap](https://github.com/containers/bubblewrap). The system is read-only, the project (and any directory added with `/add-dir`) is writable, `$HOME` is hidden except `~/.cache` (writable) and `~/.local/share/uv` (read-only), `/tmp` is private, and the command does not inherit your API keys. The network stays on. **If `bwrap` is missing or user namespaces are disabled, bash runs without the sandbox.** k3code logs a warning once, and `k3code doctor` reports it. Install bubblewrap before you run unattended.
- **Spend caps.** Off by default. `reliability.session_tokens`, `reliability.session_usd`, `reliability.day_tokens` and `reliability.day_usd` stop a turn, or the day's work, when a limit is reached. Dollar figures are estimates, not an invoice.
- **Approvals write rules.** Choosing *always* writes a narrow rule (for example `git commit *`) into the project's `.k3code/config.yaml`. Review those rules before you commit that file.
- **Secrets.** Keys live in `~/.config/k3code/env` (mode 0600) or in your environment. The config names an environment variable, never the value. `k3code export` redacts secrets.
- **Not a security boundary.** Treat k3code like a script you run yourself. It is not built to contain a hostile model, repository or MCP server.

To report a vulnerability, use the private route in [SECURITY.md](SECURITY.md).

## Configuration

State lives in `~/.k3code/` (override with `K3CODE_HOME`): `config.yaml`, session and usage databases, the journal, memory, learned preferences, logs. Secrets live only in `~/.config/k3code/env` (mode 0600) or your environment. A project can add `.k3code/config.yaml`, read from the directory k3code was started in (or `--config-dir`). It applies only after you trust that exact file: an interactive start shows what it changes and asks once, and asks again when the file changes. Headless and piped runs ignore an untrusted file. `k3code trust [PATH]` grants trust and `k3code trust --revoke` takes it back.

Precedence per top-level key: command-line flag > environment (`K3CODE_<KEY>`, scalar keys only, for example `K3CODE_PERMISSION_MODE`) > project config > user config > defaults. Nested sections and the `providers` list are replaced as a whole, not merged. Change settings with `/config`, `/update-config` or `k3code setup --step <name>`; edits are backed up and `/config rollback` restores the last one.

```yaml
providers:                          # the fallback chain, in order (add as many as you like)
  - name: gateway
    kind: openai                    # openai-compatible, or: anthropic
    base_url: https://your-gateway.example/v1
    api_key_env: GATEWAY_API_KEY    # the NAME of an environment variable (or a key in ~/.config/k3code/env)
    models:
      default: [model-a, model-b]   # tried in order before moving to the next provider
      strong: model-strong          # planning, review, advisor
      cheap: model-cheap            # background, loops, cron, titles, compaction
  - name: direct
    kind: anthropic
    base_url: https://api.anthropic.com
    api_key_env: ANTHROPIC_API_KEY
    models: {default: claude-sonnet-5-5}

permission_mode: ask                # ask | auto-edit | yolo
autonomy:
  plan_first: true
  fanout: {max_parallel: 3}
mcp:
  servers:
    search: {url: "https://example.org/mcp"}
```

Tip: keep at least one chain entry that does not go through a self-hosted gateway, so a gateway outage still has a fallback. (`k3code doctor` only warns when every entry looks like an OmniRoute gateway: its name contains `omniroute`, or it uses port 20128. It cannot recognise other gateways.)

---

## How it works

```
┌────────────┐   JSON-RPC (stdio / unix socket)   ┌──────────────────────────────────────────┐
│  k3code    │ ◄────────────────────────────────► │  core  (Python, package k3code)          │
│  TUI (Ink) │                                    │  gateway · agent loop · tools            │
└────────────┘                                    │  router (provider chain, tiers)          │
┌────────────┐   agent states, Inbox approvals    │  reliability (netwatch, retry, journal)  │
│  k3 panes  │ ◄────────────────────────────────► │  autonomy · sub-agents · automation      │
│  (Go)      │                                    │  learning · permissions · setup          │
└────────────┘                                    └───────────────┬──────────────────────────┘
                                                                  │ HTTPS
                                         provider 1 → provider 2 → provider 3 …   (+ MCP servers)
```

| Directory | What it is |
|---|---|
| [`core/`](core) | The Python core (`k3code`): gateway, agent loop, tools, router, reliability, autonomy, automation, learning, setup. A `uv` project. |
| [`tui/`](tui) | The terminal UI (TypeScript, Ink): a modified fork of the Hermes Agent TUI. |
| [`panes/`](panes) | `k3`, the multi-window terminal: a TUIOS fork (Go) plus the `internal/k3keys` keymap. |
| [`install/`](install) | `install.sh`, `uninstall.sh` (Linux, macOS, WSL), `install.ps1`, `uninstall.ps1` (Windows, via WSL), the systemd unit, a sample answers file. |
| [`scripts/`](scripts) | `dev/` (build-worker driver and task specs), `chaos/` (failure-injection tests), `exit/` (exit-criteria runner), vendor and upstream-sync tools. |
| [`docs/`](docs) | [`PLAN.md`](docs/PLAN.md) (roadmap), [`tui-contract.md`](docs/tui-contract.md) (core ↔ TUI protocol), `reports/` (one report per milestone and merge). |
| [`VENDOR.toml`](VENDOR.toml) | Ledger of every file or tree taken from another project: origin, commit, license, whether modified. |

The core and the TUI talk the Hermes TUI gateway protocol (newline-delimited JSON-RPC 2.0), extended with a few k3code events such as `proposal.show`, `plan.show` and `scope.verdict`. The router, permissions engine, reliability layer, autonomy and learning modules are mostly k3code's own code. The error classifier, cooldown store and retry helpers are adapted from Hermes Agent, the permission rules, wildcard matcher and command-arity table are Python ports of opencode, and several other designs are re-implemented from upstream projects (see [`VENDOR.toml`](VENDOR.toml) and [Credits](#credits-and-licenses)).

---

## Development

Run each block from the repository root.

```sh
# core
(cd core && uv sync && uv run pytest -q -o addopts="" && uv run ruff check src tests)

# TUI
(cd tui && npm ci && npm run build:ink && npm run build && npx vitest run)

# panes: only these packages. Do NOT run the whole upstream test tree: its remote-sync tests recurse without bound.
(cd panes && go build ./cmd/k3 && go test ./internal/k3keys/... ./internal/harness/... ./internal/input/ ./internal/app/)

# license bookkeeping
python3 scripts/vendor_check.py
```

- **Exit checks:** `scripts/exit/run_all.sh [--soak-minutes N] [--only m0,m1,…]` runs every check (real-TUI scripted flows, daemon and chaos tests, the panes tests, a clean-install test in a Fedora 44 podman container, and a 30-minute daemon soak in the background) and **overwrites the tracked** `docs/reports/exit-status.md`. Expect 30–40 minutes (an untested estimate) and heavy CPU, RAM and disk use. It needs `bash`, `python3`, `uv`, `node` (with the TUI built first), `go`, and optionally `podman`. Live-model rows stay pending while the provider quota is exhausted.
- **How it was built:** most of the code was written by headless coding agents driven by `scripts/dev/omni-worker.sh` from the task specs in `scripts/dev/tasks/`. Each task produced a branch and a report in `docs/reports/`. Those scripts are internal build tooling; you do not need them to build, test or use k3code.
- **Branches:** work branches are named by milestone, for example `w/m1-gateway`, `w/m2-ops`, `w/m4a-autonomy` or `w/m6-install`. The `w/merge-*` branches are integration merges. The exit checks are on `w/exit-verify` locally and `exit/verify` on `origin`. Integration happens on `m0-scaffold`.
- **License hygiene:** `scripts/vendor_check.py` checks each file entry in `VENDOR.toml` (the file exists, the license is MIT or Apache-2.0, the project is not on a short banned list that includes the leaked Claude Code source and the closed Ante binary). It does not check the `[[tree]]` entries (`tui/`, `panes/`) or inspect the code itself.

---

## Documentation

| Document | What it covers |
|---|---|
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Development setup, tests, commit style, and the rules for the vendored TUI |
| [`SECURITY.md`](SECURITY.md) | How to report a vulnerability privately, and what is in scope |
| [`CHANGELOG.md`](CHANGELOG.md) | What each version contains |
| [`docs/PLAN.md`](docs/PLAN.md) | The design plan and roadmap (M0 to M6) |
| [`docs/tui-contract.md`](docs/tui-contract.md) | The protocol between the core and the TUI |
| [`docs/UPSTREAM.md`](docs/UPSTREAM.md) | How the vendored TUI relates to its upstream |
| [`docs/k3-panes-test.md`](docs/k3-panes-test.md) | Manual test steps for the `k3` multi-window terminal |
| `docs/reports/` | One report per milestone and merge, including the exit-status table |
| [`VENDOR.toml`](VENDOR.toml) | Every file and tree taken from another project, with its license |

---

## Status

**Alpha, version 0.1.0 (not tagged).** This is the first version meant for people other than its author. Expect rough edges, and expect the config format and some commands to change.

As of 2026-10-08, 51 exit-criteria checks have been run: 46 pass, none fail, and 5 are pending. The pending rows need a live model, a person or days of real use. The table with the evidence behind every row is `docs/reports/exit-status.md`. The core test suite passes.

What is not verified yet:

- **Live models.** Many scripted checks use a scripted fake provider. The live-model rows ran through the `claude-cli` provider (a local Claude Code login), not through a gateway.
- **Long unattended runs.** A 30-minute daemon soak passed. The 72-hour soak is pending.
- **Platforms.** Only Linux on x86_64 has been tested. A clean install was tested in a Fedora 44 container. CI runs the installer tests on macOS; Windows (`install.ps1` via WSL) is tested only against a stand-in for `wsl.exe` on Linux.
- **Updates.** There is no release yet, so the update and rollback path has only been tested against local version directories.

| | Milestone | Built | Verified so far |
|---|---|---|---|
| **M0** | Scaffold, core skeleton, vendored TUI and panes | ✅ | All five exit rows pass, including the live-model row (through the `claude-cli` provider) |
| **M1** | Daily-driver agent: gateway, TUI (agent list, focus mode, history), permissions, commands | ✅ | All 10 exit rows pass, including the live `/review` check; three days of real use is not verified |
| **M2** | 24/7 reliability: offline pause/resume, retry, journal, governor, daemon, doctor, stats | ✅ | All five chaos checks and a 30-minute soak (119 turns, none lost, bounded memory growth) pass; the 72-hour soak is pending |
| **M3** | `k3` keymap, agent states and approvals in panes | ✅ | Keymap tests and the live pane-badge check pass; the first-time-user test is pending |
| **M4** | Plan-first and scope gate, tiers, fan-out, `/ultra*`, `/preview`, `/advisor`, loops, cron, automations | ✅ | All exit rows pass except the 30-task scope eval: its 30 labels are proposed and not yet confirmed |
| **M5** | Learning, proposals, project preparation, self-optimizer | ✅ | Demo and tests pass; live mem0 and 2 weeks of use are pending |
| **M6** | Installer, guided setup, update with rollback, release CI | ✅ | A clean Fedora 44 container installs in about a minute; setup resume, `--from-bundle` and update rollback pass. The upstream-sync check passes under the agreed policy: TUIOS stays mergeable (0 conflicting files) and the heavily modified Hermes TUI is a documented frozen fork whose upstream fixes are cherry-picked by hand ([`docs/UPSTREAM.md`](docs/UPSTREAM.md)) |


---

## Credits and licenses

k3code is released under the [MIT License](LICENSE). It stands on open-source work. Every vendored or ported file or tree is listed in [`VENDOR.toml`](VENDOR.toml); [`NOTICE`](NOTICE) and [`LICENSES/`](LICENSES) carry the notices and license texts.

| Project | License | What is used |
|---|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research) | MIT | The terminal UI (`tui/`, derived); copied or adapted retry, error-classification, cooldown and fuzzy-edit code; and the designs behind goals, loops, cron, suggestions, sub-agent worktrees, fan-out, permission-rule mining, background review and the skills curator |
| [TUIOS](https://github.com/Gaurav-Gosain/tuios) | MIT | The multi-window terminal in `panes/` |
| [opencode](https://github.com/anomalyco/opencode) | MIT | The permission rules, wildcard matching and command-arity design (Python ports) |
| [OpenAI Codex](https://github.com/openai/codex) | Apache-2.0 | The code review rubric (adapted) |
| [Atomic Agents](https://github.com/Eigenwise/atomic-agents) | MIT | The deep-research flow behind `/ultraresearch` (adapted) |

Studied for design but not copied: openclaw, pi, and Ante's public protocol documentation. No proprietary or leaked code is used.
