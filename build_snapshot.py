"""Merge KPI batches, extract + score, write the dashboard payloads.

Outputs data/snapshot.json + data/admin.json, and mirrors both into
site/data/ so the site directory is self-contained and deployable.
"""
import glob
import json
import os
import shutil
from datetime import datetime, timezone

from collector import (extract, score, admin_checks, portfolio_pacing_norm,
                       PACING_FIELD, PACING_AT_MARKET)

DATA = "data"
SITE_DATA = "site/data"
LOW_PACING_COUNT = 25
HIGH_PACING_COUNT = 10

with open(f"{DATA}/matched.json", encoding="utf-8") as f:
    matched = json.load(f)
with open(f"{DATA}/admin_recon.json", encoding="utf-8") as f:
    admin_recon = json.load(f)

kpis = {}
for path in sorted(glob.glob(f"{DATA}/kpis/*.json")):
    with open(path, encoding="utf-8") as f:
        kpis.update(json.load(f))

# one row per unique WH listing; duplicate sheet rows are an admin issue, not two KPI rows
pairs, seen = [], set()
for m in matched:
    lid = m["wh"]["id"]
    if lid in seen:
        continue
    seen.add(lid)
    pairs.append(m)

rows, failed, missing = [], [], []
for pair in pairs:
    lid = pair["wh"]["id"]
    kpi = kpis.get(lid)
    if kpi is None:
        missing.append({"listing_id": lid, "name": (pair["wh"].get("title") or "").strip()})
        continue
    if kpi.get("_failed"):
        failed.append({"listing_id": lid, "name": (pair["wh"].get("title") or "").strip(),
                       "error": kpi.get("_error")})
        continue
    rows.append(extract(pair, kpi))

norm = portfolio_pacing_norm(rows)
for r in rows:
    score(r)
    r["admin_issues"] = admin_checks(r)

scored = [r for r in rows if not r["is_new"]]
new_listings = [r for r in rows if r["is_new"]]

low_pacing = sorted([r for r in scored if r["priority_score"] > 0],
                    key=lambda r: r["priority_score"], reverse=True)[:LOW_PACING_COUNT]
high_pacing = sorted([r for r in scored if r["upside_score"] > 0],
                     key=lambda r: r["upside_score"], reverse=True)[:HIGH_PACING_COUNT]

# portfolio rollup by bedroom count (forward-looking only)
by_br = {}
for r in scored:
    br = r.get("bedrooms")
    br = int(br) if isinstance(br, (int, float)) else 0
    by_br.setdefault(br, []).append(r)


def med(vals):
    vals = sorted(v for v in vals if v is not None)
    return vals[len(vals) // 2] if vals else None


portfolio = []
for br in sorted(by_br):
    g = by_br[br]
    portfolio.append({
        "bedrooms": br,
        "listings": len(g),
        "median_occ_0_30": med([r.get("occ_adj_0_30") for r in g]),
        "median_occ_0_60": med([r.get("occ_adj_0_60") for r in g]),
        "median_market_0_60": med([r.get(PACING_FIELD) for r in g]),
        "median_adr_0_30": med([r.get("adr_0_30") for r in g]),
        "median_revpar_0_30": med([r.get("revpar_0_30") for r in g]),
        "below_market": sum(1 for r in g if (r.get(PACING_FIELD) or 9) < PACING_AT_MARKET),
        "nights_available_0_30": sum(int(r.get("nights_available_0_30") or 0) for r in g),
    })

snapshot = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "pacing": {
        "field": PACING_FIELD,
        "at_market": PACING_AT_MARKET,
        "portfolio_median": norm,
        "below_market": sum(1 for r in scored if (r.get(PACING_FIELD) or 9) < PACING_AT_MARKET),
    },
    "counts": {
        "sheet_active": len(matched),
        "unique_wh_listings": len(pairs),
        "kpis_collected": len(rows),
        "kpi_failed": len(failed),
        "kpi_missing": len(missing),
        "scored": len(scored),
        "new_no_forward_data": len(new_listings),
        "upside_candidates": sum(1 for r in scored if r["upside_score"] > 0),
    },
    "low_pacing": low_pacing,
    "high_pacing": high_pacing,
    "portfolio_by_bedroom": portfolio,
    "listings": sorted(scored, key=lambda r: r["priority_score"], reverse=True) + new_listings,
}
admin_out = {
    "generated_at": snapshot["generated_at"],
    "reconciliation": admin_recon,
    "config_issues": [
        {"name": r["name"], "wh_id": r["wh_id"], "issues": r["admin_issues"]}
        for r in rows if r["admin_issues"]
    ],
    # No forward occupancy has two very different causes. A listing that has
    # never booked is genuinely new; one with booking history is a calendar
    # someone closed out. Only the second kind is worth chasing.
    "new_no_forward_data": [
        {"name": r["name"], "wh_id": r["wh_id"],
         "last_booked_at": r.get("last_booked_at"),
         "reason": ("Brand new - no bookings yet" if not r.get("last_booked_at")
                    else "Calendar appears fully blocked - last booked %s"
                         % str(r["last_booked_at"])[:10])}
        for r in new_listings
    ],
    "kpi_failures": failed + missing,
}

os.makedirs(SITE_DATA, exist_ok=True)
for name, payload in (("snapshot.json", snapshot), ("admin.json", admin_out)):
    with open(f"{DATA}/{name}", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    shutil.copyfile(f"{DATA}/{name}", f"{SITE_DATA}/{name}")

c = snapshot["counts"]
print(f"Run {snapshot['generated_at']}")
print(f"KPIs collected {c['kpis_collected']}/{c['unique_wh_listings']} unique listings "
      f"(failed: {c['kpi_failed']}, missing: {c['kpi_missing']}, "
      f"new/no-forward-data: {c['new_no_forward_data']})")
print(f"Portfolio median market ratio (0-60, context only): {norm:.2f}x")
print(f"Below market (<{PACING_AT_MARKET:.2f}x): {snapshot['pacing']['below_market']} of {len(scored)}")
if failed or missing:
    print("STALE/FAILED:", [x["name"] for x in failed + missing])

print(f"\nREVIEW PRICING (LOW PACING) - top {len(low_pacing)}")
for i, r in enumerate(low_pacing, 1):
    occ, ratio = r.get("occ_adj_0_30"), r.get(PACING_FIELD)
    print(f"{i:>2} {r['priority_score']:>4}  {('%.0f%%' % (occ * 100)):>4} occ  "
          f"{('%.2fx' % ratio) if ratio is not None else '   -':>6} mkt  "
          f"floor {str(r.get('min_price_hits_0_30') or 0):>2}/30  {r['name']}")

print(f"\nREVIEW PRICING (HIGH PACING) - top {len(high_pacing)} of "
      f"{c['upside_candidates']} candidates")
for i, r in enumerate(high_pacing, 1):
    occ, ratio = r.get("occ_adj_0_30"), r.get(PACING_FIELD)
    print(f"{i:>2} {r['upside_score']:>4}  {('%.0f%%' % (occ * 100)):>4} occ  "
          f"{'%.2fx' % ratio:>6} mkt  floor {str(r.get('min_price_hits_0_30') or 0):>2}/30  "
          f"min ${r.get('min_price')}  {r['name']}")

print("\nPORTFOLIO BY BEDROOM")
print(f"{'BR':>3} {'units':>5} {'occ30':>6} {'occ60':>6} {'mkt60':>6} "
      f"{'ADR':>7} {'RevPAR':>7} {'<mkt':>5} {'open':>5}")
for p in portfolio:
    print(f"{p['bedrooms']:>3} {p['listings']:>5} "
          f"{('%.0f%%' % (p['median_occ_0_30'] * 100)) if p['median_occ_0_30'] is not None else '-':>6} "
          f"{('%.0f%%' % (p['median_occ_0_60'] * 100)) if p['median_occ_0_60'] is not None else '-':>6} "
          f"{('%.2fx' % p['median_market_0_60']) if p['median_market_0_60'] is not None else '-':>6} "
          f"{('$%.0f' % p['median_adr_0_30']) if p['median_adr_0_30'] is not None else '-':>7} "
          f"{('$%.0f' % p['median_revpar_0_30']) if p['median_revpar_0_30'] is not None else '-':>7} "
          f"{p['below_market']:>5} {p['nights_available_0_30']:>5}")
