"""One-month pilot report and the performance pause check. Statistics come only from stored records; a stage
with no records shows 0 of 0 (rate 'n/a'), never an estimate. Test/demo sends (own-inbox mode) are reported
separately and excluded from prospect reply, meeting and conversion metrics.

Performance pause: thresholds and the minimum sample are set by the operator before the pilot starts (no
defaults are invented). Below the minimum sample nothing is concluded and nothing is paused.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .store import Store, is_unique_violation
from .util import now_iso, parse_date

THRESHOLD_KEYS = {"max_bounce_rate": "bounced / accepted by Gmail", "max_opt_out_rate": "opt-outs / accepted by Gmail",
                  "max_negative_rate": "negative replies / accepted by Gmail"}


def _reply_state(s):
    from .gmail_sync import reply_state
    return reply_state(s)


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 4) if den else None


def _stage(name: str, entered: int, advanced: int, breakdown: Dict[str, int], denominator: str,
           note: str = "") -> Dict[str, Any]:
    return {"stage": name, "entered": entered, "advanced": advanced, "not_advanced": entered - advanced,
            "rate": _rate(advanced, entered), "denominator": denominator, "breakdown": breakdown, "note": note}


# ---------------------------------------------------------------- pilot settings
def configure_pilot(store: Store, campaign_id: str, by: str, thresholds: Dict[str, Any], min_sample: Optional[int],
                    pilot_start: Optional[str] = None, pilot_days: int = 30) -> None:
    clean: Dict[str, float] = {}
    for k, v in (thresholds or {}).items():
        if k not in THRESHOLD_KEYS or v in (None, ""):
            continue
        f = float(v)
        if not 0 <= f <= 1:
            raise ValueError("%s must be a fraction between 0 and 1 (e.g. 0.05 for 5%%)" % k)
        clean[k] = f
    if min_sample is not None and int(min_sample) < 1:
        raise ValueError("minimum sample must be at least 1")
    c = store.one("SELECT * FROM campaigns WHERE id = %s", (campaign_id,))
    if not c:
        raise ValueError("campaign not found")
    if pilot_start:
        if not clean or not min_sample:
            raise ValueError("set the performance thresholds and minimum sample before starting the pilot")
        date.fromisoformat(pilot_start[:10])
    from .safety import record_control
    store.execute("UPDATE campaigns SET thresholds_json = %s, min_sample = %s, pilot_start = COALESCE(%s, pilot_start), "
                  "pilot_days = %s, updated_at = %s WHERE id = %s",
                  (json.dumps(clean), int(min_sample) if min_sample else None, pilot_start[:10] if pilot_start else None,
                   int(pilot_days), now_iso(), campaign_id))
    record_control(store, "performance", campaign_id, "configure",
                   "thresholds %s, min sample %s, start %s" % (clean, min_sample, pilot_start or c.get("pilot_start")),
                   "human", by)


def window(c: Dict[str, Any], as_of: Optional[date] = None) -> Dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc).date()
    if not c.get("pilot_start"):
        return {"started": False, "start": None, "end": None, "as_of": as_of.isoformat(), "final": False, "day": None}
    start = date.fromisoformat(str(c["pilot_start"])[:10])
    end = start + timedelta(days=int(c.get("pilot_days") or 30))
    upto = min(as_of, end)
    return {"started": True, "start": start.isoformat(), "end": end.isoformat(), "as_of": upto.isoformat(),
            "final": as_of >= end, "day": (upto - start).days}


def _in(ts: Any, w: Dict[str, Any]) -> bool:
    if not w["started"]:
        return True
    d = parse_date(str(ts or ""))
    if d is None:
        return False
    day = d.date().isoformat()
    return w["start"] <= day < (date.fromisoformat(w["as_of"]) + timedelta(days=1)).isoformat() and day < w["end"]


# ---------------------------------------------------------------- report
def build_report(store: Store, campaign_id: str = "pilot", as_of: Optional[date] = None) -> Dict[str, Any]:
    from .feedback import draft_change_summary
    c = store.one("SELECT * FROM campaigns WHERE id = %s", (campaign_id,)) or {"id": campaign_id}
    w = window(c, as_of)
    runs = {r["id"]: r for r in store.query("SELECT id, campaign_id, is_mock FROM research_runs")}
    in_campaign = {rid for rid, r in runs.items() if (r.get("campaign_id") or "pilot") == campaign_id and not r.get("is_mock")} \
        if not store.is_mock else {rid for rid, r in runs.items() if (r.get("campaign_id") or "pilot") == campaign_id}
    cos = [r for r in store.query("SELECT * FROM companies ORDER BY created_at") if r["run_id"] in in_campaign
           and _in(r["created_at"], w)]
    cids = {r["id"] for r in cos}
    pocs = [p for p in store.query("SELECT * FROM pocs ORDER BY created_at") if p["company_id"] in cids]
    drafts = [d for d in store.query("SELECT * FROM drafts ORDER BY created_at") if d["company_id"] in cids]
    dids = {d["id"] for d in drafts}
    reviews = [r for r in store.query("SELECT draft_id, decision FROM review_decisions") if r["draft_id"] in dids]
    sends = [s for s in store.query("SELECT * FROM sends ORDER BY sent_at") if s["draft_id"] in dids]
    fb = [f for f in store.query("SELECT kind, verdict, company_id, poc_id, details_json FROM feedback") if f["company_id"] in cids]

    # 1. discovery
    s1 = Counter(r["stage1_status"] for r in cos)
    st1 = _stage("1. Discovered -> kept for research", len(cos), s1.get("shortlisted", 0) + s1.get("uncertain", 0),
                 {"shortlisted": s1.get("shortlisted", 0), "rejected": s1.get("rejected", 0), "uncertain": s1.get("uncertain", 0)},
                 "companies discovered")
    # 2. research
    short = [r for r in cos if r["stage1_status"] != "rejected"]
    done = [r for r in short if r["qualification"] and r["qualification"] != "error"]
    q = Counter(r["qualification"] for r in done)
    st2 = _stage("2. Kept -> research completed", len(short), len(done),
                 {"qualified": q.get("qualified", 0), "rejected": q.get("rejected", 0), "uncertain": q.get("uncertain", 0),
                  "not researched": sum(1 for r in short if not r["qualification"]),
                  "failed / cost-capped": sum(1 for r in short if r["qualification"] == "error" or r.get("stop_code") == "cost_cap_reached")},
                 "companies kept at discovery (shortlisted or uncertain)")
    # 3. POC
    qual_ids = {r["id"] for r in done if r["qualification"] == "qualified"}
    with_poc = {p["company_id"] for p in pocs if p["company_id"] in qual_ids}
    rejected_poc = sum(1 for p in pocs if p.get("status") == "rejected")
    reruns = sum(1 for f in fb if f["kind"] == "poc" and '"rerun": true' in (f["details_json"] or ""))
    accepted_co = {p["company_id"] for p in pocs if p["company_id"] in qual_ids and (p.get("status") or "active") == "active"}
    st3 = _stage("3. Qualified -> POC found", len(qual_ids), len(with_poc),
                 {"POC found": len(with_poc), "not found": len(qual_ids - with_poc),
                  "POC accepted (active)": len(accepted_co), "POC rejected by reviewer": rejected_poc, "re-runs": reruns},
                 "qualified companies")
    # 4. drafts
    d_by_co: Dict[str, List[Dict[str, Any]]] = {}
    for d in drafts:
        d_by_co.setdefault(d["company_id"], []).append(d)
    drafted = {cid for cid in accepted_co if cid in d_by_co}
    dec = Counter(r["decision"] for r in reviews)
    approved_d = [d for d in drafts if d["status"] in ("approved", "sent") or
                  any(r["draft_id"] == d["id"] and r["decision"] == "approve" for r in reviews)]
    edited_d = {r["draft_id"] for r in reviews if r["decision"] == "edit"}
    st4 = _stage("4. POC accepted -> draft generated", len(accepted_co), len(drafted),
                 {"drafts generated": len([d for d in drafts if d["company_id"] in accepted_co]),
                  "approved": len(approved_d), "edited": len(edited_d),
                  "rejected": sum(1 for d in drafts if d["status"] == "rejected"),
                  "superseded": sum(1 for d in drafts if d["status"] == "superseded"),
                  "review actions": dict(dec)},
                 "companies with an accepted POC")
    changes = [draft_change_summary(store, d) for d in drafts if d["status"] != "superseded"
               and (d["status"] in ("approved", "sent") or d["id"] in edited_d)]
    cat_counts = Counter(c_ for ch in changes for c_ in ch["categories"])
    ratios = sorted(ch["change_ratio"] for ch in changes)
    edit_stats = {"drafts_considered": len(changes), "edited": sum(1 for ch in changes if ch["edited"]),
                  "median_change_ratio": ratios[len(ratios) // 2] if ratios else None,
                  "mean_change_ratio": round(sum(ratios) / len(ratios), 3) if ratios else None,
                  "categories": dict(cat_counts),
                  "note": "change ratio = 1 - text similarity between first generated and final version"}

    # 5-7: sends / replies / meetings, split test vs prospect
    def send_block(group: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
        live = [s for s in group if s["mode"] == "live"]
        accepted = [s for s in live if s.get("delivery_status") in ("accepted_by_gmail", "bounced")]
        bounced = [s for s in live if s.get("delivery_status") == "bounced"]
        unknown = [s for s in live if s["status"] in ("sending", "uncertain") or s.get("delivery_status") == "unknown"]
        approved_ids = {d["id"] for d in approved_d}
        # own-inbox mode: every approved draft can only become a test send; prospect entries need a prospect send
        entered = approved_ids if label == "test/demo" else (approved_ids & {s["draft_id"] for s in group})
        attempted_drafts = {s["draft_id"] for s in live} & entered
        st5 = _stage("5. Approved draft -> send attempted (%s)" % label, len(entered), len(attempted_drafts),
                     {"send attempts": len(live), "accepted by Gmail": len(accepted), "bounced": len(bounced),
                      "unknown outcome": len(unknown), "failed (not accepted)": sum(1 for s in live if s["status"] == "failed"),
                      "dry runs (nothing sent)": sum(1 for s in group if s["mode"] == "dry-run")},
                     "approved drafts", "accepted by Gmail is not proof of inbox delivery")
        ev = store.query("SELECT e.event_type, e.send_id, c.category FROM gmail_events e LEFT JOIN reply_classifications c "
                         "ON c.event_id = e.id WHERE e.event_type IN ('reply', 'meeting_invite')")
        ids = {s["id"] for s in accepted}
        cats = Counter(r["category"] or "other" for r in ev if r["send_id"] in ids and r["event_type"] == "reply")
        replied = {r["send_id"] for r in ev if r["send_id"] in ids}
        st6 = _stage("6. Accepted by Gmail -> replied (%s)" % label, len(accepted) - len(bounced), len(replied),
                     dict({k: cats.get(k, 0) for k in ("positive", "negative", "question", "ambiguous", "out_of_office", "opt_out", "other")},
                          no_reply_after_window=sum(1 for s in accepted if _reply_state(s) == "no_reply")),
                     "sends accepted by Gmail and not bounced", "categories are rule-based classifications of replies")
        meet = sum(1 for s in accepted if s.get("meeting_status") == "invite_received")
        st7 = _stage("7. Replied -> meeting invitation (%s)" % label, len(replied), meet, {"calendar invitations received": meet},
                     "sends with a reply", "only counted from a calendar invitation (text/calendar METHOD:REQUEST) "
                                           "in the thread; no other meeting signal is used")
        return {"send": st5, "replies": st6, "meetings": st7}

    test_sends = [s for s in sends if s.get("is_test", True) in (True, 1, "1", "true", "t")]
    prospect_sends = [s for s in sends if s not in test_sends]
    cohorts = _cohorts(cos, done, accepted_co, drafted, approved_d, int(c.get("min_sample") or 0))
    return {"campaign": {"id": campaign_id, "name": c.get("name"), "pilot_start": c.get("pilot_start"),
                         "pilot_days": c.get("pilot_days")},
            "window": w, "kind": "final" if w["final"] else "preliminary", "generated_at": now_iso(),
            "stages": [st1, st2, st3, st4], "draft_edits": edit_stats,
            "test_demo": send_block(test_sends, "test/demo"),
            "prospect": send_block(prospect_sends, "prospect"),
            "prospect_note": ("No prospect sends: this project only sends to the operator's own inbox, so all send, "
                              "reply and meeting numbers are test/demo and excluded from prospect metrics."
                              if not prospect_sends else ""),
            "cohorts": cohorts, "performance": performance(store, campaign_id, as_of),
            "data_notes": ["Rates show advanced / entered with the denominator named; 'n/a' means 0 entered.",
                           "Drafts created before revision tracking have their edits combined into one backfilled revision.",
                           "Companies are counted per run row; a domain researched in two runs counts twice."]}


def _cohorts(cos, done, accepted_co, drafted, approved_d, min_sample: int) -> Dict[str, Any]:
    from .dashboard import _source_kind
    approved_co = {d["company_id"] for d in approved_d}
    by: Dict[str, Dict[str, int]] = {}
    qual = {r["id"] for r in done if r["qualification"] == "qualified"}
    for r in cos:
        k = _source_kind(r["found_via"])
        b = by.setdefault(k, {"discovered": 0, "qualified": 0, "drafted": 0, "approved": 0})
        b["discovered"] += 1
        b["qualified"] += r["id"] in qual
        b["drafted"] += r["id"] in drafted
        b["approved"] += r["id"] in approved_co
    out = {}
    for k, b in by.items():
        out[k] = dict(b, qualified_rate=_rate(b["qualified"], b["discovered"]),
                      small_sample=(not min_sample) or b["discovered"] < min_sample)
    return {"by_discovery_source": out,
            "note": "Cohorts below the minimum sample (%s) are shown for counts only; don't compare their rates." % (
                min_sample or "not set")}


# ---------------------------------------------------------------- performance pause
def performance(store: Store, campaign_id: str = "pilot", as_of: Optional[date] = None,
                apply: bool = False) -> Dict[str, Any]:
    """Prospect-only rates vs operator thresholds. Pauses the campaign only when apply=True, thresholds are set,
    and the sample reaches the operator's minimum."""
    c = store.one("SELECT * FROM campaigns WHERE id = %s", (campaign_id,)) or {}
    th = json.loads(c.get("thresholds_json") or "{}")
    min_sample = c.get("min_sample")
    w = window(c, as_of)
    sends = [s for s in store.query("SELECT * FROM sends WHERE mode = 'live' AND campaign_id = %s", (campaign_id,))
             if not s.get("is_test", True) in (True, 1, "1", "true", "t") and _in(s["sent_at"], w)]
    accepted = [s for s in sends if s.get("delivery_status") in ("accepted_by_gmail", "bounced")]
    n = len(accepted)
    counts = {"bounced": sum(1 for s in accepted if s.get("delivery_status") == "bounced"),
              "opt_out": sum(1 for s in accepted if s.get("reply_status") == "opt_out"),
              "negative": sum(1 for s in accepted if s.get("reply_status") == "negative")}
    rates = {"bounce_rate": _rate(counts["bounced"], n), "opt_out_rate": _rate(counts["opt_out"], n),
             "negative_rate": _rate(counts["negative"], n)}
    out = {"window": w, "denominator": "prospect sends accepted by Gmail", "n": n, "counts": counts, "rates": rates,
           "thresholds": th, "min_sample": min_sample, "breaches": [], "status": ""}
    if not th or not min_sample:
        out["status"] = "not configured: set thresholds and a minimum sample before the pilot"
        return out
    if n < int(min_sample):
        out["status"] = "insufficient sample: %d of %d prospect sends; no conclusion drawn, nothing paused" % (n, int(min_sample))
        return out
    for key, label in (("max_bounce_rate", "bounce_rate"), ("max_opt_out_rate", "opt_out_rate"),
                       ("max_negative_rate", "negative_rate")):
        if key in th and rates[label] is not None and rates[label] > th[key]:
            out["breaches"].append("%s %.1f%% > threshold %.1f%% (n=%d)" % (label, rates[label] * 100, th[key] * 100, n))
    out["status"] = "threshold breached" if out["breaches"] else "within thresholds"
    if apply and out["breaches"] and not c.get("performance_paused"):
        from .safety import record_control
        store.execute("UPDATE campaigns SET performance_paused = %s, updated_at = %s WHERE id = %s",
                      (True, now_iso(), campaign_id))
        record_control(store, "performance", campaign_id, "pause", "; ".join(out["breaches"]), "system", "pilot check")
    return out


def resume_performance(store: Store, campaign_id: str, by: str, reason: str) -> None:
    from .safety import ResumeRefused, record_control
    if not (reason or "").strip():
        raise ResumeRefused("a reason is required")
    store.execute("UPDATE campaigns SET performance_paused = %s, updated_at = %s WHERE id = %s", (False, now_iso(), campaign_id))
    record_control(store, "performance", campaign_id, "resume", reason, "human", by)


def save_report(store: Store, rep: Dict[str, Any]) -> str:
    """Idempotent per (campaign, kind, as_of): regenerating the same day replaces that day's snapshot."""
    key = (rep["campaign"]["id"], rep["kind"], rep["window"]["as_of"])
    existing = store.one("SELECT id FROM pilot_reports WHERE campaign_id = %s AND kind = %s AND as_of = %s", key)
    body = json.dumps(rep, default=str)
    if existing:
        store.update("pilot_reports", existing["id"], {"report_json": body, "created_at": now_iso()})
        return existing["id"]
    try:
        return store.insert("pilot_reports", {"campaign_id": key[0], "kind": key[1], "as_of": key[2],
                                              "window_start": rep["window"]["start"] or "", "window_end": rep["window"]["end"] or "",
                                              "report_json": body, "created_at": now_iso()})
    except Exception as e:
        if is_unique_violation(e):
            row = store.one("SELECT id FROM pilot_reports WHERE campaign_id = %s AND kind = %s AND as_of = %s", key)
            store.update("pilot_reports", row["id"], {"report_json": body})
            return row["id"]
        raise
