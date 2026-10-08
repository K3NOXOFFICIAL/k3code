# M4b-fanout: `/bg`, automatic fan-out of sub-agents, `/ultraplan`, `/ultracode`, `/ultraresearch`, `/artifacts`

## Current state

`core/` has:
- a multi-session gateway with background sessions and a daemon;
- tiers and routing (`routing/`), with task kinds and escalation;
- the scope gate plus plan-first (`autonomy/`). Plans can mark `parallelizable` subtasks, and `large`/`huge` verdicts set `fanout_candidate`;
- the governor (PSI and concurrency caps), the journal, the sandbox, permissions and the command registry;
- the TUI agent strip, which shows sessions and sub-agents with states.

Read `docs/reports/m4a-autonomy.md`, `docs/reports/m2-ops.md` and `docs/reports/merge-m4a.md`.

References, read-only: Hermes (MIT, `/home/user/.hermes/hermes-agent`): `tools/delegate_tool*.py`, `tools/subagent_worktree.py`, `hermes_cli/kanban_swarm.py`, `kanban_decompose.py`.

Port the designs, not the code wholesale. Record ports in `VENDOR.toml`.

## Build

1. **`/bg [prompt]`.** Start a background session with the prompt, or send the current foreground turn to the background if one is running (the gateway's `prompt.background`). The session appears in the strip and notifies on completion or when it needs input. `Ctrl+B` in the TUI does the same; see the TODO in `useInputHandlers.ts`.
2. **`task` tool (sub-agents).** The model can call `task(description, prompt, agent_type?, tier?, isolation?: "none"|"worktree", background?: bool)`.
   - It spawns a child session: depth ≤ 2, inheriting cwd, permissions mode and add-dirs, with the `subagent` task kind and the cheap/main tier by policy.
   - It returns the child's final answer, or, with `background=true`, a handle the parent can poll with `task_result(id)`.
   - Children with `isolation=worktree` get their own `git worktree` under `.k3code/worktrees/<id>` and branch `k3/<id>`. Their diff is returned to the parent, which merges it, or leaves it as a branch when there is a conflict.
   - Sub-agent events (`subagent.start`, `progress`, `tool`, `complete`) are emitted so that the TUI strip and agents overlay show them; the contract already has these events.
   - Agent types are markdown files with frontmatter (`name`, `description`, `tools`, `tier`) in `$K3CODE_HOME/agents/` and `.k3code/agents/`. Ship built-ins: `explorer` (read-only), `worker`, `reviewer` and `planner`. You may adapt prompts from earendil-works/pi `subagent` agents (MIT; fetch with curl from raw.githubusercontent.com and record in VENDOR.toml); otherwise write them yourself.
3. **Automatic fan-out.** When the scope gate says `large`/`huge` and the plan has parallelizable subtasks, the executor runs them as parallel `task` children in worktrees:
   - the number of children is capped by the governor (`max_parallel_agents`, default 3; IO-heavy work at most 2);
   - each child gets one subtask plus shared context (the plan and the relevant files);
   - a `reviewer` child checks each result against its acceptance criteria;
   - the parent merges sequentially and runs the project's tests (detect them: pytest, npm test, go test, cargo test) after each merge;
   - a failing merge or test is sent back to that child once with the error, then escalated to the parent.

   Emit `fanout.plan`, `fanout.progress` and `fanout.done` events; the TUI strip shows the children. Configure with `autonomy.fanout: {enabled, max_parallel, require_tests}`.
4. **`/ultraplan <task>`.** Deep planning on the strong tier:
   - 2–3 independent planner children produce alternative plans from different angles (MVP-first, risk-first, architecture-first);
   - a judge (strong tier) scores them and synthesizes one final plan, grafting the best ideas from the others;
   - the result is shown as a plan event and written to `.k3code/plans/<ts>-<slug>.md`;
   - it offers `/go` to execute it with fan-out.
5. **`/ultracode <task>`.** Full orchestration with plan, fan-out, review and verification:
   - run `/ultraplan`;
   - fan out implementation;
   - run an adversarial review pass (2 reviewer children with different lenses: correctness and security/robustness), and accept a finding only if both agree it is real;
   - fix the confirmed findings, then run the final tests and produce a summary report.

   Budget guard: `ultracode.max_tokens` (default 2M) and `max_agents` (default 12). Show live progress in the strip.
6. **`/ultraresearch <question>`.** Multi-source research producing a cited report:
   - adapt the flow from Eigenwise/atomic-agents `atomic-examples/deep-research` (MIT; fetch its prompts and state design with curl and record the port in VENDOR.toml). Do not add `instructor` as a dependency;
   - search tools come from the configured MCP servers, e.g. a k3nox `hub_searxng` web search or a fetch tool when present, or else a built-in `web_fetch` tool (httpx + readability-lite text extraction) plus `web_search` configured as a SearXNG URL in config (`research.searxng_url`, default `https://<searxng-host>` if reachable, otherwise disabled with a clear message);
   - flow: decompose into sub-questions, search in parallel (cheap tier), read and extract (cheap), cross-check claims, then synthesize (strong) with numbered citations;
   - write the report to `.k3code/research/<ts>-<slug>.md`.
7. **`/artifacts`.** List the files that sessions produced (plans, research reports, previews, review reports, debug dumps, exported bundles) from an `artifacts` table. Producers register their outputs there.
   - `/artifacts open <id>` prints the path, or uses `$EDITOR` in the CLI.
   - `/artifacts publish <id>` stays a stub with a TODO.

## Tests (fake provider with scripted children)
- the `task` tool: sync and background; the depth limit; worktree isolation returns a diff and leaves a branch on conflict;
- the fan-out executor: parallelism within the cap; the reviewer gate; tests run after each merge; a failure is retried once and then escalated;
- `/ultraplan`: three plans, then the judge, then a synthesized file;
- `/ultracode`: the review panel accepts a finding only when both reviewers agree; the budget stops it;
- `/ultraresearch`: fake search/fetch tools produce a report with citations that match the sources;
- `/bg` and Ctrl+B handoff at the gateway level; `/artifacts` registry.

## Acceptance (put the outputs in REPORT.md)
- `uv run pytest -q -o addopts=""` passes and ruff is clean. `npm run build` succeeds, and so does `vitest` (only the known failure is allowed).
- A fake-provider demo of `/ultracode` on a temp git repo with 3 parallel subtasks: paste the event log summary and the final `git log --graph --oneline`.
- If OmniRoute works (no quota error), run one live `/ultraresearch` with 3 sub-questions. Otherwise report it as skipped.
