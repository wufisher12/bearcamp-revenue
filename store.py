"""Append-only snapshot store.

PRODUCT-V2 requires nightly collection retained as history: week-over-week
pickup, pace trends and forward-YoY are all derived from consecutive nights,
and none of it can be backfilled. So a run is written once into a dated
directory and never mutated, and consumers resolve "the last N good nights"
through the manifest rather than by guessing dates - a machine that was off,
or an API outage, leaves a hole that date arithmetic would silently skip over.

Layout:
    data/snapshots/<YYYY-MM-DD>/
        meta.json                 run identity + per-endpoint counts + status
        listings.json.gz          trimmed active listings
        kpis.json.gz              {listing_id: KPI}
        price_calendar.jsonl.gz   one line per listing
        min_max_prices.jsonl.gz   one line per listing
        min_stay_calendar.jsonl.gz
        custom_rates.jsonl.gz
        failures.json             never silently dropped
    data/snapshots/index.json     append-only manifest, newest last
"""
import gzip
import io
import json
import os
from datetime import datetime, timezone

ROOT = os.path.join("data", "snapshots")
INDEX = os.path.join(ROOT, "index.json")

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


def _now():
    return datetime.now(timezone.utc).isoformat()


def read_index():
    if not os.path.exists(INDEX):
        return []
    with io.open(INDEX, encoding="utf-8") as f:
        return json.load(f)


def _write_index(entries):
    os.makedirs(ROOT, exist_ok=True)
    tmp = INDEX + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(entries, f, indent=1)
    os.replace(tmp, INDEX)


class Run:
    """One night's collection. Immutable once finalized."""

    def __init__(self, date=None):
        self.date = date or datetime.now().strftime("%Y-%m-%d")
        self.dir = os.path.join(ROOT, self.date)
        os.makedirs(self.dir, exist_ok=True)
        self.started_at = _now()
        self.counts = {}
        self.failures = []

    def path(self, name):
        return os.path.join(self.dir, name)

    def write_json(self, name, obj, compress=True):
        p = self.path(name + (".json.gz" if compress else ".json"))
        raw = json.dumps(obj, indent=1).encode("utf-8")
        if compress:
            with gzip.open(p, "wb") as f:
                f.write(raw)
        else:
            with open(p, "wb") as f:
                f.write(raw)
        return p

    def write_jsonl(self, name, records, compress=True):
        """records: iterable of dicts, one line each."""
        p = self.path(name + (".jsonl.gz" if compress else ".jsonl"))
        opener = gzip.open if compress else open
        n = 0
        with opener(p, "wt", encoding="utf-8", newline="\n") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
                n += 1
        self.counts[name] = n
        return p

    def read_json(self, name):
        for p in (self.path(name + ".json.gz"), self.path(name + ".json")):
            if os.path.exists(p):
                if p.endswith(".gz"):
                    with gzip.open(p, "rt", encoding="utf-8") as f:
                        return json.load(f)
                with io.open(p, encoding="utf-8") as f:
                    return json.load(f)
        raise FileNotFoundError(name)

    def fail(self, listing_id, endpoint, error):
        self.failures.append({"listing_id": listing_id,
                              "endpoint": endpoint, "error": str(error)})

    def finalize(self, status=None, extra=None):
        if status is None:
            status = STATUS_PARTIAL if self.failures else STATUS_OK
        meta = {
            "date": self.date,
            "started_at": self.started_at,
            "finished_at": _now(),
            "status": status,
            "counts": self.counts,
            "failure_count": len(self.failures),
            "basis": "nightly rent revenue only; all *_fees fields excluded",
        }
        if extra:
            meta.update(extra)
        self.write_json("meta", meta, compress=False)
        self.write_json("failures", self.failures, compress=False)

        entries = [e for e in read_index() if e.get("date") != self.date]
        entries.append({k: meta[k] for k in
                        ("date", "started_at", "finished_at", "status",
                         "failure_count")})
        entries.sort(key=lambda e: e["date"])
        _write_index(entries)
        return meta


def latest_good(n=1):
    """Most recent runs with status ok, newest first. Never date arithmetic."""
    good = [e for e in read_index() if e.get("status") == STATUS_OK]
    return [Run(e["date"]) for e in sorted(good, key=lambda e: e["date"],
                                           reverse=True)[:n]]


def coverage(days=7):
    """Which of the last `days` dates have a good snapshot - holes are normal
    and must be visible to the assembler, not inferred."""
    from datetime import date, timedelta
    idx = {e["date"]: e["status"] for e in read_index()}
    today = date.today()
    out = []
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        out.append({"date": d, "status": idx.get(d, "missing")})
    return out
