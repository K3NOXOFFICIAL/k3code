"""Research tools: ``web_search`` (SearXNG), ``web_fetch`` (httpx + readability-lite) and MCP discovery.

MCP servers win when they offer a search/fetch tool (for example ``hub_searxng`` / ``hub_fetch``); otherwise the
built-ins are used. ``research.searxng_url`` has no default: set it to a SearXNG instance (probed once). If it is
unreachable ``web_search`` is disabled with a clear message instead of failing every call.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from k3code.providers.types import ToolSpec
from k3code.research.fetch import UA, FetchStatus, WebFetcher

logger = logging.getLogger(__name__)

DEFAULT_SEARXNG = ""  # no default instance: web_search stays off until research.searxng_url is set
MAX_FETCH_CHARS = 14_000


@dataclass
class Hit:
    title: str
    url: str
    snippet: str = ""


# ── readability-lite ──

_SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre", "blockquote"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._main_depth = 0
        self.main_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("main", "article"):
            self._main_depth += 1
        if tag in _BLOCK:
            self._emit("\n")
        if tag in ("h1", "h2", "h3"):
            self._emit("\n# ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("main", "article") and self._main_depth:
            self._main_depth -= 1
        if tag in _BLOCK:
            self._emit("\n")

    def _emit(self, s: str) -> None:
        if self._skip:
            return
        self.parts.append(s)
        if self._main_depth:
            self.main_parts.append(s)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._emit(data)


def extract_text(markup: str) -> tuple[str, str]:
    """(title, text) from HTML: scripts/nav/footers dropped, <main>/<article> preferred, whitespace collapsed."""
    p = _Text()
    try:
        p.feed(markup)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup: use whatever was parsed
        logger.debug("html parse error", exc_info=True)
    chunks = p.main_parts if len("".join(p.main_parts).strip()) > 400 else p.parts
    text = html.unescape("".join(chunks))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return re.sub(r"\s+", " ", p.title).strip(), text


def _safe_url(url: str) -> str | None:
    u = urlparse(url)
    return url if u.scheme in ("http", "https") and u.netloc else None


async def fetch_page(
    url: str, *, timeout: float = 20.0, client: httpx.AsyncClient | None = None, fetcher: WebFetcher | None = None
) -> tuple[str, str]:
    """(title, text) of a web page through the shared fetch layer (cache, per-host rate, robots.txt, deadline).
    Raises ``FetchStatus`` for a non-2xx answer, ``FetchRefused`` for robots.txt, and httpx errors for the caller."""
    if fetcher is not None:
        return await _read_page(fetcher, url)
    own = WebFetcher(client=client, deadline=timeout)  # no shared fetcher: a short-lived one for this call
    try:
        return await _read_page(own, url)
    finally:
        await own.aclose()


async def _read_page(fetcher: WebFetcher, url: str) -> tuple[str, str]:
    got = await fetcher.get(url)
    if not 200 <= got.status < 300:
        raise FetchStatus(got.status, url)
    if "html" in got.content_type or got.body.lstrip().lower().startswith(("<!doctype", "<html")):
        title, text = extract_text(got.body)
    else:
        title, text = url, got.body
    if got.truncated:
        text += "\n[page cut: the fetch deadline or the size cap was reached]"
    return title or url, text[:MAX_FETCH_CHARS]


class SearxngSearch:
    """SearXNG JSON search; probes the instance once and stays disabled (with a reason) if unreachable."""

    def __init__(self, url: str | None, *, client: httpx.AsyncClient | None = None) -> None:
        self.url = (url or "").rstrip("/")
        self._client = client
        self._ok: bool | None = None
        self.reason = ""

    async def _get(self, params: dict[str, str]) -> httpx.Response:
        own = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0, headers={"User-Agent": UA})
        try:
            return await client.get(f"{self.url}/search", params=params)
        finally:
            if own:
                await client.aclose()

    async def available(self) -> bool:
        if self._ok is not None:
            return self._ok
        if not self.url:
            self._ok, self.reason = False, "no SearXNG URL configured (set research.searxng_url)"
            return False
        try:
            r = await self._get({"q": "test", "format": "json"})
            r.raise_for_status()
            r.json()
            self._ok = True
        except Exception as e:  # noqa: BLE001
            self._ok = False
            self.reason = (f"SearXNG at {self.url} is unreachable or not returning JSON ({type(e).__name__}); "
                           "set research.searxng_url to a working instance")
        return self._ok

    async def search(self, query: str, n: int = 5) -> list[Hit]:
        r = await self._get({"q": query, "format": "json"})
        r.raise_for_status()
        out = []
        for item in (r.json().get("results") or [])[:n]:
            if isinstance(item, dict) and item.get("url"):
                out.append(Hit(str(item.get("title") or item["url"]), str(item["url"]), str(item.get("content") or "")))
        return out


class DuckDuckGoSearch:
    """Keyless fallback: DuckDuckGo's HTML endpoint. Used only when SearXNG is not reachable, so /ultraresearch works on
    a machine without a reachable SearXNG (a fresh install, a laptop on a hotel network)."""

    URL = "https://html.duckduckgo.com/html/"
    _ANCHOR = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    _SNIPPET = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|td|div)>', re.S)

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @staticmethod
    def _clean(fragment: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()

    @classmethod
    def parse(cls, page: str, n: int = 5) -> list[Hit]:
        anchors = list(cls._ANCHOR.finditer(page))
        hits: list[Hit] = []
        for i, m in enumerate(anchors):
            href = html.unescape(m.group(1))
            if href.startswith("//"):
                href = "https:" + href
            query = parse_qs(urlparse(href).query)
            url = unquote(query["uddg"][0]) if "uddg" in query else href  # DuckDuckGo wraps the target in /l/?uddg=
            if _safe_url(url) is None or "duckduckgo.com/y.js" in url:  # not http(s), or an ad
                continue
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(page)
            snippet = cls._SNIPPET.search(page, m.end(), end)  # the snippet that belongs to this result only
            hits.append(Hit(cls._clean(m.group(2)) or url, url, cls._clean(snippet.group(1)) if snippet else ""))
            if len(hits) >= n:
                break
        return hits

    async def search(self, query: str, n: int = 5) -> list[Hit]:
        own = self._client is None
        agent = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
        client = self._client or httpx.AsyncClient(timeout=15.0, headers=agent)
        try:
            r = await client.post(self.URL, data={"q": query})
            r.raise_for_status()
        finally:
            if own:
                await client.aclose()
        return self.parse(r.text, n)


# ── tool providers used by the research flow ──


class ResearchTools:
    """What the flow needs: ``search`` and ``fetch``. Subclassed by the built-in and the MCP provider (and by tests)."""

    name = "none"

    async def unavailable_reason(self) -> str:
        return ""

    async def search(self, query: str, n: int = 5) -> list[Hit]:
        raise NotImplementedError

    async def fetch(self, url: str) -> tuple[str, str]:
        raise NotImplementedError


class BuiltinTools(ResearchTools):
    name = "builtin web_search/web_fetch"

    def __init__(self, searxng_url: str | None, *, keyless_fallback: bool = True,
                 fetcher: WebFetcher | None = None) -> None:
        self.searx = SearxngSearch(searxng_url)
        self.ddg = DuckDuckGoSearch() if keyless_fallback else None
        self.fetcher = fetcher

    async def unavailable_reason(self) -> str:
        if await self.searx.available() or self.ddg is not None:
            return ""
        return self.searx.reason

    async def search(self, query: str, n: int = 5) -> list[Hit]:
        if await self.searx.available():
            return await self.searx.search(query, n)
        if self.ddg is not None:
            return await self.ddg.search(query, n)
        return []

    async def fetch(self, url: str) -> tuple[str, str]:
        return await fetch_page(url, fetcher=self.fetcher)


_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def parse_search_text(text: str) -> list[Hit]:
    """Hits from an MCP search tool's reply: JSON (results/items list) or URL-bearing text blocks."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    items = data.get("results") if isinstance(data, dict) else data if isinstance(data, list) else None
    hits: list[Hit] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and (it.get("url") or it.get("link")):
                url = str(it.get("url") or it.get("link"))
                hits.append(Hit(str(it.get("title") or url), url, str(it.get("content") or it.get("snippet") or "")))
        if hits:
            return hits
    for line in text.splitlines():
        m = _URL_RE.search(line)
        if m:
            hits.append(Hit(line.replace(m.group(0), "").strip(" -*:[]()") or m.group(0), m.group(0).rstrip(".,;")))
    return hits


class DeadLink(RuntimeError):
    """The page is gone (404/410, a 5xx, no connection): citing its search snippet would be a dead link."""


_DEAD_STATUS = re.compile(r"\b(?:404|410|5\d\d)\b")
_DEAD_TEXT = re.compile(
    r"not found|connection refused|connection reset|econnrefused|econnreset|enotfound|getaddrinfo|"
    r"name or service not known|could not resolve|name resolution|\bdns\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://\S+")


def mcp_fetch_error(text: str) -> RuntimeError:
    """DeadLink when an MCP fetch error names a missing page or a failed connection; a plain RuntimeError otherwise
    (a timeout or a 401/403/429 means the page exists). URLs echoed in the message are not matched: the path of
    .../issues/503 or the host dns.google is not a status or a DNS failure."""
    bare = _URL.sub(" ", text)
    dead = _DEAD_STATUS.search(bare) or _DEAD_TEXT.search(bare)
    return DeadLink(text[:200]) if dead else RuntimeError(text[:200])


class McpTools(ResearchTools):
    """Search/fetch through connected MCP tools (e.g. ``hub_searxng`` and ``hub_fetch``)."""

    def __init__(self, mcp: Any, search_tool: Any, fetch_tool: Any | None, fallback: BuiltinTools) -> None:
        self.mcp, self.search_tool, self.fetch_tool, self.fallback = mcp, search_tool, fetch_tool, fallback
        self.name = f"MCP {search_tool.qualified}" + (f" + {fetch_tool.qualified}" if fetch_tool else "")

    @staticmethod
    def _arg(tool: Any, *candidates: str) -> str:
        props = (tool.schema or {}).get("properties") or {}
        return next((c for c in candidates if c in props), candidates[0])

    async def search(self, query: str, n: int = 5) -> list[Hit]:
        res = await self.mcp.call(self.search_tool.qualified, {self._arg(self.search_tool, "query", "q"): query})
        if "error" in res:
            raise RuntimeError(str(res["error"])[:200])
        return parse_search_text(str(res.get("content", "")))[:n]

    async def fetch(self, url: str) -> tuple[str, str]:
        if self.fetch_tool is None:
            return await self.fallback.fetch(url)
        res = await self.mcp.call(self.fetch_tool.qualified, {self._arg(self.fetch_tool, "url", "uri"): url})
        if "error" in res:
            raise mcp_fetch_error(str(res["error"]))
        text = str(res.get("content", ""))
        return url, text[:MAX_FETCH_CHARS]


#: Names that are searches/fetches of something other than the web (memory, sessions, skills, tool discovery, files).
_NOT_WEB = ("memory", "session", "skill", "tool_search", "fleet", "mem0", "nc_", "file", "repo", "code")


#: Helpers that sit next to the real web tool and are not one: an instance description, a suggestion feed, a status
#: readout. A hub_searxng server lists these beside ``searxng_web_search``; the first ``searxng`` match used to win.
_HELPER_TOOLS = ("instance", "suggest", "autocomplete", "info", "health", "status", "config", "engines", "categories")


def rank_tool(tools: list[Any], strong: tuple[str, ...], generic: tuple[str, ...], exclude: tuple[str, ...]) -> Any:
    """Best MCP tool for a web job. A ``strong`` marker only counts on a tool whose name also carries a ``generic``
    action word (``search``/``fetch``), and helper tools never win; then ``generic`` ones minus ``exclude``."""

    def is_helper(t: Any) -> bool:
        return any(h in str(getattr(t, "name", "")).lower() for h in _HELPER_TOOLS)

    def is_excluded(t: Any) -> bool:
        return any(x in t.qualified.lower() for x in exclude)

    for t in tools:
        name = t.qualified.lower()
        if (any(m in name for m in strong) and any(g in name for g in generic)
                and not is_helper(t) and not is_excluded(t)):
            return t
    for t in tools:
        name = t.qualified.lower()
        if any(m in name for m in generic) and not is_helper(t) and not is_excluded(t):
            return t
    return None


def pick_tools(config: Any, mcp: Any, fetcher: WebFetcher | None = None) -> ResearchTools:
    """MCP search/fetch when connected, else the built-ins."""
    cfg = dict(getattr(config, "research", None) or {})
    builtin = BuiltinTools(
        cfg.get("searxng_url", DEFAULT_SEARXNG), keyless_fallback=bool(cfg.get("keyless_fallback", True)),
        fetcher=fetcher,
    )
    tools = list(mcp.tools()) if mcp is not None else []
    search = rank_tool(tools, ("searxng", "web_search", "websearch", "web-search"), ("search",), _NOT_WEB)
    if search is not None:
        fetch = rank_tool(tools, ("hub_fetch", "web_fetch", "webfetch", "fetch_url"), ("fetch", "scrape"), _NOT_WEB)
        return McpTools(mcp, search, fetch, builtin)
    return builtin


# ── agent-facing tools ──


def register_web_tools(reg: Any, config: Any, fetcher: WebFetcher | None = None) -> None:
    """``web_fetch`` always; ``web_search`` over SearXNG (probed lazily, disabled with a message if unreachable)."""
    cfg = dict(getattr(config, "research", None) or {})
    searx = SearxngSearch(cfg.get("searxng_url", DEFAULT_SEARXNG))
    fetcher = fetcher or WebFetcher.from_config(cfg)

    async def tool_fetch(arguments: dict[str, Any], *, cwd: Any = None) -> dict[str, Any]:
        url = str(arguments.get("url") or "")
        try:
            title, text = await fetch_page(url, fetcher=fetcher)
        except Exception as e:  # noqa: BLE001
            return {"error": f"web_fetch failed: {e}"}
        return {"content": f"# {title}\n{url}\n\n{text}"}

    async def tool_search(arguments: dict[str, Any], *, cwd: Any = None) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"error": "web_search needs a query"}
        if not await searx.available():
            return {"error": f"web_search is disabled: {searx.reason}"}
        try:
            hits = await searx.search(query, int(arguments.get("limit") or 5))
        except Exception as e:  # noqa: BLE001
            return {"error": f"web_search failed: {e}"}
        return {"content": "\n".join(f"- {h.title}\n  {h.url}\n  {h.snippet[:200]}" for h in hits) or "no results"}

    reg.register(
        ToolSpec(name="web_fetch", description="Fetch a web page and return its readable text.",
                 parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                 side_effect=False),
        tool_fetch,
    )
    reg.register(
        ToolSpec(name="web_search", description="Search the web (SearXNG). Returns titles, URLs and snippets.",
                 parameters={"type": "object", "properties": {"query": {"type": "string"},
                                                              "limit": {"type": "integer"}}, "required": ["query"]},
                 side_effect=False),
        tool_search,
    )
