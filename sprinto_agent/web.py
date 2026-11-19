"""Internal UI (FastAPI, server-rendered HTML, binds to 127.0.0.1).

Pages: Overview (needs-you queue, pipeline funnel, live run), Pipeline, Company, Review queue, Draft review,
Outreach (sends + reserved reply/follow-up stages), Runs, Settings."""
from __future__ import annotations

import html
import json
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import review
from .config import load_icp, load_settings, load_sources
from .dashboard import STAGES, CompanyView, action_queue, company_views, funnel, run_progress, stage_details, summary
from .store import StorageNotConfigured, open_store


def e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


# ---------------------------------------------------------------- icons (Lucide-style, 24px grid, 1.75 stroke)
_ICON_PATHS = {
    "overview": '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    "pipeline": '<path d="M3 5h18"/><path d="M6 12h12"/><path d="M10 19h4"/>',
    "review": '<path d="M4 4h16v12H8l-4 4z"/><path d="m9 10 2 2 4-4"/>',
    "outreach": '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
    "runs": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "settings": '<path d="M4 6h10"/><path d="M18 6h2"/><circle cx="16" cy="6" r="2"/><path d="M4 12h4"/><path d="M12 12h8"/><circle cx="10" cy="12" r="2"/><path d="M4 18h12"/><path d="M20 18h0"/><circle cx="18" cy="18" r="2"/>',
    "play": '<path d="M7 4v16l13-8z"/>',
    "chevron": '<path d="m9 6 6 6-6 6"/>',
    "shield": '<path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    "external": '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    "check": '<path d="m5 12 5 5L20 7"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "plug": '<path d="M9 2v6M15 2v6"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v5"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "report": '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M22 20H2"/>',
    "controls": '<path d="M12 3v18"/><rect x="7" y="7" width="10" height="5" rx="1.5"/><path d="M5 17h14"/>',
}


def icon(name: str, cls: str = "ic") -> str:
    return ('<svg class="%s" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">%s</svg>' % (cls, _ICON_PATHS[name]))


def safe_href(url: Optional[str]) -> str:
    """Only http(s) links are rendered as links; anything else (javascript:, data:) becomes inert."""
    u = (url or "").strip()
    return e(u) if u.lower().startswith(("http://", "https://")) else "#"


def pill(label: str, tone: str = "muted") -> str:
    return '<span class="pill t-%s"><i></i>%s</span>' % (e(tone), e(label))


TONE = {"verified": "good", "inferred": "warn", "unknown": "muted", "conflict": "bad", "rejected": "bad",
        "qualified": "good", "uncertain": "warn", "error": "bad", "completed": "good", "running": "info",
        "queued": "info", "failed": "bad", "pending_review": "accent", "approved": "good", "sent": "good",
        "superseded": "muted", "dry_run": "info", "name_only": "warn", "unverified": "warn", "shortlisted": "good"}
LABEL = {"pending_review": "Needs review", "dry_run": "Dry run", "name_only": "Name only", "superseded": "Superseded"}


def spill(status: Optional[str]) -> str:
    s = status or "-"
    return pill(LABEL.get(s, s.replace("_", " ").capitalize()), TONE.get(s, "muted"))


CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700&display=swap');
:root{
  --bg:#f4f4f9;--side:#fafafd;--surface:#fff;--sunk:#f7f7fb;--line:#e5e5ee;--line-2:#d6d6e2;
  --ink:#17182b;--ink-2:#45475f;--ink-3:#686a82;
  --accent:#4c43c9;--accent-ink:#382fa6;--accent-soft:#efeefd;--accent-line:#d8d5f8;
  --good:#17704a;--good-soft:#e6f4ec;--warn:#8a5a06;--warn-soft:#fcf2df;--bad:#ac2f28;--bad-soft:#fcebe9;
  --info:#285a98;--info-soft:#e7effa;--muted-soft:#eeeff4;
  --r:10px;--r-sm:7px;--shadow:0 1px 2px rgba(23,24,43,.05),0 4px 14px -6px rgba(23,24,43,.10);
  --font:'Onest',ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;
}
*{box-sizing:border-box}
html{background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
body{margin:0;font:14px/1.5 var(--font);background:var(--bg);font-feature-settings:"tnum" 0}
::selection{background:var(--accent-line);color:var(--ink)}
a{color:var(--accent-ink);text-decoration-thickness:1px;text-underline-offset:3px}
a:hover{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.ic{width:18px;height:18px;flex:none}
.num{font-variant-numeric:tabular-nums}
h1{font-size:26px;line-height:1.2;letter-spacing:-.02em;margin:0;font-weight:650}
h2{font-size:16px;line-height:1.3;margin:0;font-weight:620;letter-spacing:-.01em}
h3{font-size:14px;margin:0;font-weight:600}
p{margin:0}
.muted{color:var(--ink-3)} .small{font-size:12.5px}
/* ---- shell ---- */
.shell{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100vh}
.side{background:var(--side);border-right:1px solid var(--line);padding:22px 14px;display:flex;flex-direction:column;gap:22px;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:10px;padding:0 8px;text-decoration:none;color:var(--ink)}
.brand .bt b{display:block;font-size:15px;font-weight:650;letter-spacing:-.01em;line-height:1.1}
.brand .bt span{display:block;font-size:12px;color:var(--ink-3)}
.mark{width:32px;height:32px;border-radius:9px;background:var(--accent);color:#fff;display:grid;place-items:center;font-weight:700;font-size:15px}
.nav{display:flex;flex-direction:column;gap:2px}
.nav a{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;color:var(--ink-2);text-decoration:none;font-weight:500}
.nav a:hover{background:var(--muted-soft);color:var(--ink)}
.nav a[aria-current=page]{background:var(--accent-soft);color:var(--accent-ink)}
.nav .count{margin-left:auto;min-width:22px;height:20px;padding:0 6px;border-radius:10px;background:var(--accent);color:#fff;font-size:11.5px;font-weight:600;display:grid;place-items:center}
.nav .soon{margin-left:auto;font-size:11px;color:var(--ink-3)}
.safety{margin-top:auto;border:1px solid var(--line);background:var(--surface);border-radius:var(--r);padding:12px;display:grid;gap:8px;font-size:12.5px}
.safety .row{display:flex;gap:8px;align-items:flex-start;color:var(--ink-2)}
.safety .row .ic{width:16px;height:16px;margin-top:2px;color:var(--ink-3)}
.safety .row b{color:var(--ink);font-weight:600}
.mockbar{background:var(--bad);color:#fff;font-weight:600;font-size:12.5px;padding:7px 14px;border-radius:8px}
main{padding:30px 40px 64px;min-width:0}
.wrap{max-width:1180px;margin:0 auto;display:grid;gap:22px;grid-template-columns:minmax(0,1fr)}
.wrap>*,.stack>*,.rail>*,.split>*{min-width:0}
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
.head p{color:var(--ink-3);margin-top:6px}
.crumb{font-size:13px;color:var(--ink-3);margin-bottom:6px}
.crumb a{color:var(--ink-3);text-decoration:none} .crumb a:hover{color:var(--ink)}
/* ---- surfaces ---- */
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow)}
.panel>.ph{display:flex;align-items:center;justify-content:space-between;gap:8px 12px;padding:16px 20px 12px;flex-wrap:wrap}
.panel>.pb{padding:0 20px 18px}
.split{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:22px;align-items:start}
.stack{display:grid;gap:22px}
.rail{display:grid;gap:16px;position:sticky;top:24px}
/* ---- buttons ---- */
.btn{display:inline-flex;align-items:center;gap:8px;height:38px;padding:0 16px;border-radius:8px;border:1px solid var(--line-2);background:var(--surface);color:var(--ink);font:inherit;font-weight:560;cursor:pointer;text-decoration:none;transition:background .15s,border-color .15s,box-shadow .15s}
.btn:hover{border-color:var(--ink-3);color:var(--ink)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 1px 2px rgba(56,47,166,.3)}
.btn.primary:hover{background:var(--accent-ink);border-color:var(--accent-ink);color:#fff}
.btn.danger{color:var(--bad)} .btn.danger:hover{border-color:var(--bad)}
.btn.sm{height:32px;padding:0 12px;font-size:13px}
.btn[disabled],.btn[aria-disabled=true]{opacity:.45;cursor:not-allowed;box-shadow:none}
.btn .ic{width:16px;height:16px}
.linkbtn{display:inline-flex;align-items:center;gap:4px;font-weight:560;text-decoration:none;white-space:nowrap}
.linkbtn .ic{width:15px;height:15px}
/* ---- pills ---- */
.pill{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px;border-radius:11px;font-size:12px;font-weight:560;white-space:nowrap;background:var(--muted-soft);color:var(--ink-2)}
.pill i{width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.85}
.t-good{background:var(--good-soft);color:var(--good)} .t-warn{background:var(--warn-soft);color:var(--warn)}
.t-bad{background:var(--bad-soft);color:var(--bad)} .t-info{background:var(--info-soft);color:var(--info)}
.t-accent{background:var(--accent-soft);color:var(--accent-ink)} .t-muted{background:var(--muted-soft);color:var(--ink-3)}
/* ---- tables ---- */
.tbl{width:100%;border-collapse:collapse}
.tbl th{font-size:12px;font-weight:560;color:var(--ink-3);text-align:left;padding:10px 20px;background:var(--sunk);border-bottom:1px solid var(--line);white-space:nowrap}
.tbl td{padding:13px 20px;border-bottom:1px solid var(--line);vertical-align:top}
.tbl tr:last-child td{border-bottom:0}
.tbl tbody tr:hover td{background:#fbfbfe}
.tbl .co a{color:var(--ink);font-weight:600;text-decoration:none} .tbl .co a:hover{color:var(--accent-ink)}
.tbl .sub{font-size:12.5px;color:var(--ink-3);margin-top:2px}
.scroll{overflow-x:auto}
.empty{padding:28px 20px;color:var(--ink-3);text-align:center}
/* ---- run launcher ---- */
.launch{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:18px;align-items:center;padding:20px}
.launch .glyph{width:44px;height:44px;border-radius:11px;background:var(--accent-soft);color:var(--accent);display:grid;place-items:center}
.launch .glyph .ic{width:20px;height:20px}
.opts[hidden]{display:none}
.opts{grid-column:1/-1;border-top:1px solid var(--line);margin:0 -20px -20px;padding:16px 20px 20px;background:var(--sunk);border-radius:0 0 var(--r) var(--r);display:grid;gap:14px}
details.more>summary{cursor:pointer;color:var(--accent-ink);font-weight:560;list-style:none;display:inline-flex;gap:6px;align-items:center;margin-top:6px}
details.more>summary::-webkit-details-marker{display:none}
details.more>summary .ic{width:14px;height:14px;transition:transform .15s}
details.more[open]>summary .ic{transform:rotate(90deg)}
label.f{display:grid;gap:6px;font-weight:560;font-size:13px}
textarea,input[type=text],input[type=search],input[type=number]{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line-2);border-radius:8px;padding:9px 11px;width:100%}
textarea{font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:13px;line-height:1.55}
textarea:focus,input:focus{outline:2px solid var(--accent-line);border-color:var(--accent)}
.checks{display:flex;gap:18px;flex-wrap:wrap;align-items:center;color:var(--ink-2)}
.checks label{display:inline-flex;gap:7px;align-items:center}
input[type=checkbox]{accent-color:var(--accent);width:16px;height:16px}
.progress{height:8px;border-radius:4px;background:var(--muted-soft);overflow:hidden}
.progress>span{display:block;height:100%;background:var(--accent);border-radius:4px;transition:width .4s cubic-bezier(.2,.8,.2,1)}
.live{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--info)}
@media (prefers-reduced-motion:reduce){.progress>span{transition:none}}
/* ---- needs-you queue ---- */
.queue{display:grid}
.qrow{display:grid;grid-template-columns:56px minmax(0,1fr) auto;gap:14px;align-items:center;padding:13px 20px;border-top:1px solid var(--line);text-decoration:none;color:inherit}
.qrow:first-child{border-top:0}
a.qrow:hover{background:#fbfbfe}
.qrow .n{font-size:24px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums;text-align:center;border-radius:9px;height:44px;display:grid;place-items:center}
.qrow.zero .n{color:var(--ink-3);background:var(--muted-soft);font-size:18px}
.qrow .n.t-accent,.qrow .n.t-warn,.qrow .n.t-good,.qrow .n.t-bad{font-size:24px}
.qrow h3{font-size:14.5px}
.qrow p{color:var(--ink-3);font-size:13px}
.qrow .go{color:var(--ink-3)}
.qrow.off .n{background:transparent;border:1px dashed var(--line-2);color:var(--ink-3)}
/* ---- funnel ---- */
.funnel{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:2px;padding:4px 20px 18px}
.fcol{display:grid;grid-template-rows:auto 100px auto;gap:10px;text-decoration:none;color:inherit;padding:10px 8px;border-radius:8px;position:relative}
a.fcol:hover{background:var(--sunk)}
.fcol .lbl{font-size:12.5px;color:var(--ink-2);font-weight:560;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fcol .bar{align-self:end;display:flex;align-items:flex-end;height:100px;border-bottom:1px solid var(--line-2)}
.fcol .bar span{display:block;width:100%;max-width:56px;background:var(--accent);border-radius:4px 4px 0 0;min-height:2px}
.fcol .val{font-size:22px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1}
.fcol .rate{font-size:12px;color:var(--ink-3);margin-top:4px;min-height:18px}
.fcol.off .bar span{background:transparent;border:1.5px dashed var(--line-2);border-bottom:0;height:40px}
.fcol.off .val{color:var(--ink-3);font-size:14px;font-weight:560}
.tip{display:none;position:absolute;left:50%;bottom:calc(100% - 6px);transform:translateX(-50%);background:var(--ink);color:#fff;font-size:12px;padding:6px 9px;border-radius:6px;white-space:nowrap;pointer-events:none;z-index:2;animation:tipin .12s ease-out}
.fcol:hover .tip,.fcol:focus-visible .tip{display:block}
.fcol:nth-child(n+7) .tip{left:auto;right:0;transform:none}
@keyframes tipin{from{opacity:0}to{opacity:1}}
/* ---- stage details, compact needs-you ---- */
.stages{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;padding:0 20px 20px;border-top:1px solid var(--line);padding-top:16px}
.scard{border:1px solid var(--line);border-radius:var(--r-sm);padding:10px 12px;background:var(--sunk);display:grid;gap:6px;align-content:start;min-width:0}
.scard .sh{display:flex;justify-content:space-between;gap:8px;font-size:13px}
.scard .sh span{font-weight:600;font-size:12px;color:var(--ink-2);white-space:nowrap}
.scard .sd{font-size:12px;color:var(--ink-3);line-height:1.4}
.scard ul{margin:0;padding:0;list-style:none;display:grid;gap:3px;font-size:12.5px}
.scard li{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.scard li a{text-decoration:none;font-weight:560}
.duo{display:grid;grid-template-columns:minmax(0,340px) minmax(0,1fr);gap:22px;align-items:start}
.needs>.ph{padding:14px 16px 8px}
.minis{display:grid;gap:2px;padding:0 8px 10px}
.mini{display:grid;grid-template-columns:34px minmax(0,1fr) auto;gap:10px;align-items:center;padding:6px 8px;border-radius:7px;text-decoration:none;color:var(--ink);font-size:13px;font-weight:540}
.mini:hover{background:var(--sunk)}
.mini b{height:24px;border-radius:6px;display:grid;place-items:center;font-size:13px;font-weight:650}
.mini b.t-accent{background:var(--accent-soft);color:var(--accent-ink)} .mini b.t-warn{background:var(--warn-soft);color:var(--warn)}
.mini b.t-good{background:var(--good-soft);color:var(--good)} .mini b.t-bad{background:var(--bad-soft);color:var(--bad)}
.mini b.t-muted,.mini b.t-info{background:var(--muted-soft);color:var(--ink-2)}
.mini .ic{width:15px;height:15px;color:var(--ink-3)}
body.loading main{opacity:.55;transition:opacity .15s .1s}
.navbar-progress{position:fixed;top:0;left:0;height:2px;background:var(--accent);width:0;z-index:50;transition:width 1.2s ease-out}
body.loading .navbar-progress{width:70%}
/* ---- stepper ---- */
.steps{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:6px;padding:16px 20px}
.step{display:grid;gap:8px;font-size:12px;color:var(--ink-3);font-weight:560}
.step i{height:6px;border-radius:3px;background:var(--muted-soft)}
.step.on{color:var(--ink)} .step.on i{background:var(--accent)}
.step.off i{background:transparent;border:1.5px dashed var(--line-2);height:6px}
/* ---- misc ---- */
.kv{display:grid;grid-template-columns:120px minmax(0,1fr);gap:8px 14px;font-size:13.5px}
.kv dt{color:var(--ink-3)} .kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
.quote{border-radius:8px;background:var(--sunk);padding:10px 12px;font-size:13.5px;color:var(--ink);line-height:1.5}
.quote+.src{font-size:12px;color:var(--ink-3);margin-top:6px;overflow-wrap:anywhere}
.meter{display:grid;gap:6px}
.meter .track{position:relative;height:10px;border-radius:5px;background:var(--muted-soft)}
.meter .fill{position:absolute;inset:0 auto 0 0;border-radius:5px;background:var(--accent)}
.meter .tick{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--ink);border-radius:1px}
.flash{display:flex;gap:10px;align-items:flex-start;padding:12px 16px;border-radius:var(--r);background:var(--info-soft);color:var(--info);font-weight:500;white-space:pre-wrap}
.flash.bad{background:var(--bad-soft);color:var(--bad)} .flash.good{background:var(--good-soft);color:var(--good)}
.notice{border:1px dashed var(--line-2);border-radius:var(--r);padding:16px 18px;color:var(--ink-2);background:var(--sunk)}
.tabs{display:flex;gap:4px;flex-wrap:wrap}
.tabs a{display:inline-flex;gap:6px;align-items:center;height:32px;padding:0 12px;border-radius:8px;color:var(--ink-2);text-decoration:none;font-weight:560;font-size:13px;border:1px solid transparent}
.tabs a:hover{background:var(--surface);border-color:var(--line)}
.tabs a[aria-current=page]{background:var(--surface);border-color:var(--line-2);color:var(--ink);box-shadow:var(--shadow)}
.tabs .c{font-variant-numeric:tabular-nums;color:var(--ink-3);font-weight:500}
.toolbar{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.searchbox{position:relative;width:260px;max-width:100%}
.searchbox .ic{position:absolute;left:10px;top:50%;transform:translateY(-50%);width:16px;height:16px;color:var(--ink-3)}
.searchbox input{padding-left:34px;height:36px}
.timeline{display:grid;gap:0}
.tl{display:grid;grid-template-columns:18px minmax(0,1fr);gap:12px;padding:8px 0}
.tl i{width:10px;height:10px;border-radius:50%;background:var(--line-2);margin:5px 4px}
.tl.approve i{background:var(--good)} .tl.reject i{background:var(--bad)} .tl.edit i{background:var(--accent)} .tl.send i{background:var(--info)}
.guess{border:1px dashed var(--warn);background:var(--warn-soft);border-radius:8px;padding:10px 12px;font-size:13px;color:var(--ink)}
.guess b{color:var(--warn)}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.stat b{display:block;font-size:20px;font-weight:650;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.stat span{font-size:12.5px;color:var(--ink-3)}
.actions{display:grid;gap:10px}
.actions form{display:grid;gap:8px}
hr.sep{border:0;border-top:1px solid var(--line);margin:4px 0}
/* ---- responsive ---- */
@media (max-width:1080px){.split{grid-template-columns:minmax(0,1fr)}.rail{position:static}}
@media (max-width:860px){
  .shell{grid-template-columns:minmax(0,1fr)}
  .side{position:sticky;top:0;z-index:5;height:auto;flex-direction:row;align-items:center;gap:12px;padding:10px 16px;border-right:0;border-bottom:1px solid var(--line);overflow-x:auto}
  .brand .bt,.safety{display:none}
  .nav{flex-direction:row}
  .nav a{white-space:nowrap;padding:7px 10px}
  .nav a .ic{display:none}
  main{padding:20px 16px 48px}
  .launch{grid-template-columns:minmax(0,1fr)}
  .duo{grid-template-columns:minmax(0,1fr)}
  .launch .glyph{display:none}
  .funnel{grid-template-columns:repeat(4,minmax(0,1fr));row-gap:10px}
  .steps{grid-template-columns:repeat(4,minmax(0,1fr));row-gap:12px}
  .tbl th,.tbl td{padding:11px 14px}
  .hide-sm{display:none}
  .kv{grid-template-columns:100px minmax(0,1fr)}
}
"""

NAV_JS = """<script>
document.addEventListener('click',function(ev){var a=ev.target.closest&&ev.target.closest('a[href^="/"]');
if(a&&!ev.metaKey&&!ev.ctrlKey&&!ev.shiftKey){document.body.classList.add('loading');}});
document.addEventListener('submit',function(){document.body.classList.add('loading');});
window.addEventListener('pageshow',function(){document.body.classList.remove('loading');});
</script>"""

POLL_JS = """
<script>
(function(){
  var last = null;
  function tick(){
    fetch('/api/status',{cache:'no-store'}).then(function(r){return r.json()}).then(function(s){
      var sig = JSON.stringify(s);
      if(last !== null && sig !== last){ location.reload(); return; }
      last = sig;
      if(s.active){ setTimeout(tick, 3000); }
    }).catch(function(){ setTimeout(tick, 6000); });
  }
  tick();
})();
</script>"""


def create_app(mock: bool = False, store: Any = None, settings: Any = None, deps_factory: Any = None,
               gmail_factory: Any = None) -> FastAPI:
    app = FastAPI(title="Sprinto outreach agent", docs_url=None, redoc_url=None)
    settings = settings or load_settings(mock=mock)
    from . import auth
    from fastapi import Request
    from fastapi.responses import PlainTextResponse

    @app.middleware("http")
    async def login_gate(request: Request, call_next):
        g = auth.gate(request.url.path, request.cookies.get(auth.COOKIE))
        if g == "closed":
            return PlainTextResponse("Dashboard locked: set DASHBOARD_PASSWORD in the deployment's environment.", status_code=503)
        if g == "login":
            if request.method != "GET":
                return PlainTextResponse("Login required", status_code=401)
            return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_page(next: str = "/", error: str = ""):
        return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · Sprinto Outreach</title><style>%s</style></head><body><main style="display:grid;place-items:center;min-height:100vh;padding:16px">
<form class="panel" method="post" action="/login" style="width:min(360px,100%%);padding:24px;display:grid;gap:12px">
<h1 style="font-size:22px">Sprinto Outreach</h1><p class="muted small">Enter the dashboard password.</p>%s
<input type="hidden" name="next" value="%s"><label class="f">Password<input type="password" name="password" autofocus required></label>
<button class="btn primary" type="submit">Sign in</button></form></main></body></html>""" % (
            CSS, '<div class="flash bad">Wrong password</div>' if error else "", e(next if next.startswith("/") else "/")))

    @app.post("/login")
    def login_submit(password: str = Form(""), next: str = Form("/")):
        import time as _t
        if not auth.check_password(password):
            _t.sleep(1.0)   # slow down guessing
            return RedirectResponse("/login?error=1", status_code=303)
        r = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
        r.set_cookie(auth.COOKIE, auth.make_token(), max_age=auth.MAX_AGE, httponly=True, samesite="strict",
                     secure=auth.on_vercel())
        return r

    @app.post("/logout")
    def logout():
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(auth.COOKIE)
        return r
    state: Dict[str, Any] = {"store": store, "error": None, "threads": {}}
    if store is None:
        try:
            state["store"] = open_store(settings)
            if not mock:
                state["store"].migrate()
        except StorageNotConfigured as ex:
            state["error"] = str(ex)
        except Exception as ex:
            state["error"] = "Database connection failed (%s). Nothing can be saved." % type(ex).__name__
    reviewer = settings.sender_name or "operator"

    # ------------------------------------------------------------ helpers
    import functools

    def paged(fn):
        """GET page: batch this page's reads into one round trip (see Store.page_reads)."""
        @functools.wraps(fn)
        def w(*a, **k):
            st = state["store"]
            if st is None or not hasattr(st, "page_reads"):
                return fn(*a, **k)
            key = fn.__name__ + "|" + "|".join("%s=%s" % (n, v) for n, v in sorted(k.items()) if n != "msg")
            with st.page_reads(key):
                return fn(*a, **k)
        return w

    def running_ids(st) -> set:
        from .dashboard import snapshot
        return {r["id"] for r in snapshot(st)["runs"] if r["status"] in ("queued", "running")}

    def review_count(st) -> int:
        from .dashboard import snapshot
        return sum(1 for d in snapshot(st)["drafts"] if d["status"] == "pending_review")

    def layout(title: str, active: str, body: str, poll: bool = False, msg: str = "") -> HTMLResponse:
        st = state["store"]
        n_review = review_count(st) if st is not None else 0
        nav = [("overview", "/", "Overview", ""), ("pipeline", "/pipeline", "Pipeline", ""),
               ("review", "/review", "Review queue", ('<span class="count num">%d</span>' % n_review) if n_review else ""),
               ("outreach", "/outreach", "Outreach", ""), ("report", "/report", "Pilot report", ""),
               ("controls", "/controls", "Controls", ('<span class="count num" style="background:var(--bad)">!</span>'
                                                      if st is not None and _paused(st) else "")),
               ("runs", "/runs", "Runs", ""), ("settings", "/settings", "Settings", "")]
        nav_html = "".join('<a href="%s"%s>%s<span>%s</span>%s</a>' % (
            href, ' aria-current="page"' if key == active else "", icon(key), lbl, extra) for key, href, lbl, extra in nav)
        live = settings.send_mode == "live"
        inbox = "set" if settings.operator_inbox else "not set"
        paused_now = st is not None and _paused(st)
        safety = """<div class="safety" aria-label="Sending safety">%s
<div class="row">%s<div><b>%s</b><br>%s</div></div>
<div class="row">%s<div>Mail goes only to <b>your inbox</b> (%s)</div></div>
<div class="row">%s<div>Every email needs your approval</div></div></div>""" % (
            '<div class="row" style="color:var(--bad)">%s<div><b style="color:var(--bad)">Sending paused</b><br><a href="/controls">Review in Controls</a></div></div>' % icon("x")
            if paused_now else "", icon("shield"), "Live sending" if live else "Dry-run mode",
            "Approved drafts are emailed" if live else "Nothing is emailed", icon("mail"), e(inbox), icon("check"))
        mockbar = '<div class="mockbar">Mock mode: fake data, nothing is sent</div>' if mock else ""
        flash = ""
        if msg:
            tone = "bad" if msg.lower().startswith(("blocked", "not saved", "error")) else "good"
            flash = '<div class="flash %s" role="status">%s</div>' % (tone, e(msg))
        page = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s · Sprinto Outreach</title><style>%s</style></head><body><div class="shell">
<aside class="side"><a class="brand" href="/"><span class="mark">S</span><span class="bt"><b>Sprinto</b><span>Outreach agent</span></span></a>
<nav class="nav" aria-label="Main">%s</nav>%s%s</aside>
<main><div class="wrap">%s%s</div></main></div><div class="navbar-progress"></div>%s%s</body></html>""" % (
            e(title), CSS, nav_html, mockbar, safety, flash, body, NAV_JS, POLL_JS if poll else "")
        return HTMLResponse(page)

    def guard():
        if state["store"] is None:
            return None, layout("Storage unavailable", "overview",
                                '<div class="head"><div><h1>Storage unavailable</h1><p>%s</p></div></div>' % e(state["error"]))
        return state["store"], None

    def company_table(views: List[CompanyView], empty: str, show_run: bool = False) -> str:
        if not views:
            return '<div class="empty">%s</div>' % e(empty)
        rows = []
        for v in views:
            r = v.row
            nxt = ('<a class="linkbtn" href="%s">%s%s</a>' % (e(v.next_href), e(v.next_label), icon("chevron"))
                   if v.next_href and v.next_is_action else
                   ('<a class="linkbtn muted" href="%s">%s</a>' % (e(v.next_href), e(v.next_label)) if v.next_href
                    else '<span class="muted">%s</span>' % e(v.next_label)))
            score = '<span class="num">%s</span>' % e(r["score"]) if r.get("score") is not None else '<span class="muted">–</span>'
            contact = e(v.poc["full_name"]) if v.poc else '<span class="muted">–</span>'
            rows.append("""<tr><td class="co"><a href="/companies/%s">%s</a><div class="sub">%s · %s</div></td>
<td>%s</td><td class="hide-sm num">%s</td><td class="hide-sm">%s</td><td class="hide-sm">%s</td><td>%s</td></tr>""" % (
                e(r["id"]), e(r["name"] or r["domain"]), e(r["domain"]), e(_short_source(r["found_via"])),
                pill(v.stage_label, v.tone), "%d verified" % v.signals if r.get("qualification") else "–", score, contact, nxt))
        return """<div class="scroll"><table class="tbl"><thead><tr><th>Company</th><th>Stage</th><th class="hide-sm">ICP signals</th>
<th class="hide-sm">Score</th><th class="hide-sm">Contact</th><th>Next step</th></tr></thead><tbody>%s</tbody></table></div>""" % "".join(rows)

    # ------------------------------------------------------------ overview
    @app.get("/", response_class=HTMLResponse)
    @paged
    def overview(msg: str = ""):
        st, err = guard()
        if err:
            return err
        from .dashboard import snapshot
        snap = snapshot(st)
        if any(r.get("active_lock") for r in snap["runs"]):
            from .pipeline import recover_stale_runs
            if recover_stale_runs(st, settings.stale_run_minutes):
                snap = snapshot(st)
        run_ids = {r["id"] for r in snap["runs"] if r["status"] in ("queued", "running")}
        views = company_views(st, running_run_ids=run_ids, snap=snap)
        latest = snap["runs"][0] if snap["runs"] else None
        active = latest is not None and latest["status"] in ("queued", "running")

        # run launcher / live progress
        if active:
            pr = run_progress(st, latest)
            pct = int(100 * pr["done"] / pr["target"]) if pr["target"] else 8
            launcher = """<section class="panel"><div class="launch"><div class="glyph">%s</div>
<div><h2><span class="live"></span>&nbsp; Run in progress</h2><p class="muted">%s · <span class="num">%d of %d</span> companies researched · <span class="num">%d</span> discovered</p>
<div class="progress" style="margin-top:10px" role="progressbar" aria-valuenow="%d" aria-valuemin="0" aria-valuemax="100"><span style="width:%d%%"></span></div></div>
<a class="btn sm" href="/runs/%s">Watch run</a></div></section>""" % (
                icon("runs"), e(pr["phase"]), pr["done"], pr["target"], pr["discovered"], pct, max(pct, 4), e(latest["id"]))
        else:
            launcher = """<form class="panel" method="post" action="/run"><div class="launch"><div class="glyph">%s</div>
<div><h2>Start a prospecting run</h2><p class="muted">Finds companies from your sources, researches them against the ICP, finds a contact and drafts an email for your review.</p>
<details class="more"><summary>%s Run options</summary></details></div>
<button class="btn primary" type="submit">%s Run prospecting</button>
<div class="opts" hidden>
<label class="f">Company domains <span class="muted small" style="font-weight:400">Optional. One per line or comma-separated. Leave empty to discover from sources.</span>
<textarea name="domains" rows="2" placeholder="acme.com, example.io"></textarea></label>
<div class="checks"><label><input type="checkbox" name="use_feeds" checked> News feeds</label>
<label><input type="checkbox" name="use_search" checked> Tavily search</label>
<label><input type="checkbox" name="use_portfolios" checked> Portfolio pages</label>
<label>Research at most <input type="number" name="max_research" value="5" min="1" max="25" style="width:72px;padding:6px 8px"> companies</label></div>
<p class="muted small">Each qualified company uses one Hunter search (free plan: 50 a month).</p></div></div></form>
<script>(function(){var d=document.querySelector('details.more'),o=document.querySelector('.opts');if(d&&o){d.addEventListener('toggle',function(){o.hidden=!d.open;});}})();</script>""" % (
                icon("play"), icon("chevron"), icon("play"))

        # needs-you: compact list, only what is waiting
        items = action_queue(st, views)
        waiting_items = [it for it in items if it.connected and it.count]
        if waiting_items:
            q_rows = "".join("""<a class="mini" href="%s"><b class="t-%s num">%d</b><span>%s</span>%s</a>""" % (
                e(it.href), e(it.tone), it.count, e(it.label), icon("chevron")) for it in waiting_items)
        else:
            q_rows = '<p class="muted small" style="padding:4px 2px">Nothing is waiting for you.</p>'
        waiting = sum(it.count for it in waiting_items)
        queue = """<section class="panel needs"><div class="ph"><h2>Needs you</h2><span class="muted small">%s</span></div>
<div class="minis">%s</div></section>""" % ("%d waiting" % waiting if waiting else "All caught up", q_rows)

        # pipeline funnel + what is in each stage
        f = funnel(views)
        top = max([s["count"] for s in f if s["count"] is not None] + [1])
        cols = []
        prev = None
        for s in f:
            if not s["live"]:
                cols.append("""<a class="fcol off" href="/outreach"><div class="lbl">%s</div><div class="bar"><span></span></div>
<div><div class="val">Not connected</div><div class="rate">Connect Gmail tracking</div></div><span class="tip">Reply tracking isn't connected yet</span></a>""" % e(s["label"]))
                continue
            h = int(round(96 * s["count"] / top)) if s["count"] else 0
            rate = ""
            if prev is not None and prev["count"]:
                rate = "%d%% of %s" % (round(100.0 * s["count"] / prev["count"]), prev["label"].lower())
            cols.append("""<a class="fcol" href="/pipeline?stage=%s"><div class="lbl">%s</div><div class="bar"><span style="height:%dpx"></span></div>
<div><div class="val num">%d</div><div class="rate">%s</div></div><span class="tip">%d compan%s reached %s</span></a>""" % (
                e(s["key"]), e(s["label"]), max(h, 2), s["count"], e(rate), s["count"], "y" if s["count"] == 1 else "ies",
                e(s["label"].lower())))
            prev = s
        details = "".join(_stage_card(d) for d in stage_details(views, snap))
        funnel_html = """<section class="panel"><div class="ph"><div><h2>Pipeline</h2><p class="muted small">Unique companies across all runs, by the furthest stage they reached. Click a stage to see them.</p></div>
<a class="linkbtn" href="/pipeline">All companies %s</a></div><div class="funnel" role="list">%s</div>
<div class="stages">%s</div></section>""" % (icon("chevron"), "".join(cols), details)

        # latest run
        if latest:
            lv = company_views(st, run_id=latest["id"], running_run_ids=run_ids)
            started = (latest["started_at"] or "")[:16].replace("T", " ")
            run_html = """<section class="panel"><div class="ph"><div><h2>Latest run</h2><p class="muted small">Started %s UTC · %s</p></div>
<div style="display:flex;gap:10px;align-items:center">%s<a class="linkbtn" href="/runs/%s">Run details %s</a></div></div>%s</section>""" % (
                e(started), e(_run_line(summary(latest.get("summary_json")))), spill(latest["status"]), e(latest["id"]),
                icon("chevron"), company_table(lv, "No companies yet."))
        else:
            run_html = """<section class="panel"><div class="empty"><h2 style="margin-bottom:6px">No runs yet</h2>
Start a run above. Try it with one or two company domains first to see how the research and review flow works.</div></section>"""

        body = """<div class="head"><div><h1>Overview</h1><p>What needs you, how the pipeline is moving, and what the agent is doing right now.</p></div></div>
%s<div class="duo">%s%s</div>%s""" % (funnel_html, queue, launcher, run_html)
        return layout("Overview", "overview", body, poll=True, msg=msg)

    @app.get("/api/status")
    def api_status():
        st = state["store"]
        if st is None:
            return JSONResponse({"active": False})
        latest = st.one("SELECT * FROM research_runs ORDER BY started_at DESC LIMIT 1")
        if not latest:
            return JSONResponse({"active": False})
        pr = run_progress(st, latest)
        return JSONResponse({"active": latest["status"] in ("queued", "running"), "run": latest["id"],
                             "status": latest["status"], "done": pr["done"], "discovered": pr["discovered"]})

    @app.post("/run")
    def run(domains: str = Form(""), use_feeds: Optional[str] = Form(None), use_search: Optional[str] = Form(None),
            use_portfolios: Optional[str] = Form(None), max_research: int = Form(5)):
        st, err = guard()
        if err:
            return err
        from .pipeline import RunAlreadyActive, build_deps, execute_run, start_run
        if auth.on_vercel():
            return RedirectResponse("/?msg=" + quote("Blocked: prospecting runs take minutes and can't run on Vercel's serverless "
                                                     "functions. Start them from your machine: python -m sprinto_agent run"), status_code=303)
        deps = (deps_factory or build_deps)(settings)
        inputs = {"domains": [d.strip() for d in domains.replace(",", "\n").splitlines() if d.strip()],
                  "use_feeds": bool(use_feeds), "use_search": bool(use_search), "use_portfolios": bool(use_portfolios),
                  "max_research": max(1, min(int(max_research), 25))}
        from .costs import CostNotConfigured
        try:
            run_id = start_run(st, settings, inputs, deps.icp)
        except RunAlreadyActive as ex:  # double click / second tab: refuse, never duplicate
            return RedirectResponse("/?msg=" + quote("Blocked: %s" % ex), status_code=303)
        except CostNotConfigured as ex:
            return RedirectResponse("/controls?msg=" + quote("Blocked: %s" % ex), status_code=303)

        def work():
            try:
                execute_run(st, settings, run_id, inputs, deps)
            except Exception:
                pass  # failure is recorded on the run row

        t = threading.Thread(target=work, daemon=True)
        state["threads"][run_id] = t
        t.start()
        return RedirectResponse("/", status_code=303)

    def _background(label: str, fn) -> None:
        def work():
            try:
                fn()
            except Exception:
                pass  # outcome is recorded on the run/company rows
        t = threading.Thread(target=work, daemon=True, name=label)
        state["threads"][label] = t
        t.start()

    @app.post("/runs/{run_id}/resume")
    def resume(run_id: str):
        st, err = guard()
        if err:
            return err
        from .pipeline import RunAlreadyActive, build_deps, recover_stale_runs, resume_run, active_run
        recover_stale_runs(st, settings.stale_run_minutes)
        if active_run(st):
            return RedirectResponse("/runs/%s?msg=%s" % (run_id, quote("Blocked: another run is in progress")), status_code=303)
        from .costs import CostNotConfigured, require_ready
        from .pipeline import settings_providers
        try:
            require_ready(st, settings_providers(settings))
        except CostNotConfigured as ex:
            return RedirectResponse("/runs/%s?msg=%s" % (run_id, quote("Blocked: %s" % ex)), status_code=303)
        deps = (deps_factory or build_deps)(settings)
        _background("resume-" + run_id, lambda: resume_run(st, settings, run_id, deps))
        return RedirectResponse("/runs/%s?msg=%s" % (run_id, quote("Resuming from each company's last completed stage.")), status_code=303)

    @app.post("/companies/{cid}/retry")
    def retry(cid: str):
        st, err = guard()
        if err:
            return err
        from .pipeline import active_run, build_deps, recover_stale_runs, retry_company
        recover_stale_runs(st, settings.stale_run_minutes)
        if active_run(st):
            return RedirectResponse("/companies/%s?msg=%s" % (cid, quote("Blocked: a run is in progress; retry after it finishes")), status_code=303)
        from .costs import CostNotConfigured, require_ready
        from .pipeline import settings_providers
        try:
            require_ready(st, settings_providers(settings))
        except CostNotConfigured as ex:
            return RedirectResponse("/companies/%s?msg=%s" % (cid, quote("Blocked: %s" % ex)), status_code=303)
        deps = (deps_factory or build_deps)(settings)
        _background("retry-" + cid, lambda: retry_company(st, settings, cid, deps))
        return RedirectResponse("/companies/%s?msg=%s" % (cid, quote("Retrying from the last completed stage. Refresh in a moment.")), status_code=303)

    # ------------------------------------------------------------ pipeline
    FILTERS = [("all", "All"), ("needs_contact", "Needs contact"), ("drafted", "Draft ready"), ("qualified", "Qualified"),
               ("uncertain", "Uncertain"), ("rejected", "Rejected"), ("skipped", "Skipped"), ("error", "Failed")]

    def _match(v: CompanyView, f: str) -> bool:
        if f in ("all", ""):
            return True
        if f == "needs_contact":
            return v.stage == "qualified"
        if f == "drafted":
            return v.stage in ("drafted", "approved")
        if f == "qualified":
            return "qualified" in v.reached
        if f in [k for k, _, _ in STAGES]:
            return f in v.reached
        return v.stage == f

    @app.get("/pipeline", response_class=HTMLResponse)
    @paged
    def pipeline(stage: str = "all", q: str = ""):
        st, err = guard()
        if err:
            return err
        views = company_views(st, running_run_ids=running_ids(st))
        tabs = "".join('<a href="/pipeline?%s"%s>%s <span class="c">%d</span></a>' % (
            urlencode({"stage": k, "q": q}), ' aria-current="page"' if k == stage else "", lbl,
            sum(1 for v in views if _match(v, k))) for k, lbl in FILTERS)
        stage_name = dict((k, l) for k, l, _ in STAGES).get(stage)
        if stage_name and stage not in dict(FILTERS):
            tabs += '<a href="/pipeline?stage=%s" aria-current="page">Reached: %s</a>' % (e(stage), e(stage_name))
        shown = [v for v in views if _match(v, stage)]
        if q:
            ql = q.lower()
            shown = [v for v in shown if ql in (v.row["name"] or "").lower() or ql in v.row["domain"].lower()]
        order = {"error": 0, "drafted": 1, "approved": 2, "qualified": 3, "contact": 4, "sent": 5, "uncertain": 6,
                 "researching": 7, "rejected": 8, "discovered": 9, "skipped": 10}
        shown.sort(key=lambda v: (order.get(v.stage, 9), -(v.row.get("score") or 0)))
        body = """<div class="head"><div><h1>Pipeline</h1><p>Every company the agent has found, its current stage, and what happens next.</p></div></div>
<div class="toolbar"><nav class="tabs" aria-label="Filter by stage">%s</nav>
<form class="searchbox" method="get" action="/pipeline">%s<input type="hidden" name="stage" value="%s"><input type="search" name="q" value="%s" placeholder="Search company or domain" aria-label="Search companies"></form></div>
<section class="panel">%s</section>""" % (tabs, icon("search"), e(stage), e(q),
                                          company_table(shown, "No companies match this filter."))
        return layout("Pipeline", "pipeline", body, poll=bool(running_ids(st)))

    # ------------------------------------------------------------ company
    @app.get("/companies/{cid}", response_class=HTMLResponse)
    @paged
    def company_view(cid: str, msg: str = ""):
        st, err = guard()
        if err:
            return err
        c = st.one("SELECT * FROM companies WHERE id = %s", (cid,))
        if not c:
            return layout("Not found", "pipeline", '<div class="head"><h1>Company not found</h1></div>')
        v = [x for x in company_views(st, run_id=c["run_id"], running_run_ids=running_ids(st)) if x.row["id"] == cid][0]
        a = json.loads(c["assessment_json"]) if c.get("assessment_json") else {}
        icp = load_icp()
        threshold = int(icp.get("scoring", {}).get("qualify_threshold", 60))

        steps = "".join('<div class="step%s"><i></i>%s</div>' % (
            " on" if k in v.reached else (" off" if not live else ""), e(lbl)) for k, lbl, live in STAGES)
        action = ('<a class="btn primary" href="%s">%s</a>' % (e(v.next_href), e(v.next_label))
                  if v.next_is_action and v.next_href and not v.next_href.startswith("/companies/") else "")
        if v.stage == "qualified":
            action = '<a class="btn primary" href="#contact">Add Apollo contact</a>'

        # decision
        score = c.get("score")
        if score is not None:
            mx = max(int(a.get("max_possible") or 0), threshold, int(score), 1)
            meter = """<div class="meter"><div style="display:flex;justify-content:space-between"><b class="num">Score %d</b><span class="muted small num">qualifies at %d · best case %s</span></div>
<div class="track"><div class="fill" style="width:%d%%"></div><div class="tick" style="left:calc(%d%% - 1px)" title="Threshold %d"></div></div></div>""" % (
                int(score), threshold, e(a.get("max_possible")), min(100, int(100 * int(score) / mx)), int(100 * threshold / mx), threshold)
        else:
            meter = '<p class="muted">Not researched yet.</p>'
        reasons = "".join("<li>%s</li>" % e(r) for r in (a.get("reasons") or []))
        budget = json.loads(c.get("budget_json") or "{}")
        budget_txt = " · ".join("%s %s/%s" % (k.replace("_", " "), v[0], v[1]) for k, v in budget.items()) or "–"
        could = c.get("could_change")
        range_txt = ("%s to %s" % (c.get("score_min"), c.get("score_max"))) if c.get("score_min") is not None else "–"
        err_html = ""
        if c.get("error_kind"):
            err_html = """<div class="flash bad">Stopped by a %s problem: %s%s</div>""" % (
                e(c["error_kind"].replace("_", " ")), e(c.get("error") or c.get("stop_reason") or ""),
                ("""<form method="post" action="/companies/%s/retry" style="margin-top:8px"><button class="btn sm" type="submit">Retry from last completed stage</button></form>""" % e(cid))
                if c.get("retryable") else "")
        decision = """<section class="panel"><div class="ph"><h2>Decision</h2>%s</div><div class="pb" style="display:grid;gap:14px">%s%s
%s<dl class="kv"><dt>Why it stopped</dt><dd>%s</dd><dt>Could more evidence change it?</dt><dd>%s</dd><dt>Score range</dt><dd class="num">%s</dd>
<dt>Research stage</dt><dd>%s</dd><dt>Budget used</dt><dd class="small">%s</dd>
<dt>Identity check</dt><dd>%s</dd><dt>Found via</dt><dd>%s%s</dd><dt>Discovery</dt><dd>%s %s</dd></dl></div></section>""" % (
            spill(c.get("qualification") or "not researched"), err_html, meter,
            ("<ul style='margin:0;padding-left:18px'>%s</ul>" % reasons) if reasons else "",
            e(c.get("stop_reason") or "–"),
            ("Yes: unresolved: %s" % e(", ".join(a.get("open_questions") or []) or "see criteria")) if could else ("No" if could is not None else "–"),
            e(range_txt), e((c.get("research_stage") or "–").replace("_", " ")), e(budget_txt),
            e(" — ".join(a.get("identity") or []) or "–"), e(c["found_via"]),
            (' · <a href="%s" rel="noreferrer" target="_blank">source article</a>' % safe_href(c["lead_url"])) if c.get("lead_url") else "",
            spill(c["stage1_status"]), e(c["stage1_reason"]))

        crit_rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td class='hide-sm muted small'>%s</td></tr>" % (
            e(k.replace("_", " ").capitalize()), spill(val["status"]), e(val.get("value") or "–"), e(val.get("note")))
            for k, val in (a.get("criteria") or {}).items())
        crit = """<section class="panel"><div class="ph"><h2>ICP criteria</h2><span class="muted small">Verified = backed by a quote from a credible source</span></div>
<div class="scroll"><table class="tbl"><thead><tr><th>Criterion</th><th>Status</th><th>Value</th><th class="hide-sm">Note</th></tr></thead><tbody>%s</tbody></table></div></section>""" % (
            crit_rows or '<tr><td colspan="4" class="empty">No assessment yet.</td></tr>')

        evs = st.evidence_for(cid)
        used = [x for x in evs if x["status"] != "rejected"]
        rej = [x for x in evs if x["status"] == "rejected"]
        def flags_html(x):
            fl = json.loads(x.get("flags_json") or "[]")
            return " ".join(pill(f.replace("_", " "), "bad" if f in ("conflict", "identity_mismatch") else "warn") for f in fl)
        ev_rows = "".join("""<tr><td><b style="font-weight:560">%s</b><div class="sub">%s</div></td><td>%s<div class="sub">Tier %s · %s<br>%s confidence</div></td>
<td><div class="quote">%s</div><div class="src"><a href="%s" rel="noreferrer" target="_blank">%s</a> · published %s · retrieved %s</div><div style="margin-top:6px">%s</div></td></tr>""" % (
            e(x["criterion"].replace("_", " ").capitalize()), e(x["value"]), spill(x["status"]), e(x["source_tier"]),
            e((x.get("source_type") or "").replace("_", " ")), e(x.get("confidence") or "–"), e(x["quote"][:320]),
            safe_href(x["source_url"]), e(_short_url(x["source_url"])), e((x["published_at"] or "")[:10] or "date unknown"),
            e((x["retrieved_at"] or "")[:10]), flags_html(x))
            for x in used)
        rej_rows = "".join("<tr><td>%s</td><td class='small'>%s</td><td><div class='quote'>%s</div><div class='src'>%s</div></td></tr>" % (
            e(x["criterion"].replace("_", " ")), e(x["note"]), e(x["quote"][:220]), e(_short_url(x["source_url"]))) for x in rej)
        evidence = """<section class="panel"><div class="ph"><h2>Evidence used</h2><span class="muted small num">%d quotes</span></div>
<div class="scroll"><table class="tbl"><thead><tr><th>Criterion</th><th>Status</th><th>Quote and source</th></tr></thead><tbody>%s</tbody></table></div>
%s</section>""" % (len(used), ev_rows or '<tr><td colspan="3" class="empty">No evidence collected.</td></tr>',
                   ("""<details style="border-top:1px solid var(--line)"><summary style="padding:14px 20px;cursor:pointer;color:var(--ink-2);font-weight:560">Not used: %d candidate quotes and why</summary>
<div class="scroll"><table class="tbl"><tbody>%s</tbody></table></div></details>""" % (len(rej), rej_rows)) if rej else "")

        # rail: contact + draft
        from .pipeline import active_pocs
        from .web_extra import company_feedback_html, poc_feedback_html
        all_pocs = st.pocs_for(cid)
        pocs = active_pocs(st, cid)
        n_rejected = sum(1 for x in all_pocs if x.get("status") == "rejected")
        if pocs:
            p = pocs[-1]
            ver = json.loads(p["verification_json"] or "{}")
            ver_html = ('<div class="quote">%s</div><div class="src"><a href="%s" rel="noreferrer" target="_blank">%s</a></div>' % (
                e(ver.get("quote")), e(ver.get("url")), e(_short_url(ver.get("url") or ""))) if ver.get("url")
                else '<p class="muted small">%s</p>' % e(ver.get("note") or "Not verified on a public page."))
            contact = """<section class="panel" id="contact"><div class="ph"><h2>Contact</h2>%s</div><div class="pb" style="display:grid;gap:12px">
<div style="display:flex;gap:12px;align-items:center"><div class="glyph" style="width:40px;height:40px;border-radius:50%%;background:var(--accent-soft);color:var(--accent);display:grid;place-items:center">%s</div>
<div><b>%s</b><div class="muted small">%s · %s</div></div></div>
<dl class="kv"><dt>Source</dt><dd>%s</dd><dt>Email</dt><dd>%s</dd></dl>%s%s</div></section>""" % (
                spill(p["verification_status"]), icon("user"), e(p["full_name"]), e(p["title"]), e(p["role_label"]),
                "Hunter" if p["source"] == "hunter" else "Manual (Apollo)",
                "on file, never used as a recipient" if p.get("email") else "not provided", ver_html,
                poc_feedback_html(c, p, reviewer, n_rejected).replace("%", "%%") if c.get("qualification") == "qualified" else "")
        elif c.get("qualification") == "qualified":
            contact = """<section class="panel" id="contact"><div class="ph"><h2>Add a contact</h2>%s</div>
<form class="pb" method="post" action="/companies/%s/contact" style="display:grid;gap:10px">
<p class="muted small">Hunter found no suitable person. Look them up in Apollo and enter only what you verified. Nothing is guessed.</p>
<div class="grid2"><label class="f">First name<input type="text" name="first" required></label><label class="f">Last name<input type="text" name="last" required></label></div>
<label class="f">Title<input type="text" name="title" required placeholder="CTO, Head of Security…"></label>
<label class="f">Email <span class="muted small" style="font-weight:400">optional, must be on %s</span><input type="text" name="email"></label>
<label class="f">Apollo reference<input type="text" name="note" placeholder="Where you found them"></label>
<button class="btn primary" type="submit">Save contact and draft email</button></form></section>""" % (
                pill("Needed", "warn"), e(cid), e(c["domain"]))
        else:
            contact = """<section class="panel" id="contact"><div class="ph"><h2>Contact</h2></div><div class="pb muted small">
A contact is looked up only after a company qualifies.</div></section>"""
        drafts = st.drafts_for(cid)
        d_rows = "".join('<li style="display:flex;justify-content:space-between;gap:8px;padding:8px 0;border-top:1px solid var(--line)"><a href="/drafts/%s">%s</a>%s</li>' % (
            e(d["id"]), e(d["current_subject"]), spill(d["status"])) for d in reversed(drafts))
        draft_panel = """<section class="panel"><div class="ph"><h2>Email drafts</h2></div><div class="pb">%s</div></section>""" % (
            ('<ul style="list-style:none;margin:0;padding:0">%s</ul>' % d_rows) if d_rows else '<p class="muted small">No drafts yet.</p>')

        body = """<div><div class="crumb"><a href="/pipeline">Pipeline</a> / %s</div>
<div class="head"><div><h1>%s</h1><p><a href="https://%s" rel="noreferrer" target="_blank">%s</a> · %s</p></div>%s</div></div>
<section class="panel"><div class="steps" aria-label="Pipeline progress">%s</div></section>
<div class="split"><div class="stack">%s%s%s</div><div class="rail">%s%s%s</div></div>""" % (
            e(c["name"] or c["domain"]), e(c["name"] or c["domain"]), e(c["domain"]), e(c["domain"]), pill(v.stage_label, v.tone),
            action, steps, decision, crit, evidence, contact, draft_panel, company_feedback_html(st, c, evs, reviewer))
        return layout(c["name"] or c["domain"], "pipeline", body, msg=msg)

    @app.post("/companies/{cid}/contact")
    def add_contact(cid: str, first: str = Form(""), last: str = Form(""), title: str = Form(""),
                    email: str = Form(""), note: str = Form("")):
        st, err = guard()
        if err:
            return err
        from .webactions import add_manual_contact
        try:
            msg = "Saved: " + add_manual_contact(st, settings, cid, first, last, title, email or None, note)
        except ValueError as ex:
            msg = "Not saved: %s" % ex
        return RedirectResponse("/companies/%s?msg=%s" % (cid, quote(msg)), status_code=303)

    # ------------------------------------------------------------ review queue
    @app.get("/review", response_class=HTMLResponse)
    @paged
    def review_queue(status: str = "pending_review"):
        st, err = guard()
        if err:
            return err
        groups = [("pending_review", "Needs review"), ("approved", "Approved"), ("rejected", "Rejected"), ("sent", "Sent"),
                  ("superseded", "Superseded")]
        counts = {k: len(st.query("SELECT id FROM drafts WHERE status = %s", (k,))) for k, _ in groups}
        tabs = "".join('<a href="/review?status=%s"%s>%s <span class="c">%d</span></a>' % (
            k, ' aria-current="page"' if k == status else "", l, counts[k]) for k, l in groups)
        rows = st.query("SELECT d.*, c.name, c.domain, p.full_name, p.title FROM drafts d JOIN companies c ON c.id = d.company_id "
                        "JOIN pocs p ON p.id = d.poc_id WHERE d.status = %s ORDER BY d.updated_at DESC", (status,))
        trs = "".join("""<tr><td class="co"><a href="/drafts/%s">%s</a><div class="sub">%s</div></td><td>%s<div class="sub">%s</div></td>
<td class="hide-sm num">v%s</td><td class="hide-sm muted small">%s</td><td><a class="linkbtn" href="/drafts/%s">%s%s</a></td></tr>""" % (
            e(r["id"]), e(r["current_subject"]), e(r["name"] or r["domain"]), e(r["full_name"]), e(r["title"]), e(r["version"]),
            e((r["updated_at"] or "")[:16].replace("T", " ")), e(r["id"]),
            "Review" if status == "pending_review" else ("Send" if status == "approved" else "Open"), icon("chevron")) for r in rows)
        empty = {"pending_review": "No drafts waiting. New drafts appear here after a run qualifies a company and finds a contact.",
                 "approved": "No approved drafts waiting to be sent."}.get(status, "Nothing here.")
        body = """<div class="head"><div><h1>Review queue</h1><p>Each email is sent only after you approve that exact version, and only to your own inbox.</p></div></div>
<nav class="tabs" aria-label="Draft status">%s</nav><section class="panel">%s</section>""" % (
            tabs, ("""<div class="scroll"><table class="tbl"><thead><tr><th>Subject</th><th>Contact</th><th class="hide-sm">Version</th>
<th class="hide-sm">Updated</th><th></th></tr></thead><tbody>%s</tbody></table></div>""" % trs) if rows else '<div class="empty">%s</div>' % e(empty))
        return layout("Review queue", "review", body)

    # ------------------------------------------------------------ draft review
    @app.get("/drafts/{did}", response_class=HTMLResponse)
    @paged
    def draft_view(did: str, msg: str = ""):
        st, err = guard()
        if err:
            return err
        d = st.draft(did)
        if not d:
            return layout("Not found", "review", '<div class="head"><h1>Draft not found</h1></div>')
        c = st.one("SELECT * FROM companies WHERE id = %s", (d["company_id"],)) or {}
        poc = st.one("SELECT * FROM pocs WHERE id = %s", (d["poc_id"],)) or {}
        hook = st.one("SELECT * FROM evidence WHERE id = %s", (d["hook_evidence_id"],)) if d.get("hook_evidence_id") else None
        sends = st.sends_for(did)
        reviews = st.reviews_for(did)
        approved_ok = d["status"] == "approved" and d["approved_hash"] == d["current_hash"]
        live = settings.send_mode == "live"
        locked = d["status"] in ("sent", "superseded")
        pains = json.loads(d["pain_points_json"] or "[]")
        guidance = json.loads(d["guidance_json"] or "[]")
        warn = [g for g in guidance if g.get("rule") == "warning"]
        style = [g for g in guidance if g.get("rule") != "warning"]

        events = [(r["decided_at"], r["decision"], "%s v%s by %s%s" % (
            {"approve": "Approved", "reject": "Rejected", "edit": "Edited to"}.get(r["decision"], r["decision"]),
            r["version"], r["reviewer"], (" · " + r["note"]) if r.get("note") else "")) for r in reviews]
        events += [(s["sent_at"], "send", "%s%s" % ("Sent to your inbox" if s["status"] == "sent" else
                                                   ("Dry run recorded (nothing emailed)" if s["status"] == "dry_run" else "Send failed"),
                                                   (" · " + s["error"]) if s.get("error") else "")) for s in sends]
        events.sort(key=lambda x: x[0] or "")
        tl = "".join('<div class="tl %s"><i></i><div>%s<div class="muted small">%s</div></div></div>' % (
            e(k), e(t), e((ts or "")[:16].replace("T", " "))) for ts, k, t in events)
        tl = ('<div class="tl"><i></i><div>Drafted by the agent (%s)<div class="muted small">%s</div></div></div>' % (
            e(d["composer"]), e((d["created_at"] or "")[:16].replace("T", " ")))) + tl

        if locked:
            editor_actions = '<p class="muted small">This draft is %s and can no longer be edited.</p>' % e(LABEL.get(d["status"], d["status"]))
        else:
            editor_actions = '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><button class="btn" type="submit">Save edit</button><span class="muted small">Saving creates a new version and clears any approval.</span></div>'
        editor = """<form class="panel" method="post" action="/drafts/%s/edit"><div class="ph"><h2>Email</h2><span class="muted small num">Version %s · %s</span></div>
<div class="pb" style="display:grid;gap:12px"><input type="hidden" name="reviewer" value="%s">
<label class="f">Subject<input type="text" name="subject" value="%s" %s></label>
<label class="f">Body<textarea name="body" rows="17" %s>%s</textarea></label>%s</div></form>""" % (
            e(did), e(d["version"]), "edited" if int(d["version"]) > 1 else "as drafted", e(reviewer), e(d["current_subject"]),
            "readonly" if locked else "", "readonly" if locked else "", e(d["current_body"]), editor_actions)

        send_label = "Send to my inbox" if live else "Record dry run"
        from .safety import campaign_block, sending_paused, suppressed_reason
        from .web_extra import revisions_html
        blockers = [b for b in (sending_paused(st), campaign_block(st), suppressed_reason(st, domain=c.get("domain"), email=poc.get("email"))) if b]
        unresolved = [s for s in sends if s["status"] in ("sending", "uncertain")]
        unresolved_html = "".join("""<div class="flash bad">Uncertain send (%s, %s UTC): it may have been delivered. Check your Gmail Sent folder, then:
<div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap"><form method="post" action="/sends/%s/resolve"><input type="hidden" name="outcome" value="sent"><button class="btn sm" type="submit">It was sent</button></form>
<form method="post" action="/sends/%s/resolve"><input type="hidden" name="outcome" value="not_sent"><button class="btn sm" type="submit">It was not sent</button></form>
<form method="post" action="/sends/%s/check-gmail"><button class="btn sm" type="submit">Look it up in Gmail</button></form></div></div>""" % (
            e(s.get("error") or "no confirmation"), e((s["sent_at"] or "")[:16].replace("T", " ")), e(s["id"]), e(s["id"]), e(s["id"])) for s in unresolved)
        blocker_html = "".join('<div class="flash bad">Sending blocked: %s</div>' % e(b) for b in blockers)
        decide = ("""<section class="panel"><div class="ph"><h2>Decision</h2>%s</div><div class="pb actions">""" + unresolved_html.replace("%", "%%") + blocker_html.replace("%", "%%") + """
<dl class="kv"><dt>Recipient</dt><dd>Your inbox only%s</dd><dt>Mode</dt><dd>%s</dd><dt>Approved</dt><dd>%s</dd></dl><hr class="sep">
<form method="post" action="/drafts/%s/approve"><input type="hidden" name="expected_hash" value="%s"><input type="hidden" name="reviewer" value="%s">
<button class="btn primary" type="submit" %s>%s Approve this version</button></form>
<form method="post" action="/drafts/%s/send"><button class="btn" type="submit" %s>%s %s</button>
<span class="muted small">%s</span></form>
<form method="post" action="/drafts/%s/reject"><input type="hidden" name="reviewer" value="%s">
<div style="display:flex;gap:8px"><input type="text" name="note" placeholder="Reason (optional)" aria-label="Reason for rejecting" %s><button class="btn danger" type="submit" %s>Reject</button></div></form>
</div></section>""") % (
            spill(d["status"]), (" · " + e(settings.operator_inbox)) if settings.operator_inbox else " · <b>not set</b>",
            "Live: approved drafts are emailed" if live else "Dry run: nothing is emailed",
            ("Version %s" % e(d["version"])) if approved_ok else "Not yet",
            e(did), e(d["current_hash"]), e(reviewer), "disabled" if (locked or approved_ok) else "", icon("check"),
            e(did), "" if (approved_ok and not blockers and not unresolved) else "disabled", icon("mail"), send_label,
            "Approve first" if not approved_ok and not locked else "",
            e(did), e(reviewer), "disabled" if locked else "", "disabled" if locked else "")

        hook_html = ("""<div class="quote">%s</div><div class="src"><a href="%s" rel="noreferrer" target="_blank">%s</a> · tier %s · published %s</div>""" % (
            e(hook["quote"]), safe_href(hook["source_url"]), e(_short_url(hook["source_url"])), e(hook["source_tier"]),
            e((hook["published_at"] or "")[:10] or "date unknown"))) if hook else '<p class="muted small">No hook recorded.</p>'
        why = """<section class="panel"><div class="ph"><h2>Why this email</h2></div><div class="pb" style="display:grid;gap:14px">
<div><h3 style="margin-bottom:6px">Fact used as the hook</h3>%s</div>
<div><h3 style="margin-bottom:6px">To</h3><b>%s</b> <span class="muted">· %s</span><div class="muted small">%s · %s</div></div>
<div class="guess"><b>Guesses, not facts.</b> The pain points come from the contact's role, not from research:<ul style="margin:6px 0 0;padding-left:18px">%s</ul></div>
%s%s</div></section>""" % (
            hook_html, e(poc.get("full_name")), e(poc.get("title")), "Found by Hunter" if poc.get("source") == "hunter" else "Entered manually",
            "role confirmed on a public page" if poc.get("verification_status") == "verified" else "role not independently confirmed",
            "".join("<li>%s</li>" % e(p) for p in pains),
            "".join('<div class="flash bad">%s</div>' % e(w["detail"]) for w in warn),
            ('<div><h3 style="margin-bottom:6px">Wording learned from your past edits</h3><ul style="margin:0;padding-left:18px">%s</ul></div>' %
             "".join("<li>%s <span class='muted small'>(from %d review%s)</span></li>" % (
                 e(g["detail"]), len(g.get("review_ids", [])), "" if len(g.get("review_ids", [])) == 1 else "s") for g in style)) if style else "")

        body = """<div><div class="crumb"><a href="/review">Review queue</a> / <a href="/companies/%s">%s</a></div>
<div class="head"><div><h1>%s</h1><p>To %s at %s</p></div>%s</div></div>
<div class="split"><div class="stack">%s%s<section class="panel"><div class="ph"><h2>History</h2></div><div class="pb timeline">%s</div></section></div>
<div class="rail">%s%s</div></div>""" % (
            e(d["company_id"]), e(c.get("name") or c.get("domain")), e(d["current_subject"]), e(poc.get("full_name")),
            e(c.get("name") or c.get("domain")), spill(d["status"]), editor, revisions_html(st, d), tl, decide, why)
        return layout("Review: " + (c.get("name") or ""), "review", body, msg=msg)

    def _act(did: str, fn):
        try:
            msg = fn()
        except (review.ReviewError, review.SendBlocked) as ex:
            msg = "Blocked: %s" % ex
        return RedirectResponse("/drafts/%s?msg=%s" % (did, quote(msg or "")), status_code=303)

    @app.post("/drafts/{did}/edit")
    def do_edit(did: str, subject: str = Form(...), body: str = Form(...), reviewer: str = Form("operator")):
        st, err = guard()
        return err or _act(did, lambda: "Saved as version %s. Approve it before sending." % review.edit(st, did, subject, body, reviewer)["version"])

    @app.post("/drafts/{did}/approve")
    def do_approve(did: str, expected_hash: str = Form(...), reviewer: str = Form("operator")):
        st, err = guard()
        return err or _act(did, lambda: "Approved version %s for your inbox." % review.approve(
            st, did, reviewer, expected_hash, recipient=settings.operator_inbox)["version"])

    @app.post("/drafts/{did}/reject")
    def do_reject(did: str, reviewer: str = Form("operator"), note: str = Form("")):
        st, err = guard()
        return err or _act(did, lambda: (review.reject(st, did, reviewer, note or None), "Rejected.")[1])

    @app.post("/drafts/{did}/send")
    def do_send(did: str):
        st, err = guard()
        # only a short status in the redirect URL (no addresses or body text in URLs)
        return err or _act(did, lambda: review.send(st, settings, did)["message"])

    @app.post("/sends/{send_id}/resolve")
    def resolve_send(send_id: str, outcome: str = Form(...)):
        st, err = guard()
        if err:
            return err
        row = st.one("SELECT draft_id FROM sends WHERE id = %s", (send_id,))
        if not row:
            return RedirectResponse("/review", status_code=303)
        return _act(row["draft_id"], lambda: "Recorded: %s." % (
            "it was sent" if review.resolve_uncertain(st, send_id, outcome, reviewer)["status"] == "sent"
            else "not sent; once sending is resumed in Controls you can send this approved version again"))

    @app.post("/settings/pause")
    def toggle_pause(paused: str = Form("0"), reason: str = Form("")):
        st, err = guard()
        if err:
            return err
        from .safety import pause_sending
        if paused != "1":   # resuming needs a recorded reason: handled on the Controls page
            return RedirectResponse("/controls?msg=%s" % quote("Resume sending here, with a reason."), status_code=303)
        pause_sending(st, reason or "paused by operator", "human", reviewer)
        return RedirectResponse("/settings?msg=%s" % quote("Sending paused."), status_code=303)

    @app.post("/settings/suppress")
    def suppress(kind: str = Form("domain"), value: str = Form(""), reason: str = Form("")):
        st, err = guard()
        if err:
            return err
        from .safety import add_suppression
        try:
            added = add_suppression(st, kind, value, reason)
            msg = "Added to the opt-out list." if added else "Already on the opt-out list."
        except ValueError as ex:
            msg = "Not saved: %s" % ex
        return RedirectResponse("/settings?msg=%s" % quote(msg), status_code=303)

    @app.post("/settings/suppress/{sid}/delete")
    def unsuppress(sid: str):
        st, err = guard()
        if err:
            return err
        from .safety import remove_suppression
        remove_suppression(st, sid)
        return RedirectResponse("/settings?msg=%s" % quote("Removed from the opt-out list."), status_code=303)

    # ------------------------------------------------------------ runs
    @app.get("/runs", response_class=HTMLResponse)
    @paged
    def runs():
        st, err = guard()
        if err:
            return err
        rs = st.runs(50)
        trs = "".join("""<tr><td class="co"><a href="/runs/%s">%s</a><div class="sub">%s</div></td><td>%s</td><td class="hide-sm">%s</td>
<td class="hide-sm muted small">%s</td></tr>""" % (
            e(r["id"]), e((r["started_at"] or "")[:16].replace("T", " ")), e(_run_inputs(r.get("input_json"))), spill(r["status"]),
            e(_run_line(summary(r.get("summary_json")))), e(r["id"][:8])) for r in rs)
        body = """<div class="head"><div><h1>Runs</h1><p>Each time the agent discovered and researched companies.</p></div><a class="btn primary" href="/">%s New run</a></div>
<section class="panel">%s</section>""" % (icon("play"), ("""<div class="scroll"><table class="tbl"><thead><tr><th>Started (UTC)</th><th>Status</th>
<th class="hide-sm">Result</th><th class="hide-sm">ID</th></tr></thead><tbody>%s</tbody></table></div>""" % trs) if rs else '<div class="empty">No runs yet.</div>')
        return layout("Runs", "runs", body, poll=bool(running_ids(st)))

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    @paged
    def run_view(run_id: str, msg: str = ""):
        st, err = guard()
        if err:
            return err
        from .pipeline import recover_stale_runs
        recover_stale_runs(st, settings.stale_run_minutes)
        r = st.one("SELECT * FROM research_runs WHERE id = %s", (run_id,))
        if not r:
            return layout("Not found", "runs", '<div class="head"><h1>Run not found</h1></div>')
        active = r["status"] in ("queued", "running")
        pr = run_progress(st, r)
        s = summary(r.get("summary_json"))
        stats = [("Discovered", s.get("discovered", pr["discovered"])), ("Researched", s.get("researched", pr["done"])),
                 ("Qualified", s.get("qualified", "–")), ("Drafts", s.get("drafts", "–")), ("Errors", s.get("errors", 0))]
        stat_html = "".join('<div class="stat"><b>%s</b><span>%s</span></div>' % (e(v), e(k)) for k, v in stats)
        gaps = s.get("config_gaps") or {}
        logs = st.logs_for(run_id)
        log_rows = "".join("<tr><td class='muted small num'>%s</td><td>%s</td><td class='small'>%s</td></tr>" % (
            e((l["created_at"] or "")[11:19]), pill(l["level"].capitalize(), {"error": "bad", "warning": "warn"}.get(l["level"], "muted")),
            e(l["message"])) for l in logs)
        prog = ""
        if active:
            pct = int(100 * pr["done"] / pr["target"]) if pr["target"] else 8
            prog = """<div class="pb"><p style="margin-bottom:8px"><span class="live"></span>&nbsp; %s · <span class="num">%d of %d</span> researched</p>
<div class="progress"><span style="width:%d%%"></span></div></div>""" % (e(pr["phase"]), pr["done"], pr["target"], max(pct, 4))
        body = """<div><div class="crumb"><a href="/runs">Runs</a> / %s</div><div class="head"><div><h1>Run %s</h1><p>%s · %s</p></div>%s</div></div>
<section class="panel"><div class="ph"><h2>Summary</h2></div>%s<div class="pb" style="display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px">%s</div>
%s</section><section class="panel"><div class="ph"><h2>Companies</h2></div>%s</section>
<section class="panel"><details><summary style="padding:16px 20px;cursor:pointer;font-weight:600">Run log (%d entries)</summary>
<div class="scroll"><table class="tbl"><tbody>%s</tbody></table></div></details></section>""" % (
            e(run_id[:8]), e(run_id[:8]), e((r["started_at"] or "")[:16].replace("T", " ") + " UTC"), e(_run_inputs(r.get("input_json"))),
            spill(r["status"]) + ((' <form method="post" action="/runs/%s/resume" style="display:inline"><button class="btn primary sm" type="submit">Resume run</button></form>' % e(run_id))
                                  if r["status"] in ("interrupted", "failed") else ""), prog, stat_html,
            ('<div class="pb"><div class="notice small">Missing configuration during this run: %s</div></div>' % e(", ".join(gaps))) if gaps else "",
            company_table(company_views(st, run_id=run_id, running_run_ids=running_ids(st)), "No companies yet."),
            len(logs), log_rows or '<tr><td class="empty">No log entries.</td></tr>')
        return layout("Run", "runs", body, poll=active, msg=msg)

    # ------------------------------------------------------------ settings
    @app.get("/settings", response_class=HTMLResponse)
    @paged
    def settings_page(msg: str = ""):
        st, err = guard()
        if err:
            return err
        from .safety import list_suppressions, sending_paused
        paused = sending_paused(st)
        sup_rows = "".join("""<tr><td><b style="font-weight:560">%s</b><div class="sub">%s · added %s</div></td><td class="small">%s</td>
<td><form method="post" action="/settings/suppress/%s/delete"><button class="btn sm" type="submit">Remove</button></form></td></tr>""" % (
            e(s["value"]), e(s["kind"]), e((s["created_at"] or "")[:10]), e(s.get("reason") or ""), e(s["id"])) for s in list_suppressions(st))
        from .web_extra import controls_html
        safety_html = controls_html(st, reviewer).replace("%", "%%") + """
<section class="panel"><div class="ph"><div><h2>Opt-out list</h2><p class="muted small">Never researched, drafted or sent. A domain covers every address on it.</p></div></div>
<form class="pb" method="post" action="/settings/suppress" style="display:grid;gap:8px">
<div style="display:flex;gap:8px"><select name="kind" aria-label="Type" style="font:inherit;border:1px solid var(--line-2);border-radius:8px;padding:0 8px"><option value="domain">Domain</option><option value="email">Email</option></select>
<input type="text" name="value" placeholder="acme.com or person@acme.com" aria-label="Domain or email" required></div>
<input type="text" name="reason" placeholder="Reason (e.g. asked not to be contacted)" aria-label="Reason">
<button class="btn" type="submit">Add to opt-out list</button></form>
%s</section>""" % (
            ("""<div class="scroll"><table class="tbl"><tbody>%s</tbody></table></div>""" % sup_rows) if sup_rows else '<div class="empty small">No opt-outs yet.</div>')
        icp = load_icp()
        src = load_sources()
        missing = settings.missing() if not mock else {}
        conf = [("NEON_DATABASE_URL", "Database", "Stores runs, evidence, drafts and decisions"),
                ("TAVILY_API_KEY", "Search", "Finds companies and funding news"),
                ("GEMINI_API_KEY", "Language model", "Extracts quotes and polishes wording"),
                ("HUNTER_API_KEY", "Contacts", "Looks up people at qualified companies"),
                ("OPERATOR_INBOX", "Your inbox", "The only address email is ever sent to")]
        conf_rows = "".join("<tr><td><b style='font-weight:560'>%s</b><div class='sub'>%s</div></td><td>%s</td><td class='hide-sm muted small'>%s</td></tr>" % (
            e(lbl), e(k), pill("Missing", "bad") if k in missing else pill("Connected" if not mock else "Mock", "good" if not mock else "muted"),
            e(missing.get(k, desc))) for k, lbl, desc in conf)
        smtp_ok = bool(settings.smtp_user and settings.smtp_password)
        trig = "".join("<tr><td>%s</td><td class='num'>+%s</td><td class='hide-sm muted small'>%s</td></tr>" % (
            e(k.replace("_", " ").capitalize()), e(v.get("weight")), e(v.get("description"))) for k, v in (icp.get("triggers") or {}).items())
        tiers = src.get("tiers", {})
        tier_html = "".join("<div><h3 style='margin-bottom:6px'>%s</h3><p class='muted small'>%s</p></div>" % (e(t), e(", ".join(tiers.get(n) or tiers.get(str(n)) or [])))
                            for t, n in (("Tier 2: credible reporting", 2), ("Tier 3: estimates, lower confidence", 3)))
        body = """<div class="head"><div><h1>Settings</h1><p>What the agent is connected to and the rules it follows. Values are read from <code>.env</code> and <code>config/</code>; edit those files and restart to change them.</p></div></div>
<div class="split"><div class="stack">
<section class="panel"><div class="ph"><h2>Connections</h2></div><div class="scroll"><table class="tbl"><tbody>%s</tbody></table></div></section>
<section class="panel"><div class="ph"><div><h2>Ideal customer profile</h2><p class="muted small">%s · qualifies at score %s · config/icp.yaml</p></div></div>
<div class="pb"><dl class="kv"><dt>Must have</dt><dd>B2B SaaS, and funding stage %s</dd><dt>Triggers</dt><dd>At least %s verified</dd>
<dt>Funding hook</dt><dd>Announced within %s months</dd></dl></div>
<div class="scroll"><table class="tbl"><thead><tr><th>Trigger signal</th><th>Weight</th><th class="hide-sm">Meaning</th></tr></thead><tbody>%s</tbody></table></div></section>
<section class="panel"><div class="ph"><div><h2>Sources</h2><p class="muted small">The company's own site is always tier 1. Anything unlisted is only a lead, never evidence. config/sources.yaml</p></div></div>
<div class="pb" style="display:grid;gap:14px">%s<div><h3 style="margin-bottom:6px">Never fetched</h3><p class="muted small">%s</p></div></div></section>
</div><div class="rail">
<section class="panel"><div class="ph"><h2>Sending</h2>%s</div><div class="pb"><dl class="kv"><dt>Mode</dt><dd>%s</dd><dt>Recipient</dt><dd>%s</dd>
<dt>Gmail login</dt><dd>%s</dd><dt>Follow-up after</dt><dd>%d days</dd></dl>
<p class="muted small" style="margin-top:12px">To send for real, set <code>SEND_MODE=live</code> in .env and restart. Every email still needs your approval.</p></div></section>
%s</div></div>""" % (
            conf_rows, e(icp.get("name")), e(icp.get("scoring", {}).get("qualify_threshold")),
            e(", ".join(s.replace("series_", "Series ").title() for s in icp.get("must_have", {}).get("funding_stage", {}).get("allowed", []))),
            e(icp.get("min_triggers", 1)), e(icp.get("hook_funding_max_months", 6)), trig, tier_html, e(", ".join(src.get("never_fetch") or [])),
            pill("Live", "good") if settings.send_mode == "live" else pill("Dry run", "info"),
            "Live" if settings.send_mode == "live" else "Dry run (nothing is emailed)", e(settings.operator_inbox or "not set"),
            "configured" if smtp_ok else "not configured", settings.follow_up_days, safety_html)
        return layout("Settings", "settings", body, msg=msg)

    from types import SimpleNamespace
    from . import web_extra
    ctx = SimpleNamespace(state=state, settings=settings, guard=guard, layout=layout, reviewer=reviewer, mock=mock,
                          deps_factory=deps_factory, background=_background, gmail_factory=gmail_factory, paged=paged)
    web_extra.register(app, ctx)

    @app.on_event("startup")
    def _start_worker():
        if not mock and state.get("store") is not None and not state.get("worker"):
            state["worker"] = web_extra.start_worker(ctx, settings.gmail_sync_minutes)

    return app


def _paused(st: Any) -> bool:
    try:
        from .safety import sending_paused
        return bool(sending_paused(st))
    except Exception:
        return False


# ---------------------------------------------------------------- formatting helpers
def _stage_card(d: Dict[str, Any]) -> str:
    names = "".join('<li><a href="/companies/%s">%s</a>%s</li>' % (
        e(c["id"]), e(c["name"]), ('<span class="muted"> · %s</span>' % e(c["note"])) if c.get("note") else "")
        for c in d["companies"])
    more = '<li class="muted">+%d more</li>' % d["more"] if d["more"] else ""
    body = ('<ul>%s%s</ul>' % (names, more)) if names else '<p class="muted">None yet</p>'
    return """<div class="scard"><div class="sh"><b>%s</b><span class="num" title="Companies whose furthest stage is this one">%s here</span></div><p class="sd">%s</p>%s</div>""" % (
        e(d["label"]), e(d["count"]), e(d["detail"]), body)


def _short_source(found_via: str) -> str:
    f = found_via or ""
    if f.startswith("operator"):
        return "added by you"
    if f.startswith("RSS"):
        return "news feed"
    if f.startswith("Tavily"):
        return "search"
    if f.startswith("portfolio"):
        return "portfolio page"
    return f[:30]


def _short_url(url: str) -> str:
    u = (url or "").replace("https://", "").replace("http://", "").replace("www.", "")
    return u if len(u) <= 60 else u[:57] + "…"


def _run_line(s: Dict[str, Any]) -> str:
    if not s:
        return "in progress"
    return "%s discovered · %s researched · %s qualified · %s drafts%s" % (
        s.get("discovered", 0), s.get("researched", 0), s.get("qualified", 0), s.get("drafts", 0),
        (" · %s errors" % s["errors"]) if s.get("errors") else "")


def _run_inputs(raw: Optional[str]) -> str:
    try:
        i = json.loads(raw or "{}")
    except ValueError:
        return ""
    if i.get("domains"):
        d = i["domains"]
        return "Domains: " + ", ".join(d[:3]) + (" +%d" % (len(d) - 3) if len(d) > 3 else "")
    used = [n for n, k in (("feeds", "use_feeds"), ("search", "use_search"), ("portfolios", "use_portfolios")) if i.get(k, True)]
    return "Discovery: " + (", ".join(used) or "none")
