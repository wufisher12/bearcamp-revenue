"""Publish the Bear Camp client dashboard to the Fisher Family Hub.

Builds ONE self-describing document per client-dashboard-contract-v2 (in the
fisherfamilyhub repo, docs/client-dashboard-contract-v2.md) from the latest
good nightly snapshot and writes it to Firestore document
hub/mfg-client-bearcamp in project fisher-family-hub. The hub owns how it
looks; this script owns what it says. Whole-document write, idempotent for
the same snapshot date.

Rules carried in from the pipeline (RUNBOOK.md):
  * RENT BASIS ONLY - rent_revenue is Wheelhouse nightly_subtotal.
  * ARRIVAL-DATE ATTRIBUTION; canceled stays excluded; active listings only.
  * Full-year YoY is permitted on the Wheelhouse source (Mike, 2026-09-15).
  * OTB figures are always labeled with their as-of date (like-for-like rule).

Credentials: a Firebase service-account JSON whose path is in the
FIREBASE_SERVICE_ACCOUNT env var (default: data/firebase-sa.json). The file
lives under data/ (gitignored) and is never committed or logged. This script
writes ONLY hub/mfg-client-bearcamp, never any other document.

Usage:
    python publish_hub.py            # build + write to Firestore
    python publish_hub.py --dry-run  # build + save data/hub_dashboard.json only

Missing credentials exit 0 with a "skipped" line so the nightly chain does
not report failure before Mike has installed the key.
"""
import datetime as dt
import io
import json
import os
import sys
import time

import hub_sections
import market_tabs
import reservations as R
import store

CLIENT_ID = "bearcamp"
LABEL = "Bear Camp Cabin Rentals"
DOC_PATH = "hub/mfg-client-" + CLIENT_ID
NOTES_DOC = "mfg-client-%s-notes" % CLIENT_ID
SA_PATH = os.environ.get("FIREBASE_SERVICE_ACCOUNT",
                         os.path.join("data", "firebase-sa.json"))
OUT_JSON = os.path.join("data", "hub_dashboard.json")
MAX_DOC_BYTES = 500_000  # Firestore hard limit is 1 MB
SECTION_TYPES = {"tiles", "chart", "table", "note",
                 "listingTable", "kpiExplorer", "benchmark", "compset",
                 "compsetList"}


# ------------------------------------------------------------------ format






def dir_of(pct, flat_band=0.5):
    if pct is None:
        return "flat"
    if abs(pct) < flat_band:
        return "flat"
    return "up" if pct > 0 else "down"



# ------------------------------------------------------------- benchmarking
def _mt_block(tab, entity, role):
    for b in tab.get("blocks", []):
        if b["entity"] == entity and b.get("role") == role:
            return b
    return None


def _load_market(run):
    """Market blocks for a run: prefer the snapshot's saved copy, fall back
    to parsing the current sanitized workbook (pre-2026-09-17 snapshots)."""
    try:
        return run.read_json("market_tabs")
    except FileNotFoundError:
        path = os.path.join("data", "master_safe.xlsx")
        return market_tabs.parse_workbook(path) if os.path.exists(path) else None


MARKET_HISTORY_DIR = os.path.join("data", "market_history")


def _baseline_candidates():
    """Every saved market dataset, as (date, loader) pairs, newest first.
    Sources: nightly snapshots (market_tabs.json.gz) and one-off seeds in
    data/market_history/<date>.json (e.g. exported from the sheet's Drive
    revision history by seed_market_history.py)."""
    out = {}
    if os.path.isdir(MARKET_HISTORY_DIR):
        for fn in os.listdir(MARKET_HISTORY_DIR):
            if fn.endswith(".json"):
                p = os.path.join(MARKET_HISTORY_DIR, fn)
                out[fn[:-5]] = lambda p=p: json.load(io.open(p, encoding="utf-8"))
    for e in store.read_index():
        if e.get("status") == store.STATUS_OK:
            r = store.Run(e["date"])
            out[e["date"]] = lambda r=r: r.read_json("market_tabs")
    return sorted(out.items(), key=lambda kv: kv[0], reverse=True)


def _pickup_baseline(as_of, days=7):
    """The newest saved market dataset at least `days-1` days older than
    as_of; if none that old exists yet, the OLDEST available one (the pickup
    tile labels the true window, so a shorter one is honest, just younger).
    None only when there is no history at all."""
    cutoff = (as_of - dt.timedelta(days=days - 1)).isoformat()
    fallback = None
    for date, load in _baseline_candidates():
        if date >= as_of.isoformat():
            continue
        try:
            mt = load()
        except FileNotFoundError:
            continue
        if date <= cutoff:
            return date, mt
        fallback = (date, mt)  # newest-first iteration: last kept = oldest
    return fallback if fallback else (None, None)


def _agg(tab, entity, role):
    b = _mt_block(tab, entity, role)
    return b["period_aggregate"] if b else None


def build_benchmarking(run):
    """Portfolio vs market APO (Key Data), overall and per bedroom size.
    Returns a tab dict, or None when no market data is available."""
    mt = _load_market(run)
    if not mt or "market-data90" not in mt:
        return None
    main = mt["market-data90"]
    p_now, m_now = _agg(main, "PORTFOLIO", "today"), _agg(main, "MARKET", "today")
    p_ly, m_ly = _agg(main, "PORTFOLIO", "last_year"), _agg(main, "MARKET", "last_year")
    if p_now is None or m_now is None:
        return None
    hdr = _mt_block(main, "PORTFOLIO", "today")
    as_of = hdr["as_of"]

    base_date, base_mt = _pickup_baseline(dt.date.fromisoformat(run.date))
    # Label pickup by the baseline sheet's own as-of (from its block header),
    # which can trail the pull date by a day.
    if base_mt and "market-data90" in base_mt:
        b = _mt_block(base_mt["market-data90"], "PORTFOLIO", "today")
        if b and b.get("as_of"):
            base_date = b["as_of"]
    pickup_note = ""

    def sheet_pickup(tab):
        """Pickup from the tab's own seven_days_ago blocks (Mike, 2026-09-23:
        each tab carries today-minus-7 data). None when absent or the gap
        isn't plausibly a week (3-11 days)."""
        p7, m7 = _mt_block(tab, "PORTFOLIO", "seven_days_ago"), _mt_block(tab, "MARKET", "seven_days_ago")
        pt, mt_ = _mt_block(tab, "PORTFOLIO", "today"), _mt_block(tab, "MARKET", "today")
        if not (p7 and m7 and pt and mt_):
            return None
        vals = (pt["period_aggregate"], p7["period_aggregate"],
                mt_["period_aggregate"], m7["period_aggregate"])
        if None in vals:
            return None
        gap = market_tabs._days_apart(p7["as_of"], pt["as_of"])
        if not 3 <= gap <= 11:
            return None
        return vals[0] - vals[1], vals[2] - vals[3], p7["as_of"]

    def pickup(tab_name):
        """Portfolio and market APO change over ~a week, in pts. Prefers the
        sheet's own 7-days-ago blocks; falls back to our saved pulls."""
        cur = mt.get(tab_name)
        if cur:
            native = sheet_pickup(cur)
            if native:
                return native[0], native[1]
        if not base_mt or tab_name not in base_mt:
            return None, None
        old = base_mt[tab_name]
        pp, mm = _agg(cur, "PORTFOLIO", "today"), _agg(cur, "MARKET", "today")
        po, mo = _agg(old, "PORTFOLIO", "today"), _agg(old, "MARKET", "today")
        if None in (pp, mm, po, mo):
            return None, None
        return pp - po, mm - mo

    native_main = sheet_pickup(main)
    if native_main:
        pk_p, pk_m, base_date = native_main
    else:
        pk_p, pk_m = pickup("market-data90")

    tiles = [
        {"label": "Portfolio APO · Next 90",
         "value": "%.1f%%" % p_now,
         "delta": "%+.1f pts vs market" % (p_now - m_now),
         "dir": dir_of(p_now - m_now),
         "hint": "market %.1f%% (Pigeon Forge + 4 more)" % m_now},
    ]
    if p_ly is not None:
        tiles.append({
            "label": "Portfolio APO vs Last Year",
            "value": "%.1f%%" % p_now,
            "delta": "%+.1f pts vs LY" % (p_now - p_ly),
            "dir": dir_of(p_now - p_ly),
            "hint": "LY %.1f%% · market moved %+.1f pts"
                    % (p_ly, (m_now - m_ly) if m_ly is not None else 0)})
    if pk_p is not None:
        tiles.append({
            "label": "APO Pickup since %s" % base_date,
            "value": "%+.1f pts" % pk_p,
            "delta": "%+.1f pts vs market" % (pk_p - pk_m),
            "dir": dir_of(pk_p - pk_m),
            "hint": "market picked up %+.1f pts" % pk_m})
    else:
        pickup_note = (" Pickup vs market appears once a week of saved "
                       "nightly sheet pulls accumulates.")

    # Per-bedroom table from the <N>br-market-data90 tabs.
    rows = []
    for name in sorted((n for n in mt if n.endswith("br-market-data90")),
                       key=lambda n: int(n.split("br-")[0])):
        br = name.split("br-")[0]
        t = mt[name]
        p, m = _agg(t, "PORTFOLIO", "today"), _agg(t, "MARKET", "today")
        ply = _agg(t, "PORTFOLIO", "last_year")
        if p is None or m is None:
            continue
        bp, bm = pickup(name)
        rows.append({"cells": [
            "%s BR" % br,
            "%.1f%%" % p,
            "%.1f%%" % m,
            "%+.1f" % (p - m),
            "%+.1f" % (p - ply) if ply is not None else "-",
            "%+.1f" % (bp - bm) if bp is not None else "-",
        ]})

    sections = [{"type": "tiles", "title": "Bear Camp vs. Market",
                 "titleStyle": "heading", "items": tiles}]
    bench_chart = hub_sections.benchmark(mt, _mt_block)
    if bench_chart:
        sections.append(bench_chart)
    return {
        "id": "benchmarking",
        "label": "Benchmarking",
        "sections": sections + [
            {"type": "table",
             "title": "By bedroom size - next 90 days",
             "columns": ["Size", "Portfolio APO", "Market APO",
                         "vs Mkt (pts)", "vs LY (pts)", "Pickup vs Mkt"],
             "rows": rows},
            {"type": "note",
             "text": "Key Data Adjusted Paid Occupancy as of %s; market is "
                     "Direct (Pigeon Forge + 4 more). Sheet data is saved on "
                     "every nightly pull for reconciliation.%s"
                     % (as_of, pickup_note)},
        ],
    }


# ------------------------------------------------------------------ build
def build_doc(run):
    """Five tabs (Mike, 2026-09-23): Active Listings (interactive table with
    notes), Overview (filterable KPI explorer, Jan-Dec monthly), Pacing (same,
    next 120 days weekly), Benchmarking (tiles + six-series toggleable chart +
    per-BR table), Comp Sets (draft shell for Bear Necessities)."""
    as_of = dt.date.fromisoformat(run.date)
    rows = R.load(run)
    listings = run.read_json("listings")
    try:
        units = [u for u in run.read_json("units")
                 if str(u.get("Status", "")).strip().lower() == "active"]
    except FileNotFoundError:
        units = []
    try:
        sets_data = run.read_json("sets")
    except FileNotFoundError:
        sets_data = []

    tabs = [
        {"id": "listings", "label": "Active Listings",
         "sections": [hub_sections.listing_table(listings, units, NOTES_DOC)]},
        {"id": "overview", "label": "Overview",
         "sections": [hub_sections.kpi_explorer(rows, listings, as_of, "month")]},
        {"id": "pacing", "label": "Pacing",
         "sections": [hub_sections.kpi_explorer(rows, listings, as_of, "week")]},
    ]
    bench = build_benchmarking(run)
    if bench:
        tabs.append(bench)
    comp = hub_sections.compsets_list(sets_data, listings)
    tabs.append({"id": "compsets", "label": "Comp Sets",
                 "sections": [comp] if comp else []})

    return {"clientId": CLIENT_ID, "label": LABEL,
            "updated": int(time.time() * 1000), "asOf": run.date, "tabs": tabs}


def validate(doc):
    assert doc["clientId"] == CLIENT_ID and doc["label"]
    assert isinstance(doc["updated"], int) and doc["asOf"]
    assert 1 <= len(doc["tabs"]) <= 8
    for t in doc["tabs"]:
        assert t["id"] and t["label"] and len(t["sections"]) <= 6
        for s in t["sections"]:
            assert s["type"] in SECTION_TYPES, s["type"]
            if s["type"] == "chart":
                assert s["kind"] in ("line", "bar")
                assert s["format"] in ("currency", "percent", "number")
                assert all(len(x["values"]) == len(s["xLabels"]) for x in s["series"])
    raw = json.dumps(doc).encode("utf-8")
    assert len(raw) < MAX_DOC_BYTES, "document too large: %d bytes" % len(raw)
    return raw


# ------------------------------------------------------------------ firestore
def _fs_value(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, list):
        return {"arrayValue": {"values": [_fs_value(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: _fs_value(x) for k, x in v.items()}}}
    raise TypeError(type(v))


def write_firestore(doc):
    import requests
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    creds = service_account.Credentials.from_service_account_file(
        SA_PATH, scopes=["https://www.googleapis.com/auth/datastore"])
    creds.refresh(Request())
    project = creds.project_id
    url = ("https://firestore.googleapis.com/v1/projects/%s/databases/(default)/"
           "documents/%s" % (project, DOC_PATH))
    body = {"fields": {k: _fs_value(v) for k, v in doc.items()}}
    resp = requests.patch(url, json=body,
                          headers={"Authorization": "Bearer %s" % creds.token},
                          timeout=60)
    resp.raise_for_status()
    return project


def main(argv):
    dry = "--dry-run" in argv
    runs = store.latest_good(1)
    if not runs:
        print("publish_hub: no good snapshot - nothing to publish")
        return 1
    run = runs[0]
    doc = build_doc(run)
    raw = validate(doc)
    with io.open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1)
    if dry:
        print("publish_hub: dry run - %s built from snapshot %s (%d bytes), not written"
              % (OUT_JSON, run.date, len(raw)))
        return 0
    if not os.path.exists(SA_PATH):
        print("publish_hub: skipped - no service account at %s "
              "(set FIREBASE_SERVICE_ACCOUNT or install the key); "
              "%s saved locally" % (SA_PATH, OUT_JSON))
        return 0
    project = write_firestore(doc)
    print("publish_hub: wrote %s in %s from snapshot %s - %d bytes, %d tabs, asOf %s"
          % (DOC_PATH, project, run.date, len(raw), len(doc["tabs"]), doc["asOf"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
