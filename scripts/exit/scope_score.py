"""Behaviour-class scoring and the stored score history of the scope classifier eval (no k3code import, no model).

A scope miss only matters for what the gate then does (``ScopeVerdict.behaviour``: cheap start, direct run, plan first,
plan + fan-out). ``score`` counts agreement on that class and weighs a verdict that does LESS than the label wants
(``under``: no plan for a task that needed one, a cheap model on real work) heavier than one that does more (``over``:
a needless plan and approval). ``scope_scores.jsonl`` keeps one line per run; ``regressions`` compares a run with the
previous run of the same set on the same backend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: A row the classifier under-plans costs this many over-planned rows.
UNDER_WEIGHT = 2
#: A weighted score this far below the previous run is reported as a regression. The live classifier is not
#: deterministic: the same prompt scored 0.87 and 0.80 on the 30-row set in two runs, so a smaller drop is noise.
REGRESSION_DROP = 0.10
SCORES = Path(__file__).resolve().parent / "scope_scores.jsonl"


def score(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """``pairs`` = (label behaviour, got behaviour) per scored row, both from ``k3code.autonomy.scope.BEHAVIOURS``."""
    from k3code.autonomy.scope import BEHAVIOURS

    rank = BEHAVIOURS.index
    n = len(pairs)
    under = sum(rank(got) < rank(want) for want, got in pairs)
    over = sum(rank(got) > rank(want) for want, got in pairs)
    weighted = max(0.0, 1 - (UNDER_WEIGHT * under + over) / n) if n else 0.0
    return {"n": n, "exact": n - under - over, "under": under, "over": over, "weighted": round(weighted, 4)}


def regressions(prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[str]:
    """What got worse since ``prev`` (the last stored run of the same set and backend); [] on the first run."""
    if prev is None:
        return []
    drop = float(prev["weighted"]) - float(cur["weighted"])
    if drop < REGRESSION_DROP - 1e-9:
        return []
    same = prev.get("prompt_sha") == cur.get("prompt_sha")
    return [
        f"REGRESSION {cur['set']}: weighted class score {prev['weighted']:.2f} -> {cur['weighted']:.2f} "
        f"(under-plan rows {prev.get('under', '?')} -> {cur.get('under', '?')}); "
        + ("same prompt, so re-run before believing it" if same else "the classifier prompt changed")
    ]


def read(path: Path = SCORES) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line) if line.strip() else None
            except ValueError:
                continue  # a torn line must not break the history
            if isinstance(rec, dict):
                out.append(rec)
    return out


def previous(path: Path, set_name: str, backend: str) -> dict[str, Any] | None:
    """The newest stored run of ``set_name`` on ``backend``."""
    runs = [r for r in read(path) if r.get("set") == set_name and r.get("backend") == backend]
    return runs[-1] if runs else None


def append(path: Path, rec: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
