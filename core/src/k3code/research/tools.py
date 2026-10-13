"""Research tools: ``web_search`` (SearXNG), ``web_fetch`` (httpx + readability-lite) and MCP discovery.

MCP servers win when they offer a search/fetch tool (for example ``hub_searxng`` / ``hub_fetch``); otherwise the
built-ins are used. ``research.searxng_url`` has no default: set it to a SearXNG instance (probed once). If it is
unreachable ``web_search`` is disabled with a clear message instead of failing every call.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from k3code.providers.types import ToolSpec
from k3code.research.browser import (
    STOP_VERDICTS,
    BrowserManager,
    BrowserUnavailable,
    RenderedPage,
    Stopped,
    Verdict,
    classify,
    stop_report,
)
from k3code.research.fetch import UA, FetchRefused, FetchStatus, WebFetcher
from k3code.tools import MAX_TOOL_RESULT_CHARS

logger = logging.getLogger(__name__)

DEFAULT_SEARXNG = ""  # no default instance: web_search stays off until research.searxng_url is set
#: The most of a page web_fetch returns. Under the 10,000 characters the model is sent of any tool result (see
#: k3code.tools.clip_for_model): above them the middle of the page was cut a second time, silently.
MAX_FETCH_CHARS = 9_500
#: The most of a page's title the ``# title`` line above a fetched page shows (a title can be a whole URL).
_TITLE_ROOM = 200


@dataclass
class Hit:
    title: str
    url: str
    snippet: str = ""


# ── readability-lite ──

#: ``form`` and ``header`` are not skipped: an ASP.NET page wraps its whole body in one <form>, and an article's
#: <header> holds its title and byline.
_SKIP = {"script", "style", "noscript", "nav", "footer", "aside", "svg", "iframe"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "blockquote"}
_PRE = "\0"  # marks where a <pre> block starts and ends in the parts; removed from the page's own text
_FLAT_ROW = 1000  # a longer table row is page layout, not data: it keeps its line breaks


class _Text(HTMLParser):
    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.base = base_url
        self._skip = 0
        self._in_title = False
        self._main_depth = 0
        self.main_parts: list[str] = []
        self._pre = 0
        self._pre_in: list[list[str]] = []  # the lists that got the open <pre>'s start mark: they get its end mark
        self._link: tuple[str, int, int] | None = None  # target, where its text starts in parts and main_parts
        self._row: tuple[int, int] | None = None  # where the open table row starts in parts and main_parts
        self._cells = 0
        self._tables: list[tuple[tuple[int, int] | None, int]] = []  # the open row of each enclosing table

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("main", "article"):
            self._main_depth += 1
        elif tag == "base":
            self.base = self._target(dict(attrs).get("href")) or self.base
        if self._pre:  # inside <pre> only the text and its line breaks count
            self._pre += tag == "pre"
            if tag == "br":
                self._emit("\n")
            elif tag in _BLOCK:
                self._line_break()
            return
        if tag in _BLOCK:
            self._emit("\n")
        if tag in ("h1", "h2", "h3"):
            self._emit("\n# ")
        if tag == "pre":
            self._pre = 1
            self._pre_in = [] if self._skip else [self.parts, self.main_parts] if self._main_depth else [self.parts]
            for parts in self._pre_in:
                parts.append("\n" + _PRE)
        elif tag == "a":
            self._close_link()  # an <a> closes an open one, as in a browser
            target = self._target(dict(attrs).get("href"))
            if target and not self._skip:
                self._link = (target, len(self.parts), len(self.main_parts))
        elif tag == "table":
            self._tables.append((self._row, self._cells))
            self._row, self._cells = None, 0
            self._emit("\n\n")
        elif tag == "tr":
            self._end_row()
            self._start_row()
        elif tag in ("td", "th"):  # the end tag of a cell may be left out: the separator goes before each cell
            if self._row is None:
                self._start_row()
            self._emit(" | " if self._cells else "| ")
            self._cells += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("main", "article") and self._main_depth:
            self._main_depth -= 1
        if self._pre:
            if tag == "pre":
                self._pre -= 1
                if not self._pre:
                    self._end_pre()
            elif tag in _BLOCK:
                self._line_break()
            return
        if tag in _BLOCK:
            self._emit("\n")
        if tag == "a":
            self._close_link()
        elif tag == "tr":
            self._end_row()
        elif tag == "table" and self._tables:
            self._end_row()
            self._row, self._cells = self._tables.pop()
            self._emit("\n\n")

    def close(self) -> None:
        super().close()
        if self._pre:  # an unclosed <pre> still ends its code block
            self._pre = 0
            self._end_pre()

    def _end_pre(self) -> None:
        for parts in self._pre_in:
            parts.append(_PRE + "\n")
        self._pre_in = []

    def _line_break(self) -> None:
        """A block inside <pre> (a highlighter's line) starts a new line, unless one has just started."""
        if self.parts and self.parts[-1][-1:] not in ("\n", _PRE):
            self._emit("\n")

    def _target(self, href: str | None) -> str | None:
        """The absolute URL of an href; None for an in-page anchor or a non-web scheme (javascript:, mailto:)."""
        href = (href or "").replace(_PRE, "").strip()
        if not href or href.startswith("#"):
            return None
        try:
            url = urljoin(self.base, href)
            scheme = urlparse(url).scheme
        except ValueError:  # a malformed URL is left out; it must not end the parse of the page
            return None
        return url.replace(" ", "%20") if scheme in ("", "http", "https") else None

    def _close_link(self) -> None:
        """``[text](target)`` in place of the link's text. A link with no text (an icon, an image) stays as it is."""
        if self._link is None:
            return
        target, at, main_at = self._link
        self._link = None
        for parts, start in ((self.parts, at), (self.main_parts, main_at)):
            raw = "".join(parts[start:])
            text = " ".join(raw.split())
            if text and _PRE not in raw:
                lead, trail = raw[: len(raw) - len(raw.lstrip())], raw[len(raw.rstrip()) :]
                parts[start:] = [f"{lead}[{text}]({target}){trail}"]

    def _start_row(self) -> None:
        self._emit("\n")
        self._row, self._cells = (len(self.parts), len(self.main_parts)), 0

    def _end_row(self) -> None:
        """``| a | b |`` on one line; a row that holds code or is too long to be data keeps its line breaks."""
        if self._row is None:
            return
        if self._cells:
            self._emit(" |")
        for parts, start in zip((self.parts, self.main_parts), self._row, strict=True):
            row = "".join(parts[start:])
            flat = " ".join(row.split())
            if _PRE not in row and len(flat) <= _FLAT_ROW:
                parts[start:] = [flat]
        self._row, self._cells = None, 0

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
            if self._tables and self._row is None and not self._pre and data.isspace():
                data = "" if "\n" in data else " "  # the line breaks between rows would put blank lines into the table
            self._emit(data.replace(_PRE, ""))


def extract_text(markup: str, base_url: str = "") -> tuple[str, str]:
    """(title, text) from HTML as Markdown: scripts/nav/footers dropped, <main>/<article> preferred, <pre> kept
    verbatim in a code fence, links as ``[text](url)`` resolved against ``base_url`` (the page's URL), table rows as
    ``| a | b |``, whitespace collapsed outside code."""
    p = _Text(base_url)
    try:
        p.feed(markup)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup: use whatever was parsed
        logger.debug("html parse error", exc_info=True)
    chunks = p.main_parts if len("".join(p.main_parts).strip()) > 400 else p.parts
    # The parser has decoded the entities: a second unescape would turn "&section=" in a URL into "§ion=".
    pieces = "".join(chunks).split(_PRE)
    for i in range(0, len(pieces), 2):  # outside <pre>
        text = re.sub(r"[ \t\r\f\v]+", " ", pieces[i])
        pieces[i] = re.sub(r"\n\s*\n+", "\n\n", text)
    for i in range(1, len(pieces), 2):  # a <pre> block as it was, in a fence longer than any run of backticks in it
        code = pieces[i].replace("\r\n", "\n").strip("\n")
        fence = "`" * max([3, *(len(run) + 1 for run in re.findall(r"`+", code))])
        pieces[i] = f"{fence}\n{code}\n{fence}"
    return re.sub(r"\s+", " ", p.title).strip(), "".join(pieces).strip()


def _safe_url(url: str) -> str | None:
    u = urlparse(url)
    return url if u.scheme in ("http", "https") and u.netloc else None


async def fetch_page(
    url: str,
    *,
    timeout: float = 20.0,
    client: httpx.AsyncClient | None = None,
    fetcher: WebFetcher | None = None,
    browser: BrowserManager | None = None,
    start: int = 0,
    limit: int = MAX_FETCH_CHARS,
) -> tuple[str, str]:
    """(title, text) of a web page through the shared fetch layer (cache, per-host rate, robots.txt, deadline).
    A 403 or a challenge page is read in the browser when one is given. ``start`` is the character the text is shown
    from (a long page is read in pieces) and cut to ``limit`` chars. Raises ``Stopped`` at a CAPTCHA, a login wall or a
    paywall; ``FetchStatus`` for other non-2xx answers; ``FetchRefused`` for robots.txt; httpx errors."""
    if fetcher is not None:
        return await _read_page(fetcher, url, browser, start, limit)
    own = WebFetcher(client=client, deadline=timeout)  # no shared fetcher: a short-lived one for this call
    try:
        return await _read_page(own, url, browser, start, limit)
    finally:
        await own.aclose()


async def _read_page(
    fetcher: WebFetcher, url: str, browser: BrowserManager | None, start: int = 0, limit: int = MAX_FETCH_CHARS
) -> tuple[str, str]:
    got = await fetcher.get(url)
    verdict = classify(got.status, got.body)
    if verdict is Verdict.BLOCKED and browser is not None:
        try:
            return await _read_rendered(browser, url, fetcher, start, limit)
        except BrowserUnavailable as e:  # the error said only "install Playwright": say what was refused as well
            raise BrowserUnavailable(f"HTTP {got.status} for {url} (a blocked page): {e}") from None
    if verdict in STOP_VERDICTS:
        raise Stopped(stop_report(verdict, url))
    if not 200 <= got.status < 300:
        raise FetchStatus(got.status, url)
    if not _is_text(got.content_type):
        raise ValueError(f"{url} is not a text page ({got.content_type.split(';')[0].strip()}): nothing to read")
    if "html" in got.content_type or got.body.lstrip().lower().startswith(("<!doctype", "<html")):
        title, text = await asyncio.to_thread(extract_text, got.body, got.url)  # off the event loop: CPU-bound
    else:
        title, text = url, got.body
    note = "\n[page cut: the fetch deadline or the size cap was reached]" if got.truncated else ""
    return title or url, _cap_page(text, note, start, limit)


def _is_text(content_type: str) -> bool:
    """A body worth decoding as text: no content type, ``text/*``, or a json/xml/javascript type. A PDF or an image
    decoded as text came back as thousands of replacement characters."""
    main = content_type.split(";")[0].strip().lower()
    return not main or main.startswith("text/") or main.endswith(("json", "xml", "javascript", "x-www-form-urlencoded"))


def _page_room(budget: int, header: int) -> int:
    """The chars of page text that fit ``budget`` (what the model is sent of one tool result) under a ``header`` of
    that many chars, at most MAX_FETCH_CHARS (also under a raised budget). Over the budget the loop clips the middle of
    the result, which no marker in the page can name; the floor keeps a huge URL from leaving no room."""
    return min(MAX_FETCH_CHARS, max(budget - header, budget // 2))


def _cap_page(text: str, note: str = "", start: int = 0, limit: int = MAX_FETCH_CHARS) -> str:
    """``text`` + ``note`` within ``limit`` chars, from character ``start``: a longer page keeps its start and says how
    much of it that is and where to go on (the note used to be appended before the cut and fell off the end of a long
    page, which then looked complete; with no way to ask for the rest, the retry returned the same piece)."""
    total = len(text)
    if start > 0:
        if start >= total:
            return f"[start_index {start} is past the end of the page: it has {total} characters]{note}"
        text = text[start:]
    if len(text) + len(note) <= limit:
        return text + note
    marker = f"\n[page cut: characters {start}-{{}} of {total} are shown; continue with start_index={{}}]"
    keep = max(0, limit - len(note) - len(marker) - 2 * len(str(total)))
    return text[:keep] + marker.format(start + keep, start + keep) + note


async def _read_rendered(
    browser: BrowserManager, url: str, fetcher: WebFetcher, start: int = 0, limit: int = MAX_FETCH_CHARS
) -> tuple[str, str]:
    """A page the browser renders. The same stop rules apply: a CAPTCHA, a login or a paywall ends the read. The
    browser's own navigation is admitted like any other request: robots.txt, then a token of the host's bucket."""
    await fetcher.admit(url)
    page: RenderedPage = await browser.fetch(url)
    verdict = await asyncio.to_thread(classify, page.status, page.html)
    if verdict in STOP_VERDICTS:
        raise Stopped(stop_report(verdict, url))
    if verdict is not Verdict.OK:
        raise RuntimeError(f"still blocked in the browser (HTTP {page.status}) at {url}; not solved, not retried")
    title, text = await asyncio.to_thread(extract_text, page.html, page.url)  # off the event loop: CPU-bound
    note = "\n[page cut: the size cap was reached]" if page.truncated else ""
    return title or url, _cap_page(text, note, start, limit)


#: A failure to reach the host at all (no network, no route, DNS down): the same whichever backend is asked, unlike an
#: HTTP error or a page that is not JSON.
UNREACHABLE = (httpx.ConnectError, httpx.ConnectTimeout)


class SearxngSearch:
    """SearXNG JSON search; probes the instance once and stays disabled (with a reason) if unreachable."""

    def __init__(self, url: str | None, *, client: httpx.AsyncClient | None = None) -> None:
        self.url = (url or "").rstrip("/")
        self._client = client
        self._ok: bool | None = None
        self.reason = ""
        self.unreachable = False  # the probe could not connect (as opposed to an answer that was not JSON)

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
            self.unreachable = isinstance(e, UNREACHABLE)
            self.reason = (
                f"SearXNG at {self.url} is unreachable or not returning JSON ({type(e).__name__}); "
                "set research.searxng_url to a working instance"
            )
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

    def __init__(
        self,
        searxng_url: str | None,
        *,
        keyless_fallback: bool = True,
        fetcher: WebFetcher | None = None,
        browser: BrowserManager | None = None,
    ) -> None:
        self.searx = SearxngSearch(searxng_url)
        self.ddg = DuckDuckGoSearch() if keyless_fallback else None
        self.fetcher = fetcher
        self.browser = browser

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
        return await fetch_page(url, fetcher=self.fetcher, browser=self.browser)


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


async def mcp_search_hits(mcp: Any, tool: Any, query: str, n: int = 5) -> list[Hit]:
    """Hits from one call of a connected MCP search tool."""
    res = await mcp.call(tool.qualified, {McpTools._arg(tool, "query", "q"): query})
    if "error" in res:
        raise RuntimeError(str(res["error"])[:200])
    return parse_search_text(str(res.get("content", "")))[:n]


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
        return await mcp_search_hits(self.mcp, self.search_tool, query, n)

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


#: Words that say a search/fetch tool reaches the web (a provider or the word itself), matched as whole name parts:
#: ``github.search_issues``, ``Gmail.search_threads`` and ``Neon.search`` search something else: they must not win.
_WEB_WORDS = frozenset(
    {"web", "internet", "tavily", "brave", "exa", "serp", "serper", "ddg", "duckduckgo", "perplexity", "firecrawl"}
    | {"kagi", "bing", "searx", "searxng", "jina", "crawl", "scrape", "browse", "url", "http", "https"}
)


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]


def _is_webish(t: Any) -> bool:
    """A tool that reaches the web: a web word in its qualified name, or a url argument."""
    props = (getattr(t, "schema", None) or {}).get("properties") or {}
    return bool(_WEB_WORDS.intersection(_words(t.qualified))) or any(k in props for k in ("url", "uri"))


def _excluded(t: Any, exclude: tuple[str, ...]) -> bool:
    """A word of ``exclude`` starting one of the name's words (``file`` -> ``files``, not ``profile``); entries with an
    underscore (``tool_search``, ``nc_``) are matched as text."""
    name, words = t.qualified.lower(), _words(t.qualified)
    return any((x in name) if "_" in x else any(w.startswith(x) for w in words) for x in exclude)


def rank_tool(tools: list[Any], strong: tuple[str, ...], generic: tuple[str, ...], exclude: tuple[str, ...]) -> Any:
    """Best MCP tool for a web job. A ``strong`` marker only counts on a tool whose name also carries a ``generic``
    action word (``search``/``fetch``), and helper tools never win; then ``generic`` ones minus ``exclude``, only when
    they are web tools at all (``_is_webish``)."""

    def is_helper(t: Any) -> bool:
        return any(h in str(getattr(t, "name", "")).lower() for h in _HELPER_TOOLS)

    def is_excluded(t: Any) -> bool:
        return _excluded(t, exclude)

    for t in tools:
        name = t.qualified.lower()
        if (
            any(m in name for m in strong)
            and any(g in name for g in generic)
            and not is_helper(t)
            and not is_excluded(t)
        ):
            return t
    for t in tools:
        name = t.qualified.lower()
        if any(m in name for m in generic) and not is_helper(t) and not is_excluded(t) and _is_webish(t):
            return t
    return None


_SEARCH_MARKERS = ("searxng", "web_search", "websearch", "web-search")


def mcp_search_tool(mcp: Any) -> Any:
    """The connected MCP tool that searches the web, or None."""
    tools = list(mcp.tools()) if mcp is not None else []
    return rank_tool(tools, _SEARCH_MARKERS, ("search",), _NOT_WEB)


def pick_tools(
    config: Any, mcp: Any, fetcher: WebFetcher | None = None, browser: BrowserManager | None = None
) -> ResearchTools:
    """MCP search/fetch when connected, else the built-ins."""
    cfg = dict(getattr(config, "research", None) or {})
    builtin = BuiltinTools(
        cfg.get("searxng_url", DEFAULT_SEARXNG),
        keyless_fallback=bool(cfg.get("keyless_fallback", True)),
        fetcher=fetcher,
        browser=browser,
    )
    tools = list(mcp.tools()) if mcp is not None else []
    search = mcp_search_tool(mcp)
    if search is not None:
        fetch = rank_tool(tools, ("hub_fetch", "web_fetch", "webfetch", "fetch_url"), ("fetch", "scrape"), _NOT_WEB)
        return McpTools(mcp, search, fetch, builtin)
    return builtin


# ── agent-facing tools ──


async def fallback_search(
    query: str, n: int, *, searx: SearxngSearch, ddg: DuckDuckGoSearch | None, mcp: Any = None
) -> tuple[list[Hit], list[str], bool, bool]:
    """The agent's ``web_search`` chain: SearXNG, then keyless DuckDuckGo, then a connected MCP search tool. The first
    source with hits wins. Returns (hits, notes, answered, unreachable); ``answered`` is False when no source could be
    asked, ``unreachable`` that every source that was tried failed to connect (the network is down, not the setup)."""
    notes: list[str] = []
    answered = False
    tried = unreachable = 0  # sources that failed, and of those the ones that could not even be connected to

    def failed(down: bool) -> None:
        nonlocal tried, unreachable
        tried, unreachable = tried + 1, unreachable + down

    if not await searx.available():
        notes.append(f"SearXNG: {searx.reason}")
        if searx.url:  # a SearXNG that is not configured was never asked
            failed(searx.unreachable)
    else:
        try:
            hits = await searx.search(query, n)
        except Exception as e:  # noqa: BLE001 - one dead source must not end the chain
            notes.append(f"SearXNG failed: {type(e).__name__}")
            failed(isinstance(e, UNREACHABLE))
        else:
            answered = True
            if hits:
                return hits, notes, True, False
            notes.append("SearXNG: no results")
    if ddg is None:
        notes.append("keyless DuckDuckGo fallback is off (research.keyless_fallback)")
    else:
        try:
            hits = await ddg.search(query, n)
        except Exception as e:  # noqa: BLE001
            notes.append(f"DuckDuckGo failed: {type(e).__name__}")
            failed(isinstance(e, UNREACHABLE))
        else:
            answered = True
            if hits:
                return hits, notes, True, False
            notes.append("DuckDuckGo: no results")
    tool = mcp_search_tool(mcp)
    if tool is None:
        notes.append("no MCP web search tool is connected")
    else:
        try:
            hits = await mcp_search_hits(mcp, tool, query, n)
        except Exception as e:  # noqa: BLE001
            notes.append(f"MCP {tool.qualified} failed: {str(e)[:120]}")
            failed(False)
        else:
            answered = True
            if hits:
                return hits, notes, True, False
            notes.append("MCP search: no results")
    return [], notes, answered, not answered and tried > 0 and tried == unreachable


def register_web_tools(
    reg: Any, config: Any, fetcher: WebFetcher | None = None, mcp: Any = None, browser: BrowserManager | None = None
) -> None:
    """``web_fetch`` (escalates to the browser on a 403 or a challenge), ``web_browse`` (renders a page in the browser),
    and ``web_search`` over SearXNG, then keyless DuckDuckGo, then a connected MCP search tool."""
    cfg = dict(getattr(config, "research", None) or {})
    searx = SearxngSearch(cfg.get("searxng_url", DEFAULT_SEARXNG))
    ddg = DuckDuckGoSearch() if bool(cfg.get("keyless_fallback", True)) else None
    fetcher = fetcher or WebFetcher.from_config(cfg, getattr(config, "web", None))
    browser = browser or BrowserManager.from_config(config)
    # what the model is sent of one result: the page and the header above it must fit, or the loop cuts the middle
    budget = int((getattr(config, "context", None) or {}).get("tool_output_chars", 0) or 0) or MAX_TOOL_RESULT_CHARS

    async def tool_fetch(arguments: dict[str, Any], *, cwd: Any = None) -> dict[str, Any]:
        url = str(arguments.get("url") or "")
        try:
            start = max(0, int(arguments.get("start_index") or 0))
            room = _page_room(budget, len(url) + _TITLE_ROOM + 5)
            title, text = await fetch_page(url, fetcher=fetcher, browser=browser, start=start, limit=room)
        except Exception as e:  # noqa: BLE001
            return {"error": f"web_fetch failed: {e}"}
        return {"content": f"# {title[:_TITLE_ROOM]}\n{url}\n\n{text}"}

    async def tool_browse(arguments: dict[str, Any], *, cwd: Any = None) -> dict[str, Any]:
        url = str(arguments.get("url") or "")
        if _safe_url(url) is None:
            return {"error": f"web_browse needs an http(s) URL: {url[:200]}"}
        try:
            await fetcher.admit(url)
        except FetchRefused as e:
            return {"error": str(e)}
        try:
            page = await browser.fetch(url)
        except BrowserUnavailable as e:
            return {"error": f"web_browse is unavailable: {e}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"web_browse failed: {e}"}
        if page.downloads:
            return {"content": "downloaded (saved under the k3code home): " + ", ".join(page.downloads)}
        verdict = await asyncio.to_thread(classify, page.status, page.html)
        if verdict in STOP_VERDICTS:
            return {"error": stop_report(verdict, url)}
        if verdict is not Verdict.OK:
            return {"error": f"web_browse: still blocked (HTTP {page.status}) at {url}; not solved, not retried"}
        title, text = await asyncio.to_thread(extract_text, page.html, page.url)
        note = "\n[page cut: the size cap was reached]" if page.truncated else ""
        head = f"# {(title or url)[:_TITLE_ROOM]}\n{page.url}\n\n"
        return {"content": head + _cap_page(text, note, limit=_page_room(budget, len(head)))}

    async def tool_search(arguments: dict[str, Any], *, cwd: Any = None) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"error": "web_search needs a query"}
        hits, notes, answered, unreachable = await fallback_search(
            query, int(arguments.get("limit") or 5), searx=searx, ddg=ddg, mcp=mcp
        )
        if hits:
            return {"content": "\n".join(f"- {h.title}\n  {h.url}\n  {h.snippet[:200]}" for h in hits)}
        if answered:
            return {"content": "no results"}
        if unreachable:  # setting up a backend would not help, and a retry fails the same way
            return {"error": "web_search failed: network unreachable (no backend answered); do not retry"}
        return {"error": f"web_search is disabled: {'; '.join(notes)}"}

    reg.register(
        ToolSpec(
            name="web_fetch",
            description="Fetch a web page and return its readable text. A page that blocks "
            "plain HTTP is read in a browser; CAPTCHAs, logins and paywalls stop.\n"
            f"Limits: http(s) only; text cut at {MAX_FETCH_CHARS} chars (start_index continues a long page); about "
            "20 s per page (research.fetch_deadline).",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "start_index": {"type": "integer", "description": "Character to continue a cut page from"},
                },
                "required": ["url"],
            },
            side_effect=False,
        ),
        tool_fetch,
    )
    if browser.available():  # without Playwright every call would only fail, and the schema costs tokens each request
        reg.register(
            ToolSpec(
                name="web_browse",
                description="Render a page in a real Chromium (for pages that need JavaScript) "
                "and return its readable text. Reads only: it never clicks, types or "
                "logs in. Needs the optional Playwright install.",
                parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                side_effect=False,
            ),
            tool_browse,
        )
    reg.register(
        ToolSpec(
            name="web_search",
            description="Search the web: SearXNG if configured, else keyless DuckDuckGo, "
            "else a connected MCP search tool. Returns titles, URLs and snippets.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
            side_effect=False,
        ),
        tool_search,
    )
