"""Generate email drafts from the published snapshot.

    draft_emails.py client    -> drafts/YYYY-MM-DD-client-email.md
    draft_emails.py team      -> drafts/YYYY-MM-DD-team-admin-email.md

Drafts only. Mike reviews and sends by hand - that gate is permanent and is
never automated past, per the original design.
"""
import json
import os
import sys
from datetime import date

DASHBOARD_URL = "https://wufisher12.github.io/bearcamp-revenue/"

with open("site/data/snapshot.json", encoding="utf-8") as f:
    snap = json.load(f)
with open("site/data/admin.json", encoding="utf-8") as f:
    admin = json.load(f)

os.makedirs("drafts", exist_ok=True)
today = date.today().isoformat()


def pct(v):
    return "%.0f%%" % (v * 100) if v is not None else "-"


def client_email():
    c, p = snap["counts"], snap["pacing"]
    beating = c["scored"] - p["below_market"]
    lows = snap["low_pacing"][:5]
    ups = snap["high_pacing"][:3]

    lines = [
        f"Subject: Bear Camp pricing focus - week of {today}",
        "",
        "Hi [name],",
        "",
        "Quick summary of where the portfolio stands and what I'm working on "
        "this week.",
        "",
        f"**Portfolio health:** {beating} of {c['scored']} active listings are "
        f"pacing at or above their market ({p['portfolio_median']:.2f}x the "
        "neighborhood median). Full picture on the dashboard: "
        f"{DASHBOARD_URL}",
        "",
        "**Where my pricing attention is going this week:**",
        "",
    ]
    for r in lows:
        why = r["flag_labels"][0] if r["flag_labels"] else ""
        lines.append(f"- **{r['name']}** - {pct(r.get('occ_adj_0_30'))} booked "
                     f"next 30 days. {why}.")
    if ups:
        lines += ["", "**Rate increases I'm evaluating** (strong demand, "
                  "minimum price currently capping the rate):", ""]
        for r in ups:
            lines.append(f"- **{r['name']}** - {pct(r.get('occ_adj_0_30'))} booked, "
                         f"pacing {r['nbhd_ratio_0_60']:.2f}x its market.")
    lines += [
        "",
        "[Optional: add one closed-loop win here, e.g. \"we repriced X last "
        "week and it booked at $Y\", then delete this line.]",
        "",
        "Happy to walk through any of it.",
        "",
        "Mike",
    ]
    return "\n".join(lines), f"drafts/{today}-client-email.md"


def team_email():
    recon = admin.get("reconciliation", [])
    config = admin.get("config_issues", [])
    nfd = admin.get("new_no_forward_data", [])
    fails = admin.get("kpi_failures", [])
    blocked = [i for i in nfd if "blocked" in (i.get("reason") or "").lower()]

    lines = [
        f"Subject: Admin Review items to clear - {today}",
        "",
        "Team,",
        "",
        "Latest data run flagged the items below. Please work through them "
        "this week and fix whatever needs fixing in the sheet or Wheelhouse. "
        f"Live list is on the Admin Review tab: {DASHBOARD_URL}",
        "",
    ]
    if config:
        lines += ["**Pricing configuration gaps** (automation can misprice "
                  "these until set):", ""]
        for i in config:
            lines.append(f"- {i['name']} (WH {i['wh_id']}): {', '.join(i['issues'])}")
        lines.append("")
    if recon:
        lines += ["**Sheet vs Wheelhouse mismatches:**", ""]
        for i in recon:
            lines.append(f"- [{i['kind']}] {i['name']} (WH {i.get('wh_id')}): {i['detail']}")
        lines.append("")
    if blocked:
        lines += ["**Fully blocked calendars** - confirm each block is "
                  "intentional; these earn nothing while blocked:", ""]
        for i in blocked:
            lines.append(f"- {i['name']} (WH {i['wh_id']}): {i['reason']}")
        lines.append("")
    if fails:
        lines += ["**Data collection failures** (listing data is stale):", ""]
        for i in fails:
            lines.append(f"- {i['name']}: {i.get('error', 'no data returned')}")
        lines.append("")
    if not (config or recon or blocked or fails):
        lines += ["Nothing outstanding this week - Admin Review is clean. "
                  "No action needed.", ""]
    lines += ["Reply here when your items are done so I can re-run the check.",
              "", "Thanks,", "Mike"]
    return "\n".join(lines), f"drafts/{today}-team-admin-email.md"


which = sys.argv[1] if len(sys.argv) > 1 else ""
if which not in ("client", "team"):
    sys.exit("usage: draft_emails.py client|team")
body, path = client_email() if which == "client" else team_email()
with open(path, "w", encoding="utf-8") as f:
    f.write(body)
print(f"wrote {path} ({len(body.splitlines())} lines)")
