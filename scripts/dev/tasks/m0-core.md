# M0-core: k3code Python core skeleton (provider router + agent loop + tools + CLI)

Build the first working slice of `core/`: a Python package `k3code` (uv project at `core/`, `requires-python >=3.12`, src layout `core/src/k3code/`). The dev machine has system Python 3.14 and `uv`.

Reference checkout, read-only: Hermes Agent at `/home/user/.hermes/hermes-agent`, MIT, commit 4127d78da84b1eee105f298979cc57cc7457f98d.

## Build these modules

1. **`config.py`**
   - `K3CODE_HOME` (default `~/.k3code`) and `config.yaml` with pydantic models.
   - Settings precedence: CLI flag > env > project `.k3code/config.yaml` > user config > defaults.
   - Provider chain schema, for example:
     ```yaml
     providers:
       - {name: omniroute, kind: openai, base_url: https://<omniroute-public-host>/v1, api_key_env: OMNIROUTE_API_KEY, models: {default: auto/coding, cheap: auto/coding-cheap}}
       - {name: secondary, kind: openai, base_url: ..., api_key_env: ..., models: {...}}
       - {name: tertiary, kind: anthropic, base_url: https://api.anthropic.com, api_key_env: ANTHROPIC_API_KEY, models: {...}}
     ```
     The order is primary, secondary, tertiary. Any number of entries is allowed. `kind` is `openai` (chat-completions compatible) or `anthropic` (messages API).
   - Per-entry model fallbacks: `models.default` can be a list. Within a provider, try models in order before moving to the next provider.

2. **`providers/`**
   - `openai_compat.py` and `anthropic.py`. Both use async `httpx` with streaming and tool calling, normalized to one internal message/tool-call format in `providers/types.py`.
   - Never log keys.

3. **`router.py`, the fallback chain.**
   - Walk provider/model entries in order.
   - Classify errors into `FailoverReason`: rate_limit, quota, auth, context_overflow, network, server, timeout, bad_request, unknown. Adapt the enum and the pattern tables from Hermes `agent/error_classifier.py`, vendoring only the tables and enum.
   - Use jittered backoff by vendoring Hermes `agent/retry_utils.py` as-is.
   - Put a failing entry into cooldown, adapting the design of Hermes `agent/fallback_cooldown.py`. Honour Retry-After.
   - Normalize the chain config by adapting Hermes `hermes_cli/fallback_config.py`.
   - Rules:
     - network, timeout, server, rate_limit: retry the same entry with backoff (max N), then fail over.
     - auth or quota: fail over immediately.
     - context_overflow: raise `ContextOverflow`, which the loop will compact later.
     - bad_request: fail.
   - Emit structured events (`router.attempt`, `router.failover`, `router.exhausted`) through a callback.
   - When every entry fails with network errors, raise `AllProvidersUnreachable`. The M2 offline-pause layer will catch it.

4. **`tools/`**
   - A registry with JSON-schema tool specs.
   - Tools: `read` (line ranges), `write`, `edit` (exact string replace, unique match or `replace_all`), `bash` (timeout, cwd, captured output with truncation, process group kill), `grep` (ripgrep if present, else Python), `glob`, `todo`.
   - For fuzzy edit matching you may vendor Hermes `tools/fuzzy_match.py` if its imports are self-contained. Check first; otherwise write your own.
   - Every tool declares `side_effect: bool`. read, grep, glob and todo are pure.
   - Keep tool sizes reasonable: truncate large outputs and say so.

5. **`agent/loop.py`**
   - The agent loop: system prompt plus messages, then call the router, execute tool calls (sequential is fine for M0), and repeat until no tool calls or `max_turns`.
   - Write a concise original system prompt in `prompts/system.md`. Do not copy any Claude Code text.
   - Stream text deltas to a callback.

6. **`permissions.py` (M0 minimal)**
   - Modes: `ask` (default for interactive), `auto-edit` (edits allowed, bash asks), `yolo`.
   - In headless `-p` mode use a `--permission` flag; `ask` in headless denies side-effect tools with a clear message.

7. **`cli.py`**
   - Entry point `k3code`:
     - `k3code -p "prompt" [--model M] [--permission yolo|auto-edit|ask] [--json]` runs headless and prints the final answer.
     - `k3code` with no args starts a minimal line REPL (prompt_toolkit or plain input) that streams answers and supports `/exit` and `/model`. The real TUI comes later.
   - `k3code providers` lists the chain and whether each entry has a key. Never print the key.

8. **`VENDOR.toml`** at the repo root.
   - One `[[file]]` entry per vendored file: `project`, `upstream_path`, `commit`, `license`, `local_path`, `sha256_upstream`, `modified` (bool).
   - Add `scripts/vendor_check.py`. It verifies every entry's local file exists, the license is in {MIT, Apache-2.0} and no entry names a banned project (Open-ClaudeCode, claude-code leak, ante binary). Exit non-zero on failure.
   - Create `LICENSES/hermes-agent-MIT.txt` with Hermes' LICENSE text.

## Tests (pytest, in `core/tests/`), using `respx` or httpx MockTransport

- Primary returns 500 three times: the router retries, fails over to the secondary, the secondary succeeds, and the events are recorded.
- Primary returns 401: immediate failover with no retries.
- A 429 with Retry-After: cooldown is respected.
- Every entry raises ConnectError: `AllProvidersUnreachable`.
- Model-list fallback within one provider.
- Agent loop with a fake provider that requests the `write` tool and then `read`, then answers: the file exists and the final text is returned.
- `edit` uniqueness errors; `bash` timeout kill.
- `vendor_check.py` passes.

## Acceptance (run these and put the output summary in REPORT.md)

- `cd core && uv run pytest -q` is green, and `uv run ruff check .` is clean (add ruff as a dev dep).
- Live smoke test, if `OMNIROUTE_API_KEY` is set in the env; skip it otherwise and say so:
  `uv run k3code -p "Create hello.txt containing 'hi' in /tmp/k3smoke, then read it back and tell me its content" --permission yolo`
  This must produce the file, and the answer must contain `hi`.
- `python3 scripts/vendor_check.py` passes.
