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

import market_tabs
import reservations as R
import store

CLIENT_ID = "bearcamp"
LABEL = "Bear Camp Cabin Rentals"
DOC_PATH = "hub/mfg-client-" + CLIENT_ID
SA_PATH = os.environ.get("FIREBASE_SERVICE_ACCOUNT",
                         os.path.join("data", "firebase-sa.json"))
OUT_JSON = os.path.join("data", "hub_dashboard.json")
MAX_DOC_BYTES = 200_000
SECTION_TYPES = {"tiles", "chart", "table", "note"}


# ------------------------------------------------------------------ format
def money(v):
    v = float(v)
    if abs(v) >= 1e6:
        return "$%.2fM" % (v / 1e6)
    if abs(v) >= 1e3:
        return "$%dK" % round(v / 1e3)
    return "$%d" % round(v)


def money_exact(v):
    return "$%s" % format(int(round(v)), ",")


def delta_pct(pct):
    return "%+.1f%%" % pct


def dir_of(pct, flat_band=0.5):
    if pct is None:
        return "flat"
    if abs(pct) < flat_band:
        return "flat"
    return "up" if pct > 0 else "down"


def ly_date(d):
    return dt.date(d.year - 1, d.month, min(d.day, 28))


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


def _pickup_baseline(as_of, days=7):
    """The newest good run at least `days-1` days older than as_of that has
    saved market data - the comparison point for pickup. None while the
    per-pull history is still building."""
    cutoff = (as_of - dt.timedelta(days=days - 1)).isoformat()
    for e in sorted(store.read_index(), key=lambda e: e["date"], reverse=True):
        if e.get("status") != store.STATUS_OK or e["date"] > cutoff:
            continue
        try:
            return e["date"], store.Run(e["date"]).read_json("market_tabs")
        except FileNotFoundError:
            continue
    return None, None


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
    pickup_note = ""

    def pickup(tab_name):
        """Portfolio and market APO change since the baseline pull, in pts."""
        if not base_mt or tab_name not in base_mt:
            return None, None
        cur, old = mt.get(tab_name), base_mt[tab_name]
        pp, mm = _agg(cur, "PORTFOLIO", "today"), _agg(cur, "MARKET", "today")
        po, mo = _agg(old, "PORTFOLIO", "today"), _agg(old, "MARKET", "today")
        if None in (pp, mm, po, mo):
            return None, None
        return pp - po, mm - mo

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

    def as_pct(v):
        # Point values arrive as 0-1 fractions; block aggregates as 0-100.
        return round(v * 100, 1) if v is not None else None

    p_pts = _mt_block(main, "PORTFOLIO", "today")["points"]
    m_blk = _mt_block(main, "MARKET", "today")
    m_by_date = {p["date"]: p["value"] for p in (m_blk["points"] if m_blk else [])}
    p_ly_blk = _mt_block(main, "PORTFOLIO", "last_year")
    ly_vals = [as_pct(p["value"]) for p in (p_ly_blk["points"] if p_ly_blk else [])]
    xl, sp, sm = [], [], []
    for pt in p_pts:
        d = dt.date.fromisoformat(pt["date"])
        xl.append(d.strftime("%b %d").replace(" 0", " "))
        sp.append(as_pct(pt["value"]))
        sm.append(as_pct(m_by_date.get(pt["date"])))
    series = [{"name": "Portfolio", "values": sp},
              {"name": "Market", "values": sm}]
    if len(ly_vals) == len(xl):
        series.append({"name": "Portfolio LY", "values": ly_vals})

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

    return {
        "id": "benchmarking",
        "label": "Benchmarking",
        "sections": [
            {"type": "tiles", "title": "Portfolio vs market", "items": tiles},
            {"type": "chart",
             "title": "Next 90 days - adjusted paid occupancy by week",
             "kind": "line", "xLabels": xl, "series": series,
             "format": "percent"},
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
    as_of = dt.date.fromisoformat(run.date)
    year = as_of.year
    rows = R.load(run)
    live = R.active(rows)
    listings = {l["listing_id"]: l for l in run.read_json("listings")}

    # --- Overview: YTD realized (check-in on or before as_of), YoY same window
    ytd_cur = R.aggregate([r for r in live
                           if dt.date(year, 1, 1) <= r["check_in"] <= as_of])["all"]
    ytd_ly = R.aggregate([r for r in live
                          if dt.date(year - 1, 1, 1) <= r["check_in"] <= ly_date(as_of)])["all"]
    rev_d = ((ytd_cur["rent_revenue"] - ytd_ly["rent_revenue"])
             / ytd_ly["rent_revenue"] * 100) if ytd_ly["rent_revenue"] else None
    adr_d = ((ytd_cur["adr"] - ytd_ly["adr"]) / ytd_ly["adr"] * 100
             ) if (ytd_cur["adr"] and ytd_ly["adr"]) else None
    nts_d = ((ytd_cur["nights"] - ytd_ly["nights"]) / ytd_ly["nights"] * 100
             ) if ytd_ly["nights"] else None

    # --- Pacing: next 30/60 OTB vs same point last year
    def pace(days):
        start, end = as_of + dt.timedelta(days=1), as_of + dt.timedelta(days=days)
        return R.pace_yoy(rows, as_of, start, end)["all"]

    p30, p60 = pace(30), pace(60)

    # --- Monthly rent revenue chart, current year vs last year
    def monthly(y):
        agg = R.aggregate([r for r in live if r["check_in"].year == y],
                          key=lambda r: r["check_in"].month)
        return [round(agg.get(m, {"rent_revenue": 0})["rent_revenue"]) for m in range(1, 13)]

    # --- Top units by current-year rent revenue
    by_unit = R.aggregate([r for r in live if r["check_in"].year == year],
                          key=lambda r: r["listing_id"])
    top = sorted(by_unit.items(), key=lambda kv: -kv[1]["rent_revenue"])[:5]
    # Firestore cannot store arrays nested directly in arrays, so each table
    # row is a map: {"cells": [...]} (contract v2, amended 2026-09-15).
    unit_rows = [{"cells": [
        (listings.get(lid, {}).get("title") or lid)[:40],
        money(b["rent_revenue"]),
        str(b["nights"]),
        "$%s" % format(round(b["adr"]), ",") if b["adr"] else "-",
    ]} for lid, b in top]

    # --- Next-60 weekly OTB nights, this year vs same point last year
    def weekly_nights(base_as_of, base_start):
        out = []
        for w in range(8):
            s = base_start + dt.timedelta(days=7 * w)
            e = s + dt.timedelta(days=6)
            out.append(R.otb(rows, base_as_of, s, e)["all"]["nights"])
        return out

    wk_start = as_of + dt.timedelta(days=1)
    wk_cur = weekly_nights(as_of, wk_start)
    wk_ly = weekly_nights(ly_date(as_of), ly_date(wk_start))
    wk_labels = [(wk_start + dt.timedelta(days=7 * w)).strftime("%b %d").replace(" 0", " ")
                 for w in range(8)]

    def pace_tile(label, p, days):
        c, pr = p["current"], p["prior"]
        d = p["rent_revenue_delta_pct"]
        return {
            "label": label,
            "value": money(c["rent_revenue"]) if c else "-",
            "delta": (delta_pct(d) + " vs LY") if d is not None else "LY not comparable",
            "dir": dir_of(d),
            "hint": "%s nights on the books" % format(c["nights"], ",") if c else "",
        }

    doc = {
        "clientId": CLIENT_ID,
        "label": LABEL,
        "updated": int(time.time() * 1000),
        "asOf": run.date,
        "tabs": [
            {
                "id": "overview",
                "label": "Overview",
                "sections": [
                    {"type": "tiles", "title": "Headline", "items": [
                        {"label": "%d Rent Revenue YTD" % year,
                         "value": money(ytd_cur["rent_revenue"]),
                         "delta": delta_pct(rev_d) + " vs LY" if rev_d is not None else "",
                         "dir": dir_of(rev_d),
                         "hint": "arrivals through %s" % run.date},
                        {"label": "Rent ADR YTD",
                         "value": "$%d" % round(ytd_cur["adr"] or 0),
                         "delta": delta_pct(adr_d) + " vs LY" if adr_d is not None else "",
                         "dir": dir_of(adr_d)},
                        {"label": "Nights YTD",
                         "value": format(ytd_cur["nights"], ","),
                         "delta": delta_pct(nts_d) + " vs LY" if nts_d is not None else "",
                         "dir": dir_of(nts_d)},
                        pace_tile("Next 60 Days OTB", p60, 60),
                    ]},
                    {"type": "chart",
                     "title": "Rent revenue by arrival month - %d vs %d (booked to date)" % (year, year - 1),
                     "kind": "bar",
                     "xLabels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
                     "series": [
                         {"name": str(year), "values": monthly(year)},
                         {"name": str(year - 1), "values": monthly(year - 1)},
                     ],
                     "format": "currency"},
                    {"type": "table",
                     "title": "Top units by %d rent revenue" % year,
                     "columns": ["Unit", "Rent Revenue", "Nights", "ADR"],
                     "rows": unit_rows},
                    {"type": "note",
                     "text": "Rent revenue only (fees excluded), arrival-date "
                             "attribution, canceled stays removed. Future months "
                             "show what is booked so far. Source: Wheelhouse "
                             "nightly snapshot %s, %d active units."
                             % (run.date, len(listings))},
                ],
            },
            {
                "id": "pacing",
                "label": "Pacing",
                "sections": [
                    {"type": "tiles", "title": "On the books", "items": [
                        pace_tile("Next 30 Days OTB", p30, 30),
                        pace_tile("Next 60 Days OTB", p60, 60),
                    ]},
                    {"type": "chart",
                     "title": "Next 60 days - nights on the books by week",
                     "kind": "line",
                     "xLabels": wk_labels,
                     "series": [
                         {"name": "This year", "values": wk_cur},
                         {"name": "Same point LY", "values": wk_ly},
                     ],
                     "format": "number"},
                    {"type": "note",
                     "text": "On the books as of %s, compared with the same "
                             "point last year (%s). Rent basis, arrival-date "
                             "attribution." % (as_of, ly_date(as_of))},
                ],
            },
        ],
    }
    bench = build_benchmarking(run)
    if bench:
        doc["tabs"].append(bench)
    return doc


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
