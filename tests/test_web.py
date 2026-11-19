"""Smoke tests: every UI page renders on mock data (offline)."""
from fastapi.testclient import TestClient

from sprinto_agent.config import Settings
from sprinto_agent.mock_data import mock_deps
from sprinto_agent.pipeline import execute_run, start_run
from sprinto_agent.web import create_app


def _client(store):
    s = Settings(mock=True, operator_inbox="operator@inbox.example")
    deps = mock_deps()
    inputs = {"domains": ["quietforms.example", "drata.example"]}
    rid = start_run(store, s, inputs, deps.icp)
    execute_run(store, s, rid, inputs, deps)
    return TestClient(create_app(mock=True, store=store, settings=s)), rid


def test_all_pages_render(store):
    c, rid = _client(store)
    co = store.one("SELECT id FROM companies WHERE domain = 'ledgerly.example'")["id"]
    d = store.query("SELECT id FROM drafts")[0]["id"]
    for url in ["/", "/pipeline", "/pipeline?stage=qualified", "/pipeline?stage=needs_contact&q=led", "/review",
                "/review?status=approved", "/outreach", "/runs", "/runs/" + rid, "/settings", "/companies/" + co, "/drafts/" + d]:
        r = c.get(url)
        assert r.status_code == 200, url
        assert "Mock mode" in r.text


def test_overview_shows_queue_and_funnel(store):
    c, _ = _client(store)
    t = c.get("/").text
    assert "Needs you" in t and "Review drafts" in t and "class=\"stages\"" in t and "researched:" in t
    assert 'href="/pipeline?stage=qualified"' in t


def test_ui_review_flow_and_send_disabled_until_approved(store):
    c, _ = _client(store)
    d = store.query("SELECT * FROM drafts")[0]
    page = c.get("/drafts/" + d["id"]).text
    assert 'action="/drafts/%s/send"><button class="btn" type="submit" disabled' % d["id"] in page
    r = c.post("/drafts/%s/approve" % d["id"], data={"expected_hash": d["current_hash"], "reviewer": "op"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.post("/drafts/%s/send" % d["id"], follow_redirects=True)
    assert "DRY RUN" in r.text
    assert c.get("/outreach").text.count("Dry run") >= 1


def test_duplicate_run_click_in_ui_is_refused(store):
    from sprinto_agent.pipeline import start_run
    s = Settings(mock=True, operator_inbox="operator@inbox.example")
    c = TestClient(create_app(mock=True, store=store, settings=s, deps_factory=lambda st: mock_deps()))
    start_run(store, s, {"domains": []}, mock_deps().icp)  # a run is already active
    r = c.post("/run", data={"domains": "ledgerly.example", "max_research": "1"}, follow_redirects=True)
    assert "already in progress" in r.text
    assert len(store.query("SELECT id FROM research_runs")) == 1


def test_pause_and_opt_out_controls(store):
    c, _ = _client(store)
    assert "Pause all sending" in c.get("/settings").text
    c.post("/settings/pause", data={"paused": "1"})
    assert "Resume sending" in c.get("/settings").text
    c.post("/settings/suppress", data={"kind": "domain", "value": "ledgerly.example", "reason": "asked"})
    assert "ledgerly.example" in c.get("/settings").text
    d = store.query("SELECT * FROM drafts")[0]
    page = c.get("/drafts/" + d["id"]).text
    assert "Sending blocked" in page and "opt-out" in page
