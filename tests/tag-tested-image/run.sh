#!/usr/bin/env bash
# Runs the real "Tag the tested image" step of .github/workflows/docker-build-push-buildx.yml,
# its `run:` block extracted from the file at test time, against a registry of its own. With a
# test-script the build pushes the image by digest and this step is all that tags it, so a
# mistake here publishes nothing, or the wrong image, on every caller that tests.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
REGISTRY="tag-tested-image-$$"
trap 'docker rm -f "$REGISTRY" > /dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

python3 - "$ROOT/.github/workflows/docker-build-push-buildx.yml" "$WORK/step.sh" <<'PY'
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1]))
steps = wf["jobs"]["docker"]["steps"]
names = [s.get("name") for s in steps]
build, test, tag = (names.index(n) for n in ("Build and push", "Test the image before it is tagged", "Tag the tested image"))
assert build < test < tag, "the image is tested after it is built and before it is tagged"
for step in (steps[test], steps[tag]):
    assert step["if"] == "inputs.test-script != ''", "a caller with no test-script must not run this"
assert set(steps[tag]["env"]) == {"IMAGE", "TAGS"}, steps[tag]["env"]
assert "${{" not in steps[tag]["run"], "the script must read env only"
open(sys.argv[2], "w").write(steps[tag]["run"])
PY

# 127.0.0.1 is a registry buildx talks plain HTTP to; the port is the kernel's pick.
docker run -d --name "$REGISTRY" -p 127.0.0.1::5000 registry:3 > /dev/null
port="$(docker port "$REGISTRY" 5000/tcp | sed 's/.*://')"
base="http://127.0.0.1:$port" image="127.0.0.1:$port/owner/image"
for _ in $(seq 50); do curl -fs "$base/v2/" > /dev/null && break; sleep 0.1; done

tested="$(python3 "$HERE/registry.py" seed "$base" owner/image)"
holds() { python3 "$HERE/registry.py" holds "$base" owner/image "$1"; }
[ "$(holds "$tested")" = "$tested" ] && [ "$(holds latest)" != "$tested" ] \
  || { echo "FAIL: the registry was not seeded"; exit 1; }

# What metadata-action hands over: one tag per line. The blank line is a caller with no version.
IMAGE="$image@$tested" TAGS="$image:latest
$image:sha-0123abc

$image:1.2.3" bash -eo pipefail "$WORK/step.sh" > "$WORK/out" 2>&1 \
  || { echo "FAIL: the step exited $?"; sed 's/^/     | /' "$WORK/out"; exit 1; }

fail=0
for tag in latest sha-0123abc 1.2.3; do
  got="$(holds "$tag")"
  [ "$got" = "$tested" ] || { echo "FAIL $tag: holds $got, want the tested image $tested"; fail=1; }
done
tags="$(curl -fs "$base/v2/owner/image/tags/list" | python3 -c 'import json,sys; print(" ".join(sorted(json.load(sys.stdin)["tags"])))')"
[ "$tags" = "1.2.3 latest sha-0123abc" ] || { echo "FAIL: the repository's tags are: $tags"; fail=1; }
[ "$fail" -eq 0 ] && echo "tag-tested-image: latest, the sha and the version hold the tested image ($tested)"
exit "$fail"
