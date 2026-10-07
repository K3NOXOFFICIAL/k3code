"""Proposal ranking from decision history: kind acceptance per project, recency, similarity to dismissed ones."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from typing import Any

from k3code.autonomy.proposals import ProposalStore
from k3code.learning.decisions import DecisionLog

HALF_LIFE_DAYS = 14.0


def tokens(text: str) -> set[str]:
    return {t for t in re.split(r"\W+", text.lower()) if len(t) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def accept_rate(log: DecisionLog, kind: str, project: str = "") -> tuple[float, int, float]:
    """(Laplace-smoothed acceptance rate, evidence count, timestamp of the latest decision) for a proposal kind."""
    acc = dis = 0
    last = 0.0
    for r in log.query("proposal", project=project or None):
        if r["detail"].get("kind") != kind:
            continue
        if r["choice"] == "accept":
            acc += 1
        elif r["choice"] == "dismiss":
            dis += 1
        last = max(last, r["ts"])
    return (acc + 1) / (acc + dis + 2), acc + dis, last


def score(text: str, kind: str, *, log: DecisionLog, store: ProposalStore | None, project: str = "",
          now: float | None = None) -> float:
    now = time.time() if now is None else now
    rate, n, last = accept_rate(log, kind, project)
    if n == 0 and project:  # fall back to the all-projects rate
        rate, n, last = accept_rate(log, kind)
    recency = math.exp(-math.log(2) * ((now - last) / 86400) / HALF_LIFE_DAYS) if last else 0.5
    sim = 0.0
    if store is not None:
        mine = tokens(text)
        sim = max((jaccard(mine, tokens(p.text)) for p in store.all() if p.status == "dismissed"), default=0.0)
    return round(0.55 * rate + 0.15 * recency + 0.30 * (1 - sim) - (0.5 if sim >= 0.7 else 0.0), 4)


def rank(items: list[dict[str, str]], *, log: DecisionLog, store: ProposalStore | None, project: str = "",
         threshold: float = 0.15, now: float | None = None,
         clock: Callable[[], float] | None = None) -> list[dict[str, Any]]:
    """Sort proposer suggestions best first and drop those below ``threshold`` (each gains a ``score``)."""
    now = now if now is not None else (clock() if clock else None)
    scored = [{**it, "score": score(it["text"], it["kind"], log=log, store=store, project=project, now=now)}
              for it in items]
    scored.sort(key=lambda d: -d["score"])
    return [d for d in scored if d["score"] >= threshold]
