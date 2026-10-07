# Vendored from Eigenwise/atomic-agents@d2b61b90b2816dbdf9f42f8638b7d375bf08cd11:atomic-examples/deep-research/deep_research/state.py (MIT)
# Copyright (c) 2024 Kenny Vaneetvelde. Adapted: dataclasses kept, claim status + dedupe helpers added.
"""Shared state for the research pipeline: plan, sources (stable S1, S2, ... citation ids) and learnings."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Source:
    """A page we read. ``id`` is the citation anchor used by learnings and the report."""

    id: str
    url: str
    title: str


@dataclass
class Learning:
    """One atomic claim extracted from a single source."""

    text: str
    source_id: str  # matches some Source.id
    sub_topic: str
    #: single | corroborated | contradicted (set by the cross-check)
    status: str = "single"
    note: str = ""


@dataclass
class SubTopic:
    name: str
    initial_queries: list[str]


@dataclass
class ResearchState:
    question: str
    plan: list[SubTopic] = field(default_factory=list)
    learnings: list[Learning] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    queries_seen: set[str] = field(default_factory=set)
    urls_seen: set[str] = field(default_factory=set)

    def learnings_for(self, sub_topic: str) -> list[Learning]:
        return [x for x in self.learnings if x.sub_topic == sub_topic]

    def register_source(self, url: str, title: str) -> Source:
        """Register a source if new; ids are stable within a run."""
        for s in self.sources:
            if s.url == url:
                return s
        source = Source(id=f"S{len(self.sources) + 1}", url=url, title=title or url)
        self.sources.append(source)
        return source

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)
