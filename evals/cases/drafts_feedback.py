"""D19-D22, D26, D34-D37: drafting, model wording pass, edit/approve, unsupported claims, feedback, source demotion,
edit categories (incl. soft-judged typo vs meaningful edits), style guidance scope and conflicts."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from evals.fixtures import ICP, INBOX, make_draft_row
from evals.harness import Case
from evals.judge import judge_edit, judge_relevance
from sprinto_agent import review
from sprinto_agent.drafting import Draft, compose, llm_polish
from sprinto_agent.evidence import is_recent
from sprinto_agent.llm import LLMUnavailable
from sprinto_agent.util import now_iso

CASES = []


def C(**k):
    CASES.append(Case(**k))


FIXED_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)   # one clock for fixture and check: exact boundaries


def row(crit, value, tier="1", status="verified", days=None, quote=None, id_=None):
    pub = (FIXED_NOW - timedelta(days=days)).isoformat() if days is not None else None
    return {"id": id_ or crit + value, "criterion": crit, "value": value, "status": status, "source_tier": tier,
            "published_at": pub, "quote": quote or "Acme %s %s." % (crit, value)}


def _compose(env, rows, months=6):
    ok = {r["id"]: is_recent(r["published_at"], months, FIXED_NOW) for r in rows}
    d = compose("Acme", "Pat", "CTO / VP Engineering", rows, ok, ICP, [], sender_name="Op")
    if d is None:
        return {"draft": False}
    return {"draft": True, "hook": d.hook_evidence_id, "iso_cta": "ISO 27001 on the roadmap" in d.body,
            "guess_labelled": "guess" in d.body.lower()}


# ------------------------------------------------------------------ D19 composition
for b, days, hook in (("below", 182, "funding_stageseries_b"), ("at", 183, "funding_stageseries_b"), ("above", 184, "compliance_in_progresssoc2")):
    C(id="D19-fund-%s" % b, decision="D19", partition="funding hook recency (6 months = 183 days)", boundary=b,
      input="Series B announced %d days ago + SOC 2 in progress" % days,
      expected="funding is the hook" if days <= 183 else "stale funding skipped; compliance-in-progress is the hook",
      assertion="hook == %s" % hook, severity="High",
      run=lambda e, d=days: _compose(e, [row("funding_stage", "series_b", days=d), row("compliance_in_progress", "soc2")])["hook"],
      expect=hook)
C(id="D19-01", decision="D19", partition="already SOC 2 certified", input="certified soc2 (tier 1) + enterprise selling",
  expected="certified-aware pain/CTA (asks about ISO 27001, does not imply a SOC 2 gap)", assertion="iso_cta=True",
  severity="High", run=lambda e: _compose(e, [row("certified", "soc2"), row("enterprise_selling", "sso_saml")]),
  expect={"draft": True, "iso_cta": True, "guess_labelled": True})
C(id="D19-02", decision="D19", partition="only inferred / tier-3 facts", input="enterprise_selling inferred, tier 3",
  expected="no hook, no draft", assertion="draft=False", severity="Critical",
  run=lambda e: _compose(e, [row("enterprise_selling", "sso", tier="3", status="inferred")]), expect={"draft": False})
C(id="D19-03", decision="D19", partition="no evidence", input="no evidence rows", expected="no draft", assertion="draft=False",
  severity="Critical", run=lambda e: _compose(e, []), expect={"draft": False})
C(id="D19-04", decision="D19", partition="pain point is a labelled guess", input="compliance in progress",
  expected="pain sentence says it is a guess", assertion="guess_labelled=True", severity="High",
  run=lambda e: _compose(e, [row("compliance_in_progress", "soc2")]), expect={"draft": True, "guess_labelled": True})


def _numbers_traceable(env):
    env.run()
    d = env.draft()
    ev = " ".join(r["quote"] for r in env.store.query("SELECT quote FROM evidence WHERE status = 'verified'"))
    allowed = set(re.findall(r"\d+", ev + " " + ICP.get("value_statement", "")))
    return sorted(set(re.findall(r"\d+", d["current_body"])) - allowed)


C(id="D19-05", decision="D19", partition="end-to-end: no invented numbers", input="mock run on ledgerly.example",
  expected="every number in the draft appears in verified evidence or the value statement", assertion="untraceable numbers == []",
  severity="Critical", run=_numbers_traceable, expect=[])


# ------------------------------------------------------------------ D20 model wording pass
BASE = Draft(subject="Acme and compliance", body="Hi Pat,\n\nI noticed Acme mentions SOC 2 is in progress.\n\nA guess on my side: audits take time.\n\nIs getting audit-ready on your list?\n\nBest,\nOp",
             hook_evidence_id="h", hook_text="x", pain_points=["p"])


class FakeLLM:
    def __init__(self, out=None, exc=None):
        self.out, self.exc = out, exc

    def generate(self, prompt, json_mode=True, max_tokens=2048):
        if self.exc:
            raise self.exc
        return self.out


def _polish(env, out=None, exc=None):
    import copy
    d = llm_polish(FakeLLM(out, exc), copy.deepcopy(BASE))
    return d.composer


P = [
    ("good rewrite", "Hi Pat,\n\nI saw Acme says SOC 2 is in progress.\n\nJust a guess: audits take a while.\n\nIs getting audit-ready on your list?\n\nBest,\nOp", None, "gemini", "Low"),
    ("adds a number", "Hi Pat,\n\nAcme has SOC 2 in progress and 300 customers.\n\nA guess: audits take time.\n\nIs it on your list?", None, "template", "Critical"),
    ("drops the hook fact", "Hi Pat,\n\nA guess: audits take time.\n\nIs getting audit-ready on your list?", None, "template", "High"),
    ("adds an email address", "Hi Pat,\n\nSOC 2 is in progress at Acme.\n\nA guess: write to me at x@y.example.\n\nOk?", None, "template", "Critical"),
    ("removes the 'guess' label", "Hi Pat,\n\nAcme mentions SOC 2 is in progress. Audits take time.\n\nIs it on your list?", None, "template", "High"),
    ("too long", "Hi Pat,\n\nSOC 2 in progress at Acme, a guess. " + "word " * 150, None, "template", "Low"),
    ("model unavailable", None, LLMUnavailable("503", "outage"), "template", "High"),
]
for i, (part, out, exc, comp, sev) in enumerate(P, 1):
    C(id="D20-%02d" % i, decision="D20", partition=part, input=(out or repr(exc))[:140],
      expected="rewrite accepted" if comp == "gemini" else "rewrite rejected; template kept", assertion="composer == %s" % comp,
      severity=sev, run=lambda e, o=out, x=exc: _polish(e, o, x), expect=comp)


# ------------------------------------------------------------------ D21 / D22 edit and approve
def _edit_after_approval(env):
    d = env.approved_draft()
    review.edit(env.store, d["id"], d["current_subject"], d["current_body"] + "\nP.S. thanks", "Eval")
    d2 = env.store.draft(d["id"])
    try:
        review.send(env.store, env.settings(), d["id"])
        blocked = False
    except review.SendBlocked:
        blocked = True
    return {"status": d2["status"], "approved_hash": d2["approved_hash"], "send_blocked": blocked, "sends": env.count("SELECT id FROM sends")}


C(id="D21-01", decision="D21", partition="edit after approval", input="approve v1, edit body, try to send",
  expected="approval cleared, back to review, send blocked", assertion="status=pending_review, approved_hash=None, send_blocked, sends=0",
  severity="Critical", run=_edit_after_approval,
  expect={"status": "pending_review", "approved_hash": None, "send_blocked": True, "sends": 0})


def _edit_err(env, subject=None, body=None, setup=None):
    env.run()
    d = env.draft()
    if setup:
        setup(env, d)
    try:
        r = review.edit(env.store, d["id"], subject if subject is not None else d["current_subject"],
                        body if body is not None else d["current_body"], "Eval")
        return {"ok": True, "version": r["version"]}
    except review.ReviewError:
        return {"ok": False}


C(id="D21-02", decision="D21", partition="subject with newline (header injection)", input="subject='Hi\\nBcc: prospect@x.example'",
  expected="refused", assertion="ok=False", severity="Critical", run=lambda e: _edit_err(e, subject="Hi\nBcc: asha@ledgerly.example"),
  expect={"ok": False})
C(id="D21-03", decision="D21", partition="no actual change", input="save the same subject and body",
  expected="no new version", assertion="version=1", severity="Low", run=lambda e: _edit_err(e), expect={"ok": True, "version": 1})
C(id="D21-04", decision="D21", partition="empty body", input="body=''", expected="refused", assertion="ok=False", severity="High",
  run=lambda e: _edit_err(e, body="   "), expect={"ok": False})
C(id="D21-05", decision="D21", partition="draft already sent", input="draft status=sent, edit", expected="refused", assertion="ok=False",
  severity="Critical", run=lambda e: _edit_err(e, body="x y z", setup=lambda en, d: en.store.update("drafts", d["id"], {"status": "sent"})),
  expect={"ok": False})


def _approve(env, hash_=None, status=None):
    env.run()
    d = env.draft()
    if status:
        env.store.update("drafts", d["id"], {"status": status})
    try:
        r = review.approve(env.store, d["id"], "Eval", expected_hash=hash_, recipient=INBOX)
        return {"ok": True, "recipient": r["approved_recipient"], "hash_bound": r["approved_hash"] == r["current_hash"]}
    except review.ReviewError:
        return {"ok": False}


C(id="D22-01", decision="D22", partition="approve current version", input="approve with matching hash",
  expected="approved for exactly this version and recipient", assertion="recipient=inbox, hash bound", severity="Critical",
  run=lambda e: _approve(e), expect={"ok": True, "recipient": INBOX, "hash_bound": True})
C(id="D22-02", decision="D22", partition="stale screen (version changed)", input="expected_hash='old'",
  expected="refused", assertion="ok=False", severity="Critical", run=lambda e: _approve(e, hash_="old"), expect={"ok": False})
C(id="D22-03", decision="D22", partition="superseded draft", input="status=superseded", expected="refused", assertion="ok=False",
  severity="High", run=lambda e: _approve(e, status="superseded"), expect={"ok": False})


def _original_vs_final(env):
    from sprinto_agent.feedback import draft_change_summary, revisions
    env.run()
    d = env.draft()
    review.edit(env.store, d["id"], "New subject", d["current_body"].replace("Hi Asha", "Hey Asha"), "Eval")
    review.edit(env.store, d["id"], "New subject", env.store.draft(d["id"])["current_body"] + "\nCheers", "Eval")
    revs = revisions(env.store, d["id"])
    s = draft_change_summary(env.store, env.store.draft(d["id"]))
    return {"kinds": [r["kind"] for r in revs], "first_is_original": revs[0]["body"] == d["original_body"],
            "edits": s["edits"], "edited": s["edited"], "has_subject": "subject" in s["categories"]}


C(id="D21-06", decision="D21", partition="original vs final diff retained", input="two edits (subject+greeting, then sign-off)",
  expected="first generated draft kept; each edit a revision; summary compares first vs final",
  assertion="kinds=[generated, edit, edit], first_is_original", severity="High", run=_original_vs_final,
  expect={"kinds": ["generated", "edit", "edit"], "first_is_original": True, "edits": 2, "edited": True, "has_subject": True})


# ------------------------------------------------------------------ D26 unsupported claims
def _claims(env, add, evidence_quote=None):
    from sprinto_agent.feedback import unsupported_claims
    env.run()
    d = env.draft()
    if evidence_quote:
        co = env.company("ledgerly.example")
        env.store.insert("evidence", {"company_id": co["id"], "criterion": "employee_count", "value": "120", "status": "verified",
                                      "quote": evidence_quote, "source_url": "https://ledgerly.example/about", "source_tier": "1",
                                      "retrieved_at": now_iso(), "extractor": "rules"})
    d = dict(d, current_body=d["current_body"].replace("Best,", add + "\n\nBest,"))
    return len(unsupported_claims(env.store, d))


C(id="D26-01", decision="D26", partition="new numeric claim, no evidence", input="adds 'We got 400 startups SOC 2 certified in 2 weeks.'",
  expected="flagged", assertion="1 unsupported claim", severity="Critical",
  run=lambda e: _claims(e, "We got 400 startups SOC 2 certified in 2 weeks."), expect=1)
C(id="D26-02", decision="D26", partition="claim backed by verified evidence", input="adds 'Saw you are 120 people now.' (120 in evidence)",
  expected="not flagged", assertion="0 unsupported claims", severity="High",
  run=lambda e: _claims(e, "Saw you are 120 people now."), expect=0)
C(id="D26-03", decision="D26", partition="plain wording, no claim markers", input="adds 'Hope your week is going well.'",
  expected="not flagged", assertion="0", severity="Low", run=lambda e: _claims(e, "Hope your week is going well."), expect=0)
C(id="D26-04", decision="D26", partition="framework claim not in evidence", input="adds 'You are HIPAA compliant already.'",
  expected="flagged", assertion="1", severity="Critical", run=lambda e: _claims(e, "You are HIPAA compliant already."), expect=1)


# ------------------------------------------------------------------ D34 feedback
def _fb(env, kind, verdict, reason="r", times=1, idem=None):
    from sprinto_agent.feedback import FeedbackError, record_feedback
    env.run()
    co = env.company("ledgerly.example")
    before = (co["qualification"], co["score"], env.count("SELECT id FROM evidence"))
    out = []
    for _ in range(times):
        try:
            out.append(record_feedback(env.store, kind, verdict, "Eval", reason, company_id=co["id"], idem_key=idem)["status"])
        except FeedbackError:
            out.append("refused")
    co2 = env.company("ledgerly.example")
    return {"statuses": out, "rows": env.count("SELECT id FROM feedback WHERE kind = %s", (kind,)),
            "facts_unchanged": before == (co2["qualification"], co2["score"], env.count("SELECT id FROM evidence"))}


C(id="D34-01", decision="D34", partition="valid correction", input="qualification/should_reject, reason='too small'",
  expected="stored as an opinion; facts untouched", assertion="saved, facts_unchanged", severity="Critical",
  run=lambda e: _fb(e, "qualification", "should_reject", "too small"), expect={"statuses": ["saved"], "rows": 1, "facts_unchanged": True})
C(id="D34-02", decision="D34", partition="duplicate submit", input="same form token twice",
  expected="one row", assertion="statuses=[saved, duplicate], rows=1", severity="High",
  run=lambda e: _fb(e, "discovery", "relevant", "", times=2, idem="form-1"), expect={"statuses": ["saved", "duplicate"], "rows": 1})
C(id="D34-03", decision="D34", partition="unknown verdict", input="qualification/'force_qualified'", expected="refused",
  assertion="refused, rows=0", severity="High", run=lambda e: _fb(e, "qualification", "force_qualified"),
  expect={"statuses": ["refused"], "rows": 0})
C(id="D34-04", decision="D34", partition="missing reason", input="research/weak_source, reason=''", expected="refused",
  assertion="refused", severity="Low", run=lambda e: _fb(e, "research", "weak_source", ""), expect={"statuses": ["refused"], "rows": 0})


def _conflicting_corrections(env):
    from sprinto_agent.feedback import record_feedback
    env.run()
    co = env.company("ledgerly.example")
    record_feedback(env.store, "qualification", "should_reject", "Eval", "older view", company_id=co["id"])
    record_feedback(env.store, "qualification", "should_qualify", "Eval", "newer view", company_id=co["id"])
    rows = env.store.query("SELECT verdict FROM feedback WHERE company_id = %s ORDER BY created_at", (co["id"],))
    return {"both_kept": [r["verdict"] for r in rows], "decision": env.company("ledgerly.example")["qualification"]}


C(id="D34-05", decision="D34", partition="newer correction conflicts with older", input="should_reject, then should_qualify",
  expected="both kept in order; the code's decision is unchanged by either", assertion="both rows, decision=qualified",
  severity="High", run=_conflicting_corrections, expect={"both_kept": ["should_reject", "should_qualify"], "decision": "qualified"},
  assumption="There is no rule for 'which reviewer opinion wins'; feedback never changes the decision, so both are kept.")


def _outage(env):
    from pathlib import Path
    from sprinto_agent.feedback import flush_outbox, record_feedback
    env.run()
    co = env.company("ledgerly.example")
    real = env.store.insert
    ob = Path(env.tmp) / "outbox.jsonl"

    def down(t, r):
        if t == "feedback":
            raise sqlite3.OperationalError("neon down")
        return real(t, r)
    env.store.insert = down
    s1 = record_feedback(env.store, "discovery", "relevant", "Eval", "", company_id=co["id"], outbox=ob)["status"]
    env.store.insert = real
    f1 = flush_outbox(env.store, ob)
    ob.write_text(json.dumps(env.store.one("SELECT * FROM feedback")) + "\n")      # replay after a worker restart
    f2 = flush_outbox(env.store, ob)
    return {"first": s1, "flush1": f1["saved"], "flush2_dup": f2["duplicate"], "rows": env.count("SELECT id FROM feedback")}


C(id="D34-06", decision="D34", partition="Neon outage during feedback", input="feedback insert fails, then DB recovers, outbox replayed twice",
  expected="kept locally, saved once, replay is a no-op", assertion="queued_locally, rows=1", severity="High", run=_outage,
  expect={"first": "queued_locally", "flush1": 1, "flush2_dup": 1, "rows": 1})


# ------------------------------------------------------------------ D35 source demotion
def _demote(env, flags, own=False):
    from sprinto_agent.feedback import demoted_domains, record_feedback
    env.run()
    co = env.company("ledgerly.example")
    url = "https://ledgerly.example/press" if own else "https://news.example/a"
    eid = env.store.insert("evidence", {"company_id": co["id"], "criterion": "funding_stage", "value": "series_b", "status": "verified",
                                        "quote": "q", "source_url": url, "source_tier": "1" if own else "2",
                                        "retrieved_at": now_iso(), "extractor": "rules"})
    for i in range(flags):
        record_feedback(env.store, "research", "weak_source", "Eval%d" % i, "thin", company_id=co["id"], evidence_id=eid)
    return demoted_domains(env.store)


for b, n, exp in (("below", 1, []), ("at", 2, ["news.example"]), ("above", 3, ["news.example"])):
    C(id="D35-%s" % b, decision="D35", partition="weak-source flags on a third-party domain", boundary=b, input="%d flag(s)" % n,
      expected="demoted" if exp else "not demoted", assertion="demoted == %s" % exp, severity="Low",
      run=lambda e, n=n: _demote(e, n), expect=exp)
C(id="D35-own", decision="D35", partition="company's own site flagged", input="3 weak-source flags on ledgerly.example/press",
  expected="own site is never demoted", assertion="demoted == []", severity="High", run=lambda e: _demote(e, 3, own=True), expect=[])


# ------------------------------------------------------------------ D36 edit categories (hard) + soft judge
def _cats(env, before, after, parts=None):
    from sprinto_agent.feedback import categorize
    return categorize("s", before, "s", after, parts or {})


BODY = "Hi Pat,\n\nI noticed Acme mentions SOC 2 is in progress.\n\nA guess: audits take time.\n\nIs it on your list?\n\nBest,\nOp"
PARTS = {"hook": "I noticed Acme mentions SOC 2 is in progress.", "pain": "A guess: audits take time.", "cta": "Is it on your list?"}
D36 = [
    ("typo-only", BODY.replace("audits", "audtis"), BODY, ["typo"], "Low"),
    ("greeting change", BODY, BODY.replace("Hi Pat", "Hey Pat"), ["tone"], "Low"),
    ("hook rewritten", BODY, BODY.replace(PARTS["hook"], "Saw Acme is growing fast."), ["hook"], "Low"),
    ("CTA rewritten", BODY, BODY.replace(PARTS["cta"], "Free for a 15-minute call?"), ["cta", "factual_claim"], "Low"),
    ("new factual claim", BODY, BODY.replace("Best,", "We serve 500 SaaS teams.\n\nBest,"), ["factual_claim"], "High"),
]
for i, (part, before, after, exp, sev) in enumerate(D36, 1):
    C(id="D36-%02d" % i, decision="D36", partition=part, input="%r -> %r" % (before[-60:], after[-60:]), expected=str(exp),
      assertion="categorize() == %s" % exp, severity=sev, run=lambda e, a=(before, after): _cats(e, *a, PARTS), expect=exp,
      assumption="A CTA that adds '15-minute' is also a numeric claim marker; both categories are expected." if part == "CTA rewritten" else "")
SOFT_EDITS = [("typo fix", "the next few monhts", "the next few months", "typo"),
              ("tone rewrite", "Happy to share how that would look for your setup if useful.", "Worth a quick call next week?", "meaningful"),
              ("removed hedge", "A guess on my side, deals may wait on SOC 2.", "Deals may wait on SOC 2.", "meaningful")]
for i, (part, a, b, lab) in enumerate(SOFT_EDITS, 1):
    C(id="D36-S%d" % i, decision="D36", partition="soft: " + part, input="%r -> %r" % (a, b), expected="judge labels it %s" % lab,
      assertion="rubric R2 label == %s" % lab, severity="Low", kind="soft", run=lambda e, x=a, y=b: {"before": x, "after": y},
      judge=judge_edit, expect=lab)


def _soft_relevance(env):
    env.run()
    rows = env.store.query("SELECT e.criterion, e.quote, e.source_url FROM evidence e WHERE e.status = 'verified' ORDER BY e.id")
    fails = []
    for r in rows:
        v = judge_relevance({"quote": r["quote"], "company": "Ledgerly", "domain": "ledgerly.example",
                             "source_domain": r["source_url"].split("/")[2].replace("www.", ""), "criterion": r["criterion"]})
        if v["label"] != "pass":
            fails.append({"criterion": r["criterion"], "reason": v["reason"]})
    return {"checked": len(rows), "fails": fails}


C(id="D09-S1", decision="D09", partition="soft: accepted evidence is relevant", input="all verified evidence from the Ledgerly mock run",
  expected="every accepted quote passes rubric R1", assertion="judge: fails == []", severity="High", kind="soft",
  run=_soft_relevance, judge=lambda o: {"label": "pass" if not o["fails"] else "fail",
                                        "reason": "%d of %d failed R1: %s" % (len(o["fails"]), o["checked"], o["fails"][:3])},
  expect="pass")


# ------------------------------------------------------------------ D37 style guidance
def _guidance(env, scenario):
    from sprinto_agent.feedback import accepted_guidance, decide_style_guidance
    env.run()
    st = env.store
    ts = now_iso()

    def add(rule, role="*", seg="*", decided="2026-11-01T00:00:00+00:00"):
        gid = st.insert("style_guidance", {"scope_role": role, "scope_segment": seg, "rule": rule, "detail": rule,
                                           "evidence_json": "[]", "status": "proposed", "created_at": ts})
        decide_style_guidance(st, gid, True, "Eval")
        st.update("style_guidance", gid, {"decided_at": decided})
    if scenario == "relevant":
        add("greeting:Hey", role="CTO / VP Engineering")
        return [g["rule"] + "=" + g.get("value", "") for g in accepted_guidance(st, "CTO / VP Engineering", "series_b")]
    if scenario == "irrelevant":
        add("greeting:Hey", role="Founder / CEO")
        return [g["rule"] for g in accepted_guidance(st, "CTO / VP Engineering", "series_b")]
    if scenario == "conflict":
        add("greeting:Hey", decided="2026-11-01T00:00:00+00:00")
        add("greeting:Hello", decided="2026-11-10T00:00:00+00:00")
        g = accepted_guidance(st, "CTO / VP Engineering", "series_b")
        rules = {x["rule"]: x for x in g}             # compose() keeps the last one per rule
        return rules["greeting"]["value"]
    if scenario == "proposed_only":
        st.insert("style_guidance", {"scope_role": "*", "scope_segment": "*", "rule": "prefer_shorter", "detail": "x",
                                     "evidence_json": "[]", "status": "proposed", "created_at": ts})
        return accepted_guidance(st, "CTO / VP Engineering", "series_b")


C(id="D37-01", decision="D37", partition="relevant past correction (same role)", input="accepted greeting:Hey scoped to CTO role; draft for CTO",
  expected="applied", assertion="['greeting=Hey']", severity="Low", run=lambda e: _guidance(e, "relevant"), expect=["greeting=Hey"])
C(id="D37-02", decision="D37", partition="irrelevant past correction (other role)", input="accepted greeting:Hey scoped to Founder/CEO; draft for CTO",
  expected="not applied", assertion="[]", severity="High", run=lambda e: _guidance(e, "irrelevant"), expect=[])
C(id="D37-03", decision="D37", partition="newer correction conflicts with older", input="greeting:Hey accepted 1 Nov, greeting:Hello accepted 10 Nov",
  expected="newest accepted wins, deterministically", assertion="greeting == Hello", severity="High",
  run=lambda e: _guidance(e, "conflict"), expect="Hello")
C(id="D37-04", decision="D37", partition="proposed but not accepted", input="prefer_shorter proposed only", expected="not used",
  assertion="[]", severity="High", run=lambda e: _guidance(e, "proposed_only"), expect=[])


def _typo_no_guidance(env):
    from sprinto_agent.feedback import propose_style_guidance
    env.run()
    d = env.draft()
    b = d["current_body"]
    review.edit(env.store, d["id"], d["current_subject"], b.replace("compliance", "complaince", 1), "Eval")
    review.edit(env.store, d["id"], d["current_subject"], b, "Eval")
    return propose_style_guidance(env.store, min_edits=1)


C(id="D37-05", decision="D37", partition="typo-only edits", input="two typo-level edits on the same draft",
  expected="no style rule learned", assertion="propose_style_guidance() == []", severity="Low", run=_typo_no_guidance, expect=[])
