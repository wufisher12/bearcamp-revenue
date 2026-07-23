"""Split unique matched listings into batch files for parallel KPI collection."""
import json
import os

with open("data/matched.json", encoding="utf-8") as f:
    matched = json.load(f)

seen = set()
targets = []
for m in matched:
    wh = m["wh"]
    if wh["id"] in seen:
        continue
    seen.add(wh["id"])
    targets.append({
        "listing_id": wh["id"],
        "channel": wh.get("channel") or "brightside",
        "wh_id": wh.get("wheelhouse_id"),
        "title": (wh.get("title") or "").strip(),
    })

os.makedirs("data/batches", exist_ok=True)
os.makedirs("data/kpis", exist_ok=True)
BATCHES = 14
chunks = [targets[i::BATCHES] for i in range(BATCHES)]
for i, chunk in enumerate(chunks, 1):
    with open(f"data/batches/batch_{i:02d}.json", "w", encoding="utf-8") as f:
        json.dump(chunk, f, indent=1)
print(f"{len(targets)} unique listings -> {BATCHES} batches "
      f"(sizes: {[len(c) for c in chunks]})")
non_brightside = [t for t in targets if t["channel"] != "brightside"]
print("non-brightside channels:", non_brightside or "none")
