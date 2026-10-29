#!/usr/bin/env bash
# Runs the real "Minor version tag" step of both native multi-arch workflows (self-hosted and
# GitHub-hosted), its `run:` block extracted from each file at test time, one case per version,
# and checks that the version reaches both the tags and the image's OCI version label.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass=0 fail=0
# case <version> <expected X.Y tag, empty for none>
case_() {
  local version=$1 want=$2 out="$WORK/out" got
  : > "$out"
  VERSION=$version GITHUB_OUTPUT=$out bash -eo pipefail "$WORK/step.sh" > /dev/null
  got=$(sed -n 's/^tag=//p' "$out")
  if [ "$got" = "$want" ]; then pass=$((pass + 1)); else
    fail=$((fail + 1)); echo "FAIL $workflow '$version': tag=$got, want '$want'"; fi
}

for workflow in docker-build-push-native.yml docker-build-push-github-native.yml; do
  python3 - "$ROOT/.github/workflows/$workflow" "$WORK/step.sh" <<'PY'
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1]))
meta = lambda job: next(s for s in wf["jobs"][job]["steps"] if s.get("id") == "meta")["with"]
# metadata-action derives the version label from the first tag, and the per-arch build carries
# no tags at all, so without an explicit label every image claimed its branch as its version.
label = "format('org.opencontainers.image.version={0}', inputs.version)"
assert label in meta("build")["labels"], f"{sys.argv[1]}: the per-arch image is labelled with its version"
steps = wf["jobs"]["merge"]["steps"]
names = [s.get("id") for s in steps]
assert names.index("minor") < names.index("meta"), f"{sys.argv[1]}: the minor tag is computed before the tags"
assert "steps.minor.outputs.tag" in meta("merge")["tags"], f"{sys.argv[1]}: the manifest is tagged X.Y"
step = steps[names.index("minor")]
assert set(step["env"]) == {"VERSION"}, step["env"]
assert "${{" not in step["run"], "the script must read env only"
open(sys.argv[2], "w").write(step["run"])
PY

  case_ 1.2.3        1.2
  case_ 0.2.0        0.2
  case_ 10.20.30     10.20
  # A prerelease is not the newest of its minor line, so it does not move X.Y.
  case_ 1.3.0-beta.1 ''
  case_ 1.2.3+build  ''
  # No version (a caller that publishes latest and sha only): no X.Y tag.
  case_ ''           ''
done

echo "native-version-tags: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
