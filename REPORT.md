# M1-commands — report (2026-10-07)

All 12 remaining M1 commands are implemented as one module each in `core/src/k3code/commands/`, reachable through
`slash.exec` / `command.dispatch`, plus CLI `k3code export|import|memory|config-edit`.

## What was built

| Area | Files |
|---|---|
| Foundations | `paths.py` (call-time `K3CODE_HOME`), `confio.py` (YAML io, `config.yaml.bak-<ts>` backups, validation via `Settings` + reliability + permissions schemas), `config.py` (new sections `mcp skills mem0 display goal output_style permissions`), `prompting.py` (system prompt = base + output style + memory + skills index + deferred MCP names; built **per turn**) |
| /export /import | `redact.py`, `bundle.py`, `commands/export.py`, `commands/import_.py`, `SessionStore.insert/new_id`, CLI in `cli.py` |
| /fork /branch | `commands/fork.py`, `commands/branch.py` |
| /settings | `commands/settings_cmd.py` (structured `type:"settings"` result); TUI: `shared/slash.ts` + `createSlashHandler.ts` show it in the pager overlay |
| /config | `commands/config_cmd.py` (get/set/edit/path/rollback, `--project`) |
| /output-style | `outputstyle.py`, `commands/output_style.py` (session meta + `--default` to config) |
| /memory | `memory.py` (loader: user USER.md → project K3CODE.md/AGENTS.md), `commands/memory_cmd.py` (list/add/edit/mem0) |
| /skills + `skill` tool | `skills.py`, `extratools.py`, `commands/skills_cmd.py` |
| /mcp | `mcpclient.py` (stdio + streamable HTTP, one task per server), `extratools.py` (`mcp__<server>__<tool>`, deferred schemas, `mcp_tool_search`), `ToolRegistry.deferred/activate`, `commands/mcp_cmd.py` |
| /review | `commands/review.py`, `prompts/review_rubric.md` (adapted from openai/codex, Apache-2.0), `LICENSES/codex-Apache-2.0.txt`, `NOTICE`, `VENDOR.toml` |
| /goal | `goals.py` (port of Hermes `goals.py`, recorded in `VENDOR.toml` as `port = true`), `commands/goal.py`, server goal loop + `session.control.update` |
| Gateway services | `GatewayServer.clarify / apply_file_config / activate_session / oneshot / goal_manager / emit_goal` |
| Tests | `tests/test_cmd_bundle.py test_cmd_config.py test_cmd_git.py test_cmd_mcp.py test_cmd_review_goal.py` + helpers + `fake_mcp_server.py`; vitest case for `/settings` |

## Verification

```
cd core && uv run pytest -o addopts="" -q     → 285 passed in 12s  
cd core && uv run ruff check src tests        → All checks passed!
cd tui  && npm run build                      → built dist/entry.js
cd tui  && npm run typecheck                  → exit 0
cd tui  && npm run build:ink && npx vitest run → 1590 passed, 1 failed (textInputFastEcho › colorizeEcho: terminal-colour
                                                env test, unrelated to this task), new /settings test passes
python3 scripts/vendor_check.py               → All checks passed
```

### Acceptance: export / import round trip with temp `K3CODE_HOME`

Temp config contained fake secrets (`sk-FAKEKEY…`, `tok_FAKETOKEN…`, `ghp_FAKEGITHUBTOKEN…`, `hunter2-FAKEPASS`) and
one session whose message text also contained the fake key.

```
$ k3code export --all /tmp/x.k3bundle
Exported 1 session(s), 1 settings file(s) to /tmp/x.k3bundle (secrets redacted).
$ tar tzf /tmp/x.k3bundle
manifest.json  settings/user.config.yaml  sessions/9f717b4dd8654234.json
$ for pat in FAKEKEY FAKETOKEN FAKEGITHUBTOKEN FAKEPASS; do tar xzOf /tmp/x.k3bundle | grep -c $pat; done
0  0  0  0
(settings: api_key/token/GITHUB_TOKEN/extra_password → "<redacted>"; api_key_env, base_url, max_tokens kept)

$ K3CODE_HOME=<fresh dir with max_turns: 7> k3code import /tmp/x.k3bundle --yes
k3bundle v1 … settings: user / sessions: 1
Imported.
settings written: …/config.yaml
backup: …/config.yaml.bak-20261007T115822041539
session 9f717b4dd8654234 imported
$ grep -c "<redacted>" imported-config.yaml  → 0      $ grep -c FAKE imported-config.yaml → 0
```
Imported config kept `max_turns: 7` and gained the non-secret settings; `<redacted>` values were dropped, never written.

## Deviations / decisions

- **Slash plumbing fixes (needed for any of this to show in the TUI):** the TUI sends `slash.exec` with the command
  *without* the leading `/` and reads `output`; the gateway previously treated that as plain text. `slash.exec` now
  accepts both forms, and every `message` result carries `output`. The old "plain text passthrough" test was replaced.
  `command.dispatch` still returns `type:"message"` (the TUI's `command.dispatch` fallback only knows exec/send/…,
  but slash.exec — the primary path — is fine).
- `load_config` read an import-time `K3CODE_HOME`; it now reads the env at call time (needed for tests/temp homes).
- `--check` gate runs **after** the judge says `done` (spec: "must pass before done counts"), unlike Hermes which runs
  gates before judging every turn. Failures feed back a continuation; > `max_retries` (3) pauses the goal.
- `/goal <objective>` returns `type:"send"` (the TUI submits the kick prompt); the loop lives in `GatewayServer._run_turn`
  (iterative, interrupt/error pauses the goal). Judge model = `goal.judge_model` (default `cheap`, falls back to default).
- Session JSON in bundles gets credential-shaped substrings scrubbed (known prefixes/JWT/Bearer), not generic text.
- `mcp` 2.x API differs from 1.x (snake_case fields, `streamable_http_client`, `httpx2`); the client handles both field
  spellings. Fake test server uses `mcp.server.mcpserver.MCPServer`.
- `/settings` overlay reuses the **pager** overlay (simple text list) rather than a new component.
- mem0 search assumes `POST {mem0.url}/search` with `{query, limit, user_id?}` and `Authorization: Token <key>`.
- Large skill libraries: the prompt lists at most 60 skills (names + ≤110-char descriptions); the rest via `skill {query}`.

## Open TODOs

- CLI headless/REPL paths get memory/skills/output-style in the prompt and the `skill` tool, but **not MCP** (gateway only).
- `/config set` rewrites YAML via `yaml.safe_dump` (comments in the file are lost; a `.bak` is always kept).
- `/fork --activate`/`/branch --activate` swap the gateway's live session (`session.info` emitted) but the TUI has no
  dedicated "session switched" event; the user should `/resume <id>` in the TUI to view it.
- mem0 endpoint shape unverified against the real k3nox mem0 server.
- `textInputFastEcho` vitest failure is pre-existing/environmental.
