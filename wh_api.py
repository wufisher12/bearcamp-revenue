"""Wheelhouse RM API client - replaces the MCP acquisition layer.

Writes the same files the MCP subagents used to produce, so everything
downstream (collector.py, build_snapshot.py, the site) is unchanged:

    data/wh_listings.json     array of trimmed listing objects
    data/kpis/api.json        {listing_id: trimmed KPI object}

Usage:
    python wh_api.py listings
    python wh_api.py kpis [--keep-stale]
    python wh_api.py all

The API key is read from the WHEELHOUSE_API_KEY environment variable, or
from a local key file (see load_key). The key is never logged, never
written to disk by this script, and never included in error messages.
"""
import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.usewheelhouse.com/ss_api/v1"
PER_PAGE = 100          # API max
REQUEST_PAUSE = 0.15    # docs: don't fire in parallel, space the calls
CHECKPOINT_EVERY = 10

# Exactly the KPI fields extract() in collector.py reads.
KPI_FIELDS = (
    "occupancy", "occupancy_adjusted", "pickup", "last_booked_days",
    "asking_rate", "adr", "revpar", "revenue_score",
    "min_price_occurrence", "nights_available",
    "occupancy_neighborhood_adjusted_ratio",
)
PREF_FIELDS = ("min_price", "base_price", "max_price",
               "automatic_rate_posting_enabled")

KEY_FILE_CANDIDATES = (
    "WHEELHOUSE_API_KEY.gitignore", ".env", ".env.local",
    "wheelhouse_api_key.txt",
)


def load_key():
    """Env var first, then a local (gitignored) key file.

    Accepts either a bare key on a line, or KEY=value form.
    """
    k = os.environ.get("WHEELHOUSE_API_KEY", "").strip()
    if k:
        return k

    named = os.environ.get("WHEELHOUSE_API_KEY_FILE")
    for path in ([named] if named else []) + list(KEY_FILE_CANDIDATES):
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    name, _, val = line.partition("=")
                    if name.strip().upper() == "WHEELHOUSE_API_KEY":
                        return val.strip().strip("\"'")
                else:
                    return line
    sys.exit(
        "No API key found. Set WHEELHOUSE_API_KEY in the environment, or put "
        "the key in one of: " + ", ".join(KEY_FILE_CANDIDATES)
    )


def unwrap(payload, *names):
    """Return the list inside a response, whether bare or enveloped."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for n in names + ("data", "results", "items"):
            v = payload.get(n)
            if isinstance(v, list):
                return v
    return []


def api_get(path, params, key, attempts=4):
    """GET with backoff. Never puts the key in an exception message."""
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, headers={
            "X-Integration-Api-Key": key,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 429:
                wait = e.headers.get("Retry-After")
                time.sleep(float(wait) if wait else 2 ** i)
                continue
            if e.code in (401, 403):
                sys.exit(f"{last} from {path} - the API key was rejected. "
                         "Check it is current and has RM API access.")
            if 500 <= e.code < 600 and i < attempts - 1:
                time.sleep(2 ** i)
                continue
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = type(e).__name__
            if i < attempts - 1:
                time.sleep(2 ** i)
                continue
    raise RuntimeError(f"{path} failed after {attempts} attempts ({last})")


def fetch_listings(key):
    """Page until a short page. Never assume a page count."""
    out, page = [], 1
    while True:
        payload = api_get("/listings", {
            "include_managed_listings": "true",
            "exclude_inactive": "true",
            "per_page": PER_PAGE,
            "page": page,
        }, key)
        batch = unwrap(payload, "listings")
        for l in batch:
            prefs = l.get("listing_preferences") or {}
            out.append({
                "id": l.get("id"),
                "wheelhouse_id": l.get("wheelhouse_id"),
                "title": l.get("title"),
                "num_bedrooms": l.get("num_bedrooms"),
                "currency": l.get("currency"),
                "channel": l.get("channel"),
                "listing_preferences": {k: prefs.get(k) for k in PREF_FIELDS},
            })
        print(f"  page {page}: {len(batch)} listings (total {len(out)})")
        if len(batch) < PER_PAGE:
            break
        page += 1
        time.sleep(REQUEST_PAUSE)

    os.makedirs("data", exist_ok=True)
    with open("data/wh_listings.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"wrote data/wh_listings.json ({len(out)} listings, {page} pages)")
    return out


def fetch_kpis(key, keep_stale=False):
    """One call per listing, sequential per the API's rate-limit guidance."""
    with open("data/matched.json", encoding="utf-8") as f:
        matched = json.load(f)

    targets, seen = [], set()
    for m in matched:
        wh = m["wh"]
        if wh["id"] in seen:
            continue
        seen.add(wh["id"])
        targets.append((wh["id"], wh.get("channel") or "brightside",
                        (wh.get("title") or "").strip()))

    os.makedirs("data/kpis", exist_ok=True)
    # Stale per-listing caches would silently survive the merge in
    # build_snapshot.py (it globs data/kpis/*.json), mixing old KPIs into a
    # fresh snapshot. Clear them unless explicitly told not to.
    if not keep_stale:
        for old in glob.glob("data/kpis/*.json"):
            os.remove(old)
        print(f"cleared {len(targets) and ''}stale KPI cache")

    out, failed = {}, []
    for i, (lid, channel, title) in enumerate(targets, 1):
        try:
            payload = api_get(f"/listings/{urllib.parse.quote(str(lid))}/kpis",
                              {"channel": channel}, key)
            if isinstance(payload, dict) and "kpis" in payload:
                payload = payload["kpis"]
            out[lid] = {k: payload.get(k) for k in KPI_FIELDS}
        except RuntimeError as e:
            out[lid] = {"_failed": True, "_error": str(e)}
            failed.append((lid, title, str(e)))

        if i % CHECKPOINT_EVERY == 0 or i == len(targets):
            with open("data/kpis/api.json", "w", encoding="utf-8") as f:
                json.dump(out, f, indent=1)
            print(f"  {i}/{len(targets)} collected", end="\r", flush=True)
        time.sleep(REQUEST_PAUSE)

    print()
    print(f"wrote data/kpis/api.json ({len(out) - len(failed)} ok, "
          f"{len(failed)} failed)")
    for lid, title, err in failed:
        print(f"  FAILED {lid} {title}: {err}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["listings", "kpis", "all"])
    ap.add_argument("--keep-stale", action="store_true",
                    help="don't clear existing data/kpis/*.json first")
    args = ap.parse_args()

    api_key = load_key()
    if args.mode in ("listings", "all"):
        fetch_listings(api_key)
    if args.mode in ("kpis", "all"):
        fetch_kpis(api_key, keep_stale=args.keep_stale)
