# Scheduled collection runbook

Followed by the `bearcamp-monday` and `bearcamp-thursday` scheduled tasks.
Cadence: **Monday and Thursday mornings** (Mike's call 2026-07-23 — daily was
overkill). Everything here is drafts-and-dashboard only; **never send email,
never automate past Mike's review. That gate is permanent.**

Repo: `C:/Users/mfish/Desktop/bear-camp/bearcamp-revenue`
Python (not on PATH): `C:/Users/mfish/AppData/Local/Programs/Python/Python312-arm64/python.exe`

## 0. Preconditions

Check via ToolSearch that the Wheelhouse MCP (`wheelhouse_rmGetListings`,
`wheelhouse_rmGetListingKpis`) and Drive MCP (`download_file_content`) are
available. **If either is missing, STOP**: write `drafts/<date>-RUN-FAILED.md`
explaining which connector was absent, and finish. Do not publish a partial
snapshot; the site keeps serving the previous one.

## 1. Collect

1. **Sheet** (subagent): download fileId `1VzAOiE7lm8fwo4vRYpq03sdeTkizK89vbqkFqPTmzNs`
   as xlsx via `download_file_content`, decode, parse ONLY the `Unit Mix` tab
   (row 1 meta, row 2 headers), write all rows to `data/sheet_rows.json`.
   Never use `read_file_content` — it truncates. Suffix duplicate headers " (2)".
2. **Listings** (subagent): `wheelhouse_rmGetListings` with
   `include_managed_listings: true, exclude_inactive: true, per_page: 50`,
   paging until a short/empty page — never assume a page count. Trim each to
   id, wheelhouse_id, title, num_bedrooms, currency, channel,
   listing_preferences{min,base,max,automatic_rate_posting_enabled}.
   Write `data/wh_listings.json`.
3. `run_recon.py` → matched pairs + admin items.
4. `make_batches.py` then `make_retry_batches.py` → KPI targets. Fresh KPIs are
   required for ALL matched listings every run (snapshot overwrites), so before
   batching, delete stale caches: remove all files in `data/kpis/`.
   Then `make_retry_batches.py` emits the full target list.
5. **KPIs** (parallel subagents, ~20 listings each): `wheelhouse_rmGetListingKpis`
   per listing, keep only: occupancy, occupancy_adjusted, pickup, last_booked_at,
   asking_rate, adr, revpar, revenue_score, min_price_occurrence,
   nights_available, occupancy_neighborhood_adjusted_ratio. Retry each failed
   call up to 3x, never abort a batch, checkpoint the output file every 5
   listings. **Write JSON via the Write tool only — PowerShell redirects add a
   BOM that corrupts parsing.**
6. Re-run `make_retry_batches.py`; if listings remain, run one retry wave.

## 2. Build and verify

- `build_snapshot.py` — writes `data/` + `site/data/` payloads, prints the lists.
- Sanity: kpis_collected must equal unique_wh_listings; if not, list the stale
  listings in the run report and continue (partial-run report, not a failure).

## 3. Publish

- `git add site/ && git commit` (author: Mike Fisher <mike@fishergroup.co>),
  message like "Snapshot YYYY-MM-DD". Then `git push origin main`
  (credentials are cached; the push triggers the Pages deploy).
- Verify https://wufisher12.github.io/bearcamp-revenue/ serves the new
  `generated_at` (allow a few minutes for the deploy).

## 4. Email draft

- **Monday:** `draft_emails.py client` → `drafts/<date>-client-email.md`
- **Thursday:** `draft_emails.py team` → `drafts/<date>-team-admin-email.md`
- Light copy-editing of the generated draft is fine; adding claims not present
  in the data is not. Leave the file for Mike to review and send himself.

## 5. Report

End with a short summary: listings collected/failed, top-3 priority movers vs
the previous snapshot if visible, admin item count, dashboard URL, and the
path to the day's draft. Flag anything anomalous (roster jumps, mass nulls,
deploy failure) rather than working around it silently.
