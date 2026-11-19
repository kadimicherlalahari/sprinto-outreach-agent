"""Stage 2b: point-of-contact lookup and verification.

Primary: Hunter.io Domain Search API. Fallback: a contact the operator researched manually in Apollo.
Rules: never guess a name, title or email (no pattern-generated addresses); keep only personal
contacts whose email domain matches the company; verify the role on public pages (never LinkedIn
scraping); store the source and verification evidence."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import EMAIL_RE
from .evidence import mentions_company, quote_in_page, sentences, source_tier
from .fetch import FetchError, Page
from .search import SearchUnavailable
from .util import get_logger, host_matches, registrable_domain

log = get_logger(__name__)
HUNTER_URL = "https://api.hunter.io/v2/domain-search"
MIN_HUNTER_CONFIDENCE = 70


class ContactUnavailable(Exception):
    """kind: quota | outage | credentials"""

    def __init__(self, message: str = "contact lookup unavailable", kind: str = "outage"):
        super().__init__(message)
        self.kind = kind


@dataclass
class Contact:
    first_name: str
    last_name: str
    title: str
    email: Optional[str]
    source: str                         # hunter | manual_apollo
    confidence: Optional[int] = None
    source_detail: Dict[str, Any] = field(default_factory=dict)
    role_label: str = ""
    verification_status: str = "unverified"
    verification: Dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return ("%s %s" % (self.first_name, self.last_name)).strip()


class HunterClient:
    def __init__(self, api_key: str, timeout: float = 20.0):
        import httpx
        self._key = api_key
        self._client = httpx.Client(timeout=timeout)

    def domain_search(self, domain: str, limit: int = 10) -> List[Dict[str, Any]]:
        try:
            r = self._client.get(HUNTER_URL, params={"domain": domain, "type": "personal", "limit": limit,
                                                     "api_key": self._key})
        except Exception as e:
            raise ContactUnavailable("Hunter unreachable (%s)" % type(e).__name__, "outage")
        if r.status_code == 401:
            raise ContactUnavailable("Hunter rejected the API key (HTTP 401)", "credentials")
        if r.status_code in (403, 429):
            raise ContactUnavailable("Hunter rate limit / monthly quota reached (HTTP %d)" % r.status_code, "quota")
        if r.status_code != 200:
            raise ContactUnavailable("Hunter HTTP %d" % r.status_code, "outage")
        return (r.json().get("data") or {}).get("emails") or []


class NoHunter:
    def domain_search(self, domain: str, limit: int = 10) -> List[Dict[str, Any]]:
        raise ContactUnavailable("HUNTER_API_KEY not configured; enter a manually researched Apollo contact", "credentials")


class MockHunter:
    def __init__(self, data: Dict[str, List[Dict[str, Any]]]):
        self.data = data

    def domain_search(self, domain: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self.data.get(domain, [])


def role_for(title: str, icp: Dict[str, Any]) -> Tuple[Optional[str], int]:
    for i, role in enumerate(icp.get("poc_roles", []) or []):
        for pat in role.get("patterns", []):
            if re.search(pat, title or "", re.I):
                return role["label"], i
    return None, 99


def filter_hunter(emails: List[Dict[str, Any]], domain: str, icp: Dict[str, Any]) -> Tuple[List[Contact], List[str]]:
    """Return eligible contacts ranked by role priority then confidence, and reasons for exclusions."""
    keep: List[Tuple[int, int, Contact]] = []
    dropped: List[str] = []
    for e in emails:
        first, last = (e.get("first_name") or "").strip(), (e.get("last_name") or "").strip()
        value, title = (e.get("value") or "").strip(), (e.get("position") or "").strip()
        conf = int(e.get("confidence") or 0)
        if e.get("type") != "personal":
            dropped.append("generic mailbox excluded"); continue
        if not first or not last:
            dropped.append("missing full name (not guessed)"); continue
        if not title:
            dropped.append("%s: no title from source (not guessed)" % first); continue
        if not value or not EMAIL_RE.match(value) or not host_matches(value.split("@")[-1], domain):
            dropped.append("%s: email missing or off-domain" % first); continue
        if ((e.get("verification") or {}).get("status") or "").lower() in ("invalid", "disposable"):
            dropped.append("%s: email failed Hunter verification" % first); continue
        if conf < MIN_HUNTER_CONFIDENCE:
            dropped.append("%s: Hunter confidence %d < %d" % (first, conf, MIN_HUNTER_CONFIDENCE)); continue
        label, prio = role_for(title, icp)
        if label is None:
            dropped.append("%s: role '%s' not a target role" % (first, title)); continue
        srcs = [{"uri": s.get("uri"), "extracted_on": s.get("extracted_on"), "last_seen_on": s.get("last_seen_on"),
                 "still_on_page": s.get("still_on_page")} for s in (e.get("sources") or [])][:5]
        c = Contact(first, last, title, value, "hunter", conf,
                    {"hunter_sources": srcs, "hunter_verification": e.get("verification")}, role_label=label)
        keep.append((prio, -conf, c))
    keep.sort(key=lambda x: (x[0], x[1]))
    return [c for _, _, c in keep], dropped


def manual_contact(first: str, last: str, title: str, email: Optional[str], domain: str, icp: Dict[str, Any],
                   apollo_note: str = "") -> Contact:
    """Operator-entered Apollo contact. Validates format; does not invent missing fields."""
    first, last, title = first.strip(), last.strip(), title.strip()
    if not first or not last or not title:
        raise ValueError("first name, last name and title are required (they are never guessed)")
    if email:
        email = email.strip()
        if not EMAIL_RE.match(email) or not host_matches(email.split("@")[-1], domain):
            raise ValueError("email must be a valid address on %s" % domain)
    label, _ = role_for(title, icp)
    return Contact(first, last, title, email or None, "manual_apollo", None,
                   {"entered_by": "operator", "apollo_note": apollo_note[:300]}, role_label=label or "Other (operator chosen)")


_ROLE_WORDS = ["ceo", "cto", "cfo", "coo", "ciso", "cmo", "cro", "founder", "marketing", "sales", "designer", "design",
               "recruiter", "talent", "engineer", "engineering", "product", "finance", "operations", "security",
               "compliance", "people", "legal", "growth", "support", "partnerships", "intern"]


def _roles(text: str) -> set:
    t = text.lower().replace("co-founder", "founder")
    return {w for w in _ROLE_WORDS if re.search(r"\b%s\b" % w, t)}


def _status(hit: Dict[str, Any]) -> str:
    if hit["title_confirmed"]:
        return "verified"
    return "conflict" if hit.get("role_conflict") else "name_only"


def verify_contact(c: Contact, company_name: Optional[str], domain: str, pages: List[Page], fetcher: Any,
                   search: Any, sources: Dict[str, Any]) -> Contact:
    """Look for the person's name (and a title keyword) on the company's own pages, then on credible
    public pages found via search. LinkedIn is never fetched."""
    def check(page: Page) -> Optional[Dict[str, Any]]:
        if c.full_name.lower() not in page.text.lower():
            return None
        tier = source_tier(page.url, domain, sources)
        if tier == "lead":
            return None
        if tier != "1" and not mentions_company(page.text, company_name, domain):
            return None
        quote = next((s for s in sentences(page.text) if c.full_name.lower() in s.lower()), None)
        if not quote or not quote_in_page(quote, page.text):
            return None
        title_words = [w for w in re.findall(r"[a-z]{3,}", c.title.lower()) if w not in ("and", "the", "head")]
        role_match = bool(title_words) and any(w in quote.lower() for w in title_words)
        page_roles = _roles(quote)
        conflict = bool(page_roles) and not role_match and not (page_roles & _roles(c.title))
        return {"url": page.url, "tier": tier, "quote": quote[:400], "retrieved_at": page.retrieved_at,
                "published_at": page.published_at, "title_confirmed": role_match, "role_conflict": conflict,
                "page_roles": sorted(page_roles)}

    for p in pages:
        if host_matches(registrable_domain(p.url), domain):
            hit = check(p)
            if hit:
                c.verification = hit
                c.verification_status = _status(hit)
                return c
    try:
        hits = search.search('"%s" %s' % (c.full_name, company_name or domain), max_results=4,
                             include_domains=[domain] + list(sources.get("tiers", {}).get(2) or []))
    except SearchUnavailable as e:
        c.verification = {"note": "search unavailable: %s" % e}
        return c
    for h in hits:
        if "linkedin.com" in h.url:
            continue
        try:
            p = fetcher.get(h.url)
        except FetchError:
            continue
        hit = check(p)
        if hit:
            c.verification = hit
            c.verification_status = _status(hit)
            return c
    c.verification = c.verification or {"note": "name not found on company pages or credible public pages"}
    return c


def contact_to_row(c: Contact, company_id: str) -> Dict[str, Any]:
    from .store import dumps
    from .util import now_iso
    return {"company_id": company_id, "full_name": c.full_name, "title": c.title, "email": c.email,
            "role_label": c.role_label, "source": c.source,
            "source_detail_json": dumps(dict(c.source_detail, confidence=c.confidence)),
            "verification_status": c.verification_status, "verification_json": dumps(c.verification),
            "created_at": now_iso()}


def apply_preferences(emails: List[Dict[str, Any]], prefs: Dict[str, Any], icp: Dict[str, Any]):
    """Reviewer preferences for a POC re-run. Filters and re-orders what Hunter RETURNED; never invents people.
    - excluded_titles / departments / seniority: drop matching entries
    - preferred_titles: also eligible even if not an ICP role (as 'Operator-preferred'), ranked first
    Returns (emails, icp_with_preferred_roles)."""
    import copy
    excl = [t.lower() for t in prefs.get("excluded_titles") or []]
    pref = [t.lower() for t in prefs.get("preferred_titles") or []]
    depts = [d.lower() for d in prefs.get("departments") or []]
    senior = [x.lower() for x in prefs.get("seniority") or []]
    out = []
    for e in emails:
        title = (e.get("position") or "").lower()
        if excl and any(x in title for x in excl):
            continue
        if depts and (e.get("department") or "").lower() not in depts and not any(p in title for p in pref):
            continue
        if senior and (e.get("seniority") or "").lower() not in senior and not any(p in title for p in pref):
            continue
        out.append(e)
    icp2 = copy.deepcopy(icp)
    if pref:
        role = {"label": "Operator-preferred", "patterns": [r"(?i)" + re.escape(p) for p in pref]}
        icp2["poc_roles"] = [role] + list(icp2.get("poc_roles") or [])
        icp2.setdefault("pain_hypotheses", {})
        if "Operator-preferred" not in icp2["pain_hypotheses"]:
            first = list((icp.get("pain_hypotheses") or {}).values())
            if first:
                icp2["pain_hypotheses"]["Operator-preferred"] = first[0]
    return out, icp2
