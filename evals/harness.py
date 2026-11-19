"""Evaluation harness for the Sprinto agent.

Each case is data: Input | Expected behaviour | Assertion | Severity, plus a runner that feeds the input through the
application (public interfaces end-to-end, internal functions for unit cases) and returns the observed output.

  * Hard assertions: observed output must match `expect` (equality, or subset match for dicts).
  * Soft assertions: a rubric judge (evals/judge.py) returns a structured verdict with a reason; the case passes
    if the judge's label equals the expected label. The judge is calibrated separately against labelled examples.

Gate: any Critical failure blocks live use, whatever the overall pass rate.
Offline: every case runs with outbound sockets disabled, a fresh temporary store and fake integrations.

Usage:
    python -m evals.harness                      # run all, write evals/out/report.md + results.json
    python -m evals.harness --break D21-01       # deliberately break one expected output (harness self-test)
    python -m evals.harness --repeat D21-01      # run one case twice; verdict and side effects must match
"""
from __future__ import annotations

import argparse
import copy
import json
import socket
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
SEVERITIES = ("Critical", "High", "Low")


@dataclass
class Case:
    id: str
    decision: str                      # D-number from evals/decisions.py
    partition: str                     # equivalence class this case represents
    input: str                         # exact fixture input, human-readable
    expected: str                      # expected behaviour in words
    assertion: str                     # what is checked
    severity: str                      # Critical | High | Low
    run: Callable[[Any], Any]          # (Env) -> observed output
    expect: Any = None                 # hard: value / dict subset; soft: expected judge label
    kind: str = "hard"                 # hard | soft
    boundary: str = ""                 # below | at | above | "" (not a bounded counter)
    judge: Optional[Callable[[Any], Dict[str, Any]]] = None
    assumption: str = ""               # unclear rule recorded instead of invented behaviour


@dataclass
class Result:
    case: Case
    passed: bool
    observed: Any
    reason: str
    seconds: float
    judge: Optional[Dict[str, Any]] = None
    side_effects: Dict[str, int] = field(default_factory=dict)
    na: bool = False                   # case describes a decision the app does not implement


def matches(observed: Any, expect: Any) -> Tuple[bool, str]:
    if isinstance(expect, dict) and isinstance(observed, dict):
        for k, v in expect.items():
            if k not in observed:
                return False, "missing key %r" % k
            ok, why = matches(observed[k], v)
            if not ok:
                return False, "%s: %s" % (k, why)
        return True, "all expected keys match"
    if observed == expect:
        return True, "equal"
    return False, "expected %r, observed %r" % (expect, observed)


class _NoNetwork:
    def __enter__(self):
        self._c, self._cc = socket.socket.connect, socket.create_connection

        def guard(*a, **k):
            raise AssertionError("network access attempted during an offline eval case")
        socket.socket.connect = guard
        socket.create_connection = guard
        return self

    def __exit__(self, *a):
        socket.socket.connect, socket.create_connection = self._c, self._cc
        return False


def _side_effects(env: Any) -> Dict[str, int]:
    st = env.store
    out = {}
    for t in ("sends", "drafts", "pocs", "feedback", "gmail_events", "cost_ledger", "incidents", "suppressions"):
        try:
            out[t] = int(st.one("SELECT COUNT(*) AS n FROM %s" % t)["n"])
        except Exception:
            out[t] = -1
    out["gmail_messages_sent"] = len(getattr(env, "gmail", None).sent_raw) if getattr(env, "gmail", None) else 0
    return out


def run_case(case: Case) -> Result:
    from evals.fixtures import Env
    t0 = time.time()
    if case.kind == "na":
        return Result(case, False, None, "NOT BUILT: " + (case.assumption or "decision not implemented in this app"),
                      0.0, None, {}, na=True)
    with tempfile.TemporaryDirectory() as tmp, _NoNetwork():
        env = Env(tmp)
        try:
            observed = json.loads(json.dumps(case.run(env), default=str))   # plain JSON values (tuples -> lists)
            if case.kind == "soft":
                verdict = case.judge(observed) if case.judge else {"label": None, "reason": "no judge"}
                ok = verdict.get("label") == case.expect
                return Result(case, ok, observed, verdict.get("reason", ""), time.time() - t0, verdict, _side_effects(env))
            ok, why = matches(observed, case.expect)
            return Result(case, ok, observed, why, time.time() - t0, None, _side_effects(env))
        except Exception as e:  # an exception is a failure of that case, never of the harness
            return Result(case, False, None, "exception: %s: %s" % (type(e).__name__, str(e)[:300]),
                          time.time() - t0, None, {})


def load_cases() -> List[Case]:
    from evals.cases import ALL
    cases: List[Case] = []
    for mod in ALL:
        cases += mod.CASES
    ids = [c.id for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError("duplicate case ids: %s" % sorted(dup))
    bad = [c.id for c in cases if c.severity not in SEVERITIES]
    if bad:
        raise ValueError("bad severity on %s" % bad)
    return cases


def break_expectation(expect: Any) -> Any:
    """Change ONE expected value (first key of a dict, or the scalar itself) to a plausible wrong value."""
    def wrong(v):
        if isinstance(v, bool):
            return not v
        if isinstance(v, int):
            return v + 1
        if isinstance(v, str):
            return v + "_broken"
        if isinstance(v, list):
            return v + ["__extra__"]
        return ("__broken__", v)
    if isinstance(expect, dict) and expect:
        e = dict(expect)
        k = sorted(e)[0] if "gmail_messages" not in e else "gmail_messages"
        e[k] = wrong(e[k])
        return e
    return wrong(expect)


def run_all(cases: List[Case], break_id: Optional[str] = None) -> List[Result]:
    results = []
    for c in cases:
        if break_id and c.id == break_id:
            c = copy.copy(c)
            c.expect = break_expectation(c.expect)
        results.append(run_case(c))
    return results


def summary(results: List[Result]) -> Dict[str, Any]:
    from evals.decisions import DECISIONS
    na = [r for r in results if r.na]
    results = [r for r in results if not r.na]
    tot = len(results)
    passed = sum(r.passed for r in results)
    by_sev = {s: {"total": 0, "passed": 0} for s in SEVERITIES}
    for r in results:
        by_sev[r.case.severity]["total"] += 1
        by_sev[r.case.severity]["passed"] += r.passed
    crit_fail = [r.case.id for r in results if not r.passed and r.case.severity == "Critical"]
    by_dec: Dict[str, Dict[str, Any]] = {}
    for d in DECISIONS:
        rs = [r for r in results if r.case.decision == d]
        by_dec[d] = {"cases": len(rs), "passed": sum(r.passed for r in rs),
                     "partitions": sorted({r.case.partition for r in rs}),
                     "boundaries": sorted({r.case.boundary for r in rs if r.case.boundary}),
                     "hard": sum(r.case.kind == "hard" for r in rs), "soft": sum(r.case.kind == "soft" for r in rs)}
    return {"total": tot, "passed": passed, "failed": tot - passed, "by_severity": by_sev,
            "critical_failures": crit_fail, "live_use_blocked": bool(crit_fail), "by_decision": by_dec,
            "uncovered_decisions": [d for d, v in by_dec.items() if not v["cases"] and not DECISIONS[d].get("not_built")],
            "not_built": [r.case.id for r in na],
            "boundary_cases": sum(1 for r in results if r.case.boundary)}


def _cell(x: Any, n: int = 160) -> str:
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    s = s.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def write_report(results: List[Result], calib: Optional[Dict[str, Any]] = None, path: Path = OUT) -> Dict[str, Any]:
    from evals.decisions import DECISIONS
    path.mkdir(parents=True, exist_ok=True)
    s = summary(results)
    lines = ["# Sprinto agent: evaluation report", "",
             "Generated by `python -m evals.harness`. Fake data and mocked integrations only; no network, no real email.", ""]
    gate = ("**LIVE USE BLOCKED**: Critical failures: %s" % ", ".join(s["critical_failures"])) if s["live_use_blocked"] \
        else "**Gate: no Critical failures.** Live use is not blocked by this suite (it does not prove real-world safety)."
    lines += [gate, "",
              "| | Total | Passed | Failed |", "|---|---:|---:|---:|",
              "| All cases | %d | %d | %d |" % (s["total"], s["passed"], s["failed"])]
    for sev in SEVERITIES:
        b = s["by_severity"][sev]
        lines.append("| %s | %d | %d | %d |" % (sev, b["total"], b["passed"], b["total"] - b["passed"]))
    lines += ["", "Boundary cases (below / at / above a cap or threshold): %d" % s["boundary_cases"],
              "Not built (listed, not counted as pass or fail): %d case(s): %s" % (len(s["not_built"]), ", ".join(s["not_built"]) or "none"), ""]
    if calib:
        lines += ["## Soft-judge calibration", "",
                  "Judge vs labelled examples: %d/%d agree (%.0f%%). Disagreements are logged to "
                  "`evals/out/judge_disagreements.jsonl`. Labels: %s." % (
                      calib["agree"], calib["total"], 100.0 * calib["agree"] / max(1, calib["total"]), calib["labelled_by"]), ""]
        for name, v in calib["by_rubric"].items():
            lines.append("- %s: %d/%d agree" % (name, v["agree"], v["total"]))
        lines.append("")
    lines += ["## Coverage by decision", "", "| Decision | Cases | Passed | Partitions | Boundaries | Hard/Soft |",
              "|---|---:|---:|---|---|---|"]
    for d, v in s["by_decision"].items():
        lines.append("| %s %s | %d | %d | %s | %s | %d/%d |" % (d, _cell(DECISIONS[d]["name"], 60), v["cases"], v["passed"],
                                                               _cell(", ".join(v["partitions"]), 140),
                                                               ", ".join(v["boundaries"]) or "–", v["hard"], v["soft"]))
    fails = [r for r in results if not r.passed and not r.na]
    lines += ["", "## Failures", ""]
    if not fails:
        lines.append("None.")
    for r in fails:
        lines.append("- **%s** (%s, %s): %s" % (r.case.id, r.case.severity, r.case.decision, _cell(r.reason, 300)))
    assumptions = [r.case for r in results if r.case.assumption]
    lines += ["", "## Assumptions (unclear rules recorded, not invented)", ""]
    for c in assumptions:
        lines.append("- %s: %s" % (c.id, c.assumption))
    lines += ["", "## Case tables", ""]
    for d in DECISIONS:
        rs = [r for r in results if r.case.decision == d]
        if not rs:
            continue
        lines += ["### %s: %s" % (d, DECISIONS[d]["name"]), "", "_%s_" % DECISIONS[d]["where"], "",
                  "| ID | Input | Expected Behaviour | Assertion | Severity | Type | Result |", "|---|---|---|---|---|---|---|"]
        for r in rs:
            c = r.case
            lines.append("| %s | %s | %s | %s | %s | %s%s | %s |" % (
                c.id, _cell(c.input), _cell(c.expected), _cell(c.assertion), c.severity, c.kind,
                (", " + c.boundary) if c.boundary else "",
                "NOT BUILT" if r.na else ("PASS" if r.passed else "**FAIL**: " + _cell(r.reason, 120))))
        lines.append("")
    (path / "report.md").write_text("\n".join(lines), encoding="utf-8")
    payload = {"summary": s, "calibration": calib, "results": [{
        "id": r.case.id, "decision": r.case.decision, "partition": r.case.partition, "boundary": r.case.boundary,
        "severity": r.case.severity, "kind": r.case.kind, "passed": r.passed, "reason": r.reason,
        "observed": r.observed, "judge": r.judge, "side_effects": r.side_effects, "seconds": round(r.seconds, 3)}
        for r in results]}
    (path / "results.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return s


def repeat_check(case_id: str) -> Dict[str, Any]:
    """Run one case twice in fresh environments: verdict, observed output and side effects must be identical."""
    case = next(c for c in load_cases() if c.id == case_id)
    a, b = run_case(case), run_case(case)
    same = (a.passed == b.passed and json.dumps(a.observed, default=str, sort_keys=True) ==
            json.dumps(b.observed, default=str, sort_keys=True) and a.side_effects == b.side_effects)
    return {"case": case_id, "first": a.passed, "second": b.passed, "identical": same,
            "side_effects": a.side_effects}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="evals.harness")
    p.add_argument("--break", dest="break_id")
    p.add_argument("--repeat")
    p.add_argument("--only", help="comma-separated decision ids")
    args = p.parse_args(argv)
    if args.repeat:
        r = repeat_check(args.repeat)
        print(json.dumps(r, indent=1))
        return 0 if r["identical"] else 1
    cases = load_cases()
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c.decision in keep]
    from evals.judge import calibrate
    calib = calibrate()
    results = run_all(cases, args.break_id)
    out = OUT if not args.break_id else OUT / "broken"
    s = write_report(results, calib, out)
    print("cases %d · passed %d · failed %d · critical failures %s · report %s" % (
        s["total"], s["passed"], s["failed"], s["critical_failures"] or "none", out / "report.md"))
    for r in results:
        if not r.passed and not r.na:
            print("  FAIL %-8s %-8s %s" % (r.case.id, r.case.severity, r.reason[:200]))
    return 1 if s["live_use_blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
