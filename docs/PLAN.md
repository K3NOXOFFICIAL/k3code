# k3code: a new coding harness assembled from OSS parts

## Context
the owner wants their own terminal coding harness, **k3code** (working name). It should feel like Claude Code (slash commands, an agent list under the input, ↑ history), run unattended 24/7, keep retrying when offline and pause/resume work, route to fallback providers and cheaper models, improve itself, learn the user's decisions, plan before it acts, fan out agents on its own, and ship with a multi-window terminal (a tuios fork) and a guided installer.

the owner chose **"a new harness built from the parts of other harnesses"** over a fork of a single project. The design judge's top-scored option was a whole-fork of Hermes (tied 35/35 with the small-core design). I did not take it, because it is the "Hermes fork" option the user already declined. The whole-fork stays the documented fallback if the M0 TUI spike shows that a new core is too costly.

Repo: **`K3NOXOFFICIAL/k3code`**, private, MIT, upstream notices kept. Local checkout `~/src/k3code`. gh is already authenticated with `repo` scope.

Research (2026-10-07):

| Source | License | Verdict |
|---|---|---|
| Hermes Agent | MIT | Source for most parts: goals, loops, cron, fallback, learning, approvals, Ink TUI |
| opencode | MIT | Permission rules and arity, plan/compaction prompts, task-tool contract, snapshots |
| openclaw | MIT | Onboarding wizard flow, heartbeat, systemd unit policy, task suggestions, health checks |
| tuios | MIT | Forked as `k3` panes, with a new keymap |
| codex | Apache-2.0 | Review rubric |
| pi | MIT | Sub-agent role prompts |
| atomic-agents | MIT | deep-research flow |
| ante crates | Apache-2.0 | Protocol reference only |
| **Open-ClaudeCode** | leaked, all rights reserved | **Banned** |
| ante binary | closed | **Banned** |

## Step 0: goal and token model (right after approval)
1. **Set the standing goal.** Write the full original request, plus the additions made in this session (OmniRoute workers, private repo, name k3code), to:
   - `GOAL.md` in the repo;
   - `~/.claude/projects/-home-user/memory/k3code-standing-goal.md`, with a pointer in `MEMORY.md`;
   - mem0 (`agent_id=claude-code-laptop`).

   Work continues until every milestone's exit criteria pass.
2. **Gate on cheap workers.** Run `claude -p --settings ~/.claude/settings.omniroute.json --model sonnet "reply OK"` (sonnet maps to `auto/coding-manual`). Then confirm the call appears in `hub_omniroute_insights__usage_summary` and not on the Claude account. Always pass `--model` explicitly, because the profile's OPUS slot is empty.
3. **Build model.**
   - **Claude (me):** specs, orchestration, architecture decisions, final reviews only.
   - **Code-writing work:** Bash-launched headless `claude -p` workers on the OmniRoute profile, one git worktree each, **at most 2–3 concurrently** (protected-host-a HDD).
   - **Text-only work** (docs, boilerplate, summaries): `mcp__k3nox__ai_delegate` with `auto/coding-cheap`.
   - **No `Workflow`/`agent()` fan-out during the build.** Those bill Claude; the design workflow alone used about 1.4M Claude tokens.
   - **Driver.** `scripts/dev/omni-worker.sh <task.md> <worktree>` runs a worker headless, logs to `.k3dev/runs/`, retries on OmniRoute errors, and is supervised so workers keep going while Claude is idle.

## Architecture

| Component | Language | What it is |
|---|---|---|
| `core/` k3coded | Python 3.13 (uv) | New agent loop + tool registry + provider router + scheduler; vendored modules sit behind ports |
| `tui/` | TS/Ink | Vendored Hermes `ui-tui` + `hermes-ink`, talking to k3coded over the Hermes tui_gateway JSON-RPC contract (WS/stdio) |
| `panes/` `k3` | Go | tuios subtree with the new `internal/k3keys` keymap |

**Processes** (systemd --user, linger):
- `k3code-host`: sessions, WS on 127.0.0.1, plus the tailnet with a token.
- `k3code-sched`: cron, events, netwatch, learner, governor.
- `k3code` TUI clients attach to the host. If the host is down, they fall back to a stdio child.

**Discipline**:
- Every non-new file is listed in `VENDOR.toml` (project, path, commit, license, sha256).
- `scripts/vendor_check.py` enforces the license allowlist and the banned sources.
- Core code reaches vendored code only through `core/k3/ports/*`.

### Reuse map (vendor = copy + adapt; ref = reimplement from design)

| Feature | Vendored | Reference / new |
|---|---|---|
| Coding tools (read/edit/patch/bash/grep/glob/web/todo) | hermes `tools/file_tools`, `fuzzy_match`, `patch_parser`, `todo_tool` | NEW registry |
| Permissions + plan mode | opencode `permission/index.ts` + `arity.ts` → `k3/permissions_rules.py`; opencode plan prompts | hermes `approval_smart` |
| Fallback chain P/S/T (providers and models) | hermes `fallback_config.py`, `retry_utils.py`, `error_classifier` tables, `fallback_cooldown` | ref: `chat_completion_helpers` walk, `credential_pool` |
| Retry until online + offline pause/auto-resume | hermes `cron/unreachable_retry`, `quota_hold` | NEW `netwatch` (ONLINE/DEGRADED/PROVIDER_DOWN/CAPTIVE/OFFLINE), `persistent_retry`, `journal` (side-effect tools are never re-run after a crash) |
| /goal, /loop | hermes `goals.py`, `goal_command.py`, `loops.py` (KV store and judge swapped for ports) | openclaw heartbeat cooldown |
| Automations / /schedule | hermes `cron/jobs` schedule math, `suggestions.py` + catalog | ref: `cron/scheduler`; NEW event bus (file/git/webhook) |
| Self-improvement | hermes `background_review`, `curator`, `skill_manager_tool` | NEW weekly optimizer (A/B overlays, rollback, PRs) |
| Learning decisions / permission suggestions | hermes `approvals_suggest.py`, mem0 plugin | NEW `decisions.py` + `distiller.py` → USER.md / preferences / mem0 |
| Proactive proposals | openclaw task-suggestion tools (ported), hermes suggestions dedup latch | NEW post-turn proposer |
| Degradation to cheaper models | litellm complexity-router heuristics (ported, no dependency) | NEW `degrade.py` tiers, chosen in the wizard |
| Plan-first auto mode + scope decision + auto fan-out | hermes `delegate_tool` batch, `subagent_worktree`, `kanban_decompose` prompts; pi planner/worker/reviewer prompts | NEW `scope_gate`, `complexity`, `fanout` with governor |
| Agent strip under the input, ↑ history, states | hermes ui-tui `agentsPanel`, `agentControls`, `activeSessionSwitcher` (moved below the composer, cross-session rows) | NEW `tui/src/k3/agentStrip.tsx` |
| Focus mode (only questions, input, results) | hermes `/focus` + details | NEW importance tag on every event + `focusPolicy.ts` |
| Compaction, MCP (deferred tools), memory, skills | hermes `context_compressor`, `mcp_tool`, `memory_manager`, skills; opencode compaction template | k3nox MCP + mem0 + skills-library preconfigured |
| Sessions: export/import/fork/branch/resume/rename | hermes `hermes_state_portability`, `session_export`, `rewind` | NEW `.k3bundle` (settings + session, redacted) |
| Install + guided setup + doctor + update | openclaw onboard flow (resumable) + systemd policy + health-check contract; hermes setup/doctor | NEW `install.sh`, 12-step wizard (user, system, main use, theme, provider chain, degradation tiers, keymap tour), versioned update with rollback |
| /review, /ultraresearch | codex `review/rubric.md` (Apache NOTICE); atomic-agents deep-research prompts | runs on k3nox searxng/fetch |
| Multi-window | tuios subtree | NEW `internal/k3keys` |

**Commands.** All 36 are mapped:
- goal, loop, compact, bg, effort, model, ultracode, ultraplan, preview, stats, branch, clear, exit, stop, update, settings, export, fork, import, mcp, memory, output-style, permissions, rename, resume, skills, ultraresearch, review, debug, doctor, schedule, config, update-config, add-dir, artifacts, advisor.
- The full map goes in `docs/command-map.md`.
- `/preview` produces a fast cheap-tier sketch (ASCII mockup, file tree, rough diff) before any real work.
- `/advisor` is a stronger-model reviewer, also called automatically at plan commit and at goal-done.

**k3 panes keymap** (zellij-style, replacing tuios' modes):
- Typing goes to the pane by default.
- **Ctrl+G** leader, then `p` panes, `t` tabs, `s` sessions, `r` resize, `/` search, `?` help.
- **Esc always returns** to typing.
- The bottom hint bar is always visible.
- Alt+arrows focus, Alt+n opens a new agent pane, Alt+1..9 jumps.
- Ctrl+P is the palette and shows the keys.
- k3code reports pane state (needs input / working / done) to tuios over `set-agent-state`.

## Milestones (each one ends with a commit + push to a branch and a PR into main)
- **M0 Scaffold + spikes (3–4 d).**
  - Repo, subtrees (tuios), vendored files + VENDOR.toml, core skeleton, provider router with a P/S/T chain that includes one entry bypassing OmniRoute, dev install.
  - **Spike:** count the tui_gateway methods/events that the vendored TUI actually uses. If they are unmanageable, switch to the whole-fork fallback and tell the owner.
  - *Exit:* `k3code` finishes a read/edit/bash task through OmniRoute; blocking OmniRoute fails over to the secondary; vendor_check passes; `~/.hermes` is untouched.
- **M1 Daily driver (1–2 wk).**
  - Tools, permissions + Shift+Tab modes, plan mode, agent strip, ↑ history, focus mode, compaction, MCP/mem0/skills, and the core commands (model, effort, compact, clear, exit, stop, resume, rename, fork, branch, export/import, settings, config, output-style, add-dir, permissions, memory, skills, mcp, review, goal).
  - *Exit:* scripted TUI tapes pass for each command; an export→import round trip works; /review finds a seeded bug; /goal reaches done.
- **M2 24/7 reliability (1–2 wk).**
  - systemd units with watchdog, netwatch, persistent retry, journal, governor (PSI, ≤2 IO-heavy agents), doom-loop guard, bwrap sandbox for unattended runs, doctor, stats, debug.
  - *Exit (chaos suite):* bad key → secondary within 30 s; `nmcli networking off` mid-goal pauses, and turning it back on resumes within 10 s; `kill -9` during a bash tool gives INTERRUPTED with no re-run; a 429 with Retry-After parks and resumes; a 72 h soak loses nothing.
- **M3 k3 panes (1–2 wk).**
  - The keymap above, the k3code harness manifest, `/bg --pane`, Inbox approvals.
  - *Exit:* the hint bar is always correct; upstream tuios tests pass; a first-time user can drive panes from the hints alone.
- **M4 Autonomy (2 wk).**
  - scope gate + plan-first auto, fan-out, degradation, ultraplan, ultracode, preview, advisor, ultraresearch, artifacts, schedule, event triggers, loop.
  - *Exit:* a 30-task eval scores ≥80% scope agreement; degradation cuts cost ≥30% at the same pass rate; /preview takes under 30 s.
- **M5 Learning + proactivity (1–2 wk).**
  - Decisions log, distiller, permission-rule proposals, proposal cards with a dismiss latch, project preparation, update-config, curator, optimizer.
  - *Exit:* dismissed proposals never recur; no config changes without acceptance; a bad overlay is auto-reverted.
- **M6 Install + guided setup + update (1 wk).**
  - Release CI (Go binary, TUI dist, pinned Node runtime, core wheel, checksums), `install.sh` (+ `--from-bundle`), the resumable wizard, `/update` with rollback.
  - *Exit:* a fresh Fedora VM is fully set up in under 10 min.
  - *Upstream policy (decided 2026-10-07):* the TUIOS subtree stays mergeable (a dry-run sync reports fewer than 10 conflicting files); the Hermes TUI is a documented **frozen fork** whose upstream fixes are cherry-picked by hand. See [`UPSTREAM.md`](UPSTREAM.md). The exit check verifies that policy and tooling exist and that every mergeable subtree is under the limit.
  - Installing on protected-host-b/protected-host-a **only with the owner's explicit approval** (hard limit).

## Guardrails
- Hard limits are encoded as hardline classes in the sandbox:
  - never print or store secrets;
  - no key rotation;
  - no restarts or edits of protected-host-a/protected-host-b services;
  - no access to other people's data.
- k3code uses its own `K3CODE_HOME`, never reads `~/.hermes/.env`, and has every messaging platform hard-disabled, to avoid a Telegram token collision.
- Back up before touching configs. Never push to main directly.

## Verification
- **CI:** pytest (core), vitest (tui), go test (panes), contract-drift check, vendor_check, banned-import lint.
- **Per milestone:** the exit checks above, run on the laptop. The M2 chaos suite runs as `scripts/chaos/*.sh`.
- **Token check:** after each milestone, compare OmniRoute usage against Claude usage in `/stats` and the OmniRoute insights. The goal is that most build tokens go through OmniRoute.
- **End of each milestone:** a mem0 summary memory with open TODOs.
