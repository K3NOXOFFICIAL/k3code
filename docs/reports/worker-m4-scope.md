# REPORT: m4-scope-classifier (67% -> >=80% without overfitting)

**Status: code and tests done; the live agreement numbers are NOT measured.** `K3_ALLOW_OMNIROUTE` was not set
for this task, so no model call was made (no OmniRoute, no claude-cli). Whether the new prompt reaches >=80% is
unconfirmed. Do not read the 67% -> 80% goal as achieved.

## Order of work
1. Wrote `scripts/exit/scope_eval_blind.jsonl` first (40 new requests, 8 per level, same JSON shape, interleaved
   levels, `label_source: proposed-by-claude`) and committed it (`Add blind scope eval set`) before opening
   `autonomy/scope.py`. I only used `scope_eval.jsonl` for the schema/level mix and the level names in GOAL.md.
   Caveat: I wrote the blind set and then the prompt, so both carry my notion of the levels. The blind set tests
   generalisation to new phrasings, not independence of the labeller.
2. Read scope.py, tests and `scope_eval()`.
3. Parser, 4. prompt (below). The prompt was tuned by reading `scope_eval.jsonl` and the miss list in the task
   description (trivial->small x3, small->medium, medium->small, large->medium x2, large->huge). Anchors (file counts
   2-8 / 9-60, "count scope words", tie-break) were written to fit those labels. **No model run touched the blind
   set; it was not used for tuning.** Prompt examples are in neither eval file (enforced by a unit test).
5. Live run: skipped as required (see above).
6. No label was edited; threshold stays 80%.

## Parser changes (`core/src/k3code/autonomy/scope.py`)
- New `parse_reply(text) -> (verdict | None, why)`; `parse_verdict` is a thin wrapper (same signature as before).
- Scans every top-level JSON object (`raw_decode`), so prose before/after, code fences, and a junk `{...}` before the
  real object work. The old greedy `\{.*\}` regex broke on those.
- Case-insensitive keys; labels normalised (`Medium`, `HUGE`, aliases `big`->large, `massive`->huge, ...);
  string booleans (`"true"`); missing optional fields default (risk missing -> low, unknown risk -> med);
  trailing-comma repair; if no JSON has a scope, `scope: <level>` in prose is accepted (reason "recovered from prose").
- Genuinely unusable replies (empty, no JSON, unknown scope, no scope key) give `None` plus a reason.
  `classify()` then returns `fallback_verdict(why)`: `small`, `source="fallback"`, reason
  `classifier unusable (<why>); heuristics only`, or `classifier call failed: <ExceptionType>`. Previously the reason
  was a generic string and the cause was lost.
- Not changed: the danger floor.

## Prompt changes
`CLASSIFIER_SYSTEM` now has: per-level definitions with concrete anchors (answer-only/one located edit; one
file/function incl. its test; ~2-8 files; ~9-60 files or several layers; whole repo/product), rules on scope words
("everywhere", "all", stated counts), tie-breaks between neighbours (lower if one place and no wiring, higher if
tests/docs/migration/UI/layers; no level added out of caution), "never huge for one subsystem, never small across
packages", the unchanged danger rule, and five one-line examples (one per level) not taken from either eval file.

## Verification
```
cd core && uv run pytest tests -q -k scope        # pass (13 tests in the scope selection)
cd core && uv run pytest tests -q                 # full suite passes
cd core && uv run ruff check src/k3code/autonomy/scope.py tests/test_autonomy_units.py   # All checks passed
```
New unit tests (`core/tests/test_autonomy_units.py`): 7 messy-reply parametrisations, 4 unusable-reply cases with
reason, repair notes, fallback verdict, `classify()` fallback for garbage/timeout/empty and success on a fenced
upper-case reply, prompt defines all five levels and its examples parse and cover each level once, prompt examples
are not in either eval file. The scoring helper was exercised once with a stubbed `live_chat` (fenced `HUGE` reply,
an unparseable reply, a clean reply) and printed the expected confusion matrix; this checks the harness only, it says
nothing about classifier quality.

`scripts/exit/m4_autonomy.py`: `scope_eval` now shares `_score_scope()` and prints the confusion matrix (rows=label,
columns=classifier, plus an `unparsed` column); misses of unparsed replies include the reason. New mode
`python3 scripts/exit/m4_autonomy.py blind` prints agreement, misses and matrix for the blind file (not an exit
row). With no live backend it prints "not run".

## Agreement and confusion matrix
| set | agreement | how |
|---|---|---|
| dev `scope_eval.jsonl` (30) | **not measured** (last live run before these changes: 20/30 = 67%) | needs a live backend |
| blind `scope_eval_blind.jsonl` (40) | **not measured** | `python3 scripts/exit/m4_autonomy.py blind` |

Confusion matrix: not measured (the command above prints it). Run with `K3_ALLOW_OMNIROUTE=1`, or on the
claude-cli backend if the owner allows it. If dev improves but blind stays low, the prompt is overfit.

## Deviations
- Live runs skipped per task rule. The `blind` mode was added anyway so the next live run can use it.
- The `scope_eval` row stays PENDING even at >=80%, as before (labels unconfirmed).
- Pre-existing ruff findings in `scripts/exit/m4_autonomy.py` (7, same as before my edit) were left alone.

## Open / unconfirmed
- Labels in both files are proposed by Claude; the owner has not confirmed them. Ambiguous ones: dev #21 (Django
  3->5 upgrade = huge), #25 (type hints in ~15 files = large) and #11 (print->logging across a package = medium)
  sit on file-count boundaries my prompt anchors may not match.
- Live agreement on dev and blind, and the confusion matrix.
