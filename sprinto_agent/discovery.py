"""Stage 1: discover candidate companies and shortlist them.

Inputs (all optional, combined):
  * operator-supplied domain list
  * RSS feeds of credible publications (sources.yaml)
  * accelerator / VC portfolio pages (sources.yaml)
  * Tavily search queries (leads only)
A lead article is fetched before any claim from it is used. The company's domain is taken only from a
link in the article / portfolio page whose host matches the company name — never guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .evidence import Evidence, is_recent, rule_extract, source_tier, validate
from .fetch import FetchError, Page
from .qualify import is_competitor
from .search import SearchUnavailable
from .util import get_logger, host_matches, registrable_domain

log = get_logger(__name__)

NON_COMPANY_HOSTS = ["facebook.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "instagram.com",
                     "crunchbase.com", "tracxn.com", "apple.com", "google.com", "play.google.com", "medium.com",
                     "github.com", "wikipedia.org", "t.co", "bit.ly", "wa.me", "whatsapp.com", "t.me"]
_TITLE_NAME = re.compile(r"^(?:[A-Z][\w-]*\s(?:startup|platform|firm)\s)?(?P<name>[A-Z][\w&.\-]*(?:\s[A-Z][\w&.\-]*){0,3})\s+"
                         r"(?:raises|secures|bags|lands|closes|gets|announces|nets|picks up)\b")


@dataclass
class Candidate:
    name: Optional[str]
    domain: Optional[str]
    found_via: str
    lead_url: Optional[str] = None
    lead_page: Optional[Page] = None
    stage1_status: str = "uncertain"
    stage1_reason: str = ""
    lead_evidence: List[Evidence] = field(default_factory=list)


def name_from_title(title: str) -> Optional[str]:
    m = _TITLE_NAME.search(title.strip())
    return m.group("name").strip() if m else None


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def domain_from_links(name: str, links: List[str], exclude_host: str) -> Optional[str]:
    target = _compact(name)
    if len(target) < 3:
        return None
    found = []
    for l in links:
        host = registrable_domain(l)
        if not host or host_matches(host, exclude_host) or any(host_matches(host, h) for h in NON_COMPANY_HOSTS):
            continue
        root = _compact(host.split(".")[0])
        if root == target or (len(root) >= 4 and (root.startswith(target) or target.startswith(root))):
            found.append(host)
    return found[0] if found else None


def parse_feed(xml: str) -> List[Dict[str, str]]:
    items = []
    for block in re.findall(r"<item\b.*?</item>", xml, re.S | re.I):
        def tag(t: str) -> str:
            m = re.search(r"<%s\b[^>]*>(.*?)</%s>" % (t, t), block, re.S | re.I)
            v = (m.group(1) if m else "").strip()
            return re.sub(r"^<!\[CDATA\[|\]\]>$", "", v).strip()
        items.append({"title": re.sub(r"<[^>]+>", "", tag("title")), "link": tag("link"), "date": tag("pubDate")})
    return items


class Discoverer:
    def __init__(self, fetcher: Any, search: Any, icp: Dict[str, Any], sources: Dict[str, Any],
                 notes: Optional[List[str]] = None):
        self.fetcher, self.search, self.icp, self.sources = fetcher, search, icp, sources
        self.notes = notes if notes is not None else []

    def discover(self, domains: Optional[List[str]] = None, use_feeds: bool = True, use_search: bool = True,
                 use_portfolios: bool = True, now: Optional[datetime] = None) -> List[Candidate]:
        cfg = self.sources.get("discovery", {}) or {}
        limit = int(cfg.get("max_candidates", 15))
        cands: List[Candidate] = []
        for d in domains or []:
            d = registrable_domain(d)
            if d:
                cands.append(Candidate(None, d, "operator-supplied domain list"))
        if use_feeds:
            for feed in cfg.get("feeds", []) or []:
                cands += self._from_feed(feed)
        if use_portfolios:
            for url in cfg.get("portfolio_pages", []) or []:
                cands += self._from_portfolio(url)
        if use_search:
            month_year = (now or datetime.utcnow()).strftime("%B %Y")
            for q in cfg.get("search_queries", []) or []:
                cands += self._from_search(q.replace("{month_year}", month_year))
        # de-duplicate by domain (or name if no domain)
        seen, uniq = set(), []
        for c in cands:
            key = c.domain or ("name:" + (c.name or "").lower())
            if key in seen:
                continue
            seen.add(key)
            uniq.append(c)
        for c in uniq:
            self.shortlist(c, now)
        order = {"shortlisted": 0, "uncertain": 1, "rejected": 2}
        uniq.sort(key=lambda c: order[c.stage1_status])
        kept = [c for c in uniq if c.stage1_status != "rejected"][:limit]
        return kept + [c for c in uniq if c.stage1_status == "rejected"]

    # ---- sources ----
    def _lead_from_article(self, title: str, url: str, via: str) -> Optional[Candidate]:
        name = name_from_title(title)
        if not name:
            return None
        cand = Candidate(name, None, via, lead_url=url)
        try:
            page = self.fetcher.get(url)
        except FetchError as e:
            cand.stage1_reason = "lead article could not be fetched (%s)" % e
            return cand
        cand.lead_page = page
        cand.domain = domain_from_links(name, page.links, registrable_domain(url))
        return cand

    def _from_feed(self, feed_url: str) -> List[Candidate]:
        try:
            page = self.fetcher.get(feed_url)
        except FetchError as e:
            self.notes.append("feed %s skipped: %s" % (feed_url, e))
            return []
        out = []
        for it in parse_feed(page.html)[:30]:
            if not re.search(r"\b(raises|secures|funding|series [a-h]|seed|round)\b", it["title"], re.I):
                continue
            c = self._lead_from_article(it["title"], it["link"], "RSS: %s" % registrable_domain(feed_url))
            if c:
                out.append(c)
        return out

    def _from_portfolio(self, url: str) -> List[Candidate]:
        try:
            page = self.fetcher.get(url)
        except FetchError as e:
            self.notes.append("portfolio %s skipped: %s" % (url, e))
            return []
        host = registrable_domain(url)
        out = []
        for l in page.links:
            h = registrable_domain(l)
            if h and not host_matches(h, host) and not any(host_matches(h, x) for x in NON_COMPANY_HOSTS):
                out.append(Candidate(None, h, "portfolio page %s" % host, lead_url=url))
        return out[:40]

    def _from_search(self, query: str) -> List[Candidate]:
        tier2 = list(self.sources.get("tiers", {}).get(2) or [])
        try:
            hits = self.search.search(query, max_results=8, include_domains=tier2, topic="news", days=120)
        except SearchUnavailable as e:
            self.notes.append("search '%s' skipped: %s" % (query, e))
            return []
        out = []
        for h in hits:
            c = self._lead_from_article(h.title, h.url, "Tavily search: %s" % query)
            if c:
                out.append(c)
        return out

    # ---- shortlisting ----
    def shortlist(self, c: Candidate, now: Optional[datetime] = None) -> Candidate:
        never = self.sources.get("never_fetch", []) or []
        if c.domain and any(host_matches(c.domain, d) for d in never + NON_COMPANY_HOSTS):
            c.stage1_status, c.stage1_reason = "rejected", "domain is a platform/social site, not a company site"
            return c
        if is_competitor(c.name, c.domain or "", self.icp):
            c.stage1_status, c.stage1_reason = "rejected", "compliance-automation vendor (competitor)"
            return c
        if c.found_via == "operator-supplied domain list":
            c.stage1_status, c.stage1_reason = "shortlisted", "supplied by operator; qualification decided in research"
            return c
        if c.found_via.startswith("portfolio"):
            c.stage1_status, c.stage1_reason = "uncertain", "listed on portfolio page; no stage/compliance signal yet"
            return c
        if not c.domain:
            c.stage1_status = "uncertain"
            c.stage1_reason = c.stage1_reason or "company website not linked from the lead article; domain not guessed"
            return c
        if c.lead_page is None:
            c.stage1_status = "uncertain"
            return c
        tier = source_tier(c.lead_page.url, c.domain, self.sources)
        evs = [validate(e, c.lead_page, c.name, c.domain, self.sources) for e in rule_extract(c.lead_page, "press")]
        c.lead_evidence = [e for e in evs if e.status != "rejected"]
        stages = {e.value for e in c.lead_evidence if e.criterion == "funding_stage"}
        allowed = set(self.icp.get("must_have", {}).get("funding_stage", {}).get("allowed", []))
        rejected = set(self.icp.get("must_have", {}).get("funding_stage", {}).get("rejected", []))
        months = int(self.icp.get("recent_funding_months", 12))
        if stages & rejected and not stages & allowed:
            c.stage1_status, c.stage1_reason = "rejected", "lead (tier %s) reports %s, outside ICP" % (tier, sorted(stages))
        elif stages & allowed:
            dated = any(is_recent(e.published_at, months, now) for e in c.lead_evidence if e.criterion == "funding_stage")
            c.stage1_status = "shortlisted"
            c.stage1_reason = "lead (tier %s) reports %s%s" % (tier, sorted(stages & allowed),
                                                               ", dated within window" if dated else ", date unverified")
        else:
            c.stage1_status, c.stage1_reason = "uncertain", "lead found; no ICP funding stage stated in the article"
        return c
