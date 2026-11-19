"""Opt-out / suppression list and the send pause switch.

A suppressed domain or email is never drafted for and never sent. The pause switch blocks every send
(dry-run and live) until switched off; it lives in the database (UI/CLI toggle) and can also be forced
on with SENDING_PAUSED=1 in the environment."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .store import Store, get_flag, is_unique_violation, set_flag
from .util import now_iso, registrable_domain

PAUSE_FLAG = "send_paused"


def _norm(kind: str, value: str) -> str:
    value = (value or "").strip().lower()
    if kind == "domain":
        return registrable_domain(value)
    return value


def add_suppression(store: Store, kind: str, value: str, reason: str = "") -> bool:
    """Returns False if it was already suppressed."""
    if kind not in ("domain", "email"):
        raise ValueError("kind must be 'domain' or 'email'")
    v = _norm(kind, value)
    if not v or (kind == "email" and "@" not in v):
        raise ValueError("a valid %s is required" % kind)
    try:
        store.insert("suppressions", {"kind": kind, "value": v, "reason": (reason or "")[:300], "created_at": now_iso()})
        return True
    except Exception as e:
        if is_unique_violation(e):
            return False
        raise


def remove_suppression(store: Store, suppression_id: str) -> None:
    store.execute("DELETE FROM suppressions WHERE id = %s", (suppression_id,))


def list_suppressions(store: Store) -> List[Dict[str, Any]]:
    return store.query("SELECT * FROM suppressions ORDER BY created_at DESC")


def suppressed_reason(store: Store, domain: Optional[str] = None, email: Optional[str] = None) -> Optional[str]:
    """Why this domain/email is opted out, or None. A suppressed domain covers every address on it."""
    checks = []
    if domain:
        checks.append(("domain", _norm("domain", domain)))
    if email:
        e = _norm("email", email)
        checks.append(("email", e))
        if "@" in e:
            checks.append(("domain", _norm("domain", e.split("@", 1)[1])))
    for kind, v in checks:
        row = store.one("SELECT reason FROM suppressions WHERE kind = %s AND value = %s", (kind, v))
        if row:
            return "%s %s is on the opt-out list%s" % (kind, v, (": " + row["reason"]) if row.get("reason") else "")
    return None


def sending_paused(store: Store) -> Optional[str]:
    if os.environ.get("SENDING_PAUSED", "").strip().lower() in ("1", "true", "yes", "on"):
        return "sending is paused by SENDING_PAUSED in the environment"
    if get_flag(store, PAUSE_FLAG, "0") == "1":
        return "sending is paused (resume it in Controls, with a reason)"
    return None


def set_sending_paused(store: Store, paused: bool, by: str = "operator", reason: str = "") -> None:
    """Pause needs no reason (stopping is always allowed); resuming needs a recorded human reason and no open
    incidents."""
    if paused:
        pause_sending(store, reason or "paused by operator", "human", by)
    else:
        resume_sending(store, reason, by)


# ---------------------------------------------------------------- kill switches (persistent, audited)
def record_control(store: Store, scope: str, scope_id: Optional[str], action: str, reason: str, actor: str,
                   actor_name: str) -> None:
    from .util import redact
    store.insert("control_events", {"scope": scope, "scope_id": scope_id, "action": action,
                                    "reason": redact(reason or "")[:500], "actor": actor, "actor_name": actor_name,
                                    "created_at": now_iso()})


def pause_sending(store: Store, reason: str, actor: str = "human", actor_name: str = "operator") -> None:
    set_flag(store, PAUSE_FLAG, "1", actor_name)
    set_flag(store, PAUSE_FLAG + "_reason", (reason or "")[:300], actor_name)
    record_control(store, "global", None, "pause", reason or "paused", actor, actor_name)


class ResumeRefused(Exception):
    pass


def resume_sending(store: Store, reason: str, by: str) -> None:
    if not (reason or "").strip():
        raise ResumeRefused("a reason is required to resume sending")
    if not (by or "").strip():
        raise ResumeRefused("resuming must be done by a named person")
    open_inc = open_incidents(store)
    if open_inc:
        raise ResumeRefused("%d open incident(s) must be reviewed and resolved before sending can resume" % len(open_inc))
    set_flag(store, PAUSE_FLAG, "0", by)
    set_flag(store, PAUSE_FLAG + "_reason", "", by)
    record_control(store, "global", None, "resume", reason.strip(), "human", by)


def pause_reason(store: Store) -> str:
    return get_flag(store, PAUSE_FLAG + "_reason", "")


INCIDENT_KINDS = ("wrong_recipient", "unauthorized_send", "opt_out_ignored", "unsupported_claim", "uncertain_send")


def raise_incident(store: Store, kind: str, detail: str, draft_id: Optional[str] = None,
                   send_id: Optional[str] = None) -> str:
    """Record an incident and immediately pause every queued send until a person reviews it."""
    from .util import redact
    if kind not in INCIDENT_KINDS:
        raise ValueError("unknown incident kind")
    existing = store.one("SELECT id FROM incidents WHERE kind = %s AND status = 'open' AND COALESCE(draft_id, '') = %s "
                         "AND COALESCE(send_id, '') = %s", (kind, draft_id or "", send_id or ""))
    iid = existing["id"] if existing else store.insert("incidents", {
        "kind": kind, "status": "open", "draft_id": draft_id, "send_id": send_id,
        "detail": redact(detail or "")[:600], "created_at": now_iso()})
    if not existing:
        pause_sending(store, "automatic pause: %s (%s)" % (kind.replace("_", " "), (detail or "")[:120]), "system",
                      "safety check")
    return iid


def open_incidents(store: Store) -> List[Dict[str, Any]]:
    return store.query("SELECT * FROM incidents WHERE status = 'open' ORDER BY created_at")


def resolve_incident(store: Store, incident_id: str, by: str, resolution: str) -> None:
    if not (resolution or "").strip():
        raise ValueError("describe what you checked or changed to resolve the incident")
    row = store.one("SELECT * FROM incidents WHERE id = %s", (incident_id,))
    if not row or row["status"] != "open":
        raise ValueError("incident is not open")
    if row["kind"] == "uncertain_send" and row.get("send_id"):
        s = store.one("SELECT status FROM sends WHERE id = %s", (row["send_id"],))
        if s and s["status"] in ("sending", "uncertain"):
            raise ValueError("mark the uncertain send as sent or not sent first (draft page)")
    store.update("incidents", incident_id, {"status": "resolved", "resolved_at": now_iso(), "resolved_by": by,
                                            "resolution": resolution.strip()[:500]})


# ---------------------------------------------------------------- campaign pause
def campaign(store: Store, campaign_id: str = "pilot") -> Dict[str, Any]:
    return store.one("SELECT * FROM campaigns WHERE id = %s", (campaign_id,)) or {"id": campaign_id, "status": "active"}


def set_campaign_paused(store: Store, campaign_id: str, paused: bool, by: str, reason: str) -> None:
    if not paused and not (reason or "").strip():
        raise ResumeRefused("a reason is required to resume a campaign")
    store.execute("UPDATE campaigns SET status = %s, paused_reason = %s, updated_at = %s WHERE id = %s",
                  ("paused" if paused else "active", (reason or "")[:300] if paused else None, now_iso(), campaign_id))
    record_control(store, "campaign", campaign_id, "pause" if paused else "resume", reason or "paused", "human", by)


def campaign_block(store: Store, campaign_id: str = "pilot") -> Optional[str]:
    c = campaign(store, campaign_id)
    if c.get("status") == "paused":
        return "campaign '%s' is paused%s" % (c.get("name") or campaign_id,
                                              (": " + c["paused_reason"]) if c.get("paused_reason") else "")
    if c.get("performance_paused"):
        return "campaign '%s' is paused by the pilot performance check; review the numbers in Report" % (
            c.get("name") or campaign_id)
    return None
