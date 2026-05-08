#!/usr/bin/env python3
"""
Solviva Energy — Sales Support Real-Time Dashboard
Run:   python sales_support_dashboard.py
Open:  http://localhost:5100

Auto-refreshes every 5 minutes.  Fetches live data from Odoo via XML-RPC.
"""
import os
import sys
import json
import hmac
import secrets
from datetime import datetime, date, timedelta
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from odoo_client import xmlrpc_execute
from flask import (
    Flask, jsonify, request as freq, render_template_string,
    session, redirect, url_for, abort,
)

# ── Odoo connection ──────────────────────────────────────────────────────────
URL = os.getenv("ODOO_URL", "https://solviva-energy.odoo.com")
DB  = os.getenv("ODOO_DB",  "solviva-energy")
USR = os.getenv("ODOO_USER", "")
PWD = os.getenv("ODOO_PASSWORD")
KEY = os.getenv("ODOO_API_KEY")

# ── Business constants (adjust as needed) ───────────────────────────────────
STAGE_SQL       = 69    # "03 SQL"
STAGE_CANVASS   = 72    # "02A Canvassing"
TEAM_SS         = 11    # Sales Support team ID
TAG_DUPLICATE   = 36
TAG_SIMILAR     = 116
CAPACITY_PER_SS = 60    # daily lead capacity per rep
SQL_TARGET_DAY  = 5     # daily SQL target per rep
AGING_THRESHOLD = 30    # days before a canvassing lead is "aging"

F_SS       = "x_studio_crm_sales_support"
F_EA1      = "x_studio_crm_salessupport_engagement_activity_1"
F_EA2      = "x_studio_crm_salessupport_engagement_activity_2"
F_EA3      = "x_studio_crm_support_engagement_activity_3"
F_ATT1     = "x_studio_crm_salessupport_1st_attempt"
F_ATT2     = "x_studio_crm_salessupport_2nd_attempt"
F_ATT3     = "x_studio_crm_salessupport_3rd_attempt"
F_HANDOVER = "x_studio_crm_salessupport_handover_date"
F_TEAM     = "x_studio_crm_team_name"

LEAD_FIELDS = [
    "id", "name", "stage_id", "active", "create_date", "write_date",
    "tag_ids", "phone", "mobile", "lost_reason_id",
    F_SS, F_EA1, F_EA2, F_EA3, F_ATT1, F_ATT2, F_ATT3, F_HANDOVER, F_TEAM,
]

TEAM_OPTIONS = [
    "Hokage Team", "Sales Titans", "A-Team",
    "Solar Dominators", "Team TPs", "PV Pros",
]

# ── Helpers ──────────────────────────────────────────────────────────────────
def odoo_read(model, domain, fields=None, limit=0, order=None):
    kwargs = {"limit": limit, "fields": fields or LEAD_FIELDS}
    if order:
        kwargs["order"] = order
    return xmlrpc_execute(URL, DB, USR, PWD, model, "search_read",
                          args=[domain], kwargs=kwargs, api_key=KEY)

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def dt_str(d, end=False):
    if isinstance(d, date) and not isinstance(d, datetime):
        d = str(d)
    if isinstance(d, str):
        return d + (" 23:59:59" if end else " 00:00:00")
    return d.strftime("%Y-%m-%d %H:%M:%S")

def get_range(period, cs=None, ce=None):
    today = date.today()
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=today.weekday()), today
    if period == "month":
        return today.replace(day=1), today
    if period == "year":
        return today.replace(month=1, day=1), today
    if period == "custom" and cs and ce:
        return date.fromisoformat(cs), date.fromisoformat(ce)
    return today, today

# ── SS members cache ─────────────────────────────────────────────────────────
_ss_cache = None

def get_ss_members():
    """Returns {crm.team.member.id: user_name} for the SS team."""
    global _ss_cache
    if _ss_cache is None:
        members = odoo_read("crm.team.member",
                            [["crm_team_id", "=", TEAM_SS]],
                            fields=["id", "user_id"])
        _ss_cache = {m["id"]: m["user_id"][1] for m in members if m.get("user_id")}
    return _ss_cache

# ── Core data fetch ──────────────────────────────────────────────────────────
def fetch(period, ss_filter, cs=None, ce=None, team_filter="all"):
    start, end = get_range(period, cs, ce)
    s_str, e_str = dt_str(start), dt_str(end, end=True)

    ss_members = get_ss_members()
    ss_ids = list(ss_members.keys())
    ss_dom = [F_SS, "in", ss_ids] if ss_ids else [F_SS, "!=", False]
    if ss_filter != "all":
        try:
            ss_dom = [F_SS, "=", int(ss_filter)]
        except ValueError:
            pass

    base_dom = [ss_dom]
    if team_filter != "all" and team_filter in TEAM_OPTIONS:
        base_dom.append([F_TEAM, "=", team_filter])

    # MQL assigned in period
    mql = odoo_read("crm.lead", base_dom + [
        ["create_date", ">=", s_str], ["create_date", "<=", e_str],
    ])

    # SQL conversions in period (by handover date)
    sql = odoo_read("crm.lead", base_dom + [
        [F_HANDOVER, ">=", s_str], [F_HANDOVER, "<=", e_str],
    ])

    # Contacts/activities in period (any attempt date in range)
    acts_raw = odoo_read("crm.lead", base_dom + [
        "|", "|",
        [F_ATT1, ">=", s_str],
        [F_ATT2, ">=", s_str],
        [F_ATT3, ">=", s_str],
    ])
    acts = [
        l for l in acts_raw
        if any(
            (t := parse_dt(l.get(f))) and start <= t.date() <= end
            for f in [F_ATT1, F_ATT2, F_ATT3]
        )
    ]

    # Lost-engaged in period
    lost_raw = odoo_read("crm.lead", base_dom + [
        ["active", "=", False],
        ["write_date", ">=", s_str], ["write_date", "<=", e_str],
    ])
    lost_engaged = [
        l for l in lost_raw
        if any(l.get(f) == "Engaged" for f in [F_EA1, F_EA2, F_EA3])
    ]

    # Active For-Callback leads (no date filter — current state)
    callbacks = odoo_read("crm.lead", base_dom + [
        ["active", "=", True],
        "|", "|",
        [F_EA1, "=", "For Callback"],
        [F_EA2, "=", "For Callback"],
        [F_EA3, "=", "For Callback"],
    ])

    # Canvassing leads (no date filter — current state)
    canvassing = odoo_read("crm.lead", base_dom + [
        ["stage_id", "=", STAGE_CANVASS], ["active", "=", True],
    ])
    cutoff = datetime.now() - timedelta(days=AGING_THRESHOLD)
    canvassing_aging = [
        l for l in canvassing
        if (t := parse_dt(l.get("write_date"))) and t < cutoff
    ]

    # Untouched backlog (all-time): active MQLs assigned to SS with NO attempt yet,
    # NOT yet promoted (not SQL, not Canvassing). Independent of period filter.
    backlog_raw = odoo_read("crm.lead", base_dom + [
        ["active", "=", True],
        [F_ATT1, "=", False], [F_ATT2, "=", False], [F_ATT3, "=", False],
        ["stage_id", "not in", [STAGE_SQL, STAGE_CANVASS]],
    ])
    backlog = backlog_raw

    return dict(
        mql=mql, sql=sql, acts=acts,
        lost_engaged=lost_engaged,
        callbacks=callbacks,
        canvassing=canvassing,
        canvassing_aging=canvassing_aging,
        backlog=backlog,
        ss_members=ss_members,
        start=str(start), end=str(end), period=period,
    )

# ── Metric computation ───────────────────────────────────────────────────────
def ss_name(lead, members):
    ss = lead.get(F_SS)
    if not ss:
        return "Unassigned"
    if isinstance(ss, list):
        return ss[1]
    return members.get(ss, str(ss))

def has_attempt_in(lead, start, end):
    for f in [F_ATT1, F_ATT2, F_ATT3]:
        t = parse_dt(lead.get(f))
        if t and start <= t.date() <= end:
            return True
    return False

def get_eas(lead):
    return [lead.get(F_EA1) or "", lead.get(F_EA2) or "", lead.get(F_EA3) or ""]

def compute(data, ss_filter):
    mql  = data["mql"]
    sql  = data["sql"]
    acts = data["acts"]
    sm   = data["ss_members"]
    start = date.fromisoformat(data["start"])
    end   = date.fromisoformat(data["end"])
    days  = max(1, (end - start).days + 1)

    # Deduplicate act leads by id
    acts_seen, acts_uniq = set(), []
    for l in acts:
        if l["id"] not in acts_seen:
            acts_seen.add(l["id"])
            acts_uniq.append(l)

    contacted    = acts_uniq
    engaged      = [l for l in contacted if "Engaged" in get_eas(l)]
    not_engaged  = [l for l in contacted if "No Answer" in get_eas(l) and "Engaged" not in get_eas(l)]
    for_callback = [l for l in contacted if "For Callback" in get_eas(l) and "Engaged" not in get_eas(l)]
    untouched    = [l for l in mql if not any(parse_dt(l.get(f)) for f in [F_ATT1, F_ATT2, F_ATT3])]
    duplicates   = [l for l in mql if set(l.get("tag_ids", [])) & {TAG_DUPLICATE, TAG_SIMILAR}]
    canv_from_mql = [l for l in mql if l.get("stage_id") and l["stage_id"][0] == STAGE_CANVASS]

    # Overnight leads (created between 8PM and 8AM next day) within the period
    overnight = [
        l for l in mql
        if (t := parse_dt(l.get("create_date"))) and (t.hour >= 20 or t.hour < 8)
    ]

    # Backlog: active untouched MQLs across all time (independent of period)
    backlog_n = len(data.get("backlog", []))

    mql_n  = len(mql)
    sql_n  = len(sql)
    cont_n = len(contacted)
    eng_n  = len(engaged)
    ne_n   = len(not_engaged)
    unt_n  = len(untouched)
    dup_n  = len(duplicates)
    canv_n = len(canv_from_mql)
    leng_n = len(data["lost_engaged"])
    cb_n   = len(for_callback)

    num_ss = (1 if ss_filter != "all" else max(1, len(sm)))
    capacity  = num_ss * CAPACITY_PER_SS * days
    sql_target = num_ss * SQL_TARGET_DAY * days

    cap_rate  = round(cont_n / capacity * 100, 1) if capacity else 0
    resp_rate = round(eng_n / cont_n * 100, 1) if cont_n else 0
    sql_eng   = round(sql_n / eng_n * 100, 1) if eng_n else 0
    sql_mql   = round(sql_n / mql_n * 100, 1) if mql_n else 0

    # SQL ranking per SS
    per_ss_sql = defaultdict(int)
    for l in sql:
        per_ss_sql[ss_name(l, sm)] += 1
    ranking = sorted(per_ss_sql.items(), key=lambda x: x[1], reverse=True)

    # Engagement breakdown per attempt (Eng 1/2/3)
    ea_labels  = ["Engaged", "For Callback", "No Answer"]
    ea_attempt = {lab: [0, 0, 0] for lab in ea_labels}
    for l in acts_uniq:
        for i, f in enumerate([F_EA1, F_EA2, F_EA3]):
            v = l.get(f)
            if v in ea_attempt:
                ea_attempt[v][i] += 1

    # Activities per SS
    per_ss = defaultdict(lambda: dict(mql=0, contacted=0, engaged=0, sql=0, canvassing=0))
    for l in mql:
        per_ss[ss_name(l, sm)]["mql"] += 1
    for l in contacted:
        per_ss[ss_name(l, sm)]["contacted"] += 1
    for l in engaged:
        per_ss[ss_name(l, sm)]["engaged"] += 1
    for l in sql:
        per_ss[ss_name(l, sm)]["sql"] += 1
    for l in canv_from_mql:
        per_ss[ss_name(l, sm)]["canvassing"] += 1
    ss_labels = sorted(per_ss.keys())

    # Lost-engaged by day
    lost_by_day = defaultdict(int)
    for l in data["lost_engaged"]:
        t = parse_dt(l.get("write_date"))
        if t:
            lost_by_day[str(t.date())] += 1

    # Loss reason ranking (engaged-lost only)
    reason_count = defaultdict(int)
    for l in data["lost_engaged"]:
        r = l.get("lost_reason_id")
        name = r[1] if isinstance(r, list) and len(r) > 1 else "Unspecified"
        reason_count[name] += 1
    loss_reasons = sorted(reason_count.items(), key=lambda x: x[1], reverse=True)[:10]

    # Callbacks table
    cb_rows = []
    for l in data["callbacks"]:
        eas = get_eas(l)
        att_dates = sorted(
            [s for s in [l.get(F_ATT1), l.get(F_ATT2), l.get(F_ATT3)] if s],
            reverse=True,
        )
        cb_rows.append(dict(
            id=l["id"],
            name=l["name"],
            ss=ss_name(l, sm),
            phone=l.get("phone") or l.get("mobile") or "—",
            ea1=eas[0] or "—", ea2=eas[1] or "—", ea3=eas[2] or "—",
            last_attempt=(att_dates[0] or "")[:16],
            stage=l.get("stage_id", [None, "—"])[1] if isinstance(l.get("stage_id"), list) else "—",
        ))
    cb_rows.sort(key=lambda x: x["last_attempt"], reverse=True)

    # Canvassing aging table
    aging_rows = []
    for l in data["canvassing_aging"]:
        wd = (l.get("write_date") or "")[:10]
        days_old = (date.today() - date.fromisoformat(wd)).days if wd else 0
        aging_rows.append(dict(
            id=l["id"], name=l["name"],
            ss=ss_name(l, sm),
            phone=l.get("phone") or l.get("mobile") or "—",
            last_activity=wd,
            days_since=days_old,
        ))
    aging_rows.sort(key=lambda x: x["days_since"], reverse=True)

    return dict(
        kpis=dict(
            mql=mql_n, duplicate=dup_n, contacted=cont_n,
            engaged=eng_n, not_engaged=ne_n, sql=sql_n,
            canvassing=canv_n, lost_engaged=leng_n,
            untouched=unt_n, for_callback=cb_n,
            overnight=len(overnight), backlog=backlog_n,
            capacity_rate=cap_rate, response_rate=resp_rate,
            sql_per_engaged=sql_eng, sql_per_mql=sql_mql,
            sql_target=sql_target,
        ),
        sql_ranking=[{"name": n, "count": c} for n, c in ranking],
        loss_reasons=[{"reason": n, "count": c} for n, c in loss_reasons],
        engagement_breakdown=dict(
            labels=["1st Attempt", "2nd Attempt", "3rd Attempt"],
            datasets=[
                dict(label=k, data=ea_attempt[k]) for k in ea_labels
            ],
        ),
        activities_per_ss=dict(
            labels=ss_labels,
            mql        =[per_ss[n]["mql"]        for n in ss_labels],
            contacted  =[per_ss[n]["contacted"]  for n in ss_labels],
            engaged    =[per_ss[n]["engaged"]     for n in ss_labels],
            sql        =[per_ss[n]["sql"]         for n in ss_labels],
            canvassing =[per_ss[n]["canvassing"]  for n in ss_labels],
        ),
        lost_by_day=dict(lost_by_day),
        callbacks=cb_rows[:60],
        canvassing_aging=aging_rows[:60],
        ss_members=[{"id": k, "name": v} for k, v in sm.items()],
        period=data["period"],
        start_date=data["start"],
        end_date=data["end"],
        refreshed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        odoo_url=URL,
    )

# ── Trend data ───────────────────────────────────────────────────────────────
def fetch_trend(mode, ss_filter, team_filter="all"):
    """Return week-by-week or month-by-month MQL/SQL/Engaged counts."""
    sm = get_ss_members()
    ss_ids = list(sm.keys())
    ss_dom = [F_SS, "in", ss_ids] if ss_ids else [F_SS, "!=", False]
    if ss_filter != "all":
        try:
            ss_dom = [F_SS, "=", int(ss_filter)]
        except ValueError:
            pass

    base_dom = [ss_dom]
    if team_filter != "all" and team_filter in TEAM_OPTIONS:
        base_dom.append([F_TEAM, "=", team_filter])

    today = date.today()
    if mode == "month":
        lookback = 365
        def bucket(d): return d.strftime("%b %Y")
        def key_list():
            buckets = []
            cur = today.replace(day=1)
            for _ in range(12):
                buckets.append(cur.strftime("%b %Y"))
                cur = (cur - timedelta(days=1)).replace(day=1)
            return list(reversed(buckets))
    else:
        lookback = 84
        def bucket(d):
            mon = d - timedelta(days=d.weekday())
            return mon.strftime("W%W %Y")
        def key_list():
            buckets = []
            cur = today - timedelta(days=today.weekday())
            for _ in range(12):
                buckets.append(cur.strftime("W%W %Y"))
                cur -= timedelta(weeks=1)
            return list(reversed(buckets))

    start = today - timedelta(days=lookback)
    s_str, e_str = dt_str(start), dt_str(today, end=True)

    mql = odoo_read("crm.lead", base_dom + [
        ["create_date", ">=", s_str], ["create_date", "<=", e_str],
    ], fields=["id", "create_date", F_EA1, F_EA2, F_EA3])

    sql = odoo_read("crm.lead", base_dom + [
        [F_HANDOVER, ">=", s_str], [F_HANDOVER, "<=", e_str],
    ], fields=["id", F_HANDOVER])

    mql_by  = defaultdict(int)
    eng_by  = defaultdict(int)
    sql_by  = defaultdict(int)

    for l in mql:
        t = parse_dt(l.get("create_date"))
        if t:
            b = bucket(t.date())
            mql_by[b] += 1
            if any(l.get(f) == "Engaged" for f in [F_EA1, F_EA2, F_EA3]):
                eng_by[b] += 1

    for l in sql:
        t = parse_dt(l.get(F_HANDOVER))
        if t:
            sql_by[bucket(t.date())] += 1

    keys = key_list()
    return dict(
        labels=keys,
        mql    =[mql_by.get(k, 0) for k in keys],
        engaged=[eng_by.get(k, 0) for k in keys],
        sql    =[sql_by.get(k, 0) for k in keys],
    )

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Server-side password gate ────────────────────────────────────────────────
# Set APP_PASSWORD in env (or .env). If unset, the dashboard is open (dev mode).
# FLASK_SECRET_KEY signs the session cookie; auto-generated if not provided
# (sessions invalidate on restart, which is fine).
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
app.config.update(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("RENDER", "") != "" or os.getenv("FORCE_HTTPS", "") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

PUBLIC_ENDPOINTS = {"login", "static"}

@app.before_request
def _require_login():
    if not APP_PASSWORD:
        return  # auth disabled (local dev)
    if freq.endpoint in PUBLIC_ENDPOINTS:
        return
    if session.get("authed"):
        return
    if freq.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect(url_for("login", next=freq.path))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if freq.method == "POST":
        pw = freq.form.get("password", "")
        if APP_PASSWORD and hmac.compare_digest(pw, APP_PASSWORD):
            session.clear()
            session["authed"] = True
            session.permanent = True
            nxt = freq.args.get("next") or freq.form.get("next") or "/"
            if not nxt.startswith("/"):
                nxt = "/"
            return redirect(nxt)
        error = "Incorrect password."
    nxt = freq.args.get("next", "/")
    return render_template_string(LOGIN_HTML, error=error, next=nxt), (401 if error else 200)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/data")
def api_data():
    try:
        period    = freq.args.get("period", "today")
        ss_filter = freq.args.get("ss", "all")
        team_filter = freq.args.get("team", "all")
        cs        = freq.args.get("start")
        ce        = freq.args.get("end")
        data      = fetch(period, ss_filter, cs, ce, team_filter)
        result    = compute(data, ss_filter)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/trend")
def api_trend():
    try:
        mode      = freq.args.get("mode", "week")
        ss_filter = freq.args.get("ss", "all")
        team_filter = freq.args.get("team", "all")
        return jsonify(fetch_trend(mode, ss_filter, team_filter))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/teams")
def api_teams():
    return jsonify({"teams": TEAM_OPTIONS})

# ── Login HTML ────────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign in — Sales Support Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; font-family: 'Segoe UI', system-ui, sans-serif;
    background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
    color: #e2e8f0; display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .card {
    background: #1e293b; border: 1px solid #334155; border-radius: 12px;
    padding: 36px 32px; width: 100%; max-width: 380px;
    box-shadow: 0 20px 50px rgba(0,0,0,0.4);
  }
  h1 { margin: 0 0 6px; font-size: 22px; color: #f8fafc; font-weight: 600; }
  .subtitle { margin: 0 0 24px; color: #94a3b8; font-size: 13px; }
  label { display: block; font-size: 12px; color: #94a3b8; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
  input[type=password] {
    width: 100%; padding: 12px 14px; background: #0f172a; color: #e2e8f0;
    border: 1px solid #334155; border-radius: 8px; font-size: 15px; outline: none;
    transition: border-color 0.15s;
  }
  input[type=password]:focus { border-color: #38bdf8; }
  button {
    margin-top: 18px; width: 100%; padding: 12px; border: 0; border-radius: 8px;
    background: #38bdf8; color: #0f172a; font-weight: 600; font-size: 15px;
    cursor: pointer; transition: background 0.15s;
  }
  button:hover { background: #7dd3fc; }
  .error {
    margin-top: 14px; padding: 10px 12px; background: rgba(248,113,113,0.1);
    border: 1px solid rgba(248,113,113,0.3); color: #fca5a5; border-radius: 8px;
    font-size: 13px;
  }
  .brand { text-align: center; margin-bottom: 22px; font-size: 11px; color: #64748b; letter-spacing: 1.5px; text-transform: uppercase; }
</style>
</head>
<body>
  <form class="card" method="POST" action="/login{% if next and next != '/' %}?next={{ next }}{% endif %}">
    <div class="brand">Solviva Energy</div>
    <h1>Sales Support Dashboard</h1>
    <p class="subtitle">Enter the team password to continue.</p>
    <label for="pw">Password</label>
    <input id="pw" type="password" name="password" autofocus required autocomplete="current-password">
    <input type="hidden" name="next" value="{{ next }}">
    <button type="submit">Sign in</button>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
  </form>
</body>
</html>"""

# ── HTML template ─────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sales Support Dashboard — Solviva Energy</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --accent:#38bdf8; --accent2:#818cf8; --success:#4ade80;
  --bg:#0f172a; --card:#1e293b; --card2:#243044; --border:#334155;
  --text:#e2e8f0; --muted:#94a3b8; --white:#f8fafc; --black:#0f172a;
  --red:#f87171; --yellow:#fbbf24; --orange:#fb923c;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;font-size:13px;min-height:100vh}
a{color:var(--accent)}
/* ── Top bar ── */
.topbar{background:#1e293b;padding:10px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;border-bottom:2px solid var(--accent)}
.topbar img{height:32px}
.topbar h1{font-size:17px;font-weight:700;color:var(--accent);flex:1}
.topbar .meta{font-size:11px;color:var(--muted);text-align:right}
/* ── Controls ── */
.controls{background:#172033;padding:10px 20px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;border-bottom:1px solid var(--border)}
.controls select,.controls input,.controls button{
  background:#1e293b;color:var(--text);border:1px solid var(--border);
  border-radius:4px;padding:5px 10px;font-size:12px;cursor:pointer}
.controls button{background:#1e3a5f;color:var(--accent);font-weight:600;border-color:var(--accent)}
.controls button:hover{background:var(--accent);color:var(--black)}
.badge-live{background:var(--success);color:#052e16;font-size:10px;font-weight:700;
  padding:2px 7px;border-radius:10px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
/* ── Layout ── */
.container{padding:16px 20px;max-width:1600px;margin:0 auto}
.section-title{font-size:13px;font-weight:700;color:var(--accent);margin:18px 0 8px;
  text-transform:uppercase;letter-spacing:.8px;border-left:3px solid var(--accent);padding-left:8px}
/* ── KPI cards ── */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:10px}
.kpi-card{background:var(--card);border:1px solid var(--border);border-radius:8px;
  padding:12px 14px;cursor:default;transition:border-color .2s}
.kpi-card:hover{border-color:var(--accent)}
.kpi-card .label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.kpi-card .value{font-size:28px;font-weight:800;color:var(--white);line-height:1}
.kpi-card .sub{font-size:10px;color:var(--muted);margin-top:3px}
.kpi-card.accent .value{color:var(--accent)}
.kpi-card.danger .value{color:var(--red)}
.kpi-card.warn   .value{color:var(--yellow)}
/* ── SQL Countdown ── */
.sql-countdown{background:var(--card);border:2px solid var(--accent);border-radius:12px;
  padding:18px 24px;display:flex;align-items:center;gap:24px;flex-wrap:wrap;margin-bottom:16px}
.sql-countdown .big{font-size:64px;font-weight:900;line-height:1}
.sql-countdown .big span{color:var(--accent)}
.sql-countdown .big .slash{color:var(--muted);font-size:40px}
.sql-countdown .big .target{color:var(--muted);font-size:40px}
.sql-countdown .info{flex:1;min-width:200px}
.sql-countdown .info h2{font-size:16px;font-weight:700;color:var(--accent);margin-bottom:8px}
.sql-countdown .info .hint{font-size:11px;color:var(--muted)}
.progress-bar-wrap{background:#0f172a;border-radius:6px;height:14px;overflow:hidden;margin-top:10px;border:1px solid var(--border)}
.progress-bar-fill{height:100%;border-radius:6px;transition:width .6s;background:var(--accent)}
.progress-bar-fill.warn{background:var(--yellow)}
.progress-bar-fill.danger{background:var(--red)}
/* ── Charts ── */
.charts-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:14px;margin-bottom:16px}
.chart-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px}
.chart-card h3{font-size:12px;font-weight:700;color:var(--accent);margin-bottom:12px;text-transform:uppercase;letter-spacing:.6px}
.chart-card canvas{max-height:280px}
/* ── Tables ── */
.tables-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:14px;margin-bottom:20px}
.tbl-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px}
.tbl-card h3{font-size:12px;font-weight:700;color:var(--accent);margin-bottom:10px;text-transform:uppercase;letter-spacing:.6px}
.tbl-card table{width:100%;border-collapse:collapse;font-size:11px}
.tbl-card th{background:#172033;color:var(--muted);padding:5px 7px;text-align:left;border-bottom:1px solid var(--border);font-weight:600}
.tbl-card td{padding:5px 7px;border-bottom:1px solid #243044;color:var(--text);vertical-space:nowrap}
.tbl-card tr:last-child td{border-bottom:none}
.tbl-card tr:hover td{background:#243044}
.pill{display:inline-block;border-radius:10px;padding:1px 7px;font-size:10px;font-weight:600}
.pill.eng{background:#052e16;color:var(--success)}
.pill.cb {background:#0c2340;color:var(--accent)}
.pill.na {background:#2d1515;color:var(--red)}
.pill-days{background:#2d2009;color:var(--yellow);border-radius:10px;padding:1px 7px;font-size:10px}
.pill-days.hot{background:#2d1009;color:var(--red)}
/* ── Spinner / error ── */
.loading{text-align:center;padding:60px;color:var(--muted);font-size:14px}
.error-msg{background:#2d1515;border:1px solid var(--red);border-radius:6px;
  padding:10px 14px;color:var(--red);margin:12px 0;font-size:12px}
/* ── Lead link ── */
.lead-link{color:var(--text);text-decoration:none}
.lead-link:hover{color:var(--accent);text-decoration:underline}
</style>
</head>
<body>

<div class="topbar">
  <h1>&#9728; Sales Support Dashboard</h1>
  <span class="badge-live">LIVE</span>
  <div class="meta" id="meta">Connecting to Odoo...</div>
</div>

<div class="controls">
  <label style="color:var(--muted);font-size:11px">Period:</label>
  <select id="ctl-period">
    <option value="today">Today</option>
    <option value="week">This Week</option>
    <option value="month">This Month</option>
    <option value="year">This Year</option>
    <option value="custom">Custom</option>
  </select>
  <span id="custom-range" style="display:none;gap:6px;align-items:center">
    <input type="date" id="ctl-start" style="width:130px">
    <span style="color:var(--muted)">to</span>
    <input type="date" id="ctl-end" style="width:130px">
  </span>
  <label style="color:var(--muted);font-size:11px">Sales Support:</label>
  <select id="ctl-ss">
    <option value="all">All</option>
  </select>
  <label style="color:var(--muted);font-size:11px">Team:</label>
  <select id="ctl-team">
    <option value="all">All Teams</option>
  </select>
  <button id="btn-refresh" onclick="loadData()">&#8635; Refresh</button>
  <label style="color:var(--muted);font-size:11px;margin-left:8px">Trend:</label>
  <select id="ctl-trend">
    <option value="week">Week-on-Week</option>
    <option value="month">Month-on-Month</option>
  </select>
  <button onclick="loadTrend()">Load Trend</button>
</div>

<div class="container">
  <div id="error-area"></div>

  <!-- SQL Countdown -->
  <div class="section-title">SQL Progress</div>
  <div class="sql-countdown" id="sql-countdown">
    <div class="big"><span id="sql-actual">—</span><span class="slash"> / </span><span class="target" id="sql-target">—</span></div>
    <div class="info">
      <h2>SQL vs Daily Target</h2>
      <div class="progress-bar-wrap"><div class="progress-bar-fill" id="sql-bar" style="width:0%"></div></div>
      <div class="hint" id="sql-hint" style="margin-top:6px"></div>
    </div>
  </div>

  <!-- KPI cards -->
  <div class="section-title">Key Metrics</div>
  <div class="kpi-grid" id="kpi-row1"></div>
  <div class="kpi-grid" id="kpi-row2"></div>

  <!-- Charts -->
  <div class="section-title">Performance Charts</div>
  <div class="charts-grid">
    <div class="chart-card">
      <h3>SQL Ranking — by Sales Support</h3>
      <canvas id="chart-ranking"></canvas>
    </div>
    <div class="chart-card">
      <h3>Engagement Performance — 1st / 2nd / 3rd Attempt</h3>
      <canvas id="chart-engagement"></canvas>
    </div>
    <div class="chart-card">
      <h3>Daily Activities per Sales Support</h3>
      <canvas id="chart-activities"></canvas>
    </div>
    <div class="chart-card">
      <h3>Engaged Lost — by Day</h3>
      <canvas id="chart-lost"></canvas>
    </div>
    <div class="chart-card">
      <h3>Top Loss Reasons (Engaged-Lost)</h3>
      <canvas id="chart-reasons"></canvas>
    </div>
  </div>

  <!-- Trend chart (lazy-loaded) -->
  <div class="section-title">Conversion Trend</div>
  <div class="chart-card" style="margin-bottom:16px">
    <h3 id="trend-title">Week-on-Week: MQL → Engaged → SQL</h3>
    <canvas id="chart-trend" style="max-height:300px"></canvas>
  </div>

  <!-- Tables -->
  <div class="section-title">Scheduled Callbacks</div>
  <div class="tables-grid">
    <div class="tbl-card" style="grid-column:1/-1">
      <h3>Active For-Callback Leads</h3>
      <div id="tbl-callbacks">
        <div class="loading">Loading...</div>
      </div>
    </div>
  </div>

  <div class="section-title">Canvassing Stage</div>
  <div class="tables-grid">
    <div class="tbl-card">
      <h3>Canvassing — Future Callbacks</h3>
      <div id="tbl-canv-cb">
        <div class="loading">Loading...</div>
      </div>
    </div>
    <div class="tbl-card">
      <h3>Aging Canvassing Leads (&gt;30 days inactive)</h3>
      <div id="tbl-canv-aging">
        <div class="loading">Loading...</div>
      </div>
    </div>
  </div>

</div><!-- /container -->

<script>
const ODOO_URL = "";  // auto-detected from server

// ── Chart instances ──────────────────────────────────────────────────────────
const C = {};
const LIME   = "#38bdf8";  // sky blue accent
const DG     = "#1e293b";  // card bg
const BLUE   = "#818cf8";  // indigo
const RED    = "#f87171";  // soft red
const YELLOW = "#fbbf24";  // amber
const ORANGE = "#fb923c";  // soft orange
const MUTED  = "#94a3b8";  // slate muted
const SUCCESS= "#4ade80";  // soft green

const CHART_DEFAULTS = {
  responsive: true, maintainAspectRatio: true,
  plugins: { legend: { labels: { color: "#e2e8f0", font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: MUTED, font: { size: 10 } }, grid: { color: "#273549" } },
    y: { ticks: { color: MUTED, font: { size: 10 } }, grid: { color: "#273549" } },
  }
};

function makeChart(id, type, data, extraOpts) {
  if (C[id]) { C[id].destroy(); }
  const ctx = document.getElementById(id).getContext("2d");
  C[id] = new Chart(ctx, { type, data, options: Object.assign({}, CHART_DEFAULTS, extraOpts || {}) });
}

// ── Controls ─────────────────────────────────────────────────────────────────
document.getElementById("ctl-period").addEventListener("change", function() {
  document.getElementById("custom-range").style.display = this.value === "custom" ? "flex" : "none";
});

function getParams() {
  const period = document.getElementById("ctl-period").value;
  const ss     = document.getElementById("ctl-ss").value;
  const team   = document.getElementById("ctl-team").value;
  const start  = document.getElementById("ctl-start").value;
  const end    = document.getElementById("ctl-end").value;
  let url = `/api/data?period=${period}&ss=${ss}&team=${encodeURIComponent(team)}`;
  if (period === "custom" && start && end) url += `&start=${start}&end=${end}`;
  return url;
}

async function loadTeams() {
  try {
    const resp = await fetch("/api/teams");
    const d    = await resp.json();
    const sel  = document.getElementById("ctl-team");
    (d.teams || []).forEach(t => {
      const opt = document.createElement("option");
      opt.value = t; opt.textContent = t;
      sel.appendChild(opt);
    });
  } catch (e) { /* non-blocking */ }
}

// ── Main load ─────────────────────────────────────────────────────────────────
async function loadData() {
  document.getElementById("btn-refresh").textContent = "⏳ Loading...";
  document.getElementById("error-area").innerHTML = "";
  try {
    const resp = await fetch(getParams());
    const d    = await resp.json();
    if (d.error) throw new Error(d.error);
    render(d);
    // Populate SS dropdown from members
    const sel = document.getElementById("ctl-ss");
    const cur = sel.value;
    sel.innerHTML = '<option value="all">All</option>';
    (d.ss_members || []).forEach(m => {
      const opt = document.createElement("option");
      opt.value = m.id; opt.textContent = m.name;
      if (String(m.id) === cur) opt.selected = true;
      sel.appendChild(opt);
    });
  } catch (e) {
    document.getElementById("error-area").innerHTML =
      `<div class="error-msg">&#9888; ${e.message}</div>`;
  } finally {
    document.getElementById("btn-refresh").textContent = "↻ Refresh";
  }
}

function render(d) {
  const k = d.kpis;
  // Meta
  document.getElementById("meta").innerHTML =
    `<span>Period: <strong>${d.start_date} → ${d.end_date}</strong></span> &nbsp;
     <span>Refreshed: <strong>${d.refreshed_at}</strong></span>`;

  // SQL Countdown
  const pct = k.sql_target > 0 ? Math.min(100, Math.round(k.sql / k.sql_target * 100)) : 0;
  document.getElementById("sql-actual").textContent = k.sql;
  document.getElementById("sql-target").textContent = k.sql_target;
  const bar = document.getElementById("sql-bar");
  bar.style.width = pct + "%";
  bar.className = "progress-bar-fill" + (pct >= 100 ? "" : pct >= 60 ? " warn" : " danger");
  document.getElementById("sql-hint").textContent =
    pct >= 100 ? `✅ Target reached! (${pct}%)` :
    pct >= 60  ? `🔶 On track — ${pct}% of target` :
                 `🔴 Below target — ${pct}% (${k.sql_target - k.sql} more needed)`;

  // KPI row 1
  renderKPIs("kpi-row1", [
    { label: "MQL Assigned",   value: k.mql,        cls: "" },
    { label: "Duplicate",      value: k.duplicate,  cls: "warn" },
    { label: "Contacted",      value: k.contacted,  cls: "" },
    { label: "Engaged",        value: k.engaged,    cls: "accent" },
    { label: "Not Engaged",    value: k.not_engaged,cls: "danger" },
    { label: "For Callback",   value: k.for_callback,cls: "warn" },
    { label: "Untouched",      value: k.untouched,  cls: "danger", sub: `(in period)` },
    { label: "Backlog",        value: k.backlog,    cls: "danger", sub: `untouched all-time` },
  ]);

  // KPI row 2
  renderKPIs("kpi-row2", [
    { label: "SQL",            value: k.sql,          cls: "accent",  sub: `target: ${k.sql_target}` },
    { label: "Canvassing",     value: k.canvassing,   cls: "" },
    { label: "Lost (Engaged)", value: k.lost_engaged, cls: "danger" },
    { label: "Overnight 8PM-8AM", value: k.overnight, cls: "warn",    sub: `of MQL` },
    { label: "% Capacity",     value: k.capacity_rate + "%", cls: pct_cls(k.capacity_rate) },
    { label: "% Response",     value: k.response_rate + "%", cls: pct_cls(k.response_rate) },
    { label: "% SQL / Engaged",value: k.sql_per_engaged + "%", cls: pct_cls(k.sql_per_engaged) },
    { label: "% SQL / MQL",    value: k.sql_per_mql + "%",    cls: pct_cls(k.sql_per_mql) },
  ]);

  // Chart: SQL Ranking
  const rk = d.sql_ranking || [];
  makeChart("chart-ranking", "bar", {
    labels: rk.map(r => r.name),
    datasets: [{ label: "SQL", data: rk.map(r => r.count),
      backgroundColor: LIME, borderRadius: 5 }],
  }, { indexAxis: "y", plugins: { legend: { display: false } },
       scales: { x: { ticks: { color: MUTED }, grid: { color: "#273549" } },
                 y: { ticks: { color: MUTED, font: { size: 10 } }, grid: { color: "#273549" } } } });

  // Chart: Engagement Breakdown
  const eb = d.engagement_breakdown;
  const ENG_COLORS = [SUCCESS, LIME, BLUE];
  makeChart("chart-engagement", "bar", {
    labels: eb.labels,
    datasets: eb.datasets.map((ds, i) => ({
      label: ds.label, data: ds.data,
      backgroundColor: ENG_COLORS[i], borderRadius: 4,
    })),
  }, { plugins: { legend: { labels: { color: "#e2e8f0" } } } });

  // Chart: Activities per SS
  const ap = d.activities_per_ss;
  makeChart("chart-activities", "bar", {
    labels: ap.labels,
    datasets: [
      { label: "MQL",        data: ap.mql,       backgroundColor: "#334155", borderRadius:3 },
      { label: "Contacted",  data: ap.contacted,  backgroundColor: BLUE,     borderRadius:3 },
      { label: "Engaged",    data: ap.engaged,    backgroundColor: SUCCESS,   borderRadius:3 },
      { label: "SQL",        data: ap.sql,        backgroundColor: LIME,      borderRadius:3 },
      { label: "Canvassing", data: ap.canvassing, backgroundColor: YELLOW,   borderRadius:3 },
    ],
  });

  // Chart: Engaged Lost by day
  const lbd = d.lost_by_day || {};
  const lKeys = Object.keys(lbd).sort();
  makeChart("chart-lost", "bar", {
    labels: lKeys.length ? lKeys : ["No data"],
    datasets: [{ label: "Lost (Engaged)", data: lKeys.map(k => lbd[k]),
      backgroundColor: RED, borderRadius: 4 }],
  }, { plugins: { legend: { display: false } } });

  // Chart: Top Loss Reasons (engaged-lost only)
  const lr = d.loss_reasons || [];
  makeChart("chart-reasons", "bar", {
    labels: lr.length ? lr.map(r => r.reason) : ["No data"],
    datasets: [{ label: "Count", data: lr.map(r => r.count),
      backgroundColor: ORANGE, borderRadius: 4 }],
  }, { indexAxis: "y", plugins: { legend: { display: false } },
       scales: { x: { ticks: { color: MUTED }, grid: { color: "#273549" } },
                 y: { ticks: { color: MUTED, font: { size: 10 } }, grid: { color: "#273549" } } } });

  // Tables
  renderCallbacksTable(d.callbacks || []);
  renderCanvCallbackTable(d.callbacks || [], d.canvassing_aging || []);
  renderAgingTable(d.canvassing_aging || []);
}

function pct_cls(v) {
  return v >= 60 ? "accent" : v >= 30 ? "warn" : "danger";
}

function renderKPIs(containerId, items) {
  document.getElementById(containerId).innerHTML = items.map(i =>
    `<div class="kpi-card ${i.cls || ''}">
       <div class="label">${i.label}</div>
       <div class="value">${i.value}</div>
       ${i.sub ? `<div class="sub">${i.sub}</div>` : ""}
     </div>`
  ).join("");
}

function pillClass(v) {
  if (v === "Engaged")      return "pill eng";
  if (v === "For Callback") return "pill cb";
  if (v === "No Answer")    return "pill na";
  return "";
}
function pill(v) {
  return v && v !== "—" ? `<span class="${pillClass(v)}">${v}</span>` : "<span style='color:var(--muted)'>—</span>";
}

function renderCallbacksTable(rows) {
  if (!rows.length) {
    document.getElementById("tbl-callbacks").innerHTML = "<div style='color:var(--muted);padding:12px'>No scheduled callbacks.</div>";
    return;
  }
  document.getElementById("tbl-callbacks").innerHTML = `
    <table>
      <thead><tr>
        <th>Lead</th><th>Sales Support</th><th>Phone</th>
        <th>Eng 1</th><th>Eng 2</th><th>Eng 3</th><th>Last Attempt</th><th>Stage</th>
      </tr></thead>
      <tbody>${rows.map(r =>
        `<tr>
          <td><a class="lead-link" href="javascript:void(0)" title="ID ${r.id}">${r.name}</a></td>
          <td>${r.ss}</td><td>${r.phone}</td>
          <td>${pill(r.ea1)}</td><td>${pill(r.ea2)}</td><td>${pill(r.ea3)}</td>
          <td style="color:var(--muted)">${r.last_attempt || "—"}</td>
          <td style="color:var(--muted);font-size:10px">${r.stage}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

function renderCanvCallbackTable(cbRows, _aging) {
  // Canvassing leads that also have For Callback status
  const rows = cbRows.filter(r => r.stage && r.stage.toLowerCase().includes("canvas"));
  const el = document.getElementById("tbl-canv-cb");
  if (!rows.length) {
    el.innerHTML = "<div style='color:var(--muted);padding:12px'>None found.</div>";
    return;
  }
  el.innerHTML = `<table>
    <thead><tr><th>Lead</th><th>Sales Support</th><th>Phone</th><th>Eng 1</th><th>Eng 2</th><th>Last Attempt</th></tr></thead>
    <tbody>${rows.map(r =>
      `<tr>
        <td>${r.name}</td><td>${r.ss}</td><td>${r.phone}</td>
        <td>${pill(r.ea1)}</td><td>${pill(r.ea2)}</td>
        <td style="color:var(--muted)">${r.last_attempt||"—"}</td>
      </tr>`).join("")}
    </tbody></table>`;
}

function renderAgingTable(rows) {
  const el = document.getElementById("tbl-canv-aging");
  if (!rows.length) {
    el.innerHTML = "<div style='color:var(--muted);padding:12px'>No aging canvassing leads.</div>";
    return;
  }
  el.innerHTML = `<table>
    <thead><tr><th>Lead</th><th>Sales Support</th><th>Phone</th><th>Last Activity</th><th>Days Idle</th></tr></thead>
    <tbody>${rows.map(r =>
      `<tr>
        <td>${r.name}</td><td>${r.ss}</td><td>${r.phone}</td>
        <td style="color:var(--muted)">${r.last_activity}</td>
        <td><span class="pill-days ${r.days_since > 60 ? 'hot' : ''}">${r.days_since}d</span></td>
      </tr>`).join("")}
    </tbody></table>`;
}

// ── Trend chart ───────────────────────────────────────────────────────────────
async function loadTrend() {
  const mode = document.getElementById("ctl-trend").value;
  const ss   = document.getElementById("ctl-ss").value;
  const team = document.getElementById("ctl-team").value;
  document.getElementById("trend-title").textContent =
    (mode === "month" ? "Month-on-Month" : "Week-on-Week") + ": MQL → Engaged → SQL";
  try {
    const resp = await fetch(`/api/trend?mode=${mode}&ss=${ss}&team=${encodeURIComponent(team)}`);
    const d    = await resp.json();
    if (d.error) throw new Error(d.error);
    makeChart("chart-trend", "line", {
      labels: d.labels,
      datasets: [
        { label: "MQL Assigned", data: d.mql,     borderColor: MUTED,   backgroundColor: "transparent", tension:.3, pointRadius:4 },
        { label: "Engaged",      data: d.engaged,  borderColor: SUCCESS, backgroundColor: "transparent", tension:.3, pointRadius:4 },
        { label: "SQL",          data: d.sql,      borderColor: LIME,    backgroundColor: "transparent", tension:.3, pointRadius:5, borderWidth:2 },
      ],
    }, { plugins: { legend: { labels: { color: "#e2e8f0" } } } });
  } catch (e) {
    document.getElementById("trend-title").textContent = "Error: " + e.message;
  }
}

// ── Auto-refresh every 5 minutes ──────────────────────────────────────────────
loadTeams();
loadData();
loadTrend();
setInterval(loadData, 5 * 60 * 1000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5100"))
    print("=" * 60)
    print("  Solviva Sales Support Dashboard")
    print(f"  Odoo: {URL}  DB: {DB}")
    print(f"  Open: http://localhost:{port}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
