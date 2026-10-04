#!/usr/bin/env python3
"""
The Morning Five — cross-brief dedupe audit.

Reads a client's dated briefs (clients/<id>/runs/<date>.md), extracts every
role that was surfaced (Tier A/B `### score — Title @ Company` headings AND
Tier C `| score | role | company | location |` table rows), and reports when
the SAME role (dedupe key = company + normalized-title, with locations shown)
appears in more than one brief.

Why this exists: the engine's dedupe rule (clients/ENGINE.md) is role-level
over a ~14-day window. A role that resurfaces INSIDE that window should have
been deduped; a role that resurfaces AFTER it is allowed, but must be tagged
"seen <date>" so the client knows it's a re-surface, not a new find. This
script catches both failure modes BEFORE delivery instead of after.

Isolation is preserved: each client is audited only against its own runs/.

Zero third-party dependencies (stdlib only).

Usage:
  python clients/dedupe_audit.py <client-id>            # audit one client
  python clients/dedupe_audit.py --all                 # every client in _registry.json
  python clients/dedupe_audit.py <client-id> --window 14
  python clients/dedupe_audit.py <client-id> --latest-only   # only repeats hitting the newest brief

Exit code: 0 if the LATEST brief re-surfaces nothing from inside the window,
1 if it does (an untagged within-window duplicate reached the newest brief) —
so the engine / a CI step can gate delivery on it. Older historical repeats
are reported for awareness but never fail the gate on their own.
"""

import argparse
import json
import os
import re
import sys
import _runs
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

# Windows consoles default to cp1252 and can't encode the report's arrows/emoji.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# --- Brief heading formats -------------------------------------------------
# The brief layout has changed once already (mid-July 2026). When it did, the
# old regex silently matched NOTHING and the gate reported "clean — exit 0" on
# every brief for six weeks while duplicates accreted unchecked. Both formats
# are therefore supported, and `load_roles` fails loudly on a parse shortfall
# rather than trusting a suspiciously empty result. Never delete a format here.
#
# LEGACY (≤ 2026-07-15):  ### 83 — Title @ Company
HEADING_LEGACY_RE = re.compile(r"^#{3}\s+(\d{1,3})\s*[—–-]\s*(.+?)\s*$")
# CURRENT (≥ 2026-07-29):  ### [NEW — ]12. Title — Company (Location) — Score: 74 — *notes*
HEADING_RE = re.compile(
    r"^#{3}\s+(?:NEW\s*[—–-]\s*)?\d{1,3}\.\s*(.+?)\s*[—–-]\s*Score:\s*~?(\d{1,3})\b")
# Any '### ' heading that looks like it is meant to be a role entry — used to
# measure parse coverage (see load_roles). Excludes section headings like
# "### 🟢 Tier A" which carry no digit-dot index.
ROLEISH_RE = re.compile(r"^#{3}\s+(?:NEW\s*[—–-]\s*)?\d{1,3}[.\s]")
# Tier C table row:  | 50 | Role | Company | Location | ...
TABLE_RE = re.compile(r"^\|\s*(\d{2,3})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|")
# Trailing "(Chennai, Hybrid)" style location on a company fragment.
PAREN_LOC_RE = re.compile(r"\(([^)]*)\)\s*$")
# A [label](url) markdown link wrapper.
LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")

# Company org-unit suffixes that don't change the employer identity — strip so
# "BNP Paribas India Solutions" and "BNP Paribas" collapse to the same key.
COMPANY_SUFFIXES = {"plc", "ltd", "limited", "pvt", "inc", "llc",
                    "gsc", "gbs", "gds", "usi", "gcc", "india", "solutions"}


def clean_company(raw):
    """Strip a trailing annotation (⚠️, *note*, (parenthetical), · …) and org suffixes."""
    # Cut at the first annotation delimiter after the company name.
    for delim in ("⚠", "*", "(", "·", "—", "  "):
        idx = raw.find(delim)
        if idx != -1:
            raw = raw[:idx]
    raw = re.sub(r"[`_]", "", raw).strip()
    key = re.sub(r"[^a-z0-9 ]", "", raw.lower()).strip()
    toks = key.split()
    while toks and toks[-1] in COMPANY_SUFFIXES:
        toks.pop()
    return raw.strip(), " ".join(toks)


def norm_title(raw):
    """Normalize a role title for keying: drop ref/job ids, parentheticals, punctuation."""
    t = re.sub(r"[`_*]", "", raw)
    t = re.sub(r"\([^)]*\)", " ", t)                 # (ref 25000Q6Q), (C09), (Power BI) …
    t = re.sub(r"\bref\b|\bjob id\b", " ", t, flags=re.I)
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def parse_role_line(line):
    """Return (title_display, company_display, company_key, title_key, location) or None."""
    # CURRENT format: "### 12. Title — Company (Location) — Score: 74 — *notes*"
    m = HEADING_RE.match(line)
    if m:
        body = m.group(1)
        lm = LINK_RE.search(body)
        if lm:                                       # unwrap [Title — Company](url)
            body = lm.group(1)
        # The last em-dash segment is "Company (Location)"; everything before is the title.
        parts = re.split(r"\s+[—–]\s+", body)
        if len(parts) < 2:
            # No company separator (e.g. a "…cluster (Tamil Nadu)" aggregate heading).
            return None
        company_raw = parts[-1]
        title_raw = " — ".join(parts[:-1])
        loc = ""
        pm = PAREN_LOC_RE.search(company_raw)
        if pm:
            loc = pm.group(1).strip()
            company_raw = company_raw[:pm.start()].strip()
        comp_disp, comp_key = clean_company(company_raw)
        return title_raw.strip(), comp_disp, comp_key, norm_title(title_raw), loc

    # LEGACY format: "### 83 — Title @ Company"
    m = HEADING_LEGACY_RE.match(line)
    if m:
        rest = m.group(2)
        lm = LINK_RE.search(rest)
        if lm:                                       # unwrap [Title @ Company](url)
            rest = lm.group(1)
        if " @ " not in rest:
            return None                              # e.g. an aggregate "additional reqs" heading
        title_raw, company_raw = rest.rsplit(" @ ", 1)
        comp_disp, comp_key = clean_company(company_raw)
        return title_raw.strip(), comp_disp, comp_key, norm_title(title_raw), ""
    m = TABLE_RE.match(line)
    if m:
        # Skip the markdown table separator / header rows (first cell must be a number).
        title_raw, company_raw, loc_raw = m.group(2), m.group(3), m.group(4)
        comp_disp, comp_key = clean_company(company_raw)
        loc = re.sub(r"[`_*]", "", loc_raw).strip()
        return title_raw.strip(), comp_disp, comp_key, norm_title(title_raw), loc
    return None


def load_roles(runs_dir):
    """Parse every dated brief → ({(company_key,title_key): [occurrence,...]}, dates, blind).

    `blind` lists briefs where role-looking headings exist but few/none parsed —
    the signature of a brief-format change that has blinded this audit. Callers
    MUST surface it: a zero-parse brief reads identically to a clean brief, and
    that false "clean" is exactly how six weeks of duplicates went unnoticed.
    """
    roles = defaultdict(list)
    dates, blind = [], []
    for date, bpath in _runs.iter_briefs(runs_dir):
        dates.append(date)
        roleish = parsed_n = 0
        with open(bpath, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if ROLEISH_RE.match(line):
                    roleish += 1
                parsed = parse_role_line(line)
                if not parsed:
                    continue
                title_disp, comp_disp, comp_key, title_key, loc = parsed
                if not comp_key or not title_key:
                    continue
                parsed_n += 1
                roles[(comp_key, title_key)].append({
                    "date": date, "title": title_disp, "company": comp_disp,
                    "location": loc, "tagged_seen": "seen" in line.lower(),
                })
        # A brief with role-shaped headings that yields <50% parses is a parser
        # regression, not a quiet brief.
        if roleish >= 3 and parsed_n < roleish * 0.5:
            blind.append((date, parsed_n, roleish))
    return roles, dates, blind


def audit_client(client_id, window, latest_only):
    runs_dir = os.path.join(HERE, client_id, "runs")
    if not os.path.isdir(runs_dir):
        print(f"[!] no runs/ dir for {client_id} ({runs_dir})")
        return 0
    roles, dates, blind = load_roles(runs_dir)
    if not dates:
        print(f"[!] no dated briefs in {runs_dir}")
        return 0
    latest = dates[-1]

    # Build one report per repeated role (a role = one company_key+title_key), each
    # with its full surfacing timeline — so a role seen 3× reads as one row, not two.
    violations, resurfaces = [], []
    for occ in roles.values():
        # Collapse same-date repeats (one row per date) and order chronologically.
        by_date = {}
        for o in occ:
            by_date.setdefault(o["date"], o)
        seq = [by_date[d] for d in sorted(by_date)]
        if len(seq) < 2:
            continue
        gaps = [(datetime.strptime(b["date"], "%Y-%m-%d")
                 - datetime.strptime(a["date"], "%Y-%m-%d")).days
                for a, b in zip(seq, seq[1:])]
        last = seq[-1]
        rep = {
            "seq": seq, "gaps": gaps,
            "within": any(g <= window for g in gaps),   # any pair inside the window
            "last_gap": gaps[-1],
            "hits_latest": last["date"] == latest,
            "last_tagged": last["tagged_seen"],
            "locs": sorted({o["location"] for o in seq if o["location"]}),
        }
        if rep["within"]:
            violations.append(rep)
        elif not rep["last_tagged"]:                     # all pairs > window, latest untagged
            resurfaces.append(rep)

    if latest_only:
        violations = [r for r in violations if r["hits_latest"]]
        resurfaces = [r for r in resurfaces if r["hits_latest"]]

    violations.sort(key=lambda r: r["seq"][-1]["date"], reverse=True)
    resurfaces.sort(key=lambda r: r["seq"][-1]["date"], reverse=True)

    print(f"\n=== Dedupe audit: {client_id} ===")
    print(f"Briefs scanned: {len(dates)} ({dates[0]} → {latest})  |  window: {window} days")
    if blind:
        print(f"\n⚠️  PARSER BLIND on {len(blind)} brief(s) — role-shaped headings found but "
              f"not parsed. The brief format has drifted away from this audit; its "
              f"\"clean\" verdict on these dates is MEANINGLESS until the format regexes "
              f"are updated:")
        for d, got, want in blind[-8:]:
            print(f"  • {d}: parsed {got} of ~{want} role headings")

    def show(rep):
        seq, gaps = rep["seq"], rep["gaps"]
        tl = seq[0]["date"] + (" ✓seen" if seq[0]["tagged_seen"] else "")
        for g, o in zip(gaps, seq[1:]):
            tl += f"  →{g}d  " + o["date"] + (" ✓seen" if o["tagged_seen"] else "")
        arrow = "   ← LATEST BRIEF" if rep["hits_latest"] else ""
        locs = f"  [locations: {'; '.join(rep['locs'])}]" if rep["locs"] else ""
        print(f"  • {seq[-1]['company']} — \"{seq[-1]['title']}\"  (surfaced {len(seq)}×){arrow}")
        print(f"      {tl}{locs}")

    if violations:
        print(f"\n🔴 WITHIN-WINDOW DUPLICATES ({len(violations)}) — same role re-surfaced within "
              f"{window}d, should have been deduped:")
        for r in violations:
            show(r)
    if resurfaces:
        print(f"\n🟠 UNTAGGED RESURFACES ({len(resurfaces)}) — allowed (>{window}d apart) "
              f"but the latest listing isn't tagged \"seen <date>\":")
        for r in resurfaces:
            show(r)
    if not violations and not resurfaces:
        print("\n✅ No cross-brief duplicates found.")

    # A resurface reaching the newest brief is allowed but should carry a "seen" tag —
    # warn (don't block); the fix is just adding the tag.
    latest_resurf = [r for r in resurfaces if r["hits_latest"]]
    if latest_resurf:
        print(f"\n[warn] {len(latest_resurf)} untagged resurface(s) in the latest brief "
              f"({latest}) — allowed, but add a \"seen <date>\" tag.")

    # Gate: fail only if a within-window duplicate reached the newest brief untagged
    # (a genuine dedupe miss — a stale repeat delivered as if new).
    latest_hits = [r for r in violations
                   if r["hits_latest"] and r["last_gap"] <= window and not r["last_tagged"]]
    if latest_hits:
        print(f"\n[gate] {len(latest_hits)} within-window duplicate(s) in the latest brief "
              f"({latest}) — exit 1.")
        return 1
    print(f"\n[gate] latest brief ({latest}) clean — exit 0.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Audit a client's briefs for cross-brief duplicate roles.")
    ap.add_argument("client_id", nargs="?", help="client folder under clients/")
    ap.add_argument("--all", action="store_true", help="audit every client in _registry.json")
    ap.add_argument("--window", type=int, default=14, help="dedupe window in days (default 14)")
    ap.add_argument("--latest-only", action="store_true",
                    help="only report repeats involving the newest brief")
    args = ap.parse_args()

    if args.all:
        reg = json.load(open(os.path.join(HERE, "_registry.json"), encoding="utf-8"))
        ids = [c["id"] for c in reg.get("clients", [])]
    elif args.client_id:
        ids = [args.client_id]
    else:
        ap.error("give a <client-id> or --all")

    rc = 0
    for cid in ids:
        rc |= audit_client(cid, args.window, args.latest_only)
    sys.exit(rc)


if __name__ == "__main__":
    main()
