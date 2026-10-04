#!/usr/bin/env python3
"""
The Morning Five — apply-link liveness gate.

Reads a client's dated brief (clients/<id>/runs/<date>.md), extracts every
markdown apply link, and HTTP-checks each one so DEAD postings (404 / 410 /
"Gone") get dropped or demoted by the engine BEFORE the brief is delivered —
instead of being surfaced to the client with a "verify before applying" chore.

Zero third-party dependencies (stdlib urllib only). Best-effort: some boards
(LinkedIn, Naukri, Workday) bot-block automated HEAD/GET and return 403/999.
Those are reported as UNKNOWN, not DEAD — a 403 means "we couldn't verify",
not "the job is gone". Only 404/410 are treated as hard-dead.

Usage:
  python clients/check_links.py <client-id>              # latest brief
  python clients/check_links.py <client-id> --date 2026-06-25
  python clients/check_links.py <client-id> --json       # machine-readable

Exit code: 0 if no DEAD links, 1 if one or more DEAD links found (so the engine
/ a CI step can gate on it). UNKNOWN links never fail the gate.
"""

import argparse
import json
import os
import re
import ssl
import sys
import _runs
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))

# Windows consoles default to cp1252 and can't encode the report's arrows/emoji.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Markdown links: [label](http...).  Capture the URL only.
LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")

DEAD_CODES = {404, 410}
# Boards that habitually bot-block automated probes — a non-2xx here is almost
# always anti-bot, not a dead posting. Don't fail the gate on these.
BOT_WALLED = ("linkedin.com", "naukri.com", "myworkdayjobs.com",
              "indeed.com", "foundit.in", "glassdoor.")


def _ssl_ctx():
    ctx = ssl.create_default_context()
    # Boards with imperfect cert chains shouldn't crash a liveness probe.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def probe(url, timeout=12):
    """Return (status_label, http_code_or_None, note)."""
    ctx = _ssl_ctx()
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return ("OK", r.status, f"{method} {r.status}")
        except urllib.error.HTTPError as e:
            if e.code in DEAD_CODES:
                return ("DEAD", e.code, f"{method} {e.code} — posting gone")
            if method == "GET":  # exhausted both methods
                walled = any(b in url for b in BOT_WALLED)
                label = "UNKNOWN" if walled or e.code in (403, 405, 429, 999) else "SUSPECT"
                return (label, e.code, f"{method} {e.code}")
            # else: HEAD rejected, fall through to GET
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as e:
            if method == "GET":
                return ("UNKNOWN", None, f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001 — never let one bad URL crash the gate
            if method == "GET":
                return ("UNKNOWN", None, f"{type(e).__name__}: {e}")
    return ("UNKNOWN", None, "no response")


def latest_brief(client_dir):
    """Newest dated brief. Reads both runs/YYYY-MM/ and legacy flat runs/."""
    runs = os.path.join(client_dir, "runs")
    date, path = _runs.latest_brief(runs)
    if not path:
        sys.exit(f"[err] no dated briefs in {runs}")
    return path


def main():
    ap = argparse.ArgumentParser(description="Apply-link liveness gate for a Morning Five brief.")
    ap.add_argument("client_id")
    ap.add_argument("--date", help="YYYY-MM-DD (defaults to latest brief)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    client_dir = os.path.join(HERE, args.client_id)
    if not os.path.isdir(client_dir):
        sys.exit(f"[err] no such client: {client_dir}")

    if args.date:
        brief = _runs.brief_path(os.path.join(client_dir, "runs"), args.date)
        if not brief:
            sys.exit(f"[err] brief not found for {args.date} in {client_dir}/runs")
    else:
        brief = latest_brief(client_dir)
    if not os.path.isfile(brief):
        sys.exit(f"[err] brief not found: {brief}")

    with open(brief, encoding="utf-8") as fh:
        text = fh.read()

    # Dedupe URLs, preserve first-seen order.
    urls, seen = [], set()
    for m in LINK_RE.finditer(text):
        u = m.group(1).rstrip(".,);")
        if u not in seen:
            seen.add(u)
            urls.append(u)

    if not urls:
        print(f"[ok] no apply links found in {os.path.basename(brief)}")
        return 0

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda u: (u, *probe(u)), urls))

    dead = [r for r in results if r[1] == "DEAD"]
    suspect = [r for r in results if r[1] == "SUSPECT"]

    if args.json:
        print(json.dumps([
            {"url": u, "status": s, "code": c, "note": n}
            for (u, s, c, n) in results
        ], indent=2))
    else:
        print(f"[liveness] {os.path.basename(brief)} — {len(urls)} apply links\n")
        for (u, s, c, n) in results:
            mark = {"OK": "ok ", "DEAD": "DEAD", "SUSPECT": "??  ", "UNKNOWN": "--  "}[s]
            print(f"  [{mark}] {n:<22} {u}")
        print()
        print(f"  summary: {len(results)-len(dead)-len(suspect)} ok · "
              f"{len(dead)} DEAD · {len(suspect)} suspect · "
              f"{sum(1 for r in results if r[1]=='UNKNOWN')} unverifiable (bot-walled)")
        if dead:
            print("\n  ⚠️  DROP or DEMOTE these in the brief before delivering — the posting is gone:")
            for (u, *_rest) in dead:
                print(f"     - {u}")

    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
