"""D01-D04: run lock, stale-run recovery, stage-wise resume, candidate skipping."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from evals.harness import Case
from sprinto_agent.contacts import MockHunter
from sprinto_agent.mock_data import mock_hunter

CASES = []


def C(**k):
    CASES.append(Case(**k))


# ------------------------------------------------------------------ D01 run lock
def _start(env, n=1, threads=False):
    from sprinto_agent.pipeline import RunAlreadyActive, start_run
    s = env.settings()
    ok, refused = [], []

    def one():
        try:
            ok.append(start_run(env.store, s, {"domains": []}, env.deps().icp))
        except RunAlreadyActive:
            refused.append(1)
    if threads:
        ts = [threading.Thread(target=one) for _ in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    else:
        for _ in range(n):
            one()
    return {"started": len(ok), "refused": len(refused), "runs_in_db": env.count("SELECT id FROM research_runs")}


C(id="D01-01", decision="D01", partition="no active run", input="start_run once on an empty store",
  expected="run is created", assertion="started=1, runs_in_db=1", severity="High", run=lambda e: _start(e, 1),
  expect={"started": 1, "refused": 0, "runs_in_db": 1})
C(id="D01-02", decision="D01", partition="repeated Run clicks (sequential)", input="start_run 5 times while the first is still active",
  expected="first starts; the other 4 are refused, no duplicate job", assertion="started=1, refused=4, runs_in_db=1",
  severity="High", run=lambda e: _start(e, 5), expect={"started": 1, "refused": 4, "runs_in_db": 1})
C(id="D01-03", decision="D01", partition="concurrent workers", input="8 threads call start_run at the same time",
  expected="exactly one run is created (unique active_lock)", assertion="started=1, runs_in_db=1", severity="High",
  run=lambda e: _start(e, 8, threads=True), expect={"started": 1, "refused": 7, "runs_in_db": 1})


def _ui_clicks(env):
    from fastapi.testclient import TestClient
    from sprinto_agent.mock_data import mock_deps
    from sprinto_agent.pipeline import start_run
    from sprinto_agent.web import create_app
    s = env.settings()
    c = TestClient(create_app(mock=True, store=env.store, settings=s, deps_factory=lambda st: mock_deps()))
    start_run(env.store, s, {"domains": []}, mock_deps().icp)          # a run is in progress
    texts = [c.post("/run", data={"domains": "ledgerly.example", "max_research": "1"}, follow_redirects=True).text
             for _ in range(3)]
    return {"runs_in_db": env.count("SELECT id FROM research_runs"),
            "all_refused_visibly": all("already in progress" in t for t in texts)}


C(id="D01-04", decision="D01", partition="repeated Run clicks through the web UI", input="POST /run three times while a run is active",
  expected="each click is refused with a visible message; no new run", assertion="runs_in_db=1, refusal shown",
  severity="High", run=_ui_clicks, expect={"runs_in_db": 1, "all_refused_visibly": True})


def _after_complete(env):
    env.run()
    from sprinto_agent.pipeline import start_run
    rid = start_run(env.store, env.settings(), {"domains": []}, env.deps().icp)
    return {"second_started": bool(rid), "runs_in_db": env.count("SELECT id FROM research_runs")}


C(id="D01-05", decision="D01", partition="previous run finished", input="complete a run, then start another",
  expected="lock was released; new run starts", assertion="second_started=True", severity="High",
  run=_after_complete, expect={"second_started": True, "runs_in_db": 2})


# ------------------------------------------------------------------ D02 stale heartbeat
def _stale(env, minutes_ago):
    from sprinto_agent.pipeline import recover_stale_runs, start_run
    rid = start_run(env.store, env.settings(), {"domains": []}, env.deps().icp)
    beat = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    env.store.update("research_runs", rid, {"heartbeat_at": beat, "status": "running"})
    recover_stale_runs(env.store, 15)
    r = env.store.one("SELECT status, active_lock FROM research_runs WHERE id = %s", (rid,))
    return {"status": r["status"], "lock_released": r["active_lock"] is None}


C(id="D02-01", decision="D02", partition="fresh heartbeat", boundary="below", input="heartbeat 14m59s ago, STALE_RUN_MINUTES=15",
  expected="run stays running", assertion="status=running, lock kept", severity="High",
  run=lambda e: _stale(e, 14 + 59 / 60), expect={"status": "running", "lock_released": False})
C(id="D02-02", decision="D02", partition="stale heartbeat", boundary="above", input="heartbeat 15m01s ago",
  expected="run marked interrupted and lock released so it can be resumed", assertion="status=interrupted, lock released",
  severity="High", run=lambda e: _stale(e, 15 + 1 / 60), expect={"status": "interrupted", "lock_released": True})
C(id="D02-03", decision="D02", partition="stale heartbeat", boundary="at", input="heartbeat exactly 15m00s ago",
  expected="treated as stale (strictly older than the cutoff by the time the check runs)", assertion="status=interrupted",
  severity="Low", run=lambda e: _stale(e, 15), expect={"status": "interrupted", "lock_released": True},
  assumption="'Exactly at' the stale threshold is not specified; the check runs a few ms later, so an exactly-15-minute "
             "heartbeat is already past the cutoff.")


# ------------------------------------------------------------------ D03 restart after each stage
class CountingHunter(MockHunter):
    calls = 0

    def domain_search(self, domain, limit=10):
        CountingHunter.calls += 1
        return super().domain_search(domain, limit)


def _restart_after(env, stage):
    from sprinto_agent.pipeline import resume_run
    CountingHunter.calls = 0
    r = env.run()
    rid = r["run_id"]
    co = env.company("ledgerly.example")
    before = {"evidence": env.count("SELECT id FROM evidence WHERE company_id = %s", (co["id"],))}
    if stage in ("researched", "pending", "researching"):
        env.store.execute("DELETE FROM draft_revisions")
        env.store.execute("DELETE FROM drafts")
        env.store.execute("DELETE FROM pocs")
    if stage == "contact":
        env.store.execute("DELETE FROM draft_revisions")
        env.store.execute("DELETE FROM drafts")
    env.store.update("companies", co["id"], {"research_stage": stage})
    env.store.update("research_runs", rid, {"status": "interrupted", "active_lock": None})
    deps = env.deps()
    deps.hunter = CountingHunter(mock_hunter())
    resume_run(env.store, env.settings(), rid, deps)
    return {"evidence_same": env.count("SELECT id FROM evidence WHERE company_id = %s", (co["id"],)) == before["evidence"],
            "pocs": env.count("SELECT id FROM pocs"), "drafts": env.count("SELECT id FROM drafts WHERE status <> 'superseded'"),
            "hunter_calls": CountingHunter.calls, "sends": env.count("SELECT id FROM sends"),
            "run_status": env.store.one("SELECT status FROM research_runs WHERE id = %s", (rid,))["status"]}


for i, (stage, hunter_calls, note) in enumerate([
        ("researching", 1, "crashed mid-research: partial evidence replaced, not duplicated"),
        ("researched", 1, "crashed before contact lookup: lookup runs once"),
        ("contact", 0, "crashed before drafting: contact reused, Hunter not called again"),
        ("done", 0, "already finished: nothing repeated")], 1):
    C(id="D03-%02d" % i, decision="D03", partition="restart after stage '%s'" % stage,
      input="complete Ledgerly run, roll company back to stage=%s (removing later-stage rows), mark run interrupted, resume" % stage,
      expected=note + "; never sends", assertion="1 POC, 1 draft, evidence count unchanged, Hunter calls=%d, sends=0" % hunter_calls,
      severity="Critical" if stage == "done" else "High", run=lambda e, s=stage: _restart_after(e, s),
      expect={"evidence_same": True, "pocs": 1, "drafts": 1, "hunter_calls": hunter_calls, "sends": 0, "run_status": "completed"})


def _resume_twice(env):
    from sprinto_agent.pipeline import resume_run
    r = env.run()
    env.store.update("research_runs", r["run_id"], {"status": "interrupted", "active_lock": None})
    resume_run(env.store, env.settings(), r["run_id"], env.deps())
    env.store.update("research_runs", r["run_id"], {"status": "interrupted", "active_lock": None})
    resume_run(env.store, env.settings(), r["run_id"], env.deps())
    return {"drafts": env.count("SELECT id FROM drafts"), "pocs": env.count("SELECT id FROM pocs"),
            "sends": env.count("SELECT id FROM sends")}


C(id="D03-05", decision="D03", partition="resume repeated", input="resume the same finished run twice",
  expected="no duplicate POC, draft or send", assertion="drafts=1, pocs=1, sends=0", severity="Critical",
  run=_resume_twice, expect={"drafts": 1, "pocs": 1, "sends": 0})


def _resume_while_active(env):
    from sprinto_agent.pipeline import RunAlreadyActive, resume_run, start_run
    r = env.run()
    start_run(env.store, env.settings(), {"domains": []}, env.deps().icp)          # another run now active
    env.store.update("research_runs", r["run_id"], {"status": "interrupted"})
    try:
        resume_run(env.store, env.settings(), r["run_id"], env.deps())
        return "resumed"
    except RunAlreadyActive:
        return "refused"


C(id="D03-06", decision="D03", partition="resume while another run is active", input="run A interrupted, run B active, resume A",
  expected="refused (one active run at a time)", assertion="RunAlreadyActive raised", severity="High",
  run=_resume_while_active, expect="refused")


# ------------------------------------------------------------------ D04 candidate skipping
def _skip(env, domains, max_research=8, suppress=None, second_run=False):
    from sprinto_agent.safety import add_suppression
    if suppress:
        add_suppression(env.store, "domain", suppress, "eval")
    if second_run:
        env.run()
    r = env.run(domains=domains, max_research=max_research)
    rows = env.store.query("SELECT domain, research_stage, stop_code FROM companies WHERE run_id = %s ORDER BY created_at",
                           (r["run_id"],))
    return {x["domain"]: ("skipped:" + (x["stop_code"] or "")) if x["research_stage"] == "skipped" else "researched"
            for x in rows}


C(id="D04-01", decision="D04", partition="opted-out domain", input="suppress ledgerly.example, run [ledgerly.example]",
  expected="not researched", assertion="ledgerly skipped", severity="Critical",
  run=lambda e: _skip(e, ["ledgerly.example"], suppress="ledgerly.example"), expect={"ledgerly.example": "skipped:skipped"})
C(id="D04-02", decision="D04", partition="already in pipeline", input="Ledgerly has an open draft from run 1; run 2 includes it",
  expected="not researched again", assertion="ledgerly skipped in run 2", severity="High",
  run=lambda e: _skip(e, ["ledgerly.example"], second_run=True), expect={"ledgerly.example": "skipped:skipped"})
C(id="D04-03", decision="D04", partition="run limit", boundary="below", input="max_research=2, domains [ledgerly]",
  expected="researched", assertion="not skipped", severity="Low",
  run=lambda e: _skip(e, ["ledgerly.example"], 2), expect={"ledgerly.example": "researched"})
C(id="D04-04", decision="D04", partition="run limit", boundary="at", input="max_research=2, domains [ledgerly, quietforms]",
  expected="both researched", assertion="neither skipped", severity="Low",
  run=lambda e: _skip(e, ["ledgerly.example", "quietforms.example"], 2),
  expect={"ledgerly.example": "researched", "quietforms.example": "researched"})
C(id="D04-05", decision="D04", partition="run limit", boundary="above", input="max_research=1, domains [ledgerly, quietforms]",
  expected="second company skipped with run_limit", assertion="quietforms skipped, stop_code=run_limit", severity="Low",
  run=lambda e: _skip(e, ["ledgerly.example", "quietforms.example"], 1),
  expect={"ledgerly.example": "researched", "quietforms.example": "skipped:run_limit"})
C(id="D04-06", decision="D04", partition="competitor rejected at discovery", input="domains [drata.example]",
  expected="skipped, never researched", assertion="drata skipped", severity="High",
  run=lambda e: _skip(e, ["drata.example"]), expect={"drata.example": "skipped:skipped"})
