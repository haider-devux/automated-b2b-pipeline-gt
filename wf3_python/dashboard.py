"""
Granjur Pipeline Dashboard — a small read-only web view of the pipeline.

WHY this exists: the database is the single source of truth, and every phase (WF-1..WF-4)
moves leads through the `leads.status` column. So one web view over the DB shows the whole
pipeline's live progress + history — like n8n's canvas, but showing the real data, not wiring.

Mostly read-only. Its writes are the human-in-the-loop actions on the Outreach/Leads tabs
(approve/skip a pitch, re-arm cooldown, add a contact) — discovery + intake are fully automated,
so there is no manual company-review screen.

Run:   python dashboard.py
Then open http://localhost:5000 in your browser.
"""
import html
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
from urllib.parse import quote
from flask import Flask, render_template_string, request, abort, redirect, Response
from psycopg2.extras import RealDictCursor, Json
import db

app = Flask(__name__)

# region_code enum values (for validating a reviewed candidate before it enters the pipeline)
VALID_REGIONS = {"US", "EU", "UK", "GCC", "CN", "AU", "OTHER"}

# reuse WF-4's payload builder (self-contained, no DB) so the Outreach page can preview/queue
_outreach_path = pathlib.Path(__file__).resolve().parent.parent / "wf4_python" / "outreach.py"
_spec = importlib.util.spec_from_file_location("wf4_outreach", _outreach_path)
outreach = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(outreach)

# WF-4 real Gmail sender — used by the Follow-ups "email me a preview" buttons (stdlib-only, safe to load)
_sg_path = pathlib.Path(__file__).resolve().parent.parent / "wf4_python" / "send_gmail.py"
_sg_spec = importlib.util.spec_from_file_location("wf4_send_gmail", _sg_path)
send_gmail = importlib.util.module_from_spec(_sg_spec)
_sg_spec.loader.exec_module(send_gmail)

# reuse WF-4's Phase-3 domain-health service for the Health tab (SPF/DKIM/DMARC + blacklist + warmup)
_dh_path = pathlib.Path(__file__).resolve().parent.parent / "wf4_python" / "domain_health.py"
_dh_spec = importlib.util.spec_from_file_location("wf4_domain_health", _dh_path)
domain_health = importlib.util.module_from_spec(_dh_spec)
_dh_spec.loader.exec_module(domain_health)

# COOLDOWN handling: skipped leads rest COOLDOWN_DAYS days then flow back to QUALIFIED.
import rearm_cooldown
rearm_cooldown.ensure_column()  # add leads.cooldown_until (+ back-fill) once at startup
db.ensure_status_values()       # make sure the NEEDS_CONTACT lead status exists

import tagging                   # Phase-5 CRM tags
try:
    tagging.retag_all()         # keep leads.tags fresh at startup (idempotent)
except Exception:               # noqa: BLE001 — never let tagging block the dashboard
    pass


def _ensure_analytics_schema():
    """Idempotently create the Phase-6 email_events telemetry table (so a fresh DB just works)."""
    sqlp = pathlib.Path(__file__).resolve().parent.parent / "database" / "phase6_analytics_migration.sql"
    conn = db.get_connection()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(open(sqlp, encoding="utf-8").read())
    finally:
        conn.close()


try:
    _ensure_analytics_schema()
except Exception:               # noqa: BLE001 — analytics is additive; never block startup
    pass

# 1x1 transparent GIF for the open-tracking pixel
import base64 as _b64
_PIXEL_GIF = _b64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

import factcheck  # scores how well a pitch matches the verified facts (for the Outreach fact-check)
from pitch import psi_report_url  # public Google mobile-speed report link (verifiable source)
import sendwindows as sw  # Phase-1 send-window advisor (per-region local time + best-to-send-now)
import holiday_calendar    # Phase-2 free offline public-holiday calendar (for upcoming-holidays panel)
from datetime import date as _date, datetime as _dt, timezone as _tz

# Phase-4 follow-up copy + scheduling (PURE, no DB) — for the Follow-ups tab previews
_fc_path = pathlib.Path(__file__).resolve().parent.parent / "wf4_python" / "followup_copy.py"
_fc_spec = importlib.util.spec_from_file_location("wf4_followup_copy", _fc_path)
followup_copy = importlib.util.module_from_spec(_fc_spec)
_fc_spec.loader.exec_module(followup_copy)


@app.before_request
def _auto_rearm():
    """Auto-heal on every page load: any COOLDOWN lead whose timer is up returns to review."""
    try:
        rearm_cooldown.rearm()
    except Exception:
        pass  # never let the auto-heal break a page render

# Pipeline order + a colour class for each status (drives the funnel + badges).
STATUS_ORDER = [
    "DISCOVERED", "ENRICHING", "ENRICHED", "QUALIFYING", "QUALIFIED",
    "QUEUED_FOR_OUTREACH", "CONTACTED", "REPLIED", "BOOKED", "WON",
    "NEEDS_CONTACT", "DISQUALIFIED", "SUPPRESSED", "ERROR", "COOLDOWN", "LOST",
]
STATUS_KIND = {  # green = good, red = dead-end, blue = in-flight, grey = other
    "QUALIFIED": "ok", "WON": "ok", "BOOKED": "ok", "REPLIED": "ok",
    "DISQUALIFIED": "bad", "SUPPRESSED": "bad", "ERROR": "bad", "LOST": "bad",
    "DISCOVERED": "go", "ENRICHING": "go", "ENRICHED": "go",
    "QUALIFYING": "go", "QUEUED_FOR_OUTREACH": "go", "CONTACTED": "go",
}

# friendly names for where a lead came from (drives the Flow view)
SOURCE_LABELS = {
    "osm": "Maps (OSM)", "jobfeed": "Job boards", "manual": "Manual paste",
    "review": "Manual paste", "csv": "CSV import", "mock": "Test", "mock_wf2": "Test",
    "test_manual_week_1": "Test",
}


def source_label(s):
    return SOURCE_LABELS.get(s, s or "-")


def rearm_cell(r):
    """Actions cell for the Leads table: a manual 'Re-arm' button on COOLDOWN rows."""
    if r["status"] != "COOLDOWN":
        return ""
    auto = (f'<div class="muted" style="font-size:11px">auto: {r["cooldown_until"]:%m-%d}</div>'
            if r.get("cooldown_until") else "")
    return (f'<form method="post" action="/lead/{r["id"]}/rearm" style="margin:0">'
            f'<button class="go" style="padding:5px 10px;font-size:12px" '
            f'title="Move this company back into the pipeline now">'
            f'&#8635; Re-arm</button></form>{auto}')


def actions_cell(r):
    """Per-status action in the Leads table: Re-arm for COOLDOWN, Add-contact for NEEDS_CONTACT."""
    if r["status"] == "COOLDOWN":
        return rearm_cell(r)
    if r["status"] == "NEEDS_CONTACT":
        return (f'<a href="/lead/{r["id"]}/contact" '
                f'style="display:inline-block;padding:5px 10px;font-size:12px;border-radius:7px;'
                f'background:var(--accent);color:#fff;text-decoration:none;font-weight:600" '
                f'title="Add a real named contact so we can pitch &amp; email them">&#43; Add contact</a>')
    return ""


def factcheck_preview(l):
    """Render the pitch with any UNGROUNDED sentence highlighted red + a grounding score.
    The auto-check is English-oriented, so we only show it for English pitches (ar/zh -> skip)."""
    body = (l.get("pitch_body") or "").strip()
    if not body or (l.get("pitch_lang") or "en") != "en":
        return ""
    res = factcheck.analyze(body, {
        "site_description": l.get("description"),
        "tech_stack": l.get("tech_stack") or [],
        "trigger": l.get("qualify_trigger"),
    })
    score = res["score"]
    cls = "hi" if score >= 0.8 else ("mid" if score >= 0.6 else "lo")
    parts = []
    for s in res["sentences"]:
        t = html.escape(s["text"])
        if s["status"] == "ungrounded":
            parts.append(f'<span class="ff" title="{html.escape(s["note"] or "unverified claim")}">{t}</span>')
        elif s["status"] == "grounded":
            parts.append(f'<span class="fok">{t}</span>')
        else:
            parts.append(t)
    flagged = sum(1 for s in res["sentences"] if s["status"] == "ungrounded")
    note = (f'{flagged} sentence(s) not backed by a known fact — hover the red text to see why.'
            if flagged else 'Every claim is backed by a verified fact.')
    return (f'<div class="fcprev"><span class="gscore {cls}">Grounding {score:.0%}</span> '
            f'<span class="muted">&nbsp;{note}</span>'
            f'<div style="margin-top:8px">{" ".join(parts)}</div></div>')


def query(sql, params=None, one=False):
    """Run a read-only query, return list[dict] (or a single dict if one=True)."""
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
    finally:
        conn.close()
    if one:
        return rows[0] if rows else None
    return [dict(r) for r in rows]


def execute(sql, params=None, returning=False):
    """Run a write query (used only by the review-queue actions), commit, optionally RETURNING."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            result = cur.fetchone() if returning else None
        conn.commit()
        return result
    finally:
        conn.close()


def outreach_ready_count():
    row = query("SELECT COUNT(*) n FROM leads WHERE status='QUALIFIED' "
                "AND pitch_body IS NOT NULL AND email IS NOT NULL", one=True)
    return row["n"] if row else 0


# ----------------------------------------------------------------------------- templates
BASE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{% if refresh %}<meta http-equiv="refresh" content="5">{% endif %}
<title>Granjur Pipeline</title>
<style>
  :root{--bg:#f7f7f5;--panel:#ffffff;--line:#e9e8e3;--txt:#37352f;--mut:#8b877d;--soft:#f2f1ee;
        --accent:#2f6feb;--accent-soft:#eaf0fd;
        --ok:#178a52;--ok-bg:#e7f4ec;--bad:#d0453b;--bad-bg:#fbeae8;--go:#2f6feb;--go-bg:#eaf0fd;
        --warn:#9a6a12;--warn-bg:#f9efd8;
        --shadow:0 1px 2px rgba(16,15,15,.04),0 1px 3px rgba(16,15,15,.05)}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);-webkit-font-smoothing:antialiased;
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif}
  a{color:inherit;text-decoration:none}
  header{background:rgba(255,255,255,.82);backdrop-filter:saturate(180%) blur(10px);
    border-bottom:1px solid var(--line);padding:11px 26px;display:flex;gap:18px;align-items:center;
    position:sticky;top:0;z-index:10}
  header .brand{font-weight:700;letter-spacing:-.01em;font-size:16px}
  header .brand span{color:var(--accent)}
  header nav{display:flex;gap:2px;flex-wrap:wrap}
  header nav a{color:var(--mut);padding:6px 12px;border-radius:8px;font-size:14px;font-weight:500}
  header nav a:hover{color:var(--txt);background:var(--soft)}
  header nav a.on{color:var(--accent);background:var(--accent-soft);font-weight:600}
  .wrap{max-width:1180px;margin:0 auto;padding:26px 26px 64px}
  .pagehead{margin:2px 0 22px}
  .pagehead h1{font-size:23px;font-weight:700;letter-spacing:-.02em;margin:0}
  .pagehead p{color:var(--mut);font-size:14px;margin:5px 0 0}
  h2{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);font-weight:600;margin:26px 0 12px}
  .funnel{display:flex;flex-wrap:wrap;gap:12px}
  .stat{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--line);
    border-radius:12px;padding:14px 18px;min-width:132px;box-shadow:var(--shadow)}
  .stat.ok{border-left-color:var(--ok)} .stat.bad{border-left-color:var(--bad)}
  .stat.go{border-left-color:var(--go)}
  .stat .n{font-size:26px;font-weight:700;letter-spacing:-.02em}
  .stat .l{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:820px){.grid{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;box-shadow:var(--shadow)}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);font-size:14px}
  th{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
  tbody tr:last-child td,table tr:last-child td{border-bottom:0}
  tr:hover td{background:var(--soft)}
  .badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600;
    background:var(--soft);color:var(--mut);border:1px solid var(--line)}
  .badge.ok{background:var(--ok-bg);color:var(--ok);border-color:transparent}
  .badge.bad{background:var(--bad-bg);color:var(--bad);border-color:transparent}
  .badge.go{background:var(--go-bg);color:var(--go);border-color:transparent}
  .seg{font-weight:600} .muted{color:var(--mut)}
  .bar{height:8px;background:var(--soft);border-radius:6px;overflow:hidden}
  .bar>i{display:block;height:100%;background:var(--accent)}
  .pitch{white-space:pre-wrap;background:#faf9f7;border:1px solid var(--line);border-radius:10px;padding:14px;margin-top:8px;font-size:14px}
  .kv{display:grid;grid-template-columns:150px 1fr;gap:6px 14px} .kv div:nth-child(odd){color:var(--mut)}
  .back{color:var(--mut);font-size:13px}
  .empty{color:var(--mut);padding:18px;text-align:center}
  .pill{display:inline-block;background:var(--accent);color:#fff;border-radius:20px;
    padding:1px 8px;font-size:11px;font-weight:700;margin-left:4px}
  input,select,textarea{background:#fff;border:1px solid var(--line);color:var(--txt);
    border-radius:9px;padding:9px 11px;font:inherit;width:100%}
  input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  textarea{white-space:pre-wrap;line-height:1.55;resize:vertical;min-height:120px}
  textarea[dir=rtl]{text-align:right}
  .factbox{background:var(--go-bg);border:1px solid var(--line);border-left:3px solid var(--go);
    border-radius:10px;padding:11px 13px;margin:8px 0;font-size:13px;color:#4b5563}
  .factbox b{color:var(--txt)}
  .fcprev{background:#faf9f7;border:1px solid var(--line);border-radius:10px;padding:11px 13px;
    margin:8px 0;font-size:13px;line-height:1.6;color:var(--txt)}
  .fcprev .ff{background:var(--bad-bg);color:var(--bad);border-bottom:1px dashed var(--bad);border-radius:3px;
    padding:0 3px;cursor:help}
  .fcprev .fok{color:var(--ok)}
  .gscore{display:inline-block;border-radius:6px;padding:2px 8px;font-weight:700;font-size:12px}
  .gscore.hi{background:var(--ok-bg);color:var(--ok)} .gscore.mid{background:var(--warn-bg);color:var(--warn)}
  .gscore.lo{background:var(--bad-bg);color:var(--bad)}
  .edited{color:var(--ok);font-size:12px;font-weight:600}
  label{display:block;font-size:12px;color:var(--mut);margin:0 0 4px}
  .form-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  @media(max-width:820px){.form-grid{grid-template-columns:1fr 1fr}}
  button{cursor:pointer;border:1px solid transparent;border-radius:9px;padding:8px 14px;font:inherit;font-weight:600;font-size:14px}
  button.go{background:var(--accent);color:#fff} button.go:hover{background:#245fd0}
  button.ok{background:var(--ok);color:#fff}
  button.bad{background:var(--bad-bg);color:var(--bad);border-color:var(--line)}
  button.ghost{background:#fff;color:var(--txt);border-color:var(--line)} button.ghost:hover{background:var(--soft)}
  form.inline{display:inline}
  .tags{display:flex;flex-wrap:wrap;gap:7px;margin:2px 0}
  .tag{display:inline-flex;align-items:center;gap:6px;background:#fff;border:1px solid var(--line);
    color:var(--txt);border-radius:20px;padding:5px 12px;font-size:13px;font-weight:500;cursor:pointer}
  .tag:hover{background:var(--soft)}
  .tag.active{background:var(--accent);border-color:var(--accent);color:#fff}
  .tag .c{font-size:11px;color:var(--mut)} .tag.active .c{color:#d7e3ff}
  .rowtag{display:inline-block;background:var(--soft);border:1px solid var(--line);color:#6b675e;
    border-radius:6px;padding:1px 7px;font-size:11px;font-weight:600;margin:1px 3px 1px 0}
</style></head><body>
<header>
  <div class="brand">Granjur<span>·</span>Pipeline</div>
  <nav>
    <a href="/" class="{{ 'on' if page=='home' }}">Dashboard</a>
    <a href="/flow" class="{{ 'on' if page=='flow' }}">Flow</a>
    <a href="/leads" class="{{ 'on' if page=='leads' }}">Leads</a>
    <a href="/outreach" class="{{ 'on' if page=='outreach' }}">Outreach{% if ready %} <span class="pill">{{ ready }}</span>{% endif %}</a>
    <a href="/regions" class="{{ 'on' if page=='regions' }}">Regions</a>
    <a href="/followups" class="{{ 'on' if page=='followups' }}">Follow-ups{% if fups %} <span class="pill">{{ fups }}</span>{% endif %}</a>
    <a href="/replies" class="{{ 'on' if page=='replies' }}">Replies{% if reply_ct %} <span class="pill">{{ reply_ct }}</span>{% endif %}</a>
    <a href="/health" class="{{ 'on' if page=='health' }}">Health</a>
    <a href="/analytics" class="{{ 'on' if page=='analytics' }}">Analytics</a>
  </nav>
  <div style="margin-left:auto;color:var(--mut);font-size:12px">{{ 'live · auto-refresh' if refresh else 'live view' }}</div>
</header>
<div class="wrap">
{% set titles = {
 'home': ['Dashboard','Pipeline overview — leads by stage, segment and recent activity'],
 'flow': ['Flow','Live pipeline — where leads come from and the stage each is at'],
 'leads': ['Leads','Your lead hub — filter instantly by tag'],
 'outreach': ['Outreach','Review and approve each AI pitch before it sends'],
 'regions': ['Regions','Send-window advisor, holidays and regional isolation'],
 'followups': ['Follow-ups','Automated multi-day nudges for contacted leads'],
 'replies': ['Replies','Prospect replies — auto-detected from the inbox (positive/OOO/unsubscribe)'],
 'health': ['Email health','Deliverability — SPF/DKIM/DMARC, blacklists, warmup'],
 'analytics': ['Analytics','Funnel math and conversion by source and segment'],
} %}
{% set t = titles.get(page) %}
{% if t %}<div class="pagehead"><h1>{{ t[0] }}</h1><p>{{ t[1] }}</p></div>{% endif %}
{{ body|safe }}
</div>
</body></html>
"""


def badge(status):
    kind = STATUS_KIND.get(status, "")
    return f'<span class="badge {kind}">{status}</span>'


def _contacted_drip_rows():
    """CONTACTED leads with their initial-contact time + follow-up steps already sent."""
    return query("""
        SELECT l.id, l.first_name, l.icp_segment, l.pitch_subject, l.pitch_body, l.pitch_lang,
               l.last_contacted_at, c.legal_name, c.region,
               COALESCE((SELECT min(created_at) FROM outreach_log o
                           WHERE o.lead_id=l.id AND o.step=0 AND o.outcome IN ('sent','logged')),
                        l.last_contacted_at) AS initial_at,
               (SELECT array_agg(DISTINCT o.step) FROM outreach_log o
                  WHERE o.lead_id=l.id AND o.step>=1 AND o.outcome IN ('sent','logged')) AS steps_sent
        FROM leads l JOIN companies c ON c.id=l.company_id
        WHERE l.status='CONTACTED'
        ORDER BY l.last_contacted_at ASC""")


def due_followups_count():
    try:
        now = _dt.now(_tz.utc)
        n = 0
        for r in _contacted_drip_rows():
            p = followup_copy.next_due_step(r["steps_sent"], r["initial_at"], now)
            if not p["complete"] and p["is_due"]:
                n += 1
        return n
    except Exception:                   # noqa: BLE001 — nav badge must never break a page render
        return 0


def new_replies_count():
    try:
        return query("SELECT COUNT(*) n FROM leads WHERE status='REPLIED'", one=True)["n"]
    except Exception:  # noqa: BLE001
        return 0


def page(body, active, refresh=False):
    return render_template_string(BASE, body=body, page=active, refresh=refresh,
                                  ready=outreach_ready_count(), fups=due_followups_count(),
                                  reply_ct=new_replies_count())


# ----------------------------------------------------------------------------- routes
@app.route("/")
def home():
    counts = {r["status"]: r["n"] for r in
              query("SELECT status, COUNT(*) n FROM leads GROUP BY status")}
    total = sum(counts.values())

    # funnel cards in pipeline order (only statuses that actually have leads)
    cards = ""
    for s in STATUS_ORDER:
        if counts.get(s):
            cards += (f'<div class="stat {STATUS_KIND.get(s,"")}">'
                      f'<div class="n">{counts[s]}</div><div class="l">{s}</div></div>')

    # segment + language breakdown (of qualified leads)
    segs = query("SELECT icp_segment, COUNT(*) n FROM leads "
                 "WHERE icp_segment IS NOT NULL GROUP BY icp_segment ORDER BY icp_segment")
    langs = query("SELECT pitch_lang, COUNT(*) n FROM leads "
                  "WHERE status='QUALIFIED' GROUP BY pitch_lang ORDER BY pitch_lang")
    qmax = max([s["n"] for s in segs] + [1])

    seg_rows = "".join(
        f'<tr><td class="seg">{s["icp_segment"]}</td><td style="width:55%">'
        f'<div class="bar"><i style="width:{int(100*s["n"]/qmax)}%"></i></div></td>'
        f'<td class="muted">{s["n"]}</td></tr>' for s in segs) or \
        '<tr><td class="empty" colspan=3>No qualified leads yet</td></tr>'
    lang_rows = "".join(
        f'<tr><td class="seg">{l["pitch_lang"] or "—"}</td><td class="muted">{l["n"]}</td></tr>'
        for l in langs) or '<tr><td class="empty" colspan=2>—</td></tr>'

    # recent activity from the audit table (this is the "what happened / what failed" feed)
    events = query(
        "SELECT e.at, e.from_status, e.to_status, e.workflow, c.legal_name, e.lead_id "
        "FROM lead_events e JOIN leads l ON e.lead_id=l.id "
        "JOIN companies c ON l.company_id=c.id ORDER BY e.at DESC LIMIT 15")
    ev_rows = "".join(
        f'<tr><td class="muted">{e["at"]:%m-%d %H:%M}</td>'
        f'<td><a href="/lead/{e["lead_id"]}">{e["legal_name"]}</a></td>'
        f'<td class="muted">{e["from_status"] or "—"} → </td><td>{badge(e["to_status"])}</td>'
        f'<td class="muted">{e["workflow"] or ""}</td></tr>' for e in events) or \
        '<tr><td class="empty" colspan=5>No activity yet</td></tr>'

    body = f"""
    <h2>Pipeline funnel &nbsp;·&nbsp; {total} leads total</h2>
    <div class="funnel">{cards or '<div class="empty">No leads in the database.</div>'}</div>
    <div class="grid" style="margin-top:24px">
      <div class="panel"><h2 style="margin-top:0">Segments</h2>
        <table>{seg_rows}</table>
        <h2>Pitch language</h2><table>{lang_rows}</table></div>
      <div class="panel"><h2 style="margin-top:0">Recent activity</h2>
        <table>{ev_rows}</table></div>
    </div>"""
    return page(body, "home", refresh=True)


@app.route("/leads")
def leads():
    status = request.args.get("status")
    where = "WHERE l.status = %s" if status else ""
    rows = query(
        f"""SELECT l.id, l.status, l.icp_segment, l.pitch_lang, l.qualify_trigger,
                   l.updated_at, l.cooldown_until, c.first_seen_at,
                   c.legal_name, c.region, c.employee_count,
                   c.tech_stack, c.lighthouse_mobile, c.active_job_posts
            FROM leads l JOIN companies c ON l.company_id=c.id
            {where} ORDER BY l.updated_at DESC""",
        (status,) if status else None)

    # status filter chips (server-side)
    all_counts = {r["status"]: r["n"] for r in
                  query("SELECT status, COUNT(*) n FROM leads GROUP BY status")}
    status_chips = f'<a class="badge {"go" if not status else ""}" href="/leads">All statuses</a> '
    for s in STATUS_ORDER:
        if all_counts.get(s):
            status_chips += (f'<a href="/leads?status={s}" class="badge '
                             f'{STATUS_KIND.get(s,"") if status==s else ""}">{s} {all_counts[s]}</a> ')

    # compute each lead's CRM tags + tally them for the tag-filter bar
    tag_totals = {}
    for r in rows:
        r["_tags"] = tagging.compute_tags(r)
        for t in r["_tags"]:
            tag_totals[t] = tag_totals.get(t, 0) + 1
    tag_chips = "".join(
        f'<button class="tag" data-tag="{t}" onclick="toggleTag(this)">{t}<span class="c">{n}</span></button>'
        for t, n in sorted(tag_totals.items(), key=lambda x: (-x[1], x[0])))

    trs = ""
    for r in rows:
        rowtags = "".join(f'<span class="rowtag">{t}</span>' for t in r["_tags"])
        trs += (f'<tr data-tags="{" ".join(r["_tags"])}">'
                f'<td><a href="/lead/{r["id"]}">{r["legal_name"]}</a></td>'
                f'<td>{badge(r["status"])}</td>'
                f'<td class="seg">{r["icp_segment"] or "—"}</td>'
                f'<td>{rowtags or "<span class=muted>—</span>"}</td>'
                f'<td class="muted">{r["region"] or "—"} · {r["employee_count"] or "?"} ppl</td>'
                f'<td class="muted">{r["updated_at"]:%m-%d %H:%M}</td>'
                f'<td>{actions_cell(r)}</td></tr>')
    trs = trs or '<tr><td class="empty" colspan=7>No leads</td></tr>'

    added = ('<div class="factbox" style="border-left-color:var(--ok);margin-bottom:14px">'
             'Contact saved &#10003; — the lead is back in the queue. Run WF-3 (or <code>run_pipeline.py</code>) '
             'to write its pitch, then it appears on the Outreach tab.</div>'
             if request.args.get("added") else "")

    body = f"""
    {added}
    <h2>Filter by tag</h2>
    <div class="tags" id="tagbar">
      <button class="tag active" data-tag="" onclick="toggleTag(this)">All<span class="c">{len(rows)}</span></button>
      {tag_chips}
    </div>
    <div class="muted" style="font-size:13px;margin:10px 0 4px" id="tagcount"></div>
    <h2>Statuses</h2>
    <div class="tags" style="margin-bottom:16px">{status_chips}</div>
    <div class="panel" style="padding:8px 4px"><table id="leadtable">
      <tr><th>Company</th><th>Status</th><th>Segment</th><th>Tags</th><th>Region · size</th>
          <th>Updated</th><th>Actions</th></tr>
      {trs}</table></div>
    <script>
      var active = new Set();
      function toggleTag(btn){{
        var tag = btn.getAttribute('data-tag');
        if(tag===''){{ active.clear(); }}
        else {{ active.has(tag) ? active.delete(tag) : active.add(tag); }}
        document.querySelectorAll('#tagbar .tag').forEach(function(b){{
          var t=b.getAttribute('data-tag');
          b.classList.toggle('active', t==='' ? active.size===0 : active.has(t));
        }});
        var shown=0, total=0;
        document.querySelectorAll('#leadtable tr[data-tags]').forEach(function(tr){{
          total++;
          var tags=(tr.getAttribute('data-tags')||'').split(' ');
          var ok=[...active].every(function(a){{return tags.indexOf(a)>=0;}});
          tr.style.display = ok ? '' : 'none';
          if(ok) shown++;
        }});
        var c=document.getElementById('tagcount');
        c.textContent = active.size ? ('Showing '+shown+' of '+total+' leads · tags: '+[...active].join(' + ')) : '';
      }}
    </script>"""
    return page(body, "leads")


@app.route("/lead/<lead_id>")
def lead_detail(lead_id):
    r = query(
        """SELECT l.*, c.legal_name, c.domain, c.region, c.country, c.city, c.niche,
                  c.employee_count, c.tech_stack, c.active_job_posts, c.lighthouse_mobile, c.source
           FROM leads l JOIN companies c ON l.company_id=c.id WHERE l.id=%s""",
        (lead_id,), one=True)
    if not r:
        abort(404)
    events = query("SELECT * FROM lead_events WHERE lead_id=%s ORDER BY at", (lead_id,))

    rtl = ' dir="rtl"' if (r.get("pitch_lang") in ("ar",)) else ""
    # the middle panel adapts to where the lead is in the pipeline
    if r["status"] == "QUALIFIED":
        detail_title, detail = "Qualification (WF-3)", (
            f'<div class="kv"><div>Score</div><div>{r["qualify_score"]}</div>'
            f'<div>Trigger</div><div>{r["qualify_trigger"] or "—"}</div>'
            f'<div>Reason</div><div>{r["qualify_reason"] or "—"}</div></div>')
    elif r["status"] == "DISQUALIFIED":
        detail_title, detail = "Qualification (WF-3)", (
            f'<div class="kv"><div>Disqualify reason</div><div>{r["disqualify_reason"] or "—"}</div></div>')
    elif r["status"] == "ERROR":
        detail_title, detail = "Enrichment error (WF-2)", (
            f'<div class="kv"><div>Error</div><div>{r["last_error_message"] or "—"}</div></div>')
    else:
        detail_title, detail = "Status", '<div class="muted">Awaiting qualification (WF-3).</div>'

    pitch = ""
    if r.get("pitch_body"):
        pitch = (f'<div class="panel"><h2 style="margin-top:0">Pitch '
                 f'<span class="muted">({r["pitch_lang"]})</span></h2>'
                 f'<b{rtl}>{r["pitch_subject"] or ""}</b>'
                 f'<div class="pitch"{rtl}>{r["pitch_body"]}</div></div>')

    # outreach panel (only once the lead has entered WF-4)
    out_panel = ""
    out_rows = []
    if r.get("campaign_id"):
        out_rows += [("Campaign", r["campaign_id"]), ("Sending domain", r.get("sending_domain") or "—")]
    if r.get("last_contacted_at"):
        out_rows.append(("Contacted", f'{r["last_contacted_at"]:%Y-%m-%d %H:%M}'))
    if r.get("replied_at"):
        out_rows.append(("Replied", f'{r["replied_at"]:%Y-%m-%d %H:%M} ({r.get("reply_sentiment") or "?"})'))
    if r.get("booked_at"):
        out_rows.append(("Booked", f'{r["booked_at"]:%Y-%m-%d %H:%M}'))
    if r.get("suppressed_at"):
        out_rows.append(("Suppressed", f'{r["suppressed_at"]:%Y-%m-%d %H:%M} — {r.get("suppression_reason") or ""}'))
    if out_rows:
        kv = "".join(f"<div>{k}</div><div>{v}</div>" for k, v in out_rows)
        out_panel = (f'<div class="panel" style="margin-top:18px"><h2 style="margin-top:0">Outreach (WF-4)</h2>'
                     f'<div class="kv">{kv}</div></div>')

    # feedback loop (§1.1 Tip 4): capture the deal outcome so targeting can be tuned over time
    outcome_panel = ""
    if r["status"] in ("CONTACTED", "REPLIED", "BOOKED"):
        outcome_panel = (
            '<div class="panel" style="margin-top:18px"><h2 style="margin-top:0">Outcome (feedback loop)</h2>'
            '<p class="muted">Record the result so we can tune targeting over time.</p>'
            f'<form class="inline" method="post" action="/lead/{r["id"]}/outcome/won">'
            '<button class="ok" type="submit">Mark WON</button></form> '
            f'<form class="inline" method="post" action="/lead/{r["id"]}/outcome/lost">'
            '<button class="bad" type="submit">Mark LOST</button></form></div>')

    ev_rows = "".join(
        f'<tr><td class="muted">{e["at"]:%Y-%m-%d %H:%M}</td>'
        f'<td class="muted">{e["from_status"] or "—"} →</td><td>{badge(e["to_status"])}</td>'
        f'<td class="muted">{e["workflow"] or ""}</td>'
        f'<td class="muted">{(str(e["detail"]) if e["detail"] else "")[:70]}</td></tr>'
        for e in events) or '<tr><td class="empty" colspan=5>No events</td></tr>'

    body = f"""
    <a class="back" href="/leads">← all leads</a>
    <h2 style="margin-top:10px">{r["legal_name"]} &nbsp; {badge(r["status"])} &nbsp;
       <span class="seg">{r["icp_segment"] or ""}</span></h2>
    <div class="grid">
      <div class="panel"><h2 style="margin-top:0">Company</h2>
        <div class="kv">
          <div>Domain</div><div>{r["domain"] or "—"}</div>
          <div>Region / Country</div><div>{r["region"] or "—"} / {r["country"] or "—"}</div>
          <div>Employees</div><div>{r["employee_count"] or "?"}</div>
          <div>Niche</div><div>{r["niche"] or "—"}</div>
          <div>Tech stack</div><div>{", ".join(r["tech_stack"]) if r["tech_stack"] else "—"}</div>
          <div>Lighthouse</div><div>{r["lighthouse_mobile"] if r["lighthouse_mobile"] is not None else "—"}</div>
          <div>Contact</div><div>{r["first_name"] or ""} {r.get("last_name") or ""} · {r["email"] or "—"}</div>
          <div>Email check</div><div>{r["email_validation_status"] or "—"}</div>
        </div></div>
      <div class="panel"><h2 style="margin-top:0">{detail_title}</h2>{detail}</div>
    </div>
    {pitch}
    {out_panel}
    {outcome_panel}
    <div class="panel" style="margin-top:18px"><h2 style="margin-top:0">History (audit trail)</h2>
      <table><tr><th>When</th><th>From</th><th>To</th><th>WF</th><th>Detail</th></tr>{ev_rows}</table></div>
    """
    return page(body, "leads")


# ----------------------------------------------------------------------- outreach (WF-4, HITL)
_OUT_SELECT = ("SELECT l.id, l.email, l.first_name, l.icp_segment, l.qualify_trigger, l.pitch_subject, "
               "l.pitch_body, l.pitch_lang, c.legal_name, c.region, c.description, c.tech_stack, "
               "c.website_url, c.domain, c.lighthouse_mobile, c.last_verified_at ")
_OUT_WHERE = """
    FROM leads l JOIN companies c ON l.company_id = c.id
    WHERE l.status='QUALIFIED' AND l.pitch_body IS NOT NULL AND l.email IS NOT NULL
      AND l.email_validation_status IN ('valid','unverified')      -- never role/invalid accounts
      AND (c.country IS NULL OR c.country NOT IN ('DE','AT'))       -- opt-in-only regions -> manual, not cold mail
      AND l.suppressed_at IS NULL
      AND NOT EXISTS (SELECT 1 FROM suppression_list s WHERE s.target_value::text = l.email::text)
"""
_READY_SQL = _OUT_SELECT + _OUT_WHERE + " ORDER BY l.updated_at DESC LIMIT 100"
_ONE_READY_SQL = _OUT_SELECT + _OUT_WHERE + " AND l.id = %s LIMIT 1"


@app.route("/outreach")
def outreach_page():
    ready = query(_READY_SQL)
    sent = query("""SELECT c.legal_name, l.status, l.campaign_id, l.sending_domain, l.reply_sentiment
                    FROM leads l JOIN companies c ON l.company_id=c.id
                    WHERE l.status IN ('QUEUED_FOR_OUTREACH','CONTACTED','REPLIED','BOOKED','SUPPRESSED')
                    ORDER BY l.updated_at DESC LIMIT 25""")

    def card(l):
        pv = outreach.build_payload(l)
        rtl = ' dir="rtl"' if l["pitch_lang"] == "ar" else ""
        subj = html.escape(l["pitch_subject"] or "")
        body = l["pitch_body"] or ""
        rows = max(8, min(30, body.count("\n") + sum(len(x) // 80 + 1 for x in body.split("\n")) + 1))
        # fact-check context: the company's OWN homepage words + the verified trigger + detected tech
        site = html.escape((l.get("description") or "").strip())
        tech = ", ".join(l.get("tech_stack") or [])
        site_link = (f' &nbsp;<a class="muted" href="{html.escape(l["website_url"])}" target="_blank">open site &#8599;</a>'
                     if l.get("website_url") else "")
        # Google mobile-speed score + a clickable link to the LIVE public report (the verifiable source)
        speed = ""
        if l.get("lighthouse_mobile") is not None:
            report = psi_report_url(l)
            measured = f" (measured {l['last_verified_at']:%m-%d})" if l.get("last_verified_at") else ""
            link = (f' &nbsp;<a class="muted" href="{html.escape(report)}" target="_blank">open report &#8599;</a>'
                    if report else "")
            speed = f'<br><b>Google mobile speed:</b> {l["lighthouse_mobile"]}/100{measured}{link}'
        factbox = (f'<div class="factbox">FACT-CHECK &nbsp; <b>Trigger:</b> {html.escape(l["qualify_trigger"] or "—")}'
                   f'{" &nbsp; <b>Tech:</b> " + html.escape(tech) if tech else ""}{site_link}'
                   f'{"<br><b>Homepage says:</b> " + site if site else ""}{speed}</div>')
        return f"""
        <div class="panel" style="margin-top:12px">
          <div><b>{html.escape(l['legal_name'] or '')}</b> <span class="seg">{l['icp_segment'] or ''}</span>
               <span class="muted">&nbsp; {html.escape(l['email'] or '')} &nbsp;·&nbsp; {pv['campaign_id']} via {pv['sending_account']}</span></div>
          {factbox}
          {factcheck_preview(l)}
          <form method="post" action="/outreach/{l['id']}/save" oninput="this.querySelector('.cc').textContent=this.body.value.length+' chars'">
            <label>Subject</label>
            <input type="text" name="subject" value="{subj}"{rtl}>
            <label style="margin-top:8px">Body (edit freely, then Save or Approve)</label>
            <textarea name="body" rows="{rows}"{rtl}>{html.escape(body)}</textarea>
            <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
              <button class="ghost" type="submit" formaction="/outreach/{l['id']}/save">Save edits</button>
              <button class="ok" type="submit" formaction="/outreach/{l['id']}/approve">Save &amp; queue</button>
              <button class="bad" type="submit" formaction="/outreach/{l['id']}/skip"
                      formnovalidate onclick="return confirm('Skip this lead? It moves to COOLDOWN.')">Skip</button>
              <span class="cc muted" style="margin-left:auto">{len(body)} chars</span>
            </div>
          </form>
        </div>"""

    cards = "".join(card(l) for l in ready) or \
        '<div class="empty panel">No leads awaiting outreach approval.</div>'
    sent_rows = "".join(
        f'<tr><td>{s["legal_name"]}</td><td>{badge(s["status"])}</td>'
        f'<td class="muted">{s["campaign_id"] or "—"}</td><td class="muted">{s["sending_domain"] or "—"}</td>'
        f'<td class="muted">{s["reply_sentiment"] or ""}</td></tr>' for s in sent) or \
        '<tr><td class="empty" colspan=5>nothing queued yet</td></tr>'

    body = f"""
    <h2>Outreach review · {len(ready)} awaiting approval</h2>
    <p class="muted">Review each pitch, then approve to queue it. Nothing sends — DRY RUN is
       {'ON' if outreach.DRY_RUN else 'OFF'}; the sender (wf4.py) logs the payload and marks CONTACTED.</p>
    {cards}
    <h2 style="margin-top:28px">Outreach progress</h2>
    <div class="panel"><table>
      <tr><th>Company</th><th>Status</th><th>Campaign</th><th>Sending domain</th><th>Reply</th></tr>
      {sent_rows}</table></div>"""
    return page(body, "outreach")


def _save_pitch_edits(lead_id):
    """Persist human edits to the subject/body — ONLY while the lead is still QUALIFIED (pre-queue).
    Returns True if a change was written. Logs an 'edited' audit event."""
    subject = (request.form.get("subject") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not body:                       # never let an empty body overwrite a real pitch
        return False
    changed = execute(
        """UPDATE leads SET pitch_subject=%s, pitch_body=%s, personalized_pitch=%s, updated_at=now()
           WHERE id=%s AND status='QUALIFIED'
             AND (pitch_subject IS DISTINCT FROM %s OR pitch_body IS DISTINCT FROM %s)
           RETURNING id;""",
        (subject, body, body, lead_id, subject, body), returning=True)
    if changed:
        execute("INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail) "
                "VALUES (%s, 'QUALIFIED', 'QUALIFIED', 'human-edit', %s);",
                (lead_id, Json({"edited": "pitch", "chars": len(body)})))
    return bool(changed)


@app.route("/outreach/<lead_id>/save", methods=["POST"])
def outreach_save(lead_id):
    _save_pitch_edits(lead_id)
    return redirect("/outreach")


@app.route("/outreach/<lead_id>/approve", methods=["POST"])
def outreach_approve(lead_id):
    _save_pitch_edits(lead_id)          # bank any edits shown in the form before queuing
    row = query(_ONE_READY_SQL, (lead_id,), one=True)
    if not row:
        return redirect("/outreach")
    payload = outreach.build_payload(row)
    execute("""UPDATE leads SET status='QUEUED_FOR_OUTREACH', outreach_provider=%s, campaign_id=%s,
                   sending_domain=%s, updated_at=now() WHERE id=%s;""",
            (payload["provider"], payload["campaign_id"], payload["_meta"]["sending_domain"], lead_id))
    execute("""INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail)
               VALUES (%s, 'QUALIFIED', 'QUEUED_FOR_OUTREACH', 'WF-4', %s);""",
            (lead_id, Json({"campaign_id": payload["campaign_id"],
                            "sending_account": payload["sending_account"]})))
    return redirect("/outreach")


@app.route("/outreach/<lead_id>/skip", methods=["POST"])
def outreach_skip(lead_id):
    # Rest the lead for COOLDOWN_DAYS days, then it auto-returns to QUALIFIED (see rearm_cooldown).
    execute("""UPDATE leads SET status='COOLDOWN',
                      cooldown_until = now() + make_interval(days => %s), updated_at=now()
                WHERE id=%s AND status='QUALIFIED';""",
            (rearm_cooldown.COOLDOWN_DAYS, lead_id))
    execute("INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail) "
            "VALUES (%s, 'QUALIFIED', 'COOLDOWN', 'WF-4', %s);",
            (lead_id, Json({"reason": "skipped at outreach review",
                            "cooldown_days": rearm_cooldown.COOLDOWN_DAYS})))
    return redirect("/outreach")


@app.route("/lead/<lead_id>/rearm", methods=["POST"])
def lead_rearm(lead_id):
    """Manual button: move a COOLDOWN lead back into the pipeline right now (-> QUALIFIED)."""
    execute("""UPDATE leads SET status='QUALIFIED', cooldown_until=NULL, updated_at=now()
                WHERE id=%s AND status='COOLDOWN';""", (lead_id,))
    execute("INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail) "
            "VALUES (%s, 'COOLDOWN', 'QUALIFIED', 'rearm', %s);",
            (lead_id, Json({"reason": "manually re-armed from dashboard"})))
    return redirect(request.referrer or "/leads")


@app.route("/lead/<lead_id>/contact", methods=["GET", "POST"])
def lead_contact(lead_id):
    """Add a real named contact to a NEEDS_CONTACT lead, then re-queue it (-> ENRICHED) so WF-3
    writes it a pitch. Turns 'good company, no contact' into a sendable lead."""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        first = (request.form.get("first_name") or "").strip()
        last = (request.form.get("last_name") or "").strip()
        title = (request.form.get("job_title") or "").strip()
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            return redirect(f"/lead/{lead_id}/contact?err=bad")
        try:
            execute("""UPDATE leads SET email=%s, first_name=%s, last_name=%s, job_title=%s,
                          email_validation_status='unverified', status='ENRICHED', updated_at=now()
                        WHERE id=%s AND status='NEEDS_CONTACT';""",
                    (email, first or None, last or None, title or None, lead_id))
        except Exception:   # noqa: BLE001 — most likely the email is already used by another lead
            return redirect(f"/lead/{lead_id}/contact?err=dup")
        execute("INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail) "
                "VALUES (%s, 'NEEDS_CONTACT', 'ENRICHED', 'human-contact', %s);",
                (lead_id, Json({"email_added": True, "named": bool(first or last)})))
        return redirect("/leads?added=1")

    row = query("""SELECT l.id, l.email, l.first_name, l.last_name, l.job_title, l.status,
                          c.legal_name, c.website_url
                     FROM leads l JOIN companies c ON l.company_id=c.id WHERE l.id=%s""",
                (lead_id,), one=True)
    if not row:
        abort(404)
    err = request.args.get("err")
    warn = ""
    if err == "bad":
        warn = '<div class="factbox" style="border-left-color:var(--bad)">Please enter a valid email address.</div>'
    elif err == "dup":
        warn = '<div class="factbox" style="border-left-color:var(--bad)">That email is already used by another lead.</div>'
    site = (f' &nbsp;<a class="muted" href="{html.escape(row["website_url"])}" target="_blank">open site &#8599;</a>'
            if row.get("website_url") else "")
    def val(k):
        return html.escape(str(row.get(k) or ""))
    body = f"""
    <h2>Add a contact &nbsp;<span class="muted">{html.escape(row['legal_name'] or '')}</span></h2>
    <p class="muted" style="max-width:560px">This company qualified but has no cold-emailable contact.
       Add a real named person (from their site, LinkedIn, etc.){site} and it re-enters the pipeline to
       get a pitch on the next WF-3 run.</p>
    {warn}
    <form method="post" class="panel" style="max-width:520px">
      <label>First name</label><input type="text" name="first_name" value="{val('first_name')}">
      <label style="margin-top:8px">Last name</label><input type="text" name="last_name" value="{val('last_name')}">
      <label style="margin-top:8px">Job title</label><input type="text" name="job_title" value="{val('job_title')}" placeholder="e.g. Owner, Marketing Manager">
      <label style="margin-top:8px">Email <span style="color:var(--bad)">*</span></label>
      <input type="email" name="email" value="{val('email')}" required placeholder="name@company.com">
      <div style="margin-top:14px">
        <button class="ok" type="submit">Save contact &amp; re-queue</button>
        <a href="/leads?status=NEEDS_CONTACT" class="muted" style="margin-left:12px">Cancel</a>
      </div>
    </form>"""
    return page(body, "leads")


@app.route("/lead/<lead_id>/outcome/<result>", methods=["POST"])
def lead_outcome(lead_id, result):
    to_status = {"won": "WON", "lost": "LOST"}.get(result)
    if to_status:
        execute("UPDATE leads SET status=%s, updated_at=now() WHERE id=%s;", (to_status, lead_id))
        execute("INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail) "
                "VALUES (%s, NULL, %s, 'feedback', %s);", (lead_id, to_status, Json({"outcome": result})))
    return redirect(f"/lead/{lead_id}")


# ----------------------------------------------------------------------- Phase-6 tracking endpoints
@app.route("/t/open/<lead_id>")
def track_open(lead_id):
    """Open-tracking pixel: logs a VIEW when the recipient's client loads the 1x1 image."""
    try:
        step = int(request.args.get("step", "0"))
    except ValueError:
        step = 0
    try:
        execute("INSERT INTO email_events (lead_id, step, event_type, detail) VALUES (%s,%s,'open',%s)",
                (lead_id, step, Json({"ua": request.headers.get("User-Agent", "")[:200]})))
        execute("UPDATE leads SET first_open_at = COALESCE(first_open_at, now()) WHERE id=%s", (lead_id,))
    except Exception:               # noqa: BLE001 — a tracking miss must never error the recipient's client
        pass
    return Response(_PIXEL_GIF, mimetype="image/gif",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.route("/t/click/<lead_id>")
def track_click(lead_id):
    """Link wrapper: logs a CLICK, then 302-redirects to the real destination."""
    url = request.args.get("u") or outreach.WEBSITE_URL
    try:
        step = int(request.args.get("step", "0"))
    except ValueError:
        step = 0
    try:
        execute("INSERT INTO email_events (lead_id, step, event_type, url, detail) "
                "VALUES (%s,%s,'click',%s,%s)",
                (lead_id, step, url, Json({"ua": request.headers.get("User-Agent", "")[:200]})))
        # a click implies the mail was opened, even if the pixel was blocked
        execute("UPDATE leads SET first_click_at = COALESCE(first_click_at, now()), "
                "first_open_at = COALESCE(first_open_at, now()) WHERE id=%s", (lead_id,))
    except Exception:               # noqa: BLE001
        pass
    if not re.match(r"^https?://", url or ""):
        url = outreach.WEBSITE_URL
    return redirect(url, code=302)


# ----------------------------------------------------------------------- analytics (full funnel §6)
_FUNNEL_ORDER = ["DISCOVERED", "ENRICHED", "QUALIFIED", "QUEUED_FOR_OUTREACH", "CONTACTED", "REPLIED", "BOOKED"]


def _pct(a, b):
    return round(100 * a / b) if b else 0


def _tally(rows):
    """Funnel counts over the SENT population of a lead subset."""
    sent = [r for r in rows if r["sent"]]
    n = len(sent)
    return {"sent": n,
            "opened": sum(1 for r in sent if r["opened"]),
            "clicked": sum(1 for r in sent if r["clicked"]),
            "replied": sum(1 for r in sent if r["replied"]),
            "booked": sum(1 for r in sent if r["booked"]),
            "bounced": sum(1 for r in sent if r["bounced"])}


# role-inbox local-parts, for the "personal vs info@" cohort (mirrors the enrichment relaxation rule)
_ROLE_LOCALPARTS = {"info", "contact", "hello", "admin", "sales", "office", "enquiries", "enquiry",
                    "mail", "support", "team", "hi", "hallo", "kontakt", "reception", "orders",
                    "bookings", "hey", "customerservice", "care", "marketing", "hr"}
_STEP_LBL = {0: "initial", 1: "nudge", 2: "issue", 3: "check", 4: "breakup"}


def _email_kind(email):
    """Classify an address for the cohort: 'Personal' vs 'Role (info@…)'."""
    if not email:
        return "—"
    local = email.split("@")[0].lower()
    first = re.split(r"[._+-]", local)[0]
    return "Role (info@…)" if (first in _ROLE_LOCALPARTS or local in _ROLE_LOCALPARTS) else "Personal"


def _click_kind(url):
    """Bucket a clicked URL for the click-by-link-type panel."""
    u = (url or "").lower()
    if "unsub" in u:
        return "Unsubscribe"
    if "calendar.app.google" in u or "cal.com" in u:
        return "Calendar / booking"
    if "granjur" in u:
        return "Website (granjur)"
    return "Other link"


def _dot(ok):
    return ('<span style="color:#16a34a">&#9679;</span>' if ok
            else '<span style="color:#cbd5e1">&#9679;</span>')


@app.route("/analytics")
def analytics():
    # per-lead funnel flags straight from telemetry (real-time on each load)
    funnel = query("""
        SELECT l.id, l.icp_segment, c.region, l.email, c.legal_name, l.bounce_count, c.source,
          EXISTS(SELECT 1 FROM outreach_log o WHERE o.lead_id=l.id AND o.outcome IN ('sent','logged')) AS sent,
          EXISTS(SELECT 1 FROM email_events e WHERE e.lead_id=l.id AND e.event_type IN ('open','click')) AS opened,
          EXISTS(SELECT 1 FROM email_events e WHERE e.lead_id=l.id AND e.event_type='click') AS clicked,
          EXISTS(SELECT 1 FROM lead_events le WHERE le.lead_id=l.id AND le.to_status='REPLIED') AS replied,
          EXISTS(SELECT 1 FROM lead_events le WHERE le.lead_id=l.id AND le.to_status='BOOKED') AS booked,
          (EXISTS(SELECT 1 FROM email_events e WHERE e.lead_id=l.id AND e.event_type='bounce')
             OR l.bounced_at IS NOT NULL) AS bounced
        FROM leads l JOIN companies c ON c.id=l.company_id""")
    for r in funnel:                                      # cohort tag for the personal-vs-role matrix
        r["email_kind"] = _email_kind(r["email"])
    T = _tally(funnel)

    # KPI tiles
    kpis = [("Sent", T["sent"], ""), ("Open rate", f'{_pct(T["opened"], T["sent"])}%', "go"),
            ("Click rate", f'{_pct(T["clicked"], T["sent"])}%', "go"),
            ("Reply rate", f'{_pct(T["replied"], T["sent"])}%', "ok"),
            ("Booked", T["booked"], "ok"), ("Bounce rate", f'{_pct(T["bounced"], T["sent"])}%', "bad")]
    kpi_html = "".join(f'<div class="stat {c}"><div class="n">{v}</div><div class="l">{l}</div></div>'
                       for l, v, c in kpis)

    # email funnel bars (Sent -> Opened -> Clicked -> Replied -> Booked)
    stages = [("Sent", T["sent"]), ("Opened", T["opened"]), ("Clicked", T["clicked"]),
              ("Replied", T["replied"]), ("Booked", T["booked"])]
    base = T["sent"] or 1
    fbars = "".join(
        f'<tr><td class="seg">{name}</td>'
        f'<td style="width:55%"><div class="bar"><i style="width:{_pct(n, base)}%"></i></div></td>'
        f'<td>{n}</td><td class="muted">{_pct(n, base)}% of sent</td></tr>' for name, n in stages)

    def matrix(group_key, order=None):
        groups = {}
        for r in funnel:
            if not r["sent"]:
                continue
            k = (r.get(group_key) or "—")
            groups.setdefault(k, []).append(r)
        keys = order or sorted(groups)
        out = ""
        for k in keys:
            if k not in groups:
                continue
            d = _tally(groups[k])
            out += (f'<tr><td class="seg">{k}</td><td>{d["sent"]}</td>'
                    f'<td>{_pct(d["opened"], d["sent"])}%</td><td>{_pct(d["clicked"], d["sent"])}%</td>'
                    f'<td>{_pct(d["replied"], d["sent"])}%</td><td>{_pct(d["booked"], d["sent"])}%</td>'
                    f'<td class="muted">{d["bounced"]}</td></tr>')
        return out or '<tr><td class="empty" colspan=7>no sent emails yet</td></tr>'

    seg_matrix = matrix("icp_segment", order=["A_LEGACY_BRICK", "B_FUNDED_STARTUP", "C_LOWTECH_ECOM"])
    reg_matrix = matrix("region")

    # dead / bounced emails (unused-email extraction)
    dead = [r for r in funnel if r["bounced"]]
    dead_rows = "".join(
        f'<tr><td>{(r["legal_name"] or "?")[:30]}</td><td class="muted">{r["email"] or "—"}</td>'
        f'<td><span class="badge">{r["region"] or "—"}</span></td>'
        f'<td class="muted">{r["bounce_count"] or 0}</td></tr>' for r in dead) or \
        '<tr><td class="empty" colspan=4>No bounces — run the bounce parser to scan the inbox.</td></tr>'

    # keep the pipeline status funnel (discovery -> booked) for context
    reached = {r["to_status"]: r["n"] for r in
               query("SELECT to_status, COUNT(DISTINCT lead_id) n FROM lead_events GROUP BY to_status")}
    ptop = reached.get("DISCOVERED", 0) or 1
    pipe_rows = "".join(
        f'<tr><td class="seg">{s}</td>'
        f'<td style="width:55%"><div class="bar"><i style="width:{_pct(reached.get(s,0), ptop)}%"></i></div></td>'
        f'<td>{reached.get(s,0)}</td></tr>' for s in _FUNNEL_ORDER)

    # ---- §1 Per-email drill-down: one row per contacted lead (links to its full timeline) ----
    per_email = query("""
        SELECT l.id, c.legal_name, l.email, c.region, l.status::text AS status,
          (SELECT min(created_at) FROM outreach_log o WHERE o.lead_id=l.id AND o.step=0
             AND o.outcome IN ('sent','logged')) AS sent_at,
          (SELECT max(step)      FROM outreach_log o WHERE o.lead_id=l.id
             AND o.outcome IN ('sent','logged')) AS step,
          (SELECT count(*) FROM email_events e WHERE e.lead_id=l.id AND e.event_type='open')  AS opens,
          (SELECT count(*) FROM email_events e WHERE e.lead_id=l.id AND e.event_type='click') AS clicks,
          EXISTS(SELECT 1 FROM lead_events le WHERE le.lead_id=l.id AND le.to_status='REPLIED') AS replied,
          EXISTS(SELECT 1 FROM lead_events le WHERE le.lead_id=l.id AND le.to_status='BOOKED') AS booked,
          (l.bounced_at IS NOT NULL) AS bounced
        FROM leads l JOIN companies c ON c.id=l.company_id
        WHERE EXISTS(SELECT 1 FROM outreach_log o WHERE o.lead_id=l.id AND o.outcome IN ('sent','logged'))
        ORDER BY sent_at DESC NULLS LAST LIMIT 300""")
    pe_rows = ""
    for r in per_email:
        sent_at = r["sent_at"].strftime("%m-%d %H:%M") if r["sent_at"] else "—"
        delivered = bool(r["sent_at"]) and not r["bounced"]
        opens, clicks = r["opens"] or 0, r["clicks"] or 0
        status_cell = ('<span style="color:#dc2626">bounced</span>' if r["bounced"]
                       else html.escape(r["status"]))
        pe_rows += (
            "<tr>"
            f'<td><a href="/analytics/lead/{r["id"]}">{html.escape((r["legal_name"] or "?")[:24])}</a></td>'
            f'<td class="muted">{html.escape((r["email"] or "—")[:28])}</td>'
            f'<td><span class="badge">{r["region"] or "—"}</span></td>'
            f'<td class="muted">{sent_at}</td>'
            f'<td class="muted">{_STEP_LBL.get(r["step"] or 0, r["step"])}</td>'
            f'<td style="text-align:center">{_dot(delivered)}</td>'
            f'<td style="text-align:center">{_dot(opens > 0)}<span class="muted"> {opens or ""}</span></td>'
            f'<td style="text-align:center">{_dot(clicks > 0)}<span class="muted"> {clicks or ""}</span></td>'
            f'<td style="text-align:center">{_dot(r["replied"])}</td>'
            f'<td style="text-align:center">{_dot(r["booked"])}</td>'
            f'<td>{status_cell}</td></tr>')
    pe_rows = pe_rows or '<tr><td class="empty" colspan=11>No emails sent yet — run the sender (dry-run or live).</td></tr>'

    # ---- §2 Clicks by link type (website vs calendar vs unsubscribe) ----
    kinds = {"Website (granjur)": 0, "Calendar / booking": 0, "Unsubscribe": 0, "Other link": 0}
    for r in query("SELECT url, count(*) n FROM email_events WHERE event_type='click' GROUP BY url"):
        kinds[_click_kind(r["url"])] = kinds.get(_click_kind(r["url"]), 0) + r["n"]
    kinds["Calendar / booking"] += T["booked"]           # booking button is a direct link -> counted as bookings
    click_rows = "".join(f'<tr><td class="seg">{k}</td><td>{v}</td></tr>' for k, v in kinds.items())

    # ---- §3 Cohort conversion by source + personal-vs-role inbox ----
    src_matrix = matrix("source")
    kind_matrix = matrix("email_kind")

    # ---- §4 Daily sends vs the daily quota target ----
    target = int(os.getenv("GRANJUR_DAILY_TARGET", "19"))
    daily = query("""SELECT to_char(date_trunc('day', created_at),'YYYY-MM-DD') d, count(*) n
                     FROM outreach_log WHERE outcome IN ('sent','logged')
                     GROUP BY 1 ORDER BY 1 DESC LIMIT 14""")
    daily_rows = "".join(
        f'<tr><td class="seg">{r["d"]}</td>'
        f'<td style="width:50%"><div class="bar"><i style="width:{min(100, _pct(r["n"], target))}%"></i></div></td>'
        f'<td>{r["n"]} / {target}</td>'
        f'<td class="muted">{"met" if r["n"] >= target else "under"}</td></tr>' for r in daily) or \
        '<tr><td class="empty" colspan=4>No sends logged yet.</td></tr>'

    body = f"""
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 style="margin:0">Email performance</h2>
      <a href="/report.xlsx" class="badge" style="text-decoration:none;padding:8px 14px;font-size:13px">
        &#11015; Download central Excel database</a>
    </div>
    <div class="funnel" style="margin-top:16px">{kpi_html}</div>

    <h2 style="margin-top:24px">Conversion funnel — Sent &rarr; Opened &rarr; Clicked &rarr; Replied &rarr; Booked</h2>
    <div class="panel"><table>
      <tr><th>Stage</th><th></th><th>Leads</th><th>rate</th></tr>{fbars}</table>
      <div class="muted" style="font-size:12px;margin-top:10px">Opens = tracking pixel, clicks = wrapped
        links (incl. the personalized calendar CTA), replies/bookings = inbox + Cal.com. All free.</div></div>

    <h2 style="margin-top:24px">Every email — per-recipient tracking</h2>
    <div class="panel"><div style="overflow-x:auto"><table>
      <tr><th>Company</th><th>Email</th><th>Region</th><th>Sent</th><th>Step</th>
          <th>Deliv.</th><th>Open</th><th>Click</th><th>Reply</th><th>Book</th><th>Status</th></tr>
      {pe_rows}</table></div>
      <div class="muted" style="font-size:12px;margin-top:8px">Green dot = it happened; number = count.
        Click a company to open its full event timeline. Open/click counts fill in after <b>real sends</b>
        through a public tracking URL (set <b>GRANJUR_TRACK_BASE</b> to a domain/tunnel; localhost only tracks you).</div></div>

    <div class="grid" style="margin-top:24px">
      <div class="panel"><h2 style="margin-top:0">Clicks by link type</h2>
        <table><tr><th>Link</th><th>Clicks</th></tr>{click_rows}</table>
        <div class="muted" style="font-size:12px;margin-top:8px">The website link is click-tracked; the calendar
          button is a direct Google link, so it's measured by <b>bookings</b>.</div></div>
      <div class="panel"><h2 style="margin-top:0">Daily sends vs quota ({target}/day)</h2>
        <table><tr><th>Day</th><th></th><th>Sent</th><th></th></tr>{daily_rows}</table>
        <div class="muted" style="font-size:12px;margin-top:8px">Only send-ready leads count; errors and
          needs-contact never do.</div></div>
    </div>

    <div class="grid" style="margin-top:24px">
      <div class="panel"><h2 style="margin-top:0">Conversion by source</h2>
        <table><tr><th>Source</th><th>Sent</th><th>Open</th><th>Click</th><th>Reply</th><th>Book</th><th>Bnc</th></tr>
        {src_matrix}</table>
        <div class="muted" style="font-size:12px;margin-top:8px">Which discovery source produces
          converting leads (OSM / job board / seed / quota).</div></div>
      <div class="panel"><h2 style="margin-top:0">Personal vs role inbox (info@)</h2>
        <table><tr><th>Email type</th><th>Sent</th><th>Open</th><th>Click</th><th>Reply</th><th>Book</th><th>Bnc</th></tr>
        {kind_matrix}</table>
        <div class="muted" style="font-size:12px;margin-top:8px">Validates the relaxed-role decision: does
          emailing <b>info@</b> for small businesses actually convert?</div></div>
    </div>

    <div class="grid" style="margin-top:24px">
      <div class="panel"><h2 style="margin-top:0">Conversion by segment</h2>
        <table><tr><th>Segment</th><th>Sent</th><th>Open</th><th>Click</th><th>Reply</th><th>Book</th><th>Bnc</th></tr>
        {seg_matrix}</table></div>
      <div class="panel"><h2 style="margin-top:0">Conversion by region</h2>
        <table><tr><th>Region</th><th>Sent</th><th>Open</th><th>Click</th><th>Reply</th><th>Book</th><th>Bnc</th></tr>
        {reg_matrix}</table></div>
    </div>

    <div class="grid" style="margin-top:24px">
      <div class="panel"><h2 style="margin-top:0">Dead / bounced emails</h2>
        <table><tr><th>Company</th><th>Email</th><th>Region</th><th>Bounces</th></tr>{dead_rows}</table>
        <div class="muted" style="font-size:12px;margin-top:8px">Detected by
          <b>python wf4_python/bounce_parser.py</b> (Gmail IMAP) — dead addresses are suppressed automatically.</div></div>
      <div class="panel"><h2 style="margin-top:0">Pipeline funnel (discovery &rarr; booked)</h2>
        <table><tr><th>Stage</th><th></th><th>Leads</th></tr>{pipe_rows}</table></div>
    </div>
    <p class="muted" style="margin-top:16px">Real-time telemetry from <b>email_events</b> (pixel/clicks),
      <b>outreach_log</b> (sends) and the audit trail — 100% free, no paid analytics provider.</p>"""
    return page(body, "analytics")


@app.route("/analytics/lead/<lead_id>")
def analytics_lead(lead_id):
    """Per-email timeline: every event for one recipient (sent -> opened -> clicked -> replied -> booked)."""
    lead = query("""SELECT l.id, l.email, l.status::text AS status, l.icp_segment, c.legal_name,
                      c.region, c.source, c.website_url, l.pitch_subject
                    FROM leads l JOIN companies c ON c.id=l.company_id WHERE l.id=%s""",
                 (lead_id,), one=True)
    if not lead:
        abort(404)
    # merge three event streams into one timeline
    events = []
    for o in query("""SELECT created_at at, step, outcome, subject, sending_domain
                      FROM outreach_log WHERE lead_id=%s""", (lead_id,)):
        verb = "Sent" if o["outcome"] in ("sent", "logged") else o["outcome"].title()
        events.append((o["at"], f'{verb} ({_STEP_LBL.get(o["step"] or 0, o["step"])})',
                       html.escape(o["subject"] or "")))
    for e in query("""SELECT at, event_type, url FROM email_events WHERE lead_id=%s""", (lead_id,)):
        label = {"open": "Opened", "click": "Clicked", "bounce": "Bounced"}.get(e["event_type"], e["event_type"])
        events.append((e["at"], label, html.escape((e["url"] or "")[:80])))
    for le in query("""SELECT at, from_status, to_status, workflow FROM lead_events WHERE lead_id=%s""", (lead_id,)):
        events.append((le["at"], f'Status &rarr; {le["to_status"]}', html.escape(le["workflow"] or "")))
    events.sort(key=lambda x: x[0])
    rows = "".join(
        f'<tr><td class="muted" style="white-space:nowrap">{at:%Y-%m-%d %H:%M}</td>'
        f'<td><b>{lbl}</b></td><td class="muted">{detail}</td></tr>' for at, lbl, detail in events) or \
        '<tr><td class="empty" colspan=3>No events recorded yet.</td></tr>'
    body = f"""
    <p><a href="/analytics">&larr; back to Analytics</a></p>
    <h2>{html.escape(lead["legal_name"] or "?")}</h2>
    <p class="muted">{html.escape(lead["email"] or "—")} · {lead["region"] or "—"} ·
      source: {lead["source"] or "—"} · status: <b>{lead["status"]}</b></p>
    <div class="panel"><h2 style="margin-top:0">Email timeline</h2>
      <table><tr><th>When</th><th>Event</th><th>Detail</th></tr>{rows}</table></div>"""
    return page(body, "analytics")


_EXPORTER = None


def _exporter():
    """Lazy-load the project-root export_leads_csv module (kept out of import time)."""
    global _EXPORTER
    if _EXPORTER is None:
        root = pathlib.Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("export_leads_csv", root / "scripts" / "export_leads_csv.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _EXPORTER = m
    return _EXPORTER


@app.route("/report.xlsx")
def report_xlsx():
    """Serve the central Excel database, refreshed FROM THE LIVE DB on every request so its Summary and
    Latest Leads sheets always reflect each company's current stage. The Runs Log history is preserved;
    a plain download does not append a run row (that only happens on a real pipeline run)."""
    try:
        data = _exporter().central_bytes()
    except Exception as e:  # noqa: BLE001
        return f"Report generation failed: {e}", 500
    return Response(data,
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="granjur_central.xlsx"'})


# ----------------------------------------------------------------------- regions (Phase-1 send-window advisor)
_STATE_CLASS = {"PRIME": "ok", "GOOD": "go", "OFF": "", "WEEKEND": "bad", "HOLIDAY": "bad"}
_STATE_LABEL = {"PRIME": "PRIME · send now", "GOOD": "good · send now",
                "OFF": "off-hours", "WEEKEND": "weekend", "HOLIDAY": "public holiday"}


_SEND_REGIONS = {"US", "EU", "UK", "GCC", "CN", "AU"}


@app.route("/regions/send/<region>", methods=["POST"])
def regions_send(region):
    """Mass-send THIS region's queued emails by running WF-4 region-isolated. Respects GRANJUR_DRY_RUN
    (safe dry-run unless you've set it to 0) and the send-window + warmup gates. Redirects back with a
    one-line result banner. Optional 'force' bypasses the local-window gate (still honours warmup)."""
    region = (region or "").upper()
    if region not in _SEND_REGIONS:
        return redirect("/regions")
    wf4_dir = str(pathlib.Path(__file__).resolve().parent.parent / "wf4_python")
    env = dict(os.environ, GRANJUR_REGION=region)
    args = [sys.executable, "wf4.py"]
    # If a test inbox is configured, the button PREVIEWS to your own inbox (real send, DB untouched,
    # bypasses the window/warmup gate). Unset GRANJUR_REGION_TEST_INBOX for normal dry-run/live sends.
    test_inbox = os.getenv("GRANJUR_REGION_TEST_INBOX", "").strip()
    if test_inbox:
        args += ["--test", test_inbox]
    if request.form.get("force") == "1":
        args.append("--ignore-window")
    try:
        proc = subprocess.run(args, cwd=wf4_dir, env=env, capture_output=True, text=True, timeout=300)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        line = next((l.strip() for l in out.splitlines() if l.strip().startswith("Done.")), None)
        if not line:  # e.g. "No QUEUED_FOR_OUTREACH leads" or an error
            line = next((l.strip() for l in out.splitlines() if l.strip()), "no output")
    except Exception as e:  # noqa: BLE001 — surface any failure to the banner, never 500 the page
        line = f"send failed: {e}"
    return redirect(f"/regions?msg={quote(f'[{region}] ' + line[:240])}")


@app.route("/regions")
def regions():
    advisor = sw.all_regions()                 # every region, best-to-send-now first (DST-accurate)
    test_inbox = os.getenv("GRANJUR_REGION_TEST_INBOX", "").strip()   # buttons preview to your inbox if set
    _mode_word = (f"TEST -> {test_inbox}" if test_inbox
                  else ("LIVE" if not outreach.DRY_RUN else "DRY RUN (safe)"))

    # region-wise lead counts straight from the DB (Phase-1 data)
    counts = {r["region"]: r for r in query("""
        SELECT c.region,
               COUNT(*)                                              AS total,
               COUNT(*) FILTER (WHERE l.status='QUEUED_FOR_OUTREACH') AS queued,
               COUNT(*) FILTER (WHERE l.status='CONTACTED')          AS contacted,
               COUNT(*) FILTER (WHERE l.status IN ('QUALIFIED','ENRICHED','NEEDS_CONTACT')) AS pipeline
        FROM leads l JOIN companies c ON l.company_id=c.id
        GROUP BY c.region""")}

    # --- advisor cards (the visual clock: who to email now, who later) ---
    cards = ""
    for r in advisor:
        cls = _STATE_CLASS[r["state"]]
        badge_html = f'<span class="badge {cls}">{_STATE_LABEL[r["state"]]}</span>'
        if r["state"] in ("PRIME", "GOOD"):
            when = f'window {r["window"]}'
        elif r["state"] == "HOLIDAY":
            when = f'<b>{r["holiday"]}</b> &nbsp;·&nbsp; {r["opens_label"]}'
        else:
            when = r["opens_label"] + (f' &nbsp;·&nbsp; in {r["opens_in"]}' if r["opens_in"] else "")
        q = counts.get(r["region"], {}).get("queued", 0) or 0
        ready = r["state"] in ("PRIME", "GOOD")
        qline = (f'<div style="margin-top:4px;color:var(--ok);font-size:12px;font-weight:600">'
                 f'{q} queued — good time to send</div>'
                 if q and ready
                 else (f'<div class="muted" style="font-size:12px;margin-top:4px">{q} queued</div>' if q else ""))
        # per-region mass-send button (only when something is queued)
        send_btn = ""
        if q:
            label = f"Send {q} now" if ready else f"Send {q} (off-hours)"
            confirm = f"Send all {q} queued {r['region']} email(s) now? Mode: {_mode_word}."
            send_btn = (
                f'<form method="post" action="/regions/send/{r["region"]}" style="margin-top:9px" '
                f'onsubmit="return confirm(\'{confirm}\')">'
                f'<button class="{"go" if ready else "ghost"}" type="submit" '
                f'style="width:100%;font-size:12px;padding:7px 10px">{label}</button></form>')
        cards += (f'<div class="stat {cls}" style="min-width:172px">'
                  f'<div class="n" style="font-size:23px">{r["local_time"]}</div>'
                  f'<div class="l">{r["region"]} &nbsp;·&nbsp; {r["local_day"]}</div>'
                  f'<div style="margin-top:7px">{badge_html}</div>'
                  f'<div class="muted" style="font-size:12px;margin-top:5px">{when}</div>'
                  f'<div class="muted" style="font-size:11px;margin-top:2px">{r["tz"]}</div>'
                  f'{qline}{send_btn}</div>')

    # --- region overview table ---
    order = {r["region"]: i for i, r in enumerate(advisor)}
    state_of = {r["region"]: r for r in advisor}
    seen = sorted(counts, key=lambda rg: order.get(rg, 99))
    trs = ""
    for rg in seen:
        c = counts[rg]
        s = state_of.get(rg) or sw.region_status(rg)
        cls = _STATE_CLASS[s["state"]]
        ready = c["queued"] and s["state"] in ("PRIME", "GOOD")
        action = ('<span style="color:var(--ok);font-weight:600">send now</span>' if ready
                  else (f'<span class="muted">hold · {s["opens_label"]}</span>' if c["queued"]
                        else '<span class="muted">—</span>'))
        trs += (f'<tr><td class="seg">{rg}</td>'
                f'<td>{s["local_day"]} {s["local_time"]} <span class="muted">{s["tz"]}</span></td>'
                f'<td><span class="badge {cls}">{s["state"]}</span></td>'
                f'<td>{c["total"]}</td><td>{c["pipeline"]}</td>'
                f'<td><b>{c["queued"]}</b></td><td>{c["contacted"]}</td><td>{action}</td>'
                f'<td class="muted" style="font-size:12px">--region {rg}</td></tr>')
    trs = trs or '<tr><td class="empty" colspan=9>No leads in the database yet.</td></tr>'

    # --- held sends: leads WF-4 parked because their local window is shut (Phase-2 gate in action) ---
    held = query("""
        SELECT c.legal_name, c.region, ol.scheduled_for, ol.error
        FROM outreach_log ol
        JOIN leads l     ON l.id = ol.lead_id
        JOIN companies c ON c.id = l.company_id
        WHERE ol.outcome = 'skipped'
          AND l.status = 'QUEUED_FOR_OUTREACH'
          AND ol.id IN (SELECT max(id) FROM outreach_log WHERE outcome='skipped' GROUP BY lead_id)
        ORDER BY ol.scheduled_for NULLS LAST""")
    hrows = ""
    for h in held:
        live = sw.can_send_now(h["region"])          # recompute the open time fresh for display
        reason = h["error"] or live["reason"] or "-"
        whenstr = ("next run · daily cap resets" if "cap" in reason.lower()
                   else live["next_open_local"])
        hrows += (f'<tr><td>{(h["legal_name"] or "")[:34]}</td>'
                  f'<td><span class="badge">{h["region"]}</span></td>'
                  f'<td class="muted">{reason}</td>'
                  f'<td>{whenstr}</td></tr>')
    hrows = hrows or '<tr><td class="empty" colspan=4>Nothing held — every queued lead is inside its window.</td></tr>'

    # --- upcoming public holidays (next 45 days) per region that has leads ---
    today = _date.today()
    hol_regions = [r for r in sw.REGION_TZ if r != "OTHER" and (r in counts or True)]
    holrows = ""
    for rg in sorted(hol_regions, key=lambda x: order.get(x, 99)):
        for d, name in holiday_calendar.upcoming_holidays(rg, today, 45):
            days = (d - today).days
            when = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")
            holrows += (f'<tr><td><span class="badge">{rg}</span></td>'
                        f'<td>{d:%a %d %b %Y}</td><td>{name}</td>'
                        f'<td class="muted">{when}</td></tr>')
    holrows = holrows or '<tr><td class="empty" colspan=4>No public holidays in the next 45 days.</td></tr>'

    acc = ("" if advisor and advisor[0]["accurate"] else
           '<div class="factbox" style="border-left-color:var(--bad)">Timezone package (tzdata) missing — '
           'times are DST-blind fixed offsets. Run <b>pip install tzdata</b> for accuracy.</div>')

    # result banner after a mass-send + the current send mode
    msg = request.args.get("msg")
    banner = (f'<div class="factbox" style="border-left-color:var(--ok)">{html.escape(msg)}</div>'
              if msg else "")
    if test_inbox:
        mode = f'<span class="badge go">TEST — buttons preview to {html.escape(test_inbox)}</span>'
    elif not outreach.DRY_RUN:
        mode = '<span class="badge bad">LIVE SENDING</span>'
    else:
        mode = '<span class="badge ok">DRY RUN (safe — nothing actually sends)</span>'

    # ---- SEND BOARD: one column per region, listing its QUEUED emails + a "Send all" button ----
    q_by_region = {}
    for r in query("""SELECT c.region, c.legal_name, l.email
                      FROM leads l JOIN companies c ON c.id=l.company_id
                      WHERE l.status='QUEUED_FOR_OUTREACH' ORDER BY c.legal_name"""):
        q_by_region.setdefault(r["region"], []).append(r)
    columns = ""
    for a in advisor:                          # best-to-send-first order
        rg = a["region"]
        qleads = q_by_region.get(rg, [])
        n = len(qleads)
        cls = _STATE_CLASS[a["state"]]
        ready = a["state"] in ("PRIME", "GOOD")
        rows_html = "".join(
            f'<div style="padding:8px 2px;border-top:1px solid var(--line)">'
            f'<div style="font-weight:600;font-size:13px">{html.escape((x["legal_name"] or "?")[:32])}</div>'
            f'<div class="muted" style="font-size:12px">{html.escape(x["email"] or "—")}</div></div>'
            for x in qleads) or '<div class="empty" style="padding:16px 2px;font-size:13px">No emails queued</div>'
        btn = ""
        if n:
            confirm = f"Send all {n} queued {rg} email(s) now? Mode: {_mode_word}."
            btn = (f'<form method="post" action="/regions/send/{rg}" style="margin-top:10px" '
                   f'onsubmit="return confirm(\'{confirm}\')">'
                   f'<button class="{"go" if ready else "ghost"}" type="submit" '
                   f'style="width:100%;font-size:13px;padding:8px 10px">Send all {n}</button></form>')
        columns += (
            f'<div class="panel" style="flex:0 0 250px;min-width:250px;padding:16px">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;gap:6px">'
            f'<div style="font-weight:700;font-size:16px">{rg}</div>'
            f'<span class="badge {cls}">{a["state"]}</span></div>'
            f'<div class="muted" style="font-size:12px;margin-top:3px">{a["local_day"]} {a["local_time"]} '
            f'&nbsp;·&nbsp; {a["tz"]}</div>'
            f'<div class="muted" style="font-size:12px;margin-top:6px"><b>{n}</b> email(s) queued</div>'
            f'{btn}'
            f'<div style="margin-top:12px;max-height:320px;overflow:auto">{rows_html}</div></div>')
    board = (
        '<h2 style="margin-top:26px">Send board — mass-send by region</h2>'
        '<div class="muted" style="font-size:13px;margin:-4px 0 12px">One column per market with its queued '
        'emails. <b>Send all</b> dispatches that region\'s queued leads (in-window ones send, the rest are '
        'held; warmup cap always applies). Columns are ordered best-to-send-now first.</div>'
        f'<div style="display:flex;gap:14px;overflow-x:auto;padding-bottom:8px">{columns}</div>')

    body = f"""
    <h2>Send-window advisor &nbsp;·&nbsp; live &nbsp;·&nbsp; best to send now &rarr; later</h2>
    {banner}{acc}
    <div class="panel"><div style="display:flex;gap:10px;flex-wrap:wrap">{cards}</div>
      <div style="margin-top:12px" class="muted">Mode: {mode} &nbsp;·&nbsp; Business hours 09:00–17:00
        recipient-local · prime 09:00–11:00 &amp; 14:00–16:00 · GCC weekend Fri/Sat, others Sat/Sun.
        <span class="badge ok">gate ENFORCED</span> Each region's <b>Send</b> button runs WF-4 for just
        that market — it sends the in-window ones and holds the rest. To send for real, set
        <b>GRANJUR_DRY_RUN=0</b> and restart the dashboard.</div></div>

    {board}

    <div class="grid" style="margin-top:24px">
      <div class="panel"><h2 style="margin-top:0">Held sends — waiting for the local window</h2>
        <table><tr><th>Company</th><th>Region</th><th>Reason held</th><th>Sends when</th></tr>
        {hrows}</table>
        <div style="margin-top:10px" class="muted">These stay QUEUED and go out automatically on the next
          WF-4 run once their window opens. Force one now with
          <b>python wf4_python/wf4.py --ignore-window</b>.</div></div>
      <div class="panel"><h2 style="margin-top:0">Upcoming public holidays (next 45 days)</h2>
        <table><tr><th>Region</th><th>Date</th><th>Holiday</th><th>When</th></tr>
        {holrows}</table>
        <div style="margin-top:10px" class="muted">Cold email is skipped on these dates. Edit the free,
          offline list in <b>wf3_python/holiday_calendar.py</b>.</div></div>
    </div>

    <h2 style="margin-top:24px">Region overview — leads &amp; what's ready to send</h2>
    <div class="panel" style="overflow-x:auto"><table>
      <tr><th>Region</th><th>Local time</th><th>Window</th><th>Total</th><th>In&nbsp;pipeline</th>
          <th>Queued</th><th>Contacted</th><th>Action</th><th>Isolate</th></tr>
      {trs}</table>
      <div style="margin-top:12px" class="muted">Isolate a market end-to-end (discovery &rarr; enrich
        &rarr; qualify &rarr; outreach) with the shown flag, e.g.
        <b>python scripts/run_pipeline.py --collect --region GCC</b>. Every phase reads
        <b>GRANJUR_REGION</b> so the whole relay race stays in one market.</div></div>"""
    return page(body, "regions", refresh=True)


# ----------------------------------------------------------------------- health (Phase-3 domain health)
def _hbadge(status):
    txt = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "na": "N/A"}.get(status, "?")
    cls = {"pass": "ok", "fail": "bad"}.get(status, "")
    style = ' style="background:var(--warn-bg);color:var(--warn)"' if status == "warn" else ""
    return f'<span class="badge {cls}"{style}>{txt}</span>'


@app.route("/health")
def health():
    conn = db.get_connection()
    try:
        rep = domain_health.report(conn=conn)          # ALWAYS the one configured sender (no domain box)
        supp = query("SELECT COUNT(*) n FROM suppression_list", one=True)["n"]
        bounced = query("SELECT COUNT(*) n FROM leads WHERE bounced_at IS NOT NULL", one=True)["n"]
    finally:
        conn.close()

    w = rep["warmup"]
    verdict_cls = {"pass": "ok", "warn": "", "fail": "bad"}.get(rep["overall"], "")
    verdict_txt = {"pass": "CLEAR TO SEND", "warn": "SENDING OK — with warnings",
                   "fail": "BLOCKED"}[rep["overall"]]

    # auth / blacklist checks
    crows = ""
    for c in rep["checks"]:
        fix = f'<div class="muted" style="font-size:12px;margin-top:3px">fix: {c["fix"]}</div>' if c["fix"] else ""
        crows += (f'<tr><td>{_hbadge(c["status"])}</td><td class="seg">{c["name"]}</td>'
                  f'<td>{c["detail"]}{fix}</td></tr>')

    # warmup progress + ramp forecast
    pct = min(100, round(100 * w["sent_today"] / w["cap"])) if w["cap"] else 0
    barcol = "var(--ok)" if w["remaining"] > 0 else "var(--bad)"
    forecast = ""
    for d in range(w["age_days"], w["age_days"] + 7):
        cap_d = domain_health.daily_cap(d)
        mark = ' style="color:var(--ok);font-weight:700"' if d == w["age_days"] else ""
        forecast += f'<tr><td{mark}>day {d}{" (today)" if d == w["age_days"] else ""}</td><td{mark}>{cap_d}/day</td></tr>'

    blocking = ("" if not rep["blocking"] else
                '<div class="factbox" style="border-left-color:var(--bad)"><b>Blocking live send:</b> '
                + "; ".join(rep["blocking"]) + "</div>")

    # ---- active anti-spam safeguards protecting THIS mailbox (real numbers where relevant) ----
    def _row(status, name, detail):
        return (f'<tr><td><span class="badge ok">{status}</span></td>'
                f'<td class="seg">{name}</td><td class="muted">{detail}</td></tr>')
    checklist = "".join([
        _row("ON", "Warmup volume cap",
             f'max {w["cap"]}/day today (mailbox age {w["age_days"]}d) · {w["sent_today"]} sent · '
             f'{w["remaining"]} left — anything over the cap is HELD, so a fresh Gmail ramps slowly'),
        _row("ON", "Send-window + holiday gate",
             "never sends at 2 AM local, on a local weekend, or a public holiday (checked per recipient)"),
        _row("ON", "Bounce / dead-email suppression",
             f'{bounced} dead address(es) removed. Run <b>python wf4_python/bounce_parser.py</b> to scan '
             f'the inbox for new bounces and auto-suppress them'),
        _row("ON", "Global suppression list",
             f'{supp} address(es) permanently excluded (bounces, unsubscribes, complaints — never re-mailed)'),
        _row("ON", "Role-account skip",
             "info@ / sales@ style inboxes are never cold-emailed (they hurt sender reputation)"),
        _row("ON", "One-click unsubscribe + footer",
             "every email carries a List-Unsubscribe header, an opt-out link, and a real physical address"),
        _row("ON", "Pre-send re-validation",
             "the address format/MX is re-checked right before each send — dead addresses are dropped"),
        _row("GOOD", "Content discipline",
             "pitches are short, personal, plain-English, with at most a link or two and no spam-trigger words"),
    ])

    tips = (
        f'<div class="factbox"><b>Keeping {rep["email"]} out of spam.</b> It is a consumer Gmail, so its '
        "SPF/DKIM/DMARC are Google's (already valid) and its IPs are Google's shared pool — so the levers "
        "that actually protect you are: <b>(1) warmup</b> — stay under the daily cap above; "
        "<b>(2) engagement</b> — reply to every response (Gmail rewards real conversations); "
        "<b>(3) low volume, high relevance</b> — a fresh Gmail should send tens, not hundreds, a day; "
        "<b>(4) clean content</b> — short, personal, few links, no \"FREE / guarantee / act now\"; "
        "<b>(5) list hygiene</b> — the suppression + bounce handling above keep you off dead/complaining "
        "addresses. <b>For real volume, move to a custom sending domain you own</b> (a warmed mail. subdomain "
        "with its own SPF/DKIM/DMARC) — point <b>GMAIL_ADDRESS</b> at it and this page verifies the records.</div>")

    body = f"""
    <h2>Protecting {rep['email']} &nbsp;·&nbsp; anti-spam defense</h2>
    <div class="funnel" style="margin-bottom:14px">
      <div class="stat {verdict_cls}"><div class="n" style="font-size:20px">{verdict_txt}</div>
        <div class="l">overall</div></div>
      <div class="stat"><div class="n" style="font-size:20px">{rep['domain']}</div>
        <div class="l">sending domain</div></div>
      <div class="stat"><div class="n">{w['sent_today']}/{w['cap']}</div>
        <div class="l">sent today / cap</div></div>
      <div class="stat {'ok' if w['remaining'] else 'bad'}"><div class="n">{w['remaining']}</div>
        <div class="l">remaining today</div></div>
    </div>
    {blocking}

    <h2>Spam protection — active safeguards on this mailbox</h2>
    <div class="panel"><table>
      <tr><th>Status</th><th>Safeguard</th><th>What it does</th></tr>{checklist}</table></div>

    <div class="grid" style="margin-top:18px">
      <div class="panel"><h2 style="margin-top:0">Authentication &amp; blacklists (live DNS)</h2>
        <table><tr><th>Status</th><th>Check</th><th>Detail</th></tr>{crows}</table></div>
      <div class="panel"><h2 style="margin-top:0">Warmup — fresh-mailbox volume ramp</h2>
        <div class="muted" style="font-size:13px">Mailbox age <b>{w['age_days']} day(s)</b>
          (created {w['created']}). Today's safe cap: <b>{w['cap']}/day</b>.</div>
        <div class="bar" style="margin:10px 0"><i style="width:{pct}%;background:{barcol}"></i></div>
        <div class="muted" style="font-size:12px">{w['sent_today']} sent · {w['remaining']} remaining ·
          ceiling {w['ceiling']}/day</div>
        <h2 style="margin-top:16px">Next 7 days' caps</h2>
        <table><tr><th>Mailbox age</th><th>Daily cap</th></tr>{forecast}</table>
        <div class="muted" style="font-size:12px;margin-top:8px">WF-4 holds any send beyond the cap and
          resumes next day — the #1 way a new account avoids the spam folder.</div></div>
    </div>
    {tips}
    <p class="muted" style="margin-top:14px">100% free &amp; programmatic (dnspython + outreach_log +
      suppression list) — no paid reputation service. Mailbox date: <b>GRANJUR_MAILBOX_CREATED</b>;
      daily ceiling: <b>GRANJUR_WARMUP_CEIL</b>.</p>"""
    return page(body, "health")


# ----------------------------------------------------------------------- follow-ups (Phase-4 drip)
@app.route("/followups")
def followups():
    rows = _contacted_drip_rows()
    now = _dt.now(_tz.utc)
    replied = query("SELECT COUNT(*) n FROM leads WHERE status='REPLIED'", one=True)["n"]
    booked = query("SELECT COUNT(*) n FROM leads WHERE status='BOOKED'", one=True)["n"]

    steps_txt = " -> ".join(f"day {s['after_days']} ({s['kind']})" for s in followup_copy.FOLLOWUP_STEPS)
    due = upcoming = complete = 0
    cards = ""
    fup_data = {}                       # per-lead {kind: {"s": subject, "b": body}} for the live switcher
    _STEP_OF = {"nudge": 1, "issue": 2, "check": 3, "breakup": 4}
    for r in rows:
        plan = followup_copy.next_due_step(r["steps_sent"], r["initial_at"], now)
        days_ago = (now - r["last_contacted_at"]).days if r["last_contacted_at"] else 0
        rb = f'<span class="badge">{r["region"]}</span>'
        lid = str(r["id"])

        # generate all four steps up-front so the tabs can swap the box with NO page reload
        fup_data[lid] = {}
        for _s, _k in ((1, "nudge"), (2, "issue"), (3, "check"), (4, "breakup")):
            fs, fb, _fl = followup_copy.generate_followup(r, _k)
            fup_data[lid][_k] = {"s": fs, "b": fb}
        default_kind = plan["kind"] if (not plan["complete"] and plan.get("kind")) else "nudge"
        default = fup_data[lid][default_kind]

        if plan["complete"]:
            complete += 1
            status = '<span class="badge">sequence complete</span>'
            when = "All follow-ups sent — preview any step below."
        elif plan["is_due"]:
            due += 1
            status = f'<span class="badge ok">step {plan["step"]} DUE now</span>'
            when = ('Due now — the next <b>followup.py</b> run sends this (still subject to the '
                    'recipient-local hours + holiday + warmup gate).')
        else:
            upcoming += 1
            status = f'<span class="badge go">step {plan["step"]} in {plan["days_until"]}d</span>'
            dstr = f'{plan["due_at"]:%b %d}' if plan["due_at"] else "?"
            when = (f'Scheduled ~{dstr} (step {plan["step"]}, {plan["kind"]}, '
                    f'{plan["after_days"]}d after first contact).')

        # preview tabs (client-side switch); the due/default step is highlighted
        tabs = "".join(
            f'<button type="button" onclick="showFup(\'{lid}\',\'{_k}\',this)" class="fuptab" '
            f'style="font-size:12px;padding:5px 9px{";background:#1f3864;color:#fff" if _k == default_kind else ""}">'
            f'{_s} &middot; {_k}</button>' for _s, _k in ((1, "nudge"), (2, "issue"), (3, "check"), (4, "breakup")))
        fup_col = (
            '<div class="muted" style="font-size:12px;margin-bottom:6px">Prepared follow-up — click a step to preview</div>'
            f'<div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap">{tabs}</div>'
            f'<div class="muted" style="font-size:12px;margin-bottom:4px">Subject: <b id="fupsub-{lid}">{html.escape(default["s"])}</b></div>'
            f'<div class="pitch" id="fupbox-{lid}" style="white-space:pre-wrap;max-height:240px;overflow:auto">{html.escape(default["b"])}</div>'
            f'<form method="post" action="/followups/test/{lid}" style="margin-top:10px">'
            f'<input type="hidden" name="step" id="fupstep-{lid}" value="{_STEP_OF[default_kind]}">'
            '<button class="go" type="submit" style="font-size:12px;padding:7px 12px">'
            'Send this follow-up to my inbox</button></form>')

        orig_sub = html.escape(r["pitch_subject"] or "(no subject)")
        orig_body = html.escape(r["pitch_body"] or "(no pitch stored)")
        odir = ' dir="rtl" style="max-height:240px;overflow:auto;text-align:right"' \
            if (r.get("pitch_lang") == "ar") else ' style="max-height:240px;overflow:auto"'
        cards += f"""
        <div class="panel" style="margin-bottom:16px">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
            <div><b>{html.escape(r["legal_name"] or "?")}</b> &nbsp;{rb}
              <span class="muted">&nbsp;·&nbsp; contacted {r['last_contacted_at']:%b %d} ({days_ago}d ago)</span></div>
            <div>{status}</div>
          </div>
          <div class="muted" style="font-size:12px;margin:6px 0 12px">{when}</div>
          <div class="grid">
            <div><div class="muted" style="font-size:12px;margin-bottom:4px">Original pitch — the custom AI email
              &nbsp;<span class="badge">{r.get("pitch_lang") or "en"}</span></div>
              <div class="muted" style="font-size:12px;margin-bottom:4px">Subject: <b>{orig_sub}</b></div>
              <div class="pitch"{odir}>{orig_body}</div></div>
            <div>{fup_col}</div>
          </div>
        </div>"""
    cards = cards or '<div class="panel"><div class="empty">No CONTACTED leads yet — send some initial emails first (Outreach tab).</div></div>'

    # client-side step switcher: click a tab -> swap the box + subject + the send-form step, no reload
    fup_script = ("<script>window.FUP=" + json.dumps(fup_data).replace("</", "<\\/") + ";"
                  "var FUP_STEP={nudge:1,issue:2,check:3,breakup:4};"
                  "function showFup(id,kind,btn){var d=window.FUP[id][kind];"
                  "document.getElementById('fupbox-'+id).innerText=d.b;"
                  "document.getElementById('fupsub-'+id).innerText=d.s;"
                  "document.getElementById('fupstep-'+id).value=FUP_STEP[kind];"
                  "var t=btn.parentNode.querySelectorAll('button');"
                  "for(var i=0;i<t.length;i++){t[i].style.background='';t[i].style.color='';}"
                  "btn.style.background='#1f3864';btn.style.color='#fff';}</script>")

    _msg = request.args.get("msg")
    _banner = (f'<div class="factbox" style="border-left-color:var(--ok)">{html.escape(_msg)}</div>'
               if _msg else "")
    body = f"""
    <h2>Follow-up drip &nbsp;·&nbsp; nudges for contacted leads that never replied</h2>
    {_banner}
    <div class="funnel" style="margin-bottom:14px">
      <div class="stat"><div class="n">{len(rows)}</div><div class="l">contacted</div></div>
      <div class="stat ok"><div class="n">{due}</div><div class="l">follow-up due now</div></div>
      <div class="stat go"><div class="n">{upcoming}</div><div class="l">upcoming</div></div>
      <div class="stat"><div class="n">{complete}</div><div class="l">sequence done</div></div>
      <div class="stat ok"><div class="n">{replied}</div><div class="l">replied</div></div>
      <div class="stat go"><div class="n">{booked}</div><div class="l">booked</div></div>
    </div>
    <div class="factbox">Sequence: <b>{steps_txt}</b> (measured from first contact; edit via
      <b>GRANJUR_FOLLOWUP_DAYS</b>). Only leads still <b>CONTACTED</b> get nudged — a reply, booking, or
      unsubscribe removes them automatically. Send due nudges with
      <b>python wf4_python/followup.py</b> (dry-run) — every send obeys the Phase-2 local-hours/holiday
      gate and the Phase-3 warmup cap. Preview copy with <b>followup.py --preview</b>.</div>
    <h2 style="margin-top:20px">Contacted leads &amp; their next nudge</h2>
    {cards}
    {fup_script}"""
    return page(body, "followups")


@app.route("/replies")
def replies():
    """Unified inbox: prospect replies auto-detected over IMAP (reply_parser.py), classified real /
    out-of-office / unsubscribe, with an in-app reply box so you can respond without leaving the tool."""
    rows = query("""
        SELECT e.at, e.detail->>'kind' AS kind, e.detail->>'sentiment' AS sentiment,
               e.detail->>'subject' AS subject, e.detail->>'snippet' AS snippet,
               l.id AS lead_id, l.email, l.status::text AS status, c.legal_name, c.region
        FROM email_events e
        JOIN leads l ON l.id = e.lead_id
        JOIN companies c ON c.id = l.company_id
        WHERE e.event_type='reply'
        ORDER BY e.at DESC LIMIT 200""")
    kinds = {"real": 0, "auto": 0, "unsubscribe": 0}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    tiles = (f'<div class="stat ok"><div class="n">{kinds.get("real",0)}</div><div class="l">real replies</div></div>'
             f'<div class="stat"><div class="n">{kinds.get("auto",0)}</div><div class="l">out-of-office</div></div>'
             f'<div class="stat bad"><div class="n">{kinds.get("unsubscribe",0)}</div><div class="l">unsubscribe</div></div>')
    _kb = {"real": "ok", "auto": "", "unsubscribe": "bad"}
    _sl = {"positive": "positive", "negative": "negative", "neutral": "neutral"}
    cards = ""
    for r in rows:
        senti = f' &middot; {_sl.get(r["sentiment"], r["sentiment"] or "")}' if r["kind"] == "real" else ""
        subj = html.escape(r["subject"] or "Re:")
        replybox = (
            f'<form method="post" action="/replies/send/{r["lead_id"]}" style="margin-top:10px">'
            f'<textarea name="body" rows="3" placeholder="Type a reply to {html.escape(r["email"] or "")}…" '
            'style="width:100%;box-sizing:border-box;font:inherit;padding:8px;border:1px solid #d5dae2;border-radius:8px"></textarea>'
            '<button class="go" type="submit" style="margin-top:6px;font-size:12px;padding:7px 12px">Send reply</button></form>'
        ) if r["kind"] != "unsubscribe" else '<div class="muted" style="font-size:12px;margin-top:8px">Unsubscribed — suppressed, do not reply.</div>'
        cards += (
            '<div class="panel" style="margin-bottom:12px">'
            '<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px">'
            f'<div><b>{html.escape(r["legal_name"] or "?")}</b> <span class="badge">{r["region"] or "—"}</span> '
            f'<span class="badge {_kb.get(r["kind"],"")}">{r["kind"]}{senti}</span> '
            f'<span class="badge">{r["status"]}</span></div>'
            f'<div class="muted" style="font-size:12px">{r["at"]:%b %d %H:%M} &middot; {html.escape(r["email"] or "")}</div></div>'
            f'<div class="muted" style="font-size:12px;margin:6px 0 4px">Subject: <b>{subj}</b></div>'
            f'<div class="pitch" style="white-space:pre-wrap;max-height:160px;overflow:auto">{html.escape(r["snippet"] or "(no preview)")}</div>'
            f'{replybox}</div>')
    cards = cards or ('<div class="panel"><div class="empty">No replies detected yet. Run '
                      '<b>python wf4_python/reply_parser.py</b> (it also runs at the start of every pipeline run).</div></div>')
    _msg = request.args.get("msg")
    _banner = f'<div class="factbox" style="border-left-color:var(--ok)">{html.escape(_msg)}</div>' if _msg else ""
    body = f"""
    <h2>Replies &nbsp;·&nbsp; auto-detected from your inbox</h2>
    {_banner}
    <div class="funnel" style="margin-bottom:14px">{tiles}</div>
    <div class="factbox">Inbound mail is matched to CONTACTED leads over IMAP and classified locally: a
      <b>real</b> reply marks the lead REPLIED and stops its follow-up drip; <b>out-of-office</b> is ignored
      (the drip continues); an <b>unsubscribe</b> request suppresses the address. Scan with
      <b>python wf4_python/reply_parser.py</b> — it also runs at the start of every pipeline run.</div>
    {cards}"""
    return page(body, "replies")


@app.route("/replies/send/<lead_id>", methods=["POST"])
def replies_send(lead_id):
    """Send a manual reply to a prospect from within the dashboard (real send via Gmail)."""
    text = (request.form.get("body") or "").strip()
    lead = query("""SELECT l.email, l.pitch_subject, c.legal_name FROM leads l
                    JOIN companies c ON c.id=l.company_id WHERE l.id=%s""", (lead_id,), one=True)
    if not lead or not lead.get("email"):
        return redirect("/replies?msg=" + quote("Lead/email not found."))
    if not text:
        return redirect("/replies?msg=" + quote("Nothing to send — type a reply first."))
    try:
        # test-mode safety: if a test inbox is configured, the reply goes to YOU, not the real prospect
        test_inbox = os.getenv("GRANJUR_REGION_TEST_INBOX", "").strip()
        to = test_inbox or lead["email"]
        base_sub = "Re: " + (lead.get("pitch_subject") or "your enquiry")
        subject = (f"[TEST->{lead['email']}] " + base_sub) if test_inbox else base_sub
        unsubscribe = f"mailto:{outreach.GMAIL_ADDRESS}?subject=unsubscribe"
        inner = outreach._pitch_to_html(text) + outreach._html_signature(unsubscribe)
        html_body = outreach._finalize_html(inner, {"id": lead_id}, 0)
        send_gmail.send(to, subject, text, from_name=outreach.COMPANY_NAME,
                        unsubscribe=unsubscribe, html=html_body, logo_path=outreach.logo_path())
        line = (f"Reply PREVIEWED to your inbox (test mode) for {lead['legal_name']}." if test_inbox
                else f"Reply sent to {lead['legal_name']} <{lead['email']}>.")
    except Exception as e:  # noqa: BLE001
        line = f"reply failed: {e}"
    return redirect("/replies?msg=" + quote(line[:240]))


_FUP_KIND_BY_STEP = {1: "nudge", 2: "issue", 3: "check", 4: "breakup"}


@app.route("/followups/test/<lead_id>", methods=["POST"])
def followups_test(lead_id):
    """Email a preview of any follow-up step to your own inbox, rendered as a REPLY that quotes the
    original pitch below it (so you see the previous email too). Real send via Gmail; DB untouched."""
    inbox = (os.getenv("GRANJUR_REGION_TEST_INBOX") or os.getenv("GMAIL_ADDRESS") or "").strip()
    if not inbox:
        return redirect("/followups?msg=" + quote("Set GRANJUR_REGION_TEST_INBOX or GMAIL_ADDRESS first."))
    try:
        step = max(1, min(4, int(request.form.get("step", "1"))))
    except ValueError:
        step = 1
    kind = _FUP_KIND_BY_STEP[step]
    lead = query("""SELECT l.id, l.email, l.first_name, l.pitch_subject, l.pitch_body, l.pitch_lang,
                      l.last_contacted_at, c.legal_name, c.region
                    FROM leads l JOIN companies c ON c.id=l.company_id WHERE l.id=%s""",
                 (lead_id,), one=True)
    if not lead:
        return redirect("/followups?msg=" + quote("Lead not found."))
    try:
        subject, fbody, lang = followup_copy.generate_followup(lead, kind)
        unsubscribe = f"mailto:{outreach.GMAIL_ADDRESS}?subject=unsubscribe"
        when = f"{lead['last_contacted_at']:%b %d, %Y}" if lead.get("last_contacted_at") else "earlier"
        gmail = html.escape(os.getenv("GMAIL_ADDRESS", ""))
        quote_block = (
            '<div style="border-left:2px solid #ccc;margin-top:22px;padding-left:12px;color:#666;font-size:13px">'
            f'<div style="color:#999;font-size:12px;margin-bottom:6px">On {when}, Granjur Technologies '
            f'&lt;{gmail}&gt; wrote:</div>{outreach._pitch_to_html(lead.get("pitch_body") or "")}</div>')
        inner = (outreach._pitch_to_html(fbody) + outreach.calendar_cta_html(lead)
                 + quote_block + outreach._html_signature(unsubscribe))
        html_body = outreach._finalize_html(inner, lead, step)
        plain = f"{fbody}\n\n----- On {when}, Granjur Technologies wrote -----\n{lead.get('pitch_body') or ''}"
        th = outreach.thread_headers(lead_id, step)   # thread this preview under the earlier step(s)
        send_gmail.send(inbox, subject, plain, from_name=outreach.COMPANY_NAME,
                        unsubscribe=unsubscribe, html=html_body, logo_path=outreach.logo_path(),
                        message_id=th["message_id"], in_reply_to=th["in_reply_to"],
                        references=th["references"])
        line = f"Sent follow-up #{step} ({kind}) for {lead['legal_name']} to {inbox} (with the original quoted)."
    except Exception as e:  # noqa: BLE001
        line = f"preview send failed: {e}"
    return redirect("/followups?msg=" + quote(line[:240]))


# ----------------------------------------------------------------------- flow (n8n-style live view)
_FLOW_NODES = ["DISCOVERED", "ENRICHED", "QUALIFIED", "QUEUED_FOR_OUTREACH", "CONTACTED", "REPLIED", "BOOKED"]
_FLOW_COLS = ["DISCOVERED", "ENRICHED", "QUALIFIED", "CONTACTED", "BOOKED", "ERROR"]


@app.route("/flow")
def flow():
    cur = {r["status"]: r["n"] for r in query("SELECT status, COUNT(*) n FROM leads GROUP BY status")}

    # pipeline as connected nodes (current count at each stage)
    nodes = ""
    for i, s in enumerate(_FLOW_NODES):
        nodes += (f'<div class="stat {STATUS_KIND.get(s, "")}" style="min-width:118px">'
                  f'<div class="n">{cur.get(s, 0)}</div><div class="l">{s.replace("_FOR_OUTREACH", "")}</div></div>')
        if i < len(_FLOW_NODES) - 1:
            nodes += ('<div style="align-self:center;color:var(--accent);font-size:22px;'
                      'font-weight:700;padding:0 2px">&rarr;</div>')
    drops = " &nbsp; ".join(f"{badge(s)} {cur.get(s, 0)}"
                            for s in ("DISQUALIFIED", "ERROR", "SUPPRESSED", "COOLDOWN") if cur.get(s)) or "none"

    # source -> stage matrix (where leads come from, and where they are)
    rows = query("""SELECT c.source, l.status, COUNT(*) n
                    FROM leads l JOIN companies c ON l.company_id=c.id GROUP BY c.source, l.status""")
    matrix, totals = {}, {}
    for r in rows:
        lbl = source_label(r["source"])
        d = matrix.setdefault(lbl, {})
        d[r["status"]] = d.get(r["status"], 0) + r["n"]
        totals[lbl] = totals.get(lbl, 0) + r["n"]
    mhead = "".join(f"<th>{c[:5].title()}</th>" for c in _FLOW_COLS)
    mrows = "".join(
        f'<tr><td><b>{lbl}</b></td>' +
        "".join(f'<td>{matrix[lbl].get(c, 0) or "-"}</td>' for c in _FLOW_COLS) +
        f'<td class="muted">{totals[lbl]}</td></tr>'
        for lbl in sorted(matrix, key=lambda x: -totals[x])) or \
        '<tr><td class="empty" colspan=8>no leads yet</td></tr>'

    # live feed — each lead: where from + when discovered + current stage
    feed = query("""SELECT c.legal_name, c.source, c.first_seen_at, l.status, l.updated_at, l.id
                    FROM leads l JOIN companies c ON l.company_id=c.id
                    ORDER BY l.updated_at DESC LIMIT 30""")
    frows = "".join(
        f'<tr><td><a href="/lead/{r["id"]}">{r["legal_name"][:32]}</a></td>'
        f'<td class="muted">{source_label(r["source"])}</td>'
        f'<td class="muted">{r["first_seen_at"]:%m-%d %H:%M}</td>'
        f'<td>{badge(r["status"])}</td>'
        f'<td class="muted">{r["updated_at"]:%m-%d %H:%M}</td></tr>' for r in feed) or \
        '<tr><td class="empty" colspan=5>no leads yet</td></tr>'

    body = f"""
    <h2>Pipeline flow &nbsp;·&nbsp; live</h2>
    <div class="panel"><div style="display:flex;gap:8px;overflow-x:auto;padding-bottom:6px">{nodes}</div>
      <div style="margin-top:12px" class="muted">Drop-offs: {drops}</div></div>
    <h2 style="margin-top:24px">Where leads come from — and where they are</h2>
    <div class="panel" style="overflow-x:auto"><table>
      <tr><th>Source</th>{mhead}<th>Total</th></tr>{mrows}</table></div>
    <h2 style="margin-top:24px">Live feed</h2>
    <div class="panel"><table>
      <tr><th>Company</th><th>Source</th><th>Discovered</th><th>Stage</th><th>Updated</th></tr>{frows}</table></div>"""
    return page(body, "flow", refresh=True)


if __name__ == "__main__":
    print("Granjur Pipeline Dashboard -> open http://localhost:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
