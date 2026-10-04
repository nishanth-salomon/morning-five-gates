#!/usr/bin/env python3
"""Shared brief-file resolver for the Morning Five engine.

WHY THIS EXISTS (added 2026-09-10)
----------------------------------
Briefs used to live flat in `clients/<id>/runs/YYYY-MM-DD.md`. After ~3 months and
~55 briefs per client that folder became unreadable, so runs are now filed by month:

    clients/<id>/runs/
        2026-06/  2026-06-15.md  2026-06-15.html  ...
        2026-07/  ...
        2026-08/  ...
        2026-09/  2026-09-10.md  2026-09-10.html
        applied-log.md          <- living file, NEVER moved
        standing-picks.md       <- living file, NEVER moved
        README.md               <- living file, NEVER moved

Every gate (`check_links`, `dedupe_audit`, `brief_audit`, `deliver`) discovered briefs
with its own `os.listdir(runs)` / `runs.glob("*.md")` call, all of which assume the flat
layout. Moving the files without this module would have silently broken all four —
and `dedupe_audit` in particular fails *quietly* when it can't find briefs (it prints
"no dated briefs" and returns 0, i.e. a PASS). That is the same class of silent-green
failure as the mid-July parser regression documented in ENGINE.md Phase 1.6.

So: one resolver, used by all four scripts, that reads BOTH layouts.

BACKWARD COMPATIBILITY IS DELIBERATE
------------------------------------
`iter_briefs()` scans the runs/ root *and* every `YYYY-MM/` subfolder. A brief left flat
still gets found and still gates correctly. Nothing has to be migrated in lockstep, and a
half-migrated folder is not a broken folder.

Duplicate dates (same date flat AND in a month folder) resolve to the month-folder copy,
which is the canonical location going forward.
"""

import os
import re

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# Living files that live at the runs/ root and are never filed into a month folder.
LIVING_FILES = {"applied-log.md", "standing-picks.md", "README.md"}


def month_of(date: str) -> str:
    """'2026-09-10' -> '2026-09'."""
    return date[:7]


def iter_briefs(runs_dir):
    """[(date, abspath), ...] for every dated .md brief, oldest first.

    Scans the runs/ root (legacy flat layout) and every YYYY-MM/ subfolder (current
    layout). If a date exists in both, the month-folder copy wins.
    """
    found = {}
    if not os.path.isdir(runs_dir):
        return []

    # Legacy flat briefs at the root.
    for name in os.listdir(runs_dir):
        if name in LIVING_FILES or not name.endswith(".md"):
            continue
        stem = name[:-3]
        if DATE_RE.match(stem):
            found[stem] = os.path.join(runs_dir, name)

    # Current layout: YYYY-MM/ subfolders (these win over a flat duplicate).
    for entry in os.listdir(runs_dir):
        sub = os.path.join(runs_dir, entry)
        if not os.path.isdir(sub) or not MONTH_RE.match(entry):
            continue
        for name in os.listdir(sub):
            if not name.endswith(".md"):
                continue
            stem = name[:-3]
            if DATE_RE.match(stem):
                found[stem] = os.path.join(sub, name)

    return sorted(found.items())


def brief_dates(runs_dir):
    """['2026-06-15', ...] oldest first."""
    return [d for d, _ in iter_briefs(runs_dir)]


def brief_path(runs_dir, date):
    """Absolute path to one dated brief, or None. Month folder wins over flat."""
    monthly = os.path.join(runs_dir, month_of(date), f"{date}.md")
    if os.path.isfile(monthly):
        return monthly
    flat = os.path.join(runs_dir, f"{date}.md")
    if os.path.isfile(flat):
        return flat
    return None


def latest_brief(runs_dir):
    """(date, path) of the newest dated brief, or (None, None)."""
    briefs = iter_briefs(runs_dir)
    return briefs[-1] if briefs else (None, None)


def target_path(runs_dir, date, ext="md", create=True):
    """Where a NEW brief for `date` should be written: runs/YYYY-MM/YYYY-MM-DD.<ext>.

    This is the write-side counterpart to brief_path(). Use it when generating a brief
    or rendering its HTML so new files land in the right month folder automatically.
    """
    mdir = os.path.join(runs_dir, month_of(date))
    if create:
        os.makedirs(mdir, exist_ok=True)
    return os.path.join(mdir, f"{date}.{ext}")
