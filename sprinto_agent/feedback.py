"""Reviewer feedback, draft revision history, and reviewable style guidance.

Separation rules (tested):
  * Feedback is an OPINION. It is stored in `feedback`, never in `evidence`, and never changes a company's
    qualification, score, ICP rules, approvals, recipients, suppression or cost caps.
  * Research-quality feedback may only DEMOTE a source domain (it is then not fetched as third-party evidence);
    it can never create or upgrade a fact.
  * Draft edits produce wording-only style guidance that starts as 'proposed'; only guidance the operator
    accepts is used, scoped by POC role and company segment. Factual claims are never learned from edits.
  * Every row carries run/company/contact/draft ids and the prompt / ICP / policy versions in force.
  * Writes are idempotent (idem_key) and survive a database outage via a local outbox that is replayed later.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DATA_DIR
from .store import Store, is_unique_violation
from .util import content_hash, get_logger, now_iso, redact, registrable_domain
from . import versions

log = get_logger(__name__)

import os as _os
OUTBOX = (Path("/tmp") if _os.environ.get("VERCEL") else DATA_DIR / "outbox") / "feedback.jsonl"   # /tmp is writable on Vercel

KINDS: Dict[str, tuple] = {
    "discovery": ("relevant", "irrelevant"),
    "qualification": ("agree", "should_qualify", "should_reject", "unsure"),
    "research": ("wrong_company", "stale_fact", "conflicting_fact", "weak_source", "missing_evidence"),
    "poc": ("suitable", "unsuitable"),
    "draft": ("approve", "reject", "edit"),
    "outcome": ("meeting_held", "meeting_no_show", "not_a_meeting", "note"),
}
LIST_FIELDS = ("preferred_titles", "excluded_titles", "departments", "seniority")


class FeedbackError(ValueError):
    pass


def _clean_list(v: Any) -> List[str]:
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    return [str(x).strip()[:80] for x in (v or []) if str(x).strip()][:12]


def record_feedback(store: Store, kind: str, verdict: str, reviewer: str, reason: str = "",
                    company_id: Optional[str] = None, run_id: Optional[str] = None, poc_id: Optional[str] = None,
                    draft_id: Optional[str] = None, evidence_id: Optional[str] = None, send_id: Optional[str] = None,
                    details: Optional[Dict[str, Any]] = None, idem_key: Optional[str] = None,
                    outbox: Optional[Path] = None) -> Dict[str, Any]:
    """Save one piece of feedback. Returns {"status": "saved"|"duplicate"|"queued_locally", "id"|"idem_key"}."""
    if kind not in KINDS:
        raise FeedbackError("unknown feedback kind")
    if verdict not in KINDS[kind]:
        raise FeedbackError("verdict for %s must be one of %s" % (kind, ", ".join(KINDS[kind])))
    if kind in ("qualification", "research", "discovery") and verdict not in ("agree", "relevant") and not (reason or "").strip():
        raise FeedbackError("please give a short reason")
    if not (reviewer or "").strip():
        raise FeedbackError("reviewer is required")
    details = dict(details or {})
    for f in LIST_FIELDS:
        if f in details:
            details[f] = _clean_list(details[f])
    row: Dict[str, Any] = {
        "created_at": now_iso(), "kind": kind, "verdict": verdict,
        "reason": redact((reason or "").strip())[:1000], "details_json": json.dumps(details, sort_keys=True, default=str),
        "reviewer": reviewer.strip()[:80], "run_id": run_id, "company_id": company_id, "poc_id": poc_id,
        "draft_id": draft_id, "evidence_id": evidence_id, "send_id": send_id}
    # context the verdict refers to (who/what was judged, and with which versions)
    try:
        if company_id and not run_id:
            co = store.one("SELECT run_id FROM companies WHERE id = %s", (company_id,))
            row["run_id"] = (co or {}).get("run_id")
        run = store.one("SELECT icp_version, prompt_version, policy_version FROM research_runs WHERE id = %s",
                        (row["run_id"],)) if row["run_id"] else None
    except Exception:
        run = None
    cur = versions.current()
    row["prompt_version"] = (run or {}).get("prompt_version") or cur["prompt_version"]
    row["icp_version"] = cur["icp_version"] if not run else (run.get("icp_version") or cur["icp_version"])
    row["policy_version"] = (run or {}).get("policy_version") or cur["policy_version"]
    row["idem_key"] = idem_key or hashlib.sha256(json.dumps(
        [kind, verdict, row["reason"], row["details_json"], row["reviewer"], company_id, poc_id, draft_id, evidence_id,
         send_id]).encode()).hexdigest()[:32]
    try:
        fid = store.insert("feedback", row)
        return {"status": "saved", "id": fid}
    except Exception as e:
        if is_unique_violation(e):
            return {"status": "duplicate", "idem_key": row["idem_key"]}
        # database unavailable: keep it locally, replay later (idempotent by idem_key)
        path = outbox or OUTBOX
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        log.warning("feedback saved locally (database unavailable: %s)", type(e).__name__)
        return {"status": "queued_locally", "idem_key": row["idem_key"]}


def flush_outbox(store: Store, outbox: Optional[Path] = None) -> Dict[str, int]:
    path = outbox or OUTBOX
    if not path.exists():
        return {"saved": 0, "duplicate": 0, "left": 0}
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    left: List[str] = []
    saved = dup = 0
    for l in lines:
        try:
            row = json.loads(l)
        except ValueError:
            continue
        try:
            store.insert("feedback", row)
            saved += 1
        except Exception as e:
            if is_unique_violation(e):
                dup += 1
            else:
                left.append(l)
    if left:
        path.write_text("\n".join(left) + "\n", encoding="utf-8")
    else:
        path.unlink()
    return {"saved": saved, "duplicate": dup, "left": len(left)}


def feedback_for(store: Store, company_id: Optional[str] = None, draft_id: Optional[str] = None) -> List[Dict[str, Any]]:
    if draft_id:
        return store.query("SELECT * FROM feedback WHERE draft_id = %s ORDER BY created_at DESC", (draft_id,))
    return store.query("SELECT * FROM feedback WHERE company_id = %s ORDER BY created_at DESC", (company_id,))


# ---------------------------------------------------------------- source / query quality
def demoted_domains(store: Store, min_flags: int = 2) -> List[str]:
    """Third-party domains reviewers flagged as weak or wrong-company at least `min_flags` times.
    The researcher stops fetching them as evidence. Company-own sites are never demoted."""
    rows = store.query("SELECT f.verdict, e.source_url, c.domain FROM feedback f JOIN evidence e ON e.id = f.evidence_id "
                       "JOIN companies c ON c.id = e.company_id WHERE f.kind = 'research' "
                       "AND f.verdict IN ('weak_source', 'wrong_company')")
    counts: Dict[str, int] = {}
    for r in rows:
        d = registrable_domain(r["source_url"] or "")
        if d and d != registrable_domain(r["domain"] or ""):
            counts[d] = counts.get(d, 0) + 1
    return sorted(d for d, n in counts.items() if n >= min_flags)


def source_quality(store: Store) -> Dict[str, Any]:
    """Discovery: relevant/irrelevant by how the company was found. Research: flags by source domain."""
    disc = store.query("SELECT f.verdict, c.found_via FROM feedback f JOIN companies c ON c.id = f.company_id "
                       "WHERE f.kind = 'discovery'")
    by_src: Dict[str, Dict[str, int]] = {}
    for r in disc:
        k = (r["found_via"] or "unknown")[:120]
        by_src.setdefault(k, {"relevant": 0, "irrelevant": 0})[r["verdict"]] += 1
    res = store.query("SELECT f.verdict, e.source_url FROM feedback f LEFT JOIN evidence e ON e.id = f.evidence_id "
                      "WHERE f.kind = 'research'")
    by_dom: Dict[str, Dict[str, int]] = {}
    for r in res:
        d = registrable_domain(r["source_url"] or "") or "(no source)"
        by_dom.setdefault(d, {}).setdefault(r["verdict"], 0)
        by_dom[d][r["verdict"]] += 1
    return {"discovery": by_src, "research": by_dom, "demoted": demoted_domains(store)}


# ---------------------------------------------------------------- draft revisions + diff
CLAIM_RE = re.compile(r"\d|%|\$|€|£|₹|\b(SOC ?2|ISO ?27001|HIPAA|GDPR|PCI|Series [A-H]|raised|funding|customers?|"
                      r"clients?|certified|compliant|audit(ed)?|award|revenue|ARR|million|billion)\b", re.I)


def _paras(body: str) -> List[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text or "") if s.strip()]


_TONE_WORDS = {"hi", "hey", "hello", "dear", "best", "thanks", "regards", "cheers", "warmly"}


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def typo_only(before: str, after: str) -> bool:
    """Same words except small spelling fixes (<= 2 character edits per word); greetings/sign-offs are tone."""
    a, b = re.findall(r"\S+", before or ""), re.findall(r"\S+", after or "")
    if a == b or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        xs, ys = x.lower().strip(".,!?;:"), y.lower().strip(".,!?;:")
        if xs in _TONE_WORDS or ys in _TONE_WORDS or _lev(xs, ys) > 2:
            return False
        if set(re.sub(r"[^!?]", "", x)) != set(re.sub(r"[^!?]", "", y)):
            return False   # "." -> "!" or "?" changes tone even though only punctuation moved
    return True


def categorize(prev_subject: str, prev_body: str, subject: str, body: str,
               parts: Optional[Dict[str, str]] = None) -> List[str]:
    if (prev_subject or "").strip() == (subject or "").strip() and typo_only(prev_body, body):
        return ["typo"]
    cats = set()
    if (prev_subject or "").strip() != (subject or "").strip():
        cats.add("subject")
    parts = parts or {}
    old, new = _sentences(prev_body), _sentences(body)
    removed = [s for s in old if s not in new]
    added = [s for s in new if s not in old]

    def touches(part: str) -> bool:
        t = (parts.get(part) or "").strip()
        return bool(t) and any(t in s or s in t for s in removed)

    for part, cat in (("hook", "hook"), ("pain", "pain_point"), ("cta", "cta")):
        if touches(part):
            cats.add(cat)
    old_claims = {s for s in old if CLAIM_RE.search(s)}
    if any(CLAIM_RE.search(s) and s not in old_claims for s in added):
        cats.add("factual_claim")
    # remaining changes that keep most words are tone/wording
    leftover = [s for s in removed if not any((parts.get(p) or "\x00") in s or s in (parts.get(p) or "")
                                              for p in ("hook", "pain", "cta"))]
    if leftover or (added and not cats - {"subject"}):
        ratio = difflib.SequenceMatcher(None, " ".join(old), " ".join(new)).ratio()
        if ratio >= 0.5 or any(re.match(r"(Hi|Hey|Hello|Dear|Best|Thanks|Regards|Cheers)\b", s) for s in removed + added):
            cats.add("tone")
        elif "factual_claim" not in cats:
            cats.add("tone")
    return sorted(cats)


def text_diff(prev_subject: str, prev_body: str, subject: str, body: str) -> Dict[str, Any]:
    udiff = list(difflib.unified_diff((prev_body or "").splitlines(), (body or "").splitlines(),
                                      "previous", "this version", lineterm="", n=1))
    ratio = difflib.SequenceMatcher(None, prev_body or "", body or "").ratio()
    return {"subject": None if prev_subject == subject else {"from": prev_subject, "to": subject},
            "body_changed": (prev_body or "") != (body or ""), "similarity": round(ratio, 3),
            "change_ratio": round(1 - ratio, 3), "unified": udiff[:200]}


def revisions(store: Store, draft_id: str) -> List[Dict[str, Any]]:
    return store.query("SELECT * FROM draft_revisions WHERE draft_id = %s ORDER BY revision", (draft_id,))


def record_revision(store: Store, draft: Dict[str, Any], kind: str, subject: str, body: str, author: str,
                    reason: Optional[str] = None) -> str:
    """Append a revision. 'generated' is the first; edits carry a diff + categories vs the previous one.
    'approved'/'sent' snapshot the exact version (no new text)."""
    ensure_base_revision(store, draft)
    revs = revisions(store, draft["id"])
    prev = revs[-1] if revs else None
    parts = _parts(draft)
    diff = cats = None
    if prev is not None and kind == "edit":
        diff = text_diff(prev["subject"], prev["body"], subject, body)
        cats = categorize(prev["subject"], prev["body"], subject, body, parts)
    n = (revs[-1]["revision"] + 1) if revs else 1
    for _ in range(3):
        try:
            return store.insert("draft_revisions", {
                "draft_id": draft["id"], "revision": n, "kind": kind, "subject": subject, "body": body,
                "content_hash": content_hash(subject, body), "author": author or "operator",
                "reason": redact(reason or "")[:500] or None,
                "diff_json": json.dumps(diff) if diff else None, "categories_json": json.dumps(cats) if cats else None,
                "created_at": now_iso()})
        except Exception as e:
            if not is_unique_violation(e):
                raise
            n += 1   # a concurrent writer took this number; append after it
    raise RuntimeError("could not record draft revision")


def ensure_base_revision(store: Store, draft: Dict[str, Any]) -> None:
    """Drafts created before revision tracking get their generated version (and, if edited, the current
    text marked as a backfilled edit) so diffs still work. Idempotent."""
    if store.one("SELECT id FROM draft_revisions WHERE draft_id = %s LIMIT 1", (draft["id"],)):
        return
    try:
        store.insert("draft_revisions", {
            "draft_id": draft["id"], "revision": 1, "kind": "generated", "subject": draft["original_subject"],
            "body": draft["original_body"], "content_hash": content_hash(draft["original_subject"], draft["original_body"]),
            "author": "agent", "reason": None, "diff_json": None, "categories_json": None,
            "created_at": draft.get("created_at") or now_iso()})
    except Exception as e:
        if is_unique_violation(e):
            return
        raise
    if (draft["current_subject"], draft["current_body"]) != (draft["original_subject"], draft["original_body"]) \
            and int(draft.get("version") or 1) > 1:
        diff = text_diff(draft["original_subject"], draft["original_body"], draft["current_subject"], draft["current_body"])
        cats = categorize(draft["original_subject"], draft["original_body"], draft["current_subject"],
                          draft["current_body"], _parts(draft))
        store.insert("draft_revisions", {
            "draft_id": draft["id"], "revision": 2, "kind": "edit", "subject": draft["current_subject"],
            "body": draft["current_body"], "content_hash": draft["current_hash"], "author": "backfill",
            "reason": "edits made before revision tracking (combined)", "diff_json": json.dumps(diff),
            "categories_json": json.dumps(cats), "created_at": draft.get("updated_at") or now_iso()})


def _parts(draft: Dict[str, Any]) -> Dict[str, str]:
    try:
        return json.loads(draft.get("parts_json") or "{}") or {}
    except ValueError:
        return {}


def draft_change_summary(store: Store, draft: Dict[str, Any]) -> Dict[str, Any]:
    """First generated vs final (sent > approved > current) for reports."""
    revs = revisions(store, draft["id"])
    first = revs[0] if revs else {"subject": draft["original_subject"], "body": draft["original_body"]}
    final = {"subject": draft["current_subject"], "body": draft["current_body"]}
    d = text_diff(first["subject"], first["body"], final["subject"], final["body"])
    cats: set = set()
    for r in revs:
        if r.get("categories_json"):
            cats.update(json.loads(r["categories_json"]))
    return {"change_ratio": d["change_ratio"], "edited": d["body_changed"] or bool(d["subject"]),
            "categories": sorted(cats), "edits": sum(1 for r in revs if r["kind"] == "edit")}


# ---------------------------------------------------------------- unsupported claims (final send gate)
def unsupported_claims(store: Store, draft: Dict[str, Any]) -> List[str]:
    """Sentences with factual-claim markers that were not in the generated draft and whose key tokens do not
    appear in the company's verified evidence. Such a draft is never sent; an incident is raised instead."""
    generated = set(_sentences(draft["original_body"])) | set(_sentences(draft["original_subject"]))
    current = _sentences(draft["current_subject"]) + _sentences(draft["current_body"])
    ev = " ".join(r["quote"] + " " + r["value"] for r in store.query(
        "SELECT quote, value FROM evidence WHERE company_id = %s AND status = 'verified'", (draft["company_id"],))).lower()
    out = []
    for s in current:
        if s in generated or not CLAIM_RE.search(s):
            continue
        tokens = re.findall(r"\d[\d,.]*%?|SOC ?2|ISO ?27001|HIPAA|GDPR|PCI|Series [A-H]", s, re.I)
        if tokens and all(t.lower().replace(" ", "") in ev.replace(" ", "") for t in tokens):
            continue   # every concrete token is backed by verified evidence for this company
        out.append(s[:200])
    return out


# ---------------------------------------------------------------- style guidance (proposed -> operator decides)
def propose_style_guidance(store: Store, min_edits: int = 2) -> List[str]:
    """Wording-only rules from edit revisions, scoped by (POC role, company segment). Edits whose categories
    include a factual claim are excluded entirely. New rules start as 'proposed'."""
    rows = store.query("SELECT r.*, p.role_label, d.segment, d.original_body FROM draft_revisions r "
                       "JOIN drafts d ON d.id = r.draft_id JOIN pocs p ON p.id = d.poc_id WHERE r.kind = 'edit'")
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        cats = json.loads(r.get("categories_json") or "[]")
        if "factual_claim" in cats or cats == ["typo"]:
            continue   # never learn facts; typo fixes say nothing about style
        groups.setdefault((r["role_label"] or "*", r["segment"] or "*"), []).append(r)
    created = []
    from .drafting import CLOSING_EXTRA
    for (role, seg), edits in groups.items():
        if len(edits) < min_edits:
            continue
        rules = []
        shorter = [r for r in edits if len(r["body"]) < 0.85 * len(r["original_body"] or r["body"])]
        if len(shorter) * 2 >= len(edits):
            rules.append(("prefer_shorter", "reviewers shortened %d of %d edited drafts" % (len(shorter), len(edits)), shorter))
        dropped = [r for r in edits if CLOSING_EXTRA in (r["original_body"] or "") and CLOSING_EXTRA not in r["body"]]
        if len(dropped) >= min_edits:
            rules.append(("drop_closing_extra", "reviewers removed the extra closing line %d times" % len(dropped), dropped))
        greet: Dict[str, List[Dict[str, Any]]] = {}
        for r in edits:
            m = re.match(r"\s*(Hi|Hey|Hello|Dear)\b", r["body"])
            if m and m.group(1) != "Hi":
                greet.setdefault(m.group(1), []).append(r)
        for g, rs in greet.items():
            if len(rs) >= min_edits:
                rules.append(("greeting:" + g, "reviewers used '%s' in %d edits" % (g, len(rs)), rs))
        for rule, detail, src in rules:
            try:
                store.insert("style_guidance", {"scope_role": role, "scope_segment": seg, "rule": rule, "detail": detail,
                                                "evidence_json": json.dumps([r["id"] for r in src][:20]),
                                                "status": "proposed", "created_at": now_iso()})
                created.append(rule)
            except Exception as e:
                if not is_unique_violation(e):
                    raise
    return created


def decide_style_guidance(store: Store, guidance_id: str, accept: bool, by: str) -> None:
    store.execute("UPDATE style_guidance SET status = %s, decided_at = %s, decided_by = %s WHERE id = %s",
                  ("accepted" if accept else "dismissed", now_iso(), by, guidance_id))


def accepted_guidance(store: Store, role: Optional[str], segment: Optional[str]) -> List[Dict[str, Any]]:
    """Accepted rules matching this role/segment, converted to the composer's guidance format."""
    # oldest first, so for conflicting rules (e.g. two greetings) the most recently accepted one wins in compose()
    rows = store.query("SELECT * FROM style_guidance WHERE status = 'accepted' ORDER BY decided_at, created_at, id")
    out = []
    for r in rows:
        if r["scope_role"] not in ("*", role or "*") or r["scope_segment"] not in ("*", segment or "*"):
            continue
        rule = r["rule"]
        g: Dict[str, Any] = {"rule": rule, "detail": r["detail"], "review_ids": json.loads(r["evidence_json"] or "[]"),
                             "guidance_id": r["id"]}
        if rule.startswith("greeting:"):
            g["rule"], g["value"] = "greeting", rule.split(":", 1)[1]
        out.append(g)
    return out
