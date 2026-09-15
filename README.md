# Bear Camp Revenue

Revenue triage for Bear Camp Cabin Rentals (~287 active listings, Smokies).
Nightly Wheelhouse collection into an append-only store; a Monday ritual builds
the review views, drafts the client email, and rings a Slack doorbell.

Product spec: `../PRODUCT-V2.md`. Data contract: `../DATA-CONTRACT.md`.
Operations: `RUNBOOK.md`. Verified API surface: `API-FINDINGS.md`.

## Non-negotiables

- **Rent revenue only.** Fee data is corrupt upstream (Brightside → both the
  sheet export and Wheelhouse). Fee fields are refused at collection and at
  the sheet reader, not merely ignored.
- **Arrival-date attribution.**
- **Only `units-defined` Active listings.** The sheet decides what is in the
  portfolio; Wheelhouse says how it is doing.
- **Wheelhouse is the source of record for reservations.** The sheet export
  misses cancellations and refunds.
- **Never send.** Client email is a Gmail draft Mike sends himself. The only
  outbound message is a Slack doorbell to `#bear-camp` — counts and a link.
- **Never read a `logins` tab.** Enforced by allowlist, not convention.

## Layout

| File | Role |
|---|---|
| `collect_nightly.py` | Headless nightly collector (Task Scheduler entry via `run_nightly.cmd`) |
| `wh_api.py` | Wheelhouse REST transport — pagination, backoff, key handling |
| `store.py` | Immutable dated snapshots + manifest under `data/snapshots/` |
| `sheet_access.py` | Tab allowlist, sanitize-on-download, `units-defined` reader |
| `reservations.py` | Realized YoY, on-the-books, same-point-last-year pace (rent basis) |
| `market_tabs.py` | Parses the six-block Key Data market tabs, per bedroom size |
| `arrivals.py` | Sheet-export fallback reader; YoY hard-limited to Oct–Dec |
| `notify.py` | Slack doorbell message + delivery (webhook or queued for the Claude task) |
| `site/` | Published dashboard (GitHub Pages) — **v1 views, pending v2 rebuild** |

Legacy, superseded by v2 and kept only until the Monday assembly replaces
them: `collector.py`, `build_snapshot.py`, `draft_emails.py`,
`make_batches.py`, `make_retry_batches.py`, `run_recon.py`.

## Running things

Python is not on PATH. Use
`C:/Users/mfish/AppData/Local/Programs/Python/Python312-arm64/python.exe`.

```
python collect_nightly.py                       # full nightly run
python collect_nightly.py --only reservations   # one stage
python -c "import store; print(store.coverage(7))"   # did the last 7 nights run?
```

The Wheelhouse API key comes from `WHEELHOUSE_API_KEY` or a gitignored key
file. Everything under `data/` and `drafts/` is gitignored; only `site/` is
published, and the published site is **public** with no password.

## Publishing

Pushing to `main` with changes under `site/` deploys to
https://wufisher12.github.io/bearcamp-revenue/ via GitHub Actions.
