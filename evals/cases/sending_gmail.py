"""D23-D25, D27-D30: send gate, recipient guard, send outcomes and at-most-once, mailbox event handling, reply
classification, opt-out suppression, sync/recovery/watch/push. Live sends go to a FakeGmail; nothing leaves the process."""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from evals.fixtures import INBOX, PROSPECT, TOPIC, make_draft_row, raw_mail, recipients
from evals.harness import Case
from sprinto_agent import review

CASES = []


def C(**k):
    CASES.append(Case(**k))


def _send(env, settings=None, setup=None, approve=True, clicks=1):
    env.run()
    d = env.draft()
    if approve:
        review.approve(env.store, d["id"], "Eval", recipient=INBOX)
    if setup:
        setup(env, env.store.draft(d["id"]))
    out = []
    for _ in range(clicks):
        try:
            out.append(review.send(env.store, settings or env.live(), d["id"], gmail=env.gmail)["status"])
        except review.SendBlocked as e:
            out.append("blocked")
    return {"results": out, "gmail_messages": env.sent_messages(),
            "only_inbox": all(recipients(r) == [INBOX] for r in env.gmail.sent_raw)}


def _pause(env, d):
    from sprinto_agent.safety import pause_sending
    pause_sending(env.store, "eval", "human", "Eval")


def _campaign(env, d):
    from sprinto_agent.safety import set_campaign_paused
    set_campaign_paused(env.store, "pilot", True, "Eval", "eval")


def _suppress_email(env, d):
    from sprinto_agent.safety import add_suppression
    add_suppression(env.store, "email", PROSPECT, "asked to stop")


def _suppress_domain(env, d):
    from sprinto_agent.safety import add_suppression
    add_suppression(env.store, "domain", "ledgerly.example", "asked to stop")


def _tamper(env, d):
    env.store.update("drafts", d["id"], {"current_body": d["current_body"] + "\nTampered."})


def _uncertain_prior(env, d):
    env.store.insert("sends", {"draft_id": d["id"], "content_hash": d["current_hash"], "recipient": INBOX, "mode": "live",
                               "status": "uncertain", "sent_at": "2026-11-19T00:00:00+00:00", "live_lock": d["id"]})


G = [
    ("all conditions met", {}, None, True, ["sent"], 1, "Critical"),
    ("not approved", {}, None, False, ["blocked"], 0, "Critical"),
    ("global pause", {}, _pause, True, ["blocked"], 0, "Critical"),
    ("campaign paused", {}, _campaign, True, ["blocked"], 0, "Critical"),
    ("contact opted out", {}, _suppress_email, True, ["blocked"], 0, "Critical"),
    ("domain opted out", {}, _suppress_domain, True, ["blocked"], 0, "Critical"),
    ("content changed outside the editor", {}, _tamper, True, ["blocked"], 0, "Critical"),
    ("earlier send uncertain", {}, _uncertain_prior, True, ["blocked"], 0, "Critical"),
    ("inbox changed after approval", {"operator_inbox": "someone.else@inbox.example"}, None, True, ["blocked"], 0, "Critical"),
    ("inbox not a valid address", {"operator_inbox": "not-an-email"}, None, True, ["blocked"], 0, "Critical"),
    ("live requested in mock mode", {"mock": True}, None, True, ["blocked"], 0, "Critical"),
    ("unknown SEND_MODE", {"send_mode": "yolo"}, None, True, ["blocked"], 0, "High"),
]
for i, (part, sk, setup, appr, res, n, sev) in enumerate(G, 1):
    C(id="D23-%02d" % i, decision="D23", partition=part, input="live send via FakeGmail; settings %s; %s" % (sk or "default", part),
      expected="sent once to the inbox" if n else "blocked before anything is transmitted",
      assertion="results=%s, gmail_messages=%d" % (res, n), severity=sev,
      run=lambda e, sk=sk, setup=setup, appr=appr: _send(e, e.live(**sk), setup, appr),
      expect={"results": res, "gmail_messages": n, "only_inbox": True})
def _no_gmail(env):
    env.run()
    d = env.draft()
    review.approve(env.store, d["id"], "Eval", recipient=INBOX)
    return _try(lambda: review.send(env.store, env.live(), d["id"], gmail=None))


C(id="D23-13", decision="D23", partition="Gmail API not connected", input="live send with gmail=None (no OAuth client)",
  expected="blocked", assertion="SendBlocked", severity="High", run=lambda e: _no_gmail(e), expect="blocked")


def _try(fn):
    try:
        fn()
        return "sent"
    except review.SendBlocked:
        return "blocked"


def _dry_twice(env):
    r = _send(env, env.settings(), clicks=2)
    return {"results": r["results"], "gmail_messages": r["gmail_messages"],
            "dry_rows": env.count("SELECT id FROM sends WHERE status = 'dry_run'")}


C(id="D23-14", decision="D23", partition="dry-run, repeated", input="SEND_MODE=dry-run, click send twice",
  expected="one dry-run record; nothing transmitted; second refused", assertion="results=[dry_run, blocked], gmail_messages=0",
  severity="Critical", run=_dry_twice, expect={"results": ["dry_run", "blocked"], "gmail_messages": 0, "dry_rows": 1})


# ------------------------------------------------------------------ D24 recipient guard
def _guard(env, hdrs):
    from sprinto_agent.safety import open_incidents
    m = EmailMessage()
    m["From"] = INBOX
    for k, v in hdrs.items():
        m[k] = v
    m["Subject"] = "x"
    m.set_content("y")
    try:
        review.assert_only_operator(m, INBOX)
        return "allowed"
    except review.SendBlocked:
        return "blocked"


for i, (hdrs, out, part) in enumerate((({"To": INBOX}, "allowed", "only the inbox"),
                                        ({"To": INBOX.upper()}, "allowed", "inbox in upper case"),
                                        ({"To": INBOX, "Bcc": PROSPECT}, "blocked", "prospect in Bcc"),
                                        ({"To": INBOX, "Cc": PROSPECT}, "blocked", "prospect in Cc"),
                                        ({"To": "%s, %s" % (INBOX, PROSPECT)}, "blocked", "two recipients in To"),
                                        ({"To": PROSPECT}, "blocked", "prospect only"),
                                        ({}, "blocked", "no recipient")), 1):
    C(id="D24-%02d" % i, decision="D24", partition=part, input=str(hdrs), expected=out, assertion="assert_only_operator -> %s" % out,
      severity="Critical", run=lambda e, h=hdrs: _guard(e, h), expect=out)


def _wrong_recipient_e2e(env):
    import sprinto_agent.review as R
    from sprinto_agent.safety import open_incidents, sending_paused
    real = R.build_message

    def bad(settings, subject, body, label, send_id=None):
        m = EmailMessage()
        m["From"], m["To"], m["Bcc"], m["Subject"] = INBOX, INBOX, PROSPECT, subject
        m.set_content(body)
        R.assert_only_operator(m, INBOX)
        return m
    R.build_message = bad
    try:
        r = _send(env)
    finally:
        R.build_message = real
    return {"results": r["results"], "gmail_messages": r["gmail_messages"], "paused": bool(sending_paused(env.store)),
            "incident": [i["kind"] for i in open_incidents(env.store)]}


C(id="D24-08", decision="D24", partition="wrong recipient produced by a bug", input="message builder adds Bcc: prospect",
  expected="blocked, incident opened, sending paused", assertion="gmail_messages=0, incident=wrong_recipient, paused",
  severity="Critical", run=_wrong_recipient_e2e,
  expect={"results": ["blocked"], "gmail_messages": 0, "paused": True, "incident": ["wrong_recipient"]})


def _prospect_in_body(env):
    def setup(e, d):
        review.edit(e.store, d["id"], d["current_subject"], d["current_body"] + "\nReply to %s directly." % PROSPECT, "Eval")
        review.approve(e.store, d["id"], "Eval", recipient=INBOX)
    r = _send(env, setup=setup)
    return {"gmail_messages": r["gmail_messages"], "only_inbox": r["only_inbox"]}


C(id="D24-09", decision="D24", partition="prospect address inside the body", input="body mentions asha@ledgerly.example",
  expected="still delivered only to the inbox", assertion="only_inbox=True", severity="Critical", run=_prospect_in_body,
  expect={"gmail_messages": 1, "only_inbox": True})


# ------------------------------------------------------------------ D25 outcomes / at-most-once
def _outcome(env, mode):
    from sprinto_agent.safety import open_incidents, sending_paused
    env.gmail.send_mode = mode
    r = _send(env)
    s = env.store.query("SELECT status, delivery_status, live_lock FROM sends")[0]
    env.gmail.send_mode = "ok"
    again = _try(lambda: review.send(env.store, env.live(), env.draft()["id"], gmail=env.gmail))
    return {"first": r["results"][0], "status": s["status"], "delivery": s["delivery_status"], "locked": s["live_lock"] is not None,
            "retry": again, "gmail_messages": env.sent_messages(), "paused": bool(sending_paused(env.store)),
            "incidents": [i["kind"] for i in open_incidents(env.store)]}


C(id="D25-01", decision="D25", partition="accepted by Gmail", input="FakeGmail send_mode=ok",
  expected="recorded accepted_by_gmail (never 'delivered'); retry refused", assertion="delivery=accepted_by_gmail, retry=blocked, 1 message",
  severity="Critical", run=lambda e: _outcome(e, "ok"),
  expect={"first": "sent", "delivery": "accepted_by_gmail", "retry": "blocked", "gmail_messages": 1, "paused": False})
C(id="D25-02", decision="D25", partition="rejected by Gmail (definite)", input="send_mode=reject (HTTP 400)",
  expected="failed, lock released, retry allowed and sends exactly once", assertion="status=failed, retry=sent, 1 message",
  severity="Critical", run=lambda e: _outcome(e, "reject"),
  expect={"first": "blocked", "status": "failed", "locked": False, "retry": "sent", "gmail_messages": 1})
C(id="D25-03", decision="D25", partition="timeout, outcome unknown", input="send_mode=uncertain",
  expected="uncertain, lock kept, incident + pause, no automatic retry", assertion="status=uncertain, retry=blocked, 0 messages",
  severity="Critical", run=lambda e: _outcome(e, "uncertain"),
  expect={"status": "uncertain", "delivery": "unknown", "locked": True, "retry": "blocked", "gmail_messages": 0,
          "paused": True, "incidents": ["uncertain_send"]})
C(id="D25-04", decision="D25", partition="accepted, then connection dropped", input="send_mode=accept_then_drop",
  expected="uncertain (never re-sent); exactly one message exists", assertion="retry=blocked, gmail_messages=1",
  severity="Critical", run=lambda e: _outcome(e, "accept_then_drop"),
  expect={"status": "uncertain", "retry": "blocked", "gmail_messages": 1, "paused": True})


def _concurrent(env, n):
    env.run()
    d = env.draft()
    review.approve(env.store, d["id"], "Eval", recipient=INBOX)
    res = []

    def click():
        res.append(_try(lambda: review.send(env.store, env.live(), d["id"], gmail=env.gmail)))
    ts = [threading.Thread(target=click) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return {"sent": res.count("sent"), "gmail_messages": env.sent_messages()}


C(id="D25-05", decision="D25", partition="concurrent clicks / workers", input="6 threads send the same approved draft",
  expected="exactly one message", assertion="sent=1, gmail_messages=1", severity="Critical", run=lambda e: _concurrent(e, 6),
  expect={"sent": 1, "gmail_messages": 1})


def _db_fail_then_restart(env):
    from sprinto_agent.store import open_mock_store
    env.run()
    d = env.draft()
    review.approve(env.store, d["id"], "Eval", recipient=INBOX)
    real = env.store.update

    def broken(t, rid, f):
        if t == "sends" and f.get("status") == "sent":
            raise sqlite3.OperationalError("neon down")
        return real(t, rid, f)
    env.store.update = broken
    first = _try(lambda: review.send(env.store, env.live(), d["id"], gmail=env.gmail))
    env.store.update = real
    fresh = open_mock_store("%s/eval.sqlite3" % env.tmp)                     # worker restart
    second = _try(lambda: review.send(fresh, env.live(), d["id"], gmail=env.gmail))
    return {"first": first, "after_restart": second, "gmail_messages": env.sent_messages()}


C(id="D25-06", decision="D25", partition="Neon write fails after Gmail accepted, then restart", input="update(sends, sent) raises; new process retries",
  expected="send stays locked; restart cannot send again", assertion="gmail_messages=1", severity="Critical",
  run=_db_fail_then_restart, expect={"first": "blocked", "after_restart": "blocked", "gmail_messages": 1})


def _resolve_flow(env, outcome):
    from sprinto_agent.safety import open_incidents, resolve_incident, resume_sending
    env.gmail.send_mode = "uncertain"
    _send(env)
    env.gmail.send_mode = "ok"
    sid = env.store.query("SELECT id FROM sends")[0]["id"]
    review.resolve_uncertain(env.store, sid, outcome, "Eval")
    for i in open_incidents(env.store):
        resolve_incident(env.store, i["id"], "Eval", "checked Sent folder")
    resume_sending(env.store, "checked", "Eval")
    again = _try(lambda: review.send(env.store, env.live(), env.draft()["id"], gmail=env.gmail))
    return {"retry": again, "gmail_messages": env.sent_messages()}


C(id="D25-07", decision="D25", partition="uncertain resolved as not sent", input="operator: not sent, resolves incident, resumes",
  expected="exactly one retry allowed", assertion="retry=sent, gmail_messages=1", severity="Critical",
  run=lambda e: _resolve_flow(e, "not_sent"), expect={"retry": "sent", "gmail_messages": 1})
C(id="D25-08", decision="D25", partition="uncertain resolved as sent", input="operator: it was sent, resumes",
  expected="never sent again", assertion="retry=blocked, gmail_messages=0", severity="Critical",
  run=lambda e: _resolve_flow(e, "sent"), expect={"retry": "blocked", "gmail_messages": 0})


# ------------------------------------------------------------------ D27 mailbox events
def _sent_setup(env, n_sends=1):
    from sprinto_agent.gmail_sync import ensure_watch
    ensure_watch(env.store, env.gmail, TOPIC)
    env.run()
    d = env.draft()
    review.approve(env.store, d["id"], "Eval", recipient=INBOX)
    review.send(env.store, env.live(), d["id"], gmail=env.gmail)
    sends = [env.store.query("SELECT * FROM sends")[0]]
    if n_sends == 2:
        d2 = make_draft_row(env.store, d["company_id"], d["poc_id"], d["run_id"], "second", "second body")
        review.approve(env.store, d2, "Eval", recipient=INBOX)
        review.send(env.store, env.live(), d2, gmail=env.gmail)
        sends = env.store.query("SELECT * FROM sends ORDER BY sent_at")
    return d, sends


def _events(env, deliveries, n_sends=1):
    from sprinto_agent.gmail_sync import sync
    d, sends = _sent_setup(env, n_sends)
    for kind, kw in deliveries(sends):
        env.gmail.deliver(**kw)
    sync(env.store, env.gmail, env.live())
    ev = env.store.query("SELECT e.event_type, e.send_id, c.category FROM gmail_events e LEFT JOIN reply_classifications c "
                         "ON c.event_id = e.id WHERE e.event_type <> 'own_sent' ORDER BY e.created_at")
    return {"events": [(x["event_type"], x["category"], [s["id"] for s in sends].index(x["send_id"])) for x in ev],
            "own_sent": env.count("SELECT id FROM gmail_events WHERE event_type = 'own_sent'"),
            "total_stored": env.count("SELECT id FROM gmail_events")}


def M(frm, body, ref=None, labels=None, **k):
    return ("x", {"raw": raw_mail(frm, INBOX, "Re: x", body, in_reply_to=ref, **k), **({"labels": labels} if labels else {})})


C(id="D27-01", decision="D27", partition="prospect-side reply in the thread", input="reply from tester@other.example with In-Reply-To our Message-ID",
  expected="stored as reply, matched to the send", assertion="events=[(reply, positive, send 0)]", severity="High",
  run=lambda e: _events(e, lambda s: [M("tester@other.example", "Interested, let's talk.", s[0]["rfc822_message_id"])]),
  expect={"events": [["reply", "positive", 0]]})
C(id="D27-02", decision="D27", partition="operator's own message in the thread", input="message from the inbox, label SENT, in thread",
  expected="own_sent, never a reply", assertion="no reply events", severity="Critical",
  run=lambda e: _events(e, lambda s: [M(INBOX, "note to self: interested", s[0]["rfc822_message_id"], labels=["SENT", "INBOX"])]),
  expect={"events": [], "own_sent": 2})
C(id="D27-03", decision="D27", partition="unrelated mail", input="mail with no thread/reference to our sends",
  expected="not stored at all", assertion="total_stored = 1 (only our own copy)", severity="High",
  run=lambda e: _events(e, lambda s: [M("friend@other.example", "lunch?")]), expect={"events": [], "total_stored": 1})
C(id="D27-04", decision="D27", partition="two sends: reply matched to the right thread", input="2 sends; reply references send #2's Message-ID",
  expected="matched to send #2 only", assertion="events=[(reply, question, 1)]", severity="Critical",
  run=lambda e: _events(e, lambda s: [M("tester@other.example", "How much does it cost?", s[1]["rfc822_message_id"])], n_sends=2),
  expect={"events": [["reply", "question", 1]]})
C(id="D27-05", decision="D27", partition="bounce", input="mailer-daemon DSN naming our Message-ID",
  expected="bounce event, delivery_status=bounced", assertion="events=[(bounce, bounce, 0)]", severity="High",
  run=lambda e: _events(e, lambda s: [("x", {"raw": raw_mail("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", INBOX,
                                                             "Delivery Status Notification (Failure)",
                                                             "Message %s not delivered" % s[0]["rfc822_message_id"]),
                                             "thread_id": s[0]["gmail_thread_id"]})]),
  expect={"events": [["bounce", "bounce", 0]]})
C(id="D27-06", decision="D27", partition="calendar invitation", input="reply with text/calendar METHOD:REQUEST",
  expected="meeting_invite (the only meeting signal used)", assertion="events=[(meeting_invite, positive, 0)]", severity="High",
  run=lambda e: _events(e, lambda s: [M("tester@other.example", "invite attached", s[0]["rfc822_message_id"],
                                         calendar="BEGIN:VCALENDAR\nMETHOD:REQUEST\nEND:VCALENDAR")]),
  expect={"events": [["meeting_invite", "positive", 0]]})


# ------------------------------------------------------------------ D28 classification
def _cls(env, body, hdrs=None):
    from sprinto_agent.gmail_sync import classify_reply
    return classify_reply(hdrs or {}, body)


for i, (body, hdrs, cat, part) in enumerate((
        ("Interested, let's chat next week.", None, "positive", "positive"),
        ("Not interested, thanks.", None, "negative", "'not interested' (contains 'interested')"),
        ("Sorry for not looking into this right now.", None, "negative", "soft decline (live Gmail test text)"),
        ("How much does Sprinto cost?", None, "question", "question"),
        ("We already use Vanta.", None, "question", "objection"),
        ("I am out of the office until Monday.", None, "out_of_office", "out of office"),
        ("Back soon", {"Auto-Submitted": "auto-replied"}, "out_of_office", "auto-reply header"),
        ("Please unsubscribe me.", None, "opt_out", "opt-out"),
        ("Remove me from your list, not interested.", None, "opt_out", "opt-out wins over negative"),
        ("ok", None, "other", "no signal"),
        ("Thanks\n\nOn Mon, 1 Jan 2026 at 10:00, A <\na@b.example> wrote:\n> are you interested? unsubscribe", None, "other",
         "signals only in quoted text (wrapped header)"),
        ("", None, "other", "empty body")), 1):
    C(id="D28-%02d" % i, decision="D28", partition=part, input=repr(body)[:120] + (" headers=%s" % hdrs if hdrs else ""),
      expected=cat, assertion="classify_reply == %s" % cat, severity="Critical" if cat == "opt_out" else "High",
      run=lambda e, b=body, h=hdrs: _cls(e, b, h), expect=cat)


# ------------------------------------------------------------------ D29 opt-out
def _optout(env, body):
    from sprinto_agent.gmail_sync import sync
    from sprinto_agent.safety import suppressed_reason
    d, sends = _sent_setup(env)
    env.gmail.deliver(raw_mail("tester@other.example", INBOX, "Re", body, in_reply_to=sends[0]["rfc822_message_id"]))
    sync(env.store, env.gmail, env.live())
    d2 = make_draft_row(env.store, d["company_id"], d["poc_id"], d["run_id"], "follow up", "follow up body")
    review.approve(env.store, d2, "Eval", recipient=INBOX)
    nxt = _try(lambda: review.send(env.store, env.live(), d2, gmail=env.gmail))
    return {"suppressed": bool(suppressed_reason(env.store, email=PROSPECT)), "next_send": nxt, "gmail_messages": env.sent_messages()}


C(id="D29-01", decision="D29", partition="explicit opt-out reply", input="reply 'Please stop emailing me.'",
  expected="contact suppressed at once; queued follow-up blocked", assertion="suppressed, next_send=blocked, 1 message total",
  severity="Critical", run=lambda e: _optout(e, "Please stop emailing me."),
  expect={"suppressed": True, "next_send": "blocked", "gmail_messages": 1})
C(id="D29-02", decision="D29", partition="'unsubscribe' only in quoted text", input="'Thanks!' + quoted '> unsubscribe'",
  expected="not an opt-out; follow-up allowed", assertion="suppressed=False, next_send=sent", severity="High",
  run=lambda e: _optout(e, "Thanks!\n\n> unsubscribe"), expect={"suppressed": False, "next_send": "sent", "gmail_messages": 2})


# ------------------------------------------------------------------ D30 sync / push / watch
def _push(env, times, mid="pubsub-1"):
    from sprinto_agent.gmail_sync import handle_push
    d, sends = _sent_setup(env)
    env.gmail.deliver(raw_mail("tester@other.example", INBOX, "Re", "Interested", in_reply_to=sends[0]["rfc822_message_id"]))
    env_ = {"message": {"messageId": mid, "data": base64.b64encode(json.dumps({"historyId": env.gmail.hid}).encode()).decode()}}
    out = [handle_push(env.store, env.gmail, env.live(), env_)["status"] for _ in range(times)]
    return {"statuses": out, "replies": env.count("SELECT id FROM gmail_events WHERE event_type = 'reply'"),
            "notifications": env.count("SELECT id FROM gmail_notifications")}


C(id="D30-01", decision="D30", partition="duplicate push notification", input="same Pub/Sub messageId delivered 3 times",
  expected="processed once", assertion="statuses=[ok, duplicate, duplicate], replies=1", severity="High",
  run=lambda e: _push(e, 3), expect={"statuses": ["ok", "duplicate", "duplicate"], "replies": 1, "notifications": 1})


def _missed(env, expired=False):
    from sprinto_agent.gmail_sync import mailbox, sync
    d, sends = _sent_setup(env)
    env.gmail.deliver(raw_mail("tester@other.example", INBOX, "Re", "Not interested", in_reply_to=sends[0]["rfc822_message_id"]))
    env.gmail.history_expired = expired
    r = sync(env.store, env.gmail, env.live(), trigger="poll")
    r2 = sync(env.store, env.gmail, env.live(), trigger="poll")
    return {"status": r["status"], "replies": env.count("SELECT id FROM gmail_events WHERE event_type = 'reply'"),
            "cursor_is_latest": mailbox(env.store)["history_id"] == str(env.gmail.hid)}


C(id="D30-02", decision="D30", partition="missed notification", input="reply arrives, no push; poll sync twice",
  expected="recovered by history sync, once", assertion="replies=1", severity="High", run=lambda e: _missed(e),
  expect={"status": "ok", "replies": 1, "cursor_is_latest": True})
C(id="D30-03", decision="D30", partition="expired history id", input="history.list -> 404",
  expected="recovery sync of tracked threads, fresh cursor", assertion="replies=1, cursor_is_latest", severity="High",
  run=lambda e: _missed(e, expired=True), expect={"status": "ok", "replies": 1, "cursor_is_latest": True})


def _watch(env, hours_left, fail=False):
    from sprinto_agent.gmail_sync import ensure_watch, mailbox
    ensure_watch(env.store, env.gmail, TOPIC)
    calls0 = env.gmail.watch_calls
    exp = int((datetime.now(timezone.utc) + timedelta(hours=hours_left)).timestamp() * 1000)
    env.store.update("gmail_mailbox", "primary", {"watch_expiration": str(exp)})
    env.gmail.watch_fail = fail
    st = ensure_watch(env.store, env.gmail, TOPIC)
    return {"renewed": env.gmail.watch_calls > calls0, "status": st, "visible": mailbox(env.store)["watch_status"]}


for b, h, ren in (("below", 23.9, True), ("above", 24.1, False)):
    C(id="D30-watch-%s" % b, decision="D30", partition="watch renewal window (24h before expiry)", boundary=b,
      input="watch expires in %.1f h" % h, expected="renewed" if ren else "left alone", assertion="renewed=%s" % ren,
      severity="High", run=lambda e, h=h: _watch(e, h), expect={"renewed": ren, "status": "active"})
C(id="D30-watch-fail", decision="D30", partition="watch renewal fails", input="watch() raises HTTP 403 when renewing",
  expected="failure recorded and visible; polling continues", assertion="status=failed, visible=failed", severity="High",
  run=lambda e: _watch(e, 2, fail=True), expect={"renewed": True, "status": "failed", "visible": "failed"})


def _outage(env, which):
    from sprinto_agent.gmail_sync import mailbox, sync
    d, sends = _sent_setup(env)
    cur = mailbox(env.store)["history_id"]
    env.gmail.deliver(raw_mail("tester@other.example", INBOX, "Re", "Pricing?", in_reply_to=sends[0]["rfc822_message_id"]))
    if which == "gmail":
        env.gmail.outage = True
        r = sync(env.store, env.gmail, env.live())["status"]
        env.gmail.outage = False
    else:
        real = env.store.insert

        def down(t, row):
            if t == "gmail_events":
                raise sqlite3.OperationalError("neon down")
            return real(t, row)
        env.store.insert = down
        try:
            r = sync(env.store, env.gmail, env.live())["status"]
        except sqlite3.OperationalError:
            r = "raised"
        env.store.insert = real
    held = mailbox(env.store)["history_id"] == cur
    sync(env.store, env.gmail, env.live())
    return {"during": r, "cursor_held": held, "replies_after": env.count("SELECT id FROM gmail_events WHERE event_type = 'reply'")}


C(id="D30-04", decision="D30", partition="Gmail outage during sync", input="FakeGmail.outage=True, then recovers",
  expected="sync failed (visible), cursor not advanced, event captured later exactly once", assertion="cursor_held, replies_after=1",
  severity="High", run=lambda e: _outage(e, "gmail"), expect={"during": "failed", "cursor_held": True, "replies_after": 1})
C(id="D30-05", decision="D30", partition="Neon outage during sync", input="gmail_events insert raises, then recovers",
  expected="cursor not advanced, event captured later exactly once", assertion="cursor_held, replies_after=1", severity="High",
  run=lambda e: _outage(e, "neon"), expect={"cursor_held": True, "replies_after": 1})


def _push_auth(env, token):
    from fastapi.testclient import TestClient
    from sprinto_agent.web import create_app
    s = env.live(gmail_push_token="s3cret", mock=False)
    c = TestClient(create_app(mock=False, store=env.store, settings=s, gmail_factory=lambda: env.gmail))
    r = c.post("/gmail/push?token=%s" % token, json={"message": {"messageId": "m1", "data": ""}})
    return r.status_code


C(id="D30-06", decision="D30", partition="push endpoint without the shared token", input="POST /gmail/push?token=wrong",
  expected="403, nothing processed", assertion="status_code=403", severity="High", run=lambda e: _push_auth(e, "wrong"), expect=403)
C(id="D30-07", decision="D30", partition="push endpoint with the shared token", input="POST /gmail/push?token=s3cret",
  expected="accepted", assertion="status_code=200", severity="Low", run=lambda e: _push_auth(e, "s3cret"), expect=200)
