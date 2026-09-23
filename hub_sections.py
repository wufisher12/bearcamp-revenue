"""Builders for the interactive hub dashboard sections (contract v2.1).

The split stays: this code owns WHAT (rows, component sums, series values),
the hub owns HOW IT LOOKS and all interactivity. Filterable KPIs ship as
ADDITIVE COMPONENTS per (bedrooms, tags) group so the hub can aggregate any
filter selection and evaluate the tiny expression set (sum:x, ratio:a/b,
count) declared per KPI - the hub does no other math.

Bases carried in: rent only, arrival-date attribution, canceled stays
excluded, active listings only. "Nights available" is CALENDAR nights
(listings x days) on the same-units basis - owner blocks are not excluded
because historical block data is not collected. Labeled in the note.
"""
import datetime as dt

import reservations as R

# Wheelhouse settings deep link, keyed by WH ID (units-defined column B).
WH_LISTING_URL = "https://app.usewheelhouse.com/l/%s/settings/minimum_price"
MAPS_URL = "https://www.google.com/maps/search/?api=1&query=%s"

# Nights Available was removed as a KPI (Mike, 2026-09-23): historical
# owner-block data is not collectable, so the number would be calendar
# nights, not true availability. The `avail` component stays - it is the
# denominator for APO and Adj. RevPAR (calendar-night basis, noted).
KPIS = [
    {"label": "Rent Revenue", "expr": "sum:rent", "format": "currency"},
    {"label": "Rent ADR", "expr": "ratio:rent/booked", "format": "currency"},
    {"label": "APO", "expr": "ratio:booked/avail", "format": "percent"},
    {"label": "Adj. RevPAR", "expr": "ratio:rent/avail", "format": "currency"},
    {"label": "Listings", "expr": "count", "format": "number"},
]


def _tags(listing):
    tags = [t.strip() for t in (listing.get("pool") or "").split(",") if t.strip()]
    if listing.get("jk"):
        tags.append("JK")
    return tags


def _url(link):
    link = (link or "").strip()
    if not link:
        return None
    return link if link.startswith("http") else "https://" + link


def _quote(s):
    import urllib.parse
    return urllib.parse.quote_plus(s)


# ------------------------------------------------------------ listing table
def listing_table(listings, units_rows, notes_doc):
    by_whid = {str(u.get("WH ID") or ""): u for u in units_rows}
    rows, by_br = [], {}
    for l in sorted(listings, key=lambda x: (x.get("sheet_name") or x.get("title") or "").lower()):
        u = by_whid.get(str(l.get("wh_id") or ""), {})
        br = str(l.get("bedrooms") or "?")
        by_br[br] = by_br.get(br, 0) + 1
        addr = " ".join(str(x) for x in (u.get("Address"), l.get("city"), "TN") if x)
        prefs = l.get("listing_preferences") or {}
        rows.append({
            "id": l["listing_id"],
            "name": l.get("sheet_name") or l.get("title"),
            "url": _url(l.get("airbnb_link")),
            "city": l.get("city"),
            "mapsUrl": MAPS_URL % _quote(addr) if u.get("Address") else None,
            "bedrooms": l.get("bedrooms"),
            "absMin": prefs.get("min_price"),
            "whUrl": WH_LISTING_URL % l["wh_id"] if l.get("wh_id") else None,
            "tags": _tags(l),
        })
    return {
        "type": "listingTable",
        "notesDoc": notes_doc,
        "summary": {"total": len(rows),
                    "byBedrooms": dict(sorted(by_br.items(),
                                              key=lambda kv: (kv[0] == "?", int(kv[0]) if kv[0].isdigit() else 99)))},
        "rows": rows,
        "note": "Abs. Min opens the listing in Wheelhouse (requires a "
                "Wheelhouse login). Notes are visible to everyone with "
                "portal access and keep a revision history.",
    }


# ------------------------------------------------------------ kpi explorer
def _period_starts(granularity, as_of, days=120):
    if granularity == "month":
        return ([dt.date(as_of.year, m, 1) for m in range(1, 13)],
                [dt.date(as_of.year - 1, m, 1) for m in range(1, 13)],
                ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    n = days // 7
    cur = [as_of + dt.timedelta(days=1 + 7 * i) for i in range(n)]
    ly = [d.replace(year=d.year - 1) if not (d.month == 2 and d.day == 29)
          else d.replace(year=d.year - 1, day=28) for d in cur]
    labels = [d.strftime("%b %d").replace(" 0", " ") for d in cur]
    return cur, ly, labels


def _period_end(start, granularity):
    if granularity == "month":
        nxt = dt.date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        return nxt - dt.timedelta(days=1)
    return start + dt.timedelta(days=6)


def kpi_explorer(rows, listings, as_of, granularity, note_extra=""):
    """One section: components per (bedrooms, tags) group per period, current
    year vs same-point-last-year (booked-by cutoff on both sides)."""
    ly_as_of = as_of.replace(year=as_of.year - 1) if (as_of.month, as_of.day) != (2, 29) \
        else as_of.replace(year=as_of.year - 1, day=28)
    cur_starts, ly_starts, labels = _period_starts(granularity, as_of)
    nper = len(labels)

    groups = {}
    for l in listings:
        key = (str(l.get("bedrooms") or "?"), tuple(sorted(_tags(l))))
        g = groups.setdefault(key, {"listings": set(), "cur": [dict(rent=0.0, booked=0) for _ in range(nper)],
                                    "ly": [dict(rent=0.0, booked=0) for _ in range(nper)]})
        g["listings"].add(l["listing_id"])
    gid_of = {lid: (str(l.get("bedrooms") or "?"), tuple(sorted(_tags(l))))
              for l in listings for lid in [l["listing_id"]]}

    def bucket(starts, year_shift):
        idx = {}
        for i, s in enumerate(starts):
            e = _period_end(s, granularity)
            d = s
            while d <= e:
                idx[d.toordinal()] = i
                d += dt.timedelta(days=1)
        return idx

    cur_idx, ly_idx = bucket(cur_starts, 0), bucket(ly_starts, -1)
    for r in rows:
        gkey = gid_of.get(r["listing_id"])
        if not gkey:
            continue
        ci = r["check_in"].toordinal()
        if ci in cur_idx:
            if r["is_active"]:
                b = groups[gkey]["cur"][cur_idx[ci]]
                b["rent"] += r.get("rent_revenue") or 0
                b["booked"] += r["nights"] or 0
        elif ci in ly_idx:
            # same-point-last-year: booked by ly_as_of, not canceled by then
            if (r["booked"] and r["booked"] <= ly_as_of
                    and not (r["canceled"] and r["canceled"] <= ly_as_of)
                    and (r.get("status") == "Accepted" or r["canceled"])):
                b = groups[gkey]["ly"][ly_idx[ci]]
                b["rent"] += r.get("rent_revenue") or 0
                b["booked"] += r["nights"] or 0

    out_groups, all_tags = [], set()
    for (br, tags), g in sorted(groups.items()):
        n = len(g["listings"])
        all_tags.update(tags)

        def comp(side, starts):
            return {
                "rent": [round(p["rent"]) for p in g[side]],
                "booked": [p["booked"] for p in g[side]],
                "avail": [n * ((_period_end(s, granularity) - s).days + 1) for s in starts],
            }
        out_groups.append({"bedrooms": br, "tags": list(tags), "listings": n,
                           "cur": comp("cur", cur_starts), "ly": comp("ly", ly_starts)})

    return {
        "type": "kpiExplorer",
        "granularity": granularity,
        "asOf": as_of.isoformat(),
        "lyAsOf": ly_as_of.isoformat(),
        "periods": labels,
        "kpis": KPIS,
        "filters": {"bedrooms": sorted({g["bedrooms"] for g in out_groups},
                                       key=lambda b: int(b) if b.isdigit() else 99),
                    "tags": sorted(all_tags)},
        "groups": out_groups,
        "note": "Active listings only, same-units basis. Rent only, arrival-"
                "date attribution; last year is on the books as of %s, not "
                "final. APO and Adj. RevPAR denominators are calendar nights "
                "(owner blocks not excluded).%s" % (ly_as_of, note_extra),
    }


# ------------------------------------------------------------- benchmark
def benchmark(mt, mt_block):
    """Six-series APO chart per bedroom variant. Series identity is
    (entity, vintage); the hub styles and toggles them."""
    def series_for(tab):
        p_today = mt_block(tab, "PORTFOLIO", "today")
        if not p_today:
            return None, None
        xl = [dt.date.fromisoformat(p["date"]).strftime("%b %d").replace(" 0", " ")
              for p in p_today["points"]]
        out = []
        for entity, ent_label in (("PORTFOLIO", "Bear Camp"), ("MARKET", "Market")):
            for role, vlabel, vintage in (("today", "", "today"),
                                          ("seven_days_ago", " · 7 days ago", "prior"),
                                          ("last_year", " · last year", "ly")):
                b = mt_block(tab, entity, role)
                if not b:
                    continue
                vals = [round(p["value"] * 100, 1) if p["value"] is not None else None
                        for p in b["points"]]
                vals = (vals + [None] * len(xl))[:len(xl)]
                out.append({"name": ent_label + vlabel, "entity": entity.lower(),
                            "vintage": vintage, "values": vals})
        return xl, out

    variants = []
    xl, series = series_for(mt.get("market-data90", {}))
    if series:
        variants.append({"id": "all", "label": "All bedrooms", "xLabels": xl, "series": series})
    for name in sorted((n for n in mt if n.endswith("br-market-data90")),
                       key=lambda n: int(n.split("br-")[0])):
        br = name.split("br-")[0]
        xl, series = series_for(mt[name])
        if series:
            variants.append({"id": "%sbr" % br, "label": "%s BR" % br,
                             "xLabels": xl, "series": series})
    if not variants:
        return None
    return {
        "type": "benchmark",
        "title": "Adjusted paid occupancy by week - portfolio vs market",
        "format": "percent",
        "variants": variants,
        "note": "Click a legend entry to hide or show its line.",
    }


# ------------------------------------------------------------- comp sets
def compset_shell(listings, kpis):
    """Draft comp-set view for Bear Necessities. Wheelhouse has no comp set
    on it yet (comp_set_count 0), so today's data is the listing vs its
    NEIGHBORHOOD occupancy; the comps list ships as labeled empty slots."""
    bn = next((l for l in listings
               if "bear necessities" in (l.get("sheet_name") or "").lower()), None)
    if not bn:
        return None
    k = kpis.get(bn["listing_id"], {})

    def kv(field, window):
        v = (k.get(field) or {}).get(window)
        return v

    def pct(v):
        return "%.1f%%" % (v * 100) if v is not None else "-"

    def money(v):
        return "$%s" % format(round(v), ",") if v is not None else "-"

    rows = []
    for win, label in (("0_7", "Next 7 days"), ("0_30", "Next 30 days"),
                       ("0_60", "Next 60 days"), ("0_90", "Next 90 days")):
        rows.append({"cells": [
            label,
            pct(kv("occupancy_adjusted", win)),
            pct(kv("occupancy_neighborhood_adjusted", win)),
            money(kv("asking_rate", win)),
        ]})
    return {
        "type": "compset",
        "name": bn.get("sheet_name") or bn.get("title"),
        "whUrl": WH_LISTING_URL % bn["wh_id"] if bn.get("wh_id") else None,
        "criteria": "%s BR · %s · %s. Comps: similar size, private pool, "
                    "Sevierville/Pigeon Forge corridor - selected in Wheelhouse."
                    % (bn.get("bedrooms"), (bn.get("pool") or "no pool"), bn.get("city")),
        "links": [{"label": "Comp slot %d - paste OTA link" % i, "url": None}
                  for i in range(1, 6)],
        "stats": {
            "columns": ["Window", "Listing APO", "Neighborhood APO", "Avg asking rate"],
            "rows": rows,
        },
        "note": "Draft layout. No Wheelhouse comp set exists for this listing "
                "yet (comp_set_count = 0), so market context is the Wheelhouse "
                "neighborhood; once a comp set is created, its occupancy and "
                "rate data replace the neighborhood columns.",
    }
