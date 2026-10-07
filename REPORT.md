# M4b-fanout — report

## What was built
**Sub-agents** (`core/src/k3code/subagents/`)
- `types.py`: agent types from markdown + frontmatter (`name`, `description`, `tools`, `tier`), loaded from the built-ins (`builtin/{explorer,worker,reviewer,planner}.md`, written from scratch), `$K3CODE_HOME/agents/` and `<project>/.k3code/agents/` (later wins).
- `runner.py`: `SubagentManager` — child `AgentLoop`s with their own reliability bundle (no netwatch probe per child, so parallel children never share the retry wrapper), parent's permission mode + add-dirs, task kind `subagent`, tier = argument > agent type > policy; depth ≤ 2 (`DepthLimit`); emits `subagent.spawn_requested/start/tool/progress/complete`; `Handle` registry; usage rows (`subagent`).
- `worktree.py`: `git worktree add .k3code/worktrees/<id> -b k3/<id>`, commit, diff, trial-merge/commit/abort, conflict-abort leaving the branch; `.k3code/.gitignore` (`*`) so worktrees never dirty the repo.
- `tools.py`: `task(description, prompt, agent_type?, tier?, isolation?, background?)` and `task_result(id, wait?)` (registered at depth < 2; `task`/`task_result` are in `PURE_TOOLS`, so no approval prompt per spawn). `budget.py`: `AgentBudget` (agents/tokens).
- Gateway: `subagent.list/interrupt/tail` RPCs; `/stop` interrupts children.

**/bg + Ctrl+B**: `commands/bg.py`, `prompt.background` RPC (`text` → new background session; no text → hands the running turn to a background session, attaches the client to a fresh one), completion/needs-input/failure notification to the origin session (`notification.show kind=background`, `session.background_done`); TUI `Ctrl+B` in `useInputHandlers.ts` (TODO resolved; resumes the fresh session).

**Fan-out** (`autonomy/fanout.py`): large/huge + `parallelizable` + ≥2 subtasks (classifier's, else the plan's Steps) → worker per subtask in a worktree (cap = `min(autonomy.fanout.max_parallel, GovernorConfig.max_parallel_agents=3)`, IO-heavy ≤ `max_io_heavy`=2) → `reviewer` child per result (strong tier, `VERDICT: pass|fail`) → sequential trial merge, **tests run on the merged result before it is committed** (pytest / `npm test` / `go test` / `cargo test`, or `autonomy.fanout.test_command`) → a review/merge/test failure goes back to that child once (for conflicts the parent state is merged into the child's worktree so it resolves the markers), then escalates to the parent model with the details (branch kept). Events `fanout.plan/progress/done`. Config `autonomy.fanout: {enabled, max_parallel, require_tests, test_command, test_timeout}`. Skipped outside a git repo.

**/ultraplan** (`autonomy/ultra.py`): 3 planner children (MVP-first, risk-first, architecture-first; strong tier) → judge on the strong tier (scores + synthesized plan; falls back to the best-scored plan if the judge gives no structure) → `plan.show` event + `.k3code/plans/<ts>-<slug>.md` + artifact; `/go` then approves it and the next turn fans out.
**/ultracode**: ultraplan → fan-out → adversarial panel (2 reviewer children: correctness, security/robustness) → cross-vote on the merged candidates by both lenses (a finding is accepted only if **both** vote real) → one worker fixes the confirmed ones (worktree, merged) → final tests → report (`.k3code/reports/`). Budget guard `ultracode.max_tokens` (2M) / `max_agents` (12): `BudgetStop` ends the run with a STOPPED section. Live progress: `ultra.progress` + the children in the strip.
**/ultraresearch** (`research/`): planner → parallel search (cheap) → parallel fetch+extract (cheap) → cross-check (cheap) → synthesis (strong) → programmatic citation check: unknown `[Sn]` ids are dropped and `## Sources` is rebuilt from the source registry, so numbers/titles/URLs always match. Tools: MCP search/fetch tools when connected (k3nox `hub_searxng`/`hub_fetch`), else built-in `web_search` (SearXNG, `research.searxng_url`, default `https://<searxng-host>`, probed once; disabled with a clear message when unreachable) and `web_fetch` (httpx + readability-lite); both are also registered as agent tools. Report → `.k3code/research/<ts>-<slug>.md`. Prompts/state adapted from Eigenwise/atomic-agents deep-research (MIT; no `instructor`).
**/artifacts** (`artifacts.py`, `commands/artifacts_cmd.py`): SQLite `artifacts.db`; list / `open <id>` (prints path, mentions `$EDITOR`) / `publish` (stub + TODO). Producers: ultraplan, ultraresearch, ultracode report, `/preview`, `/review`, `/debug dump`, `/export`.
**TUI/contract**: event types + handlers for `fanout.*`, `ultra.progress`, `research.progress`; `docs/tui-contract.md` section; `plan.show` note updated.

## Verification (exact commands)
- `cd core && uv run pytest -q -o addopts=""` → `433 passed` (was 386; +47: `test_subagents` 10, `test_artifacts` 4, `test_bg` 5, `test_fanout` 11, `test_ultra` 9, `test_research` 8). `uv run ruff check src tests scripts` → All checks passed. `python3 scripts/vendor_check.py` → All checks passed.
- `cd tui && npm ci && npm run build:ink && npm run build && npx tsc --noEmit && npx vitest run` → build OK, tsc clean, `Test Files 1 failed | 174 passed`, `Tests 1 failed | 1589 passed | 2 skipped` — the only failure is the known `textInputFastEcho`.
- Fake-provider demo: `cd core && uv run python scripts/demo_ultracode.py` (`/ultracode` on a temp git repo, 3 parallel subtasks):

Event log summary:
```
ultra.progress   planning                   3 independent planners  [0/12 agents]
ultra.progress   judging                    3 plans  [3/12 agents]
ultra.progress   implementing                 [3/12 agents]
fanout.plan      3 subtasks, max_parallel=3, tests=true
fanout.progress  t1 merged    (1/3) STEP-ONE add f1 (parallel)
fanout.progress  t3 merged    (2/3) STEP-THREE add f3 (parallel)
fanout.progress  t2 merged    (3/3) STEP-TWO add f2 (parallel)
fanout.done      merged 3/3, tests pass
ultra.progress   adversarial review         correctness, security/robustness  [9/12 agents]
ultra.progress   cross-checking findings    2 candidates  [11/12 agents]
ultra.progress   fixing                     1 confirmed findings  [11/12 agents]
ultra.progress   final tests                true  [12/12 agents]
```
Event counts:
```
fanout.done                 1
fanout.plan                 1
fanout.progress             15
message.complete            1
message.delta               1
message.start               1
plan.show                   1
subagent.complete           12
subagent.spawn_requested    12
subagent.start              12
subagent.tool               4
ultra.progress              7
```
`git log --graph --oneline` of the demo repo:
```
*   b153bee Merge k3/sa-5f4e365a
|\  
| * a08f0d3 k3code sub-agent sa-5f4e365a: fix findings
|/  
*   21c3ec1 Merge k3/sa-835ecaeb
|\  
| * 675f50d k3code sub-agent sa-835ecaeb: STEP-TWO add f2 (parallel)
* |   05a4635 Merge k3/sa-ded6fbcb
|\ \  
| * | 5d55f5a k3code sub-agent sa-ded6fbcb: STEP-THREE add f3 (parallel)
| |/  
* |   90fb789 Merge k3/sa-70a06ee1
|\ \  
| |/  
|/|   
| * 0a13845 k3code sub-agent sa-70a06ee1: STEP-ONE add f1 (parallel)
|/  
* e395a0c init
```
Final report line: `Budget used: 12/12 agents, 450/2000000 tokens`; tests passed after each merge (`true` as the test command).
- **Live `/ultraresearch`: SKIPPED.** OmniRoute answers HTTP 429 `usage_limit_exceeded` (daily quota 100%, resets 2026-10-08T03:00Z) and `https://<searxng-host>` is not reachable from this machine. Covered by `tests/test_research.py` (fake tools + scripted models, plus respx-mocked SearXNG and pages).

## Deviations
- Children are in-process `AgentLoop`s tracked by a `SubagentManager`, not persisted gateway sessions (they appear in the strip via `subagent.*` events, and `subagent.list/tail/interrupt`). They have no stored transcript of their own.
- Pi `subagent` agent prompts were **not** used (the four built-in agent types are original), so there is no pi entry in `VENDOR.toml`. VENDOR entries added: Hermes `subagent_worktree.py` (worktree.py) and `kanban_swarm.py` (fanout.py) as design ports, atomic-agents `state.py` / planner prompts / `main.py` (research). The `sha256_upstream` of adapted files is the upstream digest (`modified = true`).
- Tests after a merge run on the *uncommitted* merge result (`merge --no-commit`) and a failing result is aborted, rather than committing and reverting; same effect, no polluted history.
- `/ultracode` cross-checks findings with two strong-tier model calls (one per lens) rather than two more child agents, to stay inside the default 12-agent budget (3 planners + 3 workers + 3 reviewers + 2 panel + 1 fixer = 12).
- Reviewer verdict without a `VERDICT:` line counts as pass. Children inherit the parent's approval callback; in `default` mode their approvals reach the user's client.
- `/artifacts open` prints the path and the `$EDITOR` command; it does not spawn the editor (the gateway has no terminal).
- Commit trailer is `Claude Sonnet 5.5` (harness attribution), as in earlier reports.

## Open TODOs
- `/artifacts publish` is a stub (TODO(M6)).
- Re-run a live `/ultraresearch` with 3 sub-questions after the quota reset (needs a reachable SearXNG or an MCP search tool).
- Fan-out merges in completion order, not plan order; a smarter dependency order would reduce rework.
- No TUI-side vitest for Ctrl+B or the new event handlers (gateway-level tests only, as specified).
- Child transcripts are not stored; consider persisting them under the parent session for `/resume`.
