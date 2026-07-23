"""Run the sheet <-> Wheelhouse reconciliation from collector.py and print Admin Review."""
import json
from collector import reconcile

DATA = "data"

with open(f"{DATA}/sheet_rows.json", encoding="utf-8") as f:
    sheet_rows = json.load(f)
with open(f"{DATA}/wh_listings.json", encoding="utf-8") as f:
    wh_listings = json.load(f)

result = reconcile(sheet_rows, wh_listings)
matched, admin = result["matched"], result["admin"]

by_kind = {}
for item in admin:
    by_kind.setdefault(item["kind"], []).append(item)

print(f"Sheet active rows: {sum(1 for r in sheet_rows if str(r.get('Status','')).strip().lower()=='active')}")
print(f"Wheelhouse listings: {len(wh_listings)}")
print(f"Matched: {len(matched)} "
      f"(by wh_id: {sum(1 for m in matched if m['matched_by']=='wh_id')}, "
      f"by name fallback: {sum(1 for m in matched if m['matched_by']=='name_fallback')})")
print(f"Admin Review items: {len(admin)}\n")

for kind in ["name_mismatch", "bad_wh_id", "missing_wh_id", "orphan"]:
    items = by_kind.pop(kind, [])
    if not items:
        continue
    print(f"--- {kind} ({len(items)}) ---")
    for it in items:
        print(f"  [{it.get('wh_id')}] {it['name']}: {it['detail']}")
    print()
for kind, items in by_kind.items():
    print(f"--- {kind} ({len(items)}) ---")
    for it in items:
        print(f"  [{it.get('wh_id')}] {it['name']}: {it['detail']}")
    print()

# duplicate WH ID usage on the sheet (two active rows -> same WH listing)
seen = {}
for m in matched:
    lid = m["wh"]["id"]
    seen.setdefault(lid, []).append(str(m["sheet"].get("Listing Name", "")).strip())
dups = {k: v for k, v in seen.items() if len(v) > 1}
if dups:
    print(f"--- duplicate sheet rows joined to one WH listing ({len(dups)}) ---")
    for lid, names in dups.items():
        print(f"  {lid}: {names}")

with open(f"{DATA}/matched.json", "w", encoding="utf-8") as f:
    json.dump(matched, f)
with open(f"{DATA}/admin_recon.json", "w", encoding="utf-8") as f:
    json.dump(admin, f, indent=2)
print("\nWrote data/matched.json and data/admin_recon.json")
