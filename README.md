# k3code

**A terminal coding agent that keeps working when you are not looking.**

k3code plans before it acts, can run several agents in parallel, retries through dropped connections and provider outages, and sends cheap background work to cheaper models. It has a Python core, a terminal UI (TypeScript, Ink) and an optional multi-window terminal (`k3`, a fork of TUIOS). It works with OpenAI-compatible and Anthropic providers, and with a local Claude Code login.

> **Status: alpha, version 0.1.0 (not tagged).** The core is built and its test suite passes, and most exit criteria have scripted evidence. Live-model behaviour, the 72-hour daemon soak and multi-day use are not verified yet. Read [Safety](#safety) before you add a provider key or let it run unattended, and [Status](#status) for what has been tested.
> Install with the command in [Install](#install): it uses the latest `v*` tag, or `Main` until a tag exists. There is no tagged release yet.

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
| **Autonomy** | In auto mode it classifies the task (trivial → huge), plans first, and fans out parallel sub-agents in git worktrees with a reviewer gate. `/goal` and `/loop` keep going until a condition holds. `/ultracode` runs a plan, fan-out, review and test pipeline once, or stays on as a session mode; saying `ultracode` in a prompt runs it once. Cron jobs and event-triggered automations run unattended. |
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

If you install from a private fork or mirror, authenticate once first with `gh auth login` and `gh auth setup-git` (on Windows, do this inside WSL, where the install runs). A Windows clone made with Windows git also installs without that: when WSL cannot reach the repository and nothing is installed yet, the installer builds the checkout it runs from (as `--from-source` does).

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

Useful flags: `--ref REF` (a tag, branch or commit), `--prefix DIR`, `--no-install-deps` (fetch nothing; fail or skip with hints instead), `--check` (reports the platform and missing dependencies, changes nothing), `--from-bundle FILE` (imports a `k3code export`), `--minimal` (skips presetup, below), `--force` (replace a `k3code` or `k3` in `~/.local/bin` that is not this installer's link), `--allow-root` (the installer refuses root and sudo otherwise, for example for a container). Uninstall with `sh install/uninstall.sh`; it removes the versions, the links and the Chromium location under `~/.local/share/k3code`, and keeps your data (`~/.k3code`, `~/.config/k3code`) unless you pass `--purge`, which asks you to type `yes` (`--purge --yes` without asking).

**Presetup** runs after the install and is on by default; it never fails the install. It checks the sandbox (`bwrap`): when it is missing, it prints the install command for your distribution and runs nothing (it asks for a `sudo` run only on an interactive terminal, never with `--yes`). It installs Chromium for the browser tool (the headless shell, about 115 MiB download and about 266 MB on disk; `K3CODE_SKIP_CHROMIUM=1` skips only this), and prints a health subset (`k3code doctor --install`, warnings only). `--minimal` skips all of it.

### First run

```sh
k3code onboard    # first-time setup: fast (endpoint and key) or full (every step)
k3code doctor     # health checks with fix hints
k3code            # start the TUI
```

A plain interactive start with no provider configured asks once, "fast or full setup?", and does not ask again (the answer is kept in `$K3CODE_HOME/onboarding.json`). Fast asks only for an API endpoint and key (or the `claude-cli` provider); run over an existing config, it puts that provider first and keeps your other providers behind it as fallbacks. Full runs the whole setup wizard. Nothing is forced: `k3code onboard` works at any time, and a headless `-p` or piped run without a provider prints one hint and exits with code 78 instead of prompting.

The full wizard has 12 steps: `welcome`, `about`, `system`, `usage`, `providers`, `tiers`, `permissions`, `integrations`, `theme`, `service`, `tour`, `summary`. It asks about you, your system, what you mainly use k3code for, your provider chain, model tiers, permissions, optional integrations (MCP, mem0, skills), the theme, and whether to install the 24/7 service. When asked for a provider key, the input is hidden and the key is saved in `~/.config/k3code/env` (mode 0600); the config only names the variable. Run one step again with `k3code setup --step NAME`.

### Maintain

```sh
k3code doctor                                    # health checks with fix hints
k3code setup --step providers                    # re-run one step (any step name above)
k3code setup --restart                           # ignore saved progress and start over
k3code setup --non-interactive --answers FILE    # unattended setup (add --no-probe to skip live provider tests)
k3code export my.k3bundle                        # settings (secrets and configured key values redacted) and sessions
k3code import my.k3bundle                        # merge them on another machine (existing config is backed up)
k3code update --check                            # show the current and latest version
k3code update                                    # smoke-tested update; rolls back automatically if it fails
k3code update --rollback                         # switch back to the previous version
```

There is no release yet. An install built from a checkout (`install.sh --from-source`) therefore updates from that checkout: `k3code update` (also `/update` in the TUI) pulls it with `git pull --ff-only` and rebuilds whenever there is no release to fetch, including a private repository without a GitHub token. On Windows the clone is pulled with Windows git, which has your GitHub credentials. If git still cannot sign in, pull the clone yourself and run `k3code update --from-source --no-pull`. `/update now` in the TUI runs the update in the background: through `systemd-run` when the daemon is a systemd unit, otherwise (macOS, WSL without systemd) as a detached process that logs to `~/.local/share/k3code/update.log`. An install made with `install.sh --from-git` (the default) keeps no checkout: `k3code update` looks up the newest commit of the branch or tag it was installed from (`git ls-remote`) and, when there is one, rebuilds from it with that commit's own installer. An install pinned to a commit SHA stays where it is. `update.url` in `config.yaml` points it at a fork or mirror.

### Three ways to run it

```sh
k3code                                   # interactive TUI
k3code -p "fix the failing test" --json  # headless: one prompt, JSON result
k3code daemon                            # a long-running host: sessions, schedules and automations keep running
```

A `-p` prompt whose first word is a slash command runs that command instead of asking the model: `k3code -p "/project"` prints the stored project scan (in a fresh project it says "Not scanned yet"; `k3code -p "/project rescan"` scans and stores; `k3code doctor` shows a read-only scan), and `/skills`, `/stats` and `/help` print their usual output. A command that needs a live session (`/clear`, `/compact` …) exits with an error that says so.

With a daemon running, `k3code attach <session-id>` opens the TUI on one of its sessions (find ids with `/resume`; add `--readonly` to only watch). Detach any time; the session keeps working.

---

## Using k3code

### The TUI

| Key | Action |
|---|---|
| `Shift+Tab` | Cycle permission mode: default → accept-edits → plan → auto |
| `Ctrl+F` | Focus mode on/off (also `/focus`) |
| `Ctrl+C` | Interrupt the running turn (clears the draft first if there is one) |
| `Esc` | Close the open overlay; `Esc` `Esc` interrupts the running turn and keeps the draft (when idle it clears the draft) |
| `←` (empty input) | Open the agent view: every session and sub-agent grouped into Needs input, Working and Completed (earlier sessions of this project at the end of Completed); `↑`/`↓` select, `→` or `Enter` attaches, `x` stops (asks first), `n` starts a new session, `←`/`Esc` go back (also `/agents`). The agent list under the input only shows them |
| `↑` / `↓` (in the input) | Walk through your previous inputs (kept per project) |
| `Ctrl+B` | Send the running turn to the background |
| `Alt+Y` / `Alt+N` | Accept / dismiss the top proposal card (a bare letter would steal the first character of your message) |
| `/` | Command completion; `@` completes file paths |

### Slash commands

Type `/` to browse the live list (completion shows each command's help), or run `k3code slash /help` from a shell. Everything below exists today.

| Group | Commands |
|---|---|
| **Session and context** | `/clear` · `/compact` · `/resume` · `/rename` · `/fork` · `/branch` · `/stop` · `/exit` · `/add-dir` |
| **Models and effort** | `/tune` (one popup for the model, the reasoning effort and the ultracode mode; or typed, for example `/tune strong high ultracode`) · `/model` (bare opens the same popup; `/model <key>` switches; `/model chain` shows the fallback chain and its health, and `add`, `remove` and `move` edit it) · `/effort` (bare opens the same popup; `/effort <level>` sets it) · `/output-style` |
| **Planning and agents** | `/goal` · `/loop` · `/bg` · `/agents` (agent view; `/agents tree` shows the spawn tree) · `/preview` (fast sketch of the result, no changes) · `/go` (run the previewed task) · `/scope` · `/ultraplan` · `/ultracode` (`/ultracode <task>` runs it once; bare, `on`, `off` and `status` control the ultracode mode) · `/ultraresearch` · `/advisor` |
| **Automation** | `/schedule` (cron) · `/automations` (file, git, webhook, session, network and idle triggers) |
| **Review and learning** | `/review` · `/proposals` · `/project` (detected stacks and recipe proposals; `/project rescan`) · `/learn` · `/optimizer` · `/self-improve` |
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
The command-line flag `--permission` takes `ask`, `auto-edit`, `auto` or `yolo`. A headless run (`k3code -p`) cannot ask, so in `ask` and `auto-edit` every shell command that would ask is denied; `--permission auto` (or `headless_permission: auto` in the config) runs it as an interactive `auto` session would: bash in the sandbox, the hardline list and `deny` rules still refusing.

### Plan-first and fan-out

In `auto` mode every new task is classified first (`trivial`, `small`, `medium`, `large`, `huge`; override with `/scope`). Small, low-risk tasks run directly. Anything bigger gets a read-only planning turn on the strong tier, then runs.

`large` and `huge` tasks with independent parts are split into parallel workers in separate git worktrees (this needs a git repository with at least one commit). Each result is reviewed and merged, and tested before it is kept when the project has a test command: set `autonomy.fanout.test_command`, or it is detected for pytest, npm, Go and Cargo projects.

`/ultracode <task>` is the full pipeline: three independent plans and a judge, parallel implementation, a two-reviewer adversarial panel where a finding counts only if both agree, fixes, and a final test run, all under a token and agent budget (`ultracode.max_tokens`, `ultracode.max_agents`). It can also stay on for a whole session ([Ultracode mode](#ultracode-mode)) or run when you say its name in a prompt ([Wake words](#wake-words)).

### Tune: model, effort and ultracode

`/tune` opens one popup with the model list, the reasoning effort slider and the ultracode switch. A bare `/model` and a bare `/effort` open the same popup. This is a sample at 100 columns (the first row is your `default_model`, and the descriptions come from `descriptions` in the provider config):

```
Tune
Switch between models. Enter makes your pick the default for new sessions.

❯ 1. Default (recommended) ✓  model-a · Everyday coding
  2. strong                   model-strong · Planning and review
  3. cheap                    model-cheap · Fast and cheap

Effort      Faster                                   Smarter   Ultracode  on
            ──────────────────────────────▲─────────────────   Tab to toggle
            default    low    medium    high    xhigh    max
Ultracode: runs the multi-agent pipeline on every task
↑/↓ model · ←/→ effort · Tab ultracode · Enter default · s session · Esc cancel
Say ultracode / ultraplan / ultraresearch in a prompt to run that mode once.
```

| Key | Action |
|---|---|
| `↑` / `↓` | Move through the models (`1` to `9` jump to that row) |
| `←` / `→` | Move the effort mark: `default` (send no effort level), `low`, `medium`, `high`, `xhigh`, `max` |
| `Tab` | Ultracode on / off |
| `Enter` | Apply what you changed, and make the highlighted model the default for new sessions (nothing is written when it already is the default) |
| `s` | Apply to this session only |
| `Esc` | Close; nothing changes |

The current model has a green ✓. The popup sends everything in one request and only the fields you moved, so `Enter` on an untouched popup just closes it when the highlighted model is already the default; otherwise it makes that model the default (use `s` to leave the default alone). Model and effort apply from the next turn, the ultracode mode from the next prompt. When the highlighted model takes no effort level the popup says so. Without a session, effort and ultracode are greyed out and their keys do nothing; the model can still be set. The popup does not open, and ignores keys, while an approval, question, password or confirm prompt is waiting. It fits 80x24: the model list shrinks first, and below 70 columns the descriptions go.

The same thing, typed (the TUI opens the popup for a bare `/tune`; other clients get the current state):

```
/tune                                    show the model, effort and ultracode mode
/tune strong                             this session uses the model key "strong"
/tune model strong effort high           the same words, spelled out
/tune high ultracode                     effort high and the ultracode mode on
/tune cheap low ultracode off --global   cheap, low, ultracode off; "cheap" is also the default for new sessions
```

- Words come in any order. A bare word is a model key if it is one (a model key wins over an effort word), otherwise an effort level: `low`, `medium`, `high`, `xhigh`, `max` or `default` (send no effort level). Spell it `model <key>` or `effort <level>` if a name is ambiguous.
- `ultracode` alone means on. `ultracode on`, `off`, `true`, `false`, `yes`, `no`, `1` and `0` also work.
- `--global` (it needs a model) also makes the model the default for new sessions. `--session` is the default.
- Everything is checked first and applied all or nothing. An unknown word gives an error with the usage line and changes nothing. Effort and ultracode need a session.
- The old picker's spellings still work: `--reasoning <level>` is `effort <level>`; `minimal`, `ultra` and `none` after `effort` or `--reasoning` mean `low`, `max` and `default`; `--provider <name>` and `--tui-session` are accepted and ignored.
- `/model <key>` and `/effort <level>` do what they always did, and take the same flags: `/model strong --reasoning high --global`. Any other words after `/model <key>` are the reason kept with the switch (`/model strong --global the cheap one loops`); only the flags are settings, so an effort word in the reason stays text, and after a bare `--` everything is reason.

**The default model.** Enter in the popup and `--global` write `default_model: <key>` into your user `config.yaml` (never the project's). k3code checks that the key exists and that the file stays valid, keeps a timestamped backup next to it, then writes. The rewrite drops YAML comments; the backup keeps them. If the write fails you get the error and nothing else from that request is applied. `s` and anything without `--global` change this session only.

### Ultracode mode

Ultracode can be a mode of the session: `off` (the default) or `on`. Turn it on with `Tab` in the popup, `/tune ultracode`, or `/ultracode`:

| Command | What it does |
|---|---|
| `/ultracode` | Flip the mode |
| `/ultracode on` / `off` | Set it |
| `/ultracode status` | Say whether it is on |
| `/ultracode <task>` | Run the pipeline once for that task. It does not change the mode |

While the mode is on, the status line shows an `ultracode` chip, and a prompt you type runs the ultracode pipeline (the plan, fan-out, review, fix and test steps above, with the same budget) instead of a normal turn. The plan-first scope classifier decides: a prompt rated at least `ultracode.min_scope` runs the pipeline. The default is `small`, so everything except `trivial` (an answer-only question or one located edit) does. Set `min_scope: medium` if small tasks should stay normal turns. A `trivial` prompt, or one the classifier fails on, runs as a normal turn.

The mode leaves a prompt alone when:

- it starts with `/` (a slash command runs as typed);
- the TUI generated it rather than you typing it (a skill expansion, the `/go` send, an accepted proposal);
- an approved plan is waiting for it (`/go` after `/ultraplan`);
- the session is a background session.

A prompt that waited in the queue behind a running turn is routed when it runs, with the mode as it is then. The mode belongs to the session: it survives a resume, and `/fork`, `/branch` and background sessions start with it, as they do with the effort. Each run starts several agents and spends tokens (a run stops at `ultracode.max_tokens` or `ultracode.max_agents`), so leave the mode off when you do not want that for every prompt.

### Wake words

`ultracode`, `ultraplan` and `ultraresearch`, typed in a prompt, run that mode once when you send it. The word is taken out of the task:

```
fix the flaky login test, ultracode              runs /ultracode fix the flaky login test
ultraplan add rate limiting to the API           runs /ultraplan add rate limiting to the API
ultraresearch how do other agents retry on 429?  runs /ultraresearch how do other agents retry on 429?
```

Your transcript keeps your own words; only the task the mode gets has the word removed. A wake word wins over the ultracode mode. A prompt that is only the word gets the command's usage line as the answer, and nothing runs.

A word does not count when:

- the prompt starts with `/` (slash commands run as typed);
- it is part of something longer: a path (`src/ultracode.py`), a flag (`--ultracode`), an `@mention`, a `#tag` or a longer word;
- it is quoted or in code: in backticks, in quotes or in a code block;
- the prompt names two different wake words ("what is the difference between ultracode and ultraplan?"): that is ambiguous, so nothing runs.

`ultracode on`, `ultracode off` and `ultracode status`, and the same asked in a few words ("turn off ultracode", "ultracode mode on"), change or show the [mode](#ultracode-mode) like `/ultracode on|off|status` instead of starting a run. "ultracode on the auth module" is a task. Your `UserPromptSubmit` hooks see a prompt before a wake word or the mode does: one a hook blocks starts nothing, and what a hook adds is passed to the run.

Only text you typed is checked (a word inside a paste does not count: the TUI tells the gateway where each paste is, and the composer does not paint it): a prompt you send, a prompt that waited in the queue (checked when it runs), a steering message that ends up running as a prompt, and `/bg <prompt>`. Goal prompts, loop, cron and automation ticks, sub-agents, tool output and text the TUI generated (skills, `/go`, accepted proposals) never trigger one. Wake words work for prompts that go through the gateway (the TUI, including `k3code attach`). They do not apply to `k3code -p` or the line REPL, which run their own loop without the gateway.

While you type, the composer paints a word that counts in the accent colour, so you see it before you press Enter. The composer does not read your `wake_words` settings: a word you switched off is painted too, but does nothing.

To say one of the words without running anything, quote it or put it in backticks. To switch them off, set `wake_words.enabled: false` in your config, or switch off a single word, for example `wake_words.ultraplan: false` (see [Configuration](#configuration)).

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
k3code agents                  # open the TUI on the daemon with the agent view showing
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

- **Permission modes** are described under [Permission modes](#permission-modes). In short: `default` asks before edits and before shell commands that are not allowlisted; `plan` is read-only; `auto` runs everything that is not denied and logs each auto-approved side effect; `yolo` skips approval prompts. The config key `permission_mode` accepts `ask` (the same as `default`), `auto-edit` (the same as `accept-edits`), `yolo`, `plan` and `auto`; the `--permission` flag takes `ask`, `auto-edit`, `auto` or `yolo`.
- **`yolo` does not ask.** Use it for scratch projects and for scripted runs you can throw away. Anything the agent reads (a file, a web page, a tool result) can try to steer it, and in `yolo` nothing stops it from acting on that. Start in `default` on anything you care about.
- **Hardline list.** Refused in every mode, `yolo` included: `rm -rf /` and `rm -rf ~`, `mkfs`, `dd` to a device, `curl … | sh`, `env` and `printenv`, `cat` of `~/.ssh/` or `.env` files, `git push --force` to `main` or `master`, and stopping or restarting services over ssh on the hosts listed in `_REMOTE_HOSTS` (`core/src/k3code/permissions/hardline.py`). These are pattern matches, not a sandbox, and they only match the spellings they list. The host names in `_REMOTE_HOSTS` are placeholders in this release. Add your own patterns under `permissions.hardline` in your config.
- **Sandbox, and where it fails open.** In `auto` and `yolo` modes, and in every background, cron and loop session, bash runs inside [bubblewrap](https://github.com/containers/bubblewrap). The system is read-only, the project (and any directory added with `/add-dir`) is writable, `$HOME` is hidden except `~/.cache` (writable), `~/.local/share/uv` (read-only) and any entries you list under `sandbox.home_readonly` in your own `~/.k3code/config.yaml` (read-only; never read from a project's config; `~/.ssh`, `~/.config/k3code` and anything above them are refused), `/tmp` is private, and the command does not inherit your API keys. The network stays on. **If `bwrap` is missing or user namespaces are disabled, bash runs without the sandbox.** k3code logs a warning once, and `k3code doctor` reports it. Install bubblewrap before you run unattended.
- **Spend caps.** Off by default. `reliability.session_tokens`, `reliability.session_usd`, `reliability.day_tokens` and `reliability.day_usd` stop a turn, or the day's work, when a limit is reached. Dollar figures are estimates, not an invoice.
- **Approvals write rules.** Choosing *always* writes a narrow rule (for example `git commit *`) into the project's `.k3code/config.yaml`. Review those rules before you commit that file.
- **Secrets.** Keys live in `~/.config/k3code/env` (mode 0600) or in your environment. The config names an environment variable, never the value. `k3code export` redacts secrets, including any configured key value that appears in a session.
- **Imports ask per risky item.** `k3code import`, `/import` and the setup wizard's import show a diff for each MCP server, permission rule, provider endpoint (`base_url`, `api_key_env`) and hook in the bundle and apply it only on an explicit yes. `--yes` does not cover them; `--trust-bundle` does. Without a terminal to ask in they are skipped with a warning.
- **Learned notes stay out of the repository.** Facts from the session review go to `~/.k3code/projects/<project>/learned.md`, one capped line each (also when you edit the file), and reach the prompt fenced and labelled as auto-generated. k3code never writes them into `K3CODE.md` or `AGENTS.md`. An "auto-do high-risk plans" proposal needs 10 approvals and no denials in one project, and applies to that project only.
- **Not a security boundary.** Treat k3code like a script you run yourself. It is not built to contain a hostile model, repository or MCP server.

To report a vulnerability, use the private route in [SECURITY.md](SECURITY.md).

## Configuration

State lives in `~/.k3code/` (override with `K3CODE_HOME`): `config.yaml`, session and usage databases, the journal, memory, learned preferences, logs. Secrets live only in `~/.config/k3code/env` (mode 0600) or your environment. A project can add `.k3code/config.yaml`, read from the directory k3code was started in (or `--config-dir`). It applies only after you trust that exact file: an interactive start shows what it changes and asks once, and asks again when the file changes. Headless and piped runs ignore an untrusted file. `k3code trust [PATH]` grants trust and `k3code trust --revoke` takes it back. The same answer covers the project's `.k3code/agents`, `.k3code/skills`, `.k3code/output-styles`, `.claude/skills` and `.agents/skills`: they load only in a trusted project, and a change to any of them asks again. A project agent can add an agent but never replace a built-in or user agent of the same name. A project config cannot set `providers` (it would choose where your keys are sent); they are ignored with a warning in the trust summary and `k3code doctor`. The project's memory always loads, fenced as project instructions from the repository: `K3CODE.md`, `AGENTS.md` and `CLAUDE.md` (all that exist, identical ones once) in every directory from the repository root down to the working directory, nearest last, after `.k3code/rules/*.md`. A line `@path` imports a file (relative to the importing file, at most 5 deep, never from outside the repository; the user's `USER.md` may import from your home directory). Each file is capped at 20,000 characters and all of them together at 40,000.

Precedence per top-level key: command-line flag > environment (`K3CODE_<KEY>`, scalar keys only, for example `K3CODE_PERMISSION_MODE`) > project config > user config > defaults. Nested sections are replaced as a whole, not merged. `providers` comes from the user config only. Change settings with `/config`, `/update-config` or `k3code setup --step <name>`; edits are backed up and `/config rollback` restores the last one.

```yaml
default_model: default              # the model key new sessions start on (/tune --global and Enter in the popup write it)
providers:                          # the fallback chain, in order (add as many as you like)
  - name: gateway
    kind: openai                    # openai-compatible, or: anthropic
    base_url: https://your-gateway.example/v1
    api_key_env: GATEWAY_API_KEY    # the NAME of an environment variable (or a key in ~/.config/k3code/env)
    models:
      default: [model-a, model-b]   # tried in order before moving to the next provider
      strong: model-strong          # planning, review, advisor
      cheap: model-cheap            # background, loops, cron, titles, compaction
    descriptions:                   # optional: one line per model key, shown in the /tune popup
      default: Everyday coding
      strong: Planning and review
      cheap: Fast and cheap
  - name: direct
    kind: anthropic
    base_url: https://api.anthropic.com
    api_key_env: ANTHROPIC_API_KEY
    models: {default: claude-sonnet-5-5}

permission_mode: ask                # ask | auto-edit | plan | auto | yolo
autonomy:
  plan_first: true
  escalate_main: true               # a turn that stalls on the main tier continues on the strong tier (default)
  auto_continue: false              # true: an ordinary prompt runs as an implicit goal until the judge says done
  fanout: {max_parallel: 3}
ultracode:
  min_scope: small                  # with the ultracode mode on: a prompt rated at least this runs the pipeline
                                    #   (trivial | small | medium | large | huge); also max_tokens and max_agents
wake_words:
  enabled: true                     # false switches every wake word off
  # ultraplan: false                # or just one: ultracode | ultraplan | ultraresearch (all default to true)
mcp:
  servers:
    search: {url: "https://example.org/mcp"}
```

`descriptions` is optional on each provider; for a model key that several providers define, the first non-empty description is shown. `ultracode.min_scope` must be one of the five scope names, and an unknown key under `ultracode` or `wake_words` is ignored with a warning. See [Ultracode mode](#ultracode-mode) and [Wake words](#wake-words).

### Hooks

Hooks run your own commands on agent events, with Claude Code's contract, so existing hook scripts work:

```yaml
hooks:
  PreToolUse:                       # also PostToolUse, UserPromptSubmit, SessionStart, Stop
    - {matcher: "bash|edit", command: "~/bin/check-tool.sh", timeout: 30}   # matcher: tool-name regex
```

The command gets the event as JSON on stdin (`session_id`, `cwd`, `hook_event_name`, `tool_name`, `tool_input`, `tool_response` for PostToolUse, `prompt` for UserPromptSubmit). Exit 0 lets the action go on, and its stdout may be JSON `{"decision": "block"|"approve", "reason", "additionalContext"}`. Exit 2 blocks the action, and stderr is the reason the model sees. Any other exit status, or a run longer than `timeout` (default 60 s), is logged and blocks nothing. PreToolUse runs before the permission prompt: it can block a call or answer the prompt with `approve`, but it can never turn a deny (a hardline rule, a deny rule) into an allow. A Stop hook cannot extend a turn. Hooks run as you, outside the sandbox, with the same scrubbed environment as other child processes plus `CLAUDE_PROJECT_DIR`. Hooks in your user config always run; hooks in a project's `.k3code/config.yaml` run only once you trust the project, and the trust prompt lists each one.

Other Claude Code files k3code reads: `CLAUDE.md` (see above), skills in `~/.claude/skills` (set `skills: {import_claude: false}` in your user config to skip them) and, in a trusted project, `.claude/skills` and `.agents/skills`. A trusted project's `.mcp.json` servers show in `/mcp` as available; none starts until you run `/mcp enable <name>` (`/mcp disable <name>` stops it). That choice is stored under `~/.k3code/projects/`, not in the repository, and a changed server definition has to be enabled again. `env` and `headers` values are used as written (no `${VAR}` expansion), and `sse` servers are skipped.

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

# or all of the above, as the merge gate runs it (summary table, logs in .k3dev/ci/)
scripts/ci/check.sh
```

There are no GitHub Actions: checks, merges and releases run locally ([`docs/RELEASING.md`](docs/RELEASING.md)).

- **Exit checks:** `scripts/exit/run_all.sh [--soak-minutes N] [--only m0,m1,…]` runs every check (real-TUI scripted flows, daemon and chaos tests, the panes tests, a clean-install test in a Fedora 44 podman container, and a 30-minute daemon soak in the background) and **overwrites the tracked** `docs/reports/exit-status.md`. Expect 30–40 minutes (an untested estimate) and heavy CPU, RAM and disk use. It needs `bash`, `python3`, `uv`, `node` (with the TUI built first), `go`, and optionally `podman`. Live-model rows stay pending while the provider quota is exhausted.
- **How it was built:** most of the code was written by headless coding agents driven by `scripts/dev/omni-worker.sh` from the task specs in `scripts/dev/tasks/`. Each task produced a branch and a report in `docs/reports/`. Those scripts are internal build tooling; you do not need them to build, test or use k3code.
- **Branches:** the code was built on branches named by milestone (`w/m1-gateway`, `w/m2-ops`, `w/m4a-autonomy`, `w/m6-install` and so on, with `w/merge-*` integration merges); they are not in this repository. Integration happens on `Main`, which takes changes by pull request once `scripts/ci/check.sh` has posted `local-ci` ([`docs/RELEASING.md`](docs/RELEASING.md)).
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
- **Platforms.** Only Linux on x86_64 has been tested. A clean install was tested in a Fedora 44 container. The installer tests ran on macOS while CI ran on GitHub Actions; a macOS run is now a manual step before a release ([`docs/RELEASING.md`](docs/RELEASING.md)). Windows (`install.ps1` via WSL) is tested only against a stand-in for `wsl.exe` on Linux: `scripts/ci/platforms.sh` installs, updates, rolls back and uninstalls through it into an Ubuntu container, and on clean Ubuntu, Debian and Alpine containers.
- **Updates.** There is no release yet, so the update and rollback path has only been tested against local version directories.

| | Milestone | Built | Verified so far |
|---|---|---|---|
| **M0** | Scaffold, core skeleton, vendored TUI and panes | ✅ | All five exit rows pass, including the live-model row (through the `claude-cli` provider) |
| **M1** | Daily-driver agent: gateway, TUI (agent list, focus mode, history), permissions, commands | ✅ | All 10 exit rows pass, including the live `/review` check; three days of real use is not verified |
| **M2** | 24/7 reliability: offline pause/resume, retry, journal, governor, daemon, doctor, stats | ✅ | All five chaos checks and a 30-minute soak (119 turns, none lost, bounded memory growth) pass; the 72-hour soak is pending |
| **M3** | `k3` keymap, agent states and approvals in panes | ✅ | Keymap tests and the live pane-badge check pass; the first-time-user test is pending |
| **M4** | Plan-first and scope gate, tiers, fan-out, `/ultra*`, `/preview`, `/advisor`, loops, cron, automations | ✅ | All exit rows pass except the 30-task scope eval: its 30 labels are proposed and not yet confirmed |
| **M5** | Learning, proposals, project preparation, self-optimizer | ✅ | Demo and tests pass; live mem0 and 2 weeks of use are pending |
| **M6** | Installer, guided setup, update with rollback, release script | ✅ | A clean Fedora 44 container installs in about a minute; setup resume, `--from-bundle` and update rollback pass. The upstream-sync check passes under the agreed policy: TUIOS stays mergeable (0 conflicting files) and the heavily modified Hermes TUI is a documented frozen fork whose upstream fixes are cherry-picked by hand ([`docs/UPSTREAM.md`](docs/UPSTREAM.md)) |


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
