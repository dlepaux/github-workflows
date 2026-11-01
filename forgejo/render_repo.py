#!/usr/bin/env python3
"""Render a flipped repo's forge workflow from forgejo/repos.yml (HL-508, HL-711).

The forge cannot share a workflow between repos, as render.py explains, so each repo commits its
own copy. A copy written by hand drifts: seven of them checked out the branch tip instead of the
pushed commit, so a later job could publish what an earlier one never tested (HL-711). So the
parts repos share live here once, and repos.yml holds only what differs: a repo's gates, its env
and its deploy target. A key this script does not know fails the render instead of being dropped.

Families:
  service  gates, then semantic-release, the image on a release, a weekly image rebuild, and
           optionally the deploy webhook
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
    "service": {"family", "gates", "image_timeout", "image_env", "deploy_url"},
    "package": {"family", "gates", "release_install", "release_env"},
    "image": {"family", "gates", "note", "image_timeout", "image_on_pull_request"},
    "lint": {"family"},
    "verify": {"family", "gates", "note"},
    "pages": {"family", "gates", "note", "site_dir", "served"},
}
ALWAYS_PRESENT = {
    "service": {"family", "image_timeout"},
    "package": {"family", "gates"},
    "image": {"family", "gates", "note", "image_timeout"},
    "lint": {"family"},
    "verify": {"family", "gates", "note"},
    "pages": {"family", "gates", "note", "site_dir", "served"},
}
WORKFLOW_FILE = {
    "service": "ci.yml",
    "package": "ci.yml",
    "image": "ci.yml",
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
# The rebuild does not deploy: image-update-exporter reports the digest, and the pull stays a decision.
"""
LINT_NOTE = """\
# Layer 1 of the three-layer auth model: every compose service is loopback-bound or allowlisted.
"""


def runs_on(job_class):
    if job_class not in CLASSES:
        raise RenderError(f"unknown job class {job_class}")
    return ["self-hosted", "arm64", f"ci-{job_class}"]


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
    named = {"checkout": CHECKOUT, "openscad": OPENSCAD_STEP}
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


def service_workflow(repo, entry, deploy_step_run):
    gates = gate_jobs(repo, entry)
    timeout = entry["image_timeout"]
    jobs = dict(gates)
    jobs["release"] = release_job(gates, version_output=True)
    jobs["docker"] = {
        "needs": "release",
        "if": "needs.release.outputs.version != ''",
        "runs-on": runs_on("heavy"),
        "timeout-minutes": timeout,
        "steps": [tag_checkout(), build_step(entry, version=True)],
    }
    rebuild = {"needs": "quality"} if "quality" in gates else {}
    rebuild.update({
        # Named, not `!= 'push'`: a pull request must never publish :latest.
        "if": "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'",
        "runs-on": runs_on("heavy"),
        "timeout-minutes": timeout,
        "steps": [CHECKOUT, build_step(entry, version=False)],
    })
    jobs["rebuild"] = rebuild
    if "deploy_url" in entry:
        jobs["deploy"] = deploy_job(deploy_step_run, entry["deploy_url"])
    triggers = {**GATED_TRIGGERS, "schedule": [{"cron": "0 4 * * 1"}]}
    return {"name": "CI/CD", "on": triggers, "jobs": jobs}, SERVICE_NOTE


def package_workflow(repo, entry):
    gates = gate_jobs(repo, entry)
    jobs = dict(gates)
    jobs["release"] = release_job(
        gates, install=entry.get("release_install", False), release_env=entry.get("release_env"))
    return {"name": "CI/CD", "on": GATED_TRIGGERS, "jobs": jobs}, ""


def image_workflow(repo, entry):
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
    workflow = {"name": "CI", "on": GATED_TRIGGERS, "defaults": RUN_DEFAULTS, "jobs": {**gates, "image": job}}
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
        workflow, note = image_workflow(repo, entry)
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
