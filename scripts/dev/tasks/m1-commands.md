# M1-commands: the remaining M1 slash commands, plus sessions and settings

## Current state (read first)

- The gateway (`core/src/k3code/gateway/`) and the command registry (`core/src/k3code/commands/`, one module per command) exist.
- Already implemented: `/model`, `/effort`, `/clear`, `/compact`, `/rename`, `/resume`, `/stop`, `/exit`, `/help`, `/add-dir`, `/focus` (TUI).
- Sessions are stored in SQLite (`gateway/sessions.py`).
- Permissions live in `core/src/k3code/permissions/`.
- The TUI is in `tui/`.
- Reports to read: `docs/reports/m1-gateway.md`, `docs/reports/m1-permissions.md`, `docs/reports/m1-tui.md`.

## Implement

Each command is one module in the registry, with help text, and works via the gateway's `command.dispatch`. The TUI already sends slash commands there.

- **`/export [path]`**
  - Writes a `.k3bundle` (a tar.gz) containing:
    - `manifest.json` (version, created, contents);
    - `settings/` (user config plus project `.k3code/config.yaml`, **with secrets redacted**: any key matching `key|token|secret|password`, and any value that looks like a key, become `"<redacted>"`; `api_key_env` names are kept);
    - `sessions/<id>.json` (the current session by default; `--all` exports all of them; `--session ID` exports one).
  - Flags `--settings-only` and `--session-only`.
  - Also add CLI `k3code export …`.
- **`/import <path>`**
  - Reads a bundle, shows what it contains, and asks for confirmation through the `clarify` server request. In headless mode use `--yes`.
  - Merges settings, backing up the existing config to `config.yaml.bak-<ts>` first. It never overwrites secrets with `<redacted>`.
  - Imports sessions under new ids if they collide.
  - Also add CLI `k3code import`.
- **`/fork [title]`**
  - Copies the current session (messages, cwd, model, mode) into a new session, and activates it if requested.
- **`/branch [name]`**
  - Creates a git branch in the session cwd (`git switch -c`) **and** forks the session, linking the two.
  - With `--worktree`, creates a git worktree under `.k3code/worktrees/<name>` and moves the forked session's cwd there.
  - Refuses with a clear message if the cwd isn't a git repo.
- **`/settings`**
  - Returns a structured view: model chain, effort, permission mode and rules count, focus mode, theme, output style, providers (without keys), and paths.
  - In the TUI, show it as an overlay. Reuse an existing overlay component; a simple list is fine.
- **`/config get|set|edit|path|rollback`**
  - `set` validates against the pydantic config, writes to user config (`--project` for the project config), and keeps backups.
  - `rollback` restores the latest backup.
  - Read the reliability and permissions config schemas too.
- **`/output-style [name]`**
  - Presets: `default`, `concise`, `explanatory`, `learning`, plus custom ones from `$K3CODE_HOME/output-styles/*.md` and the project `.k3code/output-styles/*.md`.
  - The style text is appended to the system prompt. Persist the choice per session, with a default in config.
- **`/memory`**
  - Project memory is `K3CODE.md` in the repo root, with `AGENTS.md` as a fallback. User memory is `$K3CODE_HOME/memory/USER.md`.
  - Both are loaded into the system prompt; write the loader if none exists.
  - Subcommands:
    - `/memory` lists the files and their sizes;
    - `/memory add <text>` appends to project memory, or to user memory with `--user`;
    - `/memory edit` opens `$EDITOR` in the CLI, and in the TUI returns the path;
    - `/memory mem0 <query>` searches mem0 if configured (`mem0.url` + `api_key_env`); otherwise it says that mem0 isn't configured.
- **`/skills`**
  - Skills are directories containing a `SKILL.md` with frontmatter (`name`, `description`), searched in `$K3CODE_HOME/skills`, `.k3code/skills`, and any extra roots from config (`skills.roots`, e.g. `~/.local/share/k3nox/skills-library`).
  - `/skills` lists them. `/skills show <name>` shows the full text.
  - Add a `skill` tool the model can call to load a skill's full text on demand. Put only the names and descriptions in the system prompt.
- **`/mcp`**
  - Add an MCP client (the `mcp` Python SDK as a dependency) for stdio and streamable-HTTP servers declared in config under `mcp.servers`.
  - Their tools are registered as `mcp__<server>__<tool>`.
  - **Deferred loading:** only the names go into the prompt, plus an `mcp_tool_search` tool that returns full schemas on demand. Keep context small.
  - Subcommands: `/mcp` lists servers with status and tool counts; `/mcp reload`.
  - Permissions: MCP tools default to `ask`, matched by rules via the tool name.
- **`/review [target]`**
  - Reviews the git diff (staged, unstaged, or `<base>..HEAD`), or a path, using a review prompt adapted from openai/codex `codex-rs/prompts/templates/review/rubric.md` (Apache-2.0).
    - Fetch it with curl from raw.githubusercontent.com. If the path moved, search the tree API.
    - Keep its license notice: add `LICENSES/codex-Apache-2.0.txt`, a NOTICE, and a `VENDOR.toml` entry.
  - Output is a list of findings: severity, file:line, issue, suggestion.
  - It runs as a sub-turn using the main model.
- **`/goal <objective>` | `/goal status|pause|resume|clear`**
  - Port the design of Hermes' `hermes_cli/goals.py` (MIT, read-only at `~/.hermes/hermes-agent`):
    - a persistent `GoalState` per session;
    - after each turn, a cheap-model judge decides `continue` or `done`;
    - while the verdict is `continue`, a continuation user message is sent automatically;
    - a turn budget (default 30) is the backstop;
    - an optional `--check "<shell cmd>"` gate must pass before `done` counts.
  - Emit `session.control.update` so the TUI goal bar shows it.
  - Record the port in `VENDOR.toml` as `port`.

## Tests
- Unit plus gateway-level tests for each command.
- Export/import round-trip: settings redaction keeps no secret strings, sessions are re-imported, and the config backup exists.
- Fork/branch: use a temp git repo.
- Config set/rollback.
- Output-style is applied in the system prompt.
- Memory loader order.
- The skill tool.
- MCP: an in-process fake stdio MCP server (a small Python script) for listing tools and calls, and deferred search.
- Review: the fake provider returns structured findings.
- Goal loop: the fake judge says continue twice and then done; the turn budget stops it; the `--check` gate is enforced.

## Acceptance (put the outputs in REPORT.md)
- `uv run pytest -q` passes, ruff is clean, and `npm run build` succeeds.
- Run `k3code export --all /tmp/x.k3bundle` and then `k3code import /tmp/x.k3bundle --yes` with a temp `K3CODE_HOME`. Paste the outputs and a `grep -c` showing no secrets in the bundle.
