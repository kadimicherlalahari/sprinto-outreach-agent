from sprinto_agent.config import Settings
from sprinto_agent.drafting import guidance_from_history
from sprinto_agent.mock_data import mock_deps
from sprinto_agent.pipeline import execute_run, start_run
from sprinto_agent.store import open_real_store, StorageNotConfigured

import pytest


def run_mock(store, domains=("quietforms.example", "drata.example")):
    s = Settings(mock=True, operator_inbox="operator@inbox.example")
    deps = mock_deps()
    inputs = {"domains": list(domains)}
    rid = start_run(store, s, inputs, deps.icp)
    summary = execute_run(store, s, rid, inputs, deps)
    return rid, summary


def test_mock_run_end_to_end_offline(store):
    rid, summary = run_mock(store)
    cos = {c["domain"]: c for c in store.companies_for_run(rid)}
    assert cos["ledgerly.example"]["qualification"] == "qualified"
    assert cos["carelink.example"]["stage1_status"] == "rejected"
    assert cos["drata.example"]["stage1_status"] == "rejected"
    assert cos["quietforms.example"]["qualification"] == "uncertain"
    assert "stopped" in cos["quietforms.example"]["stop_reason"]
    assert summary["drafts"] == 1
    d = store.query("SELECT * FROM drafts")[0]
    assert d["status"] == "pending_review" and d["current_body"].startswith("Hi Asha,")
    hook = store.one("SELECT * FROM evidence WHERE id = %s", (d["hook_evidence_id"],))
    assert hook["status"] == "verified" and hook["source_url"].startswith("https://")
    assert store.one("SELECT is_mock FROM research_runs WHERE id = %s", (rid,))["is_mock"] in (1, True)


def test_one_company_failure_does_not_stop_run(store, monkeypatch):
    import sprinto_agent.pipeline as p
    real = p.process_company

    def flaky(store_, settings, run_id, cid, c, deps, summary, **kw):
        if c.domain == "ledgerly.example":
            raise RuntimeError("boom")
        return real(store_, settings, run_id, cid, c, deps, summary, **kw)
    monkeypatch.setattr(p, "process_company", flaky)
    rid, summary = run_mock(store)
    cos = {c["domain"]: c for c in store.companies_for_run(rid)}
    assert cos["ledgerly.example"]["qualification"] == "error"
    assert cos["quietforms.example"]["qualification"] == "uncertain"
    assert summary["errors"] == 1
    assert store.one("SELECT status FROM research_runs WHERE id = %s", (rid,))["status"] == "completed"


def test_mock_store_refuses_real_mode(store):
    with pytest.raises(RuntimeError, match="mock data"):
        start_run(store, Settings(mock=False), {}, {"name": "x"})


def test_missing_database_is_reported_not_faked():
    with pytest.raises(StorageNotConfigured, match="nothing was saved"):
        open_real_store(Settings(neon_url=None))


def test_review_guidance_is_traceable_and_wording_only():
    reviews = [
        {"id": "r1", "decision": "edit", "body": "Hey A,\nshort", "original_body": "Hi A,\n" + "x" * 200},
        {"id": "r2", "decision": "edit", "body": "Hey B,\nshort", "original_body": "Hi B,\n" + "x" * 200},
        {"id": "r3", "decision": "approve", "body": "Hi C", "original_body": "Hi C"},
    ]
    g = guidance_from_history(reviews)
    rules = {x["rule"]: x for x in g}
    assert set(rules) == {"prefer_shorter", "greeting"}
    assert rules["prefer_shorter"]["review_ids"] == ["r1", "r2"]
    assert rules["greeting"]["value"] == "Hey"


def test_review_history_changes_wording_not_facts(store):
    from sprinto_agent import review
    rid, _ = run_mock(store)
    d = store.query("SELECT * FROM drafts")[0]
    body2 = d["current_body"].replace("Hi Asha,", "Hey Asha,").replace("\n\nHappy to share how that would look for your setup if useful.", "")
    review.edit(store, d["id"], d["current_subject"], body2, "op")
    review.approve(store, d["id"], "op")
    led = store.one("SELECT id FROM companies WHERE domain = 'ledgerly.example' AND run_id = %s", (rid,))["id"]
    n_led = len(store.evidence_for(led))
    rid2, _ = run_mock(store)
    # edits never become evidence: no evidence row carries edited wording, and Ledgerly's evidence is unchanged
    assert not store.query("SELECT id FROM evidence WHERE quote LIKE %s", ("%Hey Asha%",))
    assert len(store.evidence_for(led)) == n_led
    # Ledgerly already has an approved draft, so the second run does not research or draft it again
    led2 = store.one("SELECT * FROM companies WHERE domain = 'ledgerly.example' AND run_id = %s", (rid2,))
    assert led2["research_stage"] == "skipped" and "already in the pipeline" in led2["stop_reason"]
    assert not store.query("SELECT id FROM drafts WHERE run_id = %s", (rid2,))


def test_certified_company_gets_non_contradicting_pain(icp):
    from sprinto_agent.drafting import compose
    rows = [{"id": "e1", "criterion": "certified", "value": "soc2", "status": "verified", "source_tier": "1",
             "quote": "Acme is SOC 2 Type 2 certified.", "published_at": None},
            {"id": "e2", "criterion": "funding_stage", "value": "series_a", "status": "verified", "source_tier": "2",
             "quote": "Acme raised a Series A.", "published_at": "2025-01-01"}]
    d = compose("Acme", "Jo", "Founder / CEO", rows, {"e1": False, "e2": False}, icp, [])
    assert d.hook_evidence_id == "e1"                      # stale funding not used as hook
    assert "waiting on a SOC 2" not in d.body and "already has SOC 2" in d.body


def test_store_reconnects_after_dropped_connection():
    import sqlite3
    import psycopg
    from sprinto_agent.store import Store

    class Dead:
        def cursor(self):
            raise psycopg.OperationalError("terminating connection due to idle-in-transaction timeout")
        def close(self):
            pass
    fresh = sqlite3.connect(":memory:")
    s = Store(Dead(), "sqlite", is_mock=True, label="t", reconnect=lambda: fresh)
    assert s.query("SELECT 1 AS x") == [{"x": 1}]
