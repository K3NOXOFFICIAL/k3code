# M0 exit check — 2026-10-07 (verified by the orchestrator)

| Criterion | Result |
|---|---|
| `k3code` completes a read/edit/bash task through OmniRoute | **PASS**. `k3code -p "Read app.py, edit greet…, run it with bash…" --permission yolo`: the file was edited, bash ran, and the answer was `hello, k3!`. An independent `python3` check passed. |
| Blocking the primary fails over to the next entry | **PASS**. The primary `http://127.0.0.1:9` was retried twice with jittered backoff, classified as `network`, then failed over to `omniroute-tailnet`, which returned 200. This happened on every turn. |
| `vendor_check.py` passes | **PASS**: 4 vendored files plus 3 trees, all MIT. |
| `~/.hermes` untouched | **PASS**: 0 files in the Hermes checkout are newer than this session's start. |
| Core tests + lint | **PASS**: 41 pytest tests, ruff clean. |
| TUI builds | **PASS**: `tui/dist/entry.js` builds. vitest: 1662 passed, 2 failed (the worker attributes both to upstream). |
| Panes build | **PASS**: the tuios snapshot builds (`go build ./cmd/tuios`). |

**Spike:** the TUI's gateway contract has 121 methods. 33 are M1-core methods, 24 are M1-core events and 4 are server requests (see `docs/tui-contract.md`), so a new core behind the vendored TUI is feasible. The whole-fork fallback is not needed.

## Follow-ups (go into M1/M2 tasks)
- The router does not keep a network-failed entry in cooldown across turns, so the primary is retried on every turn (about 7 s wasted per turn). Use a short cooldown, e.g. 60 s, for `network`.
- INFO logs go to stdout and mix with the final answer. Send logs to stderr and print only the answer on stdout in `-p` mode.
- A truly OmniRoute-independent chain entry needs a direct provider API key, which the owner must provide. The guided setup (M6) will ask for it. The config schema already supports it.
