"""Inventory collected KPIs, build retry batches for whatever is missing or failed."""
import glob
import json
import os

targets = []
for path in sorted(glob.glob("data/batches/batch_*.json")):
    with open(path, encoding="utf-8") as f:
        targets.extend(json.load(f))

collected, bad_files = {}, []
for path in sorted(glob.glob("data/kpis/*.json")):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
    except Exception as e:
        bad_files.append((path, str(e)))
        continue
    for lid, kpi in data.items():
        if isinstance(kpi, dict) and not kpi.get("_failed"):
            collected[lid] = True

remaining = [t for t in targets if t["listing_id"] not in collected]
print(f"targets: {len(targets)}  collected OK: {len(collected)}  remaining: {len(remaining)}")
for p, e in bad_files:
    print(f"UNPARSEABLE (its listings go to retry): {p}: {e}")

for f_ in glob.glob("data/batches/retry_*.json"):
    os.remove(f_)
if remaining:
    n = max(1, (len(remaining) + 19) // 20)  # ~20 per batch
    chunks = [remaining[i::n] for i in range(n)]
    for i, chunk in enumerate(chunks, 1):
        with open(f"data/batches/retry_{i:02d}.json", "w", encoding="utf-8") as f:
            json.dump(chunk, f, indent=1)
    print(f"wrote {n} retry batches (sizes: {[len(c) for c in chunks]})")
