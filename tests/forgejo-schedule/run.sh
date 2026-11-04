#!/usr/bin/env bash
# HL-798: forgejo/schedule.py gives each repo's scheduled run its own minute. These cases hold what
# it is for: the same repo gets the same minute in every process, every minute lands inside the
# window, the window sits after the backup lanes and before Mako's schedules, and names spread.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import os, subprocess, sys
root = sys.argv[1]
sys.path.insert(0, f"{root}/forgejo")
import schedule as s

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def refuses(name, *args):
    try:
        s.cron(*args)
    except ValueError:
        check(name, True)
    else:
        check(name, False)


names = [f"repo-{i}" for i in range(3000)] + ["katha", "cargo-boundary", "dlepaux.github.io", "A_b.c-1"]
minutes = [s.minute_of(n) for n in names]
end = s.WINDOW_START + s.WINDOW_MINUTES
check("every minute is inside the window", all(s.WINDOW_START <= m < end for m in minutes))
check("names reach every minute of the window", set(minutes) == set(range(s.WINDOW_START, end)))
# Spread, not merely reach: no minute holds more than twice its share of 3,000 names.
share = len(minutes) / s.WINDOW_MINUTES
check("no minute takes more than twice its share", max(minutes.count(m) for m in set(minutes)) < 2 * share)

# The window's edges, as schedule.py's docstring derives them (UTC): the monthly mako-full dump
# ends by 04:17 in winter, and Mako's own schedules start at 05:17.
check("the window opens after the last backup lane ends", s.WINDOW_START >= 4 * 60 + 18)
check("the window closes before Mako's schedules", end <= 5 * 60 + 17)

# Pinned: a change of hash or window moves every repo's schedule, so it must be seen here.
check("katha's weekly line", s.cron("katha", "1") == "54 4 * * 1")
check("cargo-boundary's nightly line", s.cron("cargo-boundary", "*") == "6 5 * * *")
def said(line):
    minute, hour, *rest = line.split(" ")
    return int(hour) * 60 + int(minute), rest


check("each cron line says its minute, daily",
      all(said(s.cron(n, "*")) == (s.minute_of(n), ["*", "*", "*"]) for n in names))
check("the day field is passed through", s.cron("katha", "*").endswith(" * * *") and s.cron("katha", "0").endswith(" * * 0"))

# The same minute in every process: str's hash() is salted per process, which would move them all.
outs = {
    subprocess.run([sys.executable, f"{root}/forgejo/schedule.py", "katha", "1"], text=True, capture_output=True,
                   env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip()
    for seed in ("0", "1", "12345")
}
check("the same line under every hash seed", outs == {"54 4 * * 1"})

refuses("a range of days", "katha", "1-5")
refuses("no such day", "katha", "7")
refuses("an empty name", "", "*")
refuses("a path for a name", "../katha", "*")
refuses("a space in a name", "a b", "*")
bad = subprocess.run([sys.executable, f"{root}/forgejo/schedule.py", "katha"], capture_output=True)
check("the CLI refuses a missing field", bad.returncode == 2)
bad = subprocess.run([sys.executable, f"{root}/forgejo/schedule.py", "katha", "1,3"], capture_output=True, text=True)
check("the CLI refuses a bad field and says why", bad.returncode == 2 and "day-of-week" in bad.stderr)

print(f"schedule: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
PY
