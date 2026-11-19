"""UI for feedback, Gmail tracking, kill switches, cost caps and the pilot report. Registered on the app by
web.create_app. All writes go through the same modules the CLI uses; nothing here bypasses a safety check."""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .util import new_id


def _e(x: Any) -> str:
    import html
    return html.escape("" if x is None else str(x))


def _pct(r: Optional[float]) -> str:
    return "n/a" if r is None else "%.0f%%" % (r * 100) if r * 100 >= 10 or r == 0 else "%.1f%%" % (r * 100)


def _go(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse("%s%smsg=%s" % (path, "&" if "?" in path else "?", quote(msg[:400])), status_code=303)


SELECT = 'style="font:inherit;border:1px solid var(--line-2);border-radius:8px;padding:6px 8px;background:var(--surface)"'


# ---------------------------------------------------------------- snippets used inside existing pages
def company_feedback_html(st: Any, c: Dict[str, Any], evs: List[Dict[str, Any]], reviewer: str) -> str:
    from .feedback import feedback_for
    cid = c["id"]
    rows = feedback_for(st, company_id=cid)
    hist = "".join("""<li style="padding:8px 0;border-top:1px solid var(--line)"><b style="font-weight:560">%s: %s</b>
<span class="muted small"> · %s · %s</span>%s</li>""" % (
        _e(f["kind"].capitalize()), _e(f["verdict"].replace("_", " ")), _e(f["reviewer"]), _e((f["created_at"] or "")[:16].replace("T", " ")),
        ('<div class="small">%s</div>' % _e(f["reason"])) if f.get("reason") else "") for f in rows[:12])
    ev_opts = "".join('<option value="%s">%s: %s</option>' % (_e(x["id"]), _e(x["criterion"].replace("_", " ")),
                                                              _e((x["value"] or "")[:40])) for x in evs if x["status"] != "rejected")
    nonce = new_id()

    def form(kind: str, label: str, options: List[tuple], extra: str = "") -> str:
        opts = "".join('<option value="%s">%s</option>' % (v, l) for v, l in options)
        return """<form method="post" action="/companies/%s/feedback" style="display:grid;gap:6px">
<input type="hidden" name="kind" value="%s"><input type="hidden" name="idem" value="%s-%s"><input type="hidden" name="reviewer" value="%s">
<label class="f small">%s<select name="verdict" %s>%s</select></label>%s
<div style="display:flex;gap:6px"><input type="text" name="reason" placeholder="Reason" aria-label="Reason"><button class="btn sm" type="submit">Save</button></div></form>""" % (
            _e(cid), kind, nonce, kind, _e(reviewer), label, SELECT, opts, extra)

    return """<section class="panel" id="feedback"><div class="ph"><div><h2>Your feedback</h2><p class="muted small">Opinions, kept apart from evidence. They never change the score, the ICP rules or any fact.</p></div></div>
<div class="pb" style="display:grid;gap:14px">
%s%s%s
<details><summary class="small" style="cursor:pointer;color:var(--ink-2)">Feedback history (%d)</summary><ul style="list-style:none;margin:0;padding:0">%s</ul></details></div></section>""" % (
        form("discovery", "Was this company worth finding? <span class='muted'>(found via %s)</span>" % _e((c.get("found_via") or "")[:60]),
             [("relevant", "Relevant"), ("irrelevant", "Irrelevant")]),
        form("qualification", "Is the ICP decision right?",
             [("agree", "Agree with the decision"), ("should_qualify", "Should qualify"), ("should_reject", "Should be rejected"), ("unsure", "Unsure")]),
        form("research", "Research quality problem",
             [("weak_source", "Weak source"), ("wrong_company", "Wrong company"), ("stale_fact", "Stale fact"),
              ("conflicting_fact", "Conflicting fact"), ("missing_evidence", "Missing evidence")],
             '<label class="f small">About<select name="evidence_id" %s><option value="">(whole company)</option>%s</select></label>' % (SELECT, ev_opts)),
        len(rows), hist or '<li class="muted small">None yet.</li>')


def poc_feedback_html(c: Dict[str, Any], poc: Dict[str, Any], reviewer: str, rejected_count: int) -> str:
    return """<details style="border-top:1px solid var(--line);padding-top:10px"><summary class="small" style="cursor:pointer;font-weight:560">Is this the right person?</summary>
<form method="post" action="/companies/%s/poc-feedback" style="display:grid;gap:8px;margin-top:10px">
<input type="hidden" name="poc_id" value="%s"><input type="hidden" name="reviewer" value="%s"><input type="hidden" name="idem" value="%s">
<div style="display:flex;gap:12px" class="small"><label><input type="radio" name="verdict" value="suitable" checked> Suitable</label>
<label><input type="radio" name="verdict" value="unsuitable"> Unsuitable: look again</label></div>
<input type="text" name="reason" placeholder="Reason" aria-label="Reason">
<input type="text" name="preferred_titles" placeholder="Preferred titles (comma-separated)" aria-label="Preferred titles">
<input type="text" name="excluded_titles" placeholder="Exclude titles" aria-label="Excluded titles">
<div class="grid2"><input type="text" name="departments" placeholder="Departments (e.g. it, executive)" aria-label="Departments">
<input type="text" name="seniority" placeholder="Seniority (executive, senior)" aria-label="Seniority"></div>
<p class="muted small">Looking again uses only people Hunter returns with a verified email. Nobody is guessed. %s</p>
<button class="btn sm" type="submit">Save</button></form></details>""" % (
        _e(c["id"]), _e(poc["id"]), _e(reviewer), new_id(),
        ("%d earlier contact%s rejected." % (rejected_count, "" if rejected_count == 1 else "s")) if rejected_count else "")


def revisions_html(st: Any, d: Dict[str, Any]) -> str:
    from .feedback import ensure_base_revision, revisions
    try:
        ensure_base_revision(st, d)
    except Exception:
        pass
    revs = revisions(st, d["id"])
    items = []
    for r in revs:
        cats = json.loads(r.get("categories_json") or "[]")
        diff = json.loads(r.get("diff_json") or "null") or {}
        lines = []
        for l in diff.get("unified", [])[2:40]:
            if l.startswith("+") and not l.startswith("+++"):
                lines.append('<div style="color:var(--good)">%s</div>' % _e(l))
            elif l.startswith("-") and not l.startswith("---"):
                lines.append('<div style="color:var(--bad);text-decoration:line-through">%s</div>' % _e(l))
        subj = diff.get("subject")
        items.append("""<div class="tl %s"><i></i><div><b style="font-weight:560">Revision %d · %s</b> <span class="muted small">by %s · %s</span>
%s%s%s%s</div></div>""" % (
            "edit" if r["kind"] == "edit" else ("approve" if r["kind"] == "approved" else "send" if r["kind"] == "sent" else ""),
            r["revision"], _e(r["kind"]), _e(r["author"]), _e((r["created_at"] or "")[:16].replace("T", " ")),
            (" " + " ".join('<span class="pill t-%s"><i></i>%s</span>' % ("bad" if x == "factual_claim" else "accent", _e(x.replace("_", " ")))
                            for x in cats)) if cats else "",
            ('<div class="small">Subject: <s>%s</s> &rarr; %s</div>' % (_e(subj["from"]), _e(subj["to"]))) if subj else "",
            ('<div class="small muted">%s%% changed</div>' % int(round(100 * diff.get("change_ratio", 0)))) if diff else "",
            ('<div class="quote small" style="font-family:ui-monospace,monospace;white-space:pre-wrap">%s</div>' % "".join(lines)) if lines else ""))
    return """<section class="panel"><div class="ph"><div><h2>Revisions</h2><p class="muted small">First generated draft, every edit with what changed, and the approved / sent version.</p></div></div>
<div class="pb timeline">%s</div></section>""" % ("".join(items) or '<p class="muted small">No revisions recorded.</p>')


def controls_html(st: Any, reviewer: str, compact: bool = False) -> str:
    from .safety import open_incidents, pause_reason, sending_paused
    paused = sending_paused(st)
    inc = open_incidents(st)
    inc_rows = "".join("""<div class="flash bad" style="display:grid;gap:8px"><div><b>%s</b> · %s<br><span class="small">%s</span></div>
<form method="post" action="/controls/incidents/%s/resolve" style="display:flex;gap:6px;flex-wrap:wrap"><input type="hidden" name="by" value="%s">
<input type="text" name="resolution" placeholder="What you checked or changed" aria-label="Resolution" required style="flex:1;min-width:180px">
<button class="btn sm" type="submit">Mark resolved</button></form></div>""" % (
        _e(i["kind"].replace("_", " ").capitalize()), _e((i["created_at"] or "")[:16].replace("T", " ")), _e(i.get("detail")),
        _e(i["id"]), _e(reviewer)) for i in inc)
    if paused:
        ctl = """<form method="post" action="/controls/resume" style="display:grid;gap:8px">
<p class="small"><b>Paused:</b> %s</p><input type="hidden" name="by" value="%s">
<input type="text" name="reason" placeholder="Why it's safe to resume (required)" aria-label="Reason to resume" required>
<label class="small"><input type="checkbox" name="confirm" value="1" required> I reviewed the open incidents and queued drafts</label>
<button class="btn primary" type="submit">Resume sending</button></form>""" % (_e(pause_reason(st) or paused), _e(reviewer))
    else:
        ctl = """<form method="post" action="/settings/pause" style="display:grid;gap:8px"><input type="hidden" name="paused" value="1">
<input type="text" name="reason" placeholder="Reason (optional)" aria-label="Reason to pause">
<button class="btn danger" type="submit">Pause all sending</button></form>"""
    return """<section class="panel"><div class="ph"><div><h2>Send kill switch</h2><p class="muted small">Checked at the final gate right before every send. Pausing is instant; resuming needs a recorded reason and no open incidents.</p></div>%s</div>
<div class="pb" style="display:grid;gap:10px">%s%s</div></section>""" % (
        '<span class="pill t-bad"><i></i>Paused</span>' if paused else '<span class="pill t-good"><i></i>Active</span>', inc_rows, ctl)


# ---------------------------------------------------------------- routes
def register(app: Any, ctx: Any) -> None:
    settings = ctx.settings

    def guard():
        return ctx.guard()

    # ----- feedback -----
    @app.post("/companies/{cid}/feedback")
    def company_feedback(cid: str, kind: str = Form(...), verdict: str = Form(...), reason: str = Form(""),
                         evidence_id: str = Form(""), reviewer: str = Form("operator"), idem: str = Form("")):
        st, err = guard()
        if err:
            return err
        from .feedback import FeedbackError, record_feedback
        if kind not in ("discovery", "qualification", "research"):
            return _go("/companies/%s" % cid, "Not saved: unknown feedback type")
        co = st.one("SELECT * FROM companies WHERE id = %s", (cid,))
        if not co:
            return _go("/pipeline", "Not saved: company not found")
        details: Dict[str, Any] = {}
        if kind == "discovery":
            details = {"found_via": co.get("found_via"), "lead_url": co.get("lead_url")}
        if kind == "qualification":
            details = {"code_decision": co.get("qualification"), "score": co.get("score")}
        ev = None
        if evidence_id:
            ev = st.one("SELECT id, source_url, criterion FROM evidence WHERE id = %s AND company_id = %s", (evidence_id, cid))
            if ev:
                details = {"source_url": ev["source_url"], "criterion": ev["criterion"]}
        try:
            r = record_feedback(st, kind, verdict, reviewer, reason, company_id=cid, evidence_id=ev["id"] if ev else None,
                                details=details, idem_key=("ui-" + idem) if idem else None)
        except FeedbackError as ex:
            return _go("/companies/%s" % cid, "Not saved: %s" % ex)
        msg = {"saved": "Feedback saved. It's an opinion: the evidence and decision are unchanged.",
               "duplicate": "Already saved (duplicate submit ignored).",
               "queued_locally": "Database unavailable: feedback kept locally and will be saved automatically."}[r["status"]]
        return _go("/companies/%s#feedback" % cid, msg)

    @app.post("/companies/{cid}/poc-feedback")
    def poc_feedback(cid: str, poc_id: str = Form(...), verdict: str = Form(...), reason: str = Form(""),
                     preferred_titles: str = Form(""), excluded_titles: str = Form(""), departments: str = Form(""),
                     seniority: str = Form(""), reviewer: str = Form("operator"), idem: str = Form("")):
        st, err = guard()
        if err:
            return err
        from .feedback import FeedbackError, record_feedback
        prefs = {"preferred_titles": preferred_titles, "excluded_titles": excluded_titles, "departments": departments,
                 "seniority": seniority}
        if verdict == "suitable":
            try:
                record_feedback(st, "poc", "suitable", reviewer, reason, company_id=cid, poc_id=poc_id,
                                idem_key=("ui-" + idem) if idem else None)
            except FeedbackError as ex:
                return _go("/companies/%s" % cid, "Not saved: %s" % ex)
            return _go("/companies/%s#contact" % cid, "Saved: contact marked suitable.")
        from .pipeline import active_run, build_deps, rerun_contact
        from .auth import on_vercel
        if on_vercel():
            return _go("/companies/%s" % cid, "Blocked: contact re-runs run in the background and need the local app "
                                              "(python -m sprinto_agent poc-rerun %s)" % cid)
        if active_run(st):
            return _go("/companies/%s" % cid, "Blocked: a run is in progress; try again after it finishes")
        deps = (ctx.deps_factory or build_deps)(settings)

        def work():
            from .feedback import _clean_list
            rerun_contact(st, settings, cid, deps, reviewer, {k: _clean_list(v) for k, v in prefs.items()}, reason)
        ctx.background("poc-" + cid, work)
        return _go("/companies/%s#contact" % cid, "Contact marked unsuitable. Looking again with your preferences; "
                                                  "refresh in a moment.")

    # ----- kill switches -----
    @app.post("/controls/resume")
    def resume(reason: str = Form(""), by: str = Form(""), confirm: str = Form("")):
        st, err = guard()
        if err:
            return err
        from .safety import ResumeRefused, resume_sending
        if confirm != "1":
            return _go("/controls", "Blocked: confirm you reviewed incidents and queued drafts")
        try:
            resume_sending(st, reason, by or ctx.reviewer)
        except ResumeRefused as ex:
            return _go("/controls", "Blocked: %s" % ex)
        return _go("/controls", "Sending resumed. Reason recorded.")

    @app.post("/controls/incidents/{iid}/resolve")
    def resolve_incident(iid: str, resolution: str = Form(""), by: str = Form("")):
        st, err = guard()
        if err:
            return err
        from .safety import resolve_incident as _ri
        try:
            _ri(st, iid, by or ctx.reviewer, resolution)
        except ValueError as ex:
            return _go("/controls", "Not saved: %s" % ex)
        return _go("/controls", "Incident resolved. Sending stays paused until you resume it.")

    @app.post("/controls/campaign")
    def campaign_toggle(action: str = Form(...), reason: str = Form(""), campaign_id: str = Form("pilot")):
        st, err = guard()
        if err:
            return err
        from .safety import ResumeRefused, set_campaign_paused
        try:
            set_campaign_paused(st, campaign_id, action == "pause", ctx.reviewer, reason)
        except ResumeRefused as ex:
            return _go("/controls", "Blocked: %s" % ex)
        return _go("/controls", "Campaign %s." % ("paused" if action == "pause" else "resumed"))

    @app.post("/controls/performance/resume")
    def perf_resume(reason: str = Form(""), campaign_id: str = Form("pilot")):
        st, err = guard()
        if err:
            return err
        from .report import resume_performance
        from .safety import ResumeRefused
        try:
            resume_performance(st, campaign_id, ctx.reviewer, reason)
        except ResumeRefused as ex:
            return _go("/controls", "Blocked: %s" % ex)
        return _go("/controls", "Performance pause lifted. Reason recorded.")

    @app.post("/controls/pilot")
    def pilot(max_bounce_rate: str = Form(""), max_opt_out_rate: str = Form(""), max_negative_rate: str = Form(""),
              min_sample: str = Form(""), pilot_start: str = Form(""), campaign_id: str = Form("pilot")):
        st, err = guard()
        if err:
            return err
        from .report import configure_pilot

        def pct(v: str):
            return float(v) / 100.0 if v.strip() else None
        try:
            configure_pilot(st, campaign_id, ctx.reviewer,
                            {"max_bounce_rate": pct(max_bounce_rate), "max_opt_out_rate": pct(max_opt_out_rate),
                             "max_negative_rate": pct(max_negative_rate)},
                            int(min_sample) if min_sample.strip() else None, pilot_start.strip() or None)
        except ValueError as ex:
            return _go("/controls", "Not saved: %s" % ex)
        return _go("/controls", "Pilot settings saved.")

    @app.post("/controls/costs")
    def cost_settings(currency: str = Form(""), per_company_cap: str = Form(""), period_cap: str = Form(""),
                      tavily_price: str = Form(""), gemini_price: str = Form(""), hunter_price: str = Form(""),
                      tavily_limit: str = Form(""), gemini_limit: str = Form(""), hunter_limit: str = Form("")):
        st, err = guard()
        if err:
            return err
        from . import costs

        def n(v: str):
            return float(v) if v.strip() else None
        try:
            costs.set_setting(st, "caps", {"currency": currency.strip().upper(), "per_company_cap": n(per_company_cap),
                                           "period_cap": n(period_cap), "period": "month"}, ctx.reviewer)
            costs.set_setting(st, "prices", {"tavily": {"price": n(tavily_price)}, "gemini": {"price": n(gemini_price)},
                                             "hunter": {"price": n(hunter_price)}}, ctx.reviewer)
            costs.set_setting(st, "unit_limits", {k: v for k, v in (("tavily", n(tavily_limit)), ("gemini", n(gemini_limit)),
                                                                     ("hunter", n(hunter_limit))) if v is not None}, ctx.reviewer)
        except ValueError as ex:
            return _go("/controls", "Not saved: %s" % ex)
        paused = costs.billable_paused(st)
        return _go("/controls", "Cost limits saved." + (" Billable work is still paused: resume it explicitly below." if paused else ""))

    @app.post("/controls/billable/resume")
    def billable_resume(reason: str = Form("")):
        st, err = guard()
        if err:
            return err
        from . import costs
        try:
            costs.resume_billable(st, reason, ctx.reviewer)
        except ValueError as ex:
            return _go("/controls", "Blocked: %s" % ex)
        return _go("/controls", "Billable work resumed. Paused runs stay paused until you resume each run.")

    @app.post("/controls/style/{gid}")
    def style(gid: str, action: str = Form(...)):
        st, err = guard()
        if err:
            return err
        from .feedback import decide_style_guidance, propose_style_guidance
        if gid == "propose":
            n = propose_style_guidance(st)
            return _go("/controls", "%d new wording suggestion%s from your edits." % (len(n), "" if len(n) == 1 else "s"))
        decide_style_guidance(st, gid, action == "accept", ctx.reviewer)
        return _go("/controls", "Saved.")

    @app.get("/controls", response_class=HTMLResponse)
    @ctx.paged
    def controls(msg: str = ""):
        st, err = guard()
        if err:
            return err
        from . import costs
        from .feedback import source_quality
        from .report import THRESHOLD_KEYS, performance
        from .safety import campaign
        camp = campaign(st, "pilot")
        th = json.loads(camp.get("thresholds_json") or "{}")
        perf = performance(st, "pilot")
        caps, prices, limits = costs.get_setting(st, "caps"), costs.get_setting(st, "prices"), costs.get_setting(st, "unit_limits")
        sp = costs.spend(st)
        from .pipeline import settings_providers
        gaps = costs.readiness(st, settings_providers(settings))
        bp = costs.billable_paused(st)

        def val(v):
            return "" if v is None else _e(("%g" % float(v)))
        prov_rows = "".join("""<tr><td><b style="font-weight:560">%s</b><div class="sub">%s</div></td>
<td><input type="text" name="%s_price" value="%s" placeholder="unknown" aria-label="%s price" style="width:110px"></td>
<td><input type="text" name="%s_limit" value="%s" placeholder="none" aria-label="%s monthly limit" style="width:110px"></td>
<td class="num small">%s</td></tr>""" % (
            p, "price per million tokens" if p == "gemini" else "price per request", p, val((prices.get(p) or {}).get("price")), p,
            p, val(limits.get(p)), p,
            ("%d calls · %s" % (sp["by_provider"][p]["calls"], ("%s %s est." % (round(sp["by_provider"][p]["estimated"], 4), sp["currency"]))
                                 if not sp["by_provider"][p]["unknown_price_calls"] else "%d at unknown price" % sp["by_provider"][p]["unknown_price_calls"]))
            if p in sp["by_provider"] else "–") for p in ("tavily", "gemini", "hunter"))
        cost_html = """<section class="panel" id="costs"><div class="ph"><div><h2>Cost limits</h2><p class="muted small">You set every amount; none are assumed. Paid research doesn't start until these are set. Costs are estimates (your price x units), never shown as exact.</p></div>%s</div>
<form class="pb" method="post" action="/controls/costs" style="display:grid;gap:10px">
<div class="grid2" style="grid-template-columns:repeat(3,minmax(0,1fr))"><label class="f">Currency<input type="text" name="currency" value="%s" placeholder="e.g. USD"></label>
<label class="f">Per company, per run<input type="text" name="per_company_cap" value="%s"></label><label class="f">Per calendar month<input type="text" name="period_cap" value="%s"></label></div>
<div class="scroll"><table class="tbl"><thead><tr><th>Provider</th><th>Unit price</th><th>Monthly unit limit (if price unknown)</th><th>This month</th></tr></thead><tbody>%s</tbody></table></div>
<button class="btn" type="submit">Save cost limits</button>%s</form>
<div class="pb"><p class="small">This month (%s): <b class="num">%s %s</b> estimated%s · resets %s UTC</p>%s</div></section>""" % (
            '<span class="pill t-bad"><i></i>Billable work paused</span>' if bp else ('<span class="pill t-warn"><i></i>Not set</span>' if gaps else '<span class="pill t-good"><i></i>Set</span>'),
            _e(caps.get("currency") or ""), val(caps.get("per_company_cap")), val(caps.get("period_cap")), prov_rows,
            ('<div class="notice small">Before paid research can run: %s</div>' % _e("; ".join(gaps))) if gaps else "",
            _e(sp["period"]), _e(sp["estimated_total"]), _e(sp["currency"]),
            (" + %d calls at unknown price" % sp["unknown_price_calls"]) if sp["unknown_price_calls"] else "",
            _e(sp["reset_at"][:16].replace("T", " ")),
            ("""<form method="post" action="/controls/billable/resume" style="display:grid;gap:6px;margin-top:10px"><p class="small"><b>Paused:</b> %s</p>
<input type="text" name="reason" placeholder="Reason to resume (required)" required aria-label="Reason"><button class="btn primary sm" type="submit">Resume billable work</button>
<p class="muted small">Raising a cap does not resume anything by itself.</p></form>""" % _e(bp)) if bp else "")

        camp_paused = camp.get("status") == "paused"
        thr_inputs = "".join('<label class="f">%s (%%)<input type="text" name="%s" value="%s" placeholder="not set"><span class="muted small" style="font-weight:400">%s</span></label>' % (
            _e(k.replace("max_", "Max ").replace("_", " ")), k, _e("%g" % (th[k] * 100)) if k in th else "", _e(d)) for k, d in THRESHOLD_KEYS.items())
        pilot_html = """<section class="panel" id="pilot"><div class="ph"><div><h2>Pilot campaign</h2><p class="muted small">Thresholds and minimum sample are yours to set before the pilot starts. Nothing is paused below the minimum sample.</p></div>%s</div>
<div class="pb" style="display:grid;gap:12px">
<form method="post" action="/controls/campaign" style="display:flex;gap:8px;flex-wrap:wrap"><input type="hidden" name="action" value="%s">
<input type="text" name="reason" placeholder="%s" %s aria-label="Reason" style="flex:1;min-width:200px"><button class="btn %s sm" type="submit">%s</button></form>
<form method="post" action="/controls/pilot" style="display:grid;gap:10px"><div class="grid2" style="grid-template-columns:repeat(3,minmax(0,1fr))">%s</div>
<div class="grid2"><label class="f">Minimum prospect sends before thresholds apply<input type="text" name="min_sample" value="%s" placeholder="not set"></label>
<label class="f">Pilot start date<input type="date" name="pilot_start" value="%s"></label></div>
<button class="btn" type="submit">Save pilot settings</button></form>
<div class="notice small"><b>Performance check:</b> %s<br>Window %s to %s · denominator: %s (n=%d) · bounces %d, opt-outs %d, negative %d%s</div>%s</div></section>""" % (
            '<span class="pill t-bad"><i></i>Paused</span>' if camp_paused or camp.get("performance_paused") else '<span class="pill t-good"><i></i>Active</span>',
            "resume" if camp_paused else "pause", "Reason (required to resume)" if camp_paused else "Reason (optional)",
            "required" if camp_paused else "", "primary" if camp_paused else "danger",
            "Resume campaign" if camp_paused else "Pause campaign", thr_inputs, _e(camp.get("min_sample") or ""),
            _e(camp.get("pilot_start") or ""), _e(perf["status"]), _e(perf["window"].get("start") or "not started"),
            _e(perf["window"].get("end") or "–"), _e(perf["denominator"]), perf["n"], perf["counts"]["bounced"],
            perf["counts"]["opt_out"], perf["counts"]["negative"],
            ("<br>" + _e("; ".join(perf["breaches"]))) if perf["breaches"] else "",
            ("""<form method="post" action="/controls/performance/resume" style="display:flex;gap:8px"><input type="text" name="reason" required placeholder="Reason to lift the performance pause" aria-label="Reason">
<button class="btn sm" type="submit">Lift performance pause</button></form>""") if camp.get("performance_paused") else "")

        guid = st.query("SELECT * FROM style_guidance ORDER BY created_at DESC")
        g_rows = "".join("""<tr><td><b style="font-weight:560">%s</b><div class="sub">%s · role %s · segment %s</div></td><td>%s</td><td>%s</td></tr>""" % (
            _e(g["rule"].replace("_", " ")), _e(g.get("detail")), _e(g["scope_role"]), _e(g["scope_segment"]),
            '<span class="pill t-%s"><i></i>%s</span>' % ({"accepted": "good", "dismissed": "muted"}.get(g["status"], "accent"), _e(g["status"])),
            ("""<form method="post" action="/controls/style/%s" style="display:flex;gap:6px"><button class="btn sm" name="action" value="accept">Accept</button>
<button class="btn sm" name="action" value="dismiss">Dismiss</button></form>""" % _e(g["id"])) if g["status"] == "proposed" else "") for g in guid)
        style_html = """<section class="panel"><div class="ph"><div><h2>Wording suggestions from your edits</h2><p class="muted small">Only wording (length, greeting, closing line), scoped by role and segment. Never facts. Used only once you accept them.</p></div>
<form method="post" action="/controls/style/propose"><input type="hidden" name="action" value="propose"><button class="btn sm" type="submit">Look for suggestions</button></form></div>%s</section>""" % (
            ('<div class="scroll"><table class="tbl"><tbody>%s</tbody></table></div>' % g_rows) if g_rows else '<div class="empty small">No suggestions yet. They need at least two similar edits.</div>')

        sq = source_quality(st)
        sq_rows = "".join("<tr><td class='small'>%s</td><td class='num'>%d</td><td class='num'>%d</td></tr>" % (
            _e(k), v.get("relevant", 0), v.get("irrelevant", 0)) for k, v in sq["discovery"].items())
        rq_rows = "".join("<tr><td class='small'>%s</td><td class='small'>%s</td><td>%s</td></tr>" % (
            _e(k), _e(", ".join("%s %d" % (a.replace("_", " "), b) for a, b in v.items())),
            '<span class="pill t-warn"><i></i>Demoted</span>' if k in sq["demoted"] else "") for k, v in sq["research"].items())
        quality_html = """<section class="panel"><div class="ph"><div><h2>Source and query quality</h2><p class="muted small">From your discovery and research feedback. A third-party domain flagged weak or wrong-company twice is no longer fetched as evidence.</p></div></div>
<div class="scroll"><table class="tbl"><thead><tr><th>Found via</th><th>Relevant</th><th>Irrelevant</th></tr></thead><tbody>%s</tbody></table></div>
<div class="scroll"><table class="tbl"><thead><tr><th>Source domain</th><th>Flags</th><th></th></tr></thead><tbody>%s</tbody></table></div></section>""" % (
            sq_rows or '<tr><td colspan="3" class="empty small">No discovery feedback yet.</td></tr>',
            rq_rows or '<tr><td colspan="3" class="empty small">No research feedback yet.</td></tr>')

        ev = st.query("SELECT * FROM control_events ORDER BY created_at DESC LIMIT 25")
        ev_rows = "".join("<tr><td class='muted small num'>%s</td><td>%s %s</td><td class='small'>%s</td><td class='small'>%s</td></tr>" % (
            _e((x["created_at"] or "")[:16].replace("T", " ")), _e(x["scope"]), _e(x["action"]), _e(x["reason"]),
            _e("%s (%s)" % (x.get("actor_name") or "", x["actor"]))) for x in ev)
        log_html = """<section class="panel"><details><summary style="padding:16px 20px;cursor:pointer;font-weight:600">Control history (%d)</summary>
<div class="scroll"><table class="tbl"><tbody>%s</tbody></table></div></details></section>""" % (len(ev), ev_rows or '<tr><td class="empty">None yet.</td></tr>')

        body = """<div class="head"><div><h1>Controls</h1><p>Kill switches, cost limits, pilot thresholds and what the agent learns from your feedback.</p></div></div>
<div class="split"><div class="stack">%s%s%s%s</div><div class="rail">%s%s</div></div>""" % (
            cost_html, pilot_html, style_html, quality_html, controls_html(st, ctx.reviewer), log_html)
        return ctx.layout("Controls", "controls", body, msg=msg)

    # ----- outreach + gmail -----
    def gmail_client():
        if ctx.gmail_factory:
            return ctx.gmail_factory()
        from .gmail import build_client
        return build_client(settings)

    @app.post("/outreach/sync")
    def manual_sync():
        st, err = guard()
        if err:
            return err
        client = gmail_client()
        if client is None:
            return _go("/outreach", "Gmail isn't connected: set GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET and run gmail-auth")
        from .gmail_sync import ensure_watch, sync
        ensure_watch(st, client, settings.gmail_pubsub_topic)
        r = sync(st, client, settings, trigger="manual")
        return _go("/outreach", "Sync %s: %d messages checked, %d new events%s" % (
            r["status"], r["seen"], r["added"], (" (%s)" % r["error"]) if r.get("error") else ""))

    @app.post("/sends/{send_id}/check-gmail")
    def check_gmail(send_id: str):
        st, err = guard()
        if err:
            return err
        row = st.one("SELECT draft_id FROM sends WHERE id = %s", (send_id,))
        client = gmail_client()
        if not row or client is None:
            return _go("/outreach", "Gmail isn't connected; check your Sent folder by hand")
        from .gmail import GmailError
        from .review import check_uncertain_in_gmail
        try:
            r = check_uncertain_in_gmail(st, send_id, client)
        except GmailError as ex:
            return _go("/drafts/%s" % row["draft_id"], "Blocked: Gmail check failed (%s)" % ex)
        return _go("/drafts/%s" % row["draft_id"], "Gmail check: %s. Confirm below to resolve." % r["detail"])

    @app.get("/cron/gmail-sync")
    def cron_gmail_sync(request: Request):
        """Vercel Cron: Authorization: Bearer <CRON_SECRET>. Renews the watch, syncs the mailbox, flushes feedback."""
        import hmac
        import os
        want = os.environ.get("CRON_SECRET") or ""
        got = (request.headers.get("authorization") or "").replace("Bearer ", "", 1)
        if not want or not hmac.compare_digest(got, want):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        st = ctx.state["store"]
        client = gmail_client()
        if st is None:
            return JSONResponse({"status": "no database"}, status_code=503)
        from .feedback import flush_outbox
        from .report import performance
        out = {"outbox": flush_outbox(st)}
        if client is not None:
            from .gmail_sync import ensure_watch, sync
            out["watch"] = ensure_watch(st, client, settings.gmail_pubsub_topic)
            out["sync"] = sync(st, client, settings, trigger="poll")
        out["performance"] = performance(st, "pilot", apply=True)["status"]
        return JSONResponse(out)

    @app.post("/gmail/push")
    async def gmail_push(request: Request, token: str = ""):
        """Pub/Sub push endpoint. Requires the shared GMAIL_PUSH_TOKEN in the query string."""
        import hmac
        if not settings.gmail_push_token or not hmac.compare_digest(token, settings.gmail_push_token):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        st = ctx.state["store"]
        client = gmail_client()
        if st is None or client is None:
            return JSONResponse({"status": "unavailable"}, status_code=503)   # Pub/Sub retries later
        try:
            envelope = await request.json()
        except Exception:
            return JSONResponse({"status": "ignored"}, status_code=200)
        from .gmail_sync import handle_push
        try:
            r = handle_push(st, client, settings, envelope)
        except Exception as ex:   # database down: ask Pub/Sub to retry
            return JSONResponse({"status": "retry", "error": type(ex).__name__}, status_code=503)
        return JSONResponse({"status": r.get("status")})

    @app.get("/outreach", response_class=HTMLResponse)
    @ctx.paged
    def outreach(msg: str = ""):
        st, err = guard()
        if err:
            return err
        from .gmail_sync import status as gstatus
        rows = st.query("SELECT s.*, d.current_subject, d.company_id, c.name, c.domain, p.full_name FROM sends s "
                        "JOIN drafts d ON d.id = s.draft_id JOIN companies c ON c.id = d.company_id "
                        "JOIN pocs p ON p.id = d.poc_id ORDER BY s.sent_at DESC")
        tone = {"accepted_by_gmail": "good", "bounced": "bad", "unknown": "warn", "not_sent": "muted"}
        lbl = {"accepted_by_gmail": "Accepted by Gmail", "bounced": "Bounced", "unknown": "Unknown", "not_sent": "Not sent"}
        rtone = {"positive": "good", "question": "accent", "negative": "bad", "opt_out": "bad", "out_of_office": "muted",
                 "other": "muted", "replied": "good"}
        trs = "".join("""<tr><td class="co"><a href="/drafts/%s">%s</a><div class="sub">%s · %s</div></td>
<td>%s<div class="sub">%s%s</div></td><td>%s</td><td class="hide-sm muted small num">%s</td><td>%s</td></tr>""" % (
            _e(r["draft_id"]), _e(r["current_subject"]), _e(r["name"] or r["domain"]), _e(r["full_name"]),
            '<span class="pill t-%s"><i></i>%s</span>' % (tone.get(r.get("delivery_status") or "", "muted"),
                                                          _e(lbl.get(r.get("delivery_status") or "", r["status"].replace("_", " ")))),
            _e(r.get("transport") or ""), (" · thread %s" % _e((r.get("gmail_thread_id") or "")[:10])) if r.get("gmail_thread_id") else "",
            ('<span class="pill t-%s"><i></i>%s</span>' % (rtone.get(r.get("reply_status") or "", "muted"), _e((r.get("reply_status") or "").replace("_", " "))))
            if r.get("reply_status") and r.get("reply_status") != "not_tracked" else '<span class="muted small">no reply</span>',
            _e((r["sent_at"] or "")[:16].replace("T", " ")),
            '<span class="pill t-info"><i></i>Test</span>' if r.get("is_test", True) in (True, 1) else '<span class="pill t-accent"><i></i>Prospect</span>')
            for r in rows)
        gs = gstatus(st)
        mb = gs["mailbox"]
        connected = gmail_client() is not None
        sync_rows = "".join("<tr><td class='muted small num'>%s</td><td>%s</td><td class='small'>%s</td><td class='num small'>%s seen · %s new</td><td class='small'>%s</td></tr>" % (
            _e((x["started_at"] or "")[:16].replace("T", " ")), _e(x["trigger"]),
            '<span class="pill t-%s"><i></i>%s</span>' % ({"ok": "good", "partial": "warn", "failed": "bad"}.get(x["status"], "info"), _e(x["status"])),
            _e(x["messages_seen"]), _e(x["events_added"]), _e(x.get("error") or "")) for x in gs["recent"])
        replies = st.query("SELECT e.*, c.category FROM gmail_events e LEFT JOIN reply_classifications c ON c.event_id = e.id "
                           "WHERE e.event_type <> 'own_sent' ORDER BY e.created_at DESC LIMIT 30")
        rep_rows = "".join("<tr><td>%s<div class='sub'>%s</div></td><td>%s</td><td class='small'>%s</td><td>%s</td></tr>" % (
            _e(x["event_type"].replace("_", " ")), _e((x["created_at"] or "")[:16].replace("T", " ")),
            '<span class="pill t-%s"><i></i>%s</span>' % (rtone.get(x.get("category") or "", "muted"), _e((x.get("category") or "–").replace("_", " "))),
            _e((x.get("snippet") or "")[:160]), '<span class="pill t-info"><i></i>Test</span>' if x.get("is_test", True) in (True, 1) else "")
            for x in replies)
        accepted = sum(1 for r in rows if r.get("delivery_status") == "accepted_by_gmail")
        body = """<div class="head"><div><h1>Outreach</h1><p>What happened after each approved email: Gmail's answer, bounces and replies. Every send so far went to your own inbox, so all of it is <b>test data</b> and is kept out of prospect metrics.</p></div>
<form method="post" action="/outreach/sync"><button class="btn" type="submit" %s>Sync Gmail now</button></form></div>
<div class="grid2" style="grid-template-columns:repeat(4,minmax(0,1fr))">
<div class="panel pb stat" style="padding:16px 20px"><b>%d</b><span>Accepted by Gmail</span></div>
<div class="panel pb stat" style="padding:16px 20px"><b>%d</b><span>Bounced</span></div>
<div class="panel pb stat" style="padding:16px 20px"><b>%d</b><span>Replies (test)</span></div>
<div class="panel pb stat" style="padding:16px 20px"><b>%d</b><span>Dry runs (nothing sent)</span></div></div>
<section class="panel"><div class="ph"><div><h2>Mailbox tracking</h2><p class="muted small">%s</p></div>%s</div>
<div class="pb"><dl class="kv"><dt>Watch</dt><dd>%s%s</dd><dt>Last sync</dt><dd>%s %s%s</dd><dt>Cursor</dt><dd class="num">%s</dd></dl></div>
<details><summary style="padding:0 20px 14px;cursor:pointer" class="small">Recent syncs</summary><div class="scroll"><table class="tbl"><tbody>%s</tbody></table></div></details></section>
<section class="panel"><div class="ph"><h2>Emails</h2><span class="muted small">"Accepted by Gmail" is not proof it reached an inbox</span></div>%s</section>
<section class="panel"><div class="ph"><h2>Replies, bounces and invitations</h2><span class="muted small">Raw events, with the rule-based category shown separately</span></div>%s</section>""" % (
            "" if connected else "disabled", accepted, sum(1 for r in rows if r.get("delivery_status") == "bounced"),
            len([x for x in replies if x["event_type"] == "reply"]), sum(1 for r in rows if r["status"] == "dry_run"),
            "Gmail push notifications trigger a sync; a sync also runs every %g minutes and recovers anything missed." % settings.gmail_sync_minutes
            if connected else "Not connected. Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env, run  python -m sprinto_agent gmail-auth, then restart.",
            '<span class="pill t-good"><i></i>Connected</span>' if connected else '<span class="pill t-muted"><i></i>Not connected</span>',
            _e((mb.get("watch_status") or "not configured").replace("_", " ")),
            (" · " + _e(mb["watch_error"])) if mb.get("watch_error") else "",
            _e((mb.get("last_sync_at") or "never")[:16].replace("T", " ")), _e(mb.get("last_sync_status") or ""),
            (" · " + _e(mb["last_sync_error"])) if mb.get("last_sync_error") else "", _e(mb.get("history_id") or "–"),
            sync_rows or '<tr><td class="empty small">No syncs yet.</td></tr>',
            ("""<div class="scroll"><table class="tbl"><thead><tr><th>Email</th><th>Gmail result</th><th>Reply</th><th class="hide-sm">When (UTC)</th><th>Type</th></tr></thead><tbody>%s</tbody></table></div>""" % trs)
            if rows else '<div class="empty">Nothing sent yet. Approved drafts appear here once you send them.</div>',
            ('<div class="scroll"><table class="tbl"><tbody>%s</tbody></table></div>' % rep_rows) if rep_rows else '<div class="empty small">No replies recorded.</div>')
        return ctx.layout("Outreach", "outreach", body, msg=msg)

    # ----- pilot report -----
    @app.get("/report", response_class=HTMLResponse)
    @ctx.paged
    def report_page(msg: str = ""):
        st, err = guard()
        if err:
            return err
        from .report import build_report
        rep = build_report(st, "pilot")
        w = rep["window"]

        def stage_rows(stages):
            out = []
            for s in stages:
                bd = " · ".join("%s %s" % (k, v if not isinstance(v, dict) else ", ".join("%s %s" % kv for kv in v.items()))
                                for k, v in s["breakdown"].items())
                out.append("""<tr><td><b style="font-weight:560">%s</b><div class="sub">%s</div></td><td class="num">%d</td><td class="num">%d</td>
<td class="num">%d</td><td class="num"><b>%s</b><div class="sub">of %s</div></td></tr>""" % (
                    _e(s["stage"]), _e(bd) + (("<br>" + _e(s["note"])) if s.get("note") else ""), s["entered"], s["advanced"],
                    s["not_advanced"], _pct(s["rate"]), _e(s["denominator"])))
            return "".join(out)
        head = '<thead><tr><th>Stage</th><th>Entered</th><th>Advanced</th><th>Did not</th><th>Rate</th></tr></thead>'
        td, pr = rep["test_demo"], rep["prospect"]
        de = rep["draft_edits"]
        coh = "".join("<tr><td>%s</td><td class='num'>%d</td><td class='num'>%d</td><td class='num'>%d</td><td class='num'>%d</td><td>%s</td></tr>" % (
            _e(k), v["discovered"], v["qualified"], v["drafted"], v["approved"],
            '<span class="pill t-muted"><i></i>Small sample</span>' if v["small_sample"] else _pct(v["qualified_rate"]))
            for k, v in rep["cohorts"]["by_discovery_source"].items())
        body = """<div class="head"><div><h1>Pilot report</h1><p>%s. Every number comes from stored records; missing data shows as n/a, never an estimate.</p></div>
<span class="pill t-%s"><i></i>%s</span></div>
<section class="panel"><div class="ph"><h2>Research pipeline</h2><span class="muted small">Window %s</span></div><div class="scroll"><table class="tbl">%s<tbody>%s</tbody></table></div></section>
<section class="panel"><div class="ph"><div><h2>Draft edits</h2><p class="muted small">%s</p></div></div><div class="pb"><dl class="kv">
<dt>Drafts considered</dt><dd>%d (%d edited)</dd><dt>Change, first to final</dt><dd>median %s · mean %s</dd><dt>What changed</dt><dd>%s</dd></dl></div></section>
<section class="panel"><div class="ph"><div><h2>Sends, replies and meetings: test/demo</h2><p class="muted small">Own-inbox sends. Shown for checking the plumbing only; not prospect results.</p></div><span class="pill t-info"><i></i>Test</span></div>
<div class="scroll"><table class="tbl">%s<tbody>%s</tbody></table></div></section>
<section class="panel"><div class="ph"><h2>Sends, replies and meetings: prospects</h2><span class="pill t-accent"><i></i>Prospect</span></div>
%s<div class="scroll"><table class="tbl">%s<tbody>%s</tbody></table></div></section>
<section class="panel"><div class="ph"><div><h2>By discovery source</h2><p class="muted small">%s</p></div></div><div class="scroll"><table class="tbl">
<thead><tr><th>Source</th><th>Discovered</th><th>Qualified</th><th>Drafted</th><th>Approved</th><th>Qualified rate</th></tr></thead><tbody>%s</tbody></table></div></section>
<section class="panel"><div class="ph"><h2>Notes</h2></div><div class="pb"><ul style="margin:0;padding-left:18px" class="small">%s</ul></div></section>
<form method="post" action="/report/save"><button class="btn" type="submit">Save today's snapshot</button></form>""" % (
            _e(("Pilot day %s of %s, %s to %s" % (w["day"], rep["campaign"].get("pilot_days"), w["start"], w["end"])) if w["started"]
               else "The pilot hasn't started (set a start date in Controls). Showing all records so far"),
            "good" if rep["kind"] == "final" else "warn", "Final" if rep["kind"] == "final" else "Preliminary",
            _e(("%s to %s" % (w["start"], w["as_of"])) if w["started"] else "all records"), head, stage_rows(rep["stages"]),
            _e(de["note"]), de["drafts_considered"], de["edited"],
            _pct(de["median_change_ratio"]), _pct(de["mean_change_ratio"]),
            _e(", ".join("%s %d" % (k.replace("_", " "), v) for k, v in de["categories"].items()) or "none recorded"),
            head, stage_rows([td["send"], td["replies"], td["meetings"]]),
            ('<div class="pb"><div class="notice small">%s</div></div>' % _e(rep["prospect_note"])) if rep["prospect_note"] else "",
            head, stage_rows([pr["send"], pr["replies"], pr["meetings"]]), _e(rep["cohorts"]["note"]),
            coh or '<tr><td colspan="6" class="empty small">No companies in the window.</td></tr>',
            "".join("<li>%s</li>" % _e(n) for n in rep["data_notes"] + ["Performance check: " + rep["performance"]["status"]]))
        return ctx.layout("Pilot report", "report", body, msg=msg)

    @app.post("/report/save")
    def report_save():
        st, err = guard()
        if err:
            return err
        from .report import build_report, save_report
        save_report(st, build_report(st, "pilot"))
        return _go("/report", "Snapshot saved (re-saving today replaces today's snapshot).")


# ---------------------------------------------------------------- background worker (serve only)
def start_worker(ctx: Any, interval_minutes: float) -> Optional[threading.Thread]:
    """Every interval: flush locally-queued feedback, renew the Gmail watch, sync the mailbox, re-check pilot
    performance. Each step is idempotent, so a restart or overlap is harmless."""
    settings = ctx.settings
    if settings.mock:
        return None

    def loop():
        from .feedback import flush_outbox
        from .gmail import build_client
        from .gmail_sync import ensure_watch, sync
        from .report import performance
        while True:
            st = ctx.state.get("store")
            if st is not None:
                try:
                    flush_outbox(st)
                except Exception:
                    pass
                client = build_client(settings)
                if client is not None:
                    try:
                        ensure_watch(st, client, settings.gmail_pubsub_topic)
                        sync(st, client, settings, trigger="poll")
                    except Exception:
                        pass   # recorded on gmail_mailbox / gmail_sync_runs where possible
                try:
                    performance(st, "pilot", apply=True)
                except Exception:
                    pass
            time.sleep(max(60.0, interval_minutes * 60))

    t = threading.Thread(target=loop, daemon=True, name="gmail-sync-worker")
    t.start()
    return t
