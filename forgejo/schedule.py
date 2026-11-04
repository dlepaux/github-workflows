#!/usr/bin/env python3
"""Each repo's scheduled run gets its own minute in one window, derived from its name (HL-798).

One shared line made the forge's scheduled runs land together: the weekly rebuild put 14 repos on
Monday 04:00:55 and its last jobs waited 22 min behind the cap, and the nine crates ran at 00:30
all at once every night. A minute from a hash of the name spreads them, and stays the same for a
repo from render to render, so a re-render never moves a schedule that nothing else changed.

The window is 04:30 to 05:14 UTC. Forgejo's cron is UTC and the backup hosts keep Europe/Zurich,
so every backup slot moves an hour across DST. The latest of them, read in homelab's backup roles
and srv-* host_vars on 2026-11-04, is the monthly mako-full dump at 04:05 local on the 1st, which
ran 72 min and so ends by 04:17 UTC in winter and 03:17 in summer. The Monday lifecycle prune
(04:30 local) is done by 03:39 UTC in winter, the off-site copy (04:10 local) in under a minute.
The window ends before Mako's own run of schedules, 05:17 to 05:57 UTC.

Usage:
  schedule.py <repo> <day-of-week field>    # print the cron line, e.g. `schedule.py katha 1`
"""
import hashlib
import re
import sys

WINDOW_START = 4 * 60 + 30  # minutes past 00:00 UTC
WINDOW_MINUTES = 45
REPO_NAME = re.compile(r"[A-Za-z0-9._-]+")
# A cron day-of-week field this render writes: every day, or a single day.
DAYS = re.compile(r"\*|[0-6]")


def minute_of(repo):
    """sha256, not hash(): Python salts str hashes per process, which would move every schedule."""
    if not REPO_NAME.fullmatch(repo):
        raise ValueError(f"not a repo name: {repo!r}")
    digest = hashlib.sha256(repo.encode("utf-8")).digest()
    return WINDOW_START + int.from_bytes(digest[:8], "big") % WINDOW_MINUTES


def cron(repo, days):
    if not DAYS.fullmatch(days):
        raise ValueError(f"not a day-of-week field this render writes: {days!r}")
    at = minute_of(repo)
    return f"{at % 60} {at // 60} * * {days}"


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        print(cron(argv[1], argv[2]))
    except ValueError as e:
        print(f"schedule.py: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
