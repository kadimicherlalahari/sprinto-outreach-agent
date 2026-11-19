from datetime import datetime, timedelta, timezone

from sprinto_agent.evidence import (Evidence, identity_check, is_own_compliance_claim, is_recent, quote_in_page,
                                    validate)
from sprinto_agent.fetch import html_to_page
from sprinto_agent.qualify import Budget, assess, stop_decision

SOURCES = {"tiers": {2: ["news.example"], 3: ["estimates.example"]}}
NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)


def ev(crit, val, tier="1", status="verified", published=None, quote="q"):
    return Evidence(crit, val, quote, "https://x.example/", "2026-11-18", source_tier=tier, status=status,
                    published_at=published)


def page(url, body, published=None):
    meta = '<meta property="article:published_time" content="%s">' % published if published else ""
    return html_to_page(url, 200, "<html><head>%s</head><body>%s</body></html>" % (meta, body))


# ---------- quote / source validation ----------
def test_quote_must_appear_in_page():
    p = page("https://acme.example/", "<p>Acme is a SaaS platform for payroll teams.</p>")
    good = validate(Evidence("is_b2b_saas", "true", "Acme is a SaaS platform for payroll teams.", p.url, ""), p, "Acme", "acme.example", SOURCES)
    bad = validate(Evidence("is_b2b_saas", "true", "Acme is a leading SaaS platform for banks.", p.url, ""), p, "Acme", "acme.example", SOURCES)
    assert good.status == "verified" and good.source_tier == "1"
    assert bad.status == "rejected" and "not found" in bad.note


def test_quote_matching_tolerates_whitespace_and_smart_quotes():
    assert quote_in_page("We're  SOC 2 compliant today", "Hello. We’re SOC 2\ncompliant today. Bye")


def test_unlisted_source_is_lead_only():
    p = page("https://randomblog.example/post", "<p>Acme raised a Series B round last month.</p>")
    r = validate(Evidence("funding_stage", "series_b", "Acme raised a Series B round last month.", p.url, ""), p, "Acme", "acme.example", SOURCES)
    assert r.status == "rejected" and "lead" in r.note


def test_third_party_quote_must_name_company():
    p = page("https://news.example/a", "<p>The startup raised a Series A round from investors.</p>")
    r = validate(Evidence("funding_stage", "series_a", "The startup raised a Series A round from investors.", p.url, ""), p, "Acme", "acme.example", SOURCES)
    assert r.status == "rejected" and "identity" in r.note


def test_tier3_is_only_inferred():
    p = page("https://estimates.example/acme", "<p>Acme has 85 employees according to estimates.</p>")
    r = validate(Evidence("employee_count", "85", "Acme has 85 employees according to estimates.", p.url, ""), p, "Acme", "acme.example", SOURCES)
    assert r.status == "inferred"


# ---------- own vs customers' compliance ----------
def test_customer_compliance_is_not_own_compliance():
    assert not is_own_compliance_claim("We help healthcare customers achieve HIPAA compliance and get SOC 2 ready.")
    assert not is_own_compliance_claim("Our platform helps customers stay GDPR compliant.")
    assert not is_own_compliance_claim("Get SOC 2 compliant in weeks with our product.")
    assert is_own_compliance_claim("We are currently undergoing our SOC 2 Type II audit.")
    assert is_own_compliance_claim("Acme is SOC 2 Type II certified.", "Acme")


def test_customer_compliance_quote_rejected_in_validation():
    q = "We help customers achieve SOC 2 compliance in weeks."
    p = page("https://acme.example/", "<p>%s</p>" % q)
    r = validate(Evidence("certified", "soc2", q, p.url, ""), p, "Acme", "acme.example", SOURCES)
    assert r.status == "rejected" and "customers" in r.note


# ---------- dates ----------
def test_undated_funding_is_never_recent():
    assert not is_recent(None, 12, NOW)
    assert not is_recent("sometime last year", 12, NOW)
    assert is_recent("2026-09-01", 12, NOW)
    assert not is_recent("2024-01-01", 12, NOW)


# ---------- identity ----------
def test_identity_check():
    home = page("https://acme.example/", "<h1>Acme</h1><p>Acme builds payroll software.</p>")
    lead = page("https://news.example/a", '<p>Acme raises.</p><a href="https://acme.example/">site</a>')
    assert identity_check("Acme", "acme.example", home, lead)[0] == "verified"
    other = page("https://acme.example/", "<h1>Totally Different Co</h1>")
    assert identity_check("Acme", "acme.example", other, lead)[0] == "failed"
    moved = page("https://elsewhere.example/", "<h1>Acme</h1>")
    assert identity_check("Acme", "acme.example", moved, lead)[0] == "failed"
    assert identity_check("Acme", "acme.example", None, lead)[0] == "failed"


# ---------- scoring / unknown / conflict ----------
def test_qualified_with_verified_evidence(icp):
    evs = [ev("is_b2b_saas", "true"), ev("funding_stage", "series_b", tier="2", published="2026-10-01"),
           ev("compliance_in_progress", "soc2")]
    a = assess(evs, icp, "Acme", "acme.example", NOW)
    assert a.decision == "qualified"
    assert a.criteria["recent_funding"].status == "verified"


def test_unknowns_make_uncertain_not_rejected(icp):
    a = assess([ev("is_b2b_saas", "true")], icp, "Acme", "acme.example", NOW)
    assert a.decision == "uncertain"
    assert a.criteria["funding_stage"].status == "unknown"
    assert "funding_stage" in a.open_questions


def test_conflicting_undated_funding_is_conflict(icp):
    evs = [ev("is_b2b_saas", "true"), ev("funding_stage", "series_a", tier="2"), ev("funding_stage", "series_c", tier="2"),
           ev("compliance_in_progress", "soc2")]
    a = assess(evs, icp, "Acme", "acme.example", NOW)
    assert a.criteria["funding_stage"].status == "conflict"
    assert a.decision == "uncertain"
    assert "funding_stage" in a.open_questions


def test_later_dated_round_supersedes_earlier(icp):
    evs = [ev("funding_stage", "series_a", tier="2", published="2024-02-01"),
           ev("funding_stage", "series_b", tier="2", published="2026-08-01")]
    a = assess(evs, icp, "Acme", "acme.example", NOW)
    assert a.criteria["funding_stage"].status == "verified" and a.criteria["funding_stage"].value == "series_b"


def test_late_stage_and_competitor_rejected(icp):
    a = assess([ev("is_b2b_saas", "true"), ev("funding_stage", "series_d_plus", tier="2")], icp, "Acme", "acme.example", NOW)
    assert a.decision == "rejected"
    b = assess([ev("is_b2b_saas", "true")], icp, "Drata", "drata.com", NOW)
    assert b.decision == "rejected"


def test_inferred_evidence_does_not_verify_must_have(icp):
    evs = [ev("is_b2b_saas", "true"), ev("funding_stage", "series_b", tier="3", status="inferred"),
           ev("compliance_in_progress", "soc2")]
    a = assess(evs, icp, "Acme", "acme.example", NOW)
    assert a.decision != "qualified"


def test_old_funding_not_counted_as_recent(icp):
    a = assess([ev("funding_stage", "series_b", tier="2", published="2023-01-01")], icp, "Acme", "acme.example", NOW)
    assert a.criteria["recent_funding"].value == "no"


# ---------- stop rules ----------
def test_stop_rules(icp):
    unc = assess([ev("is_b2b_saas", "true")], icp, "Acme", "acme.example", NOW)
    cont, why, code = stop_decision(unc, Budget(10, 4, pages_used=2, rounds_used=1), sources_remaining=True)
    assert cont and "could change" in why and code == "continue"
    cont, why, code = stop_decision(unc, Budget(10, 4, pages_used=10, rounds_used=2), sources_remaining=True)
    assert not cont and "budget" in why and code == "budget_pages"
    cont, why, code = stop_decision(unc, Budget(10, 4), sources_remaining=False)
    assert not cont and "no further sources" in why and code == "no_sources"
    rej = assess([ev("funding_stage", "public", tier="2")], icp, "Acme", "acme.example", NOW)
    cont, why, code = stop_decision(rej, Budget(10, 4), True)
    assert not cont and "rejected" in why and code == "rejected"


def test_qualified_stops_only_when_negatives_cannot_flip(icp):
    evs = [ev("is_b2b_saas", "true"), ev("funding_stage", "series_b", tier="2", published="2026-10-01"),
           ev("compliance_in_progress", "soc2"), ev("enterprise_selling", "sso_saml")]
    a = assess(evs, icp, "Acme", "acme.example", NOW)
    assert a.decision == "qualified"
    assert a.min_possible >= icp["scoring"]["qualify_threshold"]
    assert stop_decision(a, Budget(10, 4), True)[0] is False


# ---------- regressions from the first live run (2026-11-19) ----------
def test_partial_headcount_and_other_company_headcount_ignored():
    from sprinto_agent.evidence import rule_extract
    p = page("https://acme.example/about", "<p>In 2024, Dassault Aviation reported revenue and employs 14,600 people.</p>")
    found = [e for e in rule_extract(p, "about") if e.criterion == "employee_count"]
    assert all(e.value != "600" for e in found)
    p2 = page("https://acme.example/about", "<p>Partner firm Bigco employs 900 people across Europe.</p>")
    evs = [validate(e, p2, "Acme", "acme.example", SOURCES) for e in rule_extract(p2, "about") if e.criterion == "employee_count"]
    assert evs and all(e.status == "rejected" for e in evs)


def test_prior_round_mention_not_read_as_current_round():
    from sprinto_agent.evidence import rule_extract
    p = page("https://news.example/a", "<p>Simile raises $200M at $2B valuation 5 months after $100M Series A round.</p>")
    assert not [e for e in rule_extract(p, "press") if e.criterion == "funding_stage" and e.value == "series_a"]


def test_privacy_policy_not_used_for_product_signals(icp):
    from sprinto_agent.fetch import MockFetcher
    from sprinto_agent.research import Researcher
    from sprinto_agent.search import MockSearch
    pages = {"https://acme.example/": "<h1>Acme</h1><p>Acme builds tools for designers everywhere today.</p><a href='/privacy'>Privacy</a>",
             "https://acme.example/privacy": "<p>We collect bank account and credit card information for billing purposes.</p>"}
    r = Researcher(MockFetcher(pages), MockSearch({}), None, icp, {"tiers": {}}).research("acme.example", "Acme")
    assert not [e for e in r.evidence if e.criterion == "sensitive_data"]


# ---------- regressions from live run 3 (2026-11-19) ----------
def test_company_own_higher_stage_beats_earlier_seed_mention(icp):
    evs = [ev("funding_stage", "seed", tier="1"), ev("funding_stage", "series_a", tier="1")]
    a = assess(evs, icp, "Acme", "acme.example", NOW)
    assert a.criteria["funding_stage"].status == "verified" and a.criteria["funding_stage"].value == "series_a"


def test_stage_must_be_stated_not_inferred():
    q = "The company is a Y Combinator W24 company."
    p = page("https://acme.example/about", "<p>%s</p>" % q)
    r = validate(Evidence("funding_stage", "seed", q, p.url, ""), p, "Acme", "acme.example", SOURCES)
    assert r.status == "rejected" and "does not state" in r.note


def test_name_derived_from_homepage_so_press_is_attributed(icp):
    from sprinto_agent.fetch import MockFetcher
    from sprinto_agent.research import Researcher
    from sprinto_agent.search import MockSearch, SearchHit
    pages = {"https://froot.example/": "<html><head><title>Froot | B2B creator marketing</title></head><body><p>Froot helps B2B brands work with creators at scale.</p></body></html>",
             "https://news.example/froot": "<html><head><meta property='article:published_time' content='2026-07-22'></head><body><p>Froot raises $15M Series A to expand its B2B creator marketplace.</p><a href='https://froot.example/'>Froot</a></body></html>"}
    hits = {"froot": [SearchHit("Froot raises", "https://news.example/froot", "")]}
    r = Researcher(MockFetcher(pages), MockSearch(hits), None, icp, SOURCES).research("froot.example")
    assert r.name == "Froot"
    fs = [e for e in r.evidence if e.criterion == "funding_stage"]
    assert fs and fs[0].status == "verified" and fs[0].source_tier == "2"


def test_llm_values_normalised():
    from sprinto_agent.llm import normalize_value
    assert normalize_value("certified", "SOC 2 Type 2") == "soc2"
    assert normalize_value("enterprise_selling", "True") == "true"
