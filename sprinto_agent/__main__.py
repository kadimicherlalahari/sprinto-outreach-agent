"""CLI: python -m sprinto_agent <command> [--mock]"""
from __future__ import annotations

import argparse
import json
import sys

from .config import load_settings
from .store import StorageNotConfigured, open_store


def _store(args):
    settings = load_settings(mock=args.mock)
    try:
        return settings, open_store(settings)
    except StorageNotConfigured as e:
        print("ERROR:", e)
        sys.exit(2)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sprinto_agent")
    p.add_argument("--mock", action="store_true", help="use fake fixtures + separate mock DB; no network")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("check-config")
    r = sub.add_parser("run")
    r.add_argument("--domains", default="", help="comma-separated company domains")
    r.add_argument("--no-feeds", action="store_true")
    r.add_argument("--no-search", action="store_true")
    r.add_argument("--max-research", type=int, default=8)
    sub.add_parser("drafts")
    s = sub.add_parser("show"); s.add_argument("draft_id")
    a = sub.add_parser("approve"); a.add_argument("draft_id"); a.add_argument("--reviewer", default="operator")
    j = sub.add_parser("reject"); j.add_argument("draft_id"); j.add_argument("--reviewer", default="operator"); j.add_argument("--note")
    e = sub.add_parser("edit"); e.add_argument("draft_id"); e.add_argument("--subject"); e.add_argument("--body-file", required=True)
    e.add_argument("--reviewer", default="operator")
    sd = sub.add_parser("send"); sd.add_argument("draft_id")
    c = sub.add_parser("add-contact", help="manual Apollo fallback")
    for f in ("company_id",):
        c.add_argument(f)
    c.add_argument("--first", required=True); c.add_argument("--last", required=True); c.add_argument("--title", required=True)
    c.add_argument("--email"); c.add_argument("--note", default="")
    rd = sub.add_parser("redraft", help="regenerate a company's draft from stored evidence"); rd.add_argument("company_id")
    rs = sub.add_parser("resume", help="continue an interrupted/failed run from each company's last stage"); rs.add_argument("run_id")
    rt = sub.add_parser("retry", help="retry one company from its last completed stage"); rt.add_argument("company_id")
    sp = sub.add_parser("suppress", help="opt-out list"); sp.add_argument("action", choices=["add", "remove", "list"])
    sp.add_argument("value", nargs="?"); sp.add_argument("--kind", choices=["domain", "email"], default="domain"); sp.add_argument("--reason", default="")
    pz = sub.add_parser("pause", help="pause or resume all sending"); pz.add_argument("state", choices=["on", "off", "status"])
    pz.add_argument("--reason", default=""); pz.add_argument("--by", default="")
    fb = sub.add_parser("feedback", help="record reviewer feedback (an opinion, never a fact)")
    fb.add_argument("kind", choices=["discovery", "qualification", "research", "poc", "outcome"]); fb.add_argument("verdict")
    fb.add_argument("--company"); fb.add_argument("--evidence"); fb.add_argument("--poc"); fb.add_argument("--send")
    fb.add_argument("--reason", default=""); fb.add_argument("--by", default="operator")
    pr = sub.add_parser("poc-rerun", help="reject the current contact and look again with preferences")
    pr.add_argument("company_id"); pr.add_argument("--reason", default=""); pr.add_argument("--by", default="operator")
    for f in ("preferred-titles", "excluded-titles", "departments", "seniority"):
        pr.add_argument("--" + f, default="")
    sub.add_parser("gmail-auth", help="one-time Google sign-in for Gmail API (you sign in yourself)")
    gs = sub.add_parser("gmail-sync", help="renew the watch and sync the mailbox now"); gs.add_argument("--recovery", action="store_true")
    sub.add_parser("incidents", help="list open incidents")
    ri = sub.add_parser("resolve-incident"); ri.add_argument("incident_id"); ri.add_argument("resolution"); ri.add_argument("--by", default="operator")
    cs = sub.add_parser("costs", help="show or set cost caps / prices / unit limits")
    cs.add_argument("action", choices=["show", "set", "resume"]); cs.add_argument("--currency"); cs.add_argument("--per-company", type=float)
    cs.add_argument("--monthly", type=float); cs.add_argument("--price", action="append", default=[], help="provider=price (gemini: per million tokens)")
    cs.add_argument("--limit", action="append", default=[], help="provider=units per month (when price unknown)")
    cs.add_argument("--reason", default=""); cs.add_argument("--by", default="operator")
    cp = sub.add_parser("campaign", help="pause/resume the pilot campaign or configure it")
    cp.add_argument("action", choices=["pause", "resume", "configure", "status"]); cp.add_argument("--reason", default="")
    cp.add_argument("--start"); cp.add_argument("--min-sample", type=int); cp.add_argument("--max-bounce"); cp.add_argument("--max-opt-out")
    cp.add_argument("--max-negative"); cp.add_argument("--by", default="operator")
    rp = sub.add_parser("report", help="pilot report from stored records"); rp.add_argument("--save", action="store_true")
    rp.add_argument("--json", action="store_true")
    sub.add_parser("flush-outbox", help="save feedback that was queued locally during a database outage")
    rv = sub.add_parser("resolve-send", help="record the outcome of an uncertain send"); rv.add_argument("send_id")
    rv.add_argument("outcome", choices=["sent", "not_sent"])
    sv = sub.add_parser("serve"); sv.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)

    if args.cmd == "check-config":
        st = load_settings(mock=args.mock)
        print(repr(st))
        for k, v in st.missing().items():
            print("  missing %-20s %s" % (k, v))
        return 0
    if args.cmd == "serve":
        import uvicorn
        from .web import create_app
        print("Serving on http://127.0.0.1:%d  (%s mode)" % (args.port, "MOCK" if args.mock else "REAL"))
        uvicorn.run(create_app(mock=args.mock), host="127.0.0.1", port=args.port, log_level="warning")
        return 0

    settings, store = _store(args)
    print("Store:", store.label)
    if args.cmd == "migrate":
        print("Applied:", store.migrate() or "nothing new")
        return 0
    if args.cmd == "run":
        from .pipeline import RunAlreadyActive, build_deps, execute_run, start_run
        deps = build_deps(settings)
        inputs = {"domains": [d.strip() for d in args.domains.split(",") if d.strip()],
                  "use_feeds": not args.no_feeds, "use_search": not args.no_search, "max_research": args.max_research}
        from .costs import CostNotConfigured
        try:
            run_id = start_run(store, settings, inputs, deps.icp)
        except (RunAlreadyActive, CostNotConfigured) as ex:
            print("BLOCKED:", ex)
            return 1
        print("Run", run_id, "started", "(MOCK)" if settings.mock else "")
        summary = execute_run(store, settings, run_id, inputs, deps)
        print("Summary:", json.dumps(summary, indent=2))
        for co in store.companies_for_run(run_id):
            print("- %-18s %-18s stage=%-10s qual=%-9s score=%-4s range=%-7s %s" % (
                (co["name"] or "")[:18], co["domain"][:18], co.get("research_stage") or "-", co["qualification"] or "-",
                co["score"] if co["score"] is not None else "-",
                ("%s-%s" % (co.get("score_min"), co.get("score_max"))) if co.get("score_min") is not None else "-",
                ("[%s] " % co["error_kind"] if co.get("error_kind") else "") + (co["stop_reason"] or co["stage1_reason"] or "")[:90]))
        return 0
    if args.cmd == "drafts":
        for d in store.query("SELECT d.id, d.status, d.version, c.name FROM drafts d JOIN companies c ON c.id=d.company_id ORDER BY d.created_at DESC"):
            print("%s  %-15s v%s  %s" % (d["id"], d["status"], d["version"], d["name"]))
        return 0
    from . import review
    try:
        if args.cmd == "show":
            d = store.draft(args.draft_id)
            print("Status:", d["status"], "v%s" % d["version"])
            print("Subject:", d["current_subject"]); print(); print(d["current_body"])
        elif args.cmd == "approve":
            d = review.approve(store, args.draft_id, args.reviewer, recipient=settings.operator_inbox)
            print("Approved v%s (hash %s...)" % (d["version"], d["approved_hash"][:12]))
        elif args.cmd == "reject":
            review.reject(store, args.draft_id, args.reviewer, args.note); print("Rejected")
        elif args.cmd == "edit":
            body = open(args.body_file, encoding="utf-8").read()
            d0 = store.draft(args.draft_id)
            d = review.edit(store, args.draft_id, args.subject or d0["current_subject"], body, args.reviewer)
            print("Saved v%s; status %s (needs approval again)" % (d["version"], d["status"]))
        elif args.cmd == "send":
            out = review.send(store, settings, args.draft_id)
            print(out["message"])
            if out.get("preview"):
                print(out["preview"])
        elif args.cmd in ("resume", "retry"):
            from .pipeline import RunAlreadyActive, build_deps, resume_run, retry_company
            try:
                fn = resume_run if args.cmd == "resume" else retry_company
                out = fn(store, settings, args.run_id if args.cmd == "resume" else args.company_id, build_deps(settings))
                print("Summary:", json.dumps(out))
            except RunAlreadyActive as ex:
                print("BLOCKED:", ex)
                return 1
        elif args.cmd == "suppress":
            from .safety import add_suppression, list_suppressions, remove_suppression
            if args.action == "list":
                for s in list_suppressions(store):
                    print("%s  %-6s %-30s %s" % (s["id"], s["kind"], s["value"], s.get("reason") or ""))
            elif args.action == "add":
                print("added" if add_suppression(store, args.kind, args.value or "", args.reason) else "already on the list")
            else:
                remove_suppression(store, args.value or ""); print("removed")
        elif args.cmd == "pause":
            from .safety import ResumeRefused, sending_paused, set_sending_paused
            if args.state != "status":
                try:
                    set_sending_paused(store, args.state == "on", args.by or settings.sender_name or "operator", args.reason)
                except ResumeRefused as ex:
                    print("BLOCKED:", ex); return 1
            print("sending paused: %s" % (sending_paused(store) or "no"))
        elif args.cmd == "feedback":
            from .feedback import record_feedback
            print(record_feedback(store, args.kind, args.verdict, args.by, args.reason, company_id=args.company,
                                  evidence_id=args.evidence, poc_id=args.poc, send_id=args.send))
        elif args.cmd == "poc-rerun":
            from .pipeline import build_deps, rerun_contact
            prefs = {"preferred_titles": args.preferred_titles, "excluded_titles": args.excluded_titles,
                     "departments": args.departments, "seniority": args.seniority}
            from .feedback import _clean_list
            print(rerun_contact(store, settings, args.company_id, build_deps(settings), args.by,
                                {k: _clean_list(v) for k, v in prefs.items()}, args.reason))
        elif args.cmd == "gmail-auth":
            from .gmail import authorize
            if not (settings.gmail_client_id and settings.gmail_client_secret):
                print("BLOCKED: set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first"); return 1
            print("Saved authorisation to", authorize(settings.gmail_client_id, settings.gmail_client_secret, settings.gmail_token_path))
        elif args.cmd == "gmail-sync":
            from .gmail import build_client
            from .gmail_sync import _mb_update, ensure_watch, sync
            client = build_client(settings)
            if client is None:
                print("BLOCKED: Gmail API not configured"); return 1
            print("watch:", ensure_watch(store, client, settings.gmail_pubsub_topic))
            if args.recovery:
                _mb_update(store, history_id=None)   # forces a full re-read of tracked threads
            print(sync(store, client, settings, trigger="recovery" if args.recovery else "manual"))
        elif args.cmd == "incidents":
            from .safety import open_incidents
            for i in open_incidents(store):
                print("%s  %-18s %s  %s" % (i["id"], i["kind"], (i["created_at"] or "")[:16], i.get("detail") or ""))
        elif args.cmd == "resolve-incident":
            from .safety import resolve_incident
            resolve_incident(store, args.incident_id, args.by, args.resolution); print("resolved (sending stays paused until resumed)")
        elif args.cmd == "costs":
            from . import costs
            if args.action == "set":
                if args.currency or args.per_company is not None or args.monthly is not None:
                    caps = costs.get_setting(store, "caps")
                    caps.update({k: v for k, v in (("currency", args.currency), ("per_company_cap", args.per_company),
                                                   ("period_cap", args.monthly)) if v is not None})
                    caps["period"] = "month"
                    costs.set_setting(store, "caps", caps, args.by)
                if args.price:
                    pr_ = costs.get_setting(store, "prices")
                    for kv in args.price:
                        k, v = kv.split("=", 1)
                        pr_[k] = {"price": float(v) if v else None}
                    costs.set_setting(store, "prices", pr_, args.by)
                if args.limit:
                    li = costs.get_setting(store, "unit_limits")
                    for kv in args.limit:
                        k, v = kv.split("=", 1)
                        li[k] = float(v)
                    costs.set_setting(store, "unit_limits", li, args.by)
            elif args.action == "resume":
                costs.resume_billable(store, args.reason, args.by); print("billable work resumed")
            from .pipeline import settings_providers
            print(json.dumps({"caps": costs.get_setting(store, "caps"), "prices": costs.get_setting(store, "prices"),
                              "unit_limits": costs.get_setting(store, "unit_limits"), "paused": costs.billable_paused(store),
                              "missing": costs.readiness(store, settings_providers(settings)), "this_month": costs.spend(store)},
                             indent=2))
        elif args.cmd == "campaign":
            from .report import configure_pilot, performance
            from .safety import ResumeRefused, campaign, set_campaign_paused
            if args.action in ("pause", "resume"):
                try:
                    set_campaign_paused(store, "pilot", args.action == "pause", args.by, args.reason)
                except ResumeRefused as ex:
                    print("BLOCKED:", ex); return 1
            elif args.action == "configure":
                f = lambda v: float(v) if v not in (None, "") else None   # noqa: E731
                configure_pilot(store, "pilot", args.by, {"max_bounce_rate": f(args.max_bounce), "max_opt_out_rate": f(args.max_opt_out),
                                                          "max_negative_rate": f(args.max_negative)}, args.min_sample, args.start)
            print(json.dumps({"campaign": campaign(store, "pilot"), "performance": performance(store, "pilot")}, indent=2, default=str))
        elif args.cmd == "report":
            from .report import build_report, save_report
            rep = build_report(store, "pilot")
            if args.save:
                print("saved snapshot", save_report(store, rep))
            if args.json:
                print(json.dumps(rep, indent=2, default=str))
            else:
                w = rep["window"]
                print("%s report · %s" % (rep["kind"].capitalize(), ("%s to %s" % (w["start"], w["as_of"])) if w["started"] else "pilot not started (all records)"))
                for s_ in rep["stages"] + [rep["test_demo"][k] for k in ("send", "replies", "meetings")] + \
                        [rep["prospect"][k] for k in ("send", "replies", "meetings")]:
                    print("  %-52s entered %3d  advanced %3d  rate %-6s of %s" % (
                        s_["stage"][:52], s_["entered"], s_["advanced"],
                        "n/a" if s_["rate"] is None else "%.0f%%" % (100 * s_["rate"]), s_["denominator"]))
                if rep["prospect_note"]:
                    print("  note:", rep["prospect_note"])
                print("  draft edits:", json.dumps(rep["draft_edits"]))
                print("  performance:", rep["performance"]["status"])
        elif args.cmd == "flush-outbox":
            from .feedback import flush_outbox
            print(flush_outbox(store))
        elif args.cmd == "resolve-send":
            r = review.resolve_uncertain(store, args.send_id, args.outcome)
            print("send %s is now %s" % (args.send_id[:8], r["status"]))
        elif args.cmd == "redraft":
            from .pipeline import build_deps, redraft
            print("new draft:", redraft(store, settings, args.company_id, build_deps(settings)))
        elif args.cmd == "add-contact":
            from .webactions import add_manual_contact
            print(add_manual_contact(store, settings, args.company_id, args.first, args.last, args.title, args.email, args.note))
    except (review.ReviewError, review.SendBlocked, ValueError) as e:
        print("BLOCKED:", e)
        return 1
    except Exception as e:
        from .costs import CostNotConfigured
        if isinstance(e, CostNotConfigured):
            print("BLOCKED:", e)
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
