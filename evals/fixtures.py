"""Reproducible, fake fixtures for the evaluation harness. Everything uses reserved .example domains, a temporary
SQLite store, fake HTTP/search/model/contact/Gmail clients and a fixed clock where time matters."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from sprinto_agent.config import Settings, load_icp
from sprinto_agent.contacts import MockHunter
from sprinto_agent.evidence import Evidence
from sprinto_agent.fetch import FetchError, MockFetcher, html_to_page
from sprinto_agent.gmail import FakeGmail
from sprinto_agent.mock_data import mock_hunter, mock_pages, mock_sources
from sprinto_agent.search import MockSearch
from sprinto_agent.store import open_mock_store
from sprinto_agent.util import content_hash, now_iso

INBOX = "operator@inbox.example"
PROSPECT = "asha@ledgerly.example"
TOPIC = "projects/eval/topics/gmail"
ICP = load_icp()
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def page(title: str, body: str, published: str = "") -> str:
    meta = '<meta property="article:published_time" content="%s">' % published if published else ""
    return "<html><head><title>%s</title>%s</head><body>%s</body></html>" % (title, meta, body)


class Env:
    """One isolated world per case: fresh store, fake clients, captured side effects."""

    def __init__(self, tmpdir: str):
        self.tmp = tmpdir
        self.store = open_mock_store("%s/eval.sqlite3" % tmpdir)
        self.gmail = FakeGmail(INBOX)

    def settings(self, **k: Any) -> Settings:
        base = dict(mock=True, operator_inbox=INBOX)
        base.update(k)
        return Settings(**base)

    def live(self, **k: Any) -> Settings:
        base = dict(mock=False, send_mode="live", send_transport="gmail_api", operator_inbox=INBOX,
                    gmail_pubsub_topic=TOPIC)
        base.update(k)
        return Settings(**base)

    def deps(self, pages: Optional[Dict[str, Any]] = None, hunter: Optional[Dict[str, Any]] = None, search: Any = None,
             llm: Any = None, drop: Optional[List[str]] = None, clock: Any = None):
        from sprinto_agent.pipeline import Deps
        p = mock_pages()
        for k in drop or []:
            p.pop(k, None)
        p.update(pages or {})
        h = mock_hunter()
        if hunter is not None:
            h.update(hunter)
        return Deps(fetcher=MockFetcher(p), search=search or MockSearch({}), llm=llm, hunter=MockHunter(h),
                    icp=ICP, sources=mock_sources(), clock=clock)

    def run(self, deps: Any = None, domains=("ledgerly.example",), settings: Any = None, feeds: bool = False,
            max_research: int = 8) -> Dict[str, Any]:
        from sprinto_agent.pipeline import execute_run, start_run
        s = settings or self.settings()
        deps = deps or self.deps()
        inputs = {"domains": list(domains), "use_feeds": feeds, "use_search": False, "use_portfolios": False,
                  "max_research": max_research}
        rid = start_run(self.store, s, inputs, deps.icp)
        summary = execute_run(self.store, s, rid, inputs, deps)
        return {"run_id": rid, "summary": summary}

    def company(self, domain: str) -> Dict[str, Any]:
        return self.store.one("SELECT * FROM companies WHERE domain = %s ORDER BY created_at DESC", (domain,)) or {}

    def draft(self) -> Dict[str, Any]:
        return self.store.one("SELECT * FROM drafts WHERE status <> 'superseded' ORDER BY created_at DESC") or {}

    def approved_draft(self) -> Dict[str, Any]:
        from sprinto_agent import review
        self.run()
        d = self.draft()
        review.approve(self.store, d["id"], "Eval Reviewer", recipient=INBOX)
        return self.store.draft(d["id"])

    def count(self, sql: str, params=()) -> int:
        return int(self.store.one("SELECT COUNT(*) AS n FROM (%s) q" % sql, params)["n"])

    def sent_messages(self) -> int:
        return len(self.gmail.sent_raw)

    def set_caps(self, per_company=1.0, monthly=100.0, prices=None, limits=None):
        from sprinto_agent import costs
        costs.set_setting(self.store, "caps", {"currency": "FAKE", "per_company_cap": per_company, "period_cap": monthly,
                                               "period": "month"}, "eval")
        costs.set_setting(self.store, "prices", prices if prices is not None else
                          {"tavily": {"price": 0.01}, "gemini": {"price": 0.5}, "hunter": {"price": 0.05}}, "eval")
        costs.set_setting(self.store, "unit_limits", limits or {}, "eval")


def ev(criterion: str, value: str, tier: str = "1", status: str = "verified", published: Optional[str] = None,
       url: str = "https://acme.example/") -> Evidence:
    return Evidence(criterion=criterion, value=value, quote="Acme quote for %s." % criterion, source_url=url,
                    retrieved_at=now_iso(), source_tier=tier, published_at=published, status=status)


def must_haves(stage: str = "series_a", published: Optional[str] = None) -> List[Evidence]:
    return [ev("is_b2b_saas", "true"), ev("funding_stage", stage, published=published)]


ALL_CHECKED = {k: "checked" for k in ("is_b2b_saas", "funding_stage", "employee_count", "enterprise_selling",
                                      "compliance_in_progress", "compliance_hiring", "gdpr_exposure", "sensitive_data",
                                      "certified")}


def raw_mail(frm: str, to: str, subject: str, body: str, in_reply_to: Optional[str] = None,
             extra: Optional[Dict[str, str]] = None, calendar: Optional[str] = None) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    m["Message-ID"] = "<%s@eval.example>" % content_hash(frm + subject, body)[:16]
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(body)
    if calendar:
        m.add_attachment(calendar.encode(), maintype="text", subtype="calendar", filename="invite.ics")
    return m.as_bytes()


def recipients(raw: bytes) -> List[str]:
    from email import message_from_bytes
    from email.utils import getaddresses
    m = message_from_bytes(raw)
    return sorted(a.lower() for _, a in getaddresses(m.get_all("To", []) + m.get_all("Cc", []) + m.get_all("Bcc", [])))


def make_draft_row(store: Any, company_id: str, poc_id: str, run_id: str, subject: str = "s", body: str = "b",
                   status: str = "pending_review") -> str:
    ts = now_iso()
    return store.insert("drafts", {"company_id": company_id, "poc_id": poc_id, "run_id": run_id,
                                   "original_subject": subject, "original_body": body, "current_subject": subject,
                                   "current_body": body, "current_hash": content_hash(subject, body), "version": 1,
                                   "status": status, "approved_hash": None, "pain_points_json": "[]",
                                   "guidance_json": "[]", "composer": "template", "created_at": ts, "updated_at": ts})


def j(x: Any) -> Any:
    """Normalise outputs to plain JSON values for comparison and logging."""
    return json.loads(json.dumps(x, default=str))
