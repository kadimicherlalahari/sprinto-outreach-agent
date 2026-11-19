"""Stage 2a: company research agent loop.

Round 1  homepage + same-domain links that look like about/pricing/security/customers/careers
Round 2  sitemap.xml pages targeted at the still-open criteria (+ public PDFs)
Round 3  Tavily search for open criteria (funding/news) -> fetch the underlying credible article
Round 4  job boards linked from the company's own careers page
After every round: validate evidence, re-assess, and apply the stop rule (code, not the LLM).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from .evidence import Evidence, identity_check, is_recent, looks_like_injection, rule_extract, site_name, validate
from .fetch import FetchError, Page
from .llm import llm_extract
from .qualify import Assessment, Budget, assess, stop_decision
from .search import SearchUnavailable
from .util import get_logger, parse_date, registrable_domain, same_site

log = get_logger(__name__)

PAGE_KINDS = {
    "about": r"/(about|company|team|leadership|our-story|who-we-are)",
    "pricing": r"/(pricing|plans|enterprise)",
    "security": r"/(security|trust|compliance|privacy|soc-?2|iso|gdpr)",
    "customers": r"/(customers|case-studies|clients|success-stories)",
    "careers": r"/(careers|jobs|join-us|work-with-us|hiring)",
    "press": r"/(press|news|newsroom|blog/.*(funding|raise|series|announc)|announcements?)",
}
CRITERION_KINDS = {
    "is_b2b_saas": ["about", "pricing", "home"],
    "funding_stage": ["press", "about"],
    "recent_funding": ["press"],
    "enterprise_selling": ["pricing", "customers", "security", "home"],
    "compliance_in_progress": ["security"],
    "certified": ["security"],
    "compliance_hiring": ["careers"],
    "gdpr_exposure": ["security", "about"],
    "sensitive_data": ["about", "customers", "home"],
    "employee_count": ["about", "careers"],
}
LEGAL_PAGE = re.compile(r"/(privacy|terms|cookie|legal|dpa|eula|tos)", re.I)
ATS_HOSTS = ["boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
             "apply.workable.com", "careers.smartrecruiters.com"]


def page_kind(url: str) -> str:
    path = urlparse(url).path.lower()
    if path in ("", "/"):
        return "home"
    for kind, rx in PAGE_KINDS.items():
        if re.search(rx, path):
            return kind
    if path.endswith(".pdf"):
        return "pdf"
    return "other"


@dataclass
class ResearchResult:
    domain: str
    name: Optional[str]
    evidence: List[Evidence] = field(default_factory=list)
    assessment: Optional[Assessment] = None
    stop_reason: str = ""
    pages: Dict[str, Page] = field(default_factory=dict)
    identity: Tuple[str, str] = ("failed", "not checked")
    notes: List[str] = field(default_factory=list)
    stop_code: str = ""
    budget: Optional[Budget] = None
    audit: List[Tuple[str, str, str, str]] = field(default_factory=list)   # (kind, target, outcome, detail)
    checked_kinds: Set[str] = field(default_factory=set)                   # page kinds read successfully
    failed_kinds: Dict[str, str] = field(default_factory=dict)             # page kind -> failure kind
    site_error: Optional[str] = None                                        # homepage failure kind, if any

    def coverage(self) -> Dict[str, str]:
        """Per criterion: 'checked' (relevant page read, none failed) or 'failed:<kind>' or '' (never tried)."""
        out: Dict[str, str] = {}
        for crit, kinds in CRITERION_KINDS.items():
            failed = [self.failed_kinds[k] for k in kinds if k in self.failed_kinds]
            if failed:
                out[crit] = "failed:" + failed[0]
            elif any(k in self.checked_kinds for k in kinds):
                out[crit] = "checked"
        return out


class Researcher:
    def __init__(self, fetcher: Any, search: Any, llm: Optional[Any], icp: Dict[str, Any],
                 sources: Dict[str, Any], max_pages: int = 12, max_rounds: int = 4,
                 max_searches: int = 3, max_llm_calls: int = 12, max_seconds: float = 180.0, clock: Any = None,
                 cost_guard: Any = None):
        self.fetcher, self.search, self.llm = fetcher, search, llm
        self.icp, self.sources = icp, sources
        self.limits = dict(max_pages=max_pages, max_rounds=max_rounds, max_searches=max_searches,
                           max_llm_calls=max_llm_calls, max_seconds=max_seconds)
        self.clock = clock
        self.cost_guard = cost_guard
        self.max_pages, self.max_rounds = max_pages, max_rounds

    # ----- helpers -----
    def _fetch(self, url: str, res: ResearchResult, budget: Budget) -> Optional[Page]:
        if url in res.pages or url in self._seen_external:
            return res.pages.get(url)
        if budget.pages_used >= budget.max_pages or budget.elapsed() >= budget.max_seconds or budget.cost_stopped():
            res.audit.append(("page", url, "skipped_budget", ""))
            return None
        if registrable_domain(url) in (self.sources.get("demoted_domains") or []) and not same_site(url, res.domain):
            # reviewers flagged this third-party source as weak / wrong-company: not used as evidence
            res.audit.append(("page", url, "skipped_demoted", "source demoted by reviewer feedback"))
            return None
        budget.pages_used += 1
        kind = page_kind(url)
        try:
            page = self.fetcher.get(url)
            if page.likely_js_rendered:
                try:
                    page = self.fetcher.render(url)
                    res.notes.append("rendered JS page %s" % url)
                except FetchError as e:
                    # JS-only and not renderable: the page cannot be read, so it proves nothing either way
                    res.notes.append("%s: %s" % (url, e))
                    res.audit.append(("page", url, "js_only", str(e)))
                    res.failed_kinds.setdefault(kind, "js_only")
                    res.pages[url] = page
                    return page
        except FetchError as e:
            res.notes.append("skip %s: %s" % (url, e))
            res.audit.append(("page", url, getattr(e, "kind", "unavailable"), str(e)))
            if kind != "other":
                res.failed_kinds.setdefault(kind, getattr(e, "kind", "unavailable"))
            return None
        res.audit.append(("page", url, "ok", ""))
        if same_site(url, res.domain) or any(h in url for h in ATS_HOSTS):
            res.checked_kinds.add("careers" if any(h in url for h in ATS_HOSTS) else kind)
        if looks_like_injection(page.text):
            res.notes.append("%s contains instruction-like text; treated as data, not instructions" % url)
            res.audit.append(("page", url, "ok", "injection_suspected"))
        res.pages[url] = page
        return page

    def _extract(self, page: Page, res: ResearchResult, own_urls: Set[str]) -> None:
        kind = page_kind(page.url)
        if any(h in page.url for h in ATS_HOSTS):
            kind = "careers"
        cands = rule_extract(page, kind)
        if self.llm is not None:
            if self._budget.can_call_model():
                self._budget.llm_used += 1
                cands += llm_extract(self.llm, page, res.name, res.domain)
                res.audit.append(("model", page.url, "ok", "extraction"))
            elif not getattr(self, "_llm_budget_noted", False):
                self._llm_budget_noted = True
                res.notes.append("model-call budget reached; rule-based extraction only for remaining pages")
                res.audit.append(("model", page.url, "skipped_budget", ""))
        if LEGAL_PAGE.search(urlparse(page.url).path):
            # privacy/terms pages describe the company's own data handling, not its product or market
            cands = [c for c in cands if c.criterion not in ("sensitive_data", "is_b2b_saas", "enterprise_selling")]
        seen = {(e.criterion, e.value, e.source_url, e.quote) for e in res.evidence}
        for ev in cands:
            key = (ev.criterion, ev.value, ev.source_url, ev.quote)
            if key in seen:
                continue
            seen.add(key)
            res.evidence.append(validate(ev, page, res.name, res.domain, self.sources, own_urls))

    def _targets(self, links: List[str], domain: str, open_q: List[str], done: Set[str]) -> List[str]:
        kinds: List[str] = []
        for q in open_q or list(CRITERION_KINDS):
            for k in CRITERION_KINDS.get(q, []):
                if k not in kinds:
                    kinds.append(k)
        picked: List[str] = []
        for k in kinds:
            for l in links:
                if l in done or l in picked or not same_site(l, domain):
                    continue
                if page_kind(l) == k:
                    picked.append(l)
                    break
        return picked

    # ----- main loop -----
    def research(self, domain: str, name: Optional[str] = None, lead: Optional[Page] = None) -> ResearchResult:
        domain = registrable_domain(domain)
        res = ResearchResult(domain=domain, name=name)
        self._seen_external = {lead.url} if lead is not None else set()
        budget = Budget(clock=self.clock, cost_guard=self.cost_guard, **self.limits)
        if self.clock:
            budget.started = self.clock()
        self._budget = budget
        self._llm_budget_noted = False
        res.budget = budget
        own_urls: Set[str] = set()
        open_q: List[str] = []
        searched: Set[str] = set()
        sitemap_done = ats_done = False

        home = self._fetch("https://%s/" % domain, res, budget)
        res.identity = identity_check(name, domain, home, lead)
        if not res.name:
            res.name = site_name(home, domain)
            name = res.name
        home_fail = res.failed_kinds.get("home")
        if home is None or home_fail == "js_only":
            # Site blocked / unavailable / JS-only: we could not read it. Nothing is concluded from that.
            res.site_error = home_fail or "unavailable"
            res.stop_code = "site_" + res.site_error
            res.stop_reason = ("stopped: website %s; nothing concluded from it (retry later or add domains manually)"
                               % {"blocked": "blocked our requests", "js_only": "is JavaScript-only and could not be rendered",
                                  "not_found": "homepage not found", "rate_limited": "rate-limited us"}.get(res.site_error, "unavailable"))
            res.assessment = assess(res.evidence, self.icp, name, domain, coverage=res.coverage())
            return res
        if res.identity[0] == "failed":
            res.stop_reason = "stopped: company identity check failed (%s)" % res.identity[1]
            res.stop_code = "identity_failed"
            res.assessment = assess([], self.icp, name, domain)
            return res
        if lead is not None:
            self._extract(lead, res, own_urls)

        while True:
            budget.rounds_used += 1
            r = budget.rounds_used
            if r == 1:
                self._extract(home, res, own_urls)
                for url in self._targets(home.links, domain, [], set(res.pages)):
                    p = self._fetch(url, res, budget)
                    if p:
                        self._extract(p, res, own_urls)
                        if page_kind(url) == "careers":
                            own_urls.update(l for l in p.links if any(h in l for h in ATS_HOSTS))
            elif not sitemap_done:
                sitemap_done = True
                urls = self._sitemap_urls(domain, res, budget)
                for url in self._targets(urls, domain, open_q, set(res.pages))[:4]:
                    p = self._fetch(url, res, budget)
                    if p:
                        self._extract(p, res, own_urls)
            elif any(q in open_q for q in ("funding_stage", "recent_funding")) and "funding" not in searched:
                searched.add("funding")
                self._search_round(res, budget, own_urls,
                                   '"%s" funding round Series' % (name or domain), news=True)
            elif "compliance_hiring" in open_q and not ats_done:
                ats_done = True
                for url in list(own_urls)[:2]:
                    p = self._fetch(url, res, budget)
                    if p:
                        self._extract(p, res, own_urls)
            else:
                a = assess(res.evidence, self.icp, name, domain, coverage=res.coverage())
                res.assessment = a
                _, res.stop_reason, res.stop_code = stop_decision(a, budget, False)
                return self._finish(res)

            a = assess(res.evidence, self.icp, name, domain, coverage=res.coverage())
            res.assessment = a
            open_q = a.open_questions
            sources_left = (not sitemap_done) or ("funding" not in searched and budget.can_search()) or \
                (not ats_done and bool(own_urls))
            cont, reason, code = stop_decision(a, budget, sources_left)
            res.stop_reason, res.stop_code = reason, code
            if not cont:
                return self._finish(res)

    def _finish(self, res: ResearchResult) -> ResearchResult:
        """Flag undated, stale and conflicting evidence so the operator sees why a fact was discounted."""
        months = int(self.icp.get("recent_funding_months", 12))
        conflicted = {k for k, v in (res.assessment.criteria.items() if res.assessment else []) if v.status == "conflict"}
        for ev in res.evidence:
            if ev.status == "rejected":
                continue
            if ev.criterion == "funding_stage":
                if not parse_date(ev.published_at):
                    ev.flags.append("undated")
                elif not is_recent(ev.published_at, months):
                    ev.flags.append("stale")
            if ev.criterion in conflicted:
                ev.flags.append("conflict")
        return res

    def _sitemap_urls(self, domain: str, res: ResearchResult, budget: Budget) -> List[str]:
        urls: List[str] = []
        for sm in ("https://%s/sitemap.xml" % domain,):
            try:
                page = self.fetcher.get(sm)
            except FetchError as e:
                res.notes.append("sitemap: %s" % e)
                res.audit.append(("page", sm, getattr(e, "kind", "unavailable"), str(e)))
                continue
            budget.pages_used += 1
            res.audit.append(("page", sm, "ok", "sitemap"))
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", page.html or "")
            nested = [l for l in locs if l.endswith(".xml")][:2]
            urls += [l for l in locs if not l.endswith(".xml")]
            for n in nested:
                if budget.pages_used >= budget.max_pages:
                    break
                try:
                    sub = self.fetcher.get(n)
                    budget.pages_used += 1
                    urls += [l for l in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sub.html or "") if not l.endswith(".xml")]
                except FetchError:
                    continue
        return urls[:2000]

    def _search_round(self, res: ResearchResult, budget: Budget, own_urls: Set[str], query: str, news: bool) -> None:
        if not budget.can_search():
            res.notes.append("search skipped: search budget reached")
            res.audit.append(("search", query, "skipped_budget", ""))
            return
        budget.searches_used += 1
        tier_domains = [d for t in (2,) for d in (self.sources.get("tiers", {}).get(t) or [])]
        try:
            hits = self.search.search(query, max_results=5, include_domains=tier_domains + [res.domain],
                                      topic="news" if news else "general")
        except SearchUnavailable as e:
            res.notes.append("search skipped: %s" % e)
            res.audit.append(("search", query, getattr(e, "kind", "outage"), str(e)))
            res.failed_kinds.setdefault("press", getattr(e, "kind", "outage"))
            return
        res.audit.append(("search", query, "ok", "%d results" % len(hits)))
        for h in hits[:3]:
            # The snippet is a lead only; fetch the article and validate quotes against it.
            p = self._fetch(h.url, res, budget)
            if p:
                if not p.published_at and h.published_date:
                    res.notes.append("%s: page has no publication date; search-provided date not used as evidence" % h.url)
                self._extract(p, res, own_urls)
