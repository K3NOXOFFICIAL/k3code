# Vendored from Eigenwise/atomic-agents@d2b61b90b2816dbdf9f42f8638b7d375bf08cd11:atomic-examples/deep-research/deep_research/main.py (MIT)
# Design port: pipeline reimplemented over ModelCaller tiers; no code copied. See VENDOR.toml.
"""The research pipeline: plan sub-questions -> search (parallel, cheap) -> read+extract (parallel, cheap) ->
cross-check claims (cheap) -> synthesize with numbered citations (strong). Flow adapted from atomic-agents
deep-research (see ``prompts.py``); sources/learnings state in ``state.py``."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.artifacts import write_artifact_file
from k3code.providers.types import Message
from k3code.research import prompts
from k3code.research.state import Learning, ResearchState, SubTopic
from k3code.research.tools import Hit, ResearchTools, pick_tools
from k3code.routing.tiers import TaskKind, Tier

logger = logging.getLogger(__name__)

CITE_RE = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")
DEFAULTS = {"sub_questions": 4, "results_per_query": 4, "sources_per_topic": 3, "concurrency": 4, "max_claims": 60}


class ResearchUnavailable(Exception):
    """No search tool is reachable; the message says what to configure."""


def research_cfg(config: Any) -> dict[str, Any]:
    return {**DEFAULTS, **dict(getattr(config, "research", None) or {})}


def loose_json(text: str) -> Any:
    """First-to-last-brace (or bracket) JSON in a model reply, or None."""
    for open_, close in (("{", "}"), ("[", "]")):
        a, b = (text or "").find(open_), (text or "").rfind(close)
        if a != -1 and b > a:
            try:
                return json.loads(text[a : b + 1])
            except ValueError:
                continue
    return None


def finalize_report(report: str, state: ResearchState) -> tuple[str, list[str]]:
    """Make the citations match the sources: drop unknown ids, rebuild ``## Sources`` from what is cited.

    Returns (markdown, cited source ids in order).
    """
    body = re.split(r"\n#{1,3}\s*Sources\b", report, maxsplit=1, flags=re.I)[0].rstrip()
    known = {s.id for s in state.sources}
    cited: list[str] = []

    def fix(m: re.Match[str]) -> str:
        ids = [i.strip() for i in m.group(1).split(",")]
        keep = [i for i in ids if i in known]
        for i in keep:
            if i not in cited:
                cited.append(i)
        return f"[{', '.join(keep)}]" if keep else ""

    body = CITE_RE.sub(fix, body)
    body = re.sub(r"[ \t]+([.,;])", r"\1", body)
    if not cited:
        body += "\n\n> Warning: the report contains no verifiable citations."
    cited.sort(key=lambda i: int(i[1:]))
    lines = []
    for sid in cited:
        s = state.source(sid)
        if s is not None:
            lines.append(f"- [{s.id}]: {s.title} — {s.url}")
    return body + ("\n\n## Sources\n" + "\n".join(lines) if lines else ""), cited


@dataclass
class ResearchResult:
    question: str
    report: str
    state: ResearchState
    cited: list[str] = field(default_factory=list)
    path: Path | None = None
    tools: str = ""


class Research:
    def __init__(self, server: Any) -> None:
        self.server = server

    def tools(self) -> ResearchTools:
        override = getattr(self.server, "research_tools", None)  # test seam
        return override or pick_tools(self.server.config, self.server.mcp)

    def progress(self, session: Any, phase: str, detail: str = "") -> None:
        session.emit("research.progress", {"session_id": session.session_id, "phase": phase, "detail": detail},
                     importance="essential")
        session.emit("status.update", {"kind": "status", "text": f"/ultraresearch: {phase}", "state": "working"})

    async def _ask(
        self, session: Any, system: str, user: str, *, tier: Tier | None = None, max_tokens: int = 2048
    ) -> str:
        res = await self.server.model_caller.complete(
            TaskKind.RESEARCH_SEARCH, [Message(role="system", content=system), Message(role="user", content=user)],
            session_id=session.session_id, max_tokens=max_tokens, tier=tier,
        )
        return res.text

    async def run(self, session: Any, question: str, *, n_sub: int | None = None) -> ResearchResult:
        cfg = research_cfg(self.server.config)
        tools = self.tools()
        if reason := await tools.unavailable_reason():
            raise ResearchUnavailable(f"/ultraresearch needs a search tool and none is available: {reason}. "
                                      "Connect an MCP server with a web search tool (e.g. k3nox hub_searxng) or "
                                      "set research.searxng_url.")
        n_sub = int(n_sub or cfg["sub_questions"])
        state = ResearchState(question)
        sem = asyncio.Semaphore(int(cfg["concurrency"]))

        # 1. decompose
        self.progress(session, "decomposing", question[:80])
        plan_text = await self._ask(session, prompts.PLANNER, f"Question: {question}\nNumber of sub-topics: {n_sub}")
        state.plan = self.parse_plan(plan_text, question, n_sub)

        # 2. search in parallel (cheap tier does the query writing; the tool does the searching)
        self.progress(session, "searching", f"{sum(len(t.initial_queries) for t in state.plan)} queries")

        async def one_search(topic: SubTopic, query: str) -> tuple[SubTopic, list[Hit]]:
            async with sem:
                try:
                    return topic, await tools.search(query, int(cfg["results_per_query"]))
                except Exception as e:  # noqa: BLE001 - one failed query must not sink the run
                    logger.warning("search %r failed: %s", query, e)
                    return topic, []

        pairs = [(t, q) for t in state.plan for q in t.initial_queries if not (q in state.queries_seen
                                                                              or state.queries_seen.add(q))]
        found = await asyncio.gather(*(one_search(t, q) for t, q in pairs))
        picks: list[tuple[SubTopic, Hit]] = []
        per_topic: dict[str, int] = {}
        for topic, hits in found:
            for h in hits:
                if h.url in state.urls_seen or per_topic.get(topic.name, 0) >= int(cfg["sources_per_topic"]):
                    continue
                state.urls_seen.add(h.url)
                per_topic[topic.name] = per_topic.get(topic.name, 0) + 1
                picks.append((topic, h))
        if not picks:
            raise RuntimeError("the searches returned no results; nothing to research from")

        # 3. read + extract in parallel
        self.progress(session, "reading", f"{len(picks)} sources")

        async def one_read(topic: SubTopic, hit: Hit) -> tuple[SubTopic, Hit, str, list[str]]:
            async with sem:
                try:
                    title, text = await tools.fetch(hit.url)
                except Exception as e:  # noqa: BLE001
                    logger.warning("fetch %s failed: %s", hit.url, e)
                    title, text = hit.title, hit.snippet
                if not text.strip():
                    return topic, hit, hit.title, []
                reply = await self._ask(
                    session, prompts.EXTRACTOR,
                    f"Sub-topic: {topic.name}\nSource: {hit.url}\nTitle: {title}\n\nContent:\n{text}",
                    max_tokens=1024,
                )
                data = loose_json(reply)
                claims = [str(c).strip() for c in (data or {}).get("claims", []) if str(c).strip()] \
                    if isinstance(data, dict) else []
                return topic, hit, title or hit.title, claims

        read = await asyncio.gather(*(one_read(t, h) for t, h in picks))
        for topic, hit, title, claims in read:  # registered in plan order so S-ids are deterministic
            if claims:
                src = state.register_source(hit.url, title)
                state.learnings += [Learning(c, src.id, topic.name) for c in claims]
        if not state.learnings:
            raise RuntimeError("no claims could be extracted from the sources")

        # 4. cross-check
        await self.cross_check(session, state, int(cfg["max_claims"]))

        # 5. synthesize (strong)
        self.progress(session, "synthesizing", f"{len(state.sources)} sources, {len(state.learnings)} claims")
        draft = await self._ask(session, prompts.WRITER, self.writer_input(state), tier=Tier.STRONG, max_tokens=4096)
        report, cited = finalize_report(draft, state)
        path = self.save(session, state, report, tools.name)
        return ResearchResult(question, report, state, cited, path, tools.name)

    def parse_plan(self, text: str, question: str, n: int) -> list[SubTopic]:
        data = loose_json(text)
        topics: list[SubTopic] = []
        raw = data.get("sub_topics") if isinstance(data, dict) else data if isinstance(data, list) else None
        for t in (raw or [])[:n]:
            if isinstance(t, dict) and t.get("name"):
                qs = [str(q) for q in (t.get("initial_queries") or []) if str(q).strip()][:3]
                topics.append(SubTopic(str(t["name"]), qs or [f"{question} {t['name']}"]))
        return topics or [SubTopic("overview", [question])]

    async def cross_check(self, session: Any, state: ResearchState, max_claims: int) -> None:
        if len(state.sources) < 2 or len(state.learnings) < 2:
            return
        self.progress(session, "cross-checking", f"{len(state.learnings)} claims")
        items = state.learnings[:max_claims]
        listing = "\n".join(f"c{i} [{x.source_id}] {x.text}" for i, x in enumerate(items))
        data = loose_json(await self._ask(session, prompts.CROSSCHECK,
                                          f"Question: {state.question}\n\nClaims:\n{listing}", max_tokens=1024))
        if not isinstance(data, dict):
            return
        for cid in data.get("corroborated") or []:
            m = re.fullmatch(r"c(\d+)", str(cid))
            if m and int(m.group(1)) < len(items):
                items[int(m.group(1))].status = "corroborated"
        for c in data.get("contradictions") or []:
            if not isinstance(c, dict):
                continue
            for key in ("a", "b"):
                m = re.fullmatch(r"c(\d+)", str(c.get(key)))
                if m and int(m.group(1)) < len(items):
                    items[int(m.group(1))].status = "contradicted"
                    items[int(m.group(1))].note = str(c.get("note") or "")

    def writer_input(self, state: ResearchState) -> str:
        parts = [f"Research question: {state.question}", "", "Sources:"]
        parts += [f"- [{s.id}] {s.title} ({s.url})" for s in state.sources]
        for t in state.plan:
            ls = state.learnings_for(t.name)
            if not ls:
                continue
            parts += ["", f"## {t.name}"]
            for x in ls:
                tag = {"corroborated": "corroborated", "contradicted": "DISPUTED"}.get(x.status, "single source")
                parts.append(f"- [{x.source_id}] ({tag}) {x.text}" + (f" (conflict: {x.note})" if x.note else ""))
        return "\n".join(parts)

    def save(self, session: Any, state: ResearchState, report: str, tool_name: str) -> Path:
        cwd = Path(session.stored.cwd or ".")
        d = cwd / ".k3code" / "research"
        d.mkdir(parents=True, exist_ok=True)
        gi = cwd / ".k3code" / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        head = (f"# Research: {state.question}\n\n_{time.strftime('%Y-%m-%d %H:%M')} — tools: {tool_name}; "
                f"sub-topics: {', '.join(t.name for t in state.plan)}; {len(state.sources)} sources_\n\n")
        return write_artifact_file(self.server, "research", d, state.question, head + report + "\n",
                                   session=session.session_id)
