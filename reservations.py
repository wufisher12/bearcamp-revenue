"""Reservation analytics on the Wheelhouse snapshot store.

Source of record for reservations is Wheelhouse, not the sheet export
(Mike, 2026-09-15): Wheelhouse reflects cancellations and partial refunds,
the export does not. The sheet-based `arrivals.py` remains as a fallback.

Rules carried in:
  * RENT BASIS ONLY. `rent_revenue` here is Wheelhouse's stay-level
    `nightly_subtotal`, which equals the export's Rent Revenue on ~86% of
    joined rows; fee fields were dropped at collection.
  * ARRIVAL-DATE ATTRIBUTION. A stay belongs to its check-in month.
  * ACTIVE LISTINGS ONLY. The collector only pulls reservations for units
    Active on units-defined, so everything here is already in scope.
  * Full-year YoY is permitted on this source (Mike, 2026-09-15) - Wheelhouse
    holds 2024 onward, so the Oct-Dec restriction that applies to the sheet
    export does not apply here.

Two lenses, both needed:
  realized(...)  - stays by check-in date, regardless of when booked.
  otb(...)       - "on the books as of <date>": stays in a window that had
                   been booked by that date. Compare otb(today) against
                   otb(today - 1 year) for like-for-like pacing. Always label
                   the as-of date on anything derived from it.
"""
import datetime as _dt
import gzip
import json
import os

import store

ACCEPTED = "Accepted"


def _d(v):
    if not v:
        return None
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def load(run=None):
    """Reservations from a snapshot run (default: latest good). Adds
    derived fields; drops nothing further."""
    run = run or store.latest_good(1)[0]
    p = run.path("reservations.jsonl.gz")
    if not os.path.exists(p):
        raise FileNotFoundError("no reservations in snapshot %s" % run.date)
    out = []
    with gzip.open(p, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if "error" in r:
                continue
            ci, co = _d(r.get("start_date")), _d(r.get("end_date"))
            if not ci:
                continue
            r["check_in"], r["check_out"] = ci, co
            r["nights"] = (co - ci).days if (ci and co) else None
            r["booked"] = _d(r.get("booked_at") or r.get("created_at"))
            r["canceled"] = _d(r.get("canceled_at"))
            r["is_active"] = (r.get("status") == ACCEPTED
                              and not r.get("canceled_at"))
            out.append(r)
    return out


def active(rows):
    """Live stays only. Canceled reservations are excluded from every
    revenue figure - they are kept in the store for audit, not for math."""
    return [r for r in rows if r["is_active"]]


def in_window(rows, start, end):
    return [r for r in rows if start <= r["check_in"] <= end]


def in_months(rows, year, months):
    return [r for r in rows if r["check_in"].year == year
            and r["check_in"].month in months]


def aggregate(rows, key=None):
    """Rent-basis rollup. key(row) -> group; None = whole set."""
    groups = {} if key else {"all": _bucket()}
    for r in rows:
        g = key(r) if key else "all"
        b = groups.setdefault(g, _bucket())
        b["reservations"] += 1
        b["nights"] += r["nights"] or 0
        b["rent_revenue"] += r.get("rent_revenue") or 0
    for b in groups.values():
        b["adr"] = (b["rent_revenue"] / b["nights"]) if b["nights"] else None
    return groups


def _bucket():
    return {"reservations": 0, "nights": 0, "rent_revenue": 0.0}


def _compare(cur, prior, label):
    out = {}
    for g in sorted(set(cur) | set(prior)):
        c, p = cur.get(g), prior.get(g)
        ok = bool(c and p and c["reservations"] and p["reservations"])
        rec = {"basis": label, "current": c, "prior": p, "comparable": ok,
               "rent_revenue_delta_pct": None, "adr_delta_pct": None,
               "nights_delta_pct": None}
        if ok:
            if p["rent_revenue"]:
                rec["rent_revenue_delta_pct"] = (
                    (c["rent_revenue"] - p["rent_revenue"]) / p["rent_revenue"] * 100)
            if c["adr"] and p["adr"]:
                rec["adr_delta_pct"] = (c["adr"] - p["adr"]) / p["adr"] * 100
            if p["nights"]:
                rec["nights_delta_pct"] = (c["nights"] - p["nights"]) / p["nights"] * 100
        out[g] = rec
    return out


def realized_yoy(rows, year, months=range(1, 13), key=None):
    """Stays by check-in month, `year` vs `year-1`. Any month range."""
    months = tuple(months)
    live = active(rows)
    cur = aggregate(in_months(live, year, months), key)
    prior = aggregate(in_months(live, year - 1, months), key)
    return _compare(cur, prior,
                    "rent revenue, arrival-date attribution, %d vs %d, months %s"
                    % (year, year - 1, list(months)))


def otb(rows, as_of, start, end, key=None):
    """On the books as of `as_of`: stays with check-in in [start, end] that
    had been booked on or before `as_of` and were not canceled by then."""
    sel = []
    for r in rows:
        if not (start <= r["check_in"] <= end):
            continue
        if not r["booked"] or r["booked"] > as_of:
            continue
        if r["canceled"] and r["canceled"] <= as_of:
            continue
        if r.get("status") != ACCEPTED and not r["canceled"]:
            continue
        sel.append(r)
    return aggregate(sel, key)


def pace_yoy(rows, as_of, start, end, key=None):
    """Same-point-last-year pacing: otb(as_of, window) vs
    otb(as_of - 1y, window - 1y). Label with both as-of dates."""
    ly = _dt.date(as_of.year - 1, as_of.month, min(as_of.day, 28))
    cur = otb(rows, as_of, start, end, key)
    prior = otb(rows, ly,
                _dt.date(start.year - 1, start.month, min(start.day, 28)),
                _dt.date(end.year - 1, end.month, min(end.day, 28)), key)
    return _compare(cur, prior,
                    "rent revenue OTB, as of %s vs %s" % (as_of, ly))


def coverage(rows):
    """What the store actually holds, so nobody assumes."""
    live = active(rows)
    by_year = {}
    for r in live:
        y = r["check_in"].year
        b = by_year.setdefault(y, {"reservations": 0, "rent_revenue": 0.0,
                                   "first_check_in": r["check_in"],
                                   "last_check_in": r["check_in"]})
        b["reservations"] += 1
        b["rent_revenue"] += r.get("rent_revenue") or 0
        b["first_check_in"] = min(b["first_check_in"], r["check_in"])
        b["last_check_in"] = max(b["last_check_in"], r["check_in"])
    return {y: {**v, "first_check_in": v["first_check_in"].isoformat(),
                     "last_check_in": v["last_check_in"].isoformat()}
            for y, v in sorted(by_year.items())}
