"""Read model for the UI: pipeline counts, per-company stage and next step, the action queue.
Pure reads over the store; nothing here changes data."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .store import Store

# Pipeline stages in order. `live=False` stages are reserved for reply tracking (not connected yet).
STAGES = [
    ("discovered", "Discovered", True),
    ("researched", "Researched", True),
    ("qualified", "Qualified", True),
    ("contact", "Contact found", True),
    ("drafted", "Drafted", True),
    ("approved", "Approved", True),
    ("sent", "Sent", True),
    ("replied", "Replied", True),
]


@dataclass
class CompanyView:
    row: Dict[str, Any]
    stage: str                 # key used for the pill
    stage_label: str
    tone: str                  # good | warn | bad | info | muted | accent
    next_label: str
    next_href: Optional[str]
    next_is_action: bool
    signals: int
    poc: Optional[Dict[str, Any]] = None
    draft: Optional[Dict[str, Any]] = None
    sent: bool = False
    dry_run: bool = False
    reached: List[str] = field(default_factory=list)   # pipeline stage keys this company reached


def _latest_per_domain(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: r["created_at"]):
        key = r["domain"] if r["domain"] != "(unknown)" else "name:" + (r.get("name") or r["id"])
        prev = seen.get(key)
        # a later row only replaces an earlier one if it actually got further (researched)
        if prev is None or r.get("qualification") or not prev.get("qualification"):
            seen[key] = r
    return list(seen.values())


SNAPSHOT_SQL = [
    ("companies", "SELECT * FROM companies ORDER BY created_at"),
    ("pocs", "SELECT * FROM pocs ORDER BY created_at"),
    ("drafts", "SELECT * FROM drafts WHERE status <> 'superseded' ORDER BY created_at"),
    ("signals", "SELECT company_id, COUNT(DISTINCT criterion) AS n FROM evidence WHERE status = 'verified' GROUP BY company_id"),
    ("sends", "SELECT * FROM sends ORDER BY sent_at"),
    ("runs", "SELECT * FROM research_runs ORDER BY started_at DESC"),
]


def load_snapshot(store: Store) -> Dict[str, List[Dict[str, Any]]]:
    """Everything the overview/pipeline pages need, in one pipelined round trip on Neon."""
    res = store.query_many([(sql, ()) for _, sql in SNAPSHOT_SQL])
    return {name: rows for (name, _), rows in zip(SNAPSHOT_SQL, res)}


def snapshot(store: Store) -> Dict[str, List[Dict[str, Any]]]:
    return store.display_snapshot(load_snapshot)


def company_views(store: Store, run_id: Optional[str] = None, running_run_ids: Optional[set] = None,
                  snap: Optional[Dict[str, Any]] = None) -> List[CompanyView]:
    snap = snap or snapshot(store)
    if run_id:
        rows = [r for r in snap["companies"] if r["run_id"] == run_id]
    else:
        rows = _latest_per_domain(snap["companies"])
    if not rows:
        return []
    ids = {r["id"] for r in rows}
    pocs: Dict[str, Dict[str, Any]] = {}
    for p in snap["pocs"]:
        if p["company_id"] in ids:
            pocs[p["company_id"]] = p
    drafts: Dict[str, Dict[str, Any]] = {}
    for d in snap["drafts"]:
        if d["company_id"] in ids:
            drafts[d["company_id"]] = d
    sig = {r["company_id"]: int(r["n"]) for r in snap["signals"]}
    sends: Dict[str, List[Dict[str, Any]]] = {}
    for s in snap["sends"]:
        sends.setdefault(s["draft_id"], []).append(s)
    running_run_ids = running_run_ids if running_run_ids is not None else \
        {r["id"] for r in snap["runs"] if r["status"] in ("queued", "running")}
    return [_view(r, pocs.get(r["id"]), drafts.get(r["id"]), sig.get(r["id"], 0),
                  sends.get(drafts[r["id"]]["id"], []) if r["id"] in drafts else [],
                  r["run_id"] in running_run_ids) for r in rows]


def _view(r: Dict[str, Any], poc, draft, signals: int, sends: List[Dict[str, Any]], running: bool) -> CompanyView:
    cid = r["id"]
    q = r.get("qualification")
    sent = any(s["status"] == "sent" for s in sends)
    dry = any(s["status"] == "dry_run" for s in sends)
    reached = ["discovered"]
    if q and q != "error":
        reached.append("researched")
    if q == "qualified":
        reached.append("qualified")
    if poc:
        reached.append("contact")
    if draft:
        reached.append("drafted")
    if draft and draft["status"] in ("approved", "sent"):
        reached.append("approved")
    if sent:
        reached.append("sent")
    replied = [x for x in sends if (x.get("reply_status") or "not_tracked") not in ("not_tracked", "")]
    if sent and replied:
        reached.append("replied")
    href = "/companies/%s" % cid

    def v(stage, label, tone, nl, nh, act):
        return CompanyView(r, stage, label, tone, nl, nh, act, signals, poc, draft, sent, dry, reached)

    if r["stage1_status"] == "rejected":
        return v("skipped", "Skipped", "muted", "Skipped at discovery", href, False)
    if r["domain"] == "(unknown)":
        return v("discovered", "No website", "muted", "Website not linked in source", href, False)
    if q is None:
        if running:
            return v("researching", "Researching", "info", "In progress", None, False)
        why = (r.get("stop_reason") or "Waiting for a run").replace("not researched: ", "")
        return v("discovered", "Not researched", "muted", (why[:1].upper() + why[1:])[:60], href, False)
    if q == "error" or (r.get("error_kind") and r.get("retryable")):
        return v("error", "Needs retry" if r.get("retryable") else "Failed", "bad",
                 "Retry (%s)" % (r.get("error_kind") or "error").replace("_", " "), href, True)
    if q == "rejected":
        return v("rejected", "Rejected", "bad", "See why", href, False)
    if q == "uncertain":
        return v("uncertain", "Uncertain", "warn", "See open questions", href, False)
    # qualified
    if sent and "replied" in reached:
        cat = [x.get("reply_status") for x in sends if (x.get("reply_status") or "not_tracked") != "not_tracked"][-1]
        return v("replied", "Replied: %s" % cat.replace("_", " "), "good" if cat == "positive" else
                 ("bad" if cat in ("negative", "opt_out") else "info"), "See reply", "/outreach", False)
    if sent:
        return v("sent", "Sent", "good", "Awaiting reply", href, False)
    if draft and draft["status"] == "approved":
        return v("approved", "Approved", "accent", "Send draft", "/drafts/%s" % draft["id"], True)
    if draft and draft["status"] == "pending_review":
        return v("drafted", "Draft ready", "accent", "Review draft", "/drafts/%s" % draft["id"], True)
    if draft and draft["status"] == "rejected":
        return v("drafted", "Draft rejected", "muted", "Open company", href, False)
    if not poc:
        return v("qualified", "Needs contact", "warn", "Add Apollo contact", href + "#contact", True)
    return v("contact", "Contact found", "good", "Open company", href, False)


def funnel(views: List[CompanyView]) -> List[Dict[str, Any]]:
    out = []
    for key, label, live in STAGES:
        n = sum(1 for v in views if key in v.reached) if live else None
        out.append({"key": key, "label": label, "live": live, "count": n})
    return out


@dataclass
class ActionItem:
    label: str
    detail: str
    count: Optional[int]
    href: Optional[str]
    tone: str
    connected: bool = True


def action_queue(store: Store, views: List[CompanyView]) -> List[ActionItem]:
    drafts = [v for v in views if v.stage == "drafted" and v.draft and v.draft["status"] == "pending_review"]
    approved = [v for v in views if v.stage == "approved"]
    need_contact = [v for v in views if v.stage == "qualified"]
    failed = [v for v in views if v.stage == "error"]
    snap = snapshot(store)
    seen_u: List[str] = []
    for s in snap["sends"]:
        if s["status"] in ("sending", "uncertain") and s["draft_id"] not in seen_u:
            seen_u.append(s["draft_id"])
    uncertain = [{"draft_id": d} for d in seen_u]
    interrupted = [r for r in snap["runs"] if r["status"] in ("interrupted", "failed")]
    retryable = [v for v in views if v.row.get("error_kind") and v.row.get("retryable")]
    items = [
        ActionItem("Resolve uncertain sends", "Gmail didn't confirm delivery; check your Sent folder before anything is retried",
                   len(uncertain), "/drafts/%s" % uncertain[0]["draft_id"] if uncertain else "/outreach", "bad"),
        ActionItem("Review drafts", "Approve, edit or reject each email before it can be sent",
                   len(drafts), "/review", "accent"),
        ActionItem("Add a contact", "Qualified companies where Hunter found no suitable person",
                   len(need_contact), "/pipeline?stage=needs_contact", "warn"),
        ActionItem("Send approved drafts", "Approved versions waiting to go to your inbox",
                   len(approved), "/review?status=approved", "good"),
        ActionItem("Retry blocked or failed companies", "A site, API or database problem stopped them; retry continues from the last completed stage",
                   len({v.row["id"] for v in failed + retryable}), "/pipeline?stage=error", "bad"),
        ActionItem("Resume interrupted runs", "A run stopped part-way (crash or restart); resuming skips work already done",
                   len(interrupted), "/runs/%s" % interrupted[0]["id"] if interrupted else "/runs", "warn"),
        ActionItem("Follow-ups due", "Reply tracking isn't connected yet; follow-ups will appear here",
                   None, "/outreach", "muted", connected=False),
    ]
    return items


def run_progress(store: Store, run: Dict[str, Any]) -> Dict[str, Any]:
    rows = store.query("SELECT qualification, stage1_status, domain, stop_reason FROM companies WHERE run_id = %s", (run["id"],))
    inputs = json.loads(run.get("input_json") or "{}")
    limit = int(inputs.get("max_research", 8))
    candidates = [r for r in rows if r["stage1_status"] != "rejected" and r["domain"] != "(unknown)"]
    target = min(len(candidates), limit)
    done = sum(1 for r in rows if r["qualification"])
    return {"discovered": len(rows), "target": target, "done": done,
            "phase": "Discovering companies" if not rows else ("Researching companies" if done < target else "Finishing")}


def summary(raw: Optional[str]) -> Dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def _source_kind(found_via: str) -> str:
    f = found_via or ""
    if f.startswith("operator"):
        return "added by you"
    if f.startswith("RSS"):
        return "news feeds"
    if f.startswith("Tavily"):
        return "search"
    if f.startswith("portfolio"):
        return "portfolios"
    return "other"


def stage_details(views: List[CompanyView], snap: Optional[Dict[str, Any]] = None, limit: int = 4) -> List[Dict[str, Any]]:
    """Per live stage: who is there (furthest stage reached) and one line of what matters at that stage."""
    out: List[Dict[str, Any]] = []
    order = [k for k, _, live in STAGES if live]

    def at(key: str) -> List[CompanyView]:
        # companies whose furthest stage is `key`
        return [v for v in views if v.reached and v.reached[-1] == key]

    def reached(key: str) -> List[CompanyView]:
        return [v for v in views if key in v.reached]

    def name(v: CompanyView) -> str:
        return v.row.get("name") or v.row["domain"]

    def pack(key, label, members, detail, note=None):
        members = list(members)
        out.append({"key": key, "label": label, "count": len(members), "detail": detail,
                    "companies": [{"id": v.row["id"], "name": name(v), "note": note(v) if note else ""} for v in members[:limit]],
                    "more": max(0, len(members) - limit)})

    disc = reached("discovered")
    by_src = Counter(_source_kind(v.row.get("found_via")) for v in disc)
    skipped = sum(1 for v in disc if v.stage in ("skipped", "discovered"))
    pack("discovered", "Discovered", at("discovered"),
         "%d found (%s)%s" % (len(disc), ", ".join("%d %s" % (n, k) for k, n in by_src.most_common()) or "none",
                              "; %d not researched" % skipped if skipped else ""),
         lambda v: v.stage_label.lower())
    res = reached("researched")
    q = Counter(v.row.get("qualification") for v in res)
    pack("researched", "Researched", at("researched"),
         "%d researched: %d qualified, %d uncertain, %d rejected" % (len(res), q.get("qualified", 0), q.get("uncertain", 0),
                                                                    q.get("rejected", 0)),
         lambda v: v.stage_label.lower())
    qual = reached("qualified")
    no_poc = at("qualified")
    pack("qualified", "Qualified", sorted(qual, key=lambda v: -(v.row.get("score") or 0)),
         "%d passed the ICP%s" % (len(qual), "; %d need a contact" % len(no_poc) if no_poc else ""),
         lambda v: "score %s" % v.row.get("score"))
    con = reached("contact")
    pack("contact", "Contact found", con,
         "%d with a named contact (%d verified on their site)" % (
             len(con), sum(1 for v in con if v.poc and v.poc.get("verification_status") == "verified")),
         lambda v: ("%s, %s" % (v.poc["full_name"], v.poc["title"]))[:40] if v.poc else "")
    dr = reached("drafted")
    st = Counter(v.draft["status"] for v in dr if v.draft)
    pack("drafted", "Drafted", dr,
         "%d waiting for review, %d rejected" % (st.get("pending_review", 0), st.get("rejected", 0)),
         lambda v: LABELS.get(v.draft["status"], v.draft["status"]) if v.draft else "")
    ap = reached("approved")
    pack("approved", "Approved", ap, "%d approved, %d waiting to send" % (len(ap), len(at("approved"))))
    sent = reached("sent")
    pack("sent", "Sent", at("sent"), "%d sent to your inbox (test sends; prospects are never emailed)" % len(sent))
    rep = reached("replied")
    pack("replied", "Replied", rep, "%d with a reply tracked from Gmail (test replies, kept out of prospect metrics)" % len(rep),
         lambda v: v.stage_label.replace("Replied: ", ""))
    return [d for d in out if d["key"] in order]


LABELS = {"pending_review": "needs review", "approved": "approved", "rejected": "rejected", "sent": "sent"}
