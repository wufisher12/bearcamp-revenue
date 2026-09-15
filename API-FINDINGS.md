# API-FINDINGS.md — verified Wheelhouse RM API surface

Endpoint probes run 2026-09-03 against the live account; the bulk listings pull referenced
below was first run 2026-08-20 and re-run 2026-09-03. This documents what was
**observed**, and partially fills the gap left by the referenced-but-missing
`API-MIGRATION.md`. Anything not confirmed by an actual response is marked UNVERIFIED.

**Base URL:** `https://api.usewheelhouse.com/ss_api/v1`
**Auth header:** `X-Integration-Api-Key` (RM API key; single key, no separate user key)
**Rate limits:** 429 exists. Docs say space calls out and batch rather than firing in
parallel. Collector uses sequential calls with a 0.15s pause and Retry-After backoff.

## Confirmed endpoints

| Endpoint | Status | Shape |
|---|---|---|
| `GET /listings` | 200 | bare JSON array; `page`/`per_page` (max 100), `include_managed_listings`, `exclude_inactive`. Page until a short page — 406 listings over 5 pages (pull of 2026-08-20; re-pulled 2026-09-03). |
| `GET /listings/{id}/kpis?channel=` | 200 | dict of metric -> {window: value} |
| `GET /listings/{id}/min_max_prices?channel=` | 200 | `{data: [{stay_date, min_price, max_price}]}` — **per-stay-date resolved floor/ceiling** |
| `GET /listings/{id}/price_calendar?channel=` | 200 | list[364] of {stay_date, price, is_available, is_booked, reservation_id, block_time, ...} |
| `GET /listings/{id}/min_stay_calendar?channel=` | 200 | list[546] of {stay_date, min_stay} |
| `GET /listings/{id}/custom_rates?channel=` | 200 | list of {start_date, end_date, rate_type, per-weekday values, expires_at} |
| `GET /listings/{id}/reservations?channel=` | 200 | list[50] of {booked_at, canceled_at, start_date, end_date, nightly_subtotal, num_guests, ...} |
| `GET /listings/{id}/preferences` | **404** | not at this path (config comes inline on `/listings`) |
| `GET /listings/{id}/seasonal_min_prices` | **404** | no such path |
| `GET /listings/{id}/seasons` | **404** | no such path |

## Seasonal min rates — answers PRODUCT-V2 §7 open item (READ half)

There is **no season-object endpoint**. Seasonal (and weekend) minimums are exposed as a
**resolved per-stay-date calendar** via `/min_max_prices`. Observed on `9757-bearcamp`:
min alternates 95 / 105 with 105 landing on Fridays and Saturdays — i.e. the endpoint returns
the *effective* floor per night after seasonal/weekend rules are applied.

Consequences for the Monday Checklist and wizard:
- "seasonal min (current season)" is derivable: read `/min_max_prices` and report the
  effective min for the relevant date range (and the distinct values present).
- "min binding?" should compare the price calendar against this **per-date** min, not
  against the single `listing_preferences.min_price`. The flat-min assumption in the
  current scorer is wrong wherever seasonal/weekend uplift exists.
- **WRITE capability is UNVERIFIED and was deliberately not tested** — probing a PUT/POST
  would mutate production pricing, and PRODUCT-V2 §4 forbids anything executing without
  explicit approval. Confirm from docs or a sandbox before the wizard treats seasonal-min
  as executable; until then it stays a deep-link per PRODUCT-V2 §7.

## KPI response — field notes

~42 metrics, each a dict keyed by window. Windows: forward `0_7 0_14 0_21 0_30 0_60 0_90
0_180 0_365`, trailing `7_0 14_0 21_0 30_0 60_0 90_0 180_0 365_0`.

**Breaking change vs the MCP payload:** there is no `last_booked_at` timestamp. The direct
API exposes `last_booked_days` (numeric, windowed). `collector.py::extract` and `days_since`
must be adapted — use the trailing-window value (all trailing windows returned the same
value, 15, on the probe) rather than parsing a timestamp.

Metrics newly relevant to PRODUCT-V2 that the current pipeline ignores:
- `revpar_adjusted_occupancy` — **this is the Adjusted RevPAR** the Client Report (§5)
  specifies (revenue over bookable nights). No need to compute it.
- `nights_blocked`, `revenue_blocked` — quantify owner-blocked calendars. `revenue_blocked`
  at `0_365` gives the dollar value of a blocked calendar directly.
- `nights_bookable`, `nights_booked`, `bookings`, `pickup_bookings`, `lead_time`,
  `length_of_stay`, `comp_set_*`.

## Reservation-level data is available from Wheelhouse

`/listings/{id}/reservations` returns `booked_at` and `canceled_at` per reservation. This
overlaps the master sheet's `2026-arrivals` / `2025-arrivals` tabs — and the sheet exports
are the ones DATA-CONTRACT §6.2–6.4 flags as broken (2025 incomplete, 2026 truncated at
Sep 30, fee basis shifted). Worth evaluating whether Wheelhouse reservations can serve as
the YoY source instead of, or as a cross-check against, the sheet exports. Not yet assessed:
history depth (does it reach back through 2025?) and whether fee fields are consistent.
