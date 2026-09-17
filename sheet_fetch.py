"""Headless download of the Bear Camp master workbook via the Drive API.

The nightly runs with no Claude session and no Drive connector, but the same
service account that publishes the hub dashboard (data/firebase-sa.json) can
export the sheet directly - provided two one-time conditions hold:

  1. The Drive API is enabled on the service account's GCP project.
  2. The master sheet is shared (Viewer) with the service account's
     client_email.

fetch() downloads the workbook as xlsx to the RAW path and returns True.
Any failure returns False with one printed line - callers fall back to the
last sanitized copy, so a Drive outage never kills a collection night.

The raw file may contain forbidden tabs; callers must sanitize immediately
(sheet_access.sanitize) - collect_nightly.prepare_sheet() already does.
"""
import os

FILE_ID = "16ij0oBMEaiLXONhya7P6k-Wns-oNgazKPhN31ggsnAU"
XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")
SA_PATH = os.environ.get("FIREBASE_SERVICE_ACCOUNT",
                         os.path.join("data", "firebase-sa.json"))


def fetch(dest, timeout=120):
    """Export the master sheet as xlsx to `dest`. True on success."""
    if not os.path.exists(SA_PATH):
        print("  sheet_fetch: no service account at %s - using last copy" % SA_PATH)
        return False
    try:
        import requests
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request

        creds = service_account.Credentials.from_service_account_file(
            SA_PATH,
            scopes=["https://www.googleapis.com/auth/drive.readonly"])
        creds.refresh(Request())
        url = ("https://www.googleapis.com/drive/v3/files/%s/export"
               "?mimeType=%s" % (FILE_ID, XLSX_MIME))
        resp = requests.get(
            url, headers={"Authorization": "Bearer %s" % creds.token},
            timeout=timeout)
        if resp.status_code != 200:
            # 403/404 mean the sheet is not shared with the SA (or the Drive
            # API is off). Body may echo details; never log tokens.
            print("  sheet_fetch: HTTP %d - using last copy" % resp.status_code)
            return False
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(resp.content)
        os.replace(tmp, dest)
        print("  sheet_fetch: downloaded %d bytes" % len(resp.content))
        return True
    except Exception as exc:  # network, auth - never fatal for the nightly
        print("  sheet_fetch: %s - using last copy" % exc)
        return False


if __name__ == "__main__":
    import sys
    ok = fetch(os.path.join("data", "master.xlsx"))
    sys.exit(0 if ok else 1)
