"""Evidence model, rule-based extraction, and validation (quote/source/identity/own-compliance checks).

Everything retrieved is untrusted. A fact becomes evidence only if:
  * its quote appears verbatim (whitespace/quote-normalised) in the fetched page text,
  * the source is tier 1/2/3 (search "leads" are never evidence),
  * third-party quotes name the company (identity),
  * compliance claims are about the company itself, not its customers.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .fetch import Page
from .util import host_matches, normalize_ws, parse_date, redact_pii, registrable_domain, same_site

CRITERIA = ["is_b2b_saas", "funding_stage", "employee_count", "enterprise_selling", "compliance_in_progress",
            "certified", "compliance_hiring", "gdpr_exposure", "sensitive_data"]


@dataclass
class Evidence:
    criterion: str
    value: str
    quote: str
    source_url: str
    retrieved_at: str
    source_tier: str = "lead"
    published_at: Optional[str] = None
    status: str = "verified"          # verified | inferred | rejected
    extractor: str = "rules"
    note: str = ""
    source_type: str = "unlisted"     # company_site | job_board | publication | third_party_estimate | unlisted
    confidence: str = "low"           # high | medium | low
    flags: List[str] = field(default_factory=list)

    def to_row(self, company_id: str) -> Dict[str, Any]:
        import json
        d = asdict(self)
        d.pop("flags", None)
        d["flags_json"] = json.dumps(sorted(set(self.flags)))
        d["company_id"] = company_id
        return d


# Instruction-like text inside scraped pages is data, never instructions. Quotes containing it are rejected.
INJECTION = re.compile(
    r"\b(ignore|disregard|forget)\b[^.]{0,40}\b(previous|prior|above|all|earlier)\b[^.]{0,20}\b(instructions?|prompts?|rules)"
    r"|\b(system prompt|you are (now )?(an? )?(ai|assistant|language model|chatgpt|gemini|claude))\b"
    r"|\b(send|forward|email|cc|bcc)\b[^.]{0,40}\b(this|the|all)?\b[^.]{0,20}\b(email|draft|message|data)\b[^.]{0,30}\bto\b[^.]{0,40}@"
    r"|\b(mark|classify|score|rate)\b[^.]{0,30}\b(this company|us|it)\b[^.]{0,30}\b(as )?(qualified|approved|high priority)", re.I)


def looks_like_injection(text: str) -> bool:
    return bool(INJECTION.search(text or ""))


def similar_named_domains(page: Page, company_name: Optional[str], domain: str) -> List[str]:
    """Domains linked from a page whose name matches the company's but which are NOT its domain."""
    if not company_name:
        return []
    target = re.sub(r"[^a-z0-9]", "", company_name.lower())
    if len(target) < 3:
        return []
    out = []
    for l in page.links:
        host = registrable_domain(l)
        if not host or host_matches(host, domain) or host_matches(host, registrable_domain(page.url)):
            continue
        root = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
        if target in root or (len(root) >= 4 and root in target):
            out.append(host)
    return sorted(set(out))


# ---------------- source tiers ----------------
def source_tier(url: str, company_domain: str, sources_cfg: Dict[str, Any],
                own_urls: Optional[set] = None) -> str:
    host = registrable_domain(url)
    if host_matches(host, company_domain) or (own_urls and url in own_urls):
        return "1"  # company's own domain, or its job board linked from its own careers page
    tiers = (sources_cfg or {}).get("tiers", {}) or {}
    for tier in (2, 3):
        for d in tiers.get(tier, tiers.get(str(tier), [])) or []:
            if host_matches(host, d):
                return str(tier)
    return "lead"


# ---------------- own-compliance guard ----------------
_CUSTOMER_FRAMING = re.compile(
    r"\b(help(s|ing)?|enable(s)?|empower(s)?|let(s)?|allow(s)?)\b[^.]{0,60}\b(customers?|clients?|you|your|teams|businesses|companies|organi[sz]ations)\b"
    r"|\b(your|customers'?|clients'?)\s+(own\s+)?(soc ?2|iso|gdpr|hipaa|compliance|audit)"
    r"|\bfor your\b[^.]{0,40}\b(compliance|audit)"
    r"|\b(get|become|achieve|stay)\s+(soc ?2|iso ?27001|gdpr|hipaa|compliant)", re.I)
_OWN_FRAMING = re.compile(
    r"\b(we|we're|we are|we have|our|us)\b|\bis (soc ?2|iso)|\bhas (achieved|completed|obtained|received)", re.I)


def is_own_compliance_claim(quote: str, company_name: Optional[str] = None) -> bool:
    """True only if the quote asserts the company's OWN compliance, not its customers'."""
    q = quote
    if _CUSTOMER_FRAMING.search(q):
        return False
    if _OWN_FRAMING.search(q):
        return True
    if company_name and re.search(r"\b%s\b" % re.escape(company_name), q, re.I):
        return True
    return False


# ---------------- validation ----------------
def quote_in_page(quote: str, page_text: str) -> bool:
    q = normalize_ws(quote)
    return len(q) >= 12 and q in normalize_ws(page_text)


def mentions_company(text: str, company_name: Optional[str], domain: str) -> bool:
    t = text.lower()
    if registrable_domain(domain) in t:
        return True
    if company_name and len(company_name) >= 3:
        return re.search(r"\b%s\b" % re.escape(company_name.lower()), t) is not None
    return False


def validate(ev: Evidence, page: Page, company_name: Optional[str], domain: str,
             sources_cfg: Dict[str, Any], own_urls: Optional[set] = None) -> Evidence:
    ev.source_tier = source_tier(page.url, domain, sources_cfg, own_urls)
    ev.retrieved_at = page.retrieved_at
    if page.published_at and not ev.published_at:
        ev.published_at = page.published_at
    if not quote_in_page(ev.quote, page.text):
        ev.status, ev.note = "rejected", "quote not found in fetched page text"
        return ev
    if ev.source_tier == "lead":
        ev.status, ev.note = "rejected", "source is not on a credible tier; usable only as a lead"
        return ev
    ev.source_type = {"1": "job_board" if (own_urls and page.url in own_urls) else "company_site",
                       "2": "publication", "3": "third_party_estimate"}.get(ev.source_tier, "unlisted")
    if looks_like_injection(ev.quote):
        ev.status, ev.note = "rejected", "quote contains instruction-like text (possible prompt injection); treated as data"
        ev.flags.append("injection_suspected")
        return ev
    if ev.source_tier != "1" and not mentions_company(ev.quote, company_name, domain):
        ev.status, ev.note = "rejected", "third-party quote does not name the company (identity not established)"
        return ev
    if ev.source_tier != "1":
        links_us = any(same_site(l, domain) for l in page.links) or registrable_domain(domain) in page.text.lower()
        others = similar_named_domains(page, company_name, domain)
        if others and not links_us:
            ev.status = "rejected"
            ev.note = "refers to a similarly named company (%s), not %s" % (", ".join(others[:2]), domain)
            ev.flags.append("identity_mismatch")
            return ev
        if not links_us:
            ev.flags.append("identity_unconfirmed")
    if ev.criterion in ("certified", "compliance_in_progress", "gdpr_exposure") and not is_own_compliance_claim(ev.quote, company_name):
        ev.status, ev.note = "rejected", "describes customers' compliance, not the company's own"
        return ev
    if ev.criterion == "funding_stage" and not _stage_stated(ev.quote, ev.value):
        ev.status, ev.note = "rejected", "quote does not state this funding stage (no inference from e.g. accelerator batch)"
        return ev
    if ev.criterion == "sensitive_data" and (re.search(r"/blog/", page.url) or not (
            _OWN_FRAMING.search(ev.quote) or mentions_company(ev.quote, company_name, domain))):
        ev.status, ev.note = "rejected", "not a statement about the company's own product/data (blog or customer story)"
        return ev
    if ev.criterion == "employee_count" and not (_OWN_FRAMING.search(ev.quote) or mentions_company(ev.quote, company_name, domain)):
        ev.status, ev.note = "rejected", "headcount sentence is not clearly about this company"
        return ev
    if ev.source_tier == "3":
        ev.status, ev.note = "inferred", "third-party estimate (lower confidence)"
    if "identity_unconfirmed" in ev.flags and ev.status == "verified":
        ev.status, ev.note = "inferred", "names the company but does not link to its domain (identity not confirmed)"
    ev.confidence = ("high" if ev.status == "verified" and ev.source_tier in ("1", "2") else
                     "medium" if ev.status == "verified" else "low")
    clean = redact_pii(ev.quote)
    if clean != ev.quote:
        ev.quote = clean
        ev.flags.append("pii_redacted")
    return ev


def _stage_stated(quote: str, value: str) -> bool:
    q = quote.lower().replace("-", " ")
    if value == "seed":
        return "seed" in q
    if value == "public":
        return bool(re.search(r"\b(ipo|publicly traded|listed on|nasdaq|nyse)\b", q))
    if value.startswith("series_") and value != "series_d_plus":
        return ("series " + value[-1]) in q
    if value == "series_d_plus":
        return bool(re.search(r"series [d-h]\b", q))
    return False


def is_recent(published_at: Optional[str], months: int, now: Optional[datetime] = None) -> bool:
    d = parse_date(published_at)
    if d is None:
        return False  # no reliable date -> never treated as recent
    now = now or datetime.now(timezone.utc)
    return timedelta(0) <= now - d <= timedelta(days=int(months * 30.5)) or (d > now and d - now < timedelta(days=2))


# ---------------- company identity ----------------
def site_name(page: Optional[Page], domain: str) -> Optional[str]:
    """Company name from its own homepage: og:site_name, else the <title> part that matches the domain."""
    import html as _h
    if page is None or not page.html:
        return None
    m = re.search(r"<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"']{2,60})", page.html, re.I)
    if m:
        return _h.unescape(m.group(1)).strip()
    root = registrable_domain(domain).split(".")[0].lower()
    m = re.search(r"<title[^>]*>([^<]{2,160})</title>", page.html, re.I)
    if m:
        for part in re.split(r"\s[|\-\u2013\u2014:]\s", _h.unescape(m.group(1)).strip()):
            part = part.strip()
            if part and root in re.sub(r"[^a-z0-9]", "", part.lower()) and len(part) <= 40:
                return part
    return None



def identity_check(company_name: Optional[str], domain: str, homepage: Optional[Page],
                   lead: Optional[Page] = None) -> Tuple[str, str]:
    """Is `domain` really the company named in the lead? -> (verified|inferred|failed, reason)."""
    if homepage is None:
        return "failed", "homepage could not be fetched"
    if not same_site(homepage.url, domain):
        return "failed", "homepage redirected off-domain to %s" % registrable_domain(homepage.url)
    if not company_name:
        return "inferred", "no company name from lead; domain supplied directly"
    head = (homepage.html[:5000] if homepage.html else "") + " " + homepage.text[:3000]
    name_on_site = re.search(r"\b%s\b" % re.escape(company_name), head, re.I) is not None
    lead_links = lead is not None and any(same_site(l, domain) for l in lead.links)
    if name_on_site and (lead is None or lead_links):
        return "verified", "name appears on homepage" + ("; lead article links to domain" if lead_links else "")
    if name_on_site:
        return "inferred", "name appears on homepage but lead article does not link to this domain"
    return "failed", "company name '%s' not found on %s homepage" % (company_name, domain)


# ---------------- rule-based extraction ----------------
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")

_RULES: List[Tuple[str, str, re.Pattern]] = [
    ("is_b2b_saas", "true", re.compile(r"\b(saas|software[- ]as[- ]a[- ]service|cloud[- ]based platform|b2b|api platform|platform for (teams|businesses|companies|enterprises|organi[sz]ations)|book a demo|request a demo)\b", re.I)),
    ("enterprise_selling", "enterprise_plan", re.compile(r"\benterprise (plan|tier|edition|pricing)\b|\bcontact sales\b.{0,40}\benterprise\b", re.I)),
    ("enterprise_selling", "sso_saml", re.compile(r"\b(saml|sso|single sign-on|scim|audit logs?)\b", re.I)),
    ("enterprise_selling", "enterprise_customers", re.compile(r"\b(fortune 500|enterprise customers|enterprises like|trusted by .{0,40}(banks|enterprises))\b", re.I)),
    ("compliance_in_progress", "soc2", re.compile(r"\bsoc ?2\b.{0,60}\b(in progress|underway|working towards?|pursuing|undergoing|audit (is )?scheduled|expected)\b|\b(working towards?|pursuing|undergoing|preparing for)\b.{0,40}\bsoc ?2\b", re.I)),
    ("compliance_in_progress", "iso27001", re.compile(r"\biso ?27001\b.{0,60}\b(in progress|underway|working towards?|pursuing|undergoing)\b|\b(working towards?|pursuing|undergoing|preparing for)\b.{0,40}\biso ?27001\b", re.I)),
    ("certified", "soc2", re.compile(r"\bsoc ?2\b.{0,40}\b(type (ii|2|i|1) )?(certified|compliant|attested|report available|audited)\b|\b(completed|achieved|passed|obtained)\b.{0,30}\bsoc ?2\b", re.I)),
    ("certified", "iso27001", re.compile(r"\biso ?27001\b.{0,30}\b(certified|certification achieved|compliant)\b|\b(achieved|obtained|completed)\b.{0,30}\biso ?27001\b", re.I)),
    ("gdpr_exposure", "eu_presence", re.compile(r"\b(gdpr|european union|eu customers|uk customers|expan\w+ (in)?to (europe|the uk|the eu))\b", re.I)),
    ("sensitive_data", "regulated_data", re.compile(r"\b(payments?|fintech|lending|payroll|health ?care|patient|phi|hipaa|banking|insurance|kyc)\b", re.I)),
]
_ROLE_PAGE_HIRING = re.compile(r"\b(security engineer|grc|compliance (manager|analyst|lead|officer)|information security|ciso|head of security|security (lead|analyst))\b", re.I)
_FUNDING = re.compile(r"\b(raise[sd]?|raising|secure[sd]?|closes?|closed|announce[sd]?|bags?|lands?)\b(?:(?!\b(?:after|following|previous|prior|earlier|follows|since)\b).){0,80}?\b(pre-?seed|seed|series ([a-h])|ipo)\b"
                      r"|\b(pre-?seed|seed|series ([a-h]))\b (round|funding|financing)", re.I)
_EMPLOYEES = re.compile(r"(?<![\d,.])\b(team of|over|more than|nearly|around)?\s*(\d{2,5})(?![\d,.]\d)\+?\s+(employees|people|team members|teammates)\b", re.I)


def funding_value(stage_text: str) -> str:
    s = stage_text.lower().replace("-", "")
    if "ipo" in s:
        return "public"
    if "seed" in s:
        return "seed"
    m = re.search(r"series ([a-h])", s)
    if not m:
        return "unknown"
    letter = m.group(1)
    return {"a": "series_a", "b": "series_b", "c": "series_c"}.get(letter, "series_d_plus")


def sentences(text: str) -> List[str]:
    out = []
    for s in [x for block in text.split("\n") for x in _SENT.split(block)]:
        s = s.strip()
        if 30 <= len(s) <= 400 and len(s.split()) >= 5:
            out.append(s)
        elif len(s) > 400:
            out.extend(c.strip() for c in re.split(r"(?<=[;:])\s+|\s{2,}| \| ", s) if 30 <= len(c.strip()) <= 400 and len(c.split()) >= 5)
    return out


def rule_extract(page: Page, page_kind: str = "") -> List[Evidence]:
    out: List[Evidence] = []
    seen = set()

    def add(crit: str, val: str, quote: str) -> None:
        key = (crit, val)
        if key in seen:
            return
        seen.add(key)
        out.append(Evidence(criterion=crit, value=val, quote=quote, source_url=page.url,
                            retrieved_at=page.retrieved_at, published_at=page.published_at, extractor="rules"))

    for s in sentences(page.text):
        for crit, val, rx in _RULES:
            if rx.search(s):
                add(crit, val, s)
        m = _FUNDING.search(s)
        if m and re.search(r"\b(after|following|previous|prior|earlier|since)\b[^.]{0,40}$", s[:m.start(2) if m.group(2) else m.start(4)], re.I):
            m = None  # mentions an earlier round, not the current one
        if m:
            stage = m.group(2) or m.group(4) or ""
            add("funding_stage", funding_value(stage), s)
        m = _EMPLOYEES.search(s)
        if m and page_kind in ("about", "home", "careers", "press"):
            add("employee_count", m.group(2), s)
        if page_kind == "careers" and _ROLE_PAGE_HIRING.search(s):
            add("compliance_hiring", "security_or_grc_role", s)
    # Structured data (JSON-LD) used only as a supplement; validated against page text by `validate`.
    for block in page.structured:
        if isinstance(block, dict) and block.get("@type") == "Organization":
            n = block.get("numberOfEmployees")
            if isinstance(n, dict):
                n = n.get("value")
            if n and str(n).isdigit():
                q = next((s for s in sentences(page.text) if str(n) in s), None)
                if q:
                    out.append(Evidence("employee_count", str(n), q, page.url, page.retrieved_at,
                                        published_at=page.published_at, extractor="structured_data"))
    return out
