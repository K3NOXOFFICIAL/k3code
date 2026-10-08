# M1-permissions: a real permission engine with modes and approvals (opencode-style rules)

## Bug to fix first

`core/src/k3code/permissions.py::check_permission` returns allowed for **every** tool in interactive `ask` mode, so the gateway's approval callback (`gateway/server.py`, `_approval_callback_for`) is never called. In the TUI, bash and write run without asking.

This was verified with `scripts/e2e_tui_file.py` and `E2E_PROMPT="use the bash tool to run exactly this command: echo bashworks > ran.txt"`. That run reported `approval-prompts-answered=0`.

## Build

1. **Rule engine** in `core/src/k3code/permissions/` (convert the module into a package and keep `check_permission` as a compatibility wrapper).
   - Port the design of **opencode's permission system**. The repo is anomalyco/opencode, MIT.
     - Find the source with `curl https://api.github.com/repos/anomalyco/opencode/git/trees/HEAD?recursive=1` and filter paths containing `permission` or `wildcard`. Read them with `raw.githubusercontent.com`.
     - Key pieces: the permission `index.ts`, `arity.ts` (the bash command-prefix arity table) and the wildcard matcher.
     - Record the upstream commit SHA in a header comment and in `VENDOR.toml` (as a `[[file]]` entry with `modified = true`, `port-to-python`).
   - **Rules:** `{tool: "bash", pattern: "git status*", action: "allow"|"ask"|"deny"}`, plus path patterns for `write`/`edit`/`read`.
   - Evaluation order: deny > ask > allow. The most specific match wins.
   - Bash commands are split on `&&`, `||`, `;` and `|`. **Each** sub-command must be allowed; use the arity table so `git commit -m x` matches `git commit*`.
   - **Rule sources, merged in this order:** built-in defaults, then user `config.yaml` `permissions:`, then project `.k3code/config.yaml`, then session rules. Session rules come from approvals answered with "session". Approvals answered with "always" write to the project config.
   - **Built-in defaults:**
     - Allow: pure tools; `ls`/`cat`/`grep`/`rg`/`git status|diff|log`; reads inside the project and any add-dirs.
     - Ask: writes and edits (in `ask` mode), and any other bash command.
     - Deny: reading or writing outside the project and add-dirs unless approved; the hardline list below.
   - **Hardline deny** (no approval possible; covers the owner's hard limits):
     - `rm -rf /` and `rm -rf ~` variants;
     - `mkfs`;
     - `dd of=/dev/`;
     - `curl … | sh`;
     - commands that print env secrets (`env`, `printenv`, `cat ~/.ssh/*`, `cat *.env`);
     - `ssh protected-host-a`/`ssh protected-host-b` with `systemctl restart|stop` or `docker restart|stop`;
     - `git push --force` to main/master.

     Keep the list in `permissions/hardline.py` and make it extendable from config.
2. **Modes**
   - `default`: ask for edits and non-allowlisted bash.
   - `accept-edits`: edits are allowed, bash still asks.
   - `plan`: read-only. Only pure tools, plus an `exit_plan` tool. The agent must present a plan and call `exit_plan(plan)`; the user approves, and the mode switches to `default` (or `accept-edits` if chosen).
   - `auto`: everything that isn't denied and isn't hardline is allowed. Log every auto-allowed side effect to the session event stream.
   - `yolo`: everything except hardline.

   Wire it up:
   - add `session.mode.cycle` / `session.mode.set` gateway methods;
   - emit `session.info` with the current mode;
   - in the TUI, `Shift+Tab` cycles default → accept-edits → plan → auto, and the status line shows the mode. Check the vendored TUI for an existing mode/yolo indicator and reuse it.
3. **Approval round trip in the gateway**
   - The `approval` request carries the tool, a command/path preview and the choices `once` / `session` / `always` / `deny`.
   - `always` persists a rule. Suggest the narrowest sensible pattern using arity, e.g. `git commit*`, not `git*`.
   - `deny` returns a tool error the model can see ("User denied: …"). An optional free-text reason is appended.
   - **Learning hook (for M5):** log every decision to `$K3CODE_HOME/decisions.jsonl` with session, tool, pattern, choice, cwd and timestamp.
4. **Session cwd**
   - Every tool resolves relative paths against the **session's cwd**, which is the gateway's working directory unless `session.create` passes one. It must never use the cwd of the process that happened to start the core.
   - `/add-dir <path>` adds to the readable/writable roots: add the command to the command registry and persist it per session.

## Tests
- Rule matching table tests: wildcard, arity, chained bash, deny > ask > allow, hardline cannot be overridden even in yolo.
- Every mode × tool category.
- A gateway approval round trip over the protocol using the fake provider: `once`, `session` (no second prompt in the same session), `always` (writes the rule to project config; a new session doesn't ask), and `deny` (the model sees the denial).
- Plan mode: write/bash are refused, `exit_plan` triggers an approval, and the mode then changes.
- Relative paths resolve against the session cwd.

## Acceptance (put the outputs in REPORT.md)
- `cd core && uv run pytest -q` passes, and ruff is clean. `cd tui && npm run build` succeeds.
- Live run: `E2E_TARGET=ran.txt E2E_EXPECT=bashworks E2E_PROMPT="use the bash tool to run exactly this command: echo bashworks > ran.txt" uv run python scripts/e2e_tui_file.py <repo> <tmpwork> <tmphome>` must report `approval-prompts-answered>=1` and `RESULT PASS`. Run it from `core/` with `OMNIROUTE_API_KEY` set.
