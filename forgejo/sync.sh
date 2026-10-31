#!/usr/bin/env bash
# Render every crate in crates.tsv into <clones>/<repo>/.forgejo/workflows/ci.yml (HL-508).
#   sync.sh <clones-dir>          write the files
#   sync.sh --check <clones-dir>  exit 1 if any file differs from its render
# Re-run after any change to .github/workflows/rust-crate.yml: the shadow must run what GitHub runs.
# A row's mode picks the render; a row with no mode passes an empty one, which render.py refuses.
# Every crate runs nightly at 00:30 UTC, on one line: ci-dispatch's in-flight cap of one already
# takes them one at a time, so a stagger adds nothing (spec §1 Triggers). The night is when
# GitHub's heavy queue is emptiest, and nine crates finish well inside the forge's 6 h bound.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

check=false
if [ "${1:-}" = --check ]; then check=true; shift; fi
[ $# -eq 1 ] && [ -d "$1" ] || { echo "usage: sync.sh [--check] <clones-dir>" >&2; exit 2; }
clones=$1

NIGHTLY='30 0 * * *'

stale=0
while IFS=$'\t' read -r repo crate floor cli mode; do
  case "$repo" in ''|'#'*) continue ;; esac
  dir="$clones/$repo"
  [ -d "$dir/.git" ] || [ -f "$dir/.git" ] || { echo "sync.sh: $dir is not a clone" >&2; exit 1; }
  want="$(python3 "$HERE/render.py" "$crate" "$floor" "$cli" "$NIGHTLY" "$mode")"
  target="$dir/.forgejo/workflows/ci.yml"
  if $check; then
    if [ "$(cat "$target" 2>/dev/null)" != "$want" ]; then echo "stale: $target"; stale=1; fi
  else
    mkdir -p "$(dirname "$target")"
    printf '%s\n' "$want" > "$target"
    echo "wrote $target"
  fi
done < "$HERE/crates.tsv"
exit "$stale"
