"""Stage 3b: human review, approved sending, and history.

Invariants enforced here (and tested):
  * Only status 'approved' drafts whose CURRENT content hash equals the APPROVED hash can be sent.
  * Any edit creates a new version, clears approval, and returns the draft to pending_review.
  * Rejected / unreviewed / modified-after-approval drafts are never sent.
  * The only recipient is OPERATOR_INBOX. No Cc/Bcc. The prospect address is never used.
  * SEND_MODE defaults to dry-run; live sending needs SEND_MODE=live + SMTP credentials + inbox.
  * A draft is sent at most once.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from .config import EMAIL_RE, Settings
from .store import Store
from .util import content_hash, get_logger, now_iso

log = get_logger(__name__)


class ReviewError(Exception):
    pass


class SendBlocked(Exception):
    pass


def _draft_or_fail(store: Store, draft_id: str) -> Dict[str, Any]:
    d = store.draft(draft_id)
    if not d:
        raise ReviewError("draft not found")
    return d


def _record(store: Store, d: Dict[str, Any], decision: str, reviewer: str, subject: str, body: str,
            version: int, note: Optional[str]) -> str:
    return store.insert("review_decisions", {
        "draft_id": d["id"], "run_id": d["run_id"], "decision": decision, "reviewer": reviewer or "operator",
        "version": version, "subject": subject, "body": body, "content_hash": content_hash(subject, body),
        "note": note, "decided_at": now_iso()})


def edit(store: Store, draft_id: str, subject: str, body: str, reviewer: str, note: Optional[str] = None) -> Dict[str, Any]:
    d = _draft_or_fail(store, draft_id)
    if d["status"] in ("sent", "superseded"):
        raise ReviewError("draft is %s; it cannot be edited" % d["status"])
    if any(x["status"] in ("sending", "uncertain") for x in store.sends_for(draft_id)):
        raise ReviewError("a send of this draft has an uncertain outcome; resolve it before editing")
    subject, body = subject.strip(), body.replace("\r\n", "\n").strip()
    if not subject or not body:
        raise ReviewError("subject and body are required")
    if "\n" in subject or "\r" in subject:
        raise ReviewError("subject must be a single line")  # blocks header injection (e.g. a smuggled Bcc:)
    h = content_hash(subject, body)
    if h == d["current_hash"]:
        return d
    v = int(d["version"]) + 1
    from .feedback import record_feedback, record_revision
    record_revision(store, d, "edit", subject, body, reviewer, note)
    _record(store, d, "edit", reviewer, subject, body, v, note)
    record_feedback(store, "draft", "edit", reviewer, note or "", company_id=d["company_id"], run_id=d["run_id"],
                    poc_id=d["poc_id"], draft_id=draft_id, details={"version": v}, idem_key="draft-edit-%s-%s" % (draft_id, h))
    store.update("drafts", draft_id, {"current_subject": subject, "current_body": body, "current_hash": h,
                                      "version": v, "status": "pending_review", "approved_hash": None,
                                      "approved_recipient": None, "updated_at": now_iso()})
    return _draft_or_fail(store, draft_id)


def approve(store: Store, draft_id: str, reviewer: str, expected_hash: Optional[str] = None,
            note: Optional[str] = None, recipient: Optional[str] = None) -> Dict[str, Any]:
    """Approve the exact current version for one recipient (the operator's inbox at approval time).
    If `expected_hash` is given (the UI always sends the hash of the version on screen), approval fails if the
    draft changed in the meantime. Changing OPERATOR_INBOX later requires approving again."""
    d = _draft_or_fail(store, draft_id)
    if d["status"] in ("sent", "superseded"):
        raise ReviewError("draft is %s and cannot be approved" % d["status"])
    if expected_hash and expected_hash != d["current_hash"]:
        raise ReviewError("draft changed since you opened it; review the new version")
    _record(store, d, "approve", reviewer, d["current_subject"], d["current_body"], int(d["version"]),
            note or ("recipient: your inbox" if recipient else None))
    from .feedback import record_feedback, record_revision
    record_revision(store, d, "approved", d["current_subject"], d["current_body"], reviewer, note)
    record_feedback(store, "draft", "approve", reviewer, note or "", company_id=d["company_id"], run_id=d["run_id"],
                    poc_id=d["poc_id"], draft_id=draft_id, details={"version": int(d["version"])},
                    idem_key="draft-approve-%s-%s" % (draft_id, d["current_hash"]))
    store.update("drafts", draft_id, {"status": "approved", "approved_hash": d["current_hash"],
                                      "approved_recipient": (recipient or "").strip().lower() or None,
                                      "updated_at": now_iso()})
    return _draft_or_fail(store, draft_id)


def reject(store: Store, draft_id: str, reviewer: str, note: Optional[str] = None) -> Dict[str, Any]:
    d = _draft_or_fail(store, draft_id)
    if d["status"] == "sent":
        raise ReviewError("draft already sent")
    if any(s["status"] in ("sending", "uncertain") for s in store.sends_for(draft_id)):
        raise ReviewError("a send of this draft has an uncertain outcome; resolve it first")
    _record(store, d, "reject", reviewer, d["current_subject"], d["current_body"], int(d["version"]), note)
    from .feedback import record_feedback
    record_feedback(store, "draft", "reject", reviewer, note or "", company_id=d["company_id"], run_id=d["run_id"],
                    poc_id=d["poc_id"], draft_id=draft_id, details={"version": int(d["version"])},
                    idem_key="draft-reject-%s-%s" % (draft_id, d["current_hash"]))
    store.update("drafts", draft_id, {"status": "rejected", "approved_hash": None, "approved_recipient": None,
                                      "updated_at": now_iso()})
    return _draft_or_fail(store, draft_id)


# ---------------- sending ----------------
# Own-inbox mode is the only mode this project authorises: every send is a test/demo and is excluded from
# prospect engagement metrics. There is deliberately no switch that sends to a prospect.
OWN_INBOX_ONLY = True


def build_message(settings: Settings, subject: str, body: str, prospect_label: str,
                  send_id: Optional[str] = None) -> EmailMessage:
    from email.utils import make_msgid
    inbox = settings.operator_inbox
    if not inbox or not EMAIL_RE.match(inbox):
        raise SendBlocked("OPERATOR_INBOX is not set to a valid address")
    msg = EmailMessage()
    msg["From"] = (settings.smtp_user if settings.send_transport == "smtp" else None) or inbox
    msg["To"] = inbox
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=inbox.split("@", 1)[1])
    msg["X-Sprinto-Agent-Prospect"] = prospect_label[:120]
    msg["X-Sprinto-Mode"] = "own-inbox test"
    if send_id:
        msg["X-Sprinto-Send-Id"] = send_id
    msg.set_content(body)
    assert_only_operator(msg, inbox)
    return msg


def assert_only_operator(msg: EmailMessage, inbox: str) -> None:
    """Hard guard: the message must have exactly one recipient, the operator's inbox."""
    from email.utils import getaddresses
    rcpts = getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Bcc", []))
    addrs = [a.lower() for _, a in rcpts if a]
    if addrs != [inbox.lower()]:
        raise SendBlocked("recipient guard: message must go only to OPERATOR_INBOX (got %d recipients)" % len(addrs))


UNRESOLVED = ("sending", "uncertain")


def _campaign_of(store: Store, d: Dict[str, Any]) -> str:
    r = store.one("SELECT campaign_id FROM research_runs WHERE id = %s", (d["run_id"],)) or {}
    return r.get("campaign_id") or "pilot"


def gate_controls(store: Store, d: Dict[str, Any], prospect: Optional[Dict[str, Any]]) -> None:
    """Kill switches, re-checked at the final gate immediately before transmission."""
    from .safety import campaign_block, sending_paused, suppressed_reason
    paused = sending_paused(store)
    if paused:
        raise SendBlocked(paused)
    cb = campaign_block(store, _campaign_of(store, d))
    if cb:
        raise SendBlocked(cb)
    if prospect:
        sup = suppressed_reason(store, domain=prospect.get("domain"), email=prospect.get("email"))
        if sup:
            raise SendBlocked("prospect opted out: " + sup)


def send_check(settings: Settings, d: Dict[str, Any], prior_sends: List[Dict[str, Any]],
               store: Optional[Store] = None, prospect: Optional[Dict[str, Any]] = None,
               gmail: Any = None) -> None:
    """Every condition that must hold before anything is sent. Raises SendBlocked with the specific reason."""
    if store is not None:
        gate_controls(store, d, prospect)
    if any(s["status"] in UNRESOLVED for s in prior_sends):
        raise SendBlocked("an earlier send of this draft has an uncertain outcome; check your Gmail Sent folder "
                          "and resolve it before sending again")
    if d["status"] == "sent" or any(s["status"] == "sent" for s in prior_sends):
        raise SendBlocked("draft was already sent; it is sent at most once")
    if d["status"] != "approved":
        raise SendBlocked("draft status is '%s'; only approved drafts can be sent" % d["status"])
    if not d.get("approved_hash") or d["approved_hash"] != d["current_hash"] or \
            content_hash(d["current_subject"], d["current_body"]) != d["approved_hash"]:
        raise SendBlocked("draft content differs from the approved version; re-approve it")
    inbox = (settings.operator_inbox or "").strip().lower()
    if not inbox or not EMAIL_RE.match(inbox):
        raise SendBlocked("OPERATOR_INBOX is not set to a valid address")
    if (d.get("approved_recipient") or "") != inbox:
        raise SendBlocked("this draft was approved for a different recipient (OPERATOR_INBOX changed, or the approval "
                          "predates recipient checks); approve it again")
    if any(s["status"] in ("sent", "dry_run") and s["content_hash"] == d["current_hash"] and s["mode"] == settings.send_mode
           for s in prior_sends):
        raise SendBlocked("this approved version was already %s" % ("sent" if settings.send_mode == "live" else "dry-run sent"))
    if store is not None:
        from .feedback import unsupported_claims
        claims = unsupported_claims(store, d)
        if claims:
            from .safety import raise_incident
            raise_incident(store, "unsupported_claim", "draft %s: %s" % (d["id"][:8], " | ".join(claims)[:300]),
                           draft_id=d["id"])
            raise SendBlocked("the edited draft contains a factual claim that isn't backed by verified evidence (%s). "
                              "Sending is paused; remove or fix the claim, then resolve the incident in Controls"
                              % claims[0][:120])
    if settings.send_mode not in ("dry-run", "live"):
        raise SendBlocked("SEND_MODE must be 'dry-run' or 'live'")
    if settings.send_mode == "live":
        if settings.mock:
            raise SendBlocked("live sending is disabled in mock mode")
        if settings.send_transport == "smtp":
            if not (settings.smtp_user and settings.smtp_password):
                raise SendBlocked("live sending needs SMTP_USER and SMTP_APP_PASSWORD")
        elif settings.send_transport == "gmail_api":
            if gmail is None:
                raise SendBlocked("live sending needs Gmail API access: set GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET and "
                                  "run  python -m sprinto_agent gmail-auth")
        else:
            raise SendBlocked("SEND_TRANSPORT must be 'gmail_api' or 'smtp'")


# SMTP errors raised by the server *before accepting* the message: definitely not delivered, safe to retry.
_DEFINITE_FAILURES = (smtplib.SMTPAuthenticationError, smtplib.SMTPHeloError, smtplib.SMTPNotSupportedError,
                      smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError)


def send(store: Store, settings: Settings, draft_id: str, smtp_factory: Any = None, gmail: Any = None) -> Dict[str, Any]:
    """Send the exact approved version to OPERATOR_INBOX only.

    Live sends are at-most-once: a 'sending' row holding a unique live_lock is written BEFORE contacting Gmail.
    A second attempt (double click, retry, another process) hits the unique index and is refused.
    Outcomes: sent (accepted_by_gmail - never 'delivered'); failed (Gmail did not accept it: lock released,
    retry allowed); uncertain (connection lost after handing over the message: lock kept, sending paused, an
    incident is opened and nothing is retried until the operator resolves it)."""
    from .safety import raise_incident
    from .util import new_id
    d = _draft_or_fail(store, draft_id)
    company = store.one("SELECT name, domain FROM companies WHERE id = %s", (d["company_id"],)) or {}
    poc = store.one("SELECT email FROM pocs WHERE id = %s", (d["poc_id"],)) or {}
    prospect = {"domain": company.get("domain"), "email": poc.get("email")}
    if gmail is None and settings.send_mode == "live" and settings.send_transport == "gmail_api":
        from .gmail import build_client
        gmail = build_client(settings)
    send_check(settings, d, store.sends_for(draft_id), store, prospect, gmail=gmail)
    send_id = new_id()
    try:
        msg = build_message(settings, d["current_subject"], d["current_body"],
                            "%s (%s)" % (company.get("name") or "", company.get("domain") or ""), send_id)
    except SendBlocked as e:
        if "recipient guard" in str(e):
            raise_incident(store, "wrong_recipient", str(e), draft_id=draft_id)
        raise
    campaign_id = _campaign_of(store, d)
    row = {"id": send_id, "draft_id": draft_id, "content_hash": d["current_hash"], "recipient": settings.operator_inbox,
           "mode": settings.send_mode, "sent_at": now_iso(), "is_test": OWN_INBOX_ONLY, "campaign_id": campaign_id,
           "rfc822_message_id": msg["Message-ID"]}
    if settings.send_mode == "dry-run":
        row.update(status="dry_run", error=None, transport="none", delivery_status="not_sent")
        store.insert("sends", row)
        # dry-run keeps the draft 'approved' so this same version can later be sent live
        return {"status": "dry_run", "message": "DRY RUN: nothing sent. Would send to OPERATOR_INBOX only.",
                "preview": msg.as_string()}

    # 1. claim the at-most-once lock (unique index on sends.live_lock)
    row.update(status="sending", error=None, live_lock=draft_id, transport=settings.send_transport,
               delivery_status="unknown")
    try:
        store.insert("sends", row)
    except Exception as e:
        from .store import is_unique_violation
        if is_unique_violation(e):
            raise SendBlocked("this draft is already being sent or was sent; not sending it again")
        raise SendBlocked("could not record the send attempt (%s); nothing was sent" % type(e).__name__)

    # 2. final gate, immediately before transmission (a pause/opt-out may have landed meanwhile)
    try:
        gate_controls(store, d, prospect)
        assert_only_operator(msg, settings.operator_inbox)
    except SendBlocked as e:
        store.update("sends", send_id, {"status": "failed", "live_lock": None, "delivery_status": "not_sent",
                                        "error": "blocked at final gate: %s" % str(e)[:200]})
        raise

    # 3. transmit; classify failures as definite or uncertain
    if settings.send_transport == "gmail_api":
        return _send_gmail(store, settings, d, send_id, msg, gmail)
    phase = "connect"
    try:
        factory = smtp_factory or (lambda: smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30))
        with factory() as s:
            s.starttls()
            phase = "login"
            s.login(settings.smtp_user, settings.smtp_password)
            phase = "transmit"
            s.send_message(msg, to_addrs=[settings.operator_inbox])
            phase = "accepted"
    except Exception as e:
        definite = phase in ("connect", "login") or isinstance(e, _DEFINITE_FAILURES)
        if phase == "accepted":
            definite = False  # message accepted; only closing the connection failed
        return _fail(store, send_id, draft_id, definite, "%s during %s" % (type(e).__name__, phase))
    return _record_sent(store, settings, send_id, draft_id, {})


def _send_gmail(store: Store, settings: Settings, d: Dict[str, Any], send_id: str, msg: EmailMessage,
                gmail: Any) -> Dict[str, Any]:
    from .gmail import GmailError
    try:
        res = gmail.send_raw(msg.as_bytes())
    except GmailError as e:
        return _fail(store, send_id, d["id"], e.kind != "uncertain", str(e))
    except Exception as e:  # unknown failure after the request may have gone out
        return _fail(store, send_id, d["id"], False, "%s while sending" % type(e).__name__)
    if not res.get("id"):
        return _fail(store, send_id, d["id"], False, "Gmail response had no message id")
    return _record_sent(store, settings, send_id, d["id"], {"gmail_message_id": res.get("id"),
                                                            "gmail_thread_id": res.get("threadId")})


def _fail(store: Store, send_id: str, draft_id: str, definite: bool, err: str) -> Dict[str, Any]:
    from .safety import raise_incident
    status = "failed" if definite else "uncertain"
    try:
        store.update("sends", send_id, {"status": status, "error": err[:300],
                                        "delivery_status": "not_sent" if definite else "unknown",
                                        "live_lock": None if definite else draft_id})
    except Exception:
        pass  # row stays 'sending' -> treated as uncertain, which blocks retries (safe)
    if definite:
        raise SendBlocked("Gmail did not accept the message (%s); nothing was sent, you can retry" % err)
    try:
        raise_incident(store, "uncertain_send", err, draft_id=draft_id, send_id=send_id)
    except Exception:
        pass
    raise SendBlocked("Gmail send outcome is UNCERTAIN (%s). It may have been accepted. Sending is paused; check "
                      "your Gmail Sent folder, then mark it sent or not sent. It will not be retried automatically" % err)


def _record_sent(store: Store, settings: Settings, send_id: str, draft_id: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    # If this write fails the row stays 'sending' (uncertain): no duplicate can follow.
    from datetime import datetime, timedelta, timezone
    due = (datetime.now(timezone.utc) + timedelta(days=settings.follow_up_days)).isoformat(timespec="seconds")
    try:
        store.update("sends", send_id, dict({"status": "sent", "follow_up_due_at": due,
                                             "delivery_status": "accepted_by_gmail"}, **extra))
        store.update("drafts", draft_id, {"status": "sent", "updated_at": now_iso()})
        d = store.draft(draft_id)
        from .feedback import record_revision
        record_revision(store, d, "sent", d["current_subject"], d["current_body"], "system")
    except Exception as e:
        raise SendBlocked("Gmail accepted the email, but recording it failed (%s). The send stays locked, so it "
                          "cannot be sent twice; resolve it once the database is back" % type(e).__name__)
    return {"status": "sent", "message": "Accepted by Gmail for your inbox (delivery is confirmed only by the "
                                         "absence of a bounce).", "send_id": send_id}


def check_uncertain_in_gmail(store: Store, send_id: str, gmail: Any) -> Dict[str, Any]:
    """Look for the exact message (by its Message-ID) in Gmail. Evidence for the operator; it does not
    resolve the send by itself."""
    row = store.one("SELECT * FROM sends WHERE id = %s", (send_id,))
    if not row or not row.get("rfc822_message_id"):
        return {"found": None, "detail": "no Message-ID recorded for this send"}
    ids = gmail.find_rfc822(row["rfc822_message_id"])
    return {"found": bool(ids), "gmail_message_ids": ids,
            "detail": "found in Gmail: it was accepted" if ids else "not found in Gmail"}


def resolve_uncertain(store: Store, send_id: str, outcome: str, reviewer: str = "operator") -> Dict[str, Any]:
    """Operator checked Gmail's Sent folder. 'sent' records acceptance; 'not_sent' releases the lock so the
    approved version can be sent again. Recorded with who resolved it and when."""
    row = store.one("SELECT * FROM sends WHERE id = %s", (send_id,))
    if not row or row["status"] not in UNRESOLVED:
        raise ReviewError("this send is not awaiting resolution")
    if outcome == "sent":
        store.update("sends", send_id, {"status": "sent", "resolved_by": reviewer, "resolved_at": now_iso(),
                                        "delivery_status": "accepted_by_gmail",
                                        "error": (row.get("error") or "") + "; operator confirmed it was sent"})
        store.update("drafts", row["draft_id"], {"status": "sent", "updated_at": now_iso()})
    elif outcome == "not_sent":
        store.update("sends", send_id, {"status": "failed", "live_lock": None, "resolved_by": reviewer,
                                        "resolved_at": now_iso(), "delivery_status": "not_sent",
                                        "error": (row.get("error") or "") + "; operator confirmed not sent"})
    else:
        raise ReviewError("outcome must be 'sent' or 'not_sent'")
    return store.one("SELECT * FROM sends WHERE id = %s", (send_id,))
