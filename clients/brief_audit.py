#!/usr/bin/env python3
"""
The Morning Five — brief quality gate.

`check_links.py` asks "is this posting still alive?".  `dedupe_audit.py` asks
"did we already show this role?".  Neither asks the question that actually
decides whether a brief is USEFUL: *is this still a shortlist?*

Between 2026-07-29 and 2026-09-01 a real client brief grew 3 -> 38 roles
while genuinely-new leads held at ~3/run, because every unapplied role was
merged forward untouched ("nothing dropped, nothing re-scored"). Both existing
gates passed green the whole way. This script is the missing gate. It enforces,
mechanically, the rules that until now lived only as prose in ENGINE.md:

  1. SIZE      — Tier A <= 3, Tier B <= 5. A shortlist he acts on beats an
                 inventory he ignores (job-hunt/DAILY_RUN.md, mental model).
  2. STALENESS — no Tier A/B role carried unapplied for more than --expire days.
                 Age is measured over the CURRENT carry streak, so a role that
                 drops out and returns as a fresh repost is not punished for its
                 first sighting months earlier. Aged-out roles belong in
                 runs/standing-picks.md, not the brief.
  3. SCORE     — a role carried forward must be re-scored; an unchanged score on
                 an aged role means the rubric's freshness dimension (weight 10)
                 was never recomputed.
  4. VERIFIED  — no Tier A/B role carrying a "JD unverified" marker. With no JD
                 there are no must-haves, so the rubric's Evidence gate cannot
                 have run: the role is tiered on title keywords alone.
  5. FOLLOW-THROUGH — days since the last logged application. Sourcing lead #39
                 while 38 sit unapplied is negative-value work; past the
                 threshold the engine owes a recovery brief, not a sweep.

Zero third-party dependencies (stdlib only). Parsing helpers are imported from
dedupe_audit so the two gates can never disagree about what a role heading is.

Usage:
  python clients/brief_audit.py <client-id>              # gate the latest brief
  python clients/brief_audit.py --all                    # every active client
  python clients/brief_audit.py <client-id> --date 2026-09-01
  python clients/brief_audit.py <client-id> --phase0     # pre-sweep go/no-go only
  python clients/brief_audit.py --all --json             # machine-readable

Exit code: 0 if the brief passes, 1 on any FAIL. Warnings never fail the gate.
In --phase0 mode: exit 0 = sweep normally, 1 = recovery brief required.
"""

import argparse
import json
import os
import re
import sys
import _runs
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dedupe_audit as da  # noqa: E402  (parsing helpers — single source of truth)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# --- Policy defaults (override per-run with flags; keep in sync with ENGINE.md) ---
MAX_TIER_A = 3          # Tier A roles in the delivered brief
MAX_TIER_B = 5          # Tier B roles in the delivered brief
EXPIRE_DAYS = 21        # a Tier A/B role older than this must leave the brief
DECAY_DAYS = 14         # past this, a carried role must show a decayed score
STALE_APPLY_DAYS = 10   # no application in this long -> recovery brief
DEDUPE_WINDOW = 14      # a carry gap longer than this resets staleness (matches dedupe_audit)

# Tier section headings: "## 🟢 Tier A — Apply today (75–100)"
TIER_RE = re.compile(r"^#{2}\s+\S*\s*Tier\s+([ABC])\b", re.I)
# A role heading also carries its score; reuse dedupe_audit's CURRENT-format regex.
SCORE_RE = da.HEADING_RE
# Markers that say the JD was never actually read.
UNVERIFIED_RE = re.compile(r"JD unverified|unverified JD|low-confidence|not enriched", re.I)
# applied-log row: | 2026-07-24 ... | (first cell a date). Ignores the seed/example row.
LOG_DATE_RE = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})")
# The first real http(s) markdown link in a role's block = its apply URL.
APPLY_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")


def _today():
    return date.today()


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def brief_dates(runs_dir):
    """Every dated brief, oldest first. Reads runs/YYYY-MM/ and legacy flat runs/."""
    return _runs.brief_dates(runs_dir)


def parse_brief(path):
    """Return [{tier,title,company,key,score,location,unverified,apply,line}] for one brief.

    A role's details (apply link, "JD unverified" flag, edge notes) live on the lines
    BELOW its heading, so each role absorbs the block that follows it up to the next
    role heading or tier heading.
    """
    roles, tier, cur = [], None, None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            tm = TIER_RE.match(line)
            if tm:
                tier = tm.group(1).upper()
                cur = None
                continue
            parsed = da.parse_role_line(line)
            if parsed and parsed[2] and parsed[3]:
                title, comp_disp, comp_key, title_key, loc = parsed
                sm = SCORE_RE.match(line)
                cur = {
                    "tier": tier,
                    "title": title,
                    "company": comp_disp,
                    "key": (comp_key, title_key),
                    "score": int(sm.group(2)) if sm else None,
                    "location": loc,
                    "unverified": bool(UNVERIFIED_RE.search(line)),
                    "apply": None,
                    "line": line,
                }
                lm = APPLY_RE.search(line)
                if lm:
                    cur["apply"] = lm.group(1)
                roles.append(cur)
                continue
            if cur is not None:
                # Body line belonging to the current role.
                if line.startswith("#"):
                    cur = None
                    continue
                if cur["apply"] is None:
                    lm = APPLY_RE.search(line)
                    if lm:
                        cur["apply"] = lm.group(1)
                if UNVERIFIED_RE.search(line):
                    cur["unverified"] = True
    return roles


def first_surface(runs_dir, upto, gap=DEDUPE_WINDOW):
    """{role_key: date the CURRENT carry streak began} for briefs on/before `upto`.

    Staleness measures how long a role has been *continuously carried unapplied*, not how
    long ago it was ever first seen. A role that surfaced once, dropped out for months, and
    came back as a genuinely new posting is fresh — treating it as N-months-stale would
    penalise exactly the re-opened requisitions worth acting on. So any gap longer than the
    dedupe window resets the streak (the same threshold the dedupe rule uses to decide a
    resurface is legitimately new).
    """
    appearances = {}
    for d in brief_dates(runs_dir):
        if d > upto:
            break
        for r in parse_brief(_runs.brief_path(runs_dir, d)):
            appearances.setdefault(r["key"], []).append(d)
    starts = {}
    for key, dates in appearances.items():
        start = dates[0]
        for prev, cur in zip(dates, dates[1:]):
            if (_d(cur) - _d(prev)).days > gap:
                start = cur            # streak broken — this is a fresh resurface
        starts[key] = start
    return starts


def score_history(runs_dir, upto):
    """{role_key: [(date, score), ...]} — to detect a carried-but-never-re-scored role."""
    hist = {}
    for d in brief_dates(runs_dir):
        if d > upto:
            break
        for r in parse_brief(_runs.brief_path(runs_dir, d)):
            if r["score"] is not None:
                hist.setdefault(r["key"], []).append((d, r["score"]))
    return hist


def last_application(client_dir):
    """(date, count) of the most recent real row in applied-log.md, or (None, 0)."""
    path = os.path.join(client_dir, "runs", "applied-log.md")
    if not os.path.isfile(path):
        return None, 0
    dates = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = LOG_DATE_RE.match(line)
            if m:
                dates.append(m.group(1))
    return (max(dates), len(dates)) if dates else (None, 0)


def audit(client_id, brief_date=None, phase0=False, caps=(MAX_TIER_A, MAX_TIER_B),
          expire=EXPIRE_DAYS, decay=DECAY_DAYS, stale_apply=STALE_APPLY_DAYS,
          as_json=False):
    client_dir = os.path.join(HERE, client_id)
    runs_dir = os.path.join(client_dir, "runs")
    if not os.path.isdir(runs_dir):
        print(f"[!] no runs/ dir for {client_id}")
        return 0

    fails, warns = [], []
    last_apply, n_apply = last_application(client_dir)
    days_since_apply = (_today() - _d(last_apply)).days if last_apply else None

    # --- Phase 0: go / no-go on sweeping at all -----------------------------
    if phase0:
        print(f"\n=== Phase 0 follow-through check: {client_id} ===")
        if last_apply is None:
            print("  no applications ever logged — sweep, but the brief must lead with "
                  "the follow-through ask.")
            return 0
        print(f"  applications logged: {n_apply}  |  last: {last_apply} "
              f"({days_since_apply}d ago)")
        if days_since_apply >= stale_apply:
            print(f"\n  🛑 NO-GO — {days_since_apply} days since the last application "
                  f"(threshold {stale_apply}).")
            print("     Skip the sweep. Deliver a RECOVERY brief instead: the single "
                  "highest-scoring standing pick, why it is still open, and the one "
                  "outreach message to send today. Sourcing more leads onto an "
                  "unactioned pile lowers precision and spends budget for no outcome.")
            return 1
        print(f"\n  ✅ GO — last application {days_since_apply}d ago, under the "
              f"{stale_apply}d threshold. Sweep normally.")
        return 0

    # --- Load the brief under audit ----------------------------------------
    dates = brief_dates(runs_dir)
    if not dates:
        print(f"[!] no dated briefs for {client_id}")
        return 0
    bdate = brief_date or dates[-1]
    path = _runs.brief_path(runs_dir, bdate)
    if not path or not os.path.isfile(path):
        print(f"[!] no brief at {path}")
        return 0

    roles = parse_brief(path)
    firsts = first_surface(runs_dir, bdate)
    hist = score_history(runs_dir, bdate)
    bd = _d(bdate)

    tier_a = [r for r in roles if r["tier"] == "A"]
    tier_b = [r for r in roles if r["tier"] == "B"]
    tier_c = [r for r in roles if r["tier"] == "C"]
    ab = tier_a + tier_b

    print(f"\n=== Brief audit: {client_id} — {bdate} ===")
    print(f"Roles surfaced: {len(roles)}  (A:{len(tier_a)}  B:{len(tier_b)}  C:{len(tier_c)})")

    # 1. SIZE
    if len(tier_a) > caps[0]:
        fails.append(f"SIZE — Tier A has {len(tier_a)} roles, cap is {caps[0]}. "
                     f"Keep the top {caps[0]}; move the rest to runs/standing-picks.md.")
    if len(tier_b) > caps[1]:
        fails.append(f"SIZE — Tier B has {len(tier_b)} roles, cap is {caps[1]}. "
                     f"Keep the top {caps[1]}; move the rest to runs/standing-picks.md.")
    if len(roles) > 15:
        warns.append(f"SIZE — {len(roles)} total roles in the brief. This is an inventory, "
                     f"not a shortlist; the client reads the top 5 and ignores the tail.")

    # 2. STALENESS + 3. SCORE DECAY
    for r in ab:
        first = firsts.get(r["key"], bdate)
        age = (bd - _d(first)).days
        if age > expire:
            fails.append(f"STALE — \"{r['title']}\" @ {r['company']} (Tier {r['tier']}) first "
                         f"surfaced {first}, {age}d ago (expiry {expire}d) and is still "
                         f"unapplied. Expire it to standing-picks.md with a one-line note.")
        elif age > decay:
            seq = hist.get(r["key"], [])
            scores = {s for _, s in seq}
            if r["score"] is not None and len(scores) == 1 and len(seq) > 1:
                fails.append(f"SCORE — \"{r['title']}\" @ {r['company']} has held score "
                             f"{r['score']} across {len(seq)} briefs over {age}d. Freshness "
                             f"(rubric dim 6, weight 10) was never recomputed — apply the "
                             f"carry-forward decay before tiering.")
            else:
                warns.append(f"AGING — \"{r['title']}\" @ {r['company']} is {age}d old "
                             f"(decay starts {decay}d, expires {expire}d).")

    # 4. VERIFIED
    for r in ab:
        if r["unverified"]:
            fails.append(f"UNVERIFIED — \"{r['title']}\" @ {r['company']} is Tier {r['tier']} "
                         f"but flagged JD-unverified. With no JD the Evidence gate cannot "
                         f"have run; hard-cap it at Tier C until the JD is read.")

    # 4b. DIRECT LINK — "Links are mandatory and must be DIRECT" (DAILY_RUN.md Phase 5).
    #     A role the client has to go re-search on a board is friction on the exact step
    #     that is already the pipeline's bottleneck.
    no_link = [r for r in ab if not r["apply"]]
    if no_link:
        warns.append(f"LINK — {len(no_link)} of {len(ab)} Tier A/B roles carry no direct apply URL "
                     f"(client must re-search the board): "
                     + "; ".join(f"\"{r['title']}\" @ {r['company']}" for r in no_link[:4])
                     + ("; …" if len(no_link) > 4 else ""))

    # 5. FOLLOW-THROUGH
    if days_since_apply is None:
        warns.append("FOLLOW-THROUGH — applied-log.md has no real rows; the rubric can "
                     "never be calibrated (ENGINE.md Phase 3 cannot run).")
    elif days_since_apply >= stale_apply:
        fails.append(f"FOLLOW-THROUGH — {days_since_apply} days since the last logged "
                     f"application ({last_apply}), with {len(ab)} Tier A/B roles open. "
                     f"The bottleneck is applying, not sourcing — run --phase0 and deliver "
                     f"a recovery brief.")

    # --- Report -------------------------------------------------------------
    if as_json:
        print(json.dumps({"client": client_id, "date": bdate,
                          "counts": {"A": len(tier_a), "B": len(tier_b), "C": len(tier_c)},
                          "days_since_apply": days_since_apply,
                          "fails": fails, "warns": warns}, indent=2))
    else:
        if fails:
            print(f"\n🔴 FAIL ({len(fails)}):")
            for f in fails:
                print(f"  • {f}")
        if warns:
            print(f"\n🟠 WARN ({len(warns)}):")
            for w in warns:
                print(f"  • {w}")
        if not fails and not warns:
            print("\n✅ Brief is a shortlist: within caps, all picks fresh, all JDs verified.")

    if fails:
        print(f"\n[gate] {len(fails)} failure(s) — exit 1. Fix the brief before delivering.")
        return 1
    print("\n[gate] brief passes — exit 0.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Gate a client's brief on shortlist quality.")
    ap.add_argument("client_id", nargs="?", help="client folder under clients/")
    ap.add_argument("--all", action="store_true", help="audit every client in _registry.json")
    ap.add_argument("--date", help="brief date YYYY-MM-DD (default: latest)")
    ap.add_argument("--phase0", action="store_true",
                    help="pre-sweep go/no-go on follow-through only")
    ap.add_argument("--max-a", type=int, default=MAX_TIER_A)
    ap.add_argument("--max-b", type=int, default=MAX_TIER_B)
    ap.add_argument("--expire", type=int, default=EXPIRE_DAYS)
    ap.add_argument("--decay", type=int, default=DECAY_DAYS)
    ap.add_argument("--stale-apply", type=int, default=STALE_APPLY_DAYS)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if args.all:
        reg = json.load(open(os.path.join(HERE, "_registry.json"), encoding="utf-8"))
        ids = [c["id"] for c in reg.get("clients", []) if c.get("status") in ("active", "trial")]
    elif args.client_id:
        ids = [args.client_id]
    else:
        ap.error("give a <client-id> or --all")

    rc = 0
    for cid in ids:
        rc |= audit(cid, args.date, args.phase0, (args.max_a, args.max_b),
                    args.expire, args.decay, args.stale_apply, args.json)
    sys.exit(rc)


if __name__ == "__main__":
    main()
