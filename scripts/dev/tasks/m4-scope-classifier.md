# m4-scope-classifier: raise the scope classifier from 67% to >=80% without overfitting

## Problem
`scripts/exit/scope_eval.jsonl` has 30 labelled tasks (trivial/small/medium/large/huge). The product's scope classifier
(`core/src/k3code/autonomy/scope.py`, prompt + parser, run on the cheap tier) agreed on only 20/30 = 67% in the last live
run (target >= 80%). Observed misses: one `unparsed` verdict (parser or format failure), trivial rated small (x3),
small rated medium, medium rated small, large rated medium (x2), large rated huge.

## Order of work (do NOT reorder: the blind set must not be influenced by the classifier prompt)
1. **Before opening `autonomy/scope.py` or any scope prompt:** write `scripts/exit/scope_eval_blind.jsonl`, 40 NEW realistic
   coding-agent requests with labels, 8 per level, same JSON shape as `scope_eval.jsonl`. Look at `scope_eval.jsonl`
   only for the schema and the level definitions in `docs/` / `GOAL.md`, then write varied requests of your own (different
   repos, languages, phrasing, some vague, some with hidden scale like "rename X everywhere"). Commit it.
2. Read `autonomy/scope.py`, its tests and the eval harness `scope_eval()` in `scripts/exit/m4_autonomy.py`.
3. Fix the `unparsed` path: the parser must tolerate fenced JSON, prose around the JSON, wrong-case labels and a
   missing field; a genuinely unparseable reply returns a safe default verdict and records why. Add unit tests.
4. Improve the classifier prompt (clear level definitions with concrete anchors such as file counts / cross-cutting /
   needs-plan signals, tie-break rules, 2-3 short examples taken from NEITHER eval file). Tune only against
   `scope_eval.jsonl`; use `scope_eval_blind.jsonl` only to *measure* generalisation. If you tune against the blind set,
   say so and discard the number.
5. Measure with the fake path (unit tests, deterministic) and, ONLY if the task env sets `K3_ALLOW_OMNIROUTE=1`, a live
   run via `python3 scripts/exit/m4_autonomy.py scope_eval` (then also add a `blind` run mode that prints agreement on the
   blind file). Otherwise say live numbers are not measured.
6. Never edit labels in `scope_eval.jsonl` to make the number pass, and never lower the 80% threshold.

## Done when
- `uv run pytest core/tests -q -k scope` passes; `ruff check` clean on touched files.
- `REPORT.md` lists: the new prompt and parser changes, dev/blind agreement (live if measured), the confusion matrix, and
  what is still unconfirmed (labels are proposed by Claude, the owner has not confirmed them).
