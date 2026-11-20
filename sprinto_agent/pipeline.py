"""Run orchestration: Stage 1 -> Stage 2 -> contact -> draft, persisted per company stage.

Reliability rules:
  * At most one active run (database unique index on research_runs.active_lock). A second Run click,
    retry or resume while one is active raises RunAlreadyActive instead of duplicating work.
  * Every company records its research_stage: pending -> researching -> researched -> contact -> done
    (or skipped). A crashed run (no heartbeat for STALE_RUN_MINUTES) is marked 'interrupted' and resume_run()
    continues each company from its last completed stage; partial evidence from an interrupted stage is replaced.
  * One company's failure (site, API, database) never stops the rest; the error kind and retryability are stored.
  * Companies already in the pipeline (open draft or a send) and opted-out domains are not researched again.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import Settings, load_icp, load_sources
from .contacts import (Contact, ContactUnavailable, HunterClient, NoHunter, contact_to_row, filter_hunter,
                       verify_contact)
from .discovery import Candidate, Discoverer
from .drafting import compose, guidance_from_history, llm_polish
from .evidence import is_recent
from .fetch import FetchError, Fetcher
from .llm import GeminiClient, LLMUnavailable
from .research import ResearchResult, Researcher
from .safety import suppressed_reason
from .search import NoSearch, SearchUnavailable, TavilySearch
from .store import Store, audit_fetch, dumps, is_unique_violation
from .util import get_logger, now_iso, parse_date, registrable_domain

log = get_logger(__name__)

ACTIVE_DRAFT = ("pending_review", "approved")


class RunAlreadyActive(RuntimeError):
    def __init__(self, run_id: Optional[str]):
        super().__init__("a run is already in progress%s; wait for it or resume it instead of starting another"
                         % (" (%s)" % run_id[:8] if run_id else ""))
        self.run_id = run_id


@dataclass
class Deps:
    fetcher: Any
    search: Any
    llm: Optional[Any]
    hunter: Any
    icp: Dict[str, Any]
    sources: Dict[str, Any]
    clock: Any = None


def build_deps(settings: Settings) -> Deps:
    if settings.mock:
        from .mock_data import mock_deps
        return mock_deps()
    sources = load_sources()
    return Deps(
        fetcher=Fetcher(settings.user_agent, settings.request_timeout, settings.per_host_delay,
                        sources.get("never_fetch", [])),
        search=TavilySearch(settings.tavily_key) if settings.tavily_key else NoSearch(),
        llm=GeminiClient(settings.gemini_key, settings.gemini_model) if settings.gemini_key else None,
        hunter=HunterClient(settings.hunter_key) if settings.hunter_key else NoHunter(),
        icp=load_icp(), sources=sources)


def classify_error(e: BaseException):
    """-> (error_kind, retryable). Retrying is always safe (no duplicate work or sends); 'internal' usually
    needs a code fix first, so it is not offered as retryable."""
    if isinstance(e, FetchError):
        return getattr(e, "kind", "unavailable"), True
    if isinstance(e, (SearchUnavailable, ContactUnavailable, LLMUnavailable)):
        return getattr(e, "kind", "outage"), True
    mod = type(e).__module__ or ""
    if mod.startswith(("psycopg", "sqlite3")) or type(e).__name__ in ("OperationalError", "InterfaceError", "DatabaseError"):
        return "database", True
    return "internal", False


# ---------------------------------------------------------------- run lock & lifecycle
def active_run(store: Store) -> Optional[Dict[str, Any]]:
    return store.one("SELECT * FROM research_runs WHERE active_lock = 1")


def recover_stale_runs(store: Store, stale_minutes: float = 15.0) -> List[str]:
    """Mark runs whose worker stopped heartbeating (crash, killed process) as 'interrupted' and free the lock."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    out = []
    for r in store.query("SELECT * FROM research_runs WHERE active_lock = 1"):
        beat = parse_date(r.get("heartbeat_at") or r.get("started_at"))
        if beat is None or beat < cutoff:
            store.execute("UPDATE research_runs SET status = 'interrupted', active_lock = NULL WHERE id = %s", (r["id"],))
            store.log(r["id"], "run marked interrupted: no heartbeat for %s+ minutes (process crashed or was stopped); "
                               "it can be resumed" % int(stale_minutes), "warning")
            out.append(r["id"])
    return out


def _acquire_existing(store: Store, run_id: str) -> None:
    try:
        store.execute("UPDATE research_runs SET active_lock = 1, status = 'running', heartbeat_at = %s, "
                      "attempts = attempts + 1, finished_at = NULL, error = NULL WHERE id = %s", (now_iso(), run_id))
    except Exception as e:
        if is_unique_violation(e):
            a = active_run(store)
            raise RunAlreadyActive(a["id"] if a else None)
        raise


def _release(store: Store, run_id: str, status: str, summary: Optional[Dict[str, Any]] = None,
             error: Optional[str] = None) -> None:
    fields: Dict[str, Any] = {"status": status, "active_lock": None, "finished_at": now_iso()}
    if summary is not None:
        fields["summary_json"] = dumps(summary)
    if error is not None:
        fields["error"] = error[:300]
    store.update("research_runs", run_id, fields)


def _beat(store: Store, run_id: str) -> None:
    try:
        store.update("research_runs", run_id, {"heartbeat_at": now_iso()})
    except Exception:
        pass  # a missed heartbeat is not fatal; staleness is measured in minutes


def start_run(store: Store, settings: Settings, inputs: Dict[str, Any], icp: Dict[str, Any]) -> str:
    if store.is_mock != settings.mock:
        raise RuntimeError("store/mode mismatch: mock data must never be written to the real database")
    recover_stale_runs(store, settings.stale_run_minutes)
    from . import costs, versions
    if settings.mock:
        seed_mock_costs(store)
    else:
        costs.apply_config_defaults(store)        # config/costs.yaml, only if no limits are saved yet
    costs.require_ready(store, settings_providers(settings))   # operator must set caps before paid research
    v = versions.current()
    try:
        return store.insert("research_runs", {
            "is_mock": settings.mock, "status": "queued", "started_at": now_iso(), "heartbeat_at": now_iso(),
            "active_lock": 1, "input_json": dumps(inputs), "campaign_id": inputs.get("campaign_id") or "pilot",
            "icp_version": str(icp.get("name", "")) + " v" + str(icp.get("version", "")) + " " + v["icp_version"],
            "prompt_version": v["prompt_version"], "policy_version": v["policy_version"]})
    except Exception as e:
        if is_unique_violation(e):
            a = active_run(store)
            raise RunAlreadyActive(a["id"] if a else None)
        raise


def settings_providers(settings: Settings) -> List[str]:
    if settings.mock:
        return ["tavily", "gemini", "hunter"]
    return [p for p, k in (("tavily", settings.tavily_key), ("gemini", settings.gemini_key), ("hunter", settings.hunter_key)) if k]


def seed_mock_costs(store: Store) -> None:
    """Mock store only: FAKE caps and prices (currency 'FAKE') so the mock workflow exercises the meter.
    Never written to Neon."""
    from . import costs
    if not store.is_mock or costs.get_setting(store, "caps"):
        return
    costs.set_setting(store, "caps", {"currency": "FAKE", "per_company_cap": 1.0, "period_cap": 100.0, "period": "month"}, "mock seed")
    costs.set_setting(store, "prices", {"tavily": {"price": 0.01}, "gemini": {"price": 0.5}, "hunter": {"price": 0.05}}, "mock seed")


def _metered(store: Store, run_id: str, cid: str, deps: Deps):
    """Wrap the billable clients for one company with a cost meter."""
    from dataclasses import replace
    from . import costs
    from .feedback import demoted_domains
    meter = costs.Meter(store, run_id, cid)
    prov = costs.billable_providers(deps)
    sources = dict(deps.sources or {})
    try:
        sources["demoted_domains"] = demoted_domains(store)
    except Exception:
        sources["demoted_domains"] = []
    d2 = replace(deps, sources=sources,
                 search=costs.MeteredSearch(deps.search, meter) if "tavily" in prov else deps.search,
                 llm=costs.MeteredLLM(deps.llm, meter) if "gemini" in prov else deps.llm,
                 hunter=costs.MeteredHunter(deps.hunter, meter) if "hunter" in prov else deps.hunter)
    return d2, meter


def run_summary(store: Store, run_id: str) -> Dict[str, Any]:
    """Counts are read back from the database so a resumed run reports its true totals."""
    cos = store.query("SELECT qualification, error_kind, research_stage FROM companies WHERE run_id = %s", (run_id,))
    drafts = store.query("SELECT id FROM drafts WHERE run_id = %s AND status <> 'superseded'", (run_id,))
    return {"discovered": len(cos),
            "researched": sum(1 for c in cos if c["qualification"] and c["qualification"] != "error"),
            "qualified": sum(1 for c in cos if c["qualification"] == "qualified"),
            "drafts": len(drafts),
            "errors": sum(1 for c in cos if c["error_kind"]),
            "skipped": sum(1 for c in cos if c["research_stage"] == "skipped")}


# A run on Vercel works in steps: each request researches companies until its time budget is nearly used, then the
# run is parked as 'paused' with the rest 'waiting' and "Continue run" picks it up. No company is cut off mid-way.
MIN_STEP_SECONDS = 75       # don't start a company with less time than this left
STEP_MARGIN_SECONDS = 45    # time kept back for contact lookup + drafting after research


def _left(deadline: Optional[float]) -> Optional[float]:
    return None if deadline is None else deadline - time.monotonic()


def _out_of_time(store: Store, run_id: str, rest: List[str], deadline: Optional[float]) -> bool:
    left = _left(deadline)
    if left is None or left >= MIN_STEP_SECONDS:
        return False
    for cid in rest:
        _set(store, cid, stop_code="time_budget", stop_reason="waiting: researched in the next step (click Continue run)")
    store.log(run_id, "step time budget used; %d compan%s waiting for the next step" % (len(rest), "y" if len(rest) == 1 else "ies"))
    _release(store, run_id, "paused", dict(run_summary(store, run_id), paused="time budget", waiting=len(rest)))
    return True


def _capped(settings: Settings, deadline: Optional[float]) -> Settings:
    left = _left(deadline)
    if left is None:
        return settings
    from dataclasses import replace
    return replace(settings, max_seconds_per_company=max(20.0, min(settings.max_seconds_per_company, left - STEP_MARGIN_SECONDS)))


def execute_run(store: Store, settings: Settings, run_id: str, inputs: Dict[str, Any], deps: Deps,
                deadline: Optional[float] = None) -> Dict[str, Any]:
    """`deadline` (time.monotonic() value) makes this one step of the run; see MIN_STEP_SECONDS."""
    store.update("research_runs", run_id, {"status": "running", "heartbeat_at": now_iso()})
    gaps = settings.missing() if not settings.mock else {}
    for k, v in gaps.items():
        store.log(run_id, "config: %s missing -> %s" % (k, v), "warning")
    try:
        notes: List[str] = []
        disc = Discoverer(deps.fetcher, deps.search, deps.icp, deps.sources, notes)
        cands = disc.discover(domains=inputs.get("domains") or [], use_feeds=inputs.get("use_feeds", True),
                              use_search=inputs.get("use_search", True), use_portfolios=inputs.get("use_portfolios", True))
        for n in notes:
            store.log(run_id, n, "warning")
        max_research = int(inputs.get("max_research", 8))
        selected: List[Any] = []
        for c in cands:  # persist every candidate first: progress is visible and a crash loses nothing
            cid = _save_candidate(store, run_id, c)
            skip = _skip_reason(store, c, run_id)
            if c.stage1_status == "rejected" or not c.domain or skip:
                _set(store, cid, research_stage="skipped", stop_code="skipped",
                     stop_reason=skip or c.stage1_reason or "not researched")
                continue
            if len(selected) >= max_research:
                _set(store, cid, research_stage="skipped", stop_code="run_limit",
                     stop_reason="not researched: run limit of %d companies reached" % max_research)
                continue
            selected.append((c, cid))
        store.log(run_id, "discovery: %d candidates, %d selected for research" % (len(cands), len(selected)))
        from .costs import billable_paused
        for i, (c, cid) in enumerate(selected):
            paused = billable_paused(store)
            if paused:
                for _, rest in selected[i:]:
                    _set(store, rest, stop_code="cost_paused", stop_reason="waiting: billable work paused (%s)" % paused)
                summary = dict(run_summary(store, run_id), config_gaps=gaps, paused=paused)
                store.log(run_id, "run paused: %s. Resume it after a person resumes billable work in Settings" % paused, "warning")
                _release(store, run_id, "paused", summary)
                return summary
            if _out_of_time(store, run_id, [rest for _, rest in selected[i:]], deadline):
                return run_summary(store, run_id)
            _beat(store, run_id)
            _process_safely(store, _capped(settings, deadline), run_id, cid, c, deps)
        summary = dict(run_summary(store, run_id), config_gaps=gaps)
        _release(store, run_id, "completed", summary)
        return summary
    except Exception as e:
        try:
            _release(store, run_id, "failed", dict(run_summary(store, run_id), config_gaps=gaps),
                     "%s: %s" % (type(e).__name__, str(e)[:200]))
        except Exception:
            log.error("run %s failed and its status could not be recorded (database unavailable?)", run_id[:8])
        raise
    finally:
        try:
            deps.fetcher.close()
        except Exception:
            pass


def resume_run(store: Store, settings: Settings, run_id: str, deps: Deps,
               only_company: Optional[str] = None, deadline: Optional[float] = None) -> Dict[str, Any]:
    """Continue an interrupted/failed run (or retry one company) from each company's last completed stage.
    Completed work is not repeated: researched companies are not re-researched, drafted ones not redrafted."""
    recover_stale_runs(store, settings.stale_run_minutes)
    run = store.one("SELECT * FROM research_runs WHERE id = %s", (run_id,))
    if not run:
        raise ValueError("run not found")
    if bool(run.get("is_mock")) != settings.mock:
        raise RuntimeError("store/mode mismatch")
    from . import costs
    costs.require_ready(store, settings_providers(settings))   # a paused/unconfigured budget blocks resume
    _acquire_existing(store, run_id)
    store.log(run_id, "resumed%s (attempt %s)" % (" for one company" if only_company else "", int(run.get("attempts") or 1) + 1))
    try:
        rows = store.query("SELECT * FROM companies WHERE run_id = %s ORDER BY created_at", (run_id,))
        todo = []
        for row in rows:
            if only_company and row["id"] != only_company:
                continue
            stage = row.get("research_stage") or "pending"
            if stage == "skipped" or (stage == "done" and not row.get("error_kind") and row.get("stop_code") != "cost_cap_reached"):
                continue
            todo.append((row, stage))
        for i, (row, stage) in enumerate(todo):
            if _out_of_time(store, run_id, [r["id"] for r, _ in todo[i:]], deadline):
                return run_summary(store, run_id)
            if row.get("stop_code") == "time_budget":
                _set(store, row["id"], stop_code=None, stop_reason=None)
            c = Candidate(row.get("name"), row["domain"], row["found_via"], lead_url=row.get("lead_url"),
                          stage1_status=row["stage1_status"], stage1_reason=row["stage1_reason"])
            _beat(store, run_id)
            _process_safely(store, _capped(settings, deadline), run_id, row["id"], c, deps, from_stage=stage)
        summary = run_summary(store, run_id)
        _release(store, run_id, "completed", summary)
        return summary
    except Exception as e:
        try:
            _release(store, run_id, "failed", run_summary(store, run_id), "%s: %s" % (type(e).__name__, str(e)[:200]))
        except Exception:
            log.error("resume of %s failed and its status could not be recorded", run_id[:8])
        raise
    finally:
        try:
            deps.fetcher.close()
        except Exception:
            pass


def retry_company(store: Store, settings: Settings, company_id: str, deps: Deps) -> Dict[str, Any]:
    co = store.one("SELECT run_id FROM companies WHERE id = %s", (company_id,))
    if not co:
        raise ValueError("company not found")
    return resume_run(store, settings, co["run_id"], deps, only_company=company_id)


# ---------------------------------------------------------------- per company
def _set(store: Store, cid: str, **fields: Any) -> None:
    from .util import redact
    for k in ("stop_reason", "error"):
        if isinstance(fields.get(k), str):
            fields[k] = redact(fields[k])  # API errors can echo request details; never store credentials
    fields["updated_at"] = now_iso()
    store.update("companies", cid, fields)


def _save_candidate(store: Store, run_id: str, c: Candidate) -> str:
    ts = now_iso()
    return store.insert("companies", {"run_id": run_id, "name": c.name, "domain": c.domain or "(unknown)",
                                      "found_via": c.found_via, "lead_url": c.lead_url,
                                      "stage1_status": c.stage1_status, "stage1_reason": c.stage1_reason or "",
                                      "pages_fetched": 0, "research_stage": "pending", "created_at": ts, "updated_at": ts})


def _skip_reason(store: Store, c: Candidate, run_id: str) -> Optional[str]:
    if not c.domain:
        return None
    sup = suppressed_reason(store, domain=c.domain)
    if sup:
        return "not researched: " + sup
    d = registrable_domain(c.domain)
    open_draft = store.one(
        "SELECT d.id, d.status FROM drafts d JOIN companies c ON c.id = d.company_id WHERE c.domain = %s AND c.run_id <> %s "
        "AND d.status IN ('pending_review', 'approved', 'sent') LIMIT 1", (d, run_id))
    if open_draft:
        return "not researched again: already in the pipeline (draft %s is %s)" % (open_draft["id"][:8], open_draft["status"])
    return None


def _process_safely(store: Store, settings: Settings, run_id: str, cid: str, c: Candidate, deps: Deps,
                    from_stage: str = "pending") -> None:
    try:
        process_company(store, settings, run_id, cid, c, deps, {}, from_stage=from_stage)
    except Exception as e:  # isolate failures per company
        kind, retry = classify_error(e)
        log.debug(traceback.format_exc())
        try:
            row = store.one("SELECT qualification FROM companies WHERE id = %s", (cid,)) or {}
            _set(store, cid, error_kind=kind, retryable=retry, error="%s: %s" % (type(e).__name__, str(e)[:300]),
                 qualification=row.get("qualification") or "error")
            store.log(run_id, "company %s failed (%s): %s; other companies continue" % (c.domain, kind, type(e).__name__),
                      "error", cid)
        except Exception:
            log.error("company %s failed (%s) and the failure could not be recorded", c.domain, kind)


def process_company(store: Store, settings: Settings, run_id: str, cid: str, c: Candidate, deps: Deps,
                    summary: Dict[str, Any], from_stage: str = "pending") -> None:
    stage = from_stage or "pending"
    row = store.one("SELECT * FROM companies WHERE id = %s", (cid,)) or {}
    res: Optional[ResearchResult] = None
    deps, meter = _metered(store, run_id, cid, deps)
    if row.get("stop_code") == "cost_cap_reached" and stage == "done":
        stage = "pending"   # explicit human resume after a cost cap: research again with the new budget
    if stage in ("pending", "researching"):
        if stage == "researching" or store.one("SELECT id FROM evidence WHERE company_id = %s LIMIT 1", (cid,)):
            # an earlier attempt stopped mid-research: replace its partial evidence rather than duplicating it
            store.execute("DELETE FROM evidence WHERE company_id = %s", (cid,))
            store.log(run_id, "re-researching %s from scratch (previous attempt was interrupted)" % c.domain, "info", cid)
        _set(store, cid, research_stage="researching", attempts=int(row.get("attempts") or 0) + 1,
             error_kind=None, retryable=None, error=None)
        res = _research(store, settings, run_id, cid, c, deps, meter)
        a = res.assessment
        if meter.tripped:
            unresolved = ", ".join(a.open_questions) or "none"
            _set(store, cid, research_stage="done", stop_code="cost_cap_reached", error_kind="cost_cap", retryable=False,
                 stop_reason="stopped: cost cap reached (%s). Known findings kept; unresolved facts: %s" % (
                     meter.trip_message, unresolved))
            store.log(run_id, "%s: cost cap reached (%s); unresolved: %s" % (c.domain, meter.scope_label(), unresolved),
                      "warning", cid)
            return
        if res.site_error:
            # could not read the site: keep it pending so a retry/resume tries again; conclude nothing
            _set(store, cid, research_stage="pending", qualification="uncertain", error_kind=res.site_error, retryable=True)
            return
        if a.decision != "qualified":
            _set(store, cid, research_stage="done")
            return
        _set(store, cid, research_stage="researched")
        stage = "researched"
    name = (store.one("SELECT name FROM companies WHERE id = %s", (cid,)) or {}).get("name") or c.domain
    if stage == "researched":
        poc = find_contact(store, run_id, cid, name, c.domain, res, deps)
        if poc is None:
            return  # reason recorded (no suitable POC, or a retryable API failure)
        _set(store, cid, research_stage="contact", error_kind=None, retryable=None)
        stage = "contact"
    if stage in ("contact", "drafted", "done"):
        pocs = active_pocs(store, cid)
        if not pocs:
            _set(store, cid, research_stage="researched")
            return
        poc = pocs[-1]
        if poc["verification_status"] == "conflict":
            _set(store, cid, research_stage="contact", stop_code="poc_conflict",
                 stop_reason="contact's role on public pages conflicts with Hunter's title; no draft. Check it, or add a manual contact")
            return
        make_draft(store, settings, run_id, cid, name, poc, deps)
        _set(store, cid, research_stage="done", error_kind=None, retryable=None)


def _research(store: Store, settings: Settings, run_id: str, cid: str, c: Candidate, deps: Deps,
              meter: Any = None) -> ResearchResult:
    researcher = Researcher(deps.fetcher, deps.search, deps.llm, deps.icp, deps.sources,
                            settings.max_pages_per_company, settings.max_research_rounds,
                            settings.max_searches_per_company, settings.max_llm_calls_per_company,
                            settings.max_seconds_per_company, clock=deps.clock, cost_guard=meter)
    res = researcher.research(c.domain, c.name, c.lead_page)
    name = c.name or res.name or _name_from_pages(res) or c.domain
    for ev in res.evidence:
        store.insert("evidence", ev.to_row(cid))
    for kind, target, outcome, detail in res.audit:
        audit_fetch(store, run_id, cid, kind, target, outcome, detail)
    for n in res.notes:
        store.log(run_id, n, "info", cid)
    a = res.assessment
    store.log(run_id, "%s: %s, score %d (range %d-%d); %s" % (c.domain, a.decision, a.score, a.min_possible,
                                                              a.max_possible, res.stop_reason), "info", cid)
    _set(store, cid, name=name, qualification=a.decision, score=a.score, score_min=a.min_possible, score_max=a.max_possible,
         could_change=a.could_change, stop_reason=res.stop_reason, stop_code=res.stop_code or "",
         budget_json=dumps(res.budget.to_dict() if res.budget else {}),
         assessment_json=dumps(dict(a.to_dict(), identity=list(res.identity), coverage=res.coverage())),
         pages_fetched=len(res.pages))
    return res


def _name_from_pages(res: Any) -> Optional[str]:
    """Company name from its own homepage (og:site_name, else <title> if it matches the domain)."""
    import html as _h
    import re
    home = res.pages.get("https://%s/" % res.domain)
    if home is not None and home.html:
        m = re.search(r"<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"']{2,60})", home.html, re.I)
        if m:
            return _h.unescape(m.group(1)).strip()
        m = re.search(r"<title[^>]*>([^<]{2,120})</title>", home.html, re.I)
        if m:
            first = re.split(r"\s[|\-–—:]\s", _h.unescape(m.group(1)).strip())[0].strip()
            if first and res.domain.split(".")[0].lower() in first.lower().replace(" ", ""):
                return first[:60]
    for p in res.pages.values():
        for b in p.structured:
            if isinstance(b, dict) and b.get("@type") == "Organization" and b.get("name"):
                return str(b["name"])[:80]
    return None


def active_pocs(store: Store, cid: str) -> List[Dict[str, Any]]:
    return [p for p in store.pocs_for(cid) if (p.get("status") or "active") == "active"]


def find_contact(store: Store, run_id: str, cid: str, name: str, domain: str, res: Optional[ResearchResult],
                 deps: Deps, prefs: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    existing = active_pocs(store, cid)
    if existing and not prefs:
        return existing[-1]  # never look the same company up twice (Hunter quota, duplicate contacts)
    try:
        emails = deps.hunter.domain_search(domain)
        audit_fetch(store, run_id, cid, "contact_api", "hunter domain-search " + domain, "ok", "%d results" % len(emails))
    except ContactUnavailable as e:
        kind = getattr(e, "kind", "outage")
        audit_fetch(store, run_id, cid, "contact_api", "hunter domain-search " + domain, kind, str(e))
        if kind == "cost_cap":
            _set(store, cid, stop_code="cost_cap_reached", error_kind="cost_cap", retryable=False,
                 stop_reason="contact lookup not made: %s. Known findings kept" % e)
        else:
            _set(store, cid, stop_code="poc_" + kind, error_kind=kind, retryable=True,
                 stop_reason="contact lookup unavailable (%s): %s. Retry later or add a manual Apollo contact" % (kind, e))
        store.log(run_id, "POC lookup for %s: %s" % (domain, e), "warning", cid)
        return None
    from .contacts import apply_preferences
    rejected = {(p.get("email") or "").lower() for p in store.pocs_for(cid) if p.get("status") == "rejected"}
    emails = [e for e in emails if (e.get("value") or "").lower() not in rejected]
    icp_used = deps.icp
    if prefs:
        emails, icp_used = apply_preferences(emails, prefs, deps.icp)
    contacts, dropped = filter_hunter(emails, domain, icp_used)
    if dropped:
        store.log(run_id, "POC filter for %s excluded %d entries: %s" % (domain, len(dropped), "; ".join(dropped[:6])), "info", cid)
    if not contacts:
        _set(store, cid, research_stage="done" if not prefs else "researched", stop_code="no_poc",
             stop_reason="no suitable POC with a verified email from Hunter (%d returned, none eligible%s); "
                         "add a manual Apollo contact to continue" % (len(emails), " with your preferences" if prefs else ""))
        return None
    pages = list(res.pages.values()) if res else []
    pages += _team_pages(res, domain, deps)
    c = verify_contact(contacts[0], name, domain, pages, deps.fetcher, deps.search, deps.sources)
    row = save_contact(store, cid, c)
    if prefs:
        store.update("pocs", row["id"], {"lookup_prefs_json": dumps(prefs)})
    return row


def _team_pages(res: Optional[ResearchResult], domain: str, deps: Deps, limit: int = 3) -> List[Any]:
    """Fetch leadership/team pages linked from the company's own pages (for POC role verification).
    On a resumed run the research pages are no longer in memory, so the homepage is re-read."""
    import re
    from .util import same_site
    pages = dict(res.pages) if res else {}
    extra: List[Any] = []
    if not pages:
        try:
            home = deps.fetcher.get("https://%s/" % domain)
            pages[home.url] = home
            extra.append(home)
        except FetchError:
            return []
    links: List[str] = []
    for p in pages.values():
        for l in p.links:
            if same_site(l, domain) and l not in pages and l not in links and \
                    re.search(r"/(team|leadership|about|company|management|founders?)(/|$)", l.lower()):
                links.append(l)
    for l in links[:limit]:
        try:
            extra.append(deps.fetcher.get(l))
        except FetchError:
            continue
    return extra


def rerun_contact(store: Store, settings: Settings, company_id: str, deps: Deps, reviewer: str,
                  prefs: Optional[Dict[str, Any]] = None, reason: str = "") -> Dict[str, Any]:
    """POC rejected by the reviewer: record it, mark the contact rejected, supersede its open drafts, and look
    the company up again with the reviewer's preferred/excluded titles, departments and seniority. Only people
    Hunter returns with a verified-format email are used; nothing is guessed."""
    from .feedback import record_feedback
    co = store.one("SELECT * FROM companies WHERE id = %s", (company_id,))
    if not co or co.get("qualification") != "qualified":
        raise ValueError("company must be qualified")
    prefs = {k: v for k, v in (prefs or {}).items() if v}
    current = active_pocs(store, company_id)
    if current:
        poc = current[-1]
        record_feedback(store, "poc", "unsuitable", reviewer, reason, company_id=company_id, poc_id=poc["id"],
                        details=dict(prefs, rerun=True))
        store.update("pocs", poc["id"], {"status": "rejected"})
        for d in store.drafts_for(company_id):
            if d["status"] in ACTIVE_DRAFT and d["poc_id"] == poc["id"]:
                store.update("drafts", d["id"], {"status": "superseded", "approved_hash": None, "approved_recipient": None,
                                                 "updated_at": now_iso()})
    from . import costs
    costs.require_ready(store, settings_providers(settings))
    mdeps, meter = _metered(store, co["run_id"], company_id, deps)
    new = find_contact(store, co["run_id"], company_id, co["name"] or co["domain"], co["domain"], None, mdeps,
                       prefs=prefs or {"rerun": True})
    if new is None:
        return {"status": "no_contact", "message": (store.one("SELECT stop_reason FROM companies WHERE id = %s",
                                                              (company_id,)) or {}).get("stop_reason")}
    _set(store, company_id, research_stage="contact", stop_code=None, error_kind=None, retryable=None)
    did = None
    if new["verification_status"] != "conflict":
        did = make_draft(store, settings, co["run_id"], company_id, co["name"] or co["domain"], new, mdeps)
        _set(store, company_id, research_stage="done")
    return {"status": "found", "poc_id": new["id"], "name": new["full_name"], "title": new["title"], "draft_id": did}


def save_contact(store: Store, cid: str, c: Contact) -> Dict[str, Any]:
    pid = store.insert("pocs", contact_to_row(c, cid))
    return store.one("SELECT * FROM pocs WHERE id = %s", (pid,))


def redraft(store: Store, settings: Settings, company_id: str, deps: Deps) -> Optional[str]:
    """Regenerate a draft from stored evidence + POC (e.g. after a drafting-rule fix). Pending/approved
    drafts for the company are marked 'superseded' (never sendable); sent drafts are left untouched."""
    co = store.one("SELECT * FROM companies WHERE id = %s", (company_id,))
    pocs = active_pocs(store, company_id)
    if not co or co["qualification"] != "qualified" or not pocs:
        raise ValueError("company must be qualified and have a saved POC")
    for d in store.drafts_for(company_id):
        if d["status"] in ACTIVE_DRAFT:
            store.update("drafts", d["id"], {"status": "superseded", "approved_hash": None, "updated_at": now_iso()})
            store.log(co["run_id"], "draft %s superseded by redraft" % d["id"][:8], "info", company_id)
    return make_draft(store, settings, co["run_id"], company_id, co["name"], pocs[-1], deps)


def company_segment(ev_rows: List[Dict[str, Any]]) -> str:
    """Coarse cohort for scoped style guidance and report breakdowns: verified funding stage, else 'unknown'."""
    stages = [e["value"] for e in ev_rows if e["criterion"] == "funding_stage" and e["status"] == "verified"]
    for s in ("series_c", "series_b", "series_a", "seed"):
        if s in stages:
            return s
    return "unknown"


def make_draft(store: Store, settings: Settings, run_id: str, cid: str, name: str, poc: Dict[str, Any], deps: Deps) -> Optional[str]:
    co = store.one("SELECT domain FROM companies WHERE id = %s", (cid,)) or {}
    sup = suppressed_reason(store, domain=co.get("domain"), email=poc.get("email"))
    if sup:
        _set(store, cid, stop_code="suppressed", stop_reason="no draft: " + sup)
        return None
    existing = store.one(
        "SELECT d.id FROM drafts d JOIN companies c ON c.id = d.company_id WHERE c.domain = %s "
        "AND d.status IN ('pending_review', 'approved', 'sent') LIMIT 1", (co.get("domain"),))
    if existing:
        store.log(run_id, "draft not created: %s already has draft %s" % (co.get("domain"), existing["id"][:8]), "info", cid)
        return existing["id"]
    ev_rows = store.evidence_for(cid)
    months = int(deps.icp.get("hook_funding_max_months", deps.icp.get("recent_funding_months", 12)))
    recent_ok = {e["id"]: is_recent(e.get("published_at"), months) for e in ev_rows}
    from .feedback import accepted_guidance
    segment = company_segment(ev_rows)
    # wording guidance comes only from rules the operator accepted for this role/segment (never facts)
    guidance = accepted_guidance(store, poc.get("role_label"), segment)
    first = poc["full_name"].split()[0]
    d = compose(name, first, poc["role_label"], ev_rows, recent_ok, deps.icp, guidance,
                sender_name=settings.sender_name)
    if d is None:
        _set(store, cid, stop_code="no_hook", stop_reason="qualified, but no verified tier-1/2 hook for a draft")
        return None
    if deps.llm is not None:
        d = llm_polish(deps.llm, d)
    if poc["verification_status"] != "verified":
        d.guidance = d.guidance + [{"rule": "warning", "detail": "POC role not independently verified (%s); check before approving"
                                    % poc["verification_status"], "review_ids": []}]
    from .util import content_hash
    ts = now_iso()
    did = store.insert("drafts", {
        "company_id": cid, "poc_id": poc["id"], "run_id": run_id,
        "original_subject": d.subject, "original_body": d.body, "current_subject": d.subject, "current_body": d.body,
        "current_hash": content_hash(d.subject, d.body), "version": 1, "status": "pending_review",
        "approved_hash": None, "hook_evidence_id": d.hook_evidence_id, "pain_points_json": dumps(d.pain_points),
        "guidance_json": dumps(d.guidance), "composer": d.composer, "created_at": ts, "updated_at": ts,
        "parts_json": dumps(d.parts), "segment": segment})
    from .feedback import ensure_base_revision
    ensure_base_revision(store, store.draft(did))   # revision 1 = the first generated draft, kept forever
    store.log(run_id, "draft %s created for %s (waiting for review)" % (did[:8], co.get("domain")), "info", cid)
    return did


# ---------------------------------------------------------------- single company (search bar / "research one company")
class CompanyNotFound(ValueError):
    pass


def looks_like_domain(q: str) -> bool:
    q = (q or "").strip()
    return " " not in q and "." in q and len(q) >= 4


def resolve_company(store: Store, query: str, deps: Deps) -> Dict[str, Any]:
    """Turn what the operator typed into a company website.
    - A URL or domain is used as given.
    - A name already in the pipeline uses its stored domain.
    - Otherwise ONE search for the official site; only a result whose domain matches the name is accepted
      (the research step then checks the homepage really is that company). Nothing is guessed."""
    from .discovery import NON_COMPANY_HOSTS, _compact
    from .util import host_matches
    q = (query or "").strip()
    if not q:
        raise CompanyNotFound("enter a company name or website")
    if looks_like_domain(q):
        d = registrable_domain(q)
        if not d or any(host_matches(d, h) for h in NON_COMPANY_HOSTS + list(deps.sources.get("never_fetch") or [])):
            raise CompanyNotFound("%s is not a company website we can research" % q)
        return {"domain": d, "name": None, "how": "website you entered"}
    row = store.one("SELECT name, domain FROM companies WHERE LOWER(name) = %s AND domain <> '(unknown)' "
                    "ORDER BY created_at DESC LIMIT 1", (q.lower(),))
    if row:
        return {"domain": row["domain"], "name": row["name"], "how": "already in your pipeline"}
    target = _compact(q)
    if len(target) < 3:
        raise CompanyNotFound("name too short to look up; enter the website instead")
    blocked = NON_COMPANY_HOSTS + list(deps.sources.get("never_fetch") or []) + \
        [d for t in (2, 3) for d in (deps.sources.get("tiers", {}).get(t) or deps.sources.get("tiers", {}).get(str(t)) or [])]
    try:
        hits = deps.search.search('"%s" official website' % q, max_results=6)
    except SearchUnavailable as e:
        raise CompanyNotFound("could not look up '%s' (%s); enter the website instead" % (q, e))
    for h in hits:
        d = registrable_domain(h.url)
        if not d or any(host_matches(d, b) for b in blocked):
            continue
        root = _compact(d.split(".")[0])
        if root == target or (len(root) >= 4 and (root.startswith(target) or target.startswith(root))):
            return {"domain": d, "name": q, "how": "found by search (%s)" % d}
    raise CompanyNotFound("no website matching '%s' was found; enter the website instead" % q)


def research_one(store: Store, settings: Settings, query: str, deps: Deps, max_seconds: Optional[float] = None) -> Dict[str, Any]:
    """Research a single company end to end (discover -> research -> qualify -> contact -> draft) as its own run.
    `max_seconds` caps research time (used on Vercel so the whole request finishes inside the function limit)."""
    from dataclasses import replace
    from . import costs
    if settings.mock:
        seed_mock_costs(store)
    else:
        costs.apply_config_defaults(store)
    costs.require_ready(store, settings_providers(settings))
    if max_seconds:
        settings = replace(settings, max_seconds_per_company=min(settings.max_seconds_per_company, max_seconds))
    meter_deps, _ = _metered(store, "lookup", None, deps)
    target = resolve_company(store, query, meter_deps)
    inputs = {"domains": [target["domain"]], "use_feeds": False, "use_search": False, "use_portfolios": False,
              "max_research": 1, "single_company": query[:120]}
    run_id = start_run(store, settings, inputs, deps.icp)
    summary = execute_run(store, settings, run_id, inputs, deps)
    co = store.one("SELECT * FROM companies WHERE run_id = %s ORDER BY created_at LIMIT 1", (run_id,)) or {}
    return {"run_id": run_id, "company_id": co.get("id"), "domain": target["domain"], "how": target["how"],
            "qualification": co.get("qualification"), "stage": co.get("research_stage"), "stop_reason": co.get("stop_reason"),
            "summary": summary}
