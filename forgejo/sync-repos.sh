#!/usr/bin/env bash
# Render every repo in repos.yml into <clones>/<repo>/.forgejo/workflows/ (HL-711).
#   sync-repos.sh <clones-dir>          write the files
#   sync-repos.sh --check <clones-dir>  exit 1 if any file differs from its render, or a build script it names is missing
# Re-run after any change to repos.yml or render_repo.py, and after deploy.yml's webhook step moves v1.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

check=false
if [ "${1:-}" = --check ]; then check=true; shift; fi
[ $# -eq 1 ] && [ -d "$1" ] || { echo "usage: sync-repos.sh [--check] <clones-dir>" >&2; exit 2; }
clones=$1

stale=0
list="$(python3 "$HERE/render_repo.py" --list)"
while read -r repo workflow; do
  dir="$clones/$repo"
  [ -d "$dir/.git" ] || [ -f "$dir/.git" ] || { echo "sync-repos.sh: $dir is not a clone" >&2; exit 1; }
  want="$(python3 "$HERE/render_repo.py" "$repo" "$workflow")"
  target="$dir/.forgejo/workflows/$workflow"
  if $check; then
    if [ "$(cat "$target" 2>/dev/null)" != "$want" ]; then echo "stale: $target"; stale=1; fi
    # The render names this script without carrying it. Without it a release tags a version and then
    # fails to build its image, and nothing short of the first release would show that.
    if grep -q '\.forgejo/build-image\.sh' <<< "$want" && [ ! -x "$dir/.forgejo/build-image.sh" ]; then
      echo "no build script: $dir/.forgejo/build-image.sh"; stale=1
    fi
  else
    mkdir -p "$(dirname "$target")"
    printf '%s\n' "$want" > "$target"
    echo "wrote $target"
  fi
done <<< "$list"
exit "$stale"
