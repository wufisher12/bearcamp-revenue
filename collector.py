"""
Bear Camp Revenue Collector  v3
-------------------------------
Changes from v2 (Mike, 2026-07-22):
  * Market pacing now reads the 0_60 window. Everything else stays 30-day;
    the 30-day ratio was too noisy (one booking swings it).
  * Pacing is judged against the market absolutely, not against the
    portfolio median. Beating the market is a pass, full stop - only
    listings under 1.0x their neighborhood take a pacing penalty.
    (v2 flagged anything under ~1.53x because the median was 2.04x.)
  * Occupancy is now the dominant term. Priority order is "how empty is
    this calendar", with market position as a modifier.
  * All weights and thresholds are constants in the TUNING block below.
"""
import re
import json
from datetime import datetime, timezone

WINDOW_NIGHTS = 30

# ---------------------------------------------------------------- TUNING
# Everything Mike re-tunes lives here.

PACING_FIELD = "nbhd_ratio_0_60"   # market comparison window (KPIs stay 30-day)

# Pacing vs the Wheelhouse neighborhood. At or above AT_MARKET never flags.
PACING_AT_MARKET = 1.00
PACING_BEHIND = 0.85
PACING_WELL_BEHIND = 0.70
W_PACING_SLIGHT = 8
W_PACING_BEHIND = 16
W_PACING_WELL_BEHIND = 25

# Occupancy 0-30: the primary driver.
OCC_CRITICAL = 0.25
OCC_LOW = 0.40
OCC_SOFT = 0.55
W_OCC_CRITICAL = 50
W_OCC_LOW = 35
W_OCC_SOFT = 20
MIN_OPEN_NIGHTS = 8          # don't cry about a full calendar

# Booking velocity.
W_NO_PICKUP_14 = 22
W_NO_PICKUP_7 = 12
STALL_OCC_CEILING = 0.60
DROUGHT_DAYS = 21
W_DROUGHT = 15

# Min price binding, as a share of the 30-night window.
FLOOR_HEAVY = 0.60
FLOOR_SOME = 0.30
W_FLOOR_HEAVY = 15
W_FLOOR_SOME = 8

# Upside: demand is strong and the floor is capping it. Scored on its own
# track and never added to priority_score - "raise the min" and "this
# calendar is empty" are different workstreams and get different lists.
UPSIDE_OCC = 0.65
UPSIDE_OCC_TIERS = ((0.90, 30), (0.80, 22), (0.70, 14), (UPSIDE_OCC, 8))
UPSIDE_FLOOR_TIERS = ((0.60, 25), (0.30, 15))
UPSIDE_MARKET_TIERS = ((3.00, 20), (2.50, 15), (2.00, 10), (1.00, 5))

# Wheelhouse listings intentionally excluded from the managed portfolio.
INTENTIONAL_EXCLUSIONS = re.compile(r"^\s*2\d\d\s+by\s+the\s+night", re.I)


def norm_name(s):
    s = str(s or "").lower().strip()
    s = re.sub(r"-\s*new\s+listing\s*$", "", s)
    s = re.sub(r"\(.*?\)", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def clean_id(v):
    v = str(v or "").strip()
    return v[:-2] if v.endswith(".0") else v


# ---------------------------------------------------------------- reconcile

def reconcile(sheet_rows, wh_listings):
    active = [r for r in sheet_rows if str(r.get("Status", "")).strip().lower() == "active"]

    by_whid = {str(l.get("wheelhouse_id")): l for l in wh_listings}
    by_name = {}
    for l in wh_listings:
        by_name.setdefault(norm_name(l.get("title")), []).append(l)

    matched, admin = [], []
    used = set()

    for row in active:
        name = str(row.get("Listing Name", "")).strip()
        whid = clean_id(row.get("WH ID"))

        if whid and whid in by_whid:
            l = by_whid[whid]
            used.add(l["id"])
            matched.append({"sheet": row, "wh": l, "matched_by": "wh_id"})
            if norm_name(name) != norm_name(l.get("title")):
                admin.append({
                    "kind": "name_mismatch",
                    "name": name,
                    "detail": "Sheet name differs from Wheelhouse title '%s'" % (l.get("title") or "").strip(),
                    "wh_id": whid,
                })
            continue

        if whid:
            cands = by_name.get(norm_name(name), [])
            guess = ""
            if len(cands) == 1:
                guess = " Closest by name: %s" % cands[0].get("wheelhouse_id")
            admin.append({
                "kind": "bad_wh_id",
                "name": name,
                "detail": "WH ID %s not found in Wheelhouse.%s" % (whid, guess),
                "wh_id": whid,
            })
            if len(cands) == 1:
                used.add(cands[0]["id"])
                matched.append({"sheet": row, "wh": cands[0], "matched_by": "name_fallback"})
            continue

        admin.append({"kind": "missing_wh_id", "name": name,
                      "detail": "No WH ID on sheet; cannot join reliably", "wh_id": None})

    orphans = []
    for l in wh_listings:
        if l["id"] in used:
            continue
        if not l["listing_preferences"].get("automatic_rate_posting_enabled"):
            continue
        if INTENTIONAL_EXCLUSIONS.match(l.get("title") or ""):
            continue
        orphans.append({
            "kind": "orphan",
            "name": (l.get("title") or "").strip(),
            "detail": "Automation ON in Wheelhouse but not on the active sheet",
            "wh_id": l.get("wheelhouse_id"),
        })

    return {"matched": matched, "admin": admin + orphans}


# ---------------------------------------------------------------- extract

def g(d, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur or cur[p] is None:
            return default
        cur = cur[p]
    return cur


def days_since(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def extract(pair, kpi):
    sheet, wh = pair["sheet"], pair["wh"]
    p = wh.get("listing_preferences", {})
    return {
        "wh_listing_id": wh["id"],
        "wh_id": wh.get("wheelhouse_id"),
        "name": (wh.get("title") or "").strip(),
        "bedrooms": wh.get("num_bedrooms"),
        "city": str(sheet.get("City", "")).strip(),
        "matched_by": pair["matched_by"],

        "min_price": p.get("min_price"),
        "base_price": p.get("base_price"),
        "max_price": p.get("max_price"),
        "automation_on": p.get("automatic_rate_posting_enabled"),

        "occ_0_30": g(kpi, "occupancy", "0_30"),
        "occ_adj_0_30": g(kpi, "occupancy_adjusted", "0_30"),
        "occ_adj_0_60": g(kpi, "occupancy_adjusted", "0_60"),

        "pickup_7": g(kpi, "pickup", "7_0"),
        "pickup_14": g(kpi, "pickup", "14_0"),
        "pickup_30": g(kpi, "pickup", "30_0"),
        "last_booked_at": kpi.get("last_booked_at"),
        "days_since_booking": days_since(kpi.get("last_booked_at")),

        "asking_0_30": g(kpi, "asking_rate", "0_30"),
        "adr_0_30": g(kpi, "adr", "0_30"),
        "revpar_0_30": g(kpi, "revpar", "0_30"),
        "revenue_score_0_30": g(kpi, "revenue_score", "0_30"),

        "min_price_hits_0_30": g(kpi, "min_price_occurrence", "0_30"),
        "nights_available_0_30": g(kpi, "nights_available", "0_30"),
        "nbhd_ratio_0_30": g(kpi, "occupancy_neighborhood_adjusted_ratio", "0_30"),
        "nbhd_ratio_0_60": g(kpi, "occupancy_neighborhood_adjusted_ratio", "0_60"),
    }


# ---------------------------------------------------------------- admin split

def admin_checks(row):
    """Config problems -> Admin Review. Never affects priority score."""
    out = []
    if not row.get("automation_on"):
        out.append("Automation OFF")
    if row.get("base_price") is None:
        out.append("No base price set")
    if row.get("max_price") is None:
        out.append("No max price set")
    if row.get("min_price") is None:
        out.append("No min price set")
    if row.get("matched_by") == "name_fallback":
        out.append("Joined by name - WH ID did not resolve")
    return out


# ---------------------------------------------------------------- scoring

def score(row):
    """Revenue-urgency only. Config issues live in Admin Review."""
    flags = []

    def add(w, s):
        flags.append({"weight": w, "label": s})

    occ = row.get("occ_adj_0_30")
    ratio = row.get(PACING_FIELD)
    pu7, pu14 = row.get("pickup_7"), row.get("pickup_14")
    dsb = row.get("days_since_booking")
    hits = row.get("min_price_hits_0_30") or 0
    avail = row.get("nights_available_0_30") or 0

    if occ is None:
        row["flags"], row["priority_score"], row["flag_labels"] = [], 0, []
        row["upside_score"], row["upside_labels"] = 0, []
        row["is_new"] = True
        return row
    row["is_new"] = False

    if occ < OCC_CRITICAL and avail >= MIN_OPEN_NIGHTS:
        add(W_OCC_CRITICAL, "Critically empty 0-30 (%.0f%%) with %d nights open" % (occ * 100, int(avail)))
    elif occ < OCC_LOW and avail >= MIN_OPEN_NIGHTS:
        add(W_OCC_LOW, "Low occupancy 0-30 (%.0f%%) with %d nights open" % (occ * 100, int(avail)))
    elif occ < OCC_SOFT and avail >= MIN_OPEN_NIGHTS:
        add(W_OCC_SOFT, "Soft occupancy 0-30 (%.0f%%) with %d nights open" % (occ * 100, int(avail)))

    # Beating the market is a pass. Only under-market listings take a hit.
    if ratio is not None and ratio < PACING_AT_MARKET:
        if ratio < PACING_WELL_BEHIND:
            add(W_PACING_WELL_BEHIND, "Well below market 0-60 (%.2fx neighborhood)" % ratio)
        elif ratio < PACING_BEHIND:
            add(W_PACING_BEHIND, "Below market 0-60 (%.2fx neighborhood)" % ratio)
        else:
            add(W_PACING_SLIGHT, "Slightly below market 0-60 (%.2fx neighborhood)" % ratio)

    if pu7 == 0 and pu14 == 0 and occ < STALL_OCC_CEILING and avail >= MIN_OPEN_NIGHTS:
        add(W_NO_PICKUP_14, "No pickup in 14 days with soft calendar")
    elif pu7 == 0 and occ < OCC_SOFT and avail >= MIN_OPEN_NIGHTS:
        add(W_NO_PICKUP_7, "No pickup in 7 days with soft calendar")

    if dsb is not None and dsb >= DROUGHT_DAYS and occ < STALL_OCC_CEILING:
        add(W_DROUGHT, "No booking in %d days" % dsb)

    if hits:
        share = hits / WINDOW_NIGHTS
        if share >= FLOOR_HEAVY:
            add(W_FLOOR_HEAVY, "Min price binding %d/%d nights - floor is setting the rate" % (hits, WINDOW_NIGHTS))
        elif share >= FLOOR_SOME:
            add(W_FLOOR_SOME, "Min price binding %d/%d nights" % (hits, WINDOW_NIGHTS))

    row["flags"] = flags
    row["priority_score"] = sum(f["weight"] for f in flags)
    row["flag_labels"] = [f["label"] for f in flags]

    upside(row, occ, ratio, hits)
    return row


def _tier(value, tiers):
    for threshold, weight in tiers:
        if value >= threshold:
            return weight
    return 0


def upside(row, occ, ratio, hits):
    """Raise-the-min candidates. Separate track, separate list."""
    share = (hits or 0) / WINDOW_NIGHTS
    qualifies = (ratio is not None and ratio >= PACING_AT_MARKET
                 and occ >= UPSIDE_OCC and share >= 0.30)
    if not qualifies:
        row["upside_score"], row["upside_labels"] = 0, []
        return row

    row["upside_score"] = (_tier(occ, UPSIDE_OCC_TIERS)
                           + _tier(share, UPSIDE_FLOOR_TIERS)
                           + _tier(ratio, UPSIDE_MARKET_TIERS))
    row["upside_labels"] = [
        "Strong demand (%.0f%% occ, %.2fx market) with the floor binding "
        "%d/%d nights - raise min" % (occ * 100, ratio, hits, WINDOW_NIGHTS)
    ]
    return row


def portfolio_pacing_norm(rows):
    """Median market ratio across the book. Context only - it no longer
    sets any threshold (see PACING_AT_MARKET)."""
    vals = sorted(r[PACING_FIELD] for r in rows if r.get(PACING_FIELD) is not None)
    if not vals:
        return 1.0
    return vals[len(vals) // 2]
