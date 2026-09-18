#!/usr/bin/env bash
# HL-153: runs the real "Decide whether QEMU must be registered" step of
# .github/workflows/docker-build-push-buildx.yml — its `run:` block, extracted from the file at
# test time — against a fake binfmt_misc directory, one scenario per case.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 - "$ROOT/.github/workflows/docker-build-push-buildx.yml" "$WORK/step.sh" <<'PY'
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1]))
step = next(s for s in wf["jobs"]["docker"]["steps"] if s.get("id") == "qemu")
assert set(step["env"]) == {"PLATFORMS", "BINFMT_DIR"}, step["env"]
assert "${{" not in step["run"], "the script must read env only"
open(sys.argv[2], "w").write(step["run"])
PY

pass=0 fail=0
# case <name> <runner arch> <platforms> <binfmt entry: none|enabled|disabled> <expected setup>
case_() {
  local name=$1 arch=$2 platforms=$3 entry=$4 want=$5 dir out got
  dir="$WORK/$name"; out="$dir/out"; mkdir -p "$dir/binfmt"; : > "$out"
  local interp=qemu-aarch64; [ "$arch" = ARM64 ] && interp=qemu-x86_64
  case "$entry" in
    enabled)  printf 'enabled\ninterpreter /usr/libexec/qemu-binfmt/x\nflags: POCF\n' > "$dir/binfmt/$interp" ;;
    disabled) printf 'disabled\ninterpreter /usr/libexec/qemu-binfmt/x\nflags: POCF\n' > "$dir/binfmt/$interp" ;;
  esac
  RUNNER_ARCH=$arch PLATFORMS=$platforms BINFMT_DIR="$dir/binfmt" GITHUB_OUTPUT=$out \
    bash -eo pipefail "$WORK/step.sh" > /dev/null
  got=$(sed -n 's/^setup=//p' "$out")
  if [ "$got" = "$want" ]; then pass=$((pass + 1)); else
    fail=$((fail + 1)); echo "FAIL $name: setup=$got, want $want"; fi
}

# Native single-arch builds never emulate — the pre-HL-153 behaviour, unchanged.
case_ native-arm64     ARM64 linux/arm64             none     false
case_ native-amd64     X64   linux/amd64             none     false
# Cross-compiled arm64 image on an x64 fleet runner: the host's interpreter is used.
case_ x64-host-binfmt  X64   linux/arm64             enabled  false
# Same leg on a host without it (a GitHub-hosted runner): register as before.
case_ x64-no-binfmt    X64   linux/arm64             none     true
case_ x64-disabled     X64   linux/arm64             disabled true
# Multi-arch lists are matched per entry, not by substring.
case_ multi-host       X64   linux/amd64,linux/arm64 enabled  false
case_ multi-no-binfmt  X64   linux/amd64,linux/arm64 none     true
case_ arm-multi        ARM64 linux/arm64,linux/amd64 none     true
case_ arm-host-binfmt  ARM64 linux/amd64             enabled  false

echo "qemu-decision: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
