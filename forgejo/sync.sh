#!/usr/bin/env bash
# Render every crate in crates.tsv into <clones>/<repo>/.forgejo/workflows/ (HL-508).
#   sync.sh <clones-dir>          write the files
#   sync.sh --check <clones-dir>  exit 1 if any file differs from its render
# Re-run after any change to .github/workflows/rust-crate.yml: the shadow must run what GitHub runs.
# A row's mode picks the render; a row with no mode passes an empty one, which render.py refuses.
# Each crate's nightly has its own minute from its name, in the window schedule.py documents: on one
# line the nine of them queued together behind the heavy class's cap (HL-798).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

check=false
if [ "${1:-}" = --check ]; then check=true; shift; fi
[ $# -eq 1 ] && [ -d "$1" ] || { echo "usage: sync.sh [--check] <clones-dir>" >&2; exit 2; }
clones=$1

stale=0
while IFS=$'\t' read -r repo crate floor cli mode; do
  case "$repo" in ''|'#'*) continue ;; esac
  dir="$clones/$repo"
  [ -d "$dir/.git" ] || [ -f "$dir/.git" ] || { echo "sync.sh: $dir is not a clone" >&2; exit 1; }
  nightly="$(python3 "$HERE/schedule.py" "$repo" '*')"
  files="$(python3 "$HERE/render.py" --files "$mode")" || { echo "sync.sh: $repo: bad mode '$mode'" >&2; exit 1; }
  for workflow in $files; do
    want="$(python3 "$HERE/render.py" "$crate" "$floor" "$cli" "$nightly" "$mode" "$workflow")"
    target="$dir/.forgejo/workflows/$workflow"
    if $check; then
      if [ "$(cat "$target" 2>/dev/null)" != "$want" ]; then echo "stale: $target"; stale=1; fi
    else
      mkdir -p "$(dirname "$target")"
      printf '%s\n' "$want" > "$target"
      echo "wrote $target"
    fi
  done
done < "$HERE/crates.tsv"
exit "$stale"
