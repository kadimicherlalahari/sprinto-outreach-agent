"""D38-D39 plus storage failure handling: report window/counts/denominators/test separation, idempotent snapshots,
redaction, Neon outage and partial writes during a run."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from evals.fixtures import INBOX
from evals.harness import Case
from sprinto_agent import review
from sprinto_agent.util import now_iso, redact, redact_pii

CASES = []


def C(**k):
    CASES.append(Case(**k))


START = date(2026, 10, 1)


def _window(env, created_offset_days, as_of_offset=40):
    from sprinto_agent.report import build_report, configure_pilot
    env.run()
    configure_pilot(env.store, "pilot", "Eval", {"max_bounce_rate": 0.1}, 10, START.isoformat())
    ts = (START + timedelta(days=created_offset_days)).isoformat() + "T12:00:00+00:00"
    env.store.execute("UPDATE companies SET created_at = %s", (ts,))
    rep = build_report(env.store, "pilot", as_of=START + timedelta(days=as_of_offset))
    return rep["stages"][0]["entered"]


for i, (off, inside, part, b) in enumerate(((-1, False, "day before pilot start", "below"), (0, True, "pilot start day", "at"),
                                             (29, True, "last day of the 30-day window", "at"), (30, False, "day 31 (after window)", "above")), 1):
    C(id="D38-win-%d" % i, decision="D38", partition="time window: " + part, boundary=b,
      input="pilot_start=%s, records created on start%+d days" % (START, off), expected="counted" if inside else "excluded",
      assertion="stage 1 entered %s 0" % (">" if inside else "=="), severity="High",
      run=lambda e, o=off: _window(e, o) > 0, expect=inside)


def _kind(env, as_of_offset):
    from sprinto_agent.report import build_report, configure_pilot
    configure_pilot(env.store, "pilot", "Eval", {"max_bounce_rate": 0.1}, 10, START.isoformat())
    return build_report(env.store, "pilot", as_of=START + timedelta(days=as_of_offset))["kind"]


for b, off, kind in (("below", 29, "preliminary"), ("at", 30, "final"), ("above", 31, "final")):
    C(id="D38-final-%s" % b, decision="D38", partition="preliminary vs final", boundary=b, input="as_of = start + %d days" % off,
      expected=kind, assertion="kind == %s" % kind, severity="Low", run=lambda e, o=off: _kind(e, o), expect=kind)


def _counts(env):
    from sprinto_agent.gmail_sync import ensure_watch, sync
    from sprinto_agent.report import build_report, configure_pilot
    from evals.fixtures import TOPIC, raw_mail
    env.run(domains=["ledgerly.example", "carelink.example", "quietforms.example", "drata.example"])
    configure_pilot(env.store, "pilot", "Eval", {"max_bounce_rate": 0.1}, 10, date.today().isoformat())
    ensure_watch(env.store, env.gmail, TOPIC)
    d = env.draft()
    review.edit(env.store, d["id"], "Shorter", d["current_body"].replace("Hi Asha", "Hey Asha"), "Eval")
    review.approve(env.store, d["id"], "Eval", recipient=INBOX)
    review.send(env.store, env.live(), d["id"], gmail=env.gmail)
    s = env.store.query("SELECT * FROM sends")[0]
    env.gmail.deliver(raw_mail("tester@other.example", INBOX, "Re", "Interested!", in_reply_to=s["rfc822_message_id"]))
    sync(env.store, env.gmail, env.live())
    rep = build_report(env.store, "pilot")
    st = {x["stage"][:2]: x for x in rep["stages"]}
    return {"s1": [st["1."]["entered"], st["1."]["advanced"], st["1."]["not_advanced"]],
            "s2": [st["2."]["entered"], st["2."]["advanced"], st["2."]["breakdown"]["qualified"]],
            "s3": [st["3."]["entered"], st["3."]["advanced"]], "s4": [st["4."]["entered"], st["4."]["advanced"]],
            "chain": st["1."]["advanced"] == st["2."]["entered"],
            "rates_ok": all(x["rate"] == (round(x["advanced"] / x["entered"], 4) if x["entered"] else None) for x in rep["stages"]),
            "denominators": all(bool(x["denominator"]) for x in rep["stages"]),
            "test_sent": rep["test_demo"]["send"]["breakdown"]["accepted by Gmail"], "test_replies": rep["test_demo"]["replies"]["advanced"],
            "prospect_sends": rep["prospect"]["send"]["entered"], "prospect_reply_rate": rep["prospect"]["replies"]["rate"],
            "edit_categories": sorted(rep["draft_edits"]["categories"])}


C(id="D38-01", decision="D38", partition="stage counts on a known fixture",
  input="operator domains ledgerly, carelink, quietforms, drata (drata rejected at discovery; carelink rejected in research; "
        "ledgerly qualifies); edit+approve+send; 1 test reply",
  expected="exact entered/advanced/not-advanced per stage; rate = advanced/entered; demo activity separated",
  assertion="see expected dict", severity="Critical", run=_counts,
  expect={"s1": [4, 3, 1], "s2": [3, 3, 1], "s3": [1, 1], "s4": [1, 1], "chain": True, "rates_ok": True, "denominators": True,
          "test_sent": 1, "test_replies": 1, "prospect_sends": 0, "prospect_reply_rate": None,
          "edit_categories": ["subject", "tone"]})


def _snapshot(env):
    from sprinto_agent.report import build_report, save_report
    env.run()
    a = save_report(env.store, build_report(env.store, "pilot"))
    b = save_report(env.store, build_report(env.store, "pilot"))
    return {"same_id": a == b, "rows": env.count("SELECT id FROM pilot_reports")}


C(id="D38-02", decision="D38", partition="report regenerated the same day", input="save_report twice", expected="one snapshot, replaced",
  assertion="rows=1", severity="Low", run=_snapshot, expect={"same_id": True, "rows": 1})


# ------------------------------------------------------------------ D39 redaction
R = [
    ("postgres URL", "connect postgresql://user:pa55word@ep-x.neon.tech/db failed", "pa55word"),
    ("Tavily key", "auth tvly-dev-ABCDEF1234567890abcdef rejected", "tvly-dev-ABCDEF1234567890abcdef"),
    ("Gemini key", "key AIzaSyA1234567890abcdefghijklmnopqrstuv invalid", "AIzaSyA1234567890abcdefghijklmnopqrstuv"),
    ("bearer token", "Authorization: Bearer ya29.a0AfH6SMBxyzTOKENvalue1234567890", "ya29.a0AfH6SMBxyzTOKENvalue1234567890"),
    ("api_key query param", "GET https://api.hunter.io/v2/domain-search?api_key=abcdef0123456789abcdef0123456789", "abcdef0123456789abcdef0123456789"),
    ("email address", "contact operator@inbox.example now", "operator@inbox.example"),
]
for i, (part, text, secret) in enumerate(R, 1):
    C(id="D39-%02d" % i, decision="D39", partition=part, input=text, expected="secret removed", assertion="%r not in redact(text)" % secret[:20],
      severity="Critical" if part != "email address" else "High", run=lambda e, t=text, s=secret: s in redact(t), expect=False)
C(id="D39-07", decision="D39", partition="phone number in a quote", input="'call +91 98765 43210'", expected="removed",
  assertion="digits gone", severity="High", run=lambda e: "98765" in redact_pii("call +91 98765 43210"), expect=False)
C(id="D39-08", decision="D39", partition="date that looks like a number", input="'announced 2026-11-19'", expected="kept",
  assertion="date unchanged", severity="Low", run=lambda e: redact_pii("announced 2026-11-19"), expect="announced 2026-11-19")


def _log_redacted(env):
    r = env.run()
    env.store.log(r["run_id"], "Hunter failed with api_key=abcdef0123456789abcdef0123456789 for postgresql://u:secretpw@h/db")
    msg = env.store.query("SELECT message FROM run_log ORDER BY created_at DESC LIMIT 1")[0]["message"]
    return {"key_hidden": "abcdef0123456789" not in msg, "pw_hidden": "secretpw" not in msg}


C(id="D39-09", decision="D39", partition="secrets written to the run log", input="store.log() with an API key and DB password",
  expected="stored redacted", assertion="key_hidden, pw_hidden", severity="Critical", run=_log_redacted,
  expect={"key_hidden": True, "pw_hidden": True})


# ------------------------------------------------------------------ storage failures during a run (D03 / D14 area)
def _neon_outage_one_company(env):
    real = env.store.insert
    state = {"n": 0}

    def flaky(t, row):
        if t == "evidence" and row.get("source_url", "").startswith("https://quietforms.example"):
            raise sqlite3.OperationalError("connection to Neon lost")
        return real(t, row)
    env.store.insert = flaky
    env.run(domains=["quietforms.example", "ledgerly.example"])
    env.store.insert = real
    q, l = env.company("quietforms.example"), env.company("ledgerly.example")
    return {"quietforms_error": q["error_kind"], "quietforms_retryable": bool(q["retryable"]), "ledgerly": l["qualification"],
            "sends": env.count("SELECT id FROM sends")}


C(id="D03-07", decision="D03", partition="Neon outage while saving one company", input="evidence insert fails for quietforms only",
  expected="that company recorded as a database error, retryable; others continue; nothing sent",
  assertion="quietforms error_kind=database, ledgerly qualified", severity="High", run=_neon_outage_one_company,
  expect={"quietforms_error": "database", "quietforms_retryable": True, "ledgerly": "qualified", "sends": 0})


def _partial_then_retry(env):
    from sprinto_agent.pipeline import retry_company
    real = env.store.insert

    def flaky(t, row):
        if t == "evidence" and row.get("criterion") == "enterprise_selling":
            raise sqlite3.OperationalError("write failed mid-company")
        return real(t, row)
    env.store.insert = flaky
    env.run()
    env.store.insert = real
    partial = env.count("SELECT id FROM evidence")
    co = env.company("ledgerly.example")
    retry_company(env.store, env.settings(), co["id"], env.deps())
    rows = env.store.query("SELECT criterion, value, source_url, quote FROM evidence")
    dup = len(rows) - len({(r["criterion"], r["value"], r["source_url"], r["quote"]) for r in rows})
    return {"partial_saved": partial > 0, "duplicates_after_retry": dup, "decision": env.company("ledgerly.example")["qualification"],
            "drafts": env.count("SELECT id FROM drafts")}


C(id="D03-08", decision="D03", partition="partial write then retry", input="evidence write fails half-way; retry the company",
  expected="partial evidence replaced (no duplicates); company completes", assertion="duplicates_after_retry=0, drafts=1",
  severity="High", run=_partial_then_retry, expect={"partial_saved": True, "duplicates_after_retry": 0, "decision": "qualified", "drafts": 1})
