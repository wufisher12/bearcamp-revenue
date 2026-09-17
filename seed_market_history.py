"""Seed pickup-vs-market baselines from the master sheet's Drive revisions.

The nightly saves market data per pull, but that history only began
2026-09-17. This one-off exports the sheet AS OF an earlier date from Google
revision history, parses its market tabs, and saves them to
data/market_history/<as_of>.json, where publish_hub's pickup baseline
lookup finds them.

Revision visibility requires the service account to have EDIT access to the
sheet (Google hides version history from viewers). The raw export may carry
non-allowlisted tabs, so it is sanitized before parsing and deleted after.

    python seed_market_history.py [--days-ago 7]
"""
import argparse
import datetime as dt
import io
import json
import os
import sys

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

import market_tabs
import sheet_access
import sheet_fetch as S

OUT_DIR = os.path.join("data", "market_history")


def token():
    creds = service_account.Credentials.from_service_account_file(
        S.SA_PATH, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    creds.refresh(Request())
    return creds.token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-ago", type=int, default=7)
    args = ap.parse_args()
    target = dt.date.today() - dt.timedelta(days=args.days_ago)

    tok = token()
    hdr = {"Authorization": "Bearer %s" % tok}
    r = requests.get(
        "https://www.googleapis.com/drive/v3/files/%s/revisions"
        "?fields=revisions(id,modifiedTime,exportLinks)&pageSize=1000" % S.FILE_ID,
        headers=hdr, timeout=60)
    r.raise_for_status()
    revs = r.json().get("revisions", [])
    if not revs:
        sys.exit("no revisions visible - the service account needs Editor "
                 "access to the sheet to read version history")

    # Last revision saved ON or BEFORE the end of the target day.
    cut = target.isoformat() + "T23:59:59Z"
    older = [v for v in revs if v["modifiedTime"] <= cut]
    if not older:
        sys.exit("no revision on or before %s (oldest is %s)"
                 % (target, revs[0]["modifiedTime"]))
    rev = max(older, key=lambda v: v["modifiedTime"])
    link = (rev.get("exportLinks") or {}).get(S.XLSX_MIME)
    if not link:
        sys.exit("revision %s has no xlsx export link" % rev["id"])
    print("revision %s modified %s" % (rev["id"], rev["modifiedTime"]))

    raw = os.path.join("data", "revision_raw.xlsx")
    safe = os.path.join("data", "revision_safe.xlsx")
    resp = requests.get(link, headers=hdr, timeout=120)
    resp.raise_for_status()
    with open(raw, "wb") as f:
        f.write(resp.content)
    print("exported %d bytes" % len(resp.content))
    try:
        sheet_access.sanitize(raw, safe, remove_src=True)
        mt = market_tabs.parse_workbook(safe)
    finally:
        for p in (raw, safe):
            if os.path.exists(p):
                os.remove(p)

    main_tab = mt.get("market-data90")
    if not main_tab:
        sys.exit("revision has no market-data90 tab")
    blocks = market_tabs.annotate_roles(main_tab["blocks"])
    today_blk = next((b for b in blocks
                      if b["entity"] == "PORTFOLIO" and b["role"] == "today"), None)
    if not today_blk:
        sys.exit("revision market-data90 has no portfolio today block")
    as_of = today_blk["as_of"]

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "%s.json" % as_of)
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(mt, f, indent=1)
    print("seeded %s (market as of %s, %d tabs)" % (out, as_of, len(mt)))


if __name__ == "__main__":
    main()
