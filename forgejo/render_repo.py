#!/usr/bin/env python3
"""Render a flipped repo's forge workflow from forgejo/repos.yml (HL-508, HL-711).

The forge cannot share a workflow between repos, as render.py explains, so each repo commits its
own copy. A copy written by hand drifts: seven of them checked out the branch tip instead of the
pushed commit, so a later job could publish what an earlier one never tested (HL-711). So the
parts repos share live here once, and repos.yml holds only what differs: a repo's gates, its env
and its deploy target. A key this script does not know fails the render instead of being dropped.

Families:
  service  gates, then semantic-release, the image on a release, a weekly image rebuild, and
           optionally the deploy webhook. With multi_arch, each image is built natively per
           architecture and merged into one manifest, and image_smoke runs against a release's
           manifest on each architecture's own hardware
  package  gates, then semantic-release, which publishes the package
  image    gates, then the repo's own build script, publishing from main: no release and no rebuild
  lint     the fleet repos' loopback-binding check
  verify   gates and nothing after them
  pages    one job that builds a static site, and on main uploads it to the edge and proves it serves

service, image and images push to GHCR, or with `registry: forge` to the forge's registry (HL-696,
HL-843), where only a run on main reads the push token.

A service is written as three files, one per kind of trigger (SERVICE_FILES, HL-798). Forgejo
evaluates a job's `if:` only in a runner (services/actions/job_emitter.go, v15.0.11), so a job whose
condition names another event still held a runner and a cap slot to skip itself: 28% of the forge's
jobs did that. Split by trigger, each file carries only the jobs its events can run, and a job past a
red or skipped need with no `if:` is skipped by the server without a runner. Every file is the same
render, service_workflow(), for its own events; with every event it is the one workflow these
families were written as, which is what tests/forgejo-repo-render reasons about.

Usage:
  render_repo.py --list               # print each "<repo> <workflow file>", one line per file
  render_repo.py --check-forge-script <path>   # exit 1 naming each way a forge build script breaks its contract
  render_repo.py <repo> <file>        # print that workflow
  RENDER_REF=<ref> overrides the ref deploy.yml is read at (default v1).
"""
import os
import re
import subprocess
import sys

import yaml

import schedule
from render import CHECKOUT, GIT_AUTH, RELEASE_CHECKOUT, BlockDumper, RenderError

HERE = os.path.dirname(os.path.abspath(__file__))
SEMANTIC_RELEASE = "node /usr/local/lib/ci-toolchain/semantic-release/forge-release.mjs"
BUILD_IMAGE = ".forgejo/build-image.sh"
# large is a heavy job on the bigger reservation (docs/ci-capacity.md in homelab): arm64 only, and only
# for a repo measured past lean's, as its own label is what routes it to that Nomad job.
CLASSES = ("heavy", "medium", "large")
GATE_REQUIRED = {"class", "timeout", "steps"}
GATE_KEYS = GATE_REQUIRED | {"env"}
FAMILY_KEYS = {
    "service": {"family", "gates", "image_timeout", "image_env", "deploy_url", "multi_arch", "image_smoke", "site",
                "image_arch", "image_on_every_push", "deploy_rebuild", "registry"},
    "package": {"family", "gates", "release_install", "release_env"},
    "image": {"family", "gates", "note", "image_timeout", "image_on_pull_request", "deploy_url", "registry"},
    "images": {"family", "gates", "note", "images", "deploy_url", "registry"},
    "tag-release": {"family", "gates", "note", "release_script", "release_timeout"},
    "lint": {"family"},
    "verify": {"family", "gates", "note"},
    "pages": {"family", "gates", "note", "site_dir", "served"},
}
ALWAYS_PRESENT = {
    "service": {"family", "image_timeout"},
    "package": {"family", "gates"},
    "image": {"family", "gates", "note", "image_timeout"},
    "images": {"family", "gates", "note", "images", "deploy_url"},
    "tag-release": {"family", "gates", "note", "release_script", "release_timeout"},
    "lint": {"family"},
    "verify": {"family", "gates", "note"},
    "pages": {"family", "gates", "note", "site_dir", "served"},
}
# Triggers no workflow may carry (HL-921): each runs a fork's code, or its output, with the repo's secrets.
FORK_UNSAFE_TRIGGERS = ("pull_request_target", "workflow_run")
# A service's files are SERVICE_FILES'.
WORKFLOW_FILE = {
    "package": "ci.yml",
    "image": "ci.yml",
    "images": "ci.yml",
    "tag-release": "ci.yml",
    "lint": "loopback-bindings-lint.yml",
    "verify": "ci.yml",
    "pages": "pages.yml",
}

# A service's events, in the order its single workflow listed them, and the files they are split into.
# A pull request's required check is named "<workflow name> / <job> (<event>)" (the forge's
# protected_branch rows), and every file keeps the name CI/CD, so the split renames no check.
# ci.yml stays the push's file, so the run history a push links to keeps its name.
SERVICE_EVENTS = ("push", "pull_request", "workflow_dispatch", "schedule")
SERVICE_FILES = {
    "ci.yml": ("push",),
    "pull-request.yml": ("pull_request",),
    "rebuild.yml": ("workflow_dispatch", "schedule"),
}

# A pull request's run is the green check Renovate's automerge waits for (HL-130). The lint family
# stays off it on purpose: the srv-* host repos are held to a human's merge by having no check.
GATED_TRIGGERS = {
    "push": {"branches": ["main"]},
    "pull_request": {"branches": ["main"]},
    "workflow_dispatch": {},
}

# GitHub's default for a `run:` step with no `shell:`, which these families' scripts were written
# against: forgejo-runner would add pipefail (render.py's shell-default).
RUN_DEFAULTS = {"run": {"shell": "bash -e {0}"}}
# A pull request's ref is refs/pull/N/head, so a build there cannot publish by mistake. It is no
# boundary: a run takes the workflow from the pull request's head, and only the owner and Renovate push.
ON_MAIN = "github.ref == 'refs/heads/main'"
# A dispatch names any branch, and a rebuild moves :latest and deploys, so it is held to main (HL-747).
# A schedule is not: it only fires on the default branch, and Forgejo gives it the ref bare, `main`,
# so ON_MAIN would silently skip every weekly rebuild.
REBUILD_EVENTS = f"github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && {ON_MAIN})"
TAG_PUSH = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
EDGE = "static-sites@82.112.240.209"
# The pinned checkout is 50 commits deep. VitePress dates a page, and its sitemap entry, from the
# commit that last touched it, so past that depth the boundary commit's date moves with every push.
# A huge depth rather than --unshallow, which refuses a repo that fits in 50 commits.
FULL_HISTORY_STEP = {
    "name": "Fetch the whole history (plain git)",
    "env": {**GIT_AUTH, "REPO_URL": CHECKOUT["env"]["REPO_URL"], "REF": CHECKOUT["env"]["REF"]},
    "run": 'git fetch -q --depth=2147483647 "$REPO_URL" "+$REF:refs/remotes/origin/ref"\n',
}
OPENSCAD_VERSION = "2026.04.26"
# sha256 of OpenSCAD-2026.04.26-WebAssembly-node.zip, read 2026-10-30; bump both together.
OPENSCAD_SHA256 = "cffe65689337f0ed16b3577545c53372ac556fe6e0ab582d481670602e39bfe8"
OPENSCAD_ENV = {"OPENSCAD_VERSION": OPENSCAD_VERSION, "OPENSCAD_SHA256": OPENSCAD_SHA256}
# The forge's runners are arm64 and OpenSCAD ships no aarch64 snapshot, hence the WebAssembly build,
# into the job's own workspace, which the runner discards.
OPENSCAD_STEP = {
    "name": "Install OpenSCAD (WebAssembly)",
    "run": (
        "set -euo pipefail\n"
        'dir="$PWD/.openscad"; mkdir -p "$dir/bin"\n'
        'curl -fsSL --retry 5 --retry-delay 10 -o "$dir/openscad.zip" \\\n'
        '  "https://files.openscad.org/snapshots/OpenSCAD-${OPENSCAD_VERSION}-WebAssembly-node.zip"\n'
        'echo "$OPENSCAD_SHA256  $dir/openscad.zip" | sha256sum -c -\n'
        'python3 -I -m zipfile -e "$dir/openscad.zip" "$dir/x"\n'
        "# openscad.js is CommonJS; without this, node takes the repo's \"type\": \"module\" and fails.\n"
        """printf '{"type":"commonjs"}\\n' > "$dir/x/package.json"\n"""
        """printf '#!/usr/bin/env bash\\nexec node %q "$@"\\n' "$dir/x/openscad.js" > "$dir/bin/openscad"\n"""
        'chmod +x "$dir/bin/openscad"\n'
        "# This build exits 7 after printing --version (a render exits 0, a failed one 1), so the check\n"
        "# is on what it prints, not on its status.\n"
        'v="$("$dir/bin/openscad" --version 2>&1 || true)"; echo "$v"\n'
        '[ "$v" = "OpenSCAD version $OPENSCAD_VERSION" ] || { echo "::error::expected OpenSCAD $OPENSCAD_VERSION"; exit 1; }\n'
    ),
}

HEADER = """\
# Generated by dlepaux/github-workflows forgejo/render_repo.py from forgejo/repos.yml (HL-711).
# Do not edit: change repos.yml or the family, and re-render with forgejo/sync-repos.sh.
# The forge is primary and GitHub a push mirror (homelab HL-508). No job can load a workflow from
# another repo here, as the forge forces every repo private, so each step lives in this file.
"""
SERVICE_NOTE = """\
# A release's image comes from its tag, and the weekly schedule's rebuild republishes :latest on a
# patched base, since a stable service never releases and its base would otherwise never change.
"""
NO_REBUILD_DEPLOY_NOTE = """\
# The rebuild does not deploy: image-update-exporter reports the digest, and the pull stays a decision.
"""
REBUILD_DEPLOY_NOTE = """\
# The rebuild deploys too: the image roller leaves this service to its pipeline, so nothing else would ship it.
"""
# Each architecture builds on its own hardware, since a rootless runner cannot register QEMU (HL-722).
# A leg's Docker config and builder are its own: the runner keeps one daemon across jobs, and
# docker-container is the driver that can push by digest, which each registry's legs do.
IMAGE_PLATFORMS = {"arm64": "linux/arm64", "x64": "linux/amd64"}
GHCR_LOGIN = (
    ': "${GHCR_WRITE_TOKEN:?GHCR_WRITE_TOKEN is not set on this repo (homelab forgejo_actions_repo_secrets)}"\n'
    'DOCKER_CONFIG=$(mktemp -d)\n'
    'export DOCKER_CONFIG\n'
)
GHCR_LOGIN_LINE = """printf '%s' "$GHCR_WRITE_TOKEN" | docker login ghcr.io -u dlepaux --password-stdin\n"""
# The forge registry (homelab HL-696, HL-787). A private repo's images move there one repo at a time
# (`registry: forge`); a public repo's stay on GHCR. FORGE_REGISTRY_TOKEN is owner-wide write:package,
# as Forgejo 15 cannot scope a package token to a repo, so a job that holds it could overwrite any
# image. It is therefore in the env of the push step alone, and empty there unless the run is on
# main: a pull request's, or a branch's, never reads it, whatever the job's own condition says. A
# schedule's ref is the bare `main` (REBUILD_EVENTS), hence both forms.
FORGE_REGISTRY = "forgejo.lepaux.com"
# An allowlist of events, not a denylist: pull_request_target reports the base ref, so a
# `!= 'pull_request'` guard would hand it the token the day a service file triggers on it. A schedule
# takes both ref forms, so a forge that stops sending the bare branch keeps its rebuilds.
FORGE_PUBLISHES = (
    "(github.event_name == 'schedule' && (github.ref == 'main' || github.ref == 'refs/heads/main'))"
    " || ((github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == 'refs/heads/main')"
)
FORGE_TOKEN = f"${{{{ ({FORGE_PUBLISHES}) && secrets.FORGE_REGISTRY_TOKEN || '' }}}}"
FORGE_LOGIN = (
    ': "${FORGE_REGISTRY_TOKEN:?FORGE_REGISTRY_TOKEN is empty: not set on this repo (homelab forgejo_registry_repos), or the run is not on main}"\n'
    ': "${FORGE_REGISTRY_USER:?FORGE_REGISTRY_USER is empty: the repo var homelab forgejo_registry_repos sets is missing}"\n'
    'DOCKER_CONFIG=$(mktemp -d)\n'
    'export DOCKER_CONFIG\n'
)
FORGE_LOGIN_LINE = (
    """printf '%s' "$FORGE_REGISTRY_TOKEN" | docker login forgejo.lepaux.com -u "$FORGE_REGISTRY_USER" --password-stdin\n"""
)
# A forge smoke pulls with the owner's read:package token (homelab `packages-read`, which a repo that
# installs from the forge in CI already carries under this name), never the push token. It is
# owner-wide too, as Forgejo 15 lets no other account read a private user's packages, so it is
# guarded as the push token is.
FORGE_READ_TOKEN = f"${{{{ ({FORGE_PUBLISHES}) && secrets.FORGE_PACKAGE_READ_TOKEN || '' }}}}"
FORGE_READ_LOGIN = (
    ': "${FORGE_PACKAGE_READ_TOKEN:?FORGE_PACKAGE_READ_TOKEN is empty: not set on this repo (homelab forgejo_actions_repo_secrets), or the run is not on main}"\n'
    ': "${FORGE_REGISTRY_USER:?FORGE_REGISTRY_USER is empty: the repo var homelab forgejo_registry_repos sets is missing}"\n'
    'DOCKER_CONFIG=$(mktemp -d)\n'
    'export DOCKER_CONFIG\n'
)
FORGE_READ_LOGIN_LINE = (
    """printf '%s' "$FORGE_PACKAGE_READ_TOKEN" | docker login forgejo.lepaux.com -u "$FORGE_REGISTRY_USER" --password-stdin\n"""
)
REGISTRIES = ("ghcr", "forge")
BUILD_LEG = (
    "set -euo pipefail\n"
    + GHCR_LOGIN +
    'builder="forge-${GITHUB_RUN_ID}-${GITHUB_JOB}"\n'
    """trap 'docker buildx rm --force "$builder" >/dev/null 2>&1 || true; rm -rf "$DOCKER_CONFIG"' EXIT\n"""
    """printf '%s' "$GHCR_WRITE_TOKEN" | docker login ghcr.io -u dlepaux --password-stdin\n"""
    'sha=$(git rev-parse HEAD)\n'
    'args=(--build-arg "GIT_SHA=$sha" --build-arg "VERSION=${VERSION:-sha-${sha:0:7}}"\n'
    '  --build-arg "BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)")\n'
    "# A released image names its version whether or not its Dockerfile labels one.\n"
    'if [ -n "${VERSION:-}" ]; then args+=(--label "org.opencontainers.image.version=$VERSION"); fi\n'
    'docker buildx create --name "$builder" --driver docker-container --bootstrap >/dev/null\n'
    "# --pull: a rebuild exists to take a patched base, so a cached one must not stand in for it.\n"
    'docker buildx build --builder "$builder" --pull --platform "$PLATFORM" \\\n'
    '  --sbom=true --provenance=mode=max "${args[@]}" --metadata-file "$DOCKER_CONFIG/meta.json" \\\n'
    '  --output "type=image,name=$IMAGE,push-by-digest=true,name-canonical=true,push=true" .\n'
    """digest=$(python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$DOCKER_CONFIG/meta.json")\n"""
    '[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "::error::no image digest: $digest"; exit 1; }\n'
    'echo "digest=$digest" >> "$GITHUB_OUTPUT"\n'
)
# X.Y follows the newest release of its minor line, so a host can pin a minor and take its patches.
# The dry run shows the manifest before any tag moves, so one missing an architecture fails here and
# :latest stays where it was. An attestation's entry has platform unknown/unknown, which the check ignores.
MERGE_LEGS = (
    "set -euo pipefail\n"
    'for digest in "$DIGEST_ARM64" "$DIGEST_X64"; do\n'
    '  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "::error::a leg left no digest: \'$digest\'"; exit 1; }\n'
    'done\n'
    + GHCR_LOGIN +
    """trap 'rm -rf "$DOCKER_CONFIG"' EXIT\n"""
    """printf '%s' "$GHCR_WRITE_TOKEN" | docker login ghcr.io -u dlepaux --password-stdin\n"""
    'tags=("--tag=$IMAGE:latest" "--tag=$IMAGE:sha-${GITHUB_SHA:0:7}")\n'
    'if [ -n "${VERSION:-}" ]; then tags+=("--tag=$IMAGE:$VERSION"); fi\n'
    'if [[ "${VERSION:-}" =~ ^([0-9]+)\\.([0-9]+)\\.[0-9]+$ ]]; then\n'
    '  tags+=("--tag=$IMAGE:${BASH_REMATCH[1]}.${BASH_REMATCH[2]}")\n'
    'fi\n'
    'docker buildx imagetools create --dry-run "$IMAGE@$DIGEST_ARM64" "$IMAGE@$DIGEST_X64" | python3 -I -c \'\n'
    'import json, sys\n'
    'seen = {m.get("platform", {}).get("architecture") for m in json.load(sys.stdin)["manifests"]}\n'
    'sys.exit(0 if {"amd64", "arm64"} <= seen else f"manifest has only {sorted(map(str, seen))}")\n'
    "'\n"
    'docker buildx imagetools create "${tags[@]}" "$IMAGE@$DIGEST_ARM64" "$IMAGE@$DIGEST_X64"\n'
)
# The forge's legs and merge. Its registry fails `buildx imagetools create`, the GHCR merge's tool
# (forgejo#780), so each leg pushes one plain manifest and the merge joins the two by digest with
# `docker manifest`. `docker manifest` joins plain manifests only, so a forge image carries no SBOM or
# provenance attestation where a GHCR one does: nothing in the fleet reads them, a trade HL-843 records.
# A leg pushes by digest and under no tag. Forgejo 15 deletes a re-pushed tag's old version with its
# files at once (createPackageAndVersion), and serves a list's child only while some version holds it,
# so a leg tag that a later run of the commit re-pushed would pull the child out from under every
# live list built from it, until that run's merge, or for good if the merge never ran. Untagged, a
# leg is a `sha256:` version: cleanup rules skip it, and the cleanup cron drops it only once no list
# names it (homelab roles/forgejo readme, Registry retention). The leg names Docker's media types
# rather than take the default of the floating BuildKit image its builder runs, so a BuildKit release
# cannot change the type of what it pushes.
FORGE_BUILD_LEG = (
    "set -euo pipefail\n"
    + FORGE_LOGIN +
    'builder="forge-${GITHUB_RUN_ID}-${GITHUB_JOB}"\n'
    """trap 'docker buildx rm --force "$builder" >/dev/null 2>&1 || true; rm -rf "$DOCKER_CONFIG"' EXIT\n"""
    + FORGE_LOGIN_LINE +
    'sha=$(git rev-parse HEAD)\n'
    'args=(--build-arg "GIT_SHA=$sha" --build-arg "VERSION=${VERSION:-sha-${sha:0:7}}"\n'
    '  --build-arg "BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)")\n'
    "# A released image names its version whether or not its Dockerfile labels one.\n"
    'if [ -n "${VERSION:-}" ]; then args+=(--label "org.opencontainers.image.version=$VERSION"); fi\n'
    "# network=host: the builder's own container network does not use the runner's resolver, so it found\n"
    "# the forge by its public record, hairpinned through the CPE and met /v2's LAN-only rule (a 403;\n"
    "# mako-spawner v5.8.6, MK-1472, HL-877). On the host network it reaches the LAN address, as the daemon does.\n"
    "# The trade, declared: with no CNI config in the builder, a Dockerfile's RUN steps share that host network\n"
    "# too, so a build on the runner can reach the runner's own loopback listeners. Only main's pushes and the\n"
    "# schedule run this leg. Narrowing it (a CNI network for RUN, or the LAN resolver handed to buildkitd)\n"
    "# needs a test on a runner, so it is left for its own card.\n"
    'docker buildx create --name "$builder" --driver docker-container --driver-opt network=host --bootstrap >/dev/null\n'
    "# --pull: a rebuild exists to take a patched base, so a cached one must not stand in for it.\n"
    'docker buildx build --builder "$builder" --pull --platform "$PLATFORM" \\\n'
    '  --provenance=false --sbom=false "${args[@]}" --metadata-file "$DOCKER_CONFIG/meta.json" \\\n'
    '  --output "type=image,name=$IMAGE,push-by-digest=true,name-canonical=true,push=true,oci-mediatypes=false" .\n'
    """digest=$(python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$DOCKER_CONFIG/meta.json")\n"""
    '[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "::error::no image digest: $digest"; exit 1; }\n'
    "# What the registry now serves under that digest: one manifest, for this leg's platform.\n"
    """docker manifest inspect --verbose "$IMAGE@$digest" | python3 -I -c '\n"""
    "import json, os, sys\n"
    "d = json.load(sys.stdin)\n"
    "if not isinstance(d, dict):\n"
    '    sys.exit("the leg pushed a manifest list, which docker manifest cannot join")\n'
    'p = d["Descriptor"].get("platform", {})\n'
    'if p.get("os", "") + "/" + p.get("architecture", "") != os.environ["PLATFORM"]:\n'
    '    sys.exit("the leg pushed " + json.dumps(p) + ", not " + os.environ["PLATFORM"])\n'
    "'\n"
    'echo "digest=$digest" >> "$GITHUB_OUTPUT"\n'
)
# Every tag's list is made and checked before any is pushed, and :latest moves last. `create` fetches
# each leg by its digest, so the check before a push proves the platforms: a list that is not one
# arm64 and one amd64 leg moves no tag. --purge drops the local list, so the check after each push
# reads what the registry serves, digests and platforms both, and a push that fails leaves the tags
# not yet pushed, :latest among them, where they were.
FORGE_MERGE_LEGS = (
    "set -euo pipefail\n"
    'for digest in "$DIGEST_ARM64" "$DIGEST_X64"; do\n'
    '  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "::error::a leg left no digest: \'$digest\'"; exit 1; }\n'
    'done\n'
    + FORGE_LOGIN +
    """trap 'rm -rf "$DOCKER_CONFIG"' EXIT\n"""
    + FORGE_LOGIN_LINE +
    "is_the_legs() {\n"
    "  python3 -I -c '\n"
    "import json, os, sys\n"
    'want = sorted([["linux/arm64", os.environ["DIGEST_ARM64"]], ["linux/amd64", os.environ["DIGEST_X64"]]])\n'
    "got = sorted([m.get(\"platform\", {}).get(\"os\", \"\") + \"/\" + m.get(\"platform\", {}).get(\"architecture\", \"\"),\n"
    '              m.get("digest", "")] for m in json.load(sys.stdin).get("manifests", []))\n'
    'sys.exit(0 if got == want else "the list is " + json.dumps(got) + ", not the legs " + json.dumps(want))\n'
    "'\n"
    "}\n"
    'tags=("$IMAGE:sha-${GITHUB_SHA:0:7}")\n'
    'if [ -n "${VERSION:-}" ]; then tags+=("$IMAGE:$VERSION"); fi\n'
    'if [[ "${VERSION:-}" =~ ^([0-9]+)\\.([0-9]+)\\.[0-9]+$ ]]; then\n'
    '  tags+=("$IMAGE:${BASH_REMATCH[1]}.${BASH_REMATCH[2]}")\n'
    'fi\n'
    'tags+=("$IMAGE:latest")\n'
    'for tag in "${tags[@]}"; do\n'
    '  docker manifest create "$tag" "$IMAGE@$DIGEST_ARM64" "$IMAGE@$DIGEST_X64"\n'
    '  docker manifest inspect "$tag" | is_the_legs\n'
    'done\n'
    'for tag in "${tags[@]}"; do\n'
    '  docker manifest push --purge "$tag"\n'
    '  docker manifest inspect "$tag" | is_the_legs\n'
    'done\n'
)
SPLIT_NOTE = """\
# One of three files, one per kind of trigger: ci.yml (a push), pull-request.yml, and rebuild.yml (the
# schedule and a dispatch). Forgejo reads a job's `if:` only in a runner, so a job left to skip itself
# on another event's run would hold one (HL-798).
"""
LINT_NOTE = """\
# Layer 1 of the three-layer auth model: every compose service is loopback-bound or allowlisted.
"""


def runs_on(job_class, arch="arm64"):
    if job_class not in CLASSES:
        raise RenderError(f"unknown job class {job_class}")
    return ["self-hosted", arch, f"ci-{job_class}"]


def checked_keys(name, entry, allowed, required):
    unknown = set(entry) - allowed
    if unknown:
        raise RenderError(f"{name}: keys this render does not carry: {sorted(unknown)}")
    missing = required - set(entry)
    if missing:
        raise RenderError(f"{name}: missing {sorted(missing)}")


def gate_job(repo, gate_id, gate):
    """A gate's steps are the repo's own, with `checkout` standing for the one pinned checkout."""
    checked_keys(f"{repo}/{gate_id}", gate, GATE_KEYS, GATE_REQUIRED)
    if gate["steps"].count("checkout") != 1:
        raise RenderError(f"{repo}/{gate_id}: a gate takes exactly one checkout")
    named = {"checkout": CHECKOUT, "full-history": FULL_HISTORY_STEP, "openscad": OPENSCAD_STEP}
    steps = [named[s] if isinstance(s, str) else s for s in gate["steps"]]
    job = {"runs-on": runs_on(gate["class"]), "timeout-minutes": gate["timeout"]}
    env = {**gate.get("env", {}), **(OPENSCAD_ENV if "openscad" in gate["steps"] else {})}
    if env:
        job["env"] = env
    job["steps"] = steps
    return job


def gate_jobs(repo, entry):
    return {g: gate_job(repo, g, spec) for g, spec in entry.get("gates", {}).items()}


def needing(jobs):
    """`needs` with one job reads as a scalar, as a person writes it."""
    return jobs[0] if len(jobs) == 1 else jobs


def release_job(gates, install=False, release_env=None, version_output=False, push_only=False):
    """`push_only`: the file triggers on a push alone, so the condition is the trigger's already."""
    steps = [RELEASE_CHECKOUT]
    if install:
        # npm publish's prepublishOnly builds, so this job needs the dependencies too. No scripts,
        # as `prepare` would install husky before the release commit, which would then run the hooks.
        steps.append({"name": "Install", "run": "npm ci --no-audit --no-fund --ignore-scripts"})
    steps.append({
        "name": "Run semantic-release (forge)",
        "env": {**GIT_AUTH, "FORGEJO_TOKEN": "${{ github.token }}", **(release_env or {})},
        "run": SEMANTIC_RELEASE,
    })
    job = {"needs": needing(list(gates))} if gates else {}
    if not push_only:
        job["if"] = "github.event_name == 'push'"
    job.update({
        "runs-on": runs_on("medium"),
        "timeout-minutes": 15,
    })
    if version_output:
        job["outputs"] = {"version": "${{ steps.released.outputs.version }}"}
        # A release leaves its tag on HEAD, and a push that releases nothing leaves none.
        steps.append({
            "name": "Read the released version",
            "id": "released",
            "run": (
                "set -eu\n"
                "if tag=$(git describe --exact-match --tags HEAD 2>/dev/null); then\n"
                '  echo "version=${tag#v}" >> "$GITHUB_OUTPUT"\n'
                "fi\n"
            ),
        })
    job["steps"] = steps
    return job


def tag_checkout():
    """The image is built from what the release job tagged, which is past github.sha by the release commit."""
    env = {k: v for k, v in CHECKOUT["env"].items() if k not in ("REF", "SHA")}
    env["REF"] = "refs/tags/v${{ needs.release.outputs.version }}"
    return {
        "name": "Check out the release tag (plain git)",
        "env": env,
        "run": (
            "set -eu\n"
            '[ -z "$(ls -A)" ] || { echo "::error::workspace not empty"; exit 1; }\n'
            "git init -q .\n"
            'git fetch -q --depth=50 "$REPO_URL" "+$REF:refs/remotes/origin/ref"\n'
            "git checkout -q --detach refs/remotes/origin/ref\n"
        ),
    }


def build_step(repo, entry, version):
    env = {**push_env(repo, entry), **entry.get("image_env", {})}
    if version:
        env["VERSION"] = "${{ needs.release.outputs.version }}"
    return {"name": "Build and push", "env": env, "run": BUILD_IMAGE}


def deploy_job(deploy_step_run, url):
    return {
        "needs": "docker",
        "runs-on": runs_on("medium"),
        "timeout-minutes": 30,
        "steps": [{
            "name": "Trigger deploy webhook",
            "shell": "bash",
            "env": {
                "URL": url,
                "KEY": "${{ secrets.WEBHOOK_DEPLOY_KEY }}",
                "RETRIES": "3",
                "DELAY": "30",
                "DEPLOY_TIMEOUT": "900",
            },
            "run": deploy_step_run,
        }],
    }


def registry_of(entry):
    return entry.get("registry", "ghcr")


def push_env(repo, entry):
    """The push credential, and on the forge the image's name, for the one step that pushes."""
    if registry_of(entry) == "ghcr":
        return {"GHCR_WRITE_TOKEN": "${{ secrets.GHCR_WRITE_TOKEN }}"}
    return {
        "IMAGE": f"{FORGE_REGISTRY}/dlepaux/{repo}",
        "FORGE_REGISTRY_USER": "${{ vars.FORGE_REGISTRY_USER }}",
        "FORGE_REGISTRY_TOKEN": FORGE_TOKEN,
    }


# A forge repo's own .forgejo/build-image.sh receives the push step's env: IMAGE (the forge path),
# FORGE_REGISTRY_USER and the owner-wide FORGE_REGISTRY_TOKEN. Its contract: push to "$IMAGE", log in
# with the token on stdin, and hand the token to nothing else, a scanner's pull included (trivy's
# TRIVY_PASSWORD, a `-e` into another container), since that token could overwrite any image. The
# image family's script also reads PUBLISH, and the token is set exactly when PUBLISH is true, so it
# checks the token and logs in past that test only: a pull request's build has neither. An images
# repo's script names its other images from "$IMAGE" (dinle's worker: "$IMAGE-worker").
# sync-repos.sh --check holds each forge repo's script to it.
FORGE_SCRIPT_LOGIN = (
    """printf '%s' "$FORGE_REGISTRY_TOKEN" | docker login forgejo.lepaux.com -u "$FORGE_REGISTRY_USER" --password-stdin"""
)


def forge_script_commands(script):
    """The script's commands as bash splits them at newlines: comments dropped (a `#` that starts a word
    outside quotes, to the end of its line), a backslash-newline joined, and a newline inside quotes
    kept within its command. So a multi-line `docker buildx build` is read as the one command it is,
    and neither `... . # --push` nor a backslash followed by a space passes as a push (HL-881)."""
    commands, cur, quote, i, n = [], [], "", 0, len(script)
    while i < n:
        ch = script[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\" and i + 1 < n:
            if script[i + 1] != "\n":
                cur.append(script[i:i + 2])
            i += 2
            continue
        elif quote == '"':
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (not cur or cur[-1][-1] in " \t;&|()"):
            while i < n and script[i] != "\n":
                i += 1
            continue
        elif ch == "\n":
            commands.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(" " if ch == "\n" else ch)
        i += 1
    commands.append("".join(cur))
    return [c.strip() for c in commands if c.strip()]


def forge_command_pushes(command):
    """Whether one command pushes an image. A `docker buildx build` (or bake) pushes only through
    --push, an --output carrying push=true, or type=registry (which implies it). Without one it only
    builds, which is how a push-by-digest leg goes wrong when push=true is dropped (HL-881)."""
    if re.search(r'docker (image push|push|manifest push|buildx imagetools create)\b', command):
        return True
    if not re.search(r'docker buildx (build|bake)\b', command):
        return False
    return bool(re.search(r'--push\b|(?<![\w-])push=true\b|(?<![\w-])type=registry\b', command))


def forge_script_faults(script):
    """What a forge build script does against that contract, one line per fault; empty if it holds."""
    faults = []
    if "ghcr.io" in script or "GHCR_WRITE_TOKEN" in script:
        faults.append("still names GHCR (ghcr.io or GHCR_WRITE_TOKEN)")
    # A static read, not dataflow: the script must push, and use $IMAGE beyond its `:?` guard. Comments
    # count for neither, and a guard alone once passed as a push (GS-116).
    code = forge_script_commands(script)
    if not any(forge_command_pushes(cmd) for cmd in code):
        faults.append("does not push")
    # An untagged push is reachable only by the digest the build wrote out (MK-1472's legs, HL-881).
    by_digest = [c for c in code if forge_command_pushes(c) and re.search(r'(?<![\w-])push-by-digest=true\b', c)]
    reads_digest = any("containerimage.digest" in c for c in code)
    if by_digest and not (reads_digest and all("--metadata-file" in c for c in by_digest)):
        faults.append("pushes by digest but reads no digest back from --metadata-file")
    if not any(re.search(r'\$\{?IMAGE\b', ln) and not re.fullmatch(r': "\$\{IMAGE:\?[^}$`]*\}"', ln) for ln in code):
        faults.append("does not push to $IMAGE")
    if FORGE_SCRIPT_LOGIN not in script:
        faults.append("does not log in with the token on stdin")
    # After the login the token also sits in $DOCKER_CONFIG/config.json, so a container given that
    # config holds it too. This reads names only: a host tool that finds the exported DOCKER_CONFIG
    # on its own (a host-side `trivy image` after the login) is beyond a static check, which is why
    # the contract puts any scan before the login.
    for cmd in code:
        if re.search(r'docker (run|create)\b', cmd) and re.search(r'DOCKER_CONFIG|config\.json|\.docker\b', cmd):
            faults.append(f"hands the registry login on: {cmd}")
    for line in script.splitlines():
        stripped = line.strip()
        if "FORGE_REGISTRY_TOKEN" not in stripped or stripped == FORGE_SCRIPT_LOGIN:
            continue
        if not re.fullmatch(r': "\$\{FORGE_REGISTRY_TOKEN:\?[^}$`]*\}"', stripped):
            faults.append(f"hands the token on: {stripped}")
    return faults


def multi_arch_jobs(job_id, repo, entry, guard, checkout, version):
    """One leg per architecture, then a job under the plain name that merges their digests, so
    whatever follows `docker` follows the manifest. A failed leg leaves no manifest at all."""
    if "image_env" in entry:
        raise RenderError(f"{repo}: multi_arch carries no image_env, as a leg's build takes only its own args")
    leg_script, merge_script = (BUILD_LEG, MERGE_LEGS) if registry_of(entry) == "ghcr" else (FORGE_BUILD_LEG, FORGE_MERGE_LEGS)
    needs = guard.get("needs", [])
    # On the forge, push_env names the image, over GHCR's.
    env = {"IMAGE": f"ghcr.io/dlepaux/{repo}", **push_env(repo, entry)}
    if version:
        env["VERSION"] = "${{ needs.release.outputs.version }}"
    legs = {
        f"{job_id}-{arch}": {
            **guard,
            "runs-on": runs_on("heavy", arch),
            "timeout-minutes": entry["image_timeout"],
            "outputs": {"digest": "${{ steps.leg.outputs.digest }}"},
            "steps": [checkout, {
                "name": f"Build and push {platform} by digest",
                "id": "leg",
                "env": {**env, "PLATFORM": platform},
                "run": leg_script,
            }],
        }
        for arch, platform in IMAGE_PLATFORMS.items()
    }
    merge_env = {**env, **{f"DIGEST_{a.upper()}": f"${{{{ needs.{job_id}-{a}.outputs.digest }}}}" for a in IMAGE_PLATFORMS}}
    # No condition of its own: it needs both legs, which carry the guard, so past a skipped leg the
    # server skips it, where an `if:` would hold a runner only to skip it there (HL-798).
    merge = {
        "needs": ([needs] if isinstance(needs, str) else needs) + list(legs),
        "runs-on": runs_on("medium"),
        "timeout-minutes": 10,
        "steps": [{"name": "Merge into one manifest", "env": merge_env, "run": merge_script}],
    }
    return {**legs, job_id: merge}


def smoke_jobs(repo, entry):
    """A check of the published image, on each architecture's own hardware: no emulation stands in
    for the binary a host will run. On GHCR it pulls with the push token, the repo's only one there; on
    the forge with the read token (FORGE_READ_TOKEN), as the push token could overwrite any image.
    Either way under its own config. No condition: docker succeeds only on a release, so needing it is
    the guard, and the server skips it past a skipped docker without a runner (HL-798)."""
    if registry_of(entry) == "ghcr":
        image = "ghcr.io/${{ github.repository }}"
        credential = {"GHCR_WRITE_TOKEN": "${{ secrets.GHCR_WRITE_TOKEN }}"}
        login, login_line = GHCR_LOGIN, GHCR_LOGIN_LINE
    else:
        image = f"{FORGE_REGISTRY}/dlepaux/{repo}"
        credential = {"FORGE_REGISTRY_USER": "${{ vars.FORGE_REGISTRY_USER }}", "FORGE_PACKAGE_READ_TOKEN": FORGE_READ_TOKEN}
        login, login_line = FORGE_READ_LOGIN, FORGE_READ_LOGIN_LINE
    return {
        f"smoke-{arch}": {
            "needs": ["release", "docker"],
            "runs-on": runs_on("heavy", arch),
            "timeout-minutes": 10,
            "steps": [{
                "name": f"Smoke {platform}",
                "env": {
                    "IMAGE": image,
                    "VERSION": "${{ needs.release.outputs.version }}",
                    "PLATFORM": platform,
                    **credential,
                },
                "run": (
                    "set -euo pipefail\n"
                    + login +
                    """trap 'rm -rf "$DOCKER_CONFIG"' EXIT\n"""
                    + login_line
                    + entry["image_smoke"]
                ),
            }],
        }
        for arch, platform in IMAGE_PLATFORMS.items()
    }


def every_push_guard(gates, push_only=False):
    """A site whose content lives in its image ships every push, so a commit no release follows is built too.

    `always()` because release is skipped on a schedule or a dispatch, and a skipped need otherwise skips
    this job; the gates are then named, so it still cannot publish past a red one. Named events, as a
    pull request must never publish :latest, and main only, as a dispatch names any branch. A release's
    own commit is the docker job's.

    In a file that triggers on a push alone there is no skipped release to let through, so the job needs
    every gate and the release green, which is the default rule, and only the version is left to read.
    """
    needs = list(gates) + ["release"]
    if push_only:
        return {"needs": needs, "if": "needs.release.outputs.version == ''"}
    green = " && ".join(f"needs.{g}.result == 'success'" for g in gates)
    return {
        "needs": needs,
        "if": (
            f"always() && {green} && ({REBUILD_EVENTS}"
            " || (github.event_name == 'push' && needs.release.result == 'success'"
            " && needs.release.outputs.version == ''))"
        ),
    }


def check_service(repo, entry):
    # image_env is merged after the push env, so a key in both would replace the credential or the image.
    push_keys = set(push_env(repo, {"registry": "ghcr"})) | set(push_env(repo, {"registry": "forge"}))
    clash = sorted(set(entry.get("image_env", {})) & push_keys)
    if clash:
        raise RenderError(f"{repo}: image_env may not set {clash}, the push step's own credential and image")
    if "image_smoke" in entry and not entry.get("multi_arch", False):
        raise RenderError(f"{repo}: image_smoke needs multi_arch, whose legs it checks")
    if entry.get("multi_arch", False) and "image_arch" in entry:
        raise RenderError(f"{repo}: multi_arch builds both arches, image_arch picks the one")
    if entry.get("multi_arch", False) and "image_on_every_push" in entry:
        raise RenderError(f"{repo}: image_on_every_push is for one image, not two legs and a merge")
    # The arch the image runs on: vps-edge is x86_64, and the render's default is the arm64 pool.
    if entry.get("image_arch", "arm64") not in IMAGE_PLATFORMS:
        raise RenderError(f"{repo}: image_arch must be one of {sorted(IMAGE_PLATFORMS)}")
    every_push = entry.get("image_on_every_push", False)
    deploy_rebuild = entry.get("deploy_rebuild", False)
    if deploy_rebuild and "deploy_url" not in entry:
        raise RenderError(f"{repo}: deploy_rebuild needs a deploy_url")
    if every_push and "deploy_url" in entry and not deploy_rebuild:
        raise RenderError(f"{repo}: image_on_every_push needs deploy_rebuild, or a push builds and never ships")


def service_publishers(repo, entry, gates, events):
    """docker, its smoke and rebuild, each only where the file's events can run it: a job whose
    condition rules out every event the file triggers on would only hold a runner to skip itself."""
    push_only = events == {"push"}
    every_push = entry.get("image_on_every_push", False)
    builds_release = "push" in events
    rebuilds = bool(events & {"schedule", "workflow_dispatch"}) or (builds_release and every_push)
    docker_guard = {"needs": "release", "if": "needs.release.outputs.version != ''"}
    # Every gate, as for a release: a rebuild moves :latest too, so it may not publish past a red one.
    rebuild_guard = {"needs": needing(list(gates))} if gates else {}
    # Named, not `!= 'push'`: a pull request must never publish :latest.
    rebuild_guard["if"] = REBUILD_EVENTS
    if every_push and builds_release:
        rebuild_guard = every_push_guard(gates, push_only)
    jobs = {}
    if entry.get("multi_arch", False):
        if builds_release:
            jobs.update(multi_arch_jobs("docker", repo, entry, docker_guard, tag_checkout(), version=True))
            if "image_smoke" in entry:
                jobs.update(smoke_jobs(repo, entry))
        if rebuilds:
            jobs.update(multi_arch_jobs("rebuild", repo, entry, rebuild_guard, CHECKOUT, version=False))
        return jobs
    image_arch = entry.get("image_arch", "arm64")
    timeout = entry["image_timeout"]
    if builds_release:
        jobs["docker"] = {
            **docker_guard,
            "runs-on": runs_on("heavy", image_arch),
            "timeout-minutes": timeout,
            "steps": [tag_checkout(), build_step(repo, entry, version=True)],
        }
    if rebuilds:
        jobs["rebuild"] = {
            **rebuild_guard,
            "runs-on": runs_on("heavy", image_arch),
            "timeout-minutes": timeout,
            "steps": [CHECKOUT, build_step(repo, entry, version=False)],
        }
    return jobs


def service_deploy(entry, jobs, deploy_step_run):
    """The deploy follows whichever builds this file carries, or is absent when it carries none."""
    builders = [j for j in ("docker", "rebuild") if j in jobs and (j == "docker" or entry.get("deploy_rebuild", False))]
    if "deploy_url" not in entry or not builders:
        return None
    deploy = deploy_job(deploy_step_run, entry["deploy_url"])
    if len(builders) == 1:
        # One build, so the default rule (every need green) is the whole condition, and the server
        # skips the job past a skipped build without a runner.
        return {**deploy, "needs": builders[0]}
    # The image roller leaves a webhook-deployed service to its pipeline, so a rebuild no deploy
    # follows never reaches the host. Either build published, and the other was skipped, so the
    # default rule that a skipped need skips the job would never deploy.
    return {
        "needs": builders,
        "if": "always() && (needs.docker.result == 'success' || needs.rebuild.result == 'success')",
        **{k: v for k, v in deploy.items() if k != "needs"},
    }


def service_workflow(repo, entry, deploy_step_run, events=SERVICE_EVENTS):
    """The whole service for `events`. With every event it is the one workflow the tests reason about;
    sync-repos.sh writes it as SERVICE_FILES, each the same render for its own events."""
    check_service(repo, entry)
    events = frozenset(events)
    if not events or events - set(SERVICE_EVENTS):
        raise RenderError(f"{repo}: events {sorted(events)} are not a service's")
    gates = gate_jobs(repo, entry)
    jobs = dict(gates)
    if "push" in events:
        jobs["release"] = release_job(gates, version_output=True, push_only=events == {"push"})
    jobs.update(service_publishers(repo, entry, gates, events))
    deploy = service_deploy(entry, jobs, deploy_step_run)
    if deploy:
        jobs["deploy"] = deploy
    # A pull request never publishes a site, and a push is to main, as its trigger says.
    if "site" in entry and events - {"pull_request"}:
        site = site_job(repo, entry["site"])
        jobs["publish-site"] = {k: v for k, v in site.items() if k != "if"} if events == {"push"} else site
    triggers = {**GATED_TRIGGERS, "schedule": [{"cron": schedule.cron(repo, "1")}]}
    # No `concurrency`, though these repos queued under GitHub: a queued job's runner finds no task and
    # exits, and ci-dispatch never retries it (HL-738). Forgejo's default cancels the older push instead,
    # so the newest commit still wins, and a release a push cancelled heals at the next rebuild's deploy.
    note = REBUILD_DEPLOY_NOTE if entry.get("deploy_rebuild", False) else NO_REBUILD_DEPLOY_NOTE
    on = {e: t for e, t in triggers.items() if e in events}
    return {"name": "CI/CD", "on": on, "jobs": jobs}, SERVICE_NOTE + note + (SPLIT_NOTE if len(events) < len(SERVICE_EVENTS) else "")


def package_workflow(repo, entry):
    gates = gate_jobs(repo, entry)
    jobs = dict(gates)
    jobs["release"] = release_job(
        gates, install=entry.get("release_install", False), release_env=entry.get("release_env"))
    return {"name": "CI/CD", "on": GATED_TRIGGERS, "jobs": jobs}, ""


def image_workflow(repo, entry, deploy_step_run):
    """A pull request may build, so a review covers what will ship, but never holds the push token.
    On the forge, PUBLISH is FORGE_PUBLISHES itself, which this family's triggers make the same runs as
    GHCR's condition, so the token is set exactly when the script may push."""
    gates = gate_jobs(repo, entry)
    job = {"needs": needing(list(gates))}
    if not entry.get("image_on_pull_request", False):
        job["if"] = "github.event_name != 'pull_request'"
    if registry_of(entry) == "ghcr":
        publishes = f"{ON_MAIN} && github.event_name != 'pull_request'"
        env = {
            "PUBLISH": f"${{{{ {publishes} }}}}",
            "GHCR_WRITE_TOKEN": "${{ github.event_name != 'pull_request' && secrets.GHCR_WRITE_TOKEN || '' }}",
        }
    else:
        publishes = FORGE_PUBLISHES
        env = {"PUBLISH": f"${{{{ {publishes} }}}}", **push_env(repo, entry)}
    job.update({
        "runs-on": runs_on("heavy"),
        "timeout-minutes": entry["image_timeout"],
        "steps": [CHECKOUT, {"name": "Build, and publish from main", "env": env, "run": BUILD_IMAGE}],
    })
    jobs = {**gates, "image": job}
    if "deploy_url" in entry:
        # The same condition as PUBLISH: what the image job did not publish must not be deployed.
        jobs["deploy"] = {
            **deploy_job(deploy_step_run, entry["deploy_url"]),
            "needs": "image",
            "if": publishes,
        }
    workflow = {"name": "CI", "on": GATED_TRIGGERS, "defaults": RUN_DEFAULTS, "jobs": jobs}
    return workflow, note_comment(entry["note"])


def images_workflow(repo, entry, deploy_step_run):
    """A release ships several images, each on its own architecture's hardware, and deploys after all of
    them. `always()` lets a hand dispatch past the release it skips; only a release or a dispatch builds,
    since a build moves a `latest` that something else may be pulling."""
    gates = gate_jobs(repo, entry)
    release = release_job(gates, version_output=True)
    green = " && ".join(f"needs.{g}.result == 'success'" for g in gates)
    version = "needs.release.outputs.version"
    guard = {
        "needs": list(gates) + ["release"],
        "if": (
            f"always() && {ON_MAIN} && {green} && (github.event_name == 'workflow_dispatch'"
            f" || (github.event_name == 'push' && needs.release.result == 'success' && {version} != ''))"
        ),
    }
    images = {}
    for name, image in entry["images"].items():
        checked_keys(f"{repo}/{name}", image, {"arch", "timeout"}, {"arch", "timeout"})
        images[f"{name}-image"] = {
            **guard,
            "runs-on": runs_on("heavy", image["arch"]),
            "timeout-minutes": image["timeout"],
            "steps": [
                {**CHECKOUT, "if": f"{version} == ''"},
                {**tag_checkout(), "if": f"{version} != ''"},
                {**build_step(repo, entry, True), "run": f"{BUILD_IMAGE} {name}"},
            ],
        }
    deploy = {
        **deploy_job(deploy_step_run, entry["deploy_url"]),
        "needs": list(images),
        "if": "always() && " + " && ".join(f"needs.{j}.result == 'success'" for j in images),
    }
    jobs = {**gates, "release": release, **images, "deploy": deploy}
    workflow = {"name": "CI", "on": GATED_TRIGGERS, "defaults": RUN_DEFAULTS, "jobs": jobs}
    return workflow, note_comment(entry["note"])


def tag_release_workflow(repo, entry):
    """A person tags the version, and the repo's own script publishes it, past green gates."""
    gates = gate_jobs(repo, entry)
    release = {
        "needs": needing(list(gates)),
        "if": TAG_PUSH,
        "runs-on": runs_on("heavy"),
        "timeout-minutes": entry["release_timeout"],
        "steps": [CHECKOUT, {
            "name": "Build and publish",
            "env": {"FORGEJO_TOKEN": "${{ github.token }}"},
            "run": entry["release_script"],
        }],
    }
    on = {**GATED_TRIGGERS, "push": {"branches": ["main"], "tags": ["v*"]}}
    workflow = {"name": "CI", "on": on, "defaults": RUN_DEFAULTS, "jobs": {**gates, "release": release}}
    return workflow, note_comment(entry["note"])


def publish_step(site_dir):
    return {
        "name": "Publish",
        "if": ON_MAIN,
        "env": {
            "DEPLOY_KEY": "${{ secrets.STATIC_SITE_DEPLOY_KEY }}",
            "KNOWN_HOSTS": "${{ secrets.STATIC_SITE_KNOWN_HOSTS }}",
        },
        "run": (
            "set -euo pipefail\n"
            '[ -n "${DEPLOY_KEY:-}" ] && [ -n "${KNOWN_HOSTS:-}" ] || {\n'
            '  echo "::error::STATIC_SITE_DEPLOY_KEY or STATIC_SITE_KNOWN_HOSTS is not set (homelab static_sites, converge srv-apps)"; exit 1; }\n'
            'dir="$(mktemp -d)"; trap \'rm -rf "$dir"\' EXIT\n'
            "# printf is a builtin: the key never appears in a process's argv.\n"
            """printf '%s\\n' "$DEPLOY_KEY" > "$dir/key"; chmod 600 "$dir/key"\n"""
            """printf '%s\\n' "$KNOWN_HOSTS" > "$dir/known_hosts"\n"""
            "# The receiver picks the site from the key, so the remote command is ignored; none is sent.\n"
            f'tar -cz -C {site_dir} . | ssh -T -i "$dir/key" -o IdentitiesOnly=yes -o BatchMode=yes \\\n'
            '  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$dir/known_hosts" \\\n'
            f"  {EDGE}\n"
        ),
    }


def site_job(repo, site):
    """A service's docs: its own job, so an edge that refuses the upload never holds a release back."""
    checked_keys(f"{repo}/site", site, GATE_KEYS | {"dir", "served"}, GATE_REQUIRED | {"dir", "served"})
    job = gate_job(repo, "site", {k: v for k, v in site.items() if k in GATE_KEYS})
    job["steps"] = job["steps"] + [publish_step(site["dir"]), {**site["served"], "if": ON_MAIN}]
    return {"if": ON_MAIN, **job}


def note_comment(note):
    return "".join(f"# {line}\n" if line else "#\n" for line in note.splitlines())


def verify_workflow(repo, entry):
    workflow = {"name": "CI", "on": GATED_TRIGGERS, "defaults": RUN_DEFAULTS, "jobs": gate_jobs(repo, entry)}
    return workflow, note_comment(entry["note"])


def pages_workflow(repo, entry):
    jobs = gate_jobs(repo, entry)
    if list(jobs) != ["publish"]:
        raise RenderError(f"{repo}: a site has the one job, publish, as its build and upload share a workspace")
    jobs["publish"]["steps"] = jobs["publish"]["steps"] + [
        publish_step(entry["site_dir"]), {**entry["served"], "if": ON_MAIN}]
    workflow = {"name": "Pages", "on": GATED_TRIGGERS, "defaults": RUN_DEFAULTS, "jobs": jobs}
    return workflow, note_comment(entry["note"])


def lint_workflow():
    workflow = {
        "name": "loopback-bindings-lint",
        "on": {
            "push": {
                "branches": ["main"],
                "paths": [
                    "docker-compose*.yml",
                    "loopback-bind.allowlist",
                    "scripts/check-loopback-bindings.sh",
                    ".forgejo/workflows/loopback-bindings-lint.yml",
                ],
            },
            "workflow_dispatch": {},
        },
        "jobs": {
            "lint": {
                "name": "check-loopback-bindings",
                "runs-on": runs_on("medium"),
                "timeout-minutes": 2,
                "steps": [CHECKOUT, {
                    "name": "Run loopback-binding lint",
                    "env": {"NO_COLOR": "1"},
                    "run": "bash scripts/check-loopback-bindings.sh",
                }],
            },
        },
    }
    return workflow, LINT_NOTE


def render_repo(repo, entry, deploy_step_run, events=SERVICE_EVENTS):
    """One workflow. `events` scopes a service's (see SERVICE_FILES); the other families have one file."""
    family = entry.get("family")
    if family not in FAMILY_KEYS:
        raise RenderError(f"{repo}: unknown family {family}")
    checked_keys(repo, entry, FAMILY_KEYS[family], ALWAYS_PRESENT[family])
    # Only the families whose FAMILY_KEYS carry `registry` get this far with one.
    if registry_of(entry) not in REGISTRIES:
        raise RenderError(f"{repo}: registry must be one of {list(REGISTRIES)}")
    if family != "service" and tuple(events) != SERVICE_EVENTS:
        raise RenderError(f"{repo}: only a service is split by event")
    if family == "service":
        workflow, note = service_workflow(repo, entry, deploy_step_run, events)
    elif family == "package":
        workflow, note = package_workflow(repo, entry)
    elif family == "image":
        workflow, note = image_workflow(repo, entry, deploy_step_run)
    elif family == "images":
        workflow, note = images_workflow(repo, entry, deploy_step_run)
    elif family == "tag-release":
        workflow, note = tag_release_workflow(repo, entry)
    elif family == "verify":
        workflow, note = verify_workflow(repo, entry)
    elif family == "pages":
        workflow, note = pages_workflow(repo, entry)
    else:
        workflow, note = lint_workflow()
    # HL-921: agent scopes push to forks of their own, and a fork's pull request runs the fork's code.
    # pull_request runs it without the repo's secrets; these two run it, or what it left, with them.
    unsafe = sorted(set(workflow["on"]) & set(FORK_UNSAFE_TRIGGERS))
    if unsafe:
        raise RenderError(f"{repo}: {', '.join(unsafe)} would hand a fork's pull request the repo's secrets")
    # Forgejo 15 mails a run only when its workflow opts in, and the forge is these repos' only CI.
    workflow = {"enable-email-notifications": True, **workflow}
    return HEADER + note + yaml.dump(workflow, Dumper=BlockDumper, sort_keys=False, width=1000, allow_unicode=True)


def files_of(entry):
    family = entry.get("family")
    if family == "service":
        return list(SERVICE_FILES)
    if family not in WORKFLOW_FILE:
        raise RenderError(f"unknown family {family}")
    return [WORKFLOW_FILE[family]]


def render_file(repo, entry, name, deploy_step_run):
    """The file sync-repos.sh writes as `name`."""
    if name not in files_of(entry):
        raise RenderError(f"{repo}: renders {files_of(entry)}, not {name}")
    if entry["family"] == "service":
        return render_repo(repo, entry, deploy_step_run, SERVICE_FILES[name])
    return render_repo(repo, entry, deploy_step_run)


def deploy_step_source(ref):
    """The webhook step is deploy.yml's own, so tests/deploy-step's cases still describe what runs."""
    text = subprocess.run(
        ["git", "-C", HERE, "show", f"{ref}:.github/workflows/deploy.yml"],
        check=True, capture_output=True, text=True,
    ).stdout
    jobs = yaml.safe_load(text)["jobs"]
    steps = [s for job in jobs.values() for s in job["steps"] if s.get("name") == "Trigger deploy webhook"]
    if len(steps) != 1:
        raise RenderError("deploy.yml no longer has one Trigger deploy webhook step")
    return steps[0]["run"]


def load_table():
    with open(os.path.join(HERE, "repos.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def main(argv):
    table = load_table()
    if argv[1:] == ["--list"]:
        try:
            for repo, entry in table.items():
                for name in files_of(entry):
                    print(repo, name)
        except RenderError as e:
            print(f"render_repo.py: {e}", file=sys.stderr)
            return 1
        return 0
    if len(argv) == 3 and argv[1] == "--check-forge-script":
        with open(argv[2], encoding="utf-8") as f:
            faults = forge_script_faults(f.read())
        for fault in faults:
            print(f"forge build script {argv[2]}: {fault}")
        return 1 if faults else 0
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[1] not in table:
        print(f"render_repo.py: {argv[1]} is not in repos.yml", file=sys.stderr)
        return 2
    try:
        run = deploy_step_source(os.environ.get("RENDER_REF", "v1"))
        sys.stdout.write(render_file(argv[1], table[argv[1]], argv[2], run))
    except (RenderError, subprocess.CalledProcessError) as e:
        print(f"render_repo.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
