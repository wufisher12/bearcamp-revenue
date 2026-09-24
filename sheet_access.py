"""Guarded access to the Bear Camp master workbook.

The workbook has historically contained a `logins` tab holding plaintext
credentials. Drive's export API cannot fetch a subset of tabs, so the whole
file lands on disk before any code chooses what to read. The defense is
therefore structural rather than advisory:

  1. A positive ALLOWLIST. Anything not named here cannot be opened, and
     `open_tab` raises rather than returning a worksheet.
  2. `sanitize()` rewrites the downloaded workbook to a copy containing only
     allowlisted tabs and removes the raw file, so forbidden content does not
     sit on disk for longer than the download itself.

No function here ever prints or returns cell contents of a non-allowlisted
tab, and error messages name the condition, never the data.
"""
import os
import re

import openpyxl

UNITS_TAB = "units-defined"
ARRIVALS_TABS = ("2026-arrivals", "2025-arrivals")
MARKET_TAB_RE = re.compile(r"^(?:\d{1,2}br-)?market-data(?:90|180)$", re.I)

# Exact names always permitted.
ALLOWED_EXACT = {UNITS_TAB, *ARRIVALS_TABS}

# Never openable, whatever else changes. Matched case-insensitively as a
# substring so `logins`, `Logins copy`, `old logins` are all caught.
FORBIDDEN_SUBSTRINGS = ("login", "password", "credential", "secret", "api key", "apikey")


class ForbiddenTab(Exception):
    """Raised on any attempt to touch a tab outside the allowlist."""


def is_allowed(name):
    n = (name or "").strip()
    if any(bad in n.lower() for bad in FORBIDDEN_SUBSTRINGS):
        return False
    return n in ALLOWED_EXACT or bool(MARKET_TAB_RE.match(n))


def market_tabs(wb):
    return [n for n in wb.sheetnames if MARKET_TAB_RE.match(n) and is_allowed(n)]


def load(path, read_only=True):
    """Open the workbook. Does not read any cells."""
    return openpyxl.load_workbook(path, data_only=True, read_only=read_only)


def open_tab(wb, name):
    """The only sanctioned way to get a worksheet."""
    if not is_allowed(name):
        raise ForbiddenTab(
            f"tab {name!r} is not on the allowlist and must not be read"
        )
    if name not in wb.sheetnames:
        raise KeyError(f"tab {name!r} not present in workbook")
    return wb[name]


def audit(path):
    """Report which tabs are present, allowed, and forbidden. Names only."""
    wb = load(path)
    try:
        names = list(wb.sheetnames)
    finally:
        wb.close()
    return {
        "all": names,
        "allowed": [n for n in names if is_allowed(n)],
        "forbidden": [n for n in names
                      if any(b in n.lower() for b in FORBIDDEN_SUBSTRINGS)],
        "unrecognized": [n for n in names
                         if not is_allowed(n)
                         and not any(b in n.lower() for b in FORBIDDEN_SUBSTRINGS)],
    }


def sanitize(src, dst, remove_src=True):
    """Write a copy of `src` containing only allowlisted tabs.

    Returns a report. Cells of dropped tabs are never read - openpyxl removes
    the sheet wholesale. Call this immediately after download.
    """
    wb = openpyxl.load_workbook(src, data_only=True)   # not read_only: must mutate
    try:
        keep = [n for n in wb.sheetnames if is_allowed(n)]
        dropped = [n for n in wb.sheetnames if n not in keep]
        for name in dropped:
            del wb[name]
        wb.save(dst)
    finally:
        wb.close()
    if remove_src and os.path.abspath(src) != os.path.abspath(dst):
        os.remove(src)
    return {"kept": keep, "dropped": dropped, "path": dst}


# Column headers Mike has used over time; read by meaning, not exact name,
# so a sheet rename (BR -> "# BR", "Airbnb link" -> "Link", 2026-09-24)
# doesn't silently blank a field across the portfolio.
COLUMN_ALIASES = {
    "BR": ("# BR", "BR", "Bedrooms", "# Bedrooms"),
    "Link": ("Link", "Airbnb link", "Airbnb Link"),
}


def col(row, key):
    """Row value for a logical column, whatever the sheet calls it today."""
    for name in COLUMN_ALIASES.get(key, (key,)):
        if row.get(name) is not None:
            return row[name]
    return None


# ------------------------------------------------------------------ readers

def _clean(v):
    """Excel numerics arrive as floats; IDs must not render as '51058558.0'."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, str):
        return v.strip()
    return v


def rows_by_header(ws, header_row=1, first_data_row=None):
    """Yield dicts keyed by the header row. Duplicate headers get ' (2)'."""
    first_data_row = first_data_row or header_row + 1
    headers, seen = [], {}
    for cell in next(ws.iter_rows(min_row=header_row, max_row=header_row)):
        h = str(cell.value).strip() if cell.value is not None else ""
        if not h:
            headers.append(None)
            continue
        seen[h] = seen.get(h, 0) + 1
        headers.append(h if seen[h] == 1 else f"{h} ({seen[h]})")

    for row in ws.iter_rows(min_row=first_data_row):
        vals = [c.value for c in row]
        if all(v is None or str(v).strip() == "" for v in vals):
            continue
        yield {h: _clean(v) for h, v in zip(headers, vals) if h}


def read_units_defined(path_or_wb, active_only=True):
    """Row 1 = active-count cell, row 2 = headers, data from row 3.

    Returns (rows, meta). BR/ID/WH ID are coerced to strings without the
    trailing '.0'. Listing Name is whitespace-stripped - the sheet has at
    least one trailing-space name that breaks exact joins.
    """
    wb = load(path_or_wb) if isinstance(path_or_wb, str) else path_or_wb
    close = isinstance(path_or_wb, str)
    try:
        ws = open_tab(wb, UNITS_TAB)
        declared = None
        for cell in next(ws.iter_rows(min_row=1, max_row=1)):
            if cell.value is not None:
                declared = _clean(cell.value)
                break
        rows = list(rows_by_header(ws, header_row=2, first_data_row=3))
    finally:
        if close:
            wb.close()

    for r in rows:
        if "Listing Name" in r and isinstance(r["Listing Name"], str):
            r["Listing Name"] = r["Listing Name"].strip()

    active = [r for r in rows
              if str(r.get("Status", "")).strip().lower() == "active"]
    meta = {
        "declared_active_count": declared,
        "total_data_rows": len(rows),
        "active_rows": len(active),
        "declared_matches_actual": str(declared) == str(len(active)),
        "status_counts": {},
    }
    for r in rows:
        s = str(r.get("Status", "")).strip() or "(blank)"
        meta["status_counts"][s] = meta["status_counts"].get(s, 0) + 1
    return (active if active_only else rows), meta
