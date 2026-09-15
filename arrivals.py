"""Reservation-level reader for the arrivals tabs.

RENT-ONLY, ENFORCED AT THE READER (Mike, 2026-09-03: "don't pay attention to
anything related to fees"). Fee columns are not distrusted-but-carried; they are
dropped before a row is ever returned, so nothing downstream can reach them by
accident. Fee Revenue in this workbook is corrupt - 2,748 of 9,688 2026 rows
carry roughly 9x inflated `Fees/Night`, with outliers reaching $94,538 of fees
on $2,900 of rent - and the corruption is self-consistent, so no arithmetic
check catches it. `Rent Revenue` was verified clean (zero null/zero/negative
rows in either tab) and is the only permitted basis.

Attribution is by ARRIVAL DATE (`Check In`), per the same instruction.

YoY is scoped to OCTOBER-DECEMBER only (Mike, 2026-09-03). 2025 Jan-Feb are
absent and Mar-Sep sits at ~20.7% of 2026 volume, so a YoY spanning those months
reports collapses that are really missing rows. Oct-Dec 2025 is complete. Any
caller asking for YoY outside that window gets a refusal, not a wrong number.
"""
import datetime as _dt
import re

import sheet_access

TAB_2026 = "2026-arrivals"
TAB_2025 = "2025-arrivals"

# Never returned by this module, whatever the header row says. `ADR Total` and
# `RevPAR (Total)` are the column-11 pair (they differ between the two tabs) and
# are fee-inclusive, so both are banned alongside the explicit fee columns.
FEE_COLUMNS = frozenset({
    "fees/night", "fee revenue", "total revenue", "fee %",
    "adr total", "revpar (total)", "revpar total",
})

KEEP = ("Reservation ID", "Unit Name", "Creation Date", "Check In",
        "Check Out", "# Nights", "Booking Window", "CI Month",
        "ADR", "Rent Revenue")

# YoY is only trustworthy for these check-in months.
YOY_MONTHS = (10, 11, 12)


def is_fee_column(header):
    return str(header or "").strip().lower() in FEE_COLUMNS


def _date(v):
    if v is None or v == "":
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return _dt.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def norm_unit(name):
    """Join key for `Unit Name` -> `Listing Name`.

    The 2026 tab has 12 variant groups that are pure formatting: trailing
    spaces, an embedded newline, and case differences. Ungrouped they split a
    unit into two rows and halve its revenue.
    """
    s = str(name or "").lower()
    s = s.replace("\n", " ").replace("\r", " ")
    s = re.sub(r"-\s*new\s+listing\s*$", "", s.strip())
    s = re.sub(r"\(.*?\)", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def read(path, tab):
    """Reservation rows with every fee column stripped."""
    wb = sheet_access.load(path)
    try:
        ws = sheet_access.open_tab(wb, tab)
        raw = list(sheet_access.rows_by_header(ws, header_row=1))
    finally:
        wb.close()

    out, dropped = [], set()
    for r in raw:
        rec = {}
        for k, v in r.items():
            if is_fee_column(k):
                dropped.add(k)
                continue
            if k in KEEP:
                rec[k] = v
        checkin = _date(rec.get("Check In"))
        if checkin is None:
            continue
        out.append({
            "reservation_id": rec.get("Reservation ID"),
            "unit_name": str(rec.get("Unit Name") or "").strip(),
            "unit_key": norm_unit(rec.get("Unit Name")),
            "created": _date(rec.get("Creation Date")),
            "check_in": checkin,
            "check_out": _date(rec.get("Check Out")),
            "nights": _num(rec.get("# Nights")),
            "booking_window": _num(rec.get("Booking Window")),
            "adr": _num(rec.get("ADR")),           # rent-based, verified
            "rent_revenue": _num(rec.get("Rent Revenue")),
        })
    return out, sorted(dropped)


def in_months(rows, months, year=None):
    return [r for r in rows
            if r["check_in"].month in months
            and (year is None or r["check_in"].year == year)]


def aggregate(rows, key=None):
    """Rent-basis rollup. key(row) -> group; None = whole set."""
    # Seed the whole-set bucket so an empty window returns zeros rather than
    # a missing key - 2026 Oct-Dec has no rows on the books yet.
    groups = {} if key else {"all": {"reservations": 0, "nights": 0.0,
                                     "rent_revenue": 0.0}}
    for r in rows:
        g = key(r) if key else "all"
        b = groups.setdefault(g, {"reservations": 0, "nights": 0.0,
                                  "rent_revenue": 0.0})
        b["reservations"] += 1
        b["nights"] += r["nights"] or 0
        b["rent_revenue"] += r["rent_revenue"] or 0
    for b in groups.values():
        # ADR on a rent basis: rent revenue over nights sold. Never fee-inclusive.
        b["adr"] = (b["rent_revenue"] / b["nights"]) if b["nights"] else None
    return groups


def yoy(rows_2026, rows_2025, key=None, months=YOY_MONTHS):
    """Rent-revenue YoY, Oct-Dec only.

    Refuses any other window rather than returning a number built on the
    ~80%-missing Mar-Sep 2025 data.
    """
    bad = sorted(set(months) - set(YOY_MONTHS))
    if bad:
        raise ValueError(
            "YoY is only valid for check-in months %s - 2025 data is "
            "incomplete outside that window (months %s requested)"
            % (list(YOY_MONTHS), bad))

    cur = aggregate(in_months(rows_2026, months, 2026), key)
    prior = aggregate(in_months(rows_2025, months, 2025), key)

    out = {}
    for g in sorted(set(cur) | set(prior)):
        c, p = cur.get(g), prior.get(g)
        # A delta against an empty side is not a -100% decline, it is unknown.
        comparable = bool(c and p and c["reservations"] and p["reservations"])
        if not comparable:
            out[g] = {
                "basis": "rent revenue, arrival-date attribution, Oct-Dec only",
                "current": c, "prior": p,
                "rent_revenue_delta_pct": None,
                "adr_delta_pct": None,
                "comparable": False,
            }
            continue
        out[g] = {
            "basis": "rent revenue, arrival-date attribution, Oct-Dec only",
            "current": c, "prior": p,
            "rent_revenue_delta_pct": (
                ((c["rent_revenue"] - p["rent_revenue"]) / p["rent_revenue"] * 100)
                if c and p and p["rent_revenue"] else None),
            "adr_delta_pct": (
                ((c["adr"] - p["adr"]) / p["adr"] * 100)
                if c and p and c["adr"] and p["adr"] else None),
            # Both sides must actually contain reservations. The seeded
            # whole-set bucket is present even when the window is empty, and
            # 2026 Oct-Dec has nothing on the books until that export extends.
            "comparable": bool(c and p and c["reservations"] and p["reservations"]),
        }
    return out
