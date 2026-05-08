# Solviva Sales Support Dashboard

Real-time KPI dashboard for the Solviva Energy Sales Support team. Pulls live data
from Odoo via XML-RPC and renders KPIs, charts, and follow-up tables in the browser.

![Theme: Dark Navy](https://img.shields.io/badge/theme-dark%20navy-1e293b)
![Stack: Flask + Chart.js](https://img.shields.io/badge/stack-Flask%20%2B%20Chart.js-38bdf8)
![Auto-refresh: 5min](https://img.shields.io/badge/auto--refresh-5min-4ade80)

---

## Features

**KPIs** — MQL Assigned, Duplicate, Contacted, Engaged, Not Engaged, For Callback,
Untouched (period), Backlog (all-time), SQL, Canvassing, Lost (Engaged),
Overnight 8PM–8AM, % Capacity, % Response, % SQL/Engaged, % SQL/MQL.

**Charts**

- SQL Countdown vs daily target with progress bar
- SQL Ranking by Sales Support
- Engagement Performance (1st / 2nd / 3rd attempt)
- Daily Activities per Sales Support
- Engaged Lost — by Day
- Top Loss Reasons (engaged-lost)
- Conversion Trend (Week-on-Week / Month-on-Month)

**Tables**

- Active For-Callback Leads (per SS)
- Canvassing leads also flagged for callback
- Aging Canvassing Leads (>30 days idle)

**Filters** — Period (Today / Week / Month / Year / Custom), Sales Support member,
Sales Team (Hokage Team, Sales Titans, A-Team, Solar Dominators, Team TPs, PV Pros).

---

## Setup (local)

```powershell
# 1. Clone
git clone https://github.com/solvivaenergy/SalesSupportDashboard.git
cd SalesSupportDashboard

# 2. Create venv & install deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

# 3. Configure .env
Copy-Item .env.example .env
# Edit .env and fill in ODOO_USER and ODOO_API_KEY

# 4. Run
python sales_support_dashboard.py
# Open http://localhost:5100
```

### Getting an Odoo API key

1. Sign in at https://solviva-energy.odoo.com
2. Click your avatar → **My Profile** → **Account Security** tab
3. Click **New API Key**, enter a description ("Dashboard"), copy the key
4. Paste it into `.env` as `ODOO_API_KEY=...`

---

## Deployment (shared server)

The dashboard is a single-file Flask app. For production:

```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:5100 sales_support_dashboard:app
```

Put it behind a reverse proxy (nginx / Caddy / Cloudflare Tunnel) and restrict
access by IP, basic auth, or SSO. Set the `.env` on the server (do **not** commit it).

The app holds an in-memory cache of SS team members; restart on team changes.

---

## Deploy to Render (recommended — free tier works)

This repo includes [`render.yaml`](render.yaml) for one-click deployment.

1. Sign in at https://dashboard.render.com with the Solviva GitHub org account.
2. Click **New → Blueprint** and select `solvivaenergy/SalesSupportDashboard`.
3. Render reads `render.yaml` and provisions a web service. It will prompt for
   the two **secret** env vars (marked `sync: false`):
   - `ODOO_USER` — e.g. `alden.reyes@solvivaenergy.com`
   - `ODOO_API_KEY` — generated in Odoo (see "Getting an Odoo API key" above)
4. Click **Apply**. First build takes ~2 min. Subsequent deploys are automatic
   on every `git push` to `main`.
5. Render gives you a URL like `https://sales-support-dashboard.onrender.com`.

**Free tier notes**

- Service sleeps after 15 min of inactivity; first request after sleep takes
  ~30 sec to wake. For an always-on dashboard, upgrade to the **Starter** plan
  ($7/mo) or hit the URL with an uptime monitor.
- Render auto-provisions HTTPS.

**Restricting access** — Render free tier doesn't include built-in auth. Options:

- Put Cloudflare in front and use Cloudflare Access (free for ≤50 users).
- Add HTTP Basic Auth in Flask (10-line snippet, ask if you want it).
- Upgrade to Render's Pro plan and use IP allowlists.

---

## Configuration constants

Edit the top of `sales_support_dashboard.py` to tune:

| Constant                        | Default  | Meaning                                  |
| ------------------------------- | -------- | ---------------------------------------- |
| `STAGE_SQL`                     | 69       | Odoo stage ID for "03 SQL"               |
| `STAGE_CANVASS`                 | 72       | Stage ID for "02A Canvassing"            |
| `TEAM_SS`                       | 11       | `crm.team` ID for Sales Support          |
| `TAG_DUPLICATE` / `TAG_SIMILAR` | 36 / 116 | Tag IDs for duplicate detection          |
| `CAPACITY_PER_SS`               | 60       | Daily lead capacity per rep              |
| `SQL_TARGET_DAY`                | 5        | Daily SQL target per rep                 |
| `AGING_THRESHOLD`               | 30       | Days before a canvassing lead is "aging" |

---

## Required Odoo Studio fields on `crm.lead`

| Python constant | Odoo field                                        | Type                         |
| --------------- | ------------------------------------------------- | ---------------------------- |
| `F_SS`          | `x_studio_crm_sales_support`                      | many2one → `crm.team.member` |
| `F_EA1`         | `x_studio_crm_salessupport_engagement_activity_1` | selection                    |
| `F_EA2`         | `x_studio_crm_salessupport_engagement_activity_2` | selection                    |
| `F_EA3`         | `x_studio_crm_support_engagement_activity_3`      | selection                    |
| `F_ATT1..3`     | `x_studio_crm_salessupport_{1st,2nd,3rd}_attempt` | datetime                     |
| `F_HANDOVER`    | `x_studio_crm_salessupport_handover_date`         | datetime                     |
| `F_TEAM`        | `x_studio_crm_team_name`                          | selection                    |

Selection values for Engagement Activity 1/2/3: **Engaged**, **For Callback**, **No Answer**.

---

## License

Internal use — Solviva Energy.
