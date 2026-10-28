#!/usr/bin/env bash
# Render every crate in crates.tsv into <clones>/<repo>/.forgejo/workflows/ci.yml (HL-508).
#   sync.sh <clones-dir>          write the files
#   sync.sh --check <clones-dir>  exit 1 if any file differs from its render
# Re-run after any change to .github/workflows/rust-crate.yml: the shadow must run what GitHub runs.
# The nightly schedules are staggered 40 minutes apart from 00:30 UTC, because the shadow has one
# heavy slot and eleven back-to-back crates would hold it for hours (spec §1 Triggers).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

check=false
if [ "${1:-}" = --check ]; then check=true; shift; fi
[ $# -eq 1 ] && [ -d "$1" ] || { echo "usage: sync.sh [--check] <clones-dir>" >&2; exit 2; }
clones=$1

cron_for() {  # <index> -> "M H * * *"
  local minutes=$((30 + 40 * $1))
  printf '%d %d * * *' $((minutes % 60)) $((minutes / 60))
}

i=0 stale=0
while IFS=$'\t' read -r repo crate floor cli; do
  case "$repo" in ''|'#'*) continue ;; esac
  [ "$i" -lt 36 ] || { echo "sync.sh: more crates than a day of 40-minute slots" >&2; exit 1; }
  dir="$clones/$repo"
  [ -d "$dir/.git" ] || [ -f "$dir/.git" ] || { echo "sync.sh: $dir is not a clone" >&2; exit 1; }
  want="$(python3 "$HERE/render.py" "$crate" "$floor" "$cli" "$(cron_for "$i")")"
  target="$dir/.forgejo/workflows/ci.yml"
  if $check; then
    if [ "$(cat "$target" 2>/dev/null)" != "$want" ]; then echo "stale: $target"; stale=1; fi
  else
    mkdir -p "$(dirname "$target")"
    printf '%s\n' "$want" > "$target"
    echo "wrote $target"
  fi
  i=$((i + 1))
done < "$HERE/crates.tsv"
exit "$stale"
