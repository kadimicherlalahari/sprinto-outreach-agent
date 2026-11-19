"""Cost caps: configuration required before paid research, per-company and monthly caps checked before every
billable call (including concurrent ones), unknown prices limited by units and never shown as zero, explicit
human resume. Offline; fake prices in currency 'FAKE'."""
import threading
from datetime import datetime, timezone

import pytest

from sprinto_agent import costs
from sprinto_agent.config import Settings
from sprinto_agent.llm import MockLLM
from sprinto_agent.pipeline import resume_run, start_run

from test_reliability import S, deps_with, run


def set_caps(store, per_company=1.0, monthly=100.0, prices=None, limits=None):
    costs.set_setting(store, "caps", {"currency": "FAKE", "per_company_cap": per_company, "period_cap": monthly, "period": "month"}, "test")
    costs.set_setting(store, "prices", prices if prices is not None else {"tavily": {"price": 0.01}, "gemini": {"price": 0.5},
                                                                           "hunter": {"price": 0.05}}, "test")
    if limits is not None:
        costs.set_setting(store, "unit_limits", limits, "test")


def test_config_defaults_are_loaded_once_and_never_overwrite_saved_limits(store, icp):
    store.is_mock = False
    s = Settings(mock=False, tavily_key="t", gemini_key="g", hunter_key="h")
    assert start_run(store, s, {"domains": []}, icp)                    # config/costs.yaml applied
    caps = costs.get_setting(store, "caps")
    assert caps["per_company_cap"] == 0.25 and caps["period_cap"] == 20.0 and caps["currency"] == "USD"
    assert costs.get_setting(store, "prices")["hunter"]["price"] is None and costs.get_setting(store, "unit_limits")["hunter"] == 40
    hist = store.query("SELECT changed_by FROM cost_settings_history")
    assert hist and all(h["changed_by"] == "config/costs.yaml" for h in hist)
    costs.set_setting(store, "caps", dict(caps, period_cap=5.0), "Operator")
    assert costs.apply_config_defaults(store) is False and costs.get_setting(store, "caps")["period_cap"] == 5.0


def test_paid_research_refused_until_operator_sets_limits(store, icp, monkeypatch):
    store.is_mock = False                                     # behave like the real database for this check
    monkeypatch.setattr(costs, "apply_config_defaults", lambda st, by="x": False)   # no config file
    s = Settings(mock=False, tavily_key="t", gemini_key="g", hunter_key="h")
    with pytest.raises(costs.CostNotConfigured, match="per-company"):
        start_run(store, s, {"domains": ["ledgerly.example"]}, icp)
    costs.set_setting(store, "caps", {"currency": "USD", "per_company_cap": 0.5, "period_cap": 20, "period": "month"}, "op")
    with pytest.raises(costs.CostNotConfigured, match="tavily"):
        start_run(store, s, {"domains": []}, icp)
    costs.set_setting(store, "prices", {"tavily": {"price": 0.008}, "hunter": {"price": None}}, "op")
    costs.set_setting(store, "unit_limits", {"gemini": 200, "hunter": 25}, "op")   # prices unknown -> unit limits
    assert costs.readiness(store, ["tavily", "gemini", "hunter"]) == []
    assert start_run(store, s, {"domains": []}, icp)
    assert store.one("SELECT COUNT(*) AS n FROM cost_settings_history")["n"] >= 3


def test_meter_without_caps_blocks_every_billable_call(store):
    m = costs.Meter(store, "r", "c")
    with pytest.raises(costs.CostCapReached):
        m.reserve("tavily", 1)
    assert store.one("SELECT COUNT(*) AS n FROM cost_ledger")["n"] == 0


def test_company_cap_stops_research_keeps_findings_and_lists_unresolved(store, icp):
    set_caps(store, per_company=0.0015, prices={"tavily": {"price": 0.01}, "gemini": {"price": 0.5}, "hunter": {"price": 0.05}})
    deps = deps_with(icp, llm=MockLLM())                      # every page extraction is a (fake) billable model call
    rid, _ = run(store, deps, domains=["ledgerly.example"])
    co = store.one("SELECT * FROM companies WHERE domain = 'ledgerly.example'")
    assert co["stop_code"] == "cost_cap_reached" and "unresolved" in co["stop_reason"]
    assert co["error_kind"] == "cost_cap" and not co["retryable"]
    assert store.one("SELECT COUNT(*) AS n FROM evidence WHERE company_id = %s", (co["id"],))["n"] > 0   # findings kept
    led = store.query("SELECT * FROM cost_ledger WHERE company_id = %s", (co["id"],))
    assert led and sum(r["est_cost"] for r in led) <= 0.0015 + 1e-12
    assert all(r["cost_basis"] == "estimated" and r["currency"] == "FAKE" for r in led)
    assert all(r["status"] == "settled" for r in led)


def test_period_cap_pauses_all_billable_work_and_needs_explicit_resume(store, icp):
    set_caps(store, per_company=1.0, monthly=0.002)
    deps = deps_with(icp, llm=MockLLM())
    rid, summary = run(store, deps, domains=["ledgerly.example", "quietforms.example"])
    paused = costs.billable_paused(store)
    assert paused and "monthly cost cap" in paused and "resets" in paused
    run_row = store.one("SELECT status FROM research_runs WHERE id = %s", (rid,))
    assert run_row["status"] in ("paused", "completed")
    total = store.one("SELECT COALESCE(SUM(est_cost), 0) AS c FROM cost_ledger")["c"]
    assert total <= 0.002 + 1e-12
    # raising the cap does NOT resume anything by itself
    set_caps(store, per_company=1.0, monthly=50.0)
    assert costs.billable_paused(store)
    with pytest.raises(costs.CostNotConfigured, match="paused"):
        resume_run(store, S(), rid, deps)
    with pytest.raises(costs.CostCapReached):
        costs.Meter(store, rid, None).reserve("tavily", 1)
    with pytest.raises(ValueError):
        costs.resume_billable(store, "", "Op")
    costs.resume_billable(store, "raised monthly cap after review", "Op")
    assert costs.billable_paused(store) is None
    ev = store.query("SELECT * FROM control_events WHERE scope = 'billable' ORDER BY created_at")
    assert [e["action"] for e in ev] == ["pause", "resume"] and ev[0]["actor"] == "system" and ev[1]["actor"] == "human"


def test_concurrent_reservations_never_exceed_the_cap(store):
    set_caps(store, per_company=0.05, monthly=0.10)
    ok, blocked = [], []

    def worker(i):
        m = costs.Meter(store, "r", "company-%d" % (i % 4))
        try:
            ok.append(m.reserve("tavily", 1))
        except costs.CostCapReached:
            blocked.append(i)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(25)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    total = store.one("SELECT SUM(est_cost) AS c FROM cost_ledger WHERE status IN ('reserved', 'settled')")["c"]
    assert len(ok) == 10 and len(blocked) == 15 and total <= 0.10 + 1e-9
    per = store.query("SELECT company_id, SUM(est_cost) AS c FROM cost_ledger GROUP BY company_id")
    assert all(r["c"] <= 0.05 + 1e-9 for r in per)


def test_unknown_price_is_never_zero_and_has_its_own_unit_limit(store):
    set_caps(store, prices={"tavily": {"price": None}, "gemini": {"price": 0.5}, "hunter": {"price": 0.05}},
             limits={"tavily": 3})
    m = costs.Meter(store, "r", "c")
    ids = [m.reserve("tavily", 1) for _ in range(3)]
    with pytest.raises(costs.CostCapReached, match="unit limit"):
        m.reserve("tavily", 1)
    rows = store.query("SELECT * FROM cost_ledger")
    assert len(rows) == 3 and all(r["est_cost"] is None and r["cost_basis"] == "unknown_price" for r in rows)
    sp = costs.spend(store)
    assert sp["unknown_price_calls"] == 3 and sp["by_provider"]["tavily"]["estimated"] == 0.0
    # no unit limit and no price: blocked rather than run unmetered
    costs.set_setting(store, "unit_limits", {}, "test")
    with pytest.raises(costs.CostCapReached):
        costs.Meter(store, "r", "c2").reserve("tavily", 1)
    assert ids


def test_model_usage_settles_with_reported_tokens_and_is_still_an_estimate(store):
    set_caps(store)

    class Fake:
        model = "fake-model"
        last_usage = {"totalTokenCount": 1234}

        def generate(self, prompt, json_mode=True, max_tokens=2048):
            return "[]"
    m = costs.Meter(store, "r", "c")
    costs.MeteredLLM(Fake(), m).generate("x" * 400)
    row = store.one("SELECT * FROM cost_ledger")
    assert row["units"] == 1234 and row["unit_kind"] == "tokens" and row["model"] == "fake-model"
    assert row["cost_basis"] == "estimated" and row["actual_cost"] is None
    assert abs(row["est_cost"] - 1234 * 0.5 / 1e6) < 1e-12 and "reported by provider" in row["detail"]


def test_failed_call_keeps_its_reservation(store):
    set_caps(store)

    class Down:
        def search(self, q, **k):
            from sprinto_agent.search import SearchUnavailable
            raise SearchUnavailable("Tavily HTTP 503", "outage")
    m = costs.Meter(store, "r", "c")
    with pytest.raises(Exception):
        costs.MeteredSearch(Down(), m).search("q")
    row = store.one("SELECT * FROM cost_ledger")
    assert row["status"] == "settled" and row["est_cost"] == 0.01 and "may still charge" in row["detail"]


def test_cost_settings_validation_and_period_reset():
    from sprinto_agent.store import open_mock_store
    st = open_mock_store(":memory:")
    with pytest.raises(ValueError):
        costs.set_setting(st, "caps", {"per_company_cap": -1, "period_cap": 5, "currency": "USD"}, "x")
    with pytest.raises(ValueError):
        costs.set_setting(st, "caps", {"per_company_cap": 1, "period_cap": 5}, "x")           # currency required
    assert costs.period_reset(datetime(2026, 12, 15, tzinfo=timezone.utc)).startswith("2027-01-01")
    assert costs.period_key(datetime(2026, 11, 19, tzinfo=timezone.utc)) == "2026-11"
