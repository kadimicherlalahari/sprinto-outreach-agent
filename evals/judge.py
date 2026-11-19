"""Soft-judgement rubric judge, deterministic and offline (no model calls), plus its calibration.

Rubric R1, evidence relevance: is this quote usable evidence for this criterion about THIS company?
  pass if: (a) the quote names the company or comes from its own domain, AND (b) it contains a keyword that
  directly supports the criterion, AND (c) it is not customer-framed for compliance criteria.
  Otherwise fail, with the first rule that failed as the reason.

Rubric R2, edit significance: is an edit a meaningful tone/wording change, or only a typo fix?
  "typo" if: same number of words and every changed word is within 2 character edits of the original word.
  "meaningful" otherwise (words added/removed, or a word replaced by a different word).

Calibration: evals/calibration.json holds labelled examples. calibrate() runs the judge on each one, reports
agreement per rubric and appends every disagreement to evals/out/judge_disagreements.jsonl.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent

KEYWORDS = {
    "is_b2b_saas": r"\b(saas|software|platform|b2b|businesses|teams)\b",
    "funding_stage": r"\b(series [a-e]|seed|raised|funding|round)\b",
    "enterprise_selling": r"\b(enterprise|sso|saml|audit logs?|fortune|regulated)\b",
    "compliance_in_progress": r"\b(soc ?2|iso ?27001|audit|certification)\b.*\b(in progress|underway|undergoing|working towards|pursuing|expected)\b|\b(undergoing|pursuing|working towards)\b.*\b(soc ?2|iso ?27001)\b",
    "compliance_hiring": r"\b(hiring|join|role|position|job)\b.*\b(security|compliance|grc)\b|\b(security|compliance|grc)\b.*\b(engineer|manager|lead|analyst)\b",
    "gdpr_exposure": r"\b(gdpr|europe|european|eu|uk)\b",
    "sensitive_data": r"\b(payments?|financial|health|patient|payroll|hr data|card)\b",
    "employee_count": r"\b\d{2,5}\s+(employees|people|staff|team members)\b",
    "certified": r"\b(soc ?2|iso ?27001)\b.*\b(certified|compliant|completed|achieved|in place|attested)\b",
}
CUSTOMER = re.compile(r"\b(help(s|ing)?|enable(s)?)\b[^.]{0,60}\b(customers?|clients?|you|teams|businesses)\b|\byour (soc ?2|compliance|audit)", re.I)


def judge_relevance(item: Dict[str, Any]) -> Dict[str, Any]:
    quote, company, domain, crit = item["quote"], item["company"], item["domain"], item["criterion"]
    own = item.get("source_domain", "") == domain
    names = own or re.search(r"\b%s\b" % re.escape(company), quote, re.I) is not None
    if not names:
        return {"label": "fail", "rule": "R1a", "reason": "quote neither names %s nor comes from its own site" % company}
    if not re.search(KEYWORDS.get(crit, r"$^"), quote, re.I):
        return {"label": "fail", "rule": "R1b", "reason": "no statement that directly supports %s" % crit}
    if crit in ("certified", "compliance_in_progress", "gdpr_exposure") and CUSTOMER.search(quote):
        return {"label": "fail", "rule": "R1c", "reason": "describes customers' compliance, not the company's own"}
    return {"label": "pass", "rule": "R1", "reason": "names the company and directly supports %s" % crit}


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def judge_edit(item: Dict[str, Any]) -> Dict[str, Any]:
    a, b = re.findall(r"\S+", item["before"]), re.findall(r"\S+", item["after"])
    if a == b:
        return {"label": "none", "rule": "R2", "reason": "no change"}
    if len(a) != len(b):
        return {"label": "meaningful", "rule": "R2", "reason": "words added or removed (%d -> %d)" % (len(a), len(b))}
    changed = [(x, y) for x, y in zip(a, b) if x != y]
    far = [(x, y) for x, y in changed if _lev(x.lower().strip(".,!?"), y.lower().strip(".,!?")) > 2]
    if far:
        return {"label": "meaningful", "rule": "R2", "reason": "word replaced: %r -> %r" % far[0]}
    return {"label": "typo", "rule": "R2", "reason": "%d word(s) changed by <= 2 characters each" % len(changed)}


RUBRICS = {"relevance": judge_relevance, "edit": judge_edit}


def calibrate(path: Path = ROOT / "calibration.json", out: Path = ROOT / "out") -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    log = out / "judge_disagreements.jsonl"
    total = agree = 0
    by: Dict[str, Dict[str, int]] = {}
    lines: List[str] = []
    for ex in data["examples"]:
        v = RUBRICS[ex["rubric"]](ex["input"])
        ok = v["label"] == ex["human_label"]
        total += 1
        agree += ok
        b = by.setdefault(ex["rubric"], {"total": 0, "agree": 0})
        b["total"] += 1
        b["agree"] += ok
        if not ok:
            lines.append(json.dumps({"id": ex["id"], "rubric": ex["rubric"], "human": ex["human_label"],
                                     "judge": v["label"], "judge_reason": v["reason"], "note": ex.get("note", "")}))
    log.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return {"total": total, "agree": agree, "by_rubric": by, "labelled_by": data.get("labelled_by", "unknown"),
            "disagreements": len(lines)}
