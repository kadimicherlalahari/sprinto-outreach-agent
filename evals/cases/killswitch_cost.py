"""D31-D33: kill switches and incidents, pilot performance pause, cost caps (company, period, unknown price,
concurrency, explicit resume)."""
from __future__ import annotations

import threading

from evals.fixtures import INBOX, make_draft_row
from evals.harness import Case
from sprinto_agent import costs, review
from sprinto_agent.safety import (ResumeRefused, open_incidents, pause_sending, raise_incident, resolve_incident,
                                  resume_sending, sending_paused, set_campaign_paused)
from sprinto_agent.util import now_iso

CASES = []


def C(**k):
    CASES.append(Case(**k))


def _try(fn):
    try:
        fn()
        return "ok"
    except (ResumeRefused, ValueError) as e:
        return "refused"
    except review.SendBlocked:
        return "blocked"
    except costs.CostCapReached:
        return "error"


# ------------------------------------------------------------------ D31 kill switches
def _resume(env, reason, by, incident=False, resolve=False):
    pause_sending(env.store, "eval pause", "human", "Eval")
    if incident:
        iid = raise_incident(env.store, "unsupported_claim", "x")
        if resolve:
            resolve_incident(env.store, iid, "Eval", "removed claim")
    r = _try(lambda: resume_sending(env.store, reason, by))
    ev = env.store.query("SELECT action, actor, reason FROM control_events WHERE scope = 'global' ORDER BY created_at")
    return {"result": r, "paused": bool(sending_paused(env.store)), "last": [ev[-1]["action"], ev[-1]["actor"]]}


C(id="D31-01", decision="D31", partition="resume with reason and name", input="pause; resume('checked', 'Op')",
  expected="resumed; recorded as a human action with the reason", assertion="paused=False, last=[resume, human]",
  severity="Critical", run=lambda e: _resume(e, "checked", "Op"), expect={"result": "ok", "paused": False, "last": ["resume", "human"]})
C(id="D31-02", decision="D31", partition="resume without reason", input="resume('', 'Op')", expected="refused; stays paused",
  assertion="result=refused, paused=True", severity="Critical", run=lambda e: _resume(e, "", "Op"),
  expect={"result": "refused", "paused": True})
C(id="D31-03", decision="D31", partition="resume without a named person", input="resume('checked', '')", expected="refused",
  assertion="paused=True", severity="Critical", run=lambda e: _resume(e, "checked", ""), expect={"result": "refused", "paused": True})
C(id="D31-04", decision="D31", partition="resume with an open incident", input="incident open; resume('checked','Op')",
  expected="refused until the incident is resolved", assertion="paused=True", severity="Critical",
  run=lambda e: _resume(e, "checked", "Op", incident=True), expect={"result": "refused", "paused": True})
C(id="D31-05", decision="D31", partition="resume after resolving the incident", input="incident resolved with a note; resume",
  expected="resumed", assertion="paused=False", severity="High", run=lambda e: _resume(e, "checked", "Op", True, True),
  expect={"result": "ok", "paused": False})


def _queued(env):
    env.run()
    d = env.draft()
    review.approve(env.store, d["id"], "Eval", recipient=INBOX)
    ids = [d["id"]]
    for i in range(2):
        x = make_draft_row(env.store, d["company_id"], d["poc_id"], d["run_id"], "s%d" % i, "body %d" % i)
        review.approve(env.store, x, "Eval", recipient=INBOX)
        ids.append(x)
    raise_incident(env.store, "uncertain_send", "elsewhere")
    res = [_try(lambda i=i: review.send(env.store, env.live(), i, gmail=env.gmail)) for i in ids]
    return {"results": res, "gmail_messages": env.sent_messages(), "incident_actor":
            env.store.query("SELECT actor FROM control_events WHERE action = 'pause'")[0]["actor"]}


C(id="D31-06", decision="D31", partition="incident pauses all queued sends", input="3 approved drafts; an incident is raised; try each",
  expected="all blocked; pause recorded as a system action", assertion="results=[blocked x3], gmail_messages=0", severity="Critical",
  run=_queued, expect={"results": ["blocked"] * 3, "gmail_messages": 0, "incident_actor": "system"})


def _campaign(env, reason):
    set_campaign_paused(env.store, "pilot", True, "Eval", "pause")
    r = _try(lambda: set_campaign_paused(env.store, "pilot", False, "Eval", reason))
    return {"result": r, "status": env.store.one("SELECT status FROM campaigns WHERE id = 'pilot'")["status"]}


C(id="D31-07", decision="D31", partition="campaign resume without reason", input="pause campaign; resume with ''",
  expected="refused", assertion="status=paused", severity="Critical", run=lambda e: _campaign(e, ""),
  expect={"result": "refused", "status": "paused"})
C(id="D31-08", decision="D31", partition="campaign resume with reason", input="resume with 'reviewed'", expected="active",
  assertion="status=active", severity="High", run=lambda e: _campaign(e, "reviewed"), expect={"result": "ok", "status": "active"})


def _resolve_uncertain_first(env):
    iid = raise_incident(env.store, "uncertain_send", "x", send_id=None)
    env.run()
    d = env.draft()
    sid = env.store.insert("sends", {"draft_id": d["id"], "content_hash": d["current_hash"], "recipient": INBOX, "mode": "live",
                                     "status": "uncertain", "sent_at": now_iso(), "live_lock": d["id"]})
    iid2 = raise_incident(env.store, "uncertain_send", "y", draft_id=d["id"], send_id=sid)
    return _try(lambda: resolve_incident(env.store, iid2, "Eval", "looked"))


C(id="D31-09", decision="D31", partition="close uncertain-send incident before resolving the send", input="send still 'uncertain'",
  expected="refused: mark the send sent/not sent first", assertion="refused", severity="Critical", run=_resolve_uncertain_first,
  expect="refused")


# ------------------------------------------------------------------ D32 performance pause
def _perf(env, n, bounced, min_sample=10, threshold=0.10, test=False, apply=True):
    from sprinto_agent.report import configure_pilot, performance
    from datetime import date
    env.run()
    d = env.draft()
    if threshold is not None:
        configure_pilot(env.store, "pilot", "Eval", {"max_bounce_rate": threshold}, min_sample, date.today().isoformat())
    for i in range(n):
        env.store.insert("sends", {"draft_id": d["id"], "content_hash": "h%d" % i, "recipient": "x", "mode": "live", "status": "sent",
                                   "sent_at": now_iso(), "is_test": test, "campaign_id": "pilot", "reply_status": "not_tracked",
                                   "delivery_status": "bounced" if i < bounced else "accepted_by_gmail"})
    p = performance(env.store, "pilot", apply=apply)
    paused = bool(env.store.one("SELECT performance_paused FROM campaigns WHERE id = 'pilot'")["performance_paused"])
    return {"n": p["n"], "rate": p["rates"]["bounce_rate"], "paused": paused, "status": p["status"].split(":")[0]}


for b, n, bounced, paused in (("below", 9, 5, False), ("at", 10, 5, True), ("above", 11, 5, True)):
    C(id="D32-sample-%s" % b, decision="D32", partition="minimum sample (10)", boundary=b,
      input="%d prospect sends, %d bounced, max bounce 10%%, min sample 10" % (n, bounced),
      expected="paused" if paused else "no conclusion below the minimum sample", assertion="paused=%s" % paused, severity="Critical",
      run=lambda e, n=n, bo=bounced: _perf(e, n, bo), expect={"n": n, "paused": paused})
for b, bounced, paused in (("below", 0, False), ("at", 1, False), ("above", 2, True)):
    C(id="D32-rate-%s" % b, decision="D32", partition="threshold rate (10%, strictly greater pauses)", boundary=b,
      input="10 sends, %d bounced (%d%%)" % (bounced, bounced * 10), expected="paused" if paused else "within threshold",
      assertion="paused=%s" % paused, severity="High", run=lambda e, bo=bounced: _perf(e, 10, bo), expect={"paused": paused},
      assumption="'At' the threshold is treated as within it (rate must EXCEED the threshold to pause)." if b == "at" else "")
C(id="D32-01", decision="D32", partition="no thresholds configured", input="50 sends, 40 bounced, no thresholds",
  expected="never paused; status says not configured", assertion="paused=False, status=not configured", severity="High",
  run=lambda e: _perf(e, 50, 40, threshold=None), expect={"paused": False, "status": "not configured"})
C(id="D32-02", decision="D32", partition="test/demo sends only", input="20 own-inbox test sends, all bounced",
  expected="excluded from prospect metrics: n=0, not paused", assertion="n=0, paused=False", severity="Critical",
  run=lambda e: _perf(e, 20, 20, test=True), expect={"n": 0, "paused": False})


def _perf_blocks(env):
    _perf(env, 10, 9)
    d = env.draft()
    review.approve(env.store, d["id"], "Eval", recipient=INBOX)
    return _try(lambda: review.send(env.store, env.live(), d["id"], gmail=env.gmail))


C(id="D32-03", decision="D32", partition="performance pause blocks sending", input="breach -> pause; try to send an approved draft",
  expected="blocked", assertion="blocked", severity="Critical", run=_perf_blocks, expect="blocked")


# ------------------------------------------------------------------ D33 cost caps
def _reserve_n(env, n, provider="tavily", per_company=0.03, monthly=100.0, prices=None, limits=None, companies=None):
    env.set_caps(per_company, monthly, prices, limits)
    out = []
    for i in range(n):
        m = costs.Meter(env.store, "r", (companies or ["c"])[i % len(companies or ["c"])])
        try:
            m.reserve(provider, 1)
            out.append("ok")
        except costs.CostCapReached as e:
            out.append("blocked:" + e.scope.split(":")[0])
    return out


for b, n in (("below", 2), ("at", 3), ("above", 4)):
    exp = ["ok"] * min(n, 3) + ["blocked:company"] * max(0, n - 3)
    C(id="D33-company-%s" % b, decision="D33", partition="per-company cap ($0.03, $0.01/search)", boundary=b,
      input="%d searches for one company" % n, expected="calls %s the cap allowed, the next one blocked" % b,
      assertion="results == %s" % exp, severity="Critical", run=lambda e, n=n: _reserve_n(e, n), expect=exp)
for b, n in (("below", 4), ("at", 5), ("above", 6)):
    exp = ["ok"] * min(n, 5) + ["blocked:period"] * max(0, n - 5)
    C(id="D33-period-%s" % b, decision="D33", partition="monthly cap ($0.05) across companies", boundary=b,
      input="%d searches spread over 5 companies" % n, expected="blocked only beyond the cap", assertion="results == %s" % exp,
      severity="Critical", run=lambda e, n=n: _reserve_n(e, n, per_company=1.0, monthly=0.05, companies=["a", "b", "c", "d", "e"]),
      expect=exp)
for b, n in (("below", 2), ("at", 3), ("above", 4)):
    exp = ["ok"] * min(n, 3) + ["blocked:unit_limit"] * max(0, n - 3)
    C(id="D33-unit-%s" % b, decision="D33", partition="unknown price -> monthly unit limit (3)", boundary=b,
      input="hunter price unknown, limit 3, %d lookups" % n, expected="blocked only beyond the limit", assertion="results == %s" % exp,
      severity="Critical", run=lambda e, n=n: _reserve_n(e, n, provider="hunter", prices={"hunter": {"price": None}},
                                                          limits={"hunter": 3}), expect=exp)


def _unknown_recorded(env):
    _reserve_n(env, 2, provider="hunter", prices={"hunter": {"price": None}}, limits={"hunter": 5})
    rows = env.store.query("SELECT est_cost, cost_basis, actual_cost FROM cost_ledger")
    return {"never_zero": all(r["est_cost"] is None for r in rows), "basis": sorted({r["cost_basis"] for r in rows}),
            "no_actual": all(r["actual_cost"] is None for r in rows)}


C(id="D33-01", decision="D33", partition="unknown price recorded", input="2 Hunter lookups, price unknown",
  expected="cost NULL (never 0), basis unknown_price, no 'actual' claimed", assertion="never_zero, basis=[unknown_price]",
  severity="High", run=_unknown_recorded, expect={"never_zero": True, "basis": ["unknown_price"], "no_actual": True})


def _concurrent(env):
    env.set_caps(per_company=1.0, monthly=0.10)
    ok = []

    def w(i):
        try:
            costs.Meter(env.store, "r", "c%d" % (i % 5)).reserve("tavily", 1)
            ok.append(1)
        except costs.CostCapReached:
            pass
    ts = [threading.Thread(target=w, args=(i,)) for i in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    total = env.store.one("SELECT SUM(est_cost) AS c FROM cost_ledger")["c"]
    return {"allowed": len(ok), "within_cap": round(total, 8) <= 0.10}


C(id="D33-02", decision="D33", partition="concurrent calls at the period boundary", input="30 threads, $0.01 each, monthly cap $0.10",
  expected="exactly 10 allowed; no overrun", assertion="allowed=10, within_cap", severity="Critical", run=_concurrent,
  expect={"allowed": 10, "within_cap": True})


def _no_caps(env):
    try:
        costs.Meter(env.store, "r", "c").reserve("tavily", 1)
        return "ok"
    except costs.CostCapReached:
        return "blocked"


C(id="D33-03", decision="D33", partition="no caps configured", input="empty cost settings; reserve 1 search",
  expected="blocked, nothing billed", assertion="blocked", severity="Critical", run=_no_caps, expect="blocked")


def _raise_cap_no_resume(env):
    _reserve_n(env, 6, per_company=1.0, monthly=0.05, companies=["a", "b", "c", "d", "e", "f"])
    paused_before = bool(costs.billable_paused(env.store))
    env.set_caps(per_company=1.0, monthly=50.0)
    after_raise = _try(lambda: costs.Meter(env.store, "r", "z").reserve("tavily", 1))
    r1 = _try(lambda: costs.resume_billable(env.store, "", "Eval"))
    r2 = _try(lambda: costs.resume_billable(env.store, "raised budget after review", "Eval"))
    after_resume = _try(lambda: costs.Meter(env.store, "r", "z").reserve("tavily", 1))
    return {"paused_at_cap": paused_before, "after_raising_cap": after_raise, "resume_no_reason": r1,
            "resume_with_reason": r2, "after_resume": after_resume}


C(id="D33-04", decision="D33", partition="cap raised while paused", input="hit monthly cap -> paused; raise cap; resume without/with reason",
  expected="raising the cap does not resume; resume needs a reason", assertion="after_raising_cap=blocked, then ok",
  severity="Critical", run=_raise_cap_no_resume,
  expect={"paused_at_cap": True, "after_raising_cap": "error", "resume_no_reason": "refused", "resume_with_reason": "ok",
          "after_resume": "ok"})


def _e2e_company_cap(env):
    from sprinto_agent.llm import MockLLM
    env.set_caps(per_company=0.0015, monthly=100.0)
    env.run(deps=env.deps(llm=MockLLM()))
    co = env.company("ledgerly.example")
    spent = env.store.one("SELECT SUM(est_cost) AS c FROM cost_ledger WHERE company_id = %s", (co["id"],))["c"]
    return {"stop_code": co["stop_code"], "lists_unresolved": "unresolved" in (co["stop_reason"] or ""),
            "findings_kept": env.count("SELECT id FROM evidence WHERE company_id = %s", (co["id"],)) > 0,
            "within_cap": round(spent, 8) <= 0.0015}


C(id="D33-05", decision="D33", partition="company cap reached during research (end-to-end)", input="cap $0.0015; model calls ~$0.0013 each",
  expected="research stops at the cap, findings kept, unresolved facts listed", assertion="stop_code=cost_cap_reached, within_cap",
  severity="Critical", run=_e2e_company_cap,
  expect={"stop_code": "cost_cap_reached", "lists_unresolved": True, "findings_kept": True, "within_cap": True})
