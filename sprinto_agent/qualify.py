"""ICP assessment, scoring, thresholds and stop rules — all deterministic code.

Each criterion ends up as one of:
  verified  - supported by validated tier-1/2 evidence
  inferred  - supported only by tier-3 / lower-confidence evidence
  not_found - the relevant pages WERE fetched and contain no supporting statement
  unknown   - could not check (no relevant page, or fetches failed/blocked/JS-only); never proof of absence
  conflict  - credible sources disagree
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .evidence import Evidence, is_recent
from .util import parse_date, registrable_domain

TRIGGERS_FROM_EVIDENCE = ["enterprise_selling", "compliance_in_progress", "compliance_hiring",
                          "gdpr_exposure", "sensitive_data"]


@dataclass
class CriterionResult:
    status: str                       # verified | inferred | not_found | unknown | conflict
    value: Optional[str] = None
    evidence: List[Evidence] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "value": self.value, "note": self.note,
                "sources": sorted({e.source_url for e in self.evidence})}


@dataclass
class Assessment:
    criteria: Dict[str, CriterionResult]
    score: int
    max_possible: int
    min_possible: int
    decision: str                     # qualified | rejected | uncertain
    reasons: List[str]
    open_questions: List[str]         # criteria whose resolution could change the decision

    @property
    def could_change(self) -> bool:
        """Could unresolved evidence still change the decision?"""
        return self.decision != "rejected" and bool(self.open_questions)

    def to_dict(self) -> Dict[str, Any]:
        return {"score": self.score, "max_possible": self.max_possible, "min_possible": self.min_possible,
                "decision": self.decision, "reasons": self.reasons, "open_questions": self.open_questions,
                "could_change": self.could_change,
                "criteria": {k: v.to_dict() for k, v in self.criteria.items()}}


def _usable(evs: List[Evidence], crit: str) -> List[Evidence]:
    return [e for e in evs if e.criterion == crit and e.status in ("verified", "inferred")]


def _absent(crit: str, coverage: Optional[Dict[str, str]]) -> CriterionResult:
    """No usable evidence: 'not_found' only if relevant pages were fetched and none failed; else 'unknown'."""
    c = (coverage or {}).get(crit, "")
    if c == "checked":
        return CriterionResult("not_found", None, [], "relevant pages fetched; no supporting statement found")
    if c.startswith("failed"):
        return CriterionResult("unknown", None, [], "could not check: %s" % c[7:] if len(c) > 7 else "could not check")
    return CriterionResult("unknown")


def _single_valued(evs: List[Evidence], crit: str, coverage: Optional[Dict[str, str]] = None) -> CriterionResult:
    items = _usable(evs, crit)
    if not items:
        return _absent(crit, coverage)
    strong = [e for e in items if e.status == "verified"] or items
    values = {e.value for e in strong}
    if len(values) == 1:
        v = values.pop()
        return CriterionResult("verified" if strong[0].status == "verified" else "inferred", v, strong)
    # Different values. For dated funding rounds, the latest dated round supersedes older ones.
    if crit == "funding_stage":
        dated = [(parse_date(e.published_at), e) for e in strong]
        if all(d for d, _ in dated):
            dated.sort(key=lambda x: x[0])
            latest_date = dated[-1][0]
            latest = [e for d, e in dated if d == latest_date]
            if len({e.value for e in latest}) == 1:
                return CriterionResult("verified" if latest[0].status == "verified" else "inferred",
                                       latest[0].value, strong, "latest dated round supersedes earlier rounds")
        # Undated: rounds only move forward, so the highest stage reported for this company is the current one.
        order = ["seed", "series_a", "series_b", "series_c", "series_d_plus", "public"]
        known = [e for e in strong if e.value in order]
        if known and len(known) == len(strong):
            top = max(known, key=lambda e: order.index(e.value))
            if top.source_tier == "1":  # the company's own announcement of its latest round is authoritative
                return CriterionResult("verified" if top.status == "verified" else "inferred", top.value, strong,
                                       "highest stage, stated by the company itself (earlier rounds also mentioned)")
    if crit == "employee_count":
        nums = sorted(int(e.value) for e in strong if e.value.isdigit())
        tier1 = [e for e in strong if e.source_tier == "1"]
        if tier1 and len({e.value for e in tier1}) == 1:
            return CriterionResult("verified", tier1[0].value, strong, "company-published figure preferred")
        if nums and nums[-1] <= nums[0] * 1.5:
            return CriterionResult("inferred", str(nums[-1]), strong, "figures roughly agree")
    return CriterionResult("conflict", None, strong, "sources disagree: %s" % sorted(values))


def _multi_valued(evs: List[Evidence], crit: str, coverage: Optional[Dict[str, str]] = None) -> CriterionResult:
    items = _usable(evs, crit)
    if not items:
        return _absent(crit, coverage)
    ver = [e for e in items if e.status == "verified"]
    use = ver or items
    return CriterionResult("verified" if ver else "inferred", ",".join(sorted({e.value for e in use})), use)


def is_competitor(name: Optional[str], domain: str, icp: Dict[str, Any]) -> bool:
    names = [n.lower() for n in (icp.get("negatives", {}).get("competitor", {}).get("names") or [])]
    root = registrable_domain(domain).split(".")[0]
    return root in names or bool(name and name.strip().lower() in names)


def assess(evidence: List[Evidence], icp: Dict[str, Any], name: Optional[str], domain: str,
           now: Optional[datetime] = None, coverage: Optional[Dict[str, str]] = None) -> Assessment:
    crit: Dict[str, CriterionResult] = {}
    reasons: List[str] = []
    crit["is_b2b_saas"] = _single_valued(evidence, "is_b2b_saas", coverage)
    crit["funding_stage"] = _single_valued(evidence, "funding_stage", coverage)
    crit["employee_count"] = _single_valued(evidence, "employee_count", coverage)
    for t in TRIGGERS_FROM_EVIDENCE:
        crit[t] = _multi_valued(evidence, t, coverage)
    crit["certified"] = _multi_valued(evidence, "certified", coverage)
    # Certification conflict: one source says a framework is held, another that it is still in progress.
    held = set((crit["certified"].value or "").split(",")) if crit["certified"].status in ("verified", "inferred") else set()
    prog = set((crit["compliance_in_progress"].value or "").split(",")) if crit["compliance_in_progress"].status in ("verified", "inferred") else set()
    both = sorted((held & prog) - {""})
    if both:
        note = "sources disagree on %s: certified vs in progress" % ", ".join(both)
        crit["certified"] = CriterionResult("conflict", None, crit["certified"].evidence, note)
        crit["compliance_in_progress"] = CriterionResult("conflict", None, crit["compliance_in_progress"].evidence, note)

    months = int(icp.get("recent_funding_months", 12))
    fund_ev = [e for e in crit["funding_stage"].evidence]
    if not fund_ev:
        crit["recent_funding"] = CriterionResult("unknown")
    else:
        recent = [e for e in fund_ev if is_recent(e.published_at, months, now)]
        if recent:
            crit["recent_funding"] = CriterionResult("verified" if any(e.status == "verified" for e in recent) else "inferred",
                                                     recent[0].published_at, recent)
        elif any(parse_date(e.published_at) for e in fund_ev):
            crit["recent_funding"] = CriterionResult("verified", "no", fund_ev, "dated, but outside %d-month window" % months)
        else:
            crit["recent_funding"] = CriterionResult("unknown", None, fund_ev, "funding found but undated; not treated as recent")

    scoring = icp.get("scoring", {})
    base = int(scoring.get("base_for_must_haves", 30))
    threshold = int(scoring.get("qualify_threshold", 60))
    triggers_cfg = icp.get("triggers", {})
    must = icp.get("must_have", {})
    fs_cfg = must.get("funding_stage", {})

    # ---- must-haves ----
    disqualified = False
    saas = crit["is_b2b_saas"]
    fs = crit["funding_stage"]
    must_ok = 0
    must_unknown = 0
    if saas.status in ("verified",) and saas.value == "true":
        must_ok += 1
    elif saas.status == "verified" and saas.value == "false":
        disqualified = True
        reasons.append("not B2B SaaS")
    elif saas.status in ("unknown", "conflict", "inferred", "not_found"):
        must_unknown += 1
    if fs.status == "verified" and fs.value in fs_cfg.get("allowed", []):
        must_ok += 1
    elif fs.status == "verified" and fs.value in fs_cfg.get("rejected", []):
        disqualified = True
        reasons.append("funding stage %s is outside ICP" % fs.value)
    else:
        must_unknown += 1
        if fs.status == "verified" and fs.value in fs_cfg.get("uncertain", []):
            reasons.append("funding stage %s is borderline" % fs.value)
    if is_competitor(name, domain, icp):
        disqualified = True
        reasons.append("compliance-automation vendor / competitor")
    # already certified (we would have nothing to sell) -> disqualified, only on VERIFIED evidence
    ac = icp.get("negatives", {}).get("already_certified", {}) or {}
    if ac.get("disqualify") and crit["certified"].status == "verified":
        held_fw = set((crit["certified"].value or "").split(",")) & set(ac.get("frameworks") or [])
        if held_fw:
            disqualified = True
            reasons.append("already certified (%s)" % ", ".join(sorted(held_fw)))
    # company size: soft band; only a VERIFIED size far outside the soft range rejects
    er = icp.get("employee_range", {}) or {}
    ec = crit["employee_count"]
    if ec.status in ("verified", "inferred") and ec.value and ec.value.isdigit() and er.get("band_min") is not None:
        n = int(ec.value)
        if int(er["band_min"]) <= n <= int(er["band_max"]):
            pass
        elif int(er.get("soft_min", er["band_min"])) <= n <= int(er.get("soft_max", er["band_max"])):
            reasons.append("size %d is near the %s-%s band (accepted: soft boundary)" % (n, er["band_min"], er["band_max"]))
        elif ec.status == "verified":
            disqualified = True
            reasons.append("size %d is far outside the %s-%s band" % (n, er["band_min"], er["band_max"]))

    score = base * must_ok // 2
    pot = base * must_unknown // 2
    verified_triggers = 0
    unknown_triggers: List[str] = []
    inferred_triggers: List[str] = []
    for t, cfg in triggers_cfg.items():
        w = int(cfg.get("weight", 0))
        r = crit.get(t)
        if r is None:
            continue
        if t == "recent_funding" and r.value == "no":
            continue
        if r.status == "verified":
            score += w
            verified_triggers += 1
        elif r.status == "inferred":
            score += w // 2
            pot += w - w // 2
            inferred_triggers.append(t)   # a stronger source could still verify it
        elif r.status == "not_found":
            continue  # checked and absent: cannot add points unless new sources appear
        else:
            pot += w
            unknown_triggers.append(t)

    # ---- negatives (certification and size are handled above as disqualifiers) ----
    neg_unknown = 0

    max_possible = score + pot
    min_possible = score + neg_unknown
    min_triggers = int(icp.get("min_triggers", 1))

    open_q: List[str] = []
    if disqualified:
        decision = "rejected"
    elif must_ok == 2 and verified_triggers >= min_triggers and score >= threshold:
        decision = "qualified"
        reasons.append("both must-haves verified; %d verified trigger(s); score %d >= %d" % (verified_triggers, score, threshold))
        # still-unknown certification or size could disqualify, so they stay open questions
        open_q = [k for k in ("certified", "employee_count") if crit[k].status in ("unknown", "conflict")]
    elif max_possible < threshold:
        decision = "rejected"
        reasons.append("best-case score %d cannot reach threshold %d" % (max_possible, threshold))
    else:
        decision = "uncertain"
        if must_ok < 2:
            reasons.append("must-have(s) not yet verified")
        if verified_triggers < min_triggers:
            reasons.append("no verified trigger signal yet")
        open_q = [k for k in ("is_b2b_saas", "funding_stage") if crit[k].status != "verified"] + unknown_triggers
        if verified_triggers < min_triggers:
            open_q += [t for t in inferred_triggers if t not in open_q]
        open_q += [k for k, v in crit.items() if v.status == "conflict" and k not in open_q]
    return Assessment(crit, score, max_possible, min_possible, decision, reasons, open_q)


@dataclass
class Budget:
    """Per-company research limits. `clock` is injectable so tests can simulate time passing."""
    max_pages: int
    max_rounds: int
    max_searches: int = 3
    max_llm_calls: int = 12
    max_seconds: float = 180.0
    pages_used: int = 0
    rounds_used: int = 0
    searches_used: int = 0
    llm_used: int = 0
    started: float = field(default_factory=lambda: __import__("time").monotonic())
    clock: Any = None
    cost_guard: Any = None     # costs.Meter; once it trips, no further research for this company

    def elapsed(self) -> float:
        now = self.clock() if self.clock else __import__("time").monotonic()
        return now - self.started

    def cost_stopped(self) -> bool:
        return bool(self.cost_guard is not None and getattr(self.cost_guard, "tripped", None))

    def exhausted_by(self) -> Optional[str]:
        if self.cost_stopped():
            return "cost_cap_reached"
        if self.pages_used >= self.max_pages:
            return "budget_pages"
        if self.rounds_used >= self.max_rounds:
            return "budget_rounds"
        if self.elapsed() >= self.max_seconds:
            return "budget_time"
        return None

    @property
    def exhausted(self) -> bool:
        return self.exhausted_by() is not None

    def can_search(self) -> bool:
        return not self.cost_stopped() and self.searches_used < self.max_searches and self.elapsed() < self.max_seconds

    def can_call_model(self) -> bool:
        return not self.cost_stopped() and self.llm_used < self.max_llm_calls and self.elapsed() < self.max_seconds

    def to_dict(self) -> Dict[str, Any]:
        return {"pages": [self.pages_used, self.max_pages], "rounds": [self.rounds_used, self.max_rounds],
                "searches": [self.searches_used, self.max_searches], "model_calls": [self.llm_used, self.max_llm_calls],
                "seconds": [round(self.elapsed(), 1), self.max_seconds]}


def stop_decision(a: Assessment, budget: Budget, sources_remaining: bool) -> Tuple[bool, str, str]:
    """Return (continue_research, human reason, stop code)."""
    if a.decision == "rejected":
        return False, "stopped: decision is rejected (%s)" % "; ".join(a.reasons), "rejected"
    if not a.open_questions:
        return False, "stopped: decision '%s' cannot change with more evidence" % a.decision, "decision_stable"
    code = budget.exhausted_by()
    if code:
        if code == "cost_cap_reached":
            return False, "stopped: cost cap reached (%s); known findings kept; unresolved: %s" % (
                getattr(budget.cost_guard, "trip_message", "") or "cost limit", ", ".join(a.open_questions)), code
        what = {"budget_pages": "%d pages" % budget.pages_used, "budget_rounds": "%d rounds" % budget.rounds_used,
                "budget_time": "%ds time limit" % int(budget.max_seconds)}[code]
        return False, "stopped: research budget exhausted (%s); unresolved and could change the result: %s" % (
            what, ", ".join(a.open_questions)), code
    if not sources_remaining:
        return False, "stopped: no further sources to try; unresolved and could change the result: %s" % ", ".join(
            a.open_questions), "no_sources"
    return True, "continue: evidence on %s could change the decision" % ", ".join(a.open_questions), "continue"
