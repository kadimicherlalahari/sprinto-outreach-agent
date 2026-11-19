"""MOCK MODE FIXTURES — ENTIRELY FAKE. All companies, people and articles below are invented and use
reserved .example domains. Mock runs make no network calls and write to a separate SQLite file."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from .config import load_icp
from .fetch import MockFetcher
from .llm import MockLLM
from .contacts import MockHunter
from .search import MockSearch, SearchHit

FAKE = "[FAKE MOCK DATA]"


def _d(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _page(title: str, body: str, published: str = "") -> str:
    meta = '<meta property="article:published_time" content="%s">' % published if published else ""
    return "<html><head><title>%s %s</title>%s</head><body>%s</body></html>" % (title, FAKE, meta, body)


def mock_pages() -> Dict[str, str]:
    recent, old = _d(30), _d(900)
    return {
        # ---- news (fake tier-2 publication) ----
        "https://news.example/feed": """<rss><channel>
<item><title>Ledgerly raises $18M Series B to expand B2B billing platform</title><link>https://news.example/ledgerly-series-b</link><pubDate>x</pubDate></item>
<item><title>Carelink closes Series D round</title><link>https://news.example/carelink-series-d</link></item>
<item><title>Ten tips for remote teams</title><link>https://news.example/tips</link></item>
</channel></rss>""",
        "https://news.example/ledgerly-series-b": _page("Ledgerly raises Series B", """
<p>Ledgerly, a B2B billing SaaS company, has raised an $18M Series B round led by a fictional fund.</p>
<p>The company said it will use the funding to expand into European markets. Visit <a href="https://ledgerly.example/">Ledgerly</a>.</p>""", recent),
        "https://news.example/carelink-series-d": _page("Carelink closes Series D", """
<p>Carelink has closed a Series D funding round for its patient engagement platform.</p>
<p><a href="https://carelink.example/">Carelink website</a></p>""", recent),

        # ---- Ledgerly: should QUALIFY ----
        "https://ledgerly.example/": _page("Ledgerly - B2B billing", """
<h1>Ledgerly</h1><p>Ledgerly is a SaaS billing platform for B2B finance teams.</p>
<p>We process payments and invoices for mid-market companies.</p>
<a href="/about">About</a> <a href="/pricing">Pricing</a> <a href="/security">Security</a> <a href="/careers">Careers</a> <a href="/team">Team</a> <a href="/press">Press</a>"""),
        "https://ledgerly.example/press": _page("Press", """
<p>Ledgerly announces an $18M Series B to expand across Europe.</p>""", recent),
        "https://ledgerly.example/about": _page("About Ledgerly", """
<p>Ledgerly was founded in 2021 and today we are a team of 120 employees across Bengaluru and London.</p>
<p>We serve customers in the UK and the European Union.</p>"""),
        "https://ledgerly.example/pricing": _page("Pricing", """
<p>Our Enterprise plan includes SAML single sign-on and audit logs.</p><p>Contact sales for enterprise pricing.</p>"""),
        "https://ledgerly.example/security": _page("Security at Ledgerly", """
<p>Security is core to how we build Ledgerly.</p>
<p>We are currently undergoing our SOC 2 Type II audit, expected to complete next quarter.</p>
<p>Our platform helps customers stay GDPR compliant when sending invoices.</p>"""),
        "https://ledgerly.example/careers": _page("Careers", """
<p>Open roles: Senior Backend Engineer, Security Engineer (GRC focus), Account Executive.</p>"""),
        "https://ledgerly.example/team": _page("Team", """
<p>Leadership team.</p><p>Asha Mockingbird is the CTO of Ledgerly and leads engineering and security.</p>
<p>Ravi Fakename is the Co-founder and CEO.</p>"""),

        # ---- Carelink: Series D -> REJECTED; customer-compliance wording must not count as its own ----
        "https://carelink.example/": _page("Carelink", """
<h1>Carelink</h1><p>Carelink is a SaaS patient engagement platform for clinics.</p>
<p>We help healthcare customers achieve HIPAA compliance and get SOC 2 ready faster.</p>
<a href="/about">About</a>"""),
        "https://carelink.example/about": _page("About Carelink", "<p>Carelink was founded in 2015.</p>"),

        # ---- Quietforms: little evidence -> UNCERTAIN (operator-supplied domain) ----
        "https://quietforms.example/": _page("Quietforms", """
<h1>Quietforms</h1><p>Quietforms is a cloud-based platform for building forms.</p><a href="/blog">Blog</a>"""),
        "https://quietforms.example/sitemap.xml": "<urlset><url><loc>https://quietforms.example/blog</loc></url></urlset>",
        "https://quietforms.example/blog": _page("Blog", "<p>Five ways to design better forms for your users.</p>"),
    }


def mock_hunter() -> Dict[str, Any]:
    return {"ledgerly.example": [
        {"value": "info@ledgerly.example", "type": "generic", "confidence": 90, "first_name": None, "last_name": None, "position": None},
        {"value": "asha@ledgerly.example", "type": "personal", "confidence": 94, "first_name": "Asha",
         "last_name": "Mockingbird", "position": "CTO", "sources": [{"uri": "https://ledgerly.example/team", "extracted_on": "2026-10-01"}]},
        {"value": "sam@ledgerly.example", "type": "personal", "confidence": 40, "first_name": "Sam",
         "last_name": "Lowconf", "position": "VP Engineering"},
        {"value": "jo@ledgerly.example", "type": "personal", "confidence": 92, "first_name": "Jo",
         "last_name": "Marketer", "position": "Head of Marketing"},
    ]}


def mock_sources() -> Dict[str, Any]:
    return {"tiers": {2: ["news.example"], 3: []}, "never_fetch": ["linkedin.com"],
            "discovery": {"feeds": ["https://news.example/feed"], "portfolio_pages": [],
                          "search_queries": ["SaaS Series A {month_year}"], "max_candidates": 10}}


def mock_deps():
    from .pipeline import Deps
    return Deps(fetcher=MockFetcher(mock_pages()), search=MockSearch({"quietforms": []}), llm=MockLLM(),
                hunter=MockHunter(mock_hunter()), icp=load_icp(), sources=mock_sources())
