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
SET_URL = "https://app.usewheelhouse.com/u/sets/%s/overview"


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _cal_window(nights, start_iso, end_iso):
    """Posted-rate and availability stats for [start, end) of one calendar."""
    sel = [n for n in nights or [] if start_iso <= n.get("stay_date", "") < end_iso]
    prices = [n["price"] for n in sel if n.get("price")]
    open_n = sum(1 for n in sel if n.get("is_available"))
    return {
        "avg_price": (sum(prices) / len(prices)) if prices else None,
        "occ": (1 - open_n / len(sel)) if sel else None,
        "nights": sel,
    }


def compsets_list(sets_data, listings, own_calendars=None, as_of=None):
    """Every Wheelhouse comp set, future-first: block cards carry comp count
    plus next-90 calendar occupancy and posted rate; the detail adds a weekly
    comp-rate-vs-own chart and a per-comp table. Fully API-driven."""
    own_calendars = own_calendars or {}
    own_by_id = {l["listing_id"]: l for l in listings}
    start = as_of.isoformat() if as_of else ""
    end = (as_of + dt.timedelta(days=90)).isoformat() if as_of else "9999"

    out_sets = []
    for s in sets_data or []:
        members = (s.get("members") or {}).get("active") or []
        cal_by_id = {c["listing_id"]: c.get("price_calendar")
                     for c in s.get("calendars") or []}

        rows, occs, rates = [], [], []
        for m in sorted(members, key=lambda m: (m.get("title") or "").lower()):
            w = _cal_window(cal_by_id.get(m.get("listing_id")), start, end)
            if w["occ"] is not None:
                occs.append(w["occ"])
            if w["avg_price"] is not None:
                rates.append(w["avg_price"])
            rows.append({"cells": [
                {"text": (m.get("title") or "?")[:60], "url": m.get("url")},
                str(m.get("bedrooms") or "-"),
                str(m.get("sleeps") or "-"),
                "$%s" % format(round(w["avg_price"]), ",") if w["avg_price"] is not None else "-",
                "%.0f%%" % (w["occ"] * 100) if w["occ"] is not None else "-",
                "%.1f%%" % (m["occupancy_adjusted_365_0"] * 100)
                if m.get("occupancy_adjusted_365_0") is not None else "-",
                "$%s" % format(round(m["adr_365_0"]), ",") if m.get("adr_365_0") is not None else "-",
            ]})

        assoc = []
        for a in s.get("associated") or []:
            own = own_by_id.get(a.get("id"))
            assoc.append({
                "name": (own or {}).get("sheet_name") or a.get("title"),
                "id": a.get("id"),
                "whUrl": WH_LISTING_URL % a["wheelhouse_id"] if a.get("wheelhouse_id") else None,
            })

        # Posted-rate charts: pooled comp median vs own posted rate, over
        # arbitrary date buckets.
        def rate_chart(buckets, title):
            xl, med_series = [], []
            own_series = {a["id"]: [] for a in assoc if a["id"] in own_calendars}
            for bs, be, label in buckets:
                xl.append(label)
                pooled = []
                for nights in cal_by_id.values():
                    pooled += [n["price"] for n in nights or []
                               if bs <= n.get("stay_date", "") < be and n.get("price")]
                med_series.append(round(_median(pooled)) if pooled else None)
                for aid in own_series:
                    w = _cal_window(own_calendars[aid], bs, be)
                    own_series[aid].append(round(w["avg_price"]) if w["avg_price"] is not None else None)
            if all(v is None for v in med_series):
                return None
            series = [{"name": "Comp median rate", "values": med_series}]
            for a in assoc:
                if a["id"] in own_series:
                    series.append({"name": "%s (posted)" % a["name"],
                                   "values": own_series[a["id"]]})
            return {"title": title, "xLabels": xl, "series": series,
                    "format": "currency"}

        chart, chart_monthly = None, None
        if cal_by_id and as_of:
            weekly = []
            for wk in range(13):
                ws = as_of + dt.timedelta(days=7 * wk)
                weekly.append((ws.isoformat(), (ws + dt.timedelta(days=7)).isoformat(),
                               ws.strftime("%b %d").replace(" 0", " ")))
            chart = rate_chart(weekly, "Posted nightly rate by week - next 90 days")

            # Monthly, for the six months after the 90-day window. The month
            # the window ends in is only partially covered, so start there
            # (Mike, 2026-09-23); if the window happens to end exactly on a
            # month boundary, start with the following month.
            end90 = as_of + dt.timedelta(days=90)
            mstart = dt.date(end90.year, end90.month, 1)
            if (end90 + dt.timedelta(days=1)).month != end90.month:
                mstart = end90 + dt.timedelta(days=1)
            monthly = []
            for _ in range(6):
                mnext = dt.date(mstart.year + (mstart.month == 12),
                                mstart.month % 12 + 1, 1)
                monthly.append((mstart.isoformat(), mnext.isoformat(),
                                mstart.strftime("%b %Y")))
                mstart = mnext
            chart_monthly = rate_chart(
                monthly, "Posted nightly rate by month - beyond 90 days")

        crit = []
        f = s.get("filters") or {}
        if f.get("room_type", {}).get("one_of"):
            crit.append("/".join(map(str, f["room_type"]["one_of"])))
        if f.get("bedrooms", {}).get("one_of"):
            crit.append("%s BR" % ", ".join(map(str, sorted(f["bedrooms"]["one_of"]))))

        med_occ, med_rate = _median(occs), _median(rates)
        out_sets.append({
            "id": s["id"],
            "name": s.get("name"),
            "setUrl": SET_URL % s["id"],
            "kind": s.get("kind"),
            "paid": bool(s.get("is_paid")),
            "updated": str(s.get("updated_at") or "")[:10],
            "criteria": " · ".join(crit) if crit else "hand-picked comps",
            "kpis": [
                {"label": "Comps", "value": str(len(members))},
                {"label": "Occ Next 90", "value": "%.0f%%" % (med_occ * 100) if med_occ is not None else "-"},
                {"label": "Med. Rate Next 90", "value": "$%s" % format(round(med_rate), ",") if med_rate is not None else "-"},
            ],
            "associated": assoc,
            "chart": chart,
            "chartMonthly": chart_monthly,
            "columns": ["Comp", "BR", "Sleeps", "Rate Next 90",
                        "Occ Next 90", "APO 365", "ADR 365"],
            "rows": rows,
        })
    if not out_sets:
        return None
    return {
        "type": "compsetList",
        "sets": out_sets,
        "note": "Pulled automatically from Wheelhouse each night. Next-90 "
                "figures come from the comps' live calendars: rate is the "
                "posted nightly price, occupancy is the share of nights not "
                "available (booked and blocked are indistinguishable for a "
                "comp). Trailing metrics are the last 365 days - Wheelhouse "
                "does not expose per-comp 90-day history.",
    }


# ------------------------------------------------------------ reservations
def _ly_match(res_by_listing, r):
    """The listing's own booking covering the same date last year: prefer a
    stay whose range contains check-in minus a year, else the one with the
    closest check-in within 7 days. None when the listing wasn't booked."""
    ci = r["check_in"]
    target = ci.replace(year=ci.year - 1) if not (ci.month == 2 and ci.day == 29) \
        else ci.replace(year=ci.year - 1, day=28)
    best, best_gap = None, 8
    for c in res_by_listing.get(r["listing_id"], []):
        if c is r or c["check_in"].year != target.year and abs((c["check_in"] - target).days) > 7:
            continue
        if c["check_in"] <= target and c["check_out"] and target < c["check_out"]:
            return c, target
        gap = abs((c["check_in"] - target).days)
        if gap < best_gap:
            best, best_gap = c, gap
    return best, target


def reservations_section(rows, listings, notes_prefix, as_of):
    """All active stays with check-in on or after 2025-01-01, newest created
    first, sharded into companion docs by check-in half-year (a single doc
    cannot hold ~23K rows under Firestore's 1MB cap). Columnar arrays per
    shard; canceled bookings drop out on every nightly full re-pull. Fee
    fields never collected (Brightside fee bug), so rent only."""
    floor = dt.date(2025, 1, 1)
    live = [r for r in rows if r["is_active"] and r["check_in"] >= floor
            and r.get("nights") and r["nights"] > 0]

    meta = {l["listing_id"]: l for l in listings}
    lids = sorted({r["listing_id"] for r in live})
    lidx = {lid: i for i, lid in enumerate(lids)}
    listing_lookup = {}
    for lid in lids:
        l = meta.get(lid, {})
        listing_lookup[str(lidx[lid])] = {
            "n": l.get("sheet_name") or l.get("title") or lid,
            "br": l.get("bedrooms"),
            "jk": bool(l.get("jk")),
        }

    by_listing = {}
    for r in rows:
        if r["is_active"]:
            by_listing.setdefault(r["listing_id"], []).append(r)

    def shard_key(r):
        return "%dh%d" % (r["check_in"].year, 1 if r["check_in"].month <= 6 else 2)

    shards = {}
    live.sort(key=lambda r: str(r.get("booked") or ""), reverse=True)
    preview = []
    for r in live:
        ly, _ = _ly_match(by_listing, r)
        ly_adr = round((ly.get("rent_revenue") or 0) / ly["nights"]) \
            if ly and ly.get("nights") else None
        row = [
            lidx[r["listing_id"]],
            (r["booked"] or r["check_in"]).isoformat(),
            r["check_in"].isoformat(),
            r["nights"],
            round(r.get("rent_revenue") or 0),
            ly_adr,
            ly["check_in"].isoformat() if ly else None,
            ly["nights"] if ly else None,
            ((ly["check_in"] - ly["booked"]).days if ly.get("booked") else None) if ly else None,
        ]
        s = shards.setdefault(shard_key(r), {"li": [], "cr": [], "ci": [], "ni": [],
                                             "rr": [], "la": [], "lc": [], "ln": [], "lb": []})
        for k, v in zip(("li", "cr", "ci", "ni", "rr", "la", "lc", "ln", "lb"), row):
            s[k].append(v)
        if len(preview) < 200:
            preview.append({"c": row})

    shard_docs = {"%s-res-%s" % (notes_prefix, k): v for k, v in sorted(shards.items())}
    section = {
        "type": "reservations",
        "asOf": as_of.isoformat(),
        "count": len(live),
        "listings": listing_lookup,
        "shards": sorted(shard_docs.keys()),
        "preview": preview,
        "note": "Active bookings with check-in since 2025-01-01; canceled "
                "stays drop out automatically on the nightly refresh. Rent "
                "basis only - fee data stays excluded until the Brightside "
                "feed is fixed. LY ADR is the same listing's booking covering "
                "the same date last year (or the nearest check-in within a "
                "week); hover it for that booking's details.",
    }
    return section, shard_docs
