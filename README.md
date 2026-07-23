# Bear Camp Revenue

Morning revenue triage for Bear Camp Cabin Rentals — pulls fresh Wheelhouse data
for the active portfolio, reconciles it against the authoritative Google Sheet,
scores each listing for revenue urgency, and publishes a static dashboard.

## What's published

GitHub Pages serves **`site/` only**. That is the dashboard plus two derived JSON
files (`site/data/snapshot.json`, `site/data/admin.json`) holding listing names,
city, bedroom count, price bounds and forward-looking KPIs.

Everything under `data/` is gitignored and never leaves the machine — the sheet
export contains property addresses and every other tab of the workbook.

**The published site is public.** Anyone with the URL can read it; there is no
password. That was a deliberate call — revisit it if listing-level revenue data
ever needs to be private, since a private repo does *not* make a Pages site private.

## Running a collection

Python is not on PATH on the build machine. Use the full interpreter path:

```bash
PY=C:/Users/mfish/AppData/Local/Programs/Python/Python312-arm64/python.exe
$PY run_recon.py        # sheet + Wheelhouse -> data/matched.json, data/admin_recon.json
$PY make_batches.py     # split matched listings into KPI batches
$PY make_retry_batches.py  # diff collected KPIs, emit retry batches for whatever is missing
$PY build_snapshot.py   # score, write data/ + site/data/ payloads, print the lists
```

The sheet pull and the Wheelhouse pulls run through MCP connectors, so they are
driven from a Claude Code session rather than from these scripts.

Preview the dashboard locally (the page fetches JSON, so `file://` will not work):

```bash
$PY -m http.server 8731 --directory site
```

## Tuning the scoring

Every weight and threshold is a constant in the `TUNING` block at the top of
`collector.py`. Nothing else needs editing to re-tune.

Two rules the scoring encodes deliberately:

- **Beating the market is a pass.** Pacing only penalises a listing below 1.00x its
  Wheelhouse neighbourhood, judged on the 0–60 day window. All other metrics are
  30-day. Roughly 90% of the book beats its market, so a relative bar flagged
  listings that were doing fine.
- **Urgency and upside are separate lists.** `priority_score` ranks empty calendars;
  `upside_score` ranks strong sellers whose minimum price is capping the rate. They
  never mix, and configuration problems affect neither — those go to Admin Review.

## Dashboard tabs

| Tab | Contents |
|---|---|
| Review Pricing (Low Pacing) | 25 most urgent listings, sortable and filterable |
| Review Pricing (High Pacing) | 10 raise-the-min candidates |
| Portfolio by Bedroom | Forward-looking medians per bedroom tier |
| Admin Review | Reconciliation, config gaps, blocked calendars, collection status |

## Data notes

- The sheet is authoritative for **what exists**; Wheelhouse is authoritative for
  **how it is pacing**. `WH ID` is the join key.
- The active roster changes as listings onboard and leave. Never hardcode a count.
- Wheelhouse listing pagination must run until a short page returns. An early run
  assumed a page count and silently missed listings.
- Null forward occupancy has two causes that must not be conflated: a listing that
  has never booked is new; one with booking history is a closed-out calendar.
