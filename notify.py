"""Slack doorbell for the Monday assembly.

PRODUCT-V2 §1: "Slack is a doorbell, not a surface - no data in the message
beyond 'ready' + link + maybe the top-line count." This module therefore builds
a deliberately thin message. Listing names, rates and revenue never go in it.

Authorized scope (Mike, 2026-09-03): a doorbell to #bear-camp only. That is a
carve-out from the standing "never send a message on Mike's behalf" rule and it
does not extend anywhere else - the client email remains a Gmail DRAFT that Mike
sends by hand, permanently.

TRANSPORT. The nightly collector runs headless (Windows Task Scheduler, no
Claude session), so it has no MCP access. The Monday assembly is the only thing
that pings, and it can run either way:

  * If SLACK_WEBHOOK_URL is configured, post() sends the message itself and the
    assembly can also run headless.
  * Otherwise post() writes the message to data/pending_slack.json and returns
    False, and the Claude scheduled task posts it via the Slack connector to
    channel C0BTF92JCV6.

Either way the message text is built by one function, so the two paths cannot
drift.
"""
import json
import os
import urllib.error
import urllib.request

CHANNEL_NAME = "#bear-camp"
CHANNEL_ID = "C0BTF92JCV6"
DASHBOARD_URL = "https://wufisher12.github.io/bearcamp-revenue/"
PENDING = os.path.join("data", "pending_slack.json")

WEBHOOK_ENV = "SLACK_WEBHOOK_URL"


def build_message(checklist_count, run_date, coverage=None, warnings=None):
    """The doorbell. Counts and a link - never listing-level detail."""
    lines = ["*Bear Camp — Monday review ready*",
             "%d listings flagged for pricing review." % checklist_count,
             DASHBOARD_URL]

    if coverage:
        missing = [c["date"] for c in coverage if c["status"] != "ok"]
        if missing:
            lines.append("_Collection gaps this week: %d of %d nights "
                         "(%s)._" % (len(missing), len(coverage),
                                     ", ".join(sorted(missing))))
    for w in (warnings or []):
        lines.append("_%s_" % w)
    lines.append("_Data as of %s. Rent-revenue basis._" % run_date)
    return "\n".join(lines)


def post(message):
    """Send via webhook if configured; otherwise queue for the Claude task.

    Returns True if actually delivered.
    """
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if not url:
        os.makedirs("data", exist_ok=True)
        with open(PENDING, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"channel_id": CHANNEL_ID,
                       "channel": CHANNEL_NAME,
                       "message": message}, f, indent=1)
        print("no %s set - queued to %s for the Claude task to post"
              % (WEBHOOK_ENV, PENDING))
        return False

    req = urllib.request.Request(
        url, data=json.dumps({"text": message}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print("posted to %s" % CHANNEL_NAME)
        return True
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        # Never let a failed doorbell fail the assembly - the dashboard is
        # already published by this point and that is the actual deliverable.
        print("Slack post failed (%s); message left unqueued" % type(exc).__name__)
        return False


if __name__ == "__main__":
    print(build_message(10, "2026-09-03",
                        coverage=[{"date": "2026-09-02", "status": "ok"},
                                  {"date": "2026-09-01", "status": "missing"}]))
