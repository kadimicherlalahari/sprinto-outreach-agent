"""Runs the evaluation harness inside the normal test suite: no Critical failure may pass, the harness must catch a
deliberately broken expectation, and a repeated case must give the same verdict with the same side effects."""
import json

from evals import harness
from evals.judge import calibrate


def test_eval_suite_has_no_critical_failures_and_covers_every_decision(tmp_path):
    from evals.decisions import DECISIONS
    cases = harness.load_cases()
    results = harness.run_all(cases)
    s = harness.write_report(results, calibrate(out=tmp_path), tmp_path)
    assert s["critical_failures"] == [], [(r.case.id, r.reason) for r in results if not r.passed]
    assert s["uncovered_decisions"] == []
    assert set(s["by_decision"]) == set(DECISIONS)
    assert s["failed"] == 0, [(r.case.id, r.reason) for r in results if not r.passed]
    assert (tmp_path / "report.md").exists() and json.loads((tmp_path / "results.json").read_text())["summary"]["total"] == len(cases)


def test_harness_detects_a_deliberately_broken_expectation():
    case = next(c for c in harness.load_cases() if c.id == "D25-05")       # at-most-once send (Critical)
    res = harness.run_all([case], break_id="D25-05")[0]
    assert res.passed is False and "gmail_messages" in res.reason
    s = harness.summary([res])
    assert s["live_use_blocked"] is True and s["critical_failures"] == ["D25-05"]


def test_same_case_twice_same_verdict_no_extra_side_effects():
    r = harness.repeat_check("D29-01")
    assert r["first"] is True and r["second"] is True and r["identical"] is True
    assert r["side_effects"]["gmail_messages_sent"] == 1


def test_judge_calibration_logs_disagreements(tmp_path):
    c = calibrate(out=tmp_path)
    log = (tmp_path / "judge_disagreements.jsonl").read_text().strip().splitlines()
    assert c["total"] == 20 and len(log) == c["total"] - c["agree"]
    assert all({"id", "human", "judge", "judge_reason"} <= set(json.loads(l)) for l in log)
