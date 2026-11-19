"""D15-D18: POC eligibility, role verification, lookup outcomes and manual fallback, preference re-runs.
Invariant checked throughout: every saved POC's name, title and email come from the provider or the operator."""
from __future__ import annotations

from evals.fixtures import ICP, page
from evals.harness import Case
from sprinto_agent.contacts import Contact, ContactUnavailable, MockHunter, filter_hunter, manual_contact, verify_contact
from sprinto_agent.fetch import html_to_page
from sprinto_agent.mock_data import mock_hunter, mock_sources
from sprinto_agent.search import MockSearch

CASES = []


def C(**k):
    CASES.append(Case(**k))


def H(**k):
    base = {"value": "pat@acme.example", "type": "personal", "confidence": 90, "first_name": "Pat", "last_name": "Example",
            "position": "CTO"}
    base.update(k)
    return base


def _filter(env, entries):
    cs, dropped = filter_hunter(entries, "acme.example", ICP)
    return {"eligible": [c.email for c in cs], "dropped": len(dropped)}


F = [
    ("eligible CTO", [H()], {"eligible": ["pat@acme.example"]}, "Low", ""),
    ("generic mailbox", [H(value="info@acme.example", type="generic", first_name=None, last_name=None, position=None)],
     {"eligible": []}, "High", ""),
    ("missing last name (never guessed)", [H(last_name="")], {"eligible": []}, "Critical", ""),
    ("missing title (never guessed)", [H(position="")], {"eligible": []}, "Critical", ""),
    ("email on another domain", [H(value="pat@gmail.com")], {"eligible": []}, "Critical", ""),
    ("Hunter marked email invalid", [H(verification={"status": "invalid"})], {"eligible": []}, "High", ""),
    ("non-target role", [H(position="Head of Marketing")], {"eligible": []}, "High", ""),
    ("role priority: founder vs CTO", [H(value="f@acme.example", first_name="Fay", position="Co-founder & CEO"), H()],
     {"eligible": ["pat@acme.example", "f@acme.example"]}, "Low", "ICP order: CTO/VP Eng before founder/CEO."),
    ("duplicate candidate returned twice", [H(), H()], {"eligible": ["pat@acme.example", "pat@acme.example"]}, "Low",
     "filter_hunter keeps duplicates; only the first is used and a single POC row is saved (see D17-06)."),
]
for i, (part, entries, expect, sev, ass) in enumerate(F, 1):
    C(id="D15-%02d" % i, decision="D15", partition=part, input=str(entries)[:150], expected=str(expect),
      assertion="eligible emails (in rank order)", severity=sev, run=lambda e, x=entries: _filter(e, x), expect=expect,
      assumption=ass)
for b, conf, ok in (("below", 69, False), ("at", 70, True), ("above", 71, True)):
    C(id="D15-conf-%s" % b, decision="D15", partition="Hunter confidence threshold", boundary=b,
      input="confidence=%d, minimum 70" % conf, expected="eligible" if ok else "excluded", assertion="eligible count",
      severity="High", run=lambda e, c=conf: len(_filter(e, [H(confidence=c)])["eligible"]), expect=1 if ok else 0)


# ------------------------------------------------------------------ D16 verification on own pages
def _verify(env, body, title="CTO"):
    c = Contact("Pat", "Example", title, "pat@acme.example", "hunter", 90, {}, role_label="CTO / VP Engineering")
    pg = html_to_page("https://acme.example/team", 200, page("Team", body))
    out = verify_contact(c, "Acme", "acme.example", [pg], None, MockSearch({}), mock_sources())
    return out.verification_status


C(id="D16-01", decision="D16", partition="team page confirms role", input="team page: 'Pat Example is the CTO of Acme.'",
  expected="verified", assertion="verification_status=verified", severity="Low",
  run=lambda e: _verify(e, "<p>Pat Example is the CTO of Acme.</p>"), expect="verified")
C(id="D16-02", decision="D16", partition="outdated / different role on page", input="team page: 'Pat Example leads Marketing at Acme.' (Hunter says CTO)",
  expected="conflict, so no draft is made for this person", assertion="verification_status=conflict", severity="High",
  run=lambda e: _verify(e, "<p>Pat Example leads Marketing at Acme.</p>"), expect="conflict")
C(id="D16-03", decision="D16", partition="name on page, no role words",
  input="team page: 'Say hello to Pat Example, who joined the Acme team this spring.'",
  expected="name_only (warning on the draft)", assertion="verification_status=name_only", severity="Low",
  run=lambda e: _verify(e, "<p>Say hello to Pat Example, who joined the Acme team this spring.</p>"), expect="name_only")
C(id="D16-04", decision="D16", partition="name absent everywhere", input="team page without the person; search returns nothing",
  expected="stays unverified", assertion="verification_status=unverified", severity="High",
  run=lambda e: _verify(e, "<p>Our team is great.</p>"), expect="unverified")


# ------------------------------------------------------------------ D17 lookup outcomes
class FailingHunter:
    def __init__(self, kind):
        self.kind = kind

    def domain_search(self, domain, limit=10):
        raise ContactUnavailable("provider %s" % self.kind, self.kind)


def _lookup(env, hunter=None, data=None):
    d = env.deps(hunter=data)
    if hunter is not None:
        d.hunter = hunter
    env.run(deps=d)
    co = env.company("ledgerly.example")
    emails = {p["email"] for p in env.store.query("SELECT email FROM pocs")}
    provided = {x.get("value") for x in mock_hunter()["ledgerly.example"]} | {x.get("value") for x in (data or {}).get("ledgerly.example", [])}
    return {"pocs": len(emails), "stop_code": co["stop_code"], "retryable": bool(co["retryable"]),
            "drafts": env.count("SELECT id FROM drafts"), "all_from_provider": emails <= provided}


C(id="D17-01", decision="D17", partition="provider returns eligible person", input="mock Hunter: Asha Mockingbird, CTO, 94",
  expected="one POC saved, draft created", assertion="pocs=1, drafts=1, all_from_provider", severity="High",
  run=lambda e: _lookup(e), expect={"pocs": 1, "drafts": 1, "all_from_provider": True})
C(id="D17-02", decision="D17", partition="no candidate at all", input="Hunter returns []",
  expected="no POC invented; stop_code=no_poc; manual Apollo entry offered", assertion="pocs=0, drafts=0, stop_code=no_poc",
  severity="Critical", run=lambda e: _lookup(e, data={"ledgerly.example": []}),
  expect={"pocs": 0, "drafts": 0, "stop_code": "no_poc"})
C(id="D17-03", decision="D17", partition="only unsuitable candidates", input="Hunter returns marketing + generic + low-confidence only",
  expected="no POC; no_poc", assertion="pocs=0, drafts=0", severity="Critical",
  run=lambda e: _lookup(e, data={"ledgerly.example": [x for x in mock_hunter()["ledgerly.example"] if x.get("position") != "CTO"]}),
  expect={"pocs": 0, "drafts": 0, "stop_code": "no_poc"})
C(id="D17-04", decision="D17", partition="provider quota exhausted", input="Hunter raises ContactUnavailable(quota)",
  expected="retryable; no POC; other work kept", assertion="stop_code=poc_quota, retryable", severity="High",
  run=lambda e: _lookup(e, hunter=FailingHunter("quota")), expect={"pocs": 0, "stop_code": "poc_quota", "retryable": True})
C(id="D17-05", decision="D17", partition="provider outage", input="Hunter raises ContactUnavailable(outage)",
  expected="retryable; no POC", assertion="stop_code=poc_outage, retryable", severity="High",
  run=lambda e: _lookup(e, hunter=FailingHunter("outage")), expect={"pocs": 0, "stop_code": "poc_outage", "retryable": True})
C(id="D17-06", decision="D17", partition="duplicate candidate", input="Hunter returns the CTO entry twice",
  expected="one POC row", assertion="pocs=1", severity="Low",
  run=lambda e: _lookup(e, data={"ledgerly.example": [mock_hunter()["ledgerly.example"][1]] * 2}),
  expect={"pocs": 1, "drafts": 1, "all_from_provider": True})


def _manual(env, first, last, title, email):
    try:
        c = manual_contact(first, last, title, email, "acme.example", ICP, "apollo profile")
        return {"ok": True, "source": c.source, "email": c.email}
    except ValueError:
        return {"ok": False}


for i, (args, ok, part) in enumerate(((("Sam", "Lee", "CTO", "sam@acme.example"), True, "complete manual contact"),
                                       (("Sam", "Lee", "CTO", None), True, "manual contact without email"),
                                       (("Sam", "", "CTO", None), False, "missing last name"),
                                       (("Sam", "Lee", "", None), False, "missing title"),
                                       (("Sam", "Lee", "CTO", "sam@other.example"), False, "email on another domain")), 7):
    C(id="D17-%02d" % i, decision="D17", partition="manual Apollo fallback: " + part, input=str(args),
      expected="accepted as manual_apollo" if ok else "refused (nothing guessed)", assertion="ok=%s" % ok,
      severity="Critical" if not ok else "Low", run=lambda e, a=args: _manual(e, *a), expect={"ok": ok})


# ------------------------------------------------------------------ D18 preference re-run
MORE = {"ledgerly.example": mock_hunter()["ledgerly.example"] + [
    {"value": "lee@ledgerly.example", "type": "personal", "confidence": 93, "first_name": "Lee", "last_name": "Securename",
     "position": "Head of IT", "department": "it", "seniority": "senior"}]}


def _rerun(env, prefs, times=1):
    from sprinto_agent.pipeline import rerun_contact
    d = env.deps(hunter=MORE)
    env.run(deps=d)
    co = env.company("ledgerly.example")
    outs = [rerun_contact(env.store, env.settings(), co["id"], d, "Eval", prefs, "eval")["status"] for _ in range(times)]
    active = env.store.query("SELECT email FROM pocs WHERE status = 'active'")
    every = {p["email"] for p in env.store.query("SELECT email FROM pocs")}
    return {"statuses": outs, "active": [a["email"] for a in active],
            "rejected": env.count("SELECT id FROM pocs WHERE status = 'rejected'"),
            "superseded": env.count("SELECT id FROM drafts WHERE status = 'superseded'"),
            "all_from_provider": every <= {x["value"] for x in MORE["ledgerly.example"]}}


C(id="D18-01", decision="D18", partition="suggested title exists", input="reject Asha (CTO); prefer 'Head of IT'",
  expected="Lee (Head of IT) chosen; Asha rejected; her draft superseded", assertion="active=[lee@], rejected=1, superseded=1",
  severity="High", run=lambda e: _rerun(e, {"preferred_titles": ["Head of IT"]}),
  expect={"statuses": ["found"], "active": ["lee@ledgerly.example"], "rejected": 1, "superseded": 1, "all_from_provider": True})
C(id="D18-02", decision="D18", partition="suggested title not available", input="prefer 'Chief Compliance Officer', dept legal",
  expected="no contact; nobody invented", assertion="statuses=[no_contact], active=[]", severity="Critical",
  run=lambda e: _rerun(e, {"preferred_titles": ["Chief Compliance Officer"], "departments": ["legal"]}),
  expect={"statuses": ["no_contact"], "active": [], "all_from_provider": True})
C(id="D18-03", decision="D18", partition="repeated re-runs", input="re-run twice with no preferences",
  expected="each rejected person is never picked again; no invented contact", assertion="Asha not active after re-runs",
  severity="High", run=lambda e: _rerun(e, {}, times=2),
  expect={"statuses": ["no_contact", "no_contact"], "active": [], "rejected": 1, "all_from_provider": True},
  assumption="With no preferences only ICP roles are eligible; after Asha is rejected nobody else at Ledgerly qualifies "
             "(marketing, low confidence, Head of IT not an ICP role), so both re-runs end with no contact. Derived from the fixture.")
