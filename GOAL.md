# k3code: goal and requirements

This is the standing goal of the project. It states what k3code must be, how success is measured, and the constraints on how it is built.
The roadmap and exit criteria are in [`docs/PLAN.md`](docs/PLAN.md), the evidence is in [`docs/reports/exit-status.md`](docs/reports/exit-status.md), and the original request is preserved verbatim in [Appendix A](#appendix-a-the-original-request).

| | |
|---|---|
| **Set** | 2026-10-07 |
| **Owner** | the owner |
| **Name** | `k3code` (working name; a better one will be chosen later) |
| **Repository** | `K3NOXOFFICIAL/k3code` (private), synced with `~/src/k3code` |
| **Done when** | Every milestone M0–M6 meets its exit criteria, verified on the owner's laptop (see [§8](#8-definition-of-done)) |

---

## 1. Vision

A terminal coding harness that works like Claude Code (slash commands, plan mode, an agent list under the input, input history) but is **owned, open, and built to run unattended**: it keeps working when the owner is away, survives dropped connections and crashes, routes work across several providers and cheaper models, fans out agents on its own, proposes improvements, and gets better at working with its owner over time.

It is assembled from the best parts of existing open-source harnesses rather than written from nothing, and it ships as one installable, guided setup that works on a new device in minutes.

---

## 2. Principles and constraints

**How it is built**

1. **Assemble, don't reinvent.** Take designs and code from open-source harnesses where that is cheaper than writing them; record every copied or ported file in `VENDOR.toml` with its origin, commit and license.
2. **License hygiene.** Only permissive licenses (MIT, Apache-2.0) are used. Proprietary or leaked code (for example, the leaked Claude Code source republished as "Open-ClaudeCode") is never used, and closed binaries are never used as a base. `scripts/vendor_check.py` checks the license and the banned-project label of every listed file entry.
3. **Spend the owner's Claude tokens sparingly.** Build work is done mainly by headless coding agents on the owner's OmniRoute gateway; Claude orchestrates and reviews. If no OmniRoute model works, falling back to Claude Code (Sonnet 5.5) is preferred over stopping.
4. **One branch per task, milestone-named**, merged into an integration branch with a pull request into `Main`; each task leaves a report in `docs/reports/`.
5. **Test what you claim.** Features are verified with automated tests, scripted runs of the real TUI, chaos tests, and, where models are involved, live runs. Anything not yet verified is marked as such, never assumed.

**What it must respect**

6. **Hard limits from the owner's infrastructure:** never print or store secrets; never create, rotate or revoke keys; never restart or edit services on the owner's servers; never touch other people's data; the multi-user home network's forwards are read-only. These bind k3code and every task it runs, and nothing in k3code may be changed to loosen them; the hardline list encodes the destructive and secret-leaking ones. **Isolation from Hermes:** k3code keeps all of its state under its own `K3CODE_HOME` (default `~/.k3code`), never reads or writes `~/.hermes` (including `~/.hermes/.env`), and ships no messaging-platform gateway, so it cannot collide with the owner's running Hermes agent (for example over a shared chat-bot token).
7. **Linux first.** The owner uses Linux only. No new phone app or web app.
8. **No surprise installs.** Anything that touches the real system (systemd units, global config) is explicit and reversible, and is never done by tests.

---

## 3. Requirements

Each requirement has an ID, an acceptance statement, and the milestone that delivers it. Commands are listed in [§4](#4-command-set).

### A. Coding agent core

| ID | Requirement | Acceptance | Milestone |
|---|---|---|---|
| A1 | Codes like Claude Code: read, edit, patch, write, bash, grep, glob, web, todo tools; plan mode; per-tool permissions with approvals | A headless and an interactive run completes a read/edit/bash task; approvals work as once / session / always / deny | M0, M1 |
| A2 | Permission modes and learned rules; a hardline list that no mode can override | Modes cycle in the TUI; hardline commands are refused in every mode | M1 |
| A3 | MCP servers, skills, project and user memory (including mem0), output styles, extra working directories | Each is configurable and usable from commands; MCP schemas are loaded on demand to keep context small | M1, M4 |
| A4 | Context management: compaction, session fork/branch/resume/rename, export and import of sessions and settings | Round trip export → import works with secrets redacted | M1 |

### B. Autonomy

| ID | Requirement | Acceptance | Milestone |
|---|---|---|---|
| B1 | Agentic when left alone; runs 24/7 as a daemon with sessions that outlive the UI | A session keeps running after the TUI detaches; the service restarts itself and has a safe mode after a restart storm; a 72-hour unattended soak (`scripts/exit/soak.sh --hours 72`: a daemon with a `/loop` and two cron jobs) ends with the daemon alive, zero lost turns, no errors in its log and bounded memory growth | M2 |
| B2 | Goals and loops as commands (`/goal`, `/loop`) | A goal runs until its judge and optional check agree; a loop ticks until its condition, count or tick budget | M1, M4 |
| B3 | **Think ahead:** in auto mode, plan before executing; decide automatically whether the task needs a large scope (fan-out) or just a plan, then proceed without asking | In auto mode a task of scope medium or larger, or one that hits the danger list, emits a plan event before any change is made; trivial and small low-risk tasks run directly; the verdicts agree with the owner's labels on ≥ 80 % of a 30-task set | M4 |
| B4 | **Fan out agents automatically** by project and task complexity | Large tasks with independent parts run as parallel workers, are reviewed, merged and tested | M4 |
| B5 | **Task degradation:** route unimportant and background work to faster or cheaper models, escalating when they struggle | Background, loop and cron turns use cheap tiers; cost drops ≥ 30 % at the same pass rate | M4 |
| B6 | **Automations:** schedules and event triggers | Cron, file change, git, webhook, session events, network state and idle triggers fire exactly once per event, including a job missed while offline | M4 |
| B7 | **Proactive proposals:** "do you also want me to set up X", "this action could lead to Y", "you could improve Z" | In auto mode, after an approved plan and after a finished task, up to three cards appear per pass, each of kind *also set up* (X), *consequence* (Y) or *improvement* (Z), derived from that session's task, plan and result and ranked by the project's decision history; accepting a card sends its action as a new prompt; a dismissed proposal never returns | M4, M5 |

### C. Reliability and routing

| ID | Requirement | Acceptance | Milestone |
|---|---|---|---|
| C1 | If the API does not respond or the device is offline, retry until it works | Errors retry with backoff; a long rate limit parks until its reset | M2 |
| C2 | **Offline pause and automatic resume** | Network off mid-goal: the turn pauses without error and resumes within 10 s of the network returning | M2 |
| C3 | **Fallback routing across models and across providers:** primary, secondary, tertiary (and more) | A chain of at least three entries that mixes a second model on the same provider with a different provider (one reached directly, not through a gateway) is tried in order: with the first two blocked or failing, the third answers, and `/stats` shows each hop; a blocked primary fails over within 30 s; a single provider outage is a failover, not an "offline" pause | M0, M2 |
| C4 | No duplicate side effects after a crash | `kill -9` during a shell command: the call is marked interrupted and is not re-run | M2 |
| C5 | Bounded unattended operation | Concurrency, I/O pressure, disk space, token and spend budgets and a loop guard limit what an unattended run can do | M2 |

### D. Self-improvement and learning

| ID | Requirement | Acceptance | Milestone |
|---|---|---|---|
| D1 | **Self-optimizing and self-improving** | A weekly optimizer proposes changes with evidence; accepted changes run as an A/B trial and roll back automatically if worse | M5 |
| D2 | **Learn the owner's decisions and way of thinking:** better proposals, smarter permissions ("you always allow X → add a rule / do it automatically?"; "you keep switching away from model M for this kind of task → avoid M / re-tier"), better project preparation | After repeated approvals a rule is proposed (as a rule or an automatic action); repeated model switches away from a model produce a re-tiering proposal; preferences are distilled into memory; new projects are prepared on first open; after two weeks of use at least three rules were accepted and approval prompts dropped by ≥ 40 % | M5 |

### E. Interface

| ID | Requirement | Acceptance | Milestone |
|---|---|---|---|
| E1 | **Multi-session view:** agents listed under the input with states *needs input*, *working*, *completed*, navigable with the arrow keys | With a background session or sub-agent running, rows appear directly below the input, each with a live state (needs input / working / completed / failed). `↓` from an empty input (no history cycle in progress) focuses the list, `↑`/`↓` select, `Enter` attaches the session so a needs-input approval can be answered inline, `Esc` returns to the input | M1 |
| E2 | ↑ cycles previous inputs | Works together with the agent list | M1 |
| E3 | **Focus mode:** hides non-essential UI, tool calls, thinking and output; shows only questions, input prompts, final results and important information | A toggle key and `/focus`; screen-level tests assert what is hidden | M1 |
| E4 | **Multiple windows in the terminal** (like TUIOS) with a more intuitive keymap and edit mode | `k3`: typing passes through by default, one leader key opens a mode, Esc always returns, a hint bar is always visible; a first-time user can drive it from the hints alone | M3 |
| E5 | `/preview`: a quick rough sketch of what a task will produce | Returns in under 30 s without changing anything | M4 |
| E6 | Agent states and approvals integrate with the multi-window terminal | Pane badges follow state within 1 s; approvals appear in its Inbox | M3 |

### F. Setup and distribution

| ID | Requirement | Acceptance | Milestone |
|---|---|---|---|
| F1 | **Complete setup that is easy to install on new devices** | A fresh machine goes from nothing to a configured, running install in under 10 minutes | M6 |
| F2 | **Setup mode and guided first-run setup** covering: information about the user; the system and environment; what the install is mainly used for; default theme; providers, tiers, permissions, integrations; optional 24/7 service | Resumable; also scriptable with an answers file | M6 |
| F3 | Health checks and updates | `k3code doctor`; updates are smoke-tested and roll back automatically | M2, M6 |
| F4 | Settings and sessions move between devices | `.k3bundle` export/import; `--from-bundle` install | M1, M6 |

### G. Repository and process

| ID | Requirement | Acceptance | Milestone |
|---|---|---|---|
| G1 | Private GitHub repository `k3code`, synced with a local checkout | Exists; branches named by milestone are pushed | M0 |
| G2 | Documentation | A README that explains install, use, architecture and status; this goal document; milestone reports | ongoing |

---

## 4. Command set

All of these exist and appear in `/help`. A test (`core/tests/test_merge_m5.py`) parses this table and fails if a command is missing.

| Command | Purpose |
|---|---|
| `/goal` | Set an objective the agent works toward until a judge (and an optional shell check) says it is done |
| `/loop` | Repeat a prompt on an interval, or self-paced, with count, condition and tick limits |
| `/compact` | Summarize older conversation to free context |
| `/bg` | Run a task in the background (also: send the running turn to the background) |
| `/effort` | Show or set reasoning effort |
| `/model` | Show or switch the model; `/model chain` manages the fallback chain |
| `/ultracode` | Plan, fan out, adversarial review, fix, test, with a budget |
| `/ultraplan` | Deep planning: independent planners plus a judge |
| `/preview` | Quick sketch of what a task would produce, with no changes |
| `/stats` | Usage, tiers, failovers, retries and pauses |
| `/branch` | Create a git branch (optionally a worktree) and fork the session onto it |
| `/clear` | Clear the transcript |
| `/exit` | Close the session |
| `/stop` | Interrupt the running turn |
| `/update` | Check for and apply updates, with rollback |
| `/settings` | Show the effective model chain, permissions, style, providers and paths |
| `/export` | Export settings (secrets redacted) and sessions |
| `/fork` | Copy a session into a new one |
| `/import` | Import settings and sessions |
| `/mcp` | List and reload MCP servers |
| `/memory` | List, add to and edit memory; search mem0 |
| `/output-style` | Choose the response style |
| `/permissions` | Show and edit modes and rules; list suggested rules |
| `/rename` | Rename the session |
| `/resume` | Resume a stored session |
| `/skills` | List and show skills |
| `/ultraresearch` | Multi-source research report with checked citations |
| `/review` | Review a diff or path |
| `/debug` | Verbose logging and a redacted debug bundle |
| `/doctor` | Health checks with fix hints |
| `/schedule` | Cron jobs, including plain-language schedules |
| `/config` | Read, change, edit and roll back configuration |
| `/update-config` | Change configuration by describing the change in plain words |
| `/add-dir` | Add an extra working directory to the session |
| `/artifacts` | List files that sessions produced |
| `/advisor` | Critical second opinion from the strong tier |

Additional commands introduced by the design: `/focus`, `/go`, `/scope`, `/proposals`, `/learn`, `/optimizer`, `/self-improve`, `/automations`, `/daemon`, `/help`.

---

## 5. Design decisions

| Decision | Choice | Why |
|---|---|---|
| Base | A new harness assembled from parts of several projects, not a fork of one | Explicit request: take the parts of each that make sense |
| Parts | Hermes Agent (TUI, router tables, goals, loops, cron, suggestions, fan-out design), TUIOS (multi-window), opencode (permission rules), Codex (review rubric), Atomic Agents (research flow) | Best fit per feature; all permissive; every copied or ported file is recorded in `VENDOR.toml` |
| Design references | openclaw (onboarding-wizard flow, heartbeat and cooldown ideas, systemd unit policy), pi (sub-agent role ideas), Ante's public protocol documentation | Studied only; nothing is copied from them |
| Components | Python core, TypeScript/Ink TUI, Go multi-window terminal | Reuse the existing TUI and TUIOS; keep the agent logic in one language |
| Multi-window keymap | Zellij-style: typing by default, `Ctrl+G` leader, Esc always returns, hint bar always visible | The owner found TUIOS's shortcuts and edit mode unintuitive |
| Provider routing | Own fallback chain (providers and models), independent of any gateway's own routing | A gateway outage must still have a fallback |
| Model tiers | Which models fill each tier is chosen by the user in the guided setup, with no hard-coded defaults | Provider and model choice is personal and changes often |
| Task routing | Four tiers: `main` (default coding, interactive turns, sub-agents), `strong` (planning, review, advisor), `cheap` (background, loop and cron turns, titles, compaction, judges) and `fast` (previews, quick classification). A task kind maps to a tier by a policy table the user can override; a cheap-tier task that keeps failing escalates to the next tier | Cheap work must not run on expensive models, and hard work must not get stuck on cheap ones |
| Visibility | Private repository; when to make it public is the owner's decision ([§9](#9-decisions-and-inputs-needed-from-the-owner)) | Explicit request: private |

---

## 6. Out of scope for now

- Windows and macOS support; a phone app or web UI.
- Messaging-platform gateways (chat bots), hosted rooms, billing and account features inherited from upstream code (removed or disabled).
- Opening pull requests against k3code's own code automatically (`/self-improve` only drafts an issue).
- `/artifacts publish` (a stub until there is somewhere to publish to).
- Multi-user or team features.

---

## 7. Milestones

Detailed scope and exit criteria are in [`docs/PLAN.md`](docs/PLAN.md); evidence is in [`docs/reports/exit-status.md`](docs/reports/exit-status.md).

| Milestone | Delivers | Requirements |
|---|---|---|
| **M0** | Scaffold, core skeleton, vendored TUI and multi-window terminal, license tracking | A1 (basic), C3, G1 |
| **M1** | Daily-driver agent: gateway, TUI (agent list, focus mode, history), permissions, the everyday commands | A1–A4, B2, E1–E3, F4 |
| **M2** | 24/7 reliability: offline pause/resume, retry, journal, governor, daemon, doctor, stats | B1, C1–C5, F3 |
| **M3** | `k3` keymap; agent states and approvals in the multi-window terminal | E4, E6 |
| **M4** | Autonomy: plan-first, scope gate, tiers, fan-out, `/ultra*`, `/preview`, `/advisor`, loops, cron, automations | A3 (on-demand MCP schemas), B2–B7, E5 |
| **M5** | Learning, proposals, project preparation, self-optimizer | B7, D1, D2 |
| **M6** | Installer, guided setup, update with rollback, release CI | F1–F4 |

---

## 8. Definition of done

The goal is complete when **every milestone M0–M6 meets its exit criteria**, verified on the owner's laptop and recorded in `docs/reports/exit-status.md`. Concretely:

- all automated checks pass (`scripts/exit/run_all.sh`), with no row marked failing;
- the criteria that need real-world time or a person are closed: the 72-hour soak, three days and then two weeks of daily use, the first-time-user test of the multi-window keymap, and the owner's confirmation of the 30 scope labels;
- the criteria that need a live model (`/review`, `/preview`, `/ultraresearch`, cost comparison) have been run against a working provider;
- the work is merged into `Main` through the pull request.

---

## 9. Decisions and inputs needed from the owner

- Confirm or correct the 30 proposed task-size labels in `scripts/exit/scope_eval.jsonl`.
- Run the first-time-user test of `k3`, and use k3code for real for three days and then two weeks.
- Provide a working provider key (and a quota) for the live-model checks.
- Choose the final name.
- Decide when to merge the integration branch into `Main` and when to make the repository public.

---

## Appendix A: the original request

The request as written on 2026-10-07 (typos kept), followed by the additions made in the same session.

> help me to build a harness for coding. i want it to be based on opensource projects, so you dont have so much to do for finidhing it. i want it to be able to code, be agentic even when left alone (autonomous), have loops and goals as a command, have a multi session mode like the agent view / mode in claude code (needs input, working and completed), it should be able to run 24/7 while being left alone, i want it to have fail fallbacks in case the api doesnt respond or the device is offline (like continue to retry until working again) and also when offline pause the work and automatically continue when online again, it should also have automations. also i want the harness to be self optimizing and self improving. also i need automatic task degredation (automatically rerouting non important tasks and background tasks to faster / less costing models) and i want specificall these commands to exsist: goal, loop, compact, background (/bg), effort, model, ultracode, ultraplan, preview, a mode that quickly creates a kind of preview for the task (like a scetch to see fast how it will be roughly), stats, branch, clear, exit, stop, update, settings, export (settings and session), fork, import (session import and config import), mcp, memory, output-style, permissions, rename, resume, skills, ultraresearch, review, debug, doctor, schedule, config, update-config, add-dir, artifacts, advisor and a mode that hides non essential UI and also hides tool calls, thinking and output (only show questions, prompt fot input and endresult as well as other important information) i want the harness to be a complete setup which is easy to install on new devices, and also a setup mode as well as a guided setup for the first use (info about the user, info about the system and environment, what will this install mainly be used for, default theme, and other things). also the harness should be actively propose changes to the user that make sense based on the project / session (do you also want me to setup this, this action could lead to this, you could also improve this, etc), also it should learn from the users decisions and way of thinking to actively get better at proposing changes and also get better with permissions and preperation of projects (the user always seems to want to not use this model or the user always allows this action = ask user to add to permissions or to automatically do this in the future). i also need the harness to have automatic fallback routing (not only models, but also allowing to add multiple providers so a primary one, secundary one and a 3rd fallback no matter if models or different providers), i want the harness to also think ahead of its actions, in auto mode it should always create a plan first before executing so it doesnt just start a project in the dark with no plan (automatically decide if the user wants a huge scope or if it only requires a plan and then automatically going ahead). specifically the agent mode should be navigatable with the arrow keys just like claude code, if there are multiple agents they also should appear under the input field like claude code and pressing the up arrow key it should also go through the previous inputs. for this i want to create a new github repo on my github account as well as locally (synced repo). i dont have a specific name for the harness so for now call it k3code (will come up with a better name sometime in the future). also it should provide a TUI as well as multiple windows inside the terminal a bit like this project: https://github.com/Gaurav-Gosain/tuios (tho i still dont really like how the shortcuts and edit mode work in this project, maybe you have ideas to make it more user intuitive). also here a few harnesses i found which might be good for using as a base to improve or use specific snippets of: https://github.com/Eigenwise/atomic-agents https://github.com/AntigmaLabs/ante https://github.com/LING71671/Open-ClaudeCode https://github.com/nousresearch/hermes-agent, i also want the harness to be able to fan out agents automatically based on the project and task complexion.

**Additions in the same session**

- Build it as a new harness from the parts of several harnesses (not a fork of one project), for example Hermes for the agents, openclaw for loops and the guided setup, opencode for other parts.
- Multiple windows: fork TUIOS with a new, more intuitive keymap.
- The GitHub repository is private.
- The model tiers for automatic degradation are chosen in the guided setup.
- Use OmniRoute-based agents to build it, so the owner's Claude account is not drained; if no OmniRoute model works, fall back to Claude Code (Sonnet 5.5) rather than stopping.
- Later: a proper README, and a more detailed and structured goal document (this file).
