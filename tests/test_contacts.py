import pytest

from sprinto_agent.contacts import Contact, filter_hunter, manual_contact, verify_contact
from sprinto_agent.fetch import MockFetcher, html_to_page
from sprinto_agent.search import MockSearch

H = [
    {"value": "info@acme.example", "type": "generic", "confidence": 99, "first_name": None, "last_name": None, "position": None},
    {"value": "cto@acme.example", "type": "personal", "confidence": 95, "first_name": "Priya", "last_name": "Test", "position": "CTO"},
    {"value": "ceo@acme.example", "type": "personal", "confidence": 97, "first_name": "Dev", "last_name": "Test", "position": "Co-founder & CEO"},
    {"value": "nolast@acme.example", "type": "personal", "confidence": 95, "first_name": "Kim", "last_name": "", "position": "VP Engineering"},
    {"value": "notitle@acme.example", "type": "personal", "confidence": 95, "first_name": "Lee", "last_name": "Test", "position": ""},
    {"value": "low@acme.example", "type": "personal", "confidence": 30, "first_name": "Lo", "last_name": "Conf", "position": "CISO"},
    {"value": "x@other.example", "type": "personal", "confidence": 95, "first_name": "Off", "last_name": "Domain", "position": "CTO"},
    {"value": "mk@acme.example", "type": "personal", "confidence": 95, "first_name": "Mo", "last_name": "Kt", "position": "Marketing Manager"},
    {"value": None, "type": "personal", "confidence": 95, "first_name": "No", "last_name": "Email", "position": "CTO"},
]


def test_contact_filtering_and_ranking(icp):
    keep, dropped = filter_hunter(H, "acme.example", icp)
    names = [c.full_name for c in keep]
    assert names == ["Priya Test", "Dev Test"]          # CTO ranked above CEO per ICP role order
    assert all(c.email.endswith("@acme.example") for c in keep)
    assert len(dropped) == len(H) - 2
    assert any("not guessed" in d for d in dropped)


def test_no_guessed_email_or_name(icp):
    keep, _ = filter_hunter(H, "acme.example", icp)
    returned = {e["value"] for e in H if e["value"]}
    for c in keep:
        assert c.email in returned                         # only addresses Hunter actually returned
    assert all(c.first_name and c.last_name and c.title for c in keep)


def test_manual_contact_requires_fields_and_never_generates_email(icp):
    c = manual_contact("Priya", "Test", "CTO", None, "acme.example", icp, "apollo")
    assert c.email is None and c.source == "manual_apollo"
    with pytest.raises(ValueError):
        manual_contact("Priya", "", "CTO", None, "acme.example", icp)
    with pytest.raises(ValueError):
        manual_contact("Priya", "Test", "CTO", "priya@gmail.com", "acme.example", icp)


def test_verification_on_company_page(icp):
    team = html_to_page("https://acme.example/team", 200, "<p>Priya Test is the CTO of Acme.</p>")
    c = Contact("Priya", "Test", "CTO", "cto@acme.example", "hunter", role_label="CTO / VP Engineering")
    out = verify_contact(c, "Acme", "acme.example", [team], MockFetcher({}), MockSearch({}), {"tiers": {}})
    assert out.verification_status == "verified" and out.verification["url"].endswith("/team")


def test_unverified_when_not_found_and_linkedin_never_fetched(icp):
    from sprinto_agent.search import SearchHit
    hits = {"Priya Test": [SearchHit("p", "https://www.linkedin.com/in/priya", "")]}
    c = Contact("Priya", "Test", "CTO", None, "manual_apollo")
    fetcher = MockFetcher({"https://www.linkedin.com/in/priya": "<p>Priya Test CTO at Acme</p>"})
    out = verify_contact(c, "Acme", "acme.example", [], fetcher, MockSearch(hits), {"tiers": {}})
    assert out.verification_status == "unverified"
    assert fetcher.count == 0
