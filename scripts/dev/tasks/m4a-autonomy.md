# M4a-autonomy: think ahead, plan first, pick the scope automatically, degrade cheap tasks, `/preview`, `/advisor`

## Current state

- `core/` has:
  - the agent loop;
  - the router (provider chain with model lists such as `models: {default: [...], cheap: ...}`);
  - permissions with modes (`default` / `accept-edits` / `plan` / `auto` / `yolo`, plus an `exit_plan` tool);
  - the gateway (`command.dispatch`, sessions, events);
  - reliability (governor with budgets, usage db);
  - the command registry.
- Read `docs/reports/m1-*.md`, `m2-*.md` and `GOAL.md`.

## Build

### 1. Model tiers and automatic degradation (`core/src/k3code/routing/tiers.py`)

- **Tiers:**

  | Tier | Use |
  |---|---|
  | `main` | default coding |
  | `strong` | planning, review, advisor |
  | `cheap` | summaries, titles, judges, compaction, classification |
  | `fast` | previews, quick classification |

  Each tier maps to a model list per provider in config: `tiers: {main: ..., strong: ..., cheap: ..., fast: ...}`, which falls back to `models.default`.
- **Task kinds:**
  - `interactive_turn`
  - `background_turn`
  - `subagent`
  - `title`
  - `compaction`
  - `goal_judge`
  - `loop_tick`
  - `cron_job`
  - `review`
  - `plan`
  - `preview`
  - `advisor`
  - `research_search`
  - `classification`

  A policy table maps each kind to a tier. Defaults: background, loop and cron go to `cheap`; title, compaction and judge go to `cheap`; plan, review and advisor go to `strong`; preview goes to `fast`; interactive goes to `main`. The table can be overridden in config.
- **Escalation:** when a cheap-tier attempt fails (tool errors repeated, the loop guard fires, or the judge says "not done" twice), escalate to the next tier for that task, and log `routing.escalated`.
- **Fallback:** the existing router fallback chain works within the selected tier.
- Every router call site (loop, compaction, `/goal` judge, titles, …) passes a `task_kind`. `/stats` shows tokens and calls per tier.

### 2. Scope gate plus plan-first auto mode (`core/src/k3code/autonomy/scope.py`, `plan_first.py`)

- **Scope classification.** In `auto` mode, and when `autonomy.plan_first` is true (the default), every new user task is first classified with the `classification` tier.
  - The classifier's input is the prompt, a repo summary (file count, languages, git status) and recent context.
  - Its output is a typed JSON verdict: `scope` (`trivial` / `small` / `medium` / `large` / `huge`), `needs_plan`, `risk` (`low` / `med` / `high`), `parallelizable`, `suggested_subtasks`, and `reason`.
  - It is conservative: if predicted actions touch danger classes (delete, migrate, deploy, credentials, a force push), set `needs_plan=true` and `risk=high`.
- **Acting on the scope:**
  - `trivial` / `small` with low risk: execute directly.
  - `medium` and above, or `needs_plan`: run a **planning turn** first. It uses the `strong` tier in read-only plan mode and produces a structured plan with goal, steps, files, risks, verification and an estimate.
    - In `default` mode, present the plan through the `exit_plan` approval (the existing flow).
    - In `auto` mode, auto-approve plans unless `risk=high`, then execute. Emit a plan event so the TUI shows it.
  - `large` / `huge`: produce the plan with `parallelizable` subtasks marked. The fan-out executor comes in M4b; for now, execute sequentially and record `fanout_candidate=true`.
- **Overrides.** `/scope <level>` overrides the classification for the next task. Persist every verdict to `$K3CODE_HOME/scope_log.jsonl` (prompt hash, verdict, eventual outcome) for later tuning.

### 3. Thinking ahead and proactive notes (a lightweight start; full learning comes in M5)

- After a plan is produced and after a task completes, run a `cheap`-tier "proposer" pass. It returns 0 to 3 suggestions, each with a kind and a text:
  - `consequence`: "this action could lead to…";
  - `also_setup`: "do you want me to also set up…";
  - `improvement`: "you could also improve…".
- Emit them as `proposal.show` events with an id, kind, text and suggested action, and keep them in `$K3CODE_HOME/proposals.jsonl` with a dedup key.
- Add a `/proposals` command that lists them; accept or dismiss by id. A dismissed dedup key never comes back.
- In the TUI, render proposals as a small card list above the agent strip. Keys: `a` accepts, which sends the suggested action as a new prompt; `d` dismisses. This must be minimal; reuse existing components.

### 4. `/preview <task>`

- A fast rough sketch of what the result will look like before any real work. It runs on the `fast` tier with **no write tools** and a 30-second budget.
- Output is chosen by task type: an ASCII mockup for UI tasks, a file tree with one-line purposes for project scaffolding, or a rough unified diff for code changes, plus 3 bullet risks.
- Show it as a normal assistant message tagged `preview`, with a follow-up hint: "run it for real with /go, or adjust".
- `/go` executes the last previewed task with the normal flow (scope gate applies).

### 5. `/advisor [question]`

- Calls the `strong` tier with the current conversation (compacted to a summary when large) and asks for a critical review of the approach, its risks and its next steps. The result is shown as a side message and is NOT added to the main context unless the user accepts it.
- Called automatically, configurable and on by default in `auto` mode:
  - right after a plan is approved, as a brief critique appended to the plan event;
  - before `/goal` declares done. If the advisor finds blocking issues, the goal continues.

## Tests (fake provider; reuse `K3CODE_FAKE_PROVIDER` scripting)

- tier selection per task kind, config override, and escalation on repeated failure;
- scope gate: each scope routes correctly, risky actions force a plan, and `/scope` overrides;
- plan-first in auto mode: the plan runs on the strong tier, is auto-approved at low risk and requires approval at high risk;
- proposer: dedup and dismiss persistence;
- `/preview`: no write tools are available; `/go` executes;
- `/advisor` side message and the auto-invocation hooks.

## Acceptance (put the outputs in REPORT.md)

- `uv run pytest -q` passes, ruff is clean, and `npm run build` succeeds.
- Show `/stats` output with per-tier counts from a fake-provider session that used several kinds.
- **If OmniRoute works** (it returns 400 "daily usage quota" when the key is exhausted), run a live `/preview` for "a CLI todo app in python" with the OmniRoute config, and paste the output and its time, which should be under 30 s. Otherwise state that it was skipped.
