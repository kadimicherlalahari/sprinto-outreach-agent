"""Feedback loops: every feedback type is stored against the right record, kept apart from evidence, idempotent,
survives a database outage; POC re-runs use preferences without guessing; draft revisions + diffs; style guidance
is reviewable and wording-only. Offline, fake data only."""
import json
import sqlite3

import pytest

from sprinto_agent import review
from sprinto_agent.contacts import MockHunter
from sprinto_agent.feedback import (accepted_guidance, categorize, decide_style_guidance, demoted_domains,
                                    flush_outbox, propose_style_guidance, record_feedback, revisions)
from sprinto_agent.mock_data import mock_hunter
from sprinto_agent.pipeline import rerun_contact
from sprinto_agent.safety import open_incidents, sending_paused

from test_reliability import INBOX, S, approved_draft, deps_with, run


def ledgerly(store):
    return store.one("SELECT * FROM companies WHERE domain = 'ledgerly.example' ORDER BY created_at DESC")


def snapshot_facts(store, cid):
    co = store.one("SELECT qualification, score, assessment_json FROM companies WHERE id = %s", (cid,))
    ev = store.query("SELECT id, status, value, quote FROM evidence WHERE company_id = %s ORDER BY id", (cid,))
    return co, ev


def test_every_feedback_type_saved_against_correct_record_and_separate_from_facts(store, icp):
    rid, _ = run(store, deps_with(icp))
    co = ledgerly(store)
    ev = store.query("SELECT * FROM evidence WHERE company_id = %s AND status = 'verified'", (co["id"],))[0]
    poc = store.pocs_for(co["id"])[0]
    d = store.drafts_for(co["id"])[0]
    before = snapshot_facts(store, co["id"])

    out = [
        record_feedback(store, "discovery", "irrelevant", "Op", "not a SaaS we sell to", company_id=co["id"],
                        details={"found_via": co["found_via"]}),
        record_feedback(store, "qualification", "should_reject", "Op", "they are too small", company_id=co["id"]),
        record_feedback(store, "research", "weak_source", "Op", "old blog post", company_id=co["id"], evidence_id=ev["id"]),
        record_feedback(store, "poc", "suitable", "Op", "", company_id=co["id"], poc_id=poc["id"]),
        record_feedback(store, "outcome", "note", "Op", "test note", company_id=co["id"], draft_id=d["id"]),
    ]
    review.edit(store, d["id"], d["current_subject"], d["current_body"] + "\n\nThanks!", "Op", "warmer close")
    assert all(o["status"] == "saved" for o in out)

    rows = store.query("SELECT * FROM feedback WHERE company_id = %s ORDER BY created_at", (co["id"],))
    kinds = {r["kind"] for r in rows}
    assert kinds == {"discovery", "qualification", "research", "poc", "outcome", "draft"}
    for r in rows:
        assert r["run_id"] == rid and r["prompt_version"].startswith("prompts-") and r["icp_version"] and \
            r["policy_version"].startswith("policy-")
    by = {r["kind"]: r for r in rows}
    assert by["research"]["evidence_id"] == ev["id"] and by["poc"]["poc_id"] == poc["id"]
    assert by["draft"]["draft_id"] == d["id"] and by["draft"]["verdict"] == "edit"
    assert json.loads(by["discovery"]["details_json"])["found_via"] == co["found_via"]
    # opinions never touched the facts or the code's decision
    assert snapshot_facts(store, co["id"]) == before
    assert store.one("SELECT COUNT(*) AS n FROM evidence WHERE note LIKE '%%too small%%'")["n"] == 0


def test_feedback_is_idempotent_and_validated(store, icp):
    run(store, deps_with(icp))
    co = ledgerly(store)
    a = record_feedback(store, "qualification", "agree", "Op", "", company_id=co["id"], idem_key="form-1")
    b = record_feedback(store, "qualification", "agree", "Op", "", company_id=co["id"], idem_key="form-1")
    assert a["status"] == "saved" and b["status"] == "duplicate"
    assert store.one("SELECT COUNT(*) AS n FROM feedback")["n"] == 1
    with pytest.raises(ValueError):
        record_feedback(store, "qualification", "make_it_qualified_now", "Op", "x", company_id=co["id"])
    with pytest.raises(ValueError):
        record_feedback(store, "research", "weak_source", "Op", "", company_id=co["id"])   # reason required


def test_feedback_survives_database_outage_and_replays_once(store, icp, tmp_path):
    run(store, deps_with(icp))
    co = ledgerly(store)
    outbox = tmp_path / "outbox.jsonl"
    real = store.insert

    def down(table, row):
        if table == "feedback":
            raise sqlite3.OperationalError("database is unavailable")
        return real(table, row)
    store.insert = down
    r = record_feedback(store, "discovery", "relevant", "Op", "", company_id=co["id"], outbox=outbox)
    assert r["status"] == "queued_locally" and outbox.exists()
    store.insert = real
    assert flush_outbox(store, outbox) == {"saved": 1, "duplicate": 0, "left": 0}
    assert not outbox.exists()
    # replaying the same item again (e.g. worker restarted mid-flush) does not duplicate it
    outbox.write_text(json.dumps(store.one("SELECT * FROM feedback")) + "\n")
    assert flush_outbox(store, outbox)["duplicate"] == 1
    assert store.one("SELECT COUNT(*) AS n FROM feedback")["n"] == 1


def test_research_feedback_only_demotes_a_source_never_creates_a_fact(store, icp):
    from sprinto_agent.fetch import MockFetcher
    from sprinto_agent.mock_data import mock_sources
    from sprinto_agent.qualify import Budget
    from sprinto_agent.research import Researcher, ResearchResult
    from sprinto_agent.util import now_iso
    run(store, deps_with(icp))
    co = ledgerly(store)
    news = "https://news.example/ledgerly-raises"
    eid = store.insert("evidence", {"company_id": co["id"], "criterion": "funding_stage", "value": "series_b", "status": "verified",
                                    "quote": "Ledgerly raised a Series B.", "source_url": news, "source_tier": "2",
                                    "retrieved_at": now_iso(), "published_at": None, "extractor": "rules"})
    record_feedback(store, "research", "weak_source", "Op", "thin article", company_id=co["id"], evidence_id=eid)
    assert demoted_domains(store) == []                       # one flag is not enough
    record_feedback(store, "research", "wrong_company", "Op2", "different Ledgerly", company_id=co["id"], evidence_id=eid)
    assert demoted_domains(store) == ["news.example"]
    before = snapshot_facts(store, co["id"])
    src = dict(mock_sources(), demoted_domains=demoted_domains(store))
    pages = {news: "<html><body><p>Ledgerly raised a Series B round.</p></body></html>",
             "https://ledgerly.example/about": "<html><body><p>About Ledgerly.</p></body></html>"}
    r = Researcher(MockFetcher(pages), None, None, icp, src)
    r._seen_external = set()
    res = ResearchResult(domain="ledgerly.example", name="Ledgerly")
    b = Budget(max_pages=5, max_rounds=2)
    assert r._fetch(news, res, b) is None                     # demoted third-party source is not fetched
    assert any(x[0] == "page" and x[2] == "skipped_demoted" for x in res.audit)
    assert r._fetch("https://ledgerly.example/about", res, b) is not None   # the company's own site never is
    assert snapshot_facts(store, co["id"]) == before          # feedback did not add, remove or upgrade any fact


HUNTER_MORE = {"ledgerly.example": mock_hunter()["ledgerly.example"] + [
    {"value": "lee@ledgerly.example", "type": "personal", "confidence": 93, "first_name": "Lee", "last_name": "Securename",
     "position": "Head of IT", "department": "it", "seniority": "senior"},
    {"value": "kim@ledgerly.example", "type": "personal", "confidence": 95, "first_name": "Kim", "last_name": "Founderly",
     "position": "Co-founder & CEO", "department": "executive", "seniority": "executive"},
]}


def test_poc_rejection_reruns_with_preferences_and_never_guesses(store, icp):
    deps = deps_with(icp, hunter=MockHunter(HUNTER_MORE))
    run(store, deps)
    co = ledgerly(store)
    first = store.pocs_for(co["id"])[0]
    assert first["full_name"] == "Asha Mockingbird"
    old_draft = store.drafts_for(co["id"])[0]
    out = rerun_contact(store, S(), co["id"], deps, "Op", {"preferred_titles": ["Head of IT"], "excluded_titles": ["CTO"]},
                        "CTO is too senior for a first touch")
    assert out["status"] == "found" and out["name"] == "Lee Securename" and out["title"] == "Head of IT"
    assert store.one("SELECT status FROM pocs WHERE id = %s", (first["id"],))["status"] == "rejected"
    assert store.draft(old_draft["id"])["status"] == "superseded"
    new = store.one("SELECT * FROM pocs WHERE id = %s", (out["poc_id"],))
    assert new["email"] == "lee@ledgerly.example"                # exactly what Hunter returned
    assert json.loads(new["lookup_prefs_json"])["preferred_titles"] == ["Head of IT"]
    fb = store.one("SELECT * FROM feedback WHERE kind = 'poc' AND poc_id = %s", (first["id"],))
    assert fb["verdict"] == "unsuitable" and json.loads(fb["details_json"])["rerun"] is True
    # preferences that match nobody: no contact is invented, no POC row is added
    n = len(store.pocs_for(co["id"]))
    out2 = rerun_contact(store, S(), co["id"], deps, "Op", {"preferred_titles": ["Chief Compliance Officer"],
                                                            "departments": ["legal"]}, "want legal")
    assert out2["status"] == "no_contact" and len(store.pocs_for(co["id"])) == n
    assert all(p["email"] in {e["value"] for e in HUNTER_MORE["ledgerly.example"]} for p in store.pocs_for(co["id"]))
    # rejected people are never picked again
    rerun_contact(store, S(), co["id"], deps, "Op", {}, "anyone else")
    actives = [p for p in store.pocs_for(co["id"]) if p["status"] == "active"]
    assert all(p["email"] not in ("asha@ledgerly.example", "lee@ledgerly.example") for p in actives)


def test_draft_revisions_diffs_and_approval_invalidated_by_edit(store, icp):
    d = approved_draft(store, icp)
    revs = revisions(store, d["id"])
    assert [r["kind"] for r in revs] == ["generated", "approved"]
    parts = json.loads(d["parts_json"])
    body = d["current_body"].replace(parts["hook"], "Saw your team is growing fast.") \
                            .replace(parts["cta"], "Worth a quick chat next week?")
    review.edit(store, d["id"], "Quick question, Ledgerly", body, "Op", "simpler hook")
    d2 = store.draft(d["id"])
    assert d2["status"] == "pending_review" and d2["approved_hash"] is None and d2["approved_recipient"] is None
    revs = revisions(store, d["id"])
    assert [r["kind"] for r in revs] == ["generated", "approved", "edit"]
    first, last = revs[0], revs[-1]
    assert first["body"] == d["original_body"]                          # first generated draft preserved
    cats = json.loads(last["categories_json"])
    assert {"subject", "hook", "cta"} <= set(cats) and "factual_claim" not in cats
    diff = json.loads(last["diff_json"])
    assert diff["subject"]["to"] == "Quick question, Ledgerly" and 0 < diff["change_ratio"] < 1
    with pytest.raises(review.SendBlocked, match="only approved"):
        review.send(store, S(), d["id"])


def test_categorize_flags_new_factual_claims_and_tone():
    parts = {"hook": "I noticed X.", "pain": "A guess: Y.", "cta": "Is Z on the list?"}
    old = "Hi Sam,\n\nI noticed X.\n\nA guess: Y.\n\nIs Z on the list?\n\nBest,\nOp"
    new = "Hey Sam,\n\nI noticed X.\n\nA guess: Y. We helped 300 customers pass SOC 2.\n\nIs Z on the list?\n\nBest,\nOp"
    cats = categorize("s", old, "s", new, parts)
    assert "factual_claim" in cats and "tone" in cats


def test_edited_unsupported_claim_blocks_send_and_pauses(store, icp):
    d = approved_draft(store, icp)
    review.edit(store, d["id"], d["current_subject"], d["current_body"].replace(
        "Best,", "We got 400 startups SOC 2 certified in 2 weeks.\n\nBest,"), "Op")
    review.approve(store, d["id"], "Op", recipient=INBOX)
    with pytest.raises(review.SendBlocked, match="factual claim"):
        review.send(store, S(), d["id"])
    assert sending_paused(store) and open_incidents(store)[0]["kind"] == "unsupported_claim"
    assert store.sends_for(d["id"]) == []


def test_style_guidance_is_proposed_scoped_wording_only_and_needs_acceptance(store, icp):
    d = approved_draft(store, icp)
    from sprinto_agent.drafting import CLOSING_EXTRA
    # two wording-only edits (closing line removed) and one edit that adds a factual claim
    review.edit(store, d["id"], d["current_subject"], d["current_body"].replace(CLOSING_EXTRA, ""), "Op")
    review.edit(store, d["id"], d["current_subject"], store.draft(d["id"])["current_body"].replace("Hi ", "Hey "), "Op")
    review.edit(store, d["id"], d["current_subject"], store.draft(d["id"])["current_body"] + "\nWe have 500 customers.", "Op")
    created = propose_style_guidance(store)
    rows = store.query("SELECT * FROM style_guidance")
    assert rows and all(r["status"] == "proposed" for r in rows)
    assert all("customer" not in (r["detail"] or "").lower() for r in rows)            # no facts learned
    role = store.pocs_for(d["company_id"])[0]["role_label"]
    assert accepted_guidance(store, role, d["segment"]) == []                          # unused until accepted
    decide_style_guidance(store, rows[0]["id"], True, "Op")
    g = accepted_guidance(store, role, d["segment"])
    assert len(g) == 1 and g[0]["guidance_id"] == rows[0]["id"]
    assert accepted_guidance(store, "Some other role", "series_z") == [] or rows[0]["scope_role"] == "*"
    assert created
