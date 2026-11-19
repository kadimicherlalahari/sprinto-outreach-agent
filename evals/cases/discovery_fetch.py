"""D05-D08, D14, D40: discovery de-duplication, stage-1 shortlist, fetch policy, source tiers, unreadable sites,
unsafe links. HTTP cases use the real Fetcher over an in-process httpx MockTransport (no sockets)."""
from __future__ import annotations

from contextlib import contextmanager

from evals.fixtures import ICP, days_ago, page
from evals.harness import Case
from sprinto_agent.discovery import Candidate, Discoverer
from sprinto_agent.fetch import FetchError, MockFetcher
from sprinto_agent.mock_data import mock_pages, mock_sources
from sprinto_agent.search import MockSearch, SearchHit, SearchUnavailable

CASES = []


def C(**k):
    CASES.append(Case(**k))


def _disc(pages=None, sources=None, search=None, **kw):
    p = mock_pages()
    p.update(pages or {})
    notes = []
    d = Discoverer(MockFetcher(p), search or MockSearch({}), ICP, sources or mock_sources(), notes)
    return d, notes


# ------------------------------------------------------------------ D05 de-duplication
def _dedupe_forms(env):
    d, _ = _disc()
    cs = d.discover(domains=["ledgerly.example", "https://www.ledgerly.example/about", "LEDGERLY.example"],
                    use_feeds=False, use_search=False, use_portfolios=False)
    return {"candidates": len(cs), "domains": sorted({c.domain for c in cs})}


C(id="D05-01", decision="D05", partition="same domain written three ways", input="domains: ledgerly.example, https://www.ledgerly.example/about, LEDGERLY.example",
  expected="one candidate", assertion="candidates=1", severity="High", run=_dedupe_forms,
  expect={"candidates": 1, "domains": ["ledgerly.example"]})


def _dedupe_sources(env):
    d, _ = _disc()
    cs = d.discover(domains=["ledgerly.example"], use_feeds=True, use_search=False, use_portfolios=False)
    led = [c for c in cs if c.domain == "ledgerly.example"]
    return {"ledgerly_candidates": len(led), "kept_source": led[0].found_via if led else None}


C(id="D05-02", decision="D05", partition="same company from operator list and news feed", input="operator domain ledgerly.example + feed article linking ledgerly.example",
  expected="one candidate; the operator-supplied entry wins (listed first)", assertion="ledgerly_candidates=1",
  severity="High", run=_dedupe_sources, expect={"ledgerly_candidates": 1, "kept_source": "operator-supplied domain list"})


def _cap(env, n_domains):
    src = mock_sources()
    src["discovery"] = dict(src["discovery"], max_candidates=2)
    d, _ = _disc(sources=src)
    doms = ["a%d.example" % i for i in range(n_domains)]
    cs = d.discover(domains=doms, use_feeds=False, use_search=False, use_portfolios=False)
    return {"kept": len([c for c in cs if c.stage1_status != "rejected"])}


for b, n, kept in (("below", 1, 1), ("at", 2, 2), ("above", 3, 2)):
    C(id="D05-%02d" % (3 + ("below", "at", "above").index(b)), decision="D05", partition="max_candidates cap", boundary=b,
      input="max_candidates=2, %d operator domains" % n, expected="at most 2 candidates kept for research",
      assertion="kept=%d" % kept, severity="Low", run=lambda e, n=n: _cap(e, n), expect={"kept": kept})


# ------------------------------------------------------------------ D06 shortlist
def _sl(env, cand, lead=None, pages=None):
    d, _ = _disc(pages=pages)
    if lead:
        cand.lead_page = MockFetcher({**mock_pages(), **(pages or {})}).get(lead)
    d.shortlist(cand)
    return {"status": cand.stage1_status, "domain": cand.domain}


C(id="D06-01", decision="D06", partition="operator-supplied", input="Candidate(None, 'acme.example', 'operator-supplied domain list')",
  expected="shortlisted; decided in research", assertion="status=shortlisted", severity="Low",
  run=lambda e: _sl(e, Candidate(None, "acme.example", "operator-supplied domain list")), expect={"status": "shortlisted"})
C(id="D06-02", decision="D06", partition="competitor", input="Candidate('Drata', 'drata.example', 'operator-supplied domain list')",
  expected="rejected as competitor", assertion="status=rejected", severity="High",
  run=lambda e: _sl(e, Candidate("Drata", "drata.example", "operator-supplied domain list")), expect={"status": "rejected"})
C(id="D06-03", decision="D06", partition="platform / social domain", input="Candidate(None, 'linkedin.com', 'RSS: news.example')",
  expected="rejected, never researched", assertion="status=rejected", severity="Critical",
  run=lambda e: _sl(e, Candidate(None, "linkedin.com", "RSS: news.example")), expect={"status": "rejected"})
C(id="D06-04", decision="D06", partition="lead reports out-of-ICP stage", input="Carelink lead article (Series D, tier 2)",
  expected="rejected", assertion="status=rejected", severity="High",
  run=lambda e: _sl(e, Candidate("Carelink", "carelink.example", "RSS: news.example"), "https://news.example/carelink-series-d"),
  expect={"status": "rejected"})
C(id="D06-05", decision="D06", partition="lead reports in-ICP stage", input="Ledgerly lead article (Series B, tier 2, recent)",
  expected="shortlisted", assertion="status=shortlisted", severity="Low",
  run=lambda e: _sl(e, Candidate("Ledgerly", "ledgerly.example", "RSS: news.example"), "https://news.example/ledgerly-series-b"),
  expect={"status": "shortlisted"})
C(id="D06-06", decision="D06", partition="lead with no company link", input="article names 'Nolink' but links no company site",
  expected="uncertain; domain NOT guessed", assertion="status=uncertain, domain=None", severity="Critical",
  run=lambda e: _sl(e, Candidate("Nolink", None, "RSS: news.example")), expect={"status": "uncertain", "domain": None})


def _feed_down(env):
    d, notes = _disc(pages={"https://news.example/feed": FetchError("timed out", "unavailable")})
    cs = d.discover(domains=["ledgerly.example"], use_feeds=True, use_search=False, use_portfolios=False)
    return {"candidates": len(cs), "noted": any("feed" in n and "skipped" in n for n in notes)}


C(id="D06-07", decision="D06", partition="source timeout / outage (feed)", input="news feed raises FetchError('timed out')",
  expected="feed skipped with a note; other sources still used; no crash", assertion="candidates=1, noted=True",
  severity="High", run=_feed_down, expect={"candidates": 1, "noted": True})


class DownSearch:
    calls = 0

    def search(self, *a, **k):
        raise SearchUnavailable("Tavily HTTP 503", "outage")


def _search_down(env):
    d, notes = _disc(search=DownSearch())
    cs = d.discover(domains=["ledgerly.example"], use_feeds=False, use_search=True, use_portfolios=False)
    return {"candidates": len(cs), "noted": any("search" in n and "skipped" in n for n in notes)}


C(id="D06-08", decision="D06", partition="search provider outage", input="search raises SearchUnavailable(503)",
  expected="search skipped with a note; operator domains still processed", assertion="candidates=1, noted=True",
  severity="High", run=_search_down, expect={"candidates": 1, "noted": True})


def _lead_fetch_fails(env):
    d, _ = _disc(pages={"https://news.example/x": FetchError("HTTP 503", "unavailable")})
    c = d._lead_from_article("Zeta raises Series A", "https://news.example/x", "RSS: news.example")
    d.shortlist(c)
    return {"status": c.stage1_status, "domain": c.domain}


C(id="D06-09", decision="D06", partition="lead article unreachable", input="lead article returns 503",
  expected="kept as uncertain, no domain guessed", assertion="status=uncertain, domain=None", severity="High",
  run=_lead_fetch_fails, expect={"status": "uncertain", "domain": None})


# ------------------------------------------------------------------ D07 fetch policy (real Fetcher, fake transport)
@contextmanager
def _fetcher(handler, never=("linkedin.com",)):
    import httpx
    import sprinto_agent.fetch as F
    real_sleep = F.time.sleep
    F.time.sleep = lambda s: None
    f = F.Fetcher("EvalBot/1.0", per_host_delay=0, never_fetch=list(never), retries=1)
    f.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    try:
        yield f
    finally:
        F.time.sleep = real_sleep
        f.close()


def _get(url, handler, never=("linkedin.com",)):
    with _fetcher(handler, never) as f:
        try:
            p = f.get(url)
            return {"ok": True, "url": p.url, "text_len": len(p.text)}
        except FetchError as e:
            return {"ok": False, "kind": e.kind}


def H(status=200, body="<html><body><p>Hello world from Acme.</p></body></html>", robots="User-agent: *\nAllow: /",
      ctype="text/html", redirect=None, raise_exc=None):
    import httpx

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if raise_exc:
            raise raise_exc
        if redirect and req.url.path == "/old":
            return httpx.Response(301, headers={"location": redirect})
        return httpx.Response(status, content=body.encode() if isinstance(body, str) else body,
                              headers={"content-type": ctype})
    return handler


C(id="D07-01", decision="D07", partition="unsafe scheme", input="get('javascript:alert(1)')", expected="refused, no request",
  assertion="kind=unsupported", severity="Critical", run=lambda e: _get("javascript:alert(1)", H()), expect={"ok": False, "kind": "unsupported"})
C(id="D07-02", decision="D07", partition="access-controlled domain", input="get('https://www.linkedin.com/in/someone')",
  expected="refused (never_fetch)", assertion="kind=blocked", severity="Critical",
  run=lambda e: _get("https://www.linkedin.com/in/someone", H()), expect={"ok": False, "kind": "blocked"})
C(id="D07-03", decision="D07", partition="robots.txt disallow", input="robots: 'Disallow: /' then get /about",
  expected="refused", assertion="kind=blocked", severity="Critical",
  run=lambda e: _get("https://acme.example/about", H(robots="User-agent: *\nDisallow: /")), expect={"ok": False, "kind": "blocked"})
for i, (st, kind, sev) in enumerate(((401, "blocked", "High"), (403, "blocked", "High"), (451, "blocked", "High"),
                                      (404, "not_found", "Low"), (410, "not_found", "Low"), (429, "rate_limited", "High"),
                                      (500, "unavailable", "High"), (503, "unavailable", "High")), 4):
    C(id="D07-%02d" % i, decision="D07", partition="HTTP %d" % st, input="server returns HTTP %d" % st,
      expected="FetchError(%s); login/paywall not bypassed" % kind if kind == "blocked" else "FetchError(%s)" % kind,
      assertion="kind=%s" % kind, severity=sev, run=lambda e, st=st: _get("https://acme.example/p", H(status=st)),
      expect={"ok": False, "kind": kind})


def _timeout(env):
    import httpx
    return _get("https://acme.example/p", H(raise_exc=httpx.ReadTimeout("slow")))


C(id="D07-12", decision="D07", partition="timeout", input="transport raises ReadTimeout on every attempt",
  expected="FetchError(unavailable) after retries", assertion="kind=unavailable", severity="High", run=_timeout,
  expect={"ok": False, "kind": "unavailable"})
C(id="D07-13", decision="D07", partition="redirect to same site", input="/old -> 301 -> https://acme.example/new",
  expected="followed; final URL recorded", assertion="url=https://acme.example/new", severity="Low",
  run=lambda e: _get("https://acme.example/old", H(redirect="https://acme.example/new")),
  expect={"ok": True, "url": "https://acme.example/new"})
C(id="D07-14", decision="D07", partition="redirect into access-controlled domain", input="/old -> 301 -> https://www.linkedin.com/company/acme",
  expected="refused: content from a never_fetch domain is not used even via redirect", assertion="kind=blocked",
  severity="Critical", run=lambda e: _get("https://acme.example/old", H(redirect="https://www.linkedin.com/company/acme")),
  expect={"ok": False, "kind": "blocked"})
C(id="D07-15", decision="D07", partition="empty page", input="HTTP 200 with empty body",
  expected="page returned with no text; nothing extracted from it", assertion="ok=True, text_len=0", severity="Low",
  run=lambda e: _get("https://acme.example/p", H(body="")), expect={"ok": True, "text_len": 0})


def _big(env):
    body = "<html><body><p>" + ("x" * 4_000_000) + "</p></body></html>"
    r = _get("https://acme.example/p", H(body=body))
    return {"ok": r["ok"], "capped": r.get("text_len", 0) <= 3_000_001}


C(id="D07-16", decision="D07", partition="oversized page", input="HTTP 200 with a 4 MB body",
  expected="read stops at the 3 MB cap", assertion="text_len <= 3,000,001", severity="Low", run=_big,
  expect={"ok": True, "capped": True})


# ------------------------------------------------------------------ D08 source tier
def _tier(env, url, own=None):
    from sprinto_agent.evidence import source_tier
    return source_tier(url, "ledgerly.example", mock_sources(), own)


for i, (url, tier, part, own) in enumerate((
        ("https://ledgerly.example/about", "1", "company's own domain", None),
        ("https://careers.ledgerly.example/jobs", "1", "own subdomain", None),
        ("https://jobs.lever.co/ledgerly/1", "1", "job board linked from own careers page", {"https://jobs.lever.co/ledgerly/1"}),
        ("https://jobs.lever.co/other/1", "lead", "job board NOT linked from own site", None),
        ("https://news.example/x", "2", "tier-2 publication", None),
        ("https://randomblog.example/x", "lead", "unlisted site", None),
        ("https://ledgerly.example.evil.example/x", "lead", "look-alike host", None)), 1):
    C(id="D08-%02d" % i, decision="D08", partition=part, input=url, expected="tier %s" % tier, assertion="source_tier == %r" % tier,
      severity="High" if tier == "lead" else "Low", run=lambda e, u=url, o=own: _tier(e, u, o), expect=tier)


# ------------------------------------------------------------------ D14 unreadable site
def _site(env, home):
    r = env.run(deps=env.deps(pages={"https://ledgerly.example/": home}))
    co = env.company("ledgerly.example")
    return {"qualification": co["qualification"], "stage": co["research_stage"], "retryable": bool(co["retryable"]),
            "error_kind": co["error_kind"], "evidence": env.count("SELECT id FROM evidence")}


C(id="D14-01", decision="D14", partition="site blocks us", input="homepage raises FetchError(blocked)",
  expected="nothing concluded; uncertain; retryable; stays pending", assertion="qualification=uncertain, retryable, stage=pending",
  severity="High", run=lambda e: _site(e, FetchError("HTTP 403", "blocked")),
  expect={"qualification": "uncertain", "stage": "pending", "retryable": True, "error_kind": "blocked", "evidence": 0})
C(id="D14-02", decision="D14", partition="site down", input="homepage raises FetchError(unavailable)",
  expected="same as blocked; not treated as absence of evidence", assertion="uncertain, retryable", severity="High",
  run=lambda e: _site(e, FetchError("HTTP 503", "unavailable")),
  expect={"qualification": "uncertain", "stage": "pending", "retryable": True, "error_kind": "unavailable"})
C(id="D14-03", decision="D14", partition="JavaScript-only page", input="homepage is an empty JS shell (<div id=root>, scripts) and rendering is unavailable",
  expected="recorded as js_only; uncertain; retryable", assertion="error_kind=js_only", severity="High",
  run=lambda e: _site(e, "<html><head><script src='a.js'></script><script src='b.js'></script><script src='c.js'></script></head><body><div id='root'></div></body></html>"),
  expect={"qualification": "uncertain", "retryable": True, "error_kind": "js_only"})


def _one_fails_others_continue(env):
    env.run(deps=env.deps(pages={"https://quietforms.example/": FetchError("HTTP 503", "unavailable")}),
            domains=["quietforms.example", "ledgerly.example"])
    return {"ledgerly": env.company("ledgerly.example")["qualification"],
            "quietforms_retryable": bool(env.company("quietforms.example")["retryable"])}


C(id="D14-04", decision="D14", partition="one company's site fails", input="quietforms homepage 503; ledgerly normal",
  expected="other companies continue", assertion="ledgerly qualified, quietforms retryable", severity="High",
  run=_one_fails_others_continue, expect={"ledgerly": "qualified", "quietforms_retryable": True})


# ------------------------------------------------------------------ D40 unsafe links in the UI
def _href(env, url):
    from sprinto_agent.web import safe_href
    return safe_href(url)


for i, (url, out) in enumerate((("https://acme.example/a?b=1", "https://acme.example/a?b=1"), ("javascript:alert(1)", "#"),
                                 ("data:text/html,<script>", "#"), ("  JAVASCRIPT:alert(1)", "#"), ("http://acme.example", "http://acme.example"),
                                 ('https://acme.example/"onmouseover="x', "https://acme.example/&quot;onmouseover=&quot;x")), 1):
    C(id="D40-%02d" % i, decision="D40", partition="hostile link" if out.startswith("#") or "&quot;" in out else "normal link",
      input=repr(url), expected="rendered inert or escaped" if out != url else "rendered as-is",
      assertion="safe_href == %r" % out, severity="High", run=lambda e, u=url: _href(e, u), expect=out)
