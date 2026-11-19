"""Stage 3a: draft a concise, personalised email.

Structure: one verified company hook (fact, cited) -> one role-relevant pain HYPOTHESIS (labeled as a
guess) -> short Sprinto value statement from config -> low-pressure question.
Facts come only from validated evidence rows. Review history may change WORDING only (length,
greeting, phrases reviewers removed) and every such change cites the review IDs it came from."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .llm import LLMUnavailable
from .util import get_logger, parse_date

log = get_logger(__name__)

HOOK_PRIORITY = ["funding_stage", "compliance_in_progress", "certified", "compliance_hiring", "enterprise_selling", "gdpr_exposure"]
STAGE_LABEL = {"series_a": "Series A", "series_b": "Series B", "series_c": "Series C", "seed": "seed"}
CLOSING_EXTRA = "Happy to share how that would look for your setup if useful."


@dataclass
class Draft:
    subject: str
    body: str
    hook_evidence_id: Optional[str]
    hook_text: str
    pain_points: List[str]
    guidance: List[Dict[str, Any]] = field(default_factory=list)
    composer: str = "template"
    parts: Dict[str, str] = field(default_factory=dict)   # hook / pain / value / cta sentences (for edit diffs)


def pick_hook(evidence_rows: List[Dict[str, Any]], recent_months_ok: Dict[str, bool]) -> Optional[Dict[str, Any]]:
    usable = [e for e in evidence_rows if e["status"] == "verified" and e["source_tier"] in ("1", "2")]
    for crit in HOOK_PRIORITY:
        rows = [e for e in usable if e["criterion"] == crit]
        if crit == "funding_stage":
            rows = [e for e in rows if e["value"] in STAGE_LABEL and recent_months_ok.get(e["id"])]
        if rows:
            rows.sort(key=lambda e: (e["source_tier"], -(parse_date(e.get("published_at")) or parse_date("1970-01-01")).timestamp()))
            return rows[0]
    return None


def hook_sentence(ev: Dict[str, Any], company: str) -> str:
    c, v = ev["criterion"], ev["value"]
    where = "your site" if ev["source_tier"] == "1" else "the coverage"
    if c == "funding_stage":
        return "Congratulations on the %s announcement for %s." % (STAGE_LABEL[v], company)
    if c == "compliance_in_progress":
        fw = {"soc2": "SOC 2", "iso27001": "ISO 27001"}.get(v, "a compliance certification")
        return "I noticed %s mentions %s is in progress for %s." % (where, fw, company)
    if c == "certified":
        fw = {"soc2": "SOC 2", "iso27001": "ISO 27001"}.get(v, "a compliance certification")
        return "I saw on %s that %s already has %s in place." % (where, company, fw)
    if c == "compliance_hiring":
        return "I noticed %s is hiring for security / compliance roles." % company
    if c == "enterprise_selling":
        if "sso_saml" in v:
            return "I noticed %s offers enterprise features like SSO on %s." % (company, where)
        if "enterprise_plan" in v:
            return "I noticed %s has an Enterprise plan." % company
        return "I noticed %s works with enterprise customers." % company
    if c == "gdpr_exposure":
        return "I noticed %s mentions GDPR / European customers on %s." % (company, where)
    return ""


def guidance_from_history(reviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Derive wording-only guidance from past review decisions. Each rule cites its review IDs."""
    edits = [r for r in reviews if r["decision"] == "edit"]
    rules: List[Dict[str, Any]] = []
    shorter = [r for r in edits if len(r["body"]) < 0.85 * len(r.get("original_body") or r["body"])]
    if len(edits) >= 2 and len(shorter) * 2 >= len(edits):
        rules.append({"rule": "prefer_shorter", "detail": "reviewers shortened most edited drafts",
                      "review_ids": [r["id"] for r in shorter][:10]})
    greet = Counter()
    for r in edits:
        m = re.match(r"\s*(Hi|Hey|Hello|Dear)\b", r["body"])
        if m:
            greet[m.group(1)] += 1
    if greet:
        g, n = greet.most_common(1)[0]
        if g != "Hi" and n >= 2:
            rules.append({"rule": "greeting", "value": g, "detail": "reviewers used '%s' in %d edits" % (g, n),
                          "review_ids": [r["id"] for r in edits if r["body"].lstrip().startswith(g)][:10]})
    removed = Counter()
    for r in edits:
        orig = r.get("original_body") or ""
        if CLOSING_EXTRA in orig and CLOSING_EXTRA not in r["body"]:
            removed[CLOSING_EXTRA] += 1
    if removed.get(CLOSING_EXTRA, 0) >= 2:
        rules.append({"rule": "drop_closing_extra", "detail": "reviewers removed the extra closing line",
                      "review_ids": [r["id"] for r in edits if CLOSING_EXTRA in (r.get("original_body") or "")][:10]})
    return rules


def compose(company: str, first_name: str, role_label: str, evidence_rows: List[Dict[str, Any]],
            recent_ok: Dict[str, bool], icp: Dict[str, Any], guidance: List[Dict[str, Any]],
            sender_name: str = "") -> Optional[Draft]:
    hook = pick_hook(evidence_rows, recent_ok)
    if hook is None:
        return None
    pains = (icp.get("pain_hypotheses", {}) or {}).get(role_label) or \
        list((icp.get("pain_hypotheses", {}) or {}).values())[0]
    has_soc2 = any(e["criterion"] == "certified" and e["value"] == "soc2" and e["status"] == "verified"
                   and e["source_tier"] == "1" for e in evidence_rows)
    if has_soc2 and icp.get("pain_hypotheses_when_certified"):
        pains = icp["pain_hypotheses_when_certified"]  # never imply a gap the evidence contradicts
    rules = {g["rule"]: g for g in guidance}
    greeting = rules.get("greeting", {}).get("value", "Hi")
    parts = {
        "hook": hook_sentence(hook, company),
        "pain": "A guess on my side, so correct me if I'm wrong: as %s grows, %s." % (company, pains[0]),
        "value": icp.get("value_statement", "").strip(),
        "cta": ("Is adding another framework like ISO 27001 on the roadmap for the next few months, or not a priority yet?"
                if has_soc2 else "Is getting audit-ready on your list for the next few months, or is it too early?"),
    }
    lines = ["%s %s," % (greeting, first_name), "", parts["hook"], "", parts["pain"], "", parts["value"], "", parts["cta"]]
    if "drop_closing_extra" not in rules and "prefer_shorter" not in rules:
        lines += ["", CLOSING_EXTRA]
    lines += ["", "Best,", sender_name or "[your name]"]
    subject = "%s and compliance" % company
    return Draft(subject=subject, body="\n".join(lines), hook_evidence_id=hook["id"],
                 hook_text=hook["quote"], pain_points=pains[:2], guidance=guidance, parts=parts)


REWRITE_PROMPT = """Rewrite this cold email to read naturally. Keep it under 120 words.
Hard rules: keep every fact exactly as given; add NO new facts, numbers, customer names, events or claims;
keep the guess clearly labeled as a guess; keep the greeting name; keep the final question low-pressure.
Return only the email body.

<draft>
{body}
</draft>"""


def llm_polish(llm: Any, draft: Draft) -> Draft:
    """Optional wording pass. Rejected (template kept) if it introduces numbers or drops the hook."""
    try:
        out = llm.generate(REWRITE_PROMPT.format(body=draft.body), json_mode=False, max_tokens=600).strip()
    except LLMUnavailable:
        return draft
    if not out:
        return draft
    nums_in = set(re.findall(r"\d+", draft.body))
    nums_out = set(re.findall(r"\d+", out))
    first_line = draft.body.splitlines()[0]
    key_terms = re.findall(r"Series [ABC]|SOC 2|ISO 27001|SSO|GDPR|Enterprise plan|security / compliance", draft.body)
    if (nums_out - nums_in) or first_line.split()[1] not in out or len(out.split()) > 140 or "@" in out \
            or any(t not in out for t in key_terms) or "guess" not in out.lower():
        return draft
    draft.body, draft.composer = "\n".join(l.rstrip() for l in out.splitlines()), "gemini"
    return draft
