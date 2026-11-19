"""Polite, bounded HTTP fetching. Respects robots.txt, per-host delay, timeouts, size caps, retries.
Never fetches access-controlled domains (LinkedIn, search engines). Pages are untrusted data."""
from __future__ import annotations

import io
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from .util import get_logger, host_matches, now_iso, registrable_domain

log = get_logger(__name__)
MAX_BYTES = 3_000_000


class FetchError(Exception):
    """kind: blocked | unavailable | not_found | js_only | rate_limited | unsupported.
    A FetchError means "could not check", never "the fact is absent"."""

    def __init__(self, message: str = "failed", kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


class NetworkDisabled(FetchError):
    pass


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    text: str                      # visible text (html stripped / pdf extracted)
    html: str = ""
    retrieved_at: str = field(default_factory=now_iso)
    published_at: Optional[str] = None
    links: List[str] = field(default_factory=list)
    structured: List[Dict[str, Any]] = field(default_factory=list)   # JSON-LD blocks
    likely_js_rendered: bool = False


def html_to_page(url: str, status: int, html: str, content_type: str = "text/html") -> Page:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    structured = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
            structured.extend(data if isinstance(data, list) else [data])
        except (ValueError, TypeError):
            continue
    published = None
    for attrs in ({"property": "article:published_time"}, {"name": "date"}, {"itemprop": "datePublished"},
                  {"name": "pubdate"}, {"property": "og:published_time"}):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content"):
            published = m["content"]
            break
    if not published:
        t = soup.find("time", attrs={"datetime": True})
        if t:
            published = t["datetime"]
    if not published:
        for block in structured:
            if isinstance(block, dict) and block.get("datePublished"):
                published = str(block["datePublished"])
                break
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        if href.startswith("http"):
            links.append(href)
    for t in soup(["script", "style", "noscript", "svg", "template", "head"]):
        t.decompose()
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in soup.get_text("\n").split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    js = len(text) < 400 and bool(re.search(r"__NEXT_DATA__|id=[\"']?(root|app|__next)[\"']?[\s>]|ng-app|data-reactroot", html))
    return Page(url=url, status=status, content_type=content_type, text=text, html=html,
                published_at=published, links=list(dict.fromkeys(links)), structured=structured,
                likely_js_rendered=js)


def pdf_to_text(data: bytes, max_pages: int = 20) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for i, p in enumerate(reader.pages):
        if i >= max_pages:
            break
        parts.append(p.extract_text() or "")
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


class Fetcher:
    """Real fetcher. One instance per run; shared robots cache and per-host clock."""

    def __init__(self, user_agent: str, timeout: float = 15.0, per_host_delay: float = 1.5,
                 never_fetch: Optional[List[str]] = None, retries: int = 2):
        import httpx
        self.ua = user_agent
        self.client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": user_agent},
                                   limits=httpx.Limits(max_connections=5))
        self.delay = per_host_delay
        self.never = [registrable_domain(d) for d in (never_fetch or [])]
        self.retries = retries
        self._robots: Dict[str, Optional[robotparser.RobotFileParser]] = {}
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.count = 0

    def close(self) -> None:
        self.client.close()

    def blocked_domain(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return any(host_matches(host, d) for d in self.never)

    def _robots_for(self, url: str) -> Optional[robotparser.RobotFileParser]:
        p = urlparse(url)
        base = "%s://%s" % (p.scheme, p.netloc)
        if base in self._robots:
            return self._robots[base]
        rp = robotparser.RobotFileParser()
        try:
            r = self.client.get(base + "/robots.txt")
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except Exception:
            rp = None  # unknown -> treat as allowed but note it
        self._robots[base] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if self.blocked_domain(url):
            return False
        rp = self._robots_for(url)
        return True if rp is None else rp.can_fetch(self.ua, url)

    def _wait(self, host: str) -> None:
        with self._lock:
            last = self._last.get(host, 0.0)
            wait = self.delay - (time.monotonic() - last)
            self._last[host] = time.monotonic() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str) -> Page:
        if not url.startswith(("http://", "https://")):
            raise FetchError("unsupported url scheme", "unsupported")
        if self.blocked_domain(url):
            raise FetchError("domain is on never_fetch list (access-controlled)", "blocked")
        if not self.allowed(url):
            raise FetchError("disallowed by robots.txt", "blocked")
        host = urlparse(url).hostname or ""
        last_err = None
        for attempt in range(self.retries + 1):
            self._wait(host)
            try:
                with self.client.stream("GET", url) as r:
                    if self.blocked_domain(str(r.url)) or any(self.blocked_domain(str(h.url)) for h in r.history):
                        # a redirect must not carry us into an access-controlled site
                        raise FetchError("redirected to a never_fetch domain (access-controlled); not used", "blocked")
                    if r.status_code in (401, 402, 403, 407, 451):
                        raise FetchError("access restricted (HTTP %d); not bypassing" % r.status_code, "blocked")
                    if r.status_code == 429 or r.status_code >= 500:
                        last_err = FetchError("HTTP %d" % r.status_code,
                                              "rate_limited" if r.status_code == 429 else "unavailable")
                        time.sleep(2 * (attempt + 1))
                        continue
                    if r.status_code in (404, 410):
                        raise FetchError("HTTP %d" % r.status_code, "not_found")
                    if r.status_code >= 400:
                        raise FetchError("HTTP %d" % r.status_code, "unavailable")
                    buf = bytearray()
                    for chunk in r.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_BYTES:
                            del buf[MAX_BYTES:]       # keep at most MAX_BYTES even if one chunk overshoots
                            break
                    ctype = r.headers.get("content-type", "").lower()
                    self.count += 1
                    final = str(r.url)
                    if "pdf" in ctype or final.lower().endswith(".pdf"):
                        return Page(url=final, status=r.status_code, content_type="application/pdf",
                                    text=pdf_to_text(bytes(buf)))
                    if "xml" in ctype and "html" not in ctype:
                        return Page(url=final, status=r.status_code, content_type=ctype,
                                    text="", html=buf.decode("utf-8", "replace"))
                    return html_to_page(final, r.status_code, buf.decode(r.encoding or "utf-8", "replace"), ctype)
            except FetchError:
                raise
            except Exception as e:  # network errors -> retry
                last_err = FetchError(type(e).__name__, "unavailable")
                time.sleep(1 + attempt)
        raise last_err or FetchError("failed", "unavailable")

    def render(self, url: str) -> Page:
        """Rendered-browser fallback, only for JS-rendered pages. Optional dependency (playwright)."""
        if not self.allowed(url):
            raise FetchError("disallowed by robots.txt", "blocked")
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            raise FetchError("page appears JS-rendered; playwright not installed so it was not rendered", "js_only")
        self._wait(urlparse(url).hostname or "")
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(user_agent=self.ua)
            pg.goto(url, timeout=20000)
            html = pg.content()
            b.close()
        self.count += 1
        return html_to_page(url, 200, html)


class MockFetcher:
    """Serves fixture pages only. Any other URL -> FetchError. Makes no network calls."""

    def __init__(self, pages: Dict[str, str]):
        self.pages = pages
        self.count = 0

    def close(self) -> None:
        pass

    def allowed(self, url: str) -> bool:
        return True

    def get(self, url: str) -> Page:
        key = url.rstrip("/")
        for k in (url, key, key + "/"):
            if k in self.pages:
                self.count += 1
                body = self.pages[k]
                if isinstance(body, FetchError):
                    raise body
                if k.endswith(".xml"):
                    return Page(url=url, status=200, content_type="application/xml", text="", html=body)
                return html_to_page(url, 200, body)
        raise FetchError("HTTP 404 (mock)", "not_found")

    def render(self, url: str) -> Page:
        raise FetchError("page appears JS-rendered; rendering not available in mock mode", "js_only")
