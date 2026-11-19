"""Mailbox tracking: Gmail push notifications + history sync, watch renewal, recovery sync, reply
matching and classification.

Rules (tested in tests/test_gmail_tracking.py):
  * Only messages that belong to a thread we sent (thread id, or In-Reply-To/References naming our
    Message-ID) are stored. Other mail in the inbox is never read into the database.
  * Messages labelled SENT (the operator's own outgoing mail, including our original) are never counted as
    replies. If one carries our X-Sprinto-Send-Id but is unknown or went to someone other than the operator's
    inbox, an incident is raised and sending pauses.
  * Raw events (gmail_events) are stored apart from their classification (reply_classifications).
  * Every write is idempotent: a duplicate notification or a re-fetched message changes nothing.
  * The history cursor only advances after every message in the batch was stored. A Neon or Gmail failure
    leaves it where it was, so the next sync re-reads the batch (idempotently).
  * Expired history (404) -> recovery sync of every tracked thread, then a fresh cursor.
  * Retrieved email is untrusted data: it can set only a reply category, a bounce/meeting flag, and an opt-out
    suppression for that contact (the one action an opt-out must trigger). It cannot approve, edit, send,
    unsuppress, change caps or kill switches.
  * Own-inbox mode: every event is marked test/demo and excluded from prospect metrics.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Dict, List, Optional

from .gmail import GmailError, HistoryExpired
from .store import Store, is_unique_violation
from .util import get_logger, now_iso, redact, redact_pii

log = get_logger(__name__)

CATEGORIES = ("positive", "negative", "question", "ambiguous", "out_of_office", "opt_out", "bounce", "other")
NEEDS_HUMAN = ("ambiguous", "question", "other")   # replies a person should read before anything else happens
BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery (sub)?system", re.I)
RULES = [
    ("out_of_office", re.compile(r"\b(out of (the )?office|ooo|on (annual |parental )?leave|on vacation|on holiday|"
                                 r"away (from|until)|automatic reply|auto-?reply|limited access to email)\b", re.I)),
    ("opt_out", re.compile(r"\b(unsubscribe|remove (me|us)|take (me|us) off|opt(ed)?[- ]?out|stop (emailing|contacting|sending)|"
                           r"do not (contact|email)|don'?t (contact|email) (me|us)|no more emails)\b", re.I)),
    ("negative", re.compile(r"\b(not interested|no thanks|no thank you|not a (good )?fit|not relevant|no need|"
                            r"not (right )?now|we'?re (all )?set|pass on this|not looking)\b", re.I)),
    ("ambiguous", re.compile(r"\b(maybe|perhaps|possibly|next (quarter|year|month)|later this year|in a few months|"
                             r"not sure|let'?s see|circle back|revisit|down the line|keep (me|us) posted)\b", re.I)),
    ("positive", re.compile(r"\b(interested|let'?s (talk|chat|connect|meet)|sounds (good|great|interesting)|"
                            r"happy to (chat|talk|connect)|book a|schedule a|set up a (call|meeting)|tell me more|"
                            r"keen to|would love to)\b", re.I)),
    ("question", re.compile(r"\?|\b(already (use|have|work with)|how much|pricing|price|cost|budget|how does|"
                            r"what does|why (would|should))\b", re.I)),
]


def new_text(text: str) -> str:
    """Strip quoted history so the classifier only reads what the replier wrote."""
    out = []
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(">"):
            continue
        joined = line + " " + (lines[i + 1] if i + 1 < len(lines) else "")   # Gmail wraps long "On ... wrote:" headers
        if re.match(r"\s*On .{4,200}wrote:\s*$", line) or re.match(r"\s*On .{4,300}wrote:\s*$", joined) \
                or re.match(r"\s*-{2,}\s*Original Message", line, re.I):
            break
        out.append(line)
    return "\n".join(out)[:5000]


def classify_reply(headers: Dict[str, str], text: str) -> str:
    """Deterministic category from a fixed set. Untrusted text can only influence WHICH category."""
    auto = (headers.get("Auto-Submitted") or "").lower()
    body = new_text(text)
    subj = headers.get("Subject") or ""
    if auto.startswith("auto-replied") or auto.startswith("auto-generated"):
        if RULES[1][1].search(body):
            return "opt_out"
        return "out_of_office"
    for cat, rx in RULES:
        if cat == "out_of_office" and rx.search(subj + "\n" + body):
            return cat
        if cat != "out_of_office" and rx.search(body):
            if cat == "positive" and RULES[2][1].search(body):
                return "negative"   # "not interested" contains "interested"
            return cat
    return "other"


# ---------------------------------------------------------------- mailbox state
def mailbox(store: Store) -> Dict[str, Any]:
    row = store.one("SELECT * FROM gmail_mailbox WHERE id = 'primary'")
    if row:
        return row
    try:
        store.insert("gmail_mailbox", {"id": "primary", "updated_at": now_iso(), "watch_status": "not_configured"})
    except Exception as e:
        if not is_unique_violation(e):
            raise
    return store.one("SELECT * FROM gmail_mailbox WHERE id = 'primary'")


def _mb_update(store: Store, **fields: Any) -> None:
    mailbox(store)
    fields["updated_at"] = now_iso()
    store.update("gmail_mailbox", "primary", fields)


def ensure_watch(store: Store, client: Any, topic: Optional[str], now: Optional[datetime] = None,
                 renew_within_hours: float = 24.0) -> str:
    """Start or renew the Gmail watch before it expires (Gmail watches last 7 days). Failure is recorded and
    shown; polling sync keeps working, so no events are lost while the watch is down."""
    now = now or datetime.now(timezone.utc)
    mb = mailbox(store)
    if not topic:
        _mb_update(store, watch_status="not_configured", watch_error="GMAIL_PUBSUB_TOPIC not set: polling only")
        return "not_configured"
    exp = mb.get("watch_expiration")
    if exp and mb.get("watch_status") == "active":
        try:
            exp_dt = datetime.fromtimestamp(int(exp) / 1000, tz=timezone.utc)
            if exp_dt - now > timedelta(hours=renew_within_hours):
                return "active"
        except (TypeError, ValueError):
            pass
    try:
        res = client.watch(topic)
    except GmailError as e:
        _mb_update(store, watch_status="failed", watch_error=redact(str(e))[:300])
        return "failed"
    fields: Dict[str, Any] = {"watch_status": "active", "watch_error": None, "watch_expiration": str(res.get("expiration"))}
    if not mb.get("history_id") and res.get("historyId"):
        fields["history_id"] = str(res["historyId"])
    _mb_update(store, **fields)
    return "active"


def handle_push(store: Store, client: Any, settings: Any, envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Pub/Sub push body -> idempotent notification record -> history sync. The notification's historyId is
    only a hint; the stored cursor decides what is read."""
    msg = (envelope or {}).get("message") or {}
    mid = str(msg.get("messageId") or msg.get("message_id") or "")
    if not mid:
        return {"status": "ignored", "reason": "no message id"}
    try:
        data = json.loads(base64.b64decode(msg.get("data") or "").decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        data = {}
    try:
        nid = store.insert("gmail_notifications", {"pubsub_message_id": mid[:200], "history_id": str(data.get("historyId") or "")[:40],
                                                   "received_at": now_iso(), "status": "received"})
    except Exception as e:
        if is_unique_violation(e):
            return {"status": "duplicate"}
        raise
    res = sync(store, client, settings, trigger="push")
    store.update("gmail_notifications", nid, {"status": "processed" if res["status"] != "failed" else "failed"})
    return res


def sync(store: Store, client: Any, settings: Any, trigger: str = "poll") -> Dict[str, Any]:
    """Incremental history sync with fallback to recovery sync. Never raises for Gmail problems: the run is
    recorded as failed/partial and shown in the UI."""
    run_id = store.insert("gmail_sync_runs", {"trigger": trigger, "started_at": now_iso(), "status": "running",
                                              "messages_seen": 0, "events_added": 0})
    mb = mailbox(store)
    start = mb.get("history_id")
    stats = {"seen": 0, "added": 0, "failed": 0}
    status, err, end = "ok", None, start
    try:
        if not start:
            end = _recovery(store, client, settings, stats)
            status = "ok" if not stats["failed"] else "partial"
        else:
            try:
                h = client.history(start)
                for mid in h["message_ids"]:
                    stats["seen"] += 1
                    try:
                        if process_message(store, settings, client.get_message(mid)):
                            stats["added"] += 1
                    except GmailError as e:
                        if e.kind == "not_found":
                            continue   # message deleted since; nothing to record
                        stats["failed"] += 1
                        err = redact(str(e))[:300]
                end = h["history_id"] if not stats["failed"] else start
                status = "ok" if not stats["failed"] else "partial"
            except HistoryExpired:
                end = _recovery(store, client, settings, stats)
                status = "ok" if not stats["failed"] else "partial"
                err = "history expired; recovered by re-reading tracked threads"
        fields: Dict[str, Any] = {"last_sync_at": now_iso(), "last_sync_status": status, "last_sync_error": err}
        if end and status == "ok":
            fields["history_id"] = str(end)
        _mb_update(store, **fields)
    except GmailError as e:
        status, err = "failed", redact(str(e))[:300]
        try:
            _mb_update(store, last_sync_at=now_iso(), last_sync_status="failed", last_sync_error=err)
        except Exception:
            pass
    try:
        store.update("gmail_sync_runs", run_id, {"finished_at": now_iso(), "status": status, "start_history_id": start,
                                                 "end_history_id": end if status == "ok" else start,
                                                 "messages_seen": stats["seen"], "events_added": stats["added"],
                                                 "error": err})
    except Exception:
        pass
    return {"status": status, "seen": stats["seen"], "added": stats["added"], "error": err}


def _recovery(store: Store, client: Any, settings: Any, stats: Dict[str, int]) -> Optional[str]:
    """Re-read every tracked thread (sends from the last 90 days) and take a fresh cursor from the profile."""
    prof = client.profile()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    threads = store.query("SELECT DISTINCT gmail_thread_id FROM sends WHERE gmail_thread_id IS NOT NULL AND sent_at >= %s",
                          (cutoff,))
    for t in threads:
        try:
            msgs = client.get_thread(t["gmail_thread_id"])
        except GmailError as e:
            if e.kind == "not_found":
                continue
            stats["failed"] += 1
            continue
        for m in msgs:
            stats["seen"] += 1
            if process_message(store, settings, m):
                stats["added"] += 1
    return str(prof.get("historyId") or "") or None


def _match_send(store: Store, m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if m.get("thread_id"):
        row = store.one("SELECT * FROM sends WHERE gmail_thread_id = %s ORDER BY sent_at LIMIT 1", (m["thread_id"],))
        if row:
            return row
    if m.get("id"):
        row = store.one("SELECT * FROM sends WHERE gmail_message_id = %s", (m["id"],))
        if row:
            return row
    h = m.get("headers") or {}
    refs = re.findall(r"<[^<>\s]+>", (h.get("In-Reply-To") or "") + " " + (h.get("References") or ""))
    if m.get("is_delivery_report"):
        refs += re.findall(r"<[^<>\s]+@[^<>\s]+>", m.get("text") or "")
    for r in refs:
        row = store.one("SELECT * FROM sends WHERE rfc822_message_id = %s", (r,))
        if row:
            return row
    return None


def process_message(store: Store, settings: Any, m: Dict[str, Any]) -> bool:
    """Store one mailbox message if it belongs to one of our sends. Returns True if a new event was added."""
    if not m.get("id"):
        return False
    if store.one("SELECT id FROM gmail_events WHERE gmail_message_id = %s", (m["id"],)):
        return False                                    # duplicate notification / re-read: nothing changes
    h = m.get("headers") or {}
    own_send_header = (h.get("X-Sprinto-Send-Id") or "").strip()
    send = _match_send(store, m)
    if own_send_header and "SENT" in (m.get("labels") or []):
        _check_outgoing(store, settings, m, own_send_header)
    if send is None:
        return False                                    # not ours: never stored
    labels = m.get("labels") or []
    from_addr = parseaddr(h.get("From") or "")[1].lower()
    if "SENT" in labels or m["id"] == send.get("gmail_message_id"):
        etype = "own_sent"
    elif m.get("is_delivery_report") or BOUNCE_FROM.search(h.get("From") or ""):
        etype = "bounce"
    elif m.get("has_calendar_invite"):
        etype = "meeting_invite"
    else:
        etype = "reply"
    row = {"gmail_message_id": m["id"], "thread_id": m.get("thread_id"), "history_id": m.get("history_id"),
           "internal_date": m.get("internal_date"), "label_ids": ",".join(labels)[:300], "from_addr": redact_pii(from_addr)[:200],
           "subject": redact_pii(h.get("Subject") or "")[:300], "snippet": redact_pii(new_text(m.get("text") or ""))[:400],
           "headers_json": json.dumps({k: redact_pii(v)[:300] for k, v in h.items()
                                       if k in ("Date", "Auto-Submitted", "Content-Type", "In-Reply-To", "Message-ID")}),
           "event_type": etype, "send_id": send["id"], "draft_id": send["draft_id"],
           "is_test": bool(send.get("is_test", True)), "created_at": now_iso()}
    try:
        eid = store.insert("gmail_events", row)
    except Exception as e:
        if is_unique_violation(e):
            return False
        raise
    if etype == "bounce":
        store.update("sends", send["id"], {"delivery_status": "bounced"})
        _classify(store, eid, "bounce")
    elif etype == "meeting_invite":
        store.update("sends", send["id"], {"meeting_status": "invite_received", "reply_status": "replied",
                                           "replied_at": now_iso()})
        _classify(store, eid, "positive")
    elif etype == "reply":
        cat = classify_reply(h, m.get("text") or "")
        _classify(store, eid, cat)
        store.update("sends", send["id"], {"reply_status": cat, "replied_at": now_iso()})
        if cat == "opt_out":
            _apply_opt_out(store, send)
    return True


def _classify(store: Store, event_id: str, cat: str) -> None:
    assert cat in CATEGORIES
    try:
        store.insert("reply_classifications", {"event_id": event_id, "category": cat, "method": "rules",
                                               "confidence": "rule match" if cat != "other" else "no rule matched",
                                               "created_at": now_iso()})
    except Exception as e:
        if not is_unique_violation(e):
            raise


def _apply_opt_out(store: Store, send: Dict[str, Any]) -> None:
    """Opt-out: suppress the contact the draft was written for, immediately. Applies in test mode too (safe side)."""
    from .safety import add_suppression
    d = store.one("SELECT poc_id FROM drafts WHERE id = %s", (send["draft_id"],)) or {}
    poc = store.one("SELECT email FROM pocs WHERE id = %s", (d.get("poc_id"),)) if d.get("poc_id") else None
    if poc and poc.get("email"):
        add_suppression(store, "email", poc["email"], "opted out by reply (send %s)" % send["id"][:8])


def _check_outgoing(store: Store, settings: Any, m: Dict[str, Any], send_id: str) -> None:
    """A message carrying our header was sent: it must match a recorded send to the operator's inbox."""
    from .safety import raise_incident
    h = m.get("headers") or {}
    row = store.one("SELECT * FROM sends WHERE id = %s", (send_id,))
    to = [a.lower() for a in re.findall(r"[\w.+\-]+@[\w\-.]+", (h.get("To") or "") + " " + (h.get("Cc") or ""))]
    inbox = (settings.operator_inbox or "").lower()
    if row is None:
        raise_incident(store, "unauthorized_send", "Gmail shows a message with send id %s that has no send record" % send_id[:8])
    elif to and to != [inbox]:
        raise_incident(store, "wrong_recipient", "Gmail shows send %s addressed to %d other recipient(s)" % (
            send_id[:8], len([a for a in to if a != inbox])), draft_id=row["draft_id"], send_id=send_id)
    elif row and row["status"] not in ("sent", "sending", "uncertain"):
        raise_incident(store, "unauthorized_send", "Gmail shows send %s but its record says %s" % (send_id[:8], row["status"]),
                       draft_id=row["draft_id"], send_id=send_id)
    if row is not None:
        d = store.one("SELECT poc_id FROM drafts WHERE id = %s", (row["draft_id"],)) or {}
        poc = store.one("SELECT email FROM pocs WHERE id = %s", (d.get("poc_id"),)) if d.get("poc_id") else None
        email = ((poc or {}).get("email") or "").lower()
        sup = store.one("SELECT created_at FROM suppressions WHERE kind = 'email' AND value = %s", (email,)) if email else None
        if sup and str(sup["created_at"]) < str(row["sent_at"]):
            raise_incident(store, "opt_out_ignored", "send %s went out after the contact opted out" % send_id[:8],
                           draft_id=row["draft_id"], send_id=send_id)


def reply_state(send: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """What we know about a reply to one send: a reply category, 'no_reply' once the follow-up window has passed,
    otherwise 'waiting'. Dry runs and unsent rows are 'not_sent'."""
    if send.get("status") != "sent":
        return "not_sent"
    rs = send.get("reply_status") or "not_tracked"
    if rs not in ("not_tracked", ""):
        return rs
    from .util import parse_date
    due = parse_date(send.get("follow_up_due_at") or "")
    now = now or datetime.now(timezone.utc)
    if due is not None and now > due:
        return "no_reply"
    return "waiting"


def status(store: Store) -> Dict[str, Any]:
    mb = mailbox(store)
    last = store.query("SELECT * FROM gmail_sync_runs ORDER BY started_at DESC LIMIT 5")
    return {"mailbox": mb, "recent": last}
