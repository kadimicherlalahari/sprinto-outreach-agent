"""Test cases from the class evals deck (MESA T2-L7 'Evals', 12 Nov 2026, Sprinto pipeline, Decisions 1-8).
Every row on the slides is here (IDs end in -S<n>), plus the partitions and boundaries the slides leave out
(IDs end in -X<n>). Each case runs through the app's own interfaces; nothing in the app is changed to make a test
pass. Decisions 7 and 8 (call scheduling, meeting booking, CRM) are not built in this app: those rows are listed
as NOT BUILT so they are visible in the report but are never counted as passing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from evals.fixtures import ALL_CHECKED, ICP, INBOX, PROSPECT, TOPIC, ev, make_draft_row, raw_mail
from evals.harness import Case
from evals.judge import judge_edit
from sprinto_agent import review
from sprinto_agent.qualify import Budget, assess, stop_decision

CASES = []


def C(**k):
    CASES.append(Case(**k))


RECENT = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()


# ------------------------------------------------------------------ C1 shortlist (class Decision 1)
def strong(stage="series_b", saas="true", size=None, cert=None, size_tier="1", size_status="verified"):
    """All other conditions pass: B2B SaaS, stage in A-C (recent round), SOC 2 in progress (a verified trigger)."""
    trigger = ev("enterprise_selling", "enterprise_plan") if cert else ev("compliance_in_progress", "soc2")
    evs = [ev("is_b2b_saas", saas), ev("funding_stage", stage, published=RECENT), trigger]
    if size is not None:
        evs.append(ev("employee_count", str(size), tier=size_tier, status=size_status))
    if cert:
        evs.append(ev("certified", cert))
    return evs


def _shortlist(env, evs, coverage=ALL_CHECKED):
    a = assess(evs, ICP, "Acme", "acme.example", coverage=coverage)
    return {"shortlisted": a.decision == "qualified", "decision": a.decision, "reasons": " | ".join(a.reasons)}


def _reason_has(*words):
    def judge(o):
        r = o["reasons"].lower()
        missing = [w for w in words if w not in r]
        ok = o["shortlisted"] and not missing if "near" in words else (not o["shortlisted"] and not missing)
        return {"label": "pass" if ok else "fail",
                "reason": ("shortlisted=%s; reasoning: %s" % (o["shortlisted"], o["reasons"][:160])) +
                          ("" if not missing else "; missing %s" % missing)}
    return judge


C(id="C1-S1", decision="C1", partition="funding stage outside A-C: Seed", input="Funding stage = Seed (outside Series A-C)",
  expected="Rejected", assertion="shortlisted == false", severity="Low",
  run=lambda e: _shortlist(e, strong("seed")), expect={"shortlisted": False, "decision": "rejected"})
C(id="C1-S2", decision="C1", partition="not B2B SaaS", input="Funding stage = Series B, B2B SaaS = No",
  expected="Rejected", assertion='shortlisted == false AND reason == "not B2B SaaS"', severity="Low",
  run=lambda e: (lambda o: {"shortlisted": o["shortlisted"], "reason_not_b2b": "not B2B SaaS" in o["reasons"]})(_shortlist(e, strong(saas="false"))),
  expect={"shortlisted": False, "reason_not_b2b": True})
C(id="C1-S3", decision="C1", partition="already certified", input="Certifications already procured = Yes (verified SOC 2)",
  expected="Rejected (already has what we'd sell)", assertion='shortlisted == false AND reason == "already certified"',
  severity="High", run=lambda e: (lambda o: {"shortlisted": o["shortlisted"], "reason_certified": "already certified" in o["reasons"]})(
      _shortlist(e, strong(cert="soc2"))), expect={"shortlisted": False, "reason_certified": True})
C(id="C1-S4", decision="C1", partition="size moderately below band (45)", boundary="below",
  input="Size = 45 employees (just under 50), all other conditions pass",
  expected="Accepted: soft boundary, reasonably close", assertion="Judge: shortlisted == true, reasoning notes size is near-band",
  severity="High", kind="soft", run=lambda e: _shortlist(e, strong(size=45)), judge=_reason_has("near"), expect="pass")
C(id="C1-S5", decision="C1", partition="size moderately above band (220)", boundary="above",
  input="Size = 220 employees (just over 200), all other conditions pass",
  expected="Accepted: same soft boundary logic, upper side", assertion="Judge: shortlisted == true, reasoning consistent with S4",
  severity="High", kind="soft", run=lambda e: _shortlist(e, strong(size=220)), judge=_reason_has("near"), expect="pass")
C(id="C1-S6", decision="C1", partition="size far outside band (600)", input="Size = 600 employees, all other three conditions pass",
  expected="Rejected: soft judgment still has a limit", assertion="Judge: shortlisted == false, reasoning states size is far outside",
  severity="Critical", kind="soft", run=lambda e: _shortlist(e, strong(size=600)), judge=_reason_has("far outside"), expect="pass")
# --- partitions the slides leave out
for i, (stage, ok) in enumerate((("series_a", True), ("series_c", True), ("series_d_plus", False), ("public", False)), 1):
    C(id="C1-X%d" % i, decision="C1", partition="funding stage %s" % stage, input="Funding stage = %s, all else passes" % stage,
      expected="Accepted" if ok else "Rejected", assertion="shortlisted == %s" % ok, severity="Low" if ok else "High",
      run=lambda e, s=stage: _shortlist(e, strong(s))["shortlisted"], expect=ok)
C(id="C1-X5", decision="C1", partition="funding stage unknown", input="no funding evidence; B2B SaaS + trigger verified",
  expected="Not shortlisted yet, not rejected: uncertain, flagged for more research", assertion="decision == uncertain",
  severity="Critical", run=lambda e: _shortlist(e, [ev("is_b2b_saas", "true"), ev("compliance_in_progress", "soc2")], None)["decision"],
  expect="uncertain")
C(id="C1-X6", decision="C1", partition="B2B SaaS unknown", input="Series B + trigger verified, no SaaS evidence",
  expected="uncertain (never silently rejected)", assertion="decision == uncertain", severity="Critical",
  run=lambda e: _shortlist(e, [ev("funding_stage", "series_b", published=RECENT), ev("compliance_in_progress", "soc2")], None)["decision"],
  expect="uncertain")
for i, (size, ok, b) in enumerate(((50, True, "at"), (200, True, "at"), (40, True, "at"), (39, False, "above"),
                                    (300, True, "at"), (301, False, "above"), (120, True, "")), 7):
    C(id="C1-X%d" % i, decision="C1", partition="size %d (band 50-200, soft 40-300)" % size, boundary=b,
      input="Size = %d (verified), all else passes" % size, expected="Accepted" if ok else "Rejected (far outside)",
      assertion="shortlisted == %s" % ok, severity="High", run=lambda e, n=size: _shortlist(e, strong(size=n))["shortlisted"],
      expect=ok)
C(id="C1-X14", decision="C1", partition="size unknown", input="no headcount evidence, all else passes",
  expected="Accepted; size stays an open question (never rejects)", assertion="shortlisted == true", severity="High",
  run=lambda e: _shortlist(e, strong(), coverage=None)["shortlisted"], expect=True)
C(id="C1-X15", decision="C1", partition="far-outside size only from a third-party estimate",
  input="Size 600 from a tier-3 estimate (inferred), all else passes", expected="Not rejected on an estimate; accepted, size unresolved",
  assertion="shortlisted == true", severity="Critical",
  run=lambda e: _shortlist(e, strong(size=600, size_tier="3", size_status="inferred"))["shortlisted"], expect=True,
  assumption="Only VERIFIED size disqualifies; an estimate is too weak to silently reject a good lead.")
C(id="C1-X16", decision="C1", partition="certification only in progress", input="SOC 2 in progress (not held)",
  expected="Accepted (this is the buying signal, not a disqualifier)", assertion="shortlisted == true", severity="Critical",
  run=lambda e: _shortlist(e, strong())["shortlisted"], expect=True)
C(id="C1-X17", decision="C1", partition="interaction: borderline size AND weak other signals",
  input="Size 45 (near band), B2B SaaS + Series B but no verified trigger",
  expected="Not shortlisted: borderline size does not compensate for missing signals", assertion="shortlisted == false",
  severity="High", run=lambda e: _shortlist(e, [ev("is_b2b_saas", "true"), ev("funding_stage", "series_b"),
                                               ev("employee_count", "45")])["shortlisted"], expect=False,
  assumption="The slides do not define an interaction rule; the app evaluates size independently of other signals. "
             "A lead needs >=1 verified trigger regardless of size.")


# ------------------------------------------------------------------ C2 sufficiency (class Decision 2, soft)
def _research(env, pages=None, domain="ledgerly.example", drop=None):
    from sprinto_agent.research import Researcher
    d = env.deps(pages=pages, drop=drop)
    r = Researcher(d.fetcher, d.search, None, ICP, d.sources, max_pages=12, max_rounds=4).research(domain, None)
    return {"stop_code": r.stop_code, "open": r.assessment.open_questions, "decision": r.assessment.decision,
            "stale": any("stale" in e.flags for e in r.evidence), "recent_funding": r.assessment.criteria["recent_funding"].value,
            "identity_mismatch": any("identity_mismatch" in e.flags for e in r.evidence)}


def sufficiency(o):
    ok = o["stop_code"] in ("decision_stable", "rejected") and not o["open"]
    return {"label": "sufficient" if ok else "insufficient",
            "reason": "stop=%s, open questions=%s" % (o["stop_code"], o["open"])}


C(id="C2-S1", decision="C2", partition="full firmographic + contact + recent news", input="Ledgerly fixture: site, team page, dated Series B press",
  expected="Judged sufficient, loop exits", assertion="Judge: sufficient == true", severity="Low", kind="soft",
  run=lambda e: _research(e), judge=sufficiency, expect="sufficient")
C(id="C2-S2", decision="C2", partition="only the company name", input="Quietforms: homepage says only what it does; nothing else found",
  expected="Judged insufficient, more research triggered until sources run out", assertion="Judge: sufficient == false",
  severity="High", kind="soft", run=lambda e: _research(e, domain="quietforms.example"), judge=sufficiency, expect="insufficient")


def _stale(env):
    old = (datetime.now(timezone.utc) - timedelta(days=3 * 365)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    from evals.fixtures import page
    o = _research(env, pages={"https://ledgerly.example/press": page("Press", "<p>Ledgerly announces an $18M Series B to expand across Europe.</p>", old)})
    return o


C(id="C2-S3", decision="C2", partition="complete but 3 years stale", input="Ledgerly with its Series B announcement dated 3 years ago",
  expected="Insufficient on recency, not just presence", assertion="Judge catches staleness (recent_funding = no, evidence flagged stale)",
  severity="Critical", kind="soft", run=_stale,
  judge=lambda o: {"label": "pass" if o["stale"] and o["recent_funding"] == "no" else "fail",
                   "reason": "stale flag=%s, recent_funding=%s" % (o["stale"], o["recent_funding"])}, expect="pass")


def _collision(env):
    from evals.fixtures import page
    from sprinto_agent.search import MockSearch, SearchHit
    from sprinto_agent.research import Researcher
    pages = {"https://news.example/ledgerly-other": page("Ledgerly raises", "<p>Ledgerly raised a Series A this month.</p>"
                                                         "<a href='https://ledgerlyhq.example/'>Ledgerly</a>", RECENT)}
    d = env.deps(pages=pages, drop=["https://ledgerly.example/press"])
    d.search = MockSearch({"ledgerly": [SearchHit(title="Ledgerly raises", url="https://news.example/ledgerly-other", snippet="")]})
    r = Researcher(d.fetcher, d.search, None, ICP, d.sources, max_pages=12, max_rounds=4).research("ledgerly.example", "Ledgerly")
    acc = [e for e in r.evidence if "news.example" in e.source_url and e.status != "rejected"]
    return {"mismatch_flagged": any("identity_mismatch" in e.flags for e in r.evidence), "accepted_from_other_company": len(acc)}


C(id="C2-S4", decision="C2", partition="data belongs to a different company (name collision)",
  input="Article about 'Ledgerly' that links ledgerlyhq.example, not ledgerly.example",
  expected="Flagged, not accepted as a match", assertion="Judge: insufficient, reason mentions mismatch", severity="Critical",
  kind="soft", run=_collision,
  judge=lambda o: {"label": "pass" if o["mismatch_flagged"] and o["accepted_from_other_company"] == 0 else "fail",
                   "reason": "identity_mismatch=%s, accepted=%d" % (o["mismatch_flagged"], o["accepted_from_other_company"])},
  expect="pass")


# ------------------------------------------------------------------ C3 retry cap (class Decision 3, hard)
def _cap(env, used, cap=3):
    a = assess([ev("is_b2b_saas", "true")], ICP, "Acme", "acme.example")
    b = Budget(max_pages=50, max_rounds=cap)
    b.rounds_used = used
    cont, reason, code = stop_decision(a, b, True)
    escalated = (not cont) and code.startswith("budget_") and a.could_change
    return {"status": "continue" if cont else ("escalated" if escalated else code)}


C(id="C3-S1", decision="C3", partition="retry 2 of 3", boundary="below", input="retry_count = 2, cap = 3",
  expected="Loop continues", assertion="retry_count < cap -> continue", severity="Low", run=lambda e: _cap(e, 2), expect={"status": "continue"})
C(id="C3-S2", decision="C3", partition="retry 3 of 3 (at cap)", boundary="at", input="retry_count = 3, cap = 3",
  expected="Loop stops, escalates (not a 4th silent retry)", assertion='retry_count == cap -> status == "escalated"',
  severity="Critical", run=lambda e: _cap(e, 3), expect={"status": "escalated"},
  assumption="'Escalated' in this app = stopped on the budget with could_change=true: the company shows as Uncertain "
             "with the unresolved facts listed, and appears in the operator's review instead of retrying.")
C(id="C3-X1", decision="C3", partition="retry 4 of 3 (beyond cap)", boundary="above", input="retry_count = 4, cap = 3",
  expected="Still stopped/escalated, never continues", assertion='status == "escalated"', severity="Critical",
  run=lambda e: _cap(e, 4), expect={"status": "escalated"})


def _e2e_rounds(env, cap):
    from sprinto_agent.research import Researcher
    d = env.deps()
    r = Researcher(d.fetcher, d.search, None, ICP, d.sources, max_pages=12, max_rounds=cap).research("quietforms.example")
    return {"rounds_used_le_cap": r.budget.rounds_used <= cap, "escalated": r.stop_code == "budget_rounds" and r.assessment.could_change}


C(id="C3-X2", decision="C3", partition="end-to-end at the cap", boundary="at", input="Researcher(max_rounds=3) on quietforms (needs 3)",
  expected="No 4th round; stops escalated", assertion="rounds_used <= 3, escalated", severity="Critical",
  run=lambda e: _e2e_rounds(e, 3), expect={"rounds_used_le_cap": True, "escalated": True})


# ------------------------------------------------------------------ C4 relevant correction (class Decision 4, soft)
def _funding_hook(env):
    env.run()
    d = env.draft()
    hook = env.store.one("SELECT criterion FROM evidence WHERE id = %s", (d["hook_evidence_id"],))
    return {"hook": hook["criterion"] if hook else None, "mentions_round": "Series B" in d["current_body"]}


C(id="C4-S1", decision="C4", partition="'mention their funding round' + recent round", input="Ledgerly has a Series B announced 30 days ago",
  expected="Applied", assertion="Judge: apply == true (draft hook is the funding round)", severity="Low", kind="soft",
  run=_funding_hook, judge=lambda o: {"label": "apply" if o["hook"] == "funding_stage" and o["mentions_round"] else "skip",
                                      "reason": "hook=%s, mentions round=%s" % (o["hook"], o["mentions_round"])}, expect="apply",
  assumption="This app encodes 'lead with a recent funding round' as a fixed drafting rule (hook priority, <= 6 months), "
             "not as a stored correction; the check is that the rule is applied when the prospect has a recent round.")


def _guidance(env, scenario):
    from sprinto_agent.feedback import accepted_guidance, decide_style_guidance
    from sprinto_agent.util import now_iso
    env.run()

    def add(rule, role="*", seg="*", decided="2026-11-01T00:00:00+00:00"):
        gid = env.store.insert("style_guidance", {"scope_role": role, "scope_segment": seg, "rule": rule, "detail": rule,
                                                  "evidence_json": "[]", "status": "proposed", "created_at": now_iso()})
        decide_style_guidance(env.store, gid, True, "Eval")
        env.store.update("style_guidance", gid, {"decided_at": decided})
    if scenario == "irrelevant":
        add("greeting:Hey", seg="healthtech")
        g = accepted_guidance(env.store, "CTO / VP Engineering", "series_b")
        return {"conflict": False, "applied": [x["rule"] for x in g]}
    add("greeting:Hey", decided="2026-11-01T00:00:00+00:00")
    add("greeting:Hello", decided="2026-11-10T00:00:00+00:00")
    g = accepted_guidance(env.store, "CTO / VP Engineering", "series_b")
    greetings = [x for x in g if x["rule"] == "greeting"]
    chosen = {x["rule"]: x for x in g}["greeting"]
    return {"conflict": len(greetings) > 1, "chosen": chosen["value"], "chosen_detail": chosen["detail"]}


C(id="C4-S2", decision="C4", partition="tone fix for an unrelated prospect's industry",
  input="accepted wording rule scoped to segment 'healthtech'; drafting for a series_b fintech",
  expected="Not applied (irrelevant to this prospect)", assertion="Judge: apply == false", severity="High", kind="soft",
  run=lambda e: _guidance(e, "irrelevant"),
  judge=lambda o: {"label": "skip" if not o["applied"] else "apply", "reason": "applied rules: %s" % o["applied"]}, expect="skip")
C(id="C4-S3", decision="C4", partition="past correction contradicts a more recent one",
  input="greeting 'Hey' accepted 1 Nov; greeting 'Hello' accepted 10 Nov",
  expected="Most recent wins, not both or neither", assertion="Judge: 1) conflict identified 2) more recent chosen 3) choice named",
  severity="Critical", kind="soft", run=lambda e: _guidance(e, "conflict"),
  judge=lambda o: {"label": "pass" if o["conflict"] and o["chosen"] == "Hello" and "Hello" in o["chosen_detail"] else "fail",
                   "reason": "conflict=%s, chosen=%s (%s)" % (o["conflict"], o["chosen"], o["chosen_detail"])}, expect="pass")


# ------------------------------------------------------------------ C5 what counts as a correction (class Decision 5, soft)
def _edit(env, before, after):
    from sprinto_agent.feedback import categorize
    cats = categorize("s", before, "s", after, {})
    return {"before": before, "after": after, "stored_as_correction": cats != ["typo"], "categories": cats}


def edit_judge(o):
    v = judge_edit(o)
    agree = (v["label"] == "meaningful") == o["stored_as_correction"]
    return {"label": v["label"] if agree else "disagree", "reason": "judge: %s; app categories: %s" % (v["reason"], o["categories"])}


C(id="C5-S1", decision="C5", partition="whole opening hook rewritten", input="'I noticed Acme mentions SOC 2 is in progress.' -> 'Saw Acme just opened a London office.'",
  expected="Stored as a correction", assertion="Judge: meaningful == true, stored to feedback memory", severity="Low", kind="soft",
  run=lambda e: _edit(e, "I noticed Acme mentions SOC 2 is in progress.", "Saw Acme just opened a London office."),
  judge=edit_judge, expect="meaningful")
C(id="C5-S2", decision="C5", partition="one typo fixed", input="'Happy to share teh details.' -> 'Happy to share the details.'",
  expected="Not stored", assertion="Judge: meaningful == false", severity="Low", kind="soft",
  run=lambda e: _edit(e, "Happy to share teh details.", "Happy to share the details."), judge=edit_judge, expect="typo")
C(id="C5-S3", decision="C5", partition="punctuation only, but it changes tone", input="'Thanks for getting back to me.' -> 'Thanks for getting back to me!'",
  expected="Judged on intent, not edit size", assertion="Judge: does not default to 'small edit = noise'; flags meaningful",
  severity="High", kind="soft", run=lambda e: _edit(e, "Thanks for getting back to me.", "Thanks for getting back to me!"),
  judge=edit_judge, expect="meaningful")
C(id="C5-X1", decision="C5", partition="greeting change only", input="'Hi Jeff,' -> 'Hey Jeff,'",
  expected="Meaningful (tone)", assertion="app stores it as a correction", severity="Low",
  run=lambda e: _edit(e, "Hi Jeff,", "Hey Jeff,")["stored_as_correction"], expect=True)


# ------------------------------------------------------------------ C6 reply meaning (class Decision 6)
def _reply(env, body, check_hitl=False):
    from sprinto_agent.gmail_sync import ensure_watch, sync
    from sprinto_agent.safety import suppressed_reason
    ensure_watch(env.store, env.gmail, TOPIC)
    env.run()
    d = env.draft()
    review.approve(env.store, d["id"], "Eval", recipient=INBOX)
    review.send(env.store, env.live(), d["id"], gmail=env.gmail)
    s = env.store.query("SELECT * FROM sends")[0]
    env.gmail.deliver(raw_mail("tester@other.example", INBOX, "Re", body, in_reply_to=s["rfc822_message_id"]))
    sync(env.store, env.gmail, env.live())
    s = env.store.query("SELECT * FROM sends")[0]
    out = {"reply_status": s["reply_status"], "suppressed": bool(suppressed_reason(env.store, email=PROSPECT))}
    if check_hitl:
        from sprinto_agent.dashboard import action_queue, company_views
        item = [i for i in action_queue(env.store, company_views(env.store)) if i.label == "Read replies that need you"][0]
        out["routed_to_human"] = item.count == 1
    return out


C(id="C6-S1", decision="C6", partition="interested", input="\"Sounds great, let's talk.\"", expected="Interested",
  assertion='reply_status == "interested" (app category: positive)', severity="Low",
  run=lambda e: _reply(e, "Sounds great, let's talk."), expect={"reply_status": "positive", "suppressed": False})
C(id="C6-S2", decision="C6", partition="not interested + remove us", input="\"Not a fit, please remove us.\"",
  expected="Not interested, suppressed from future outreach", assertion='reply_status == "not_interested" AND added to suppression list',
  severity="Critical", run=lambda e: _reply(e, "Not a fit, please remove us."), expect={"reply_status": "opt_out", "suppressed": True},
  assumption="The app files a removal request as opt_out (a stronger form of not-interested) because that is what triggers suppression.")


def _no_reply(env, hours_after_due):
    from sprinto_agent.gmail_sync import reply_state
    env.run()
    d = env.draft()
    review.approve(env.store, d["id"], "Eval", recipient=INBOX)
    review.send(env.store, env.live(), d["id"], gmail=env.gmail)
    s = env.store.query("SELECT * FROM sends")[0]
    due = datetime.fromisoformat(s["follow_up_due_at"])
    return reply_state(s, due + timedelta(hours=hours_after_due))


C(id="C6-S3", decision="C6", partition="no reply within the window", boundary="above", input="no reply; checked 1 hour after the 3-day window",
  expected="No reply", assertion='reply_status == "no_reply"', severity="Low", run=lambda e: _no_reply(e, 1), expect="no_reply")
C(id="C6-X1", decision="C6", partition="silent, just inside the window", boundary="below", input="no reply; checked 1 hour BEFORE the window ends",
  expected="Still waiting, not yet timed out", assertion='status == "waiting"', severity="High", run=lambda e: _no_reply(e, -1), expect="waiting")
C(id="C6-X2", decision="C6", partition="silent, exactly at the window end", boundary="at", input="no reply; checked exactly at follow_up_due_at",
  expected="Still waiting (the window is inclusive)", assertion='status == "waiting"', severity="High", run=lambda e: _no_reply(e, 0),
  expect="waiting", assumption="The slides don't say whether the window end is inclusive; the app treats it as inclusive.")
C(id="C6-S4", decision="C6", partition="ambiguous reply", input="\"Maybe next quarter, let's see how things go.\"",
  expected="Judged ambiguous, routed to a human, not force-classified", assertion='reply_status == "ambiguous" -> HITL',
  severity="High", run=lambda e: _reply(e, "Maybe next quarter, let's see how things go.", check_hitl=True),
  expect={"reply_status": "ambiguous", "suppressed": False, "routed_to_human": True})
C(id="C6-X3", decision="C6", partition="'not interested' without a removal request", input="\"Not interested, thanks.\"",
  expected="Negative; not suppressed (no removal asked)", assertion="reply_status == negative, suppressed == false", severity="High",
  run=lambda e: _reply(e, "Not interested, thanks."), expect={"reply_status": "negative", "suppressed": False},
  assumption="Only an explicit removal/stop request suppresses; a plain 'not interested' is recorded but not suppressed.")


# ------------------------------------------------------------------ C7 / C8 not built
for i, (inp, exp, sev) in enumerate((("Confirms the first proposed time", 'status == "confirmed"', "Low"),
                                     ("Counter-proposes once", 'status == "counter_proposed", round increments', "Low"),
                                     ("Counter-proposes at the retry cap", 'status == "escalated" at exactly cap', "Critical"),
                                     ("Silent, just inside the reply window", 'status == "waiting"', "High"),
                                     ("Silent, just past the reply window", 'status == "timed_out" at the correct hour', "Critical")), 1):
    C(id="C7-S%d" % i, decision="C7", partition=inp, input=inp, expected=exp, assertion=exp, severity=sev, kind="na",
      run=lambda e: None, assumption="call scheduling (Decision 7) is not built in this app")
for i, (inp, exp, sev) in enumerate((("check_calendar times out once", "retry occurs before escalation", "High"),
                                     ("send_email fails (bad address)", 'status == "skipped", pipeline continues', "High"),
                                     ("book_meeting returns a permanent error", 'status == "aborted", zero retries', "Critical"),
                                     ("update_crm fails after the meeting is booked", 'status == "escalated", human alerted same cycle', "Critical")), 1):
    C(id="C8-S%d" % i, decision="C8", partition=inp, input=inp, expected=exp, assertion=exp, severity=sev, kind="na",
      run=lambda e: None, assumption="meeting booking and CRM updates (Decision 8) are not built in this app")
