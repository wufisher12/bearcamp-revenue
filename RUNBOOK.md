# Runbook — Bear Camp Revenue System v2

Supersedes the Mon/Thu MCP-driven runbook of 2026-07-23. Product spec is
`../PRODUCT-V2.md`; data rules are `../DATA-CONTRACT.md` as amended by the
directives below. The verified API surface is `API-FINDINGS.md`.

Python is NOT on PATH: `C:/Users/mfish/AppData/Local/Programs/Python/Python312-arm64/python.exe`

## Standing directives (Mike, 2026-09-03 and 2026-09-15)

| Rule | Where enforced |
|---|---|
| **Rent revenue only.** Fee data is corrupt in the sheet and in Wheelhouse (same ~9x inflation, both fed from Brightside). ADR and Adjusted RevPAR are rent-based. | `collect_nightly.py` refuses `*_fees` KPI fields by assertion and drops reservation money fields other than `nightly_subtotal`; `arrivals.py` drops fee columns at the reader |
| **Attribution by arrival date** (check-in). | `reservations.py`, `arrivals.py` |
| **Scope = `units-defined` rows with Status == Active** (287 at 2026-09-15). Not the ~408 Wheelhouse roster. | `collect_nightly.active_targets()` |
| **Wheelhouse is the source of record for reservations** — it reflects cancellations and refunds; the sheet export does not. Full-year YoY is allowed on this source. | `reservations.py` |
| The sheet export (`2025-/2026-arrivals`) is a fallback only. YoY on it is **Oct–Dec only**. | `arrivals.yoy()` raises outside that window |
| `units-defined` and the market tabs stay in the sheet — Mike maintains them. Column reads are alias-tolerant (`sheet_access.COLUMN_ALIASES`); a required column empty on every row marks the night `partial` so the publisher keeps the last good snapshot. | `sheet_access.py` allowlist, `col()`, `dead_columns()` |
| Slack doorbell to **#bear-camp** is allowed. Client email is a **Gmail draft, never sent**. Permanent. | `notify.py` |
| Never read a `logins` tab. | `sheet_access.py` — allowlist + sanitize-on-download |

## Nightly (automatic, headless)

Windows Task Scheduler task **"BearCamp Nightly Collect"** runs `run_nightly.cmd`
at 03:00 daily. Wakes the machine, runs on battery, retries twice, and runs at
next opportunity if the window is missed. No Claude session, no MCP.

It runs `collect_nightly.py`, which:
1. Tries a fresh headless download of the master sheet (`sheet_fetch.py` — Drive
   export using the hub service account at `data/firebase-sa.json`; requires the
   sheet shared Viewer with that account and the Drive API enabled on
   fisher-family-hub). Sanitizes → `data/master_safe.xlsx`; on any fetch failure
   runs against the last sanitized copy. Each run saves that night's parsed
   market blocks (`market_tabs.json.gz`) and all `units-defined` rows
   (`units.json.gz`) into the snapshot for reconciliation; APO pickup vs market
   on the hub's Benchmarking tab is derived from these saved pulls.
2. Reads Active rows from `units-defined`, pulls `/listings`, joins on WH ID.
3. Pulls per listing: KPIs, price calendar, min/max prices, min-stay calendar,
   custom rates, and **all reservation pages**.
4. Writes an immutable run to `data/snapshots/<date>/` and appends to
   `data/snapshots/index.json`. Status is `ok` / `partial` / `failed`; failures
   are listed in `failures.json`, never dropped.

Runtime ~60 min (~1,440 calendar/KPI calls + ~600 reservation calls at ~1.75 s
each). Output ~2–3 MB gzipped per night. Log: `data/logs/nightly.log`.

**Check it ran:** `python -c "import store; print(store.coverage(7))"` — any
`missing` entry is a lost night; history cannot be backfilled.

Manual run / subset: `python collect_nightly.py [--only kpis,reservations] [--limit N]`.

## Hub dashboard publish (runs after the nightly collect)

`publish_hub.py` builds the Bear Camp client dashboard document
(client-dashboard-contract-v2 in the fisherfamilyhub repo) from the latest
good snapshot and writes it to Firestore `hub/mfg-client-bearcamp` in project
`fisher-family-hub`. The hub portal renders it live. Chained in
`run_nightly.cmd` after the collector; also runnable manually any time.

- **Credentials:** a Firebase service-account JSON at `data/firebase-sa.json`
  (or point `FIREBASE_SERVICE_ACCOUNT` at another path). Generate: Firebase
  console → fisher-family-hub → Project settings → Service accounts →
  Generate new private key. `data/` is gitignored; never commit or log it.
  Without the key the step logs "skipped" and exits 0.
- **Dry run:** `python publish_hub.py --dry-run` — writes
  `data/hub_dashboard.json` only. The full document is saved there on real
  runs too, as the audit copy of what was published.
- Writes ONLY that one document, whole-document each run, idempotent per
  snapshot date. Rent basis / arrival attribution / OTB labeling rules apply
  (enforced upstream; labels baked into the document text).

## Monday assembly (to be built — PRODUCT-V2 §1)

Runs as a Claude scheduled task because it needs the Drive connector:
1. Download the master sheet to `data/master.xlsx` (Drive `download_file_content`,
   xlsx export — never `read_file_content`). The collector sanitizes it.
2. Build the Monday Checklist from the last 7 good snapshots + market tabs.
3. Publish `site/` (push to `main` triggers Pages).
4. Post the doorbell — `notify.build_message()`; deliver via `SLACK_WEBHOOK_URL`
   if set, else post `data/pending_slack.json` through the Slack connector.
5. Create the client email as a **Gmail draft**. Mike sends it.

## Secrets

`WHEELHOUSE_API_KEY` — env var, or a gitignored key file (see `wh_api.load_key`).
Never commit it, never log it, never put it in an error message. A file named
`*.gitignore` is NOT ignored by git — `.gitignore` has explicit patterns for it.

## Known data state (2026-09-15)

- Fee corruption in `2026-arrivals`: 2,748 rows created ≥ 2026-06-11 (and
  outliers earlier) — unfixed; Mike's re-export produced a byte-identical file.
  Root cause is upstream (Brightside), not the export.
- `2025-arrivals`: Jan–Feb absent, Mar–Sep at ~21% of 2026 volume. Oct–Dec complete.
- `2026-arrivals` ends at 2026-09-30 check-ins. **Wheelhouse has all of the above
  and more** (2024 → 2027), which is why it became the source of record.
- `market-data180` is a byte copy of `market-data90` (no 180-day data exists).
  The 12 per-bedroom tabs are real and distinct.
- `nightly_subtotal` == sheet `Rent Revenue` on ~86% of joined rows; the ~9%
  mismatches are consistently higher in Wheelhouse — unexplained, likely
  post-booking modifications. Ask Mike what the export's Rent Revenue nets out.
