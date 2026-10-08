# M5-learning: learn the user's decisions, propose proactively, prepare projects, self-optimize

## What already exists in `core/`

Read `docs/reports/m1-permissions.md`, `m4a-autonomy.md`, `merge-m4a.md`, `m4c-automation.md` and `m1-commands.md`.

| Component | What it does |
|---|---|
| Permissions | Every approval decision is logged to `$K3CODE_HOME/decisions.jsonl`. Rules come from config. |
| Proposals | `autonomy/proposals.py`: `proposals.jsonl` with a dedup latch, `/proposals`, and TUI cards. |
| `ModelCaller` | Tier-aware one-shot calls. |
| Memory | `K3CODE.md` / `USER.md` loader, plus mem0 client config. |
| Skills | Loader and skill tool. |
| Automations | Suggestions catalog. |
| Usage db | `/stats`. |
| Config IO | Backups and rollback. |

Reference, read-only, MIT: Hermes Agent at `~/.hermes/hermes-agent`:
- `hermes_cli/approvals_suggest.py`
- `agent/background_review.py`
- `agent/curator.py`
- `tools/skill_manager_tool.py`

Port the designs and record the ports in `VENDOR.toml`.

## Build (in `core/src/k3code/learning/`)

### 1. Decision log
Extend `decisions.jsonl` into a general log at `$K3CODE_HOME/learning/decisions.db` (SQLite), migrating the existing jsonl. Record these kinds of decisions:
- approvals (tool, pattern, choice);
- model switches (`/model` from X to Y, plus the reason if given);
- proposal accept/dismiss;
- plan approvals and rejections, including edits made to a plan;
- interrupts and stops (which tool or step was running);
- undo/rollback events;
- `/scope` overrides;
- config changes.

Each entry carries the cwd, a project id (git remote or path hash) and a timestamp.

### 2. Permission learning (port of `approvals_suggest`)
- Mine the decisions. When the same narrow pattern was approved N or more times (default 3) and never denied, propose a rule. Use the arity-aware narrowest pattern, for example: "You always allow `npm test*` in this project → add an allow rule?"
- Repeated denials produce a proposed deny rule.
- An "auto-do in future?" proposal flips that action to auto-allow in `auto` mode only.
- Deliver all of these as proposal cards with kind `permission_rule`. Accepting writes the rule to project or user config, chosen by scope: a rule that occurs in two or more projects goes to user config. Dismissing latches it.
- `/permissions suggest` lists the candidates.

### 3. Preference distiller
- A periodic job (cheap tier; it runs as an automation at daemon idle or once a day) summarizes the decision patterns into `USER.md` under the heading `## Learned preferences (auto)`. Example lines:
  - "avoids model X for Y"
  - "prefers small PRs"
  - "always wants tests run before commit"
  - "rejects plans without verification steps"
- It writes the same summary to `preferences.json`, keyed by confidence and evidence count.
- When `mem0` is configured, it also stores each preference as a mem0 memory (`agent_id` from config).
- It never stores secrets or file contents. It only rewrites the auto section, never the user's own text.
- Model switches feed back into routing. For example, when the user keeps moving away from model M for task kind K, the distiller proposes a `task_tiers` override (as a proposal card).

### 4. Smarter proposals
- Rank proposer suggestions with features from the decision history: kind acceptance rate per project, recency, and similarity to dismissed proposals (token Jaccard over dismissed texts). Drop anything below a threshold.
- The proposer prompt receives the top learned preferences. Tests must show that the acceptance-rate weighting changes the ranking.

### 5. Project preparation on first open
When a session starts in a project with no `.k3code/project.json`:
- Detect the stack: language, package manager, test/lint/build commands, CI, a monorepo, and docker.
- Write `.k3code/project.json`.
- Offer proposals:
  - create a `K3CODE.md` with build/test commands and conventions (drafted on the cheap tier from the repo files);
  - add permission allow rules for the detected safe commands (test, lint, build);
  - suggest automations (for example, nightly tests);
  - flag risks (no tests, no CI, secrets in the repo matched by simple regexes).
- Learned preferences shape these. For example, the user always adds a `Makefile`, so that is offered.
- Nothing is written without acceptance, except `project.json`.

### 6. `/update-config <natural language>`
- Example: `/update-config always use the cheap tier for background jobs and turn focus mode on`.
- The cheap tier translates the request into a JSON patch against the config schema. The patch is validated and shown as a diff, then applied (with a backup) only after confirmation through the clarify request.
- It rejects any attempt to set secrets or hardline-relaxing values.

### 7. Background review and skills curator (port of Hermes `background_review` and `curator`)
- After a session completes, with a meaningful length of more than N turns, a cheap-tier review extracts:
  - durable facts into project memory (`K3CODE.md`, auto section) and mem0;
  - reusable procedures into **skill drafts** at `$K3CODE_HOME/skills/_drafts/<name>/SKILL.md`.
- Drafts appear as proposals: "Save skill X?"
- The curator (an idle-time automation) dedups skills, marks stale ones (unused for 30 days, or failed), and proposes merges.

### 8. Self-optimizer (weekly, opt-in)
- It reads metrics from the usage db and decisions: per-tier failure and escalation rate, approval-prompt rate, loop-guard triggers, proposal acceptance rate and scope-verdict accuracy (compare `scope_log` outcomes).
- It proposes **config overlays**, for example changing a tier mapping, a scope threshold, the proposer threshold or the `max_inline_wait`, as proposal cards with the evidence attached.
- An accepted overlay is applied as an A/B experiment: it is active for N sessions (default 20), then the metrics are compared. It is kept if better, and **rolled back automatically** if worse, with a notification.
- Prompt overlays (system prompt addenda in `$K3CODE_HOME/overlays/`) follow the same A/B and rollback process.
- `/optimizer status|run|rollback <id>`.
- Code self-improvement (opening a PR) stays out of scope for now. Add a stub command `/self-improve` that writes an issue draft to `.k3code/self-improve/*.md`.

### 9. TUI
- Proposal cards already exist. Add kind icons for `permission_rule`, `preference`, `project_setup`, `skill`, `optimizer` and `consequence`/`improvement`.
- Add `/permissions suggest` output formatting.

## Tests (fake provider and fake clock)
- decision log migration; recording each decision kind;
- permission rule mining: thresholds, narrowest pattern, user vs project scope, denial counterexamples, accept writes the rule, dismiss latches;
- distiller: the auto section is rewritten while user text is preserved, the mem0 call is mocked, and no secrets appear;
- proposal ranking changes with acceptance history;
- project prep on a temp repo: detection matrix (python/uv, node/npm, go, rust), proposals generated, only `project.json` written without acceptance;
- `/update-config`: valid patch shows a diff and applies after confirmation; secrets and hardline relaxations are rejected;
- background review: drafts created, then the proposal;
- curator dedup;
- optimizer: an overlay is accepted, the A/B period passes with worse metrics, and the auto-rollback happens.

## Acceptance (put the outputs in REPORT.md)
- `timeout 900 uv run pytest -q -o addopts="" 2>&1 | tail -3` passes, and ruff is clean. The TUI builds, and vitest has only the known failure.
- **Scripted demo** (fake provider, temp home): approve `npm test` 3 times across sessions, show the permission rule proposal, accept it, and show the rule in config. Then dismiss a proposal and show that it never comes back after rerunning the proposer.
