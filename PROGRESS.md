# k3code — PROGRESS

## Done (M0-core)

- `core/` uv project (Python 3.12+, src layout, package `k3code`).
- `config.py`: layered settings (defaults < user `~/.k3code/config.yaml` <
  project `.k3code/config.yaml` < env `K3CODE_*` < CLI flags), `ProviderEntry`
  pydantic model with `api_key_env` -> `api_key` expansion.
- `providers/`: normalized `Message`/`ToolSpec`/`StreamEvent` types, `Provider`
  base, `OpenAICompatProvider` (SSE streaming), `AnthropicProvider`.
- `router/`: fallback-chain walk over (provider, model) entries — retry with
  jittered backoff for network/server/rate_limit, immediate failover for
  auth/quota/bad_request, terminal `ContextOverflow`, `AllProvidersUnreachable`
  / `ChainExhausted` when the chain is spent. Vendored-and-adapted from
  hermes-agent (`classifier.py`, `cooldown.py`; see `VENDOR.toml`).
- `tools/`: registry (`read`/`write`/`edit`/`bash`/`grep`/`glob`/`todo`),
  fuzzy find-and-replace for `edit` vendored from hermes-agent
  (`tools/fuzzy_match.py`).
- `agent/loop.py`: turn loop wiring router + tool registry + permissions.
- `permissions.py`: ask/auto-edit/yolo modes, headless-aware checks.
- `cli.py`: `-p` headless mode, bare REPL, `--permission`/`--json`/
  `--config-dir`, per-provider model-key resolution via `default_model`.
- `VENDOR.toml` + `scripts/vendor_check.py` + `LICENSES/hermes-agent-MIT.txt`.
- 43 tests (pytest + respx), all green; `ruff check .` clean;
  `scripts/vendor_check.py` passes (expected SHA256 WARNs — vendored copies
  carry an added attribution header so they never byte-match the pristine
  upstream file; this is by design in the script).
- Live smoke test against OmniRoute (see REPORT.md for the exact result).

## Bugs found and fixed this session (none were in the original test suite)

1. `ProviderEntry` was missing a declared `api_key` field even though
   `load_config()` injects one and `providers/__init__.py` reads
   `entry.api_key` — would have raised `AttributeError` at runtime.
2. `load_config()` used `p.pop("api_key_env", ...)`, destructively removing a
   *required* field before `Settings(**merged)` validated it — raised a
   `pydantic.ValidationError` for any real config with providers.
3. `click` (used by `cli.py`) was never declared in `pyproject.toml`.
4. `cli.py` passed each provider's whole `models` dict straight into
   `build_chain()` instead of resolving `config.default_model` to a key first
   — `default_model` was dead config and the chain would have used the dict's
   *keys* ("default", "cheap") as literal model ids.
5. **Agent loop never executed tool calls against a real provider.**
   `AgentLoop.run()` only collected tool calls from `StreamEvent(type="tool_call")`
   events emitted *during* streaming, but neither real provider
   (`openai_compat.py`, `anthropic.py`) ever emits that event type — both only
   attach the fully-parsed list to the final `done` message. The existing
   `test_agent.py` fakes always emitted both, masking the bug. Fixed by making
   `final_message.tool_calls` the authoritative source; added a regression
   test (`test_agent_loop_tool_calls_only_on_final_message`) that mimics the
   real providers' contract (done-message-only, no live `tool_call` events).

## Next (not started)

- M1+: todo persistence, richer permission prompting for the interactive REPL,
  context-compaction on `ContextOverflow`, structured event schemas.
