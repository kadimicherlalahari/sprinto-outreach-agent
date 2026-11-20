"""Search bar and 'research one company' (URL or name). Offline: mock pages, mock search, mock store."""
import pytest
from fastapi.testclient import TestClient

from sprinto_agent.config import Settings
from sprinto_agent.mock_data import mock_deps
from sprinto_agent.pipeline import CompanyNotFound, research_one, resolve_company
from sprinto_agent.search import MockSearch, SearchHit

S = Settings(mock=True, operator_inbox="op@inbox.example")


def deps_with_search(hits):
    d = mock_deps()
    d.search = MockSearch(hits)
    return d


@pytest.mark.parametrize("q,domain", [("ledgerly.example", "ledgerly.example"), ("https://www.ledgerly.example/pricing", "ledgerly.example"),
                                      ("LEDGERLY.example", "ledgerly.example")])
def test_url_or_domain_is_used_as_given(store, q, domain):
    assert resolve_company(store, q, mock_deps())["domain"] == domain


def test_name_resolved_only_to_a_matching_official_site(store):
    hits = {"Ledgerly": [SearchHit(title="Ledgerly news", url="https://news.example/ledgerly-raises", snippet=""),
                         SearchHit(title="Ledgerly on LinkedIn", url="https://www.linkedin.com/company/ledgerly", snippet=""),
                         SearchHit(title="Unrelated", url="https://billingtools.example/", snippet=""),
                         SearchHit(title="Ledgerly", url="https://www.ledgerly.example/", snippet="")]}
    r = resolve_company(store, "Ledgerly", deps_with_search(hits))
    assert r["domain"] == "ledgerly.example" and "search" in r["how"]


def test_name_with_no_matching_site_is_not_guessed(store):
    hits = {"Zentrix": [SearchHit(title="Zentrix raises", url="https://news.example/zentrix", snippet=""),
                        SearchHit(title="Other", url="https://someothercompany.example/", snippet="")]}
    with pytest.raises(CompanyNotFound):
        resolve_company(store, "Zentrix", deps_with_search(hits))


def test_social_or_blocked_site_refused(store):
    with pytest.raises(CompanyNotFound):
        resolve_company(store, "https://www.linkedin.com/company/acme", mock_deps())


def test_name_already_in_pipeline_uses_stored_domain(store):
    research_one(store, S, "ledgerly.example", mock_deps())
    r = resolve_company(store, "Ledgerly", deps_with_search({}))
    assert r["domain"] == "ledgerly.example" and r["how"] == "already in your pipeline"


def test_research_one_runs_end_to_end_and_respects_time_cap(store):
    r = research_one(store, S, "ledgerly.example", mock_deps(), max_seconds=150)
    assert r["qualification"] == "qualified" and r["domain"] == "ledgerly.example"
    assert store.one("SELECT COUNT(*) AS n FROM drafts")["n"] == 1
    run = store.one("SELECT status, input_json FROM research_runs WHERE id = %s", (r["run_id"],))
    assert run["status"] == "completed" and '"max_research": 1' in run["input_json"]
    again = research_one(store, S, "ledgerly.example", mock_deps())          # already has an open draft
    assert again["stage"] == "skipped" and store.one("SELECT COUNT(*) AS n FROM drafts")["n"] == 1


def client(store, monkeypatch, vercel=False):
    for k in ("VERCEL", "DASHBOARD_PASSWORD", "DEMO_LOGIN"):
        monkeypatch.delenv(k, raising=False)
    if vercel:
        monkeypatch.setenv("VERCEL", "1")
        monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
        monkeypatch.setenv("DEMO_LOGIN", "off")
    from sprinto_agent.web import create_app
    c = TestClient(create_app(mock=True, store=store, settings=S, deps_factory=lambda s: mock_deps()), base_url="https://testserver")
    if vercel:
        c.post("/login", data={"password": "pw", "next": "/"})
    return c


def test_overview_has_search_and_research_one(store, monkeypatch):
    t = client(store, monkeypatch).get("/").text
    assert 'action="/search"' in t and 'action="/run-one"' in t


def test_research_one_on_vercel_completes_in_the_request(store, monkeypatch):
    c = client(store, monkeypatch, vercel=True)
    r = c.post("/run-one", data={"q": "ledgerly.example"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/companies/")
    assert store.one("SELECT qualification FROM companies WHERE domain = 'ledgerly.example'")["qualification"] == "qualified"
    page = c.get(r.headers["location"]).text
    assert "Researched ledgerly.example" in page


def test_research_one_unknown_name_shows_reason(store, monkeypatch):
    c = client(store, monkeypatch, vercel=True)
    r = c.post("/run-one", data={"q": "Nonexistent Co"}, follow_redirects=True)
    assert "no website matching" in r.text and store.query("SELECT id FROM research_runs") == []


def test_search_page_finds_pipeline_companies(store, monkeypatch):
    research_one(store, S, "ledgerly.example", mock_deps())
    c = client(store, monkeypatch)
    assert "Ledgerly" in c.get("/search?q=ledger").text
    assert "No company in the pipeline matches" in c.get("/search?q=zzzz").text


# ---------------------------------------------------------------- run button in steps (Vercel) + clickable stage cards
def test_run_out_of_time_parks_run_and_continue_finishes_it(store):
    import time
    from sprinto_agent.pipeline import execute_run, resume_run, start_run
    deps = mock_deps()
    inputs = {"domains": [], "use_feeds": True, "use_search": True, "use_portfolios": True, "max_research": 3}
    run_id = start_run(store, S, inputs, deps.icp)
    execute_run(store, S, run_id, inputs, deps, deadline=time.monotonic() - 1)       # no time left: nothing researched
    run = store.one("SELECT status, summary_json, active_lock FROM research_runs WHERE id = %s", (run_id,))
    assert run["status"] == "paused" and run["active_lock"] is None and '"time budget"' in run["summary_json"]
    waiting = store.query("SELECT id FROM companies WHERE run_id = %s AND stop_code = 'time_budget'", (run_id,))
    picked = store.query("SELECT id FROM companies WHERE run_id = %s AND research_stage <> 'skipped'", (run_id,))
    assert waiting and len(waiting) == len(picked) and store.query("SELECT id FROM evidence") == []
    resume_run(store, S, run_id, mock_deps(), deadline=time.monotonic() + 600)
    assert store.one("SELECT status FROM research_runs WHERE id = %s", (run_id,))["status"] == "completed"
    assert store.query("SELECT id FROM companies WHERE run_id = %s AND stop_code = 'time_budget'", (run_id,)) == []
    assert store.one("SELECT COUNT(*) AS n FROM companies WHERE run_id = %s AND research_stage = 'done'", (run_id,))["n"] == len(picked)


def test_run_button_works_on_vercel_and_overview_offers_continue(store, monkeypatch):
    import sprinto_agent.web as web
    c = client(store, monkeypatch, vercel=True)
    assert 'action="/run"' in c.get("/").text                                        # the button is on the home screen
    monkeypatch.setattr(web, "STEP_SECONDS", -1)                                     # force the step to end at once
    r = c.post("/run", data={"use_feeds": "on", "use_search": "on", "use_portfolios": "on", "max_research": "2"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/") and "Continue" in r.headers["location"]
    page = c.get("/").text
    assert "Run paused between steps" in page and "/resume" in page
    monkeypatch.setattr(web, "STEP_SECONDS", 600)
    run_id = r.headers["location"].split("/runs/")[1].split("?")[0]
    r2 = c.post("/runs/%s/resume" % run_id, follow_redirects=False)
    assert "Run%20finished" in r2.headers["location"]
    assert "Run paused between steps" not in c.get("/").text


def test_stage_cards_link_to_their_pipeline_view(store, monkeypatch):
    research_one(store, S, "ledgerly.example", mock_deps())
    t = client(store, monkeypatch).get("/").text
    for key in ("discovered", "researched", "qualified", "contact", "drafted"):
        assert 'class="stretch" href="/pipeline?stage=%s"' % key in t
