"""Nightly Wheelhouse collection into the snapshot store.

Scope (Mike, 2026-09-03/15): ONLY listings on `units-defined` with Status ==
Active. Not the full ~406 Wheelhouse roster. Reservations follow the same rule:
only those attached to an Active listing are pulled.

Basis (Mike, 2026-09-03): NIGHTLY RENT REVENUE ONLY. Fee revenue is corrupt in
both the sheet export and Wheelhouse, so every fee-carrying field is refused at
the point of extraction - `*_fees` KPI fields by suffix, and the reservation
money fields other than `nightly_subtotal` by name. `nightly_subtotal` is,
despite its name, the STAY total and equals the sheet's `Rent Revenue` exactly
on ~86% of joined rows (verified 2026-09-15 on 748 reservations).

Reservations are pulled in full every night rather than incrementally, on
purpose: cancellations and partial refunds mutate old records, and a full pull
is the only way to be sure the store reflects them.

    python collect_nightly.py [--date YYYY-MM-DD] [--limit N]
                              [--only kpis,reservations,...]
"""
import argparse
import os
import sys
import time
import urllib.parse

import sheet_access
import store
import wh_api

RAW_XLSX = os.path.join("data", "master.xlsx")
SAFE_XLSX = os.path.join("data", "master_safe.xlsx")

FEE_SUFFIX = "_fees"

# Rent-basis KPI fields. Nothing here may end in _fees.
KPI_FIELDS = (
    "occupancy", "occupancy_adjusted",
    "occupancy_neighborhood_adjusted", "occupancy_neighborhood_adjusted_ratio",
    "pickup", "pickup_bookings", "bookings",
    "last_booked_days", "lead_time", "length_of_stay",
    "asking_rate", "adr", "revenue", "revpar", "revpar_adjusted_occupancy",
    "revenue_score", "revenue_blocked", "revenue_available",
    "min_price_occurrence",
    "nights_available", "nights_blocked", "nights_bookable",
    "nights_booked", "nights_calendar", "nights_percent_open",
    "comp_set_count", "comp_set_occupancy_adjusted",
    "model_date",
)
assert not [f for f in KPI_FIELDS if f.endswith(FEE_SUFFIX)], \
    "fee-basis field leaked into the rent-only KPI set"

# Reservation fields kept. `nightly_subtotal` is the stay-level rent.
# total_price / taxes / extras / extra_guest / security_deposit are dropped -
# they are fee-inclusive and the fee data is known-bad.
RES_FIELDS = (
    "id", "status", "start_date", "end_date",
    "booked_at", "created_at", "updated_at", "canceled_at",
    "nightly_subtotal", "num_guests", "source_name", "currency",
)
RES_DROPPED = ("total_price", "taxes", "extras", "extra_guest",
               "security_deposit", "comments", "confirmation_code")
assert not set(RES_FIELDS) & set(RES_DROPPED)

PREF_FIELDS = ("min_price", "base_price", "max_price",
               "automatic_rate_posting_enabled")

CALENDAR_ENDPOINTS = ("price_calendar", "min_max_prices",
                      "min_stay_calendar", "custom_rates")
ALL_STAGES = ("kpis",) + CALENDAR_ENDPOINTS + ("reservations", "sets")

# Comp-set member fields worth keeping; `amenities` in particular is a large
# blob with no dashboard use.
SET_MEMBER_DROP = ("amenities",)

RES_PAGE = 100
RES_MAX_PAGES = 30          # 3,000 reservations per listing - generous ceiling


def strip_fees(d):
    """Defense in depth: drop any *_fees key the API adds later."""
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if not k.endswith(FEE_SUFFIX)}


def prepare_sheet():
    """Fetch and sanitize the workbook before anything reads it.

    Every pull first tries a fresh headless download via the Drive API
    (sheet_fetch, using the hub service account); on any failure it runs
    against the last sanitized copy, so a Drive outage never kills the night.
    """
    import sheet_fetch
    sheet_fetch.fetch(RAW_XLSX)
    if os.path.exists(RAW_XLSX):
        rep = sheet_access.sanitize(RAW_XLSX, SAFE_XLSX, remove_src=True)
        dropped = ", ".join(rep["dropped"]) or "none"
        print("  sanitized workbook: kept %d tabs, dropped %d (%s)"
              % (len(rep["kept"]), len(rep["dropped"]), dropped))
    if not os.path.exists(SAFE_XLSX):
        sys.exit("No workbook at %s or %s. Download it first."
                 % (RAW_XLSX, SAFE_XLSX))
    return SAFE_XLSX


def active_targets(key):
    """Join units-defined Active rows to live Wheelhouse listings on WH ID."""
    actives, meta = sheet_access.read_units_defined(prepare_sheet())
    print("  units-defined: %s Active (sheet declares %s, match=%s)"
          % (meta["active_rows"], meta["declared_active_count"],
             meta["declared_matches_actual"]))

    listings = wh_api.fetch_listings(key)
    by_whid = {str(l.get("wheelhouse_id")): l for l in listings}

    targets, unmatched = [], []
    for row in actives:
        whid = str(row.get("WH ID") or "").strip()
        listing = by_whid.get(whid)
        if not listing:
            unmatched.append({"listing_name": row.get("Listing Name"),
                              "wh_id": whid})
            continue
        prefs = listing.get("listing_preferences") or {}
        targets.append({
            "listing_id": listing["id"],
            "channel": listing.get("channel") or "brightside",
            "wh_id": listing.get("wheelhouse_id"),
            "title": (listing.get("title") or "").strip(),
            # BR comes from the sheet, not Wheelhouse - the sheet drives
            # bedroom-size grouping per DATA-CONTRACT.
            "bedrooms": row.get("BR"),
            "sheet_name": row.get("Listing Name"),
            "kdd_name": row.get("KDD Name"),
            "city": row.get("City"),
            "pool": row.get("Pool"),
            "jk": str(row.get("JK") or "").strip().lower() == "yes",
            "airbnb_link": row.get("Airbnb link"),
            "listing_preferences": {k: prefs.get(k) for k in PREF_FIELDS},
        })
    return targets, unmatched, meta


def _progress(label, i, total):
    if i % 25 == 0 or i == total:
        print("    %s: %d/%d" % (label, i, total), end="\r", flush=True)


def per_listing(run, key, targets, endpoint):
    """Fetch one single-page endpoint for every target, streaming to jsonl."""
    def gen():
        total = len(targets)
        for i, t in enumerate(targets, 1):
            lid = t["listing_id"]
            path = "/listings/%s/%s" % (urllib.parse.quote(str(lid)), endpoint)
            try:
                payload = wh_api.api_get(path, {"channel": t["channel"]}, key)
                yield {"listing_id": lid, "data": wh_api.unwrap(payload, endpoint)}
            except RuntimeError as exc:
                run.fail(lid, endpoint, exc)
                yield {"listing_id": lid, "error": str(exc)}
            _progress(endpoint, i, total)
            time.sleep(wh_api.REQUEST_PAUSE)
    run.write_jsonl(endpoint, gen())
    print()


def collect_reservations(run, key, targets):
    """Every reservation for every Active listing, all pages, fee fields
    removed. One jsonl line per RESERVATION (not per listing), tagged with
    the listing it belongs to, so the file is directly analyzable."""
    def gen():
        total = len(targets)
        for i, t in enumerate(targets, 1):
            lid = t["listing_id"]
            path = "/listings/%s/reservations" % urllib.parse.quote(str(lid))
            page, n = 1, 0
            while page <= RES_MAX_PAGES:
                try:
                    payload = wh_api.api_get(
                        path, {"channel": t["channel"],
                               "per_page": RES_PAGE, "page": page}, key)
                except RuntimeError as exc:
                    run.fail(lid, "reservations", "page %d: %s" % (page, exc))
                    break
                batch = wh_api.unwrap(payload, "reservations")
                for r in batch:
                    rec = {k: r.get(k) for k in RES_FIELDS}
                    rec["listing_id"] = lid
                    rec["wh_id"] = t["wh_id"]
                    rec["bedrooms"] = t["bedrooms"]
                    rec["jk"] = t["jk"]
                    rec["rent_revenue"] = rec.pop("nightly_subtotal")
                    yield rec
                    n += 1
                if len(batch) < RES_PAGE:
                    break
                page += 1
                time.sleep(wh_api.REQUEST_PAUSE)
            if page > RES_MAX_PAGES:
                run.fail(lid, "reservations",
                         "hit RES_MAX_PAGES=%d - history may be truncated" % RES_MAX_PAGES)
            _progress("reservations", i, total)
            time.sleep(wh_api.REQUEST_PAUSE)
    run.write_jsonl("reservations", gen())
    print()


def collect_sets(run, key):
    """Comp sets (account-level, a handful of calls): every dynamic set plus
    its member comps and associated own listings."""
    try:
        sets = wh_api.api_get("/sets", {}, key)
    except RuntimeError as exc:
        run.fail("-", "sets", exc)
        return
    out = []
    for s in sets if isinstance(sets, list) else []:
        rec = dict(s)
        # price_calendar: paid sets of <= 25 members only; a 404/422 from a
        # free or oversized set is expected, recorded, and not a failure.
        for name, endpoint in (("members", "listings"),
                               ("associated", "associated_listings"),
                               ("calendars", "price_calendar")):
            try:
                data = wh_api.api_get("/sets/%s/%s" % (s["id"], endpoint), {}, key)
            except RuntimeError as exc:
                if name != "calendars":
                    run.fail(s["id"], "set-" + endpoint, exc)
                data = None
            rec[name] = data
            time.sleep(wh_api.REQUEST_PAUSE)
        if isinstance(rec.get("members"), dict):
            for group in rec["members"].values():
                for m in group if isinstance(group, list) else []:
                    for f in SET_MEMBER_DROP:
                        m.pop(f, None)
        out.append(rec)
    run.write_json("sets", out)
    run.counts["sets"] = len(out)
    print("  sets: %d comp sets" % len(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--limit", type=int, help="collect only the first N listings")
    ap.add_argument("--only", help="comma-separated subset of: %s" % ",".join(ALL_STAGES))
    args = ap.parse_args()

    stages = tuple(s.strip() for s in args.only.split(",")) if args.only else ALL_STAGES
    bad = set(stages) - set(ALL_STAGES)
    if bad:
        sys.exit("unknown stage(s): %s" % ", ".join(sorted(bad)))

    key = wh_api.load_key()
    run = store.Run(args.date)
    print("Nightly collection %s -> %s  [stages: %s]"
          % (run.date, run.dir, ", ".join(stages)))

    targets, unmatched, sheet_meta = active_targets(key)
    if args.limit:
        targets = targets[:args.limit]
    print("  targets: %d active listings%s%s"
          % (len(targets),
             " (LIMITED to %d)" % args.limit if args.limit else "",
             ", %d unmatched" % len(unmatched) if unmatched else ""))
    for u in unmatched:
        run.fail(u["wh_id"], "units-defined-join",
                 "sheet row %r has no live Wheelhouse listing" % u["listing_name"])

    run.write_json("listings", targets)
    run.counts["listings"] = len(targets)

    # Per-pull sheet record (Mike, 2026-09-17): every pull saves what the
    # sheet said that night - all units-defined rows and the parsed market
    # blocks - so market data and active units can be reconciled against the
    # exact sheet state of any past pull. Pickup-vs-market is derived from
    # consecutive saved pulls (the sheet no longer carries 7-days-ago blocks).
    try:
        all_units, _ = sheet_access.read_units_defined(SAFE_XLSX, active_only=False)
        run.write_json("units", all_units)
        run.counts["units"] = len(all_units)
    except Exception as exc:
        run.fail("-", "units-save", exc)
    try:
        import market_tabs
        mt = market_tabs.parse_workbook(SAFE_XLSX)
        run.write_json("market_tabs", mt)
        run.counts["market_tabs"] = len(mt)
    except Exception as exc:
        run.fail("-", "market_tabs", exc)

    if "kpis" in stages:
        kpis, total = {}, len(targets)
        for i, t in enumerate(targets, 1):
            lid = t["listing_id"]
            try:
                path = "/listings/%s/kpis" % urllib.parse.quote(str(lid))
                payload = wh_api.api_get(path, {"channel": t["channel"]}, key)
                if isinstance(payload, dict) and "kpis" in payload:
                    payload = payload["kpis"]
                kpis[lid] = strip_fees({k: payload.get(k) for k in KPI_FIELDS})
            except RuntimeError as exc:
                run.fail(lid, "kpis", exc)
            _progress("kpis", i, total)
            time.sleep(wh_api.REQUEST_PAUSE)
        print()
        run.write_json("kpis", kpis)
        run.counts["kpis"] = len(kpis)

    for ep in CALENDAR_ENDPOINTS:
        if ep in stages:
            per_listing(run, key, targets, ep)

    if "reservations" in stages:
        collect_reservations(run, key, targets)

    if "sets" in stages:
        collect_sets(run, key)

    meta = run.finalize(extra={"sheet": sheet_meta,
                               "scope": "units-defined Active only",
                               "stages": list(stages),
                               "unmatched": unmatched})
    print("\nstatus=%s counts=%s failures=%s"
          % (meta["status"], meta["counts"], meta["failure_count"]))
    for f in run.failures[:5]:
        print("  FAIL", f["listing_id"], f["endpoint"], f["error"][:70])


if __name__ == "__main__":
    main()
