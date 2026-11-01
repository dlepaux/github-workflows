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

Usage:
  render_repo.py --list        # print each "<repo> <workflow file>"
  render_repo.py <repo>        # print its workflow
  RENDER_REF=<ref> overrides the ref deploy.yml is read at (default v1).
"""
import os
import subprocess
import sys

import yaml

from render import CHECKOUT, GIT_AUTH, RELEASE_CHECKOUT, BlockDumper, RenderError

HERE = os.path.dirname(os.path.abspath(__file__))
SEMANTIC_RELEASE = "node /usr/local/lib/ci-toolchain/semantic-release/forge-release.mjs"
BUILD_IMAGE = ".forgejo/build-image.sh"
CLASSES = ("heavy", "medium")
GATE_REQUIRED = {"class", "timeout", "steps"}
GATE_KEYS = GATE_REQUIRED | {"env"}
FAMILY_KEYS = {
    "service": {"family", "gates", "image_timeout", "image_env", "deploy_url", "multi_arch", "image_smoke", "site",
                "image_arch", "image_on_every_push", "deploy_rebuild"},
    "package": {"family", "gates", "release_install", "release_env"},
    "image": {"family", "gates", "note", "image_timeout", "image_on_pull_request", "deploy_url"},
    "images": {"family", "gates", "note", "images", "deploy_url"},
    "lint": {"family"},
    "verify": {"family", "gates", "note"},
    "pages": {"family", "gates", "note", "site_dir", "served"},
}
ALWAYS_PRESENT = {
    "service": {"family", "image_timeout"},
    "package": {"family", "gates"},
    "image": {"family", "gates", "note", "image_timeout"},
    "images": {"family", "gates", "note", "images", "deploy_url"},
    "lint": {"family"},
    "verify": {"family", "gates", "note"},
    "pages": {"family", "gates", "note", "site_dir", "served"},
}
WORKFLOW_FILE = {
    "service": "ci.yml",
    "package": "ci.yml",
    "image": "ci.yml",
    "images": "ci.yml",
    "lint": "loopback-bindings-lint.yml",
    "verify": "ci.yml",
    "pages": "pages.yml",
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
# docker-container is the driver that can push by digest with attestations.
IMAGE_PLATFORMS = {"arm64": "linux/arm64", "x64": "linux/amd64"}
GHCR_LOGIN = (
    ': "${GHCR_WRITE_TOKEN:?GHCR_WRITE_TOKEN is not set on this repo (homelab forgejo_actions_repo_secrets)}"\n'
    'DOCKER_CONFIG=$(mktemp -d)\n'
    'export DOCKER_CONFIG\n'
)
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


def release_job(gates, install=False, release_env=None, version_output=False):
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
    job.update({
        "if": "github.event_name == 'push'",
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


def build_step(entry, version):
    env = {"GHCR_WRITE_TOKEN": "${{ secrets.GHCR_WRITE_TOKEN }}", **entry.get("image_env", {})}
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


def multi_arch_jobs(job_id, repo, entry, guard, checkout, version):
    """One leg per architecture, then a job under the plain name that merges their digests, so
    whatever follows `docker` follows the manifest. A failed leg leaves no manifest at all."""
    if "image_env" in entry:
        raise RenderError(f"{repo}: multi_arch carries no image_env, as a leg's build takes only its own args")
    needs = guard.get("needs", [])
    env = {"IMAGE": f"ghcr.io/dlepaux/{repo}", "GHCR_WRITE_TOKEN": "${{ secrets.GHCR_WRITE_TOKEN }}"}
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
                "run": BUILD_LEG,
            }],
        }
        for arch, platform in IMAGE_PLATFORMS.items()
    }
    merge_env = {**env, **{f"DIGEST_{a.upper()}": f"${{{{ needs.{job_id}-{a}.outputs.digest }}}}" for a in IMAGE_PLATFORMS}}
    merge = {
        "needs": ([needs] if isinstance(needs, str) else needs) + list(legs),
        "if": guard["if"],
        "runs-on": runs_on("medium"),
        "timeout-minutes": 10,
        "steps": [{"name": "Merge into one manifest", "env": merge_env, "run": MERGE_LEGS}],
    }
    return {**legs, job_id: merge}


def smoke_jobs(entry, docker_guard):
    """A check of the published image, on each architecture's own hardware: no emulation stands in
    for the binary a host will run. It uses the token to pull, the repo's only one, under its own config."""
    return {
        f"smoke-{arch}": {
            "needs": ["release", "docker"],
            "if": docker_guard["if"],
            "runs-on": runs_on("heavy", arch),
            "timeout-minutes": 10,
            "steps": [{
                "name": f"Smoke {platform}",
                "env": {
                    "IMAGE": "ghcr.io/${{ github.repository }}",
                    "VERSION": "${{ needs.release.outputs.version }}",
                    "PLATFORM": platform,
                    "GHCR_WRITE_TOKEN": "${{ secrets.GHCR_WRITE_TOKEN }}",
                },
                "run": (
                    "set -euo pipefail\n"
                    + GHCR_LOGIN +
                    """trap 'rm -rf "$DOCKER_CONFIG"' EXIT\n"""
                    """printf '%s' "$GHCR_WRITE_TOKEN" | docker login ghcr.io -u dlepaux --password-stdin\n"""
                    + entry["image_smoke"]
                ),
            }],
        }
        for arch, platform in IMAGE_PLATFORMS.items()
    }


def every_push_guard(gates):
    """A site whose content lives in its image ships every push, so a commit no release follows is built too.

    `always()` because release is skipped on a schedule or a dispatch, and a skipped need otherwise skips
    this job; the gates are then named, so it still cannot publish past a red one. Named events, as a
    pull request must never publish :latest. A release's own commit is the docker job's.
    """
    green = " && ".join(f"needs.{g}.result == 'success'" for g in gates)
    return {
        "needs": list(gates) + ["release"],
        "if": (
            f"always() && {green} && (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
            " || (github.event_name == 'push' && needs.release.result == 'success'"
            " && needs.release.outputs.version == ''))"
        ),
    }


def service_workflow(repo, entry, deploy_step_run):
    gates = gate_jobs(repo, entry)
    timeout = entry["image_timeout"]
    jobs = dict(gates)
    jobs["release"] = release_job(gates, version_output=True)
    docker_guard = {"needs": "release", "if": "needs.release.outputs.version != ''"}
    # Every gate, as for a release: a rebuild moves :latest too, so it may not publish past a red one.
    rebuild_guard = {"needs": needing(list(gates))} if gates else {}
    # Named, not `!= 'push'`: a pull request must never publish :latest.
    rebuild_guard["if"] = "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
    if "image_smoke" in entry and not entry.get("multi_arch", False):
        raise RenderError(f"{repo}: image_smoke needs multi_arch, whose legs it checks")
    if entry.get("multi_arch", False) and "image_arch" in entry:
        raise RenderError(f"{repo}: multi_arch builds both arches, image_arch picks the one")
    if entry.get("multi_arch", False) and "image_on_every_push" in entry:
        raise RenderError(f"{repo}: image_on_every_push is for one image, not two legs and a merge")
    # The arch the image runs on: vps-edge is x86_64, and the render's default is the arm64 pool.
    image_arch = entry.get("image_arch", "arm64")
    if image_arch not in IMAGE_PLATFORMS:
        raise RenderError(f"{repo}: image_arch must be one of {sorted(IMAGE_PLATFORMS)}")
    every_push = entry.get("image_on_every_push", False)
    deploy_rebuild = entry.get("deploy_rebuild", False)
    if deploy_rebuild and "deploy_url" not in entry:
        raise RenderError(f"{repo}: deploy_rebuild needs a deploy_url")
    if every_push and "deploy_url" in entry and not deploy_rebuild:
        raise RenderError(f"{repo}: image_on_every_push needs deploy_rebuild, or a push builds and never ships")
    if every_push:
        rebuild_guard = every_push_guard(gates)
    if entry.get("multi_arch", False):
        jobs.update(multi_arch_jobs("docker", repo, entry, docker_guard, tag_checkout(), version=True))
        if "image_smoke" in entry:
            jobs.update(smoke_jobs(entry, docker_guard))
        jobs.update(multi_arch_jobs("rebuild", repo, entry, rebuild_guard, CHECKOUT, version=False))
    else:
        jobs["docker"] = {
            **docker_guard,
            "runs-on": runs_on("heavy", image_arch),
            "timeout-minutes": timeout,
            "steps": [tag_checkout(), build_step(entry, version=True)],
        }
        jobs["rebuild"] = {
            **rebuild_guard,
            "runs-on": runs_on("heavy", image_arch),
            "timeout-minutes": timeout,
            "steps": [CHECKOUT, build_step(entry, version=False)],
        }
    if "deploy_url" in entry:
        jobs["deploy"] = deploy_job(deploy_step_run, entry["deploy_url"])
        if deploy_rebuild:
            # The image roller leaves a webhook-deployed service to its pipeline, so a rebuild no deploy
            # follows never reaches the host. Either build published, and the other was skipped, so the
            # default rule that a skipped need skips the job would never deploy.
            jobs["deploy"] = {
                "needs": ["docker", "rebuild"],
                "if": "always() && (needs.docker.result == 'success' || needs.rebuild.result == 'success')",
                **{k: v for k, v in jobs["deploy"].items() if k != "needs"},
            }
    if "site" in entry:
        jobs["publish-site"] = site_job(repo, entry["site"])
    triggers = {**GATED_TRIGGERS, "schedule": [{"cron": "0 4 * * 1"}]}
    # No `concurrency`, though these repos queued under GitHub: a queued job's runner finds no task and
    # exits, and ci-dispatch never retries it (HL-738). Forgejo's default cancels the older push instead,
    # so the newest commit still wins, and a release a push cancelled heals at the next rebuild's deploy.
    note = REBUILD_DEPLOY_NOTE if deploy_rebuild else NO_REBUILD_DEPLOY_NOTE
    return {"name": "CI/CD", "on": triggers, "jobs": jobs}, SERVICE_NOTE + note


def package_workflow(repo, entry):
    gates = gate_jobs(repo, entry)
    jobs = dict(gates)
    jobs["release"] = release_job(
        gates, install=entry.get("release_install", False), release_env=entry.get("release_env"))
    return {"name": "CI/CD", "on": GATED_TRIGGERS, "jobs": jobs}, ""


def image_workflow(repo, entry, deploy_step_run):
    """A pull request may build, so a review covers what will ship, but never holds the push token."""
    gates = gate_jobs(repo, entry)
    job = {"needs": needing(list(gates))}
    if not entry.get("image_on_pull_request", False):
        job["if"] = "github.event_name != 'pull_request'"
    job.update({
        "runs-on": runs_on("heavy"),
        "timeout-minutes": entry["image_timeout"],
        "steps": [CHECKOUT, {
            "name": "Build, and publish from main",
            "env": {
                "PUBLISH": f"${{{{ {ON_MAIN} && github.event_name != 'pull_request' }}}}",
                "GHCR_WRITE_TOKEN": "${{ github.event_name != 'pull_request' && secrets.GHCR_WRITE_TOKEN || '' }}",
            },
            "run": BUILD_IMAGE,
        }],
    })
    jobs = {**gates, "image": job}
    if "deploy_url" in entry:
        # The same condition as PUBLISH: what the image job did not publish must not be deployed.
        jobs["deploy"] = {
            **deploy_job(deploy_step_run, entry["deploy_url"]),
            "needs": "image",
            "if": f"{ON_MAIN} && github.event_name != 'pull_request'",
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
                {**build_step(entry, True), "run": f"{BUILD_IMAGE} {name}"},
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


def render_repo(repo, entry, deploy_step_run):
    family = entry.get("family")
    if family not in FAMILY_KEYS:
        raise RenderError(f"{repo}: unknown family {family}")
    checked_keys(repo, entry, FAMILY_KEYS[family], ALWAYS_PRESENT[family])
    if family == "service":
        workflow, note = service_workflow(repo, entry, deploy_step_run)
    elif family == "package":
        workflow, note = package_workflow(repo, entry)
    elif family == "image":
        workflow, note = image_workflow(repo, entry, deploy_step_run)
    elif family == "images":
        workflow, note = images_workflow(repo, entry, deploy_step_run)
    elif family == "verify":
        workflow, note = verify_workflow(repo, entry)
    elif family == "pages":
        workflow, note = pages_workflow(repo, entry)
    else:
        workflow, note = lint_workflow()
    # Forgejo 15 mails a run only when its workflow opts in, and the forge is these repos' only CI.
    workflow = {"enable-email-notifications": True, **workflow}
    return HEADER + note + yaml.dump(workflow, Dumper=BlockDumper, sort_keys=False, width=1000, allow_unicode=True)


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
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    table = load_table()
    if argv[1] == "--list":
        for repo, entry in table.items():
            print(repo, WORKFLOW_FILE[entry["family"]])
        return 0
    if argv[1] not in table:
        print(f"render_repo.py: {argv[1]} is not in repos.yml", file=sys.stderr)
        return 2
    try:
        run = deploy_step_source(os.environ.get("RENDER_REF", "v1"))
        sys.stdout.write(render_repo(argv[1], table[argv[1]], run))
    except (RenderError, subprocess.CalledProcessError) as e:
        print(f"render_repo.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
