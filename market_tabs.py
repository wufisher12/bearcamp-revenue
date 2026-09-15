"""Parser for the stacked market-data blocks in data/master.xlsx.

Each market tab holds SIX stacked blocks in columns A:B, separated by blank rows.
A block begins on a row whose column A is the literal string "Date"; column B of
that same row is the block header, formatted:

    <Entity>: Adj. Paid Occ. % | <start> - <end> as of <as_of> | <period aggregate %>

Entity is either "Direct (Pigeon Forge + 4 more)" (MARKET) or
"Bear Camp Cabin Rentals" (PORTFOLIO).

Everything (entity, window, as-of, aggregate) is parsed FROM THE HEADER TEXT.
Tab name and block position are never trusted.

SAFETY: the workbook contains a `logins` tab with plaintext credentials.
It is never opened, iterated, or named in output -- see FORBIDDEN_SHEETS.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re

# Sheets that must never be read under any circumstance.
FORBIDDEN_SHEETS = {"logins"}

# Tabs we treat as market tabs: market-data<N> and <N>br-market-data<N>.
MARKET_TAB_RE = re.compile(r"^(?:\d+br-)?market-data\d+$", re.IGNORECASE)

ENTITY_MARKET = "MARKET"
ENTITY_PORTFOLIO = "PORTFOLIO"

ENTITY_MAP = {
    "direct (pigeon forge + 4 more)": ENTITY_MARKET,
    "bear camp cabin rentals": ENTITY_PORTFOLIO,
}

# <entity>: <metric> | <start> - <end> as of <as_of> | <aggregate>
HEADER_RE = re.compile(
    r"""^\s*
        (?P<entity>.+?)\s*:\s*
        (?P<metric>.+?)\s*\|\s*
        (?P<start>\S+)\s*-\s*(?P<end>\S+)\s+as\s+of\s+(?P<as_of>\S+)\s*\|\s*
        (?P<aggregate>[-+]?[\d.,]+)\s*%?\s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

DOCUMENTED_ORDER = [
    (ENTITY_MARKET, "today"),
    (ENTITY_MARKET, "last_year"),
    (ENTITY_MARKET, "seven_days_ago"),
    (ENTITY_PORTFOLIO, "today"),
    (ENTITY_PORTFOLIO, "last_year"),
    (ENTITY_PORTFOLIO, "seven_days_ago"),
]


def is_market_tab(name: str) -> bool:
    """True for market tabs; always False for anything in FORBIDDEN_SHEETS."""
    if name.strip().lower() in FORBIDDEN_SHEETS:
        return False
    return bool(MARKET_TAB_RE.match(name.strip()))


def market_tab_names(workbook) -> list[str]:
    """Market tab names in workbook order, forbidden sheets excluded."""
    return [n for n in workbook.sheetnames if is_market_tab(n)]


def _norm_date(value):
    """Return an ISO date string from a datetime, date, or M/D/YYYY string."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    text = str(value).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return _dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text  # unparseable -- surface it verbatim rather than guessing


def _norm_number(value):
    """Coerce a cell to float. Cells arrive as str or float depending on writer."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").rstrip("%")
    # Strip a trailing ".0" artifact on integer-ish IDs/values before compare.
    if text.endswith(".0"):
        text = text[:-2]
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_header(text: str) -> dict:
    """Parse a block header string into its components.

    Raises ValueError if the header does not match the documented format.
    """
    if text is None:
        raise ValueError("header cell is empty")
    raw = str(text).strip()
    m = HEADER_RE.match(raw)
    if not m:
        raise ValueError(f"header does not match contract format: {raw!r}")
    entity_raw = m.group("entity").strip()
    entity = ENTITY_MAP.get(entity_raw.lower())
    if entity is None:
        raise ValueError(f"unrecognized entity in header: {entity_raw!r}")
    return {
        "entity": entity,
        "entity_raw": entity_raw,
        "metric": m.group("metric").strip(),
        "window_start": _norm_date(m.group("start")),
        "window_end": _norm_date(m.group("end")),
        "as_of": _norm_date(m.group("as_of")),
        "period_aggregate": _norm_number(m.group("aggregate")),
        "header_raw": raw,
    }


def parse_market_tab(worksheet) -> list[dict]:
    """Extract every stacked block from a market worksheet.

    Returns a list of dicts:
        {entity, entity_raw, metric, window_start, window_end, as_of,
         period_aggregate, header_raw, start_row, points: [{date, value}]}

    Blocks are found by scanning column A for the literal "Date"; the block runs
    until the next "Date" row or a blank row. Order is workbook order but nothing
    is inferred from it.
    """
    name = getattr(worksheet, "title", "")
    if name.strip().lower() in FORBIDDEN_SHEETS:
        raise ValueError(f"refusing to read forbidden sheet {name!r}")

    rows = list(worksheet.iter_rows(min_col=1, max_col=2, values_only=True))

    blocks: list[dict] = []
    current: dict | None = None

    for idx, row in enumerate(rows, start=1):
        a = row[0] if len(row) > 0 else None
        b = row[1] if len(row) > 1 else None

        a_text = str(a).strip() if a is not None else ""

        if a_text == "Date":
            current = parse_header(b)
            current["start_row"] = idx
            current["points"] = []
            blocks.append(current)
            continue

        if a is None and b is None:
            current = None  # blank row terminates the block
            continue

        if current is None:
            continue  # stray content outside any block

        date = _norm_date(a)
        value = _norm_number(b)
        if date is not None:
            current["points"].append({"date": date, "value": value})

    return blocks


def order_matches_documented(blocks: list[dict]) -> bool:
    """True if the six blocks appear in the documented entity/as-of order.

    Derived purely from parsed headers: within each entity, the block whose
    window_start is earliest by ~a year is the LY block; of the two sharing the
    latest window_start, the later as_of is "today" and the earlier is "7 days ago".
    """
    if len(blocks) != 6:
        return False
    actual = [(b["entity"], _role(b, blocks)) for b in blocks]
    return actual == DOCUMENTED_ORDER


def _role(block: dict, blocks: list[dict]) -> str:
    """Classify a block as today / last_year / seven_days_ago among its entity peers."""
    peers = [b for b in blocks if b["entity"] == block["entity"]]
    latest_start = max(b["window_start"] for b in peers)
    if block["window_start"] != latest_start:
        return "last_year"
    current = [b for b in peers if b["window_start"] == latest_start]
    newest_as_of = max(b["as_of"] for b in current)
    return "today" if block["as_of"] == newest_as_of else "seven_days_ago"


def annotate_roles(blocks: list[dict]) -> list[dict]:
    """Attach a derived `role` field to each block. Mutates and returns blocks."""
    for b in blocks:
        b["role"] = _role(b, blocks)
    return blocks


def tab_content_hash(worksheet) -> str:
    """SHA-256 over the normalized A:B cell values of a worksheet.

    Ignores the tab name entirely, so identical hashes across tabs mean the
    cell content is identical.
    """
    name = getattr(worksheet, "title", "")
    if name.strip().lower() in FORBIDDEN_SHEETS:
        raise ValueError(f"refusing to read forbidden sheet {name!r}")
    h = hashlib.sha256()
    for row in worksheet.iter_rows(min_col=1, max_col=2, values_only=True):
        parts = []
        for cell in row:
            if cell is None:
                parts.append("")
            elif isinstance(cell, (_dt.datetime, _dt.date)):
                parts.append(_norm_date(cell))
            else:
                text = str(cell).strip()
                if text.endswith(".0"):
                    text = text[:-2]
                parts.append(text)
        h.update("\x1f".join(parts).encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def parse_workbook(path: str) -> dict:
    """Parse every market tab in the workbook at `path`."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        out = {}
        for name in market_tab_names(wb):
            ws = wb[name]
            out[name] = {
                "content_sha256": tab_content_hash(ws),
                "blocks": annotate_roles(parse_market_tab(wb[name])),
            }
        return out
    finally:
        wb.close()


def write_json(data, path: str) -> None:
    """Write UTF-8 JSON without a BOM."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
