# k3code

**A terminal coding agent that keeps working when you are not looking.**
It plans before it acts, runs many agents in parallel, survives dropped connections and crashes, routes cheap work to cheap models, and learns how you like to work.

> **Status: pre-release (v0.0.1, working name).** Every feature of the first roadmap (M0–M6) is built and integrated on the `m0-scaffold` branch; its draft pull request into `Main` is still open, so `Main` itself only holds the initial commit. The core test suite passes (613 tests). The exit-criteria verification is partly done: of 51 checks, **41 pass and 10 are pending** (none fail) (they need a live model, a person, or days of real use). See [Project status](#project-status) and [`docs/reports/exit-status.md`](docs/reports/exit-status.md) for exactly what is verified.
> The repository is private. Release downloads need a GitHub token; there is no release yet, so you install from a clone.

---

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Using k3code](#using-k3code)
- [Running 24/7](#running-247)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Development](#development)
- [Project status](#project-status)
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

- Linux. Only x86_64 has been tested; macOS is untested.
- `git`, `curl` or `wget`, `tar` (with `xz` and `gzip` support), and a POSIX shell. Network access is needed during install.
- The installer fetches the rest into your home directory, with no root: [`uv`](https://docs.astral.sh/uv/) (it manages Python ≥ 3.12; it asks before installing `uv` unless you pass `--yes`), Node ≥ 22 for the TUI, and Go for the `k3` multi-window binary (an existing `go` on your `PATH` is used as is).
- Optional: `bubblewrap` (`bwrap`) to sandbox unattended runs, `systemd --user` for the 24/7 service.

### Install

The repository is private, so authenticate first (`gh auth login`, or use a GitHub token). Until pull request #1 is merged, the code is on the `m0-scaffold` branch:

```sh
gh repo clone K3NOXOFFICIAL/k3code -- -b m0-scaffold && cd k3code
sh install/install.sh --from-source
```

This creates a versioned install under `~/.local/share/k3code/` and links `k3code` and `k3` into `~/.local/bin/` (put that on your `PATH`).

A `--from-source` install is a regular, self-contained install: the Python core is copied into the version's own venv, so deleting a worktree or switching branches in the clone cannot break `k3code`. The clone's path is recorded in `~/.local/share/k3code/source_path`, and `k3code update --from-source` pulls it and builds a new version next to the old one (with automatic rollback). For development, `K3_EDITABLE=1 sh install/install.sh --from-source` keeps the old editable install (`pip install -e`), where your edits show up without re-installing.

The installer's own progress messages and its final exit status are written to `~/.local/share/k3code/install.log`. Output from `uv`, `npm` and `go` appears on the terminal only; to capture everything: `sh install/install.sh --from-source 2>&1 | tee install-full.log`.

Flags: `--yes` (accept the installer's own prompt, i.e. installing `uv`), `--no-setup` or `--headless` (skip the setup wizard), `--from-bundle FILE` (restore settings and sessions from `k3code export`; it then asks only for secrets), `--channel`, `--version`. Uninstall with `install/uninstall.sh` (it keeps your data unless you pass `--purge`).

### First run

```sh
k3code setup      # guided and resumable
k3code doctor     # health checks with fix hints
k3code            # start the TUI
```

The setup wizard has 12 steps: `welcome`, `about`, `system`, `usage`, `providers`, `tiers`, `permissions`, `integrations`, `theme`, `service`, `tour`, `summary`. It asks about you, your system, what you mainly use k3code for, your provider chain, model tiers, permissions, optional integrations (MCP, mem0, skills), the theme, and whether to install the 24/7 service. When asked for a provider key, paste it: it is stored hidden (mode 0600) in `~/.config/k3code/env`, and the config only names the variable.
Running `k3code` with no config starts the setup automatically.

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

### Permission modes

| Mode | Behaviour |
|---|---|
| `default` | Asks before edits and before any shell command that is not on the allowlist. |
| `accept-edits` | File edits are allowed; shell commands still ask. |
| `plan` | Read-only. The agent presents a plan (`exit_plan`) and you approve it before anything changes. |
| `auto` | Everything that is not denied runs, and every auto-approved side effect is logged. This is also the mode where the plan-first gate applies (`autonomy.gate_modes`). |
| `yolo` | Skips every approval prompt. Only the hardline list and explicit `deny` rules for file and other non-shell tools still block; `deny` rules for shell commands are ignored. `Shift+Tab` never cycles into it; set it deliberately. |

Approvals are *once*, *for this session*, *always* (writes a narrow rule such as `git commit *` to the project's `.k3code/config.yaml`; for file edits, the exact path) or *deny*.
A short **hardline list** is refused in every mode, `yolo` included: `rm -rf /` and `rm -rf ~`, `mkfs`, `dd of=/dev/…`, `curl … | sh`, `env`/`printenv`, `cat` of `~/.ssh/` or `.env` files, `git push --force` to main, and stopping or restarting services on two configured home servers over ssh. These are pattern matches, not a sandbox, and they match specific spellings only; extend them with `permissions.hardline` in your config.
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

## Configuration

State lives in `~/.k3code/` (override with `K3CODE_HOME`): `config.yaml`, session and usage databases, the journal, memory, learned preferences, logs. Secrets live only in `~/.config/k3code/env` (mode 0600) or your environment. A project can add `.k3code/config.yaml`, read from the directory k3code was started in (or `--config-dir`).

Precedence per top-level key: command-line flag > environment (`K3CODE_<KEY>`, scalar keys only, for example `K3CODE_PERMISSION_MODE`) > project config > user config > defaults. Nested sections are replaced as a whole, not merged. Change settings with `/config`, `/update-config` or `k3code setup --step <name>`; edits are backed up and `/config rollback` restores the last one.

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

Tip: keep at least one chain entry that does not go through a self-hosted gateway, so a gateway outage still has a fallback. (`k3code doctor` only warns when every entry points at an OmniRoute or k3nox host; it cannot recognise other gateways.)

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
| [`install/`](install) | `install.sh`, `uninstall.sh`, the systemd unit, a sample answers file. |
| [`scripts/`](scripts) | `dev/` (build-worker driver and task specs), `chaos/` (failure-injection tests), `exit/` (exit-criteria runner), vendor and upstream-sync tools. |
| [`docs/`](docs) | [`PLAN.md`](docs/PLAN.md) (roadmap), [`tui-contract.md`](docs/tui-contract.md) (core ↔ TUI protocol), [`reports/`](docs/reports) (one report per milestone and merge). |
| [`VENDOR.toml`](VENDOR.toml) | Ledger of every file or tree taken from another project: origin, commit, license, whether modified. |

The core and the TUI talk the Hermes TUI gateway protocol (newline-delimited JSON-RPC 2.0), extended with a few k3code events such as `proposal.show`, `plan.show` and `scope.verdict`. The router, permissions engine, reliability layer, autonomy and learning modules are mostly k3code's own code. The error classifier, cooldown store and retry helpers are adapted from Hermes Agent, the permission rules, wildcard matcher and command-arity table are Python ports of opencode, and several other designs are re-implemented from upstream projects (see [`VENDOR.toml`](VENDOR.toml) and [Credits](#credits-and-licenses)).

---

## Development

Run each block from the repository root.

```sh
# core
(cd core && uv sync && uv run pytest -q -o addopts="" && uv run ruff check src tests)

# TUI (one upstream test is known to fail)
(cd tui && npm ci && npm run build:ink && npm run build && npx vitest run)

# panes: only these packages. Do NOT run the whole upstream test tree: its remote-sync tests recurse without bound.
(cd panes && go build ./cmd/k3 && go test ./internal/k3keys/... ./internal/harness/... ./internal/input/ ./internal/app/)

# license bookkeeping
python3 scripts/vendor_check.py
```

- **Exit checks:** `scripts/exit/run_all.sh [--soak-minutes N] [--only m0,m1,…]` runs every check (real-TUI scripted flows, daemon and chaos tests, the panes tests, a clean-install test in a Fedora 44 podman container, and a 30-minute daemon soak in the background) and **overwrites the tracked** [`docs/reports/exit-status.md`](docs/reports/exit-status.md). Expect 30–40 minutes and heavy CPU, RAM and disk use. It needs `bash`, `python3`, `uv`, `node` (with the TUI built first), `go`, and optionally `podman`. Live-model rows stay pending while the provider quota is exhausted.
- **How it was built:** most of the code was written by headless coding agents driven by [`scripts/dev/omni-worker.sh`](scripts/dev/omni-worker.sh) from the task specs in [`scripts/dev/tasks/`](scripts/dev/tasks). Each task produced a branch and a report in `docs/reports/`.
- **Branches:** named by milestone. `m0/…` scaffold pieces; `m1/…` gateway, tui, permissions, commands; `m2/…` reliability and ops; `m3/…` keymap and panes integration; `m4/…` autonomy, automation, fan-out; `m5/…` learning; `m6/…` install; `exit/verify`. The `*/merge-*` branches are integration merges. Everything is integrated on `m0-scaffold`, the open draft pull request into `Main`.
- **License hygiene:** `scripts/vendor_check.py` checks each file entry in `VENDOR.toml` (the file exists, the license is MIT or Apache-2.0, the project is not on a short banned list that includes the leaked Claude Code source and the closed Ante binary). It does not check the `[[tree]]` entries (`tui/`, `panes/`) or inspect the code itself.

---

## Project status

Verification as of 2026-10-07 (51 exit checks: 41 pass, 0 fail, 10 pending). The authoritative table, with the evidence behind every row, is [`docs/reports/exit-status.md`](docs/reports/exit-status.md).

| | Milestone | Built | Verified so far |
|---|---|---|---|
| **M0** | Scaffold, core skeleton, vendored TUI and panes | ✅ | Passes; the live-model row is pending (live provider calls are paused) |
| **M1** | Daily-driver agent: gateway, TUI (agent list, focus mode, history), permissions, commands | ✅ | Nine scripted real-TUI flows pass; the live `/review` check is pending, and so is 3 days of real use |
| **M2** | 24/7 reliability: offline pause/resume, retry, journal, governor, daemon, doctor, stats | ✅ | All five chaos checks and a 30-minute soak (119 turns, none lost, memory flat) pass; the 72-hour soak is pending |
| **M3** | `k3` keymap, agent states and approvals in panes | ✅ | Keymap tests and the live pane-badge check pass; the first-time-user test is pending |
| **M4** | Plan-first and scope gate, tiers, fan-out, `/ultra*`, `/preview`, `/advisor`, loops, cron, automations | ✅ | Scripted checks pass; four checks need a live model or your task-size labels |
| **M5** | Learning, proposals, project preparation, self-optimizer | ✅ | Demo and tests pass; live mem0 and 2 weeks of use are pending |
| **M6** | Installer, guided setup, update with rollback, release CI | ✅ | A clean Fedora 44 container installs in about a minute; setup resume, `--from-bundle` and update rollback pass. The upstream-sync check passes under the agreed policy: TUIOS stays mergeable (0 conflicting files) and the heavily modified Hermes TUI is a documented frozen fork whose upstream fixes are cherry-picked by hand ([`docs/UPSTREAM.md`](docs/UPSTREAM.md)) |

Other open items: there is no release yet, so the update path is only tested against local version directories; the goals and requirements the project is measured against are in [`GOAL.md`](GOAL.md).

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
