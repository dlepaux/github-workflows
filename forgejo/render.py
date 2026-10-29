#!/usr/bin/env python3
"""Render a crate's Forgejo workflow from rust-crate.yml (HL-508), shadow or primary.

The forge cannot call this repo's reusable workflow: Forgejo resolves cross-repo `uses:` only
from a public repo, and the forge keeps every repo private. So each crate carries its own copy,
and the copy is derived here from rust-crate.yml's two verify jobs rather than written by hand,
so the shadow runs exactly what GitHub runs. Every difference is one entry of TRANSFORMS below:
that list is the edit count phase 3 costs per family (spec §1, Acceptance "Edits").

A shadow is verify only: trivy and release are dropped, so the forge never publishes (spec §1).
A primary crate (its family has flipped, HL-657) is the forge's to release, so it also carries
trivy and the release job, the release rewritten for host mode: PRIMARY_TRANSFORMS. Anything in
rust-crate.yml this script does not know how to carry over (a new `uses:` step, a new `if:`, an
input it cannot substitute, a changed release step) fails the render instead of being copied
across silently.

Usage:
  render.py <crate_name> <coverage_floor> <is_cli_tool> <cron> <shadow|primary>   # print it
  RENDER_REF=<ref> overrides the ref rust-crate.yml is read at (default v1).
"""
import os
import re
import subprocess
import sys

import yaml

SOURCE = "rust-crate.yml"
KEPT_JOBS = ("build-and-gate", "test-coverage")
PRIMARY_JOBS = KEPT_JOBS + ("trivy", "release")
LABELS = ["self-hosted", "arm64", "ci-heavy"]
# trivy is medium on GitHub (arch-any, ci-medium). The forge's medium class is arm64 only, as its
# runners must reach the LAN-only /api/actions/ (homelab group_vars nomad_forgejo_jobs, HL-657).
MEDIUM_LABELS = ["self-hosted", "arm64", "ci-medium"]
JOB_LABELS = {"trivy": MEDIUM_LABELS}
PUBLIC_ONLY = "github.event.repository.visibility == 'public'"
PRIVATE_ONLY = "github.event.repository.visibility != 'public'"
VISIBILITY_EXPR = "${{ github.event.repository.visibility }}"

# The documented edits, in the order they apply. Each is (id, why).
TRANSFORMS = (
    ("drop-jobs", "trivy and release are not verify jobs; release publishes"),
    ("runs-on", "the runner offers [self-hosted, arm64, ci-heavy]; the job must ask for all of them"),
    ("drop-fork-guard", "a pull mirror carries no pull requests, and the triggers name none"),
    ("drop-public-only", "the forge forces every repo private, so hosted-runner installs never run"),
    ("strip-private-only", "always private on the forge: private-only conditions go, visibility env is 'private'"),
    ("checkout-git", "host mode would need node for actions/checkout; plain git needs nothing"),
    ("drop-sidecars", "no shadowed crate sets needs_postgres or needs_nats"),
    ("inputs-literal", "a standalone workflow has no inputs; the caller's values are inlined"),
    ("triggers", "push to main plus a staggered nightly schedule (spec §1 Triggers)"),
    ("drop-permissions-concurrency", "GitHub token scopes; the in-flight cap of 1 serialises"),
    ("shell-default", "GitHub runs a step with no shell as `bash -e {0}`; forgejo-runner adds pipefail"),
)
# A primary keeps trivy and release, so drop-jobs gives way to these.
PRIMARY_TRANSFORMS = (
    ("trivy-medium", "trivy runs on the forge's medium class, [self-hosted, arm64, ci-medium]"),
    ("release-checkout", "full history and tags with plain git, on a local main, as semantic-release needs"),
    ("release-host-node", "node and semantic-release are homelab roles/ci-toolchain's, not setup-node and the action"),
    ("release-driver", "forge-release.mjs drops @semantic-release/github and makes the release page on the forge"),
    ("release-token", "the Kellnr publish token is the repo secret KELLNR_PUBLISH_TOKEN, in the release step's env only"),
    ("release-keys", "permissions and outputs go: Forgejo's job token is scoped to its repo, and nothing reads the outputs"),
)
# GitHub's default for a `run:` step with no `shell:`. forgejo-runner maps "" to
# `bash --noprofile --norc -e -o pipefail {0}` (act/runner/step_run.go, v13.2.0) and passes any
# other string through, so pinning this one gives the shadow GitHub's semantics.
GITHUB_DEFAULT_SHELL = "bash -e {0}"

FORK_GUARD = PRIVATE_ONLY + " && github.event.pull_request.head.repo.fork"
# Conditions matched whole, never by substring: a new condition that merely mentions one of
# these must reach the "unknown condition" refusal, not be dropped. A sidecar step names the
# input that switches it on, so asking for a sidecar fails the render instead of dropping it.
DROPPED = {FORK_GUARD: None, PUBLIC_ONLY: None}
RELEASE_IF = "inputs.release &&\ngithub.ref == 'refs/heads/main' &&\ngithub.event_name == 'push'"
# inputs.release is GitHub's verify-only switch for a flipped crate. On the forge, a crate releases
# because crates.tsv says primary, so the rendered job keeps the rest of the condition only.
FORGE_RELEASE_IF = "github.ref == 'refs/heads/main' && github.event_name == 'push'"
SIDECARS = {
    "inputs.needs_postgres": "needs_postgres",
    "inputs.needs_nats": "needs_nats",
    "always() && inputs.needs_postgres": "needs_postgres",
    "always() && inputs.needs_nats": "needs_nats",
}
# A sidecar's URL env is `${{ inputs.needs_X && format(...) || '' }}`: '' when X is off, as on GitHub.
SIDECAR_ENV = re.compile(r"\$\{\{ inputs\.(needs_[a-z]+) && .* \|\| '' \}\}")
TOP_LEVEL = {"name", True, "permissions", "concurrency", "jobs"}  # PyYAML reads bare `on:` as True

# The token travels in git's env-config, never in argv (runner hosts expose /proc cmdline), and
# only to the forge: an unscoped http.extraHeader goes to any host a step's git talks to. Forgejo's
# server_url is its AppURL, slash included; git reads the URL up to the key's last dot, so the
# key holds with or without the slash, where "<url>/.extraHeader" would not.
CHECKOUT = {
    "name": "Check out (plain git)",
    "env": {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.${{ github.server_url }}.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: token ${{ github.token }}",
        "REPO_URL": "${{ github.server_url }}/${{ github.repository }}.git",
        "REF": "${{ github.ref }}",
        "SHA": "${{ github.sha }}",
    },
    "run": (
        "set -eu\n"
        '[ -z "$(ls -A)" ] || { echo "::error::workspace not empty"; exit 1; }\n'
        "git init -q .\n"
        'git fetch -q --depth=50 "$REPO_URL" "+$REF:refs/remotes/origin/ref"\n'
        'git checkout -q --detach "$SHA"\n'
        'test "$(git rev-parse HEAD)" = "$SHA"\n'
    ),
}


# The token travels as CHECKOUT's does. semantic-release reads its config from the checkout, wants
# the branch it releases checked out and every tag fetched, and pushes the release commit and tag
# through the same header (forge-release.mjs). git would follow main's tags without --tags; it is
# there so a remote.tagOpt or a tag off main cannot leave the version computed from too few tags.
GIT_AUTH = {k: CHECKOUT["env"][k] for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")}
RELEASE_CHECKOUT = {
    "name": "Check out with history and tags (plain git)",
    "env": {**GIT_AUTH, "REPO_URL": CHECKOUT["env"]["REPO_URL"], "REF": "${{ github.ref }}", "SHA": "${{ github.sha }}"},
    "run": (
        "set -eu\n"
        'test "$REF" = refs/heads/main\n'
        '[ -z "$(ls -A)" ] || { echo "::error::workspace not empty"; exit 1; }\n'
        "git init -q .\n"
        'git remote add origin "$REPO_URL"\n'
        'git fetch -q --tags origin "+refs/heads/main:refs/remotes/origin/main"\n'
        'git checkout -q -B main "$SHA"\n'
        'test "$(git rev-parse HEAD)" = "$SHA"\n'
    ),
}
RELEASE_STEP = {
    "name": "Run semantic-release (forge)",
    "env": {
        **GIT_AUTH,
        "FORGEJO_TOKEN": "${{ github.token }}",
        "CARGO_REGISTRIES_GORDON_TOKEN": "${{ secrets.KELLNR_PUBLISH_TOKEN }}",
    },
    "run": (
        "set -eu\n"
        '[ -n "${CARGO_REGISTRIES_GORDON_TOKEN:-}" ] || {\n'
        '  echo "::error::KELLNR_PUBLISH_TOKEN is not set on this repo (homelab forgejo_actions_repo_secrets)"; exit 1; }\n'
        "node /usr/local/lib/ci-toolchain/semantic-release/forge-release.mjs\n"
    ),
}


class RenderError(Exception):
    pass


# rust-crate.yml's release steps as this render knows them, compared whole (an action's version
# aside): a changed `with:` or `env:` on a dropped or replaced step fails the render, since
# the forge would otherwise silently not do what GitHub does.
RELEASE_SOURCE = yaml.safe_load(r"""
- name: Refuse fork pull requests on self-hosted runners
  if: github.event.pull_request.head.repo.fork
  run: |
    echo "::error::Fork pull request on a self-hosted runner — refused (HL-390)."
    exit 1
- uses: actions/checkout@
  with:
    fetch-depth: 0
    persist-credentials: true
- uses: actions/setup-node@
  with:
    node-version: "24"
- name: Ensure cargo-edit
  run: command -v cargo-set-version >/dev/null 2>&1 || cargo install cargo-edit --locked
- name: Source kellnr token from repo secret
  env:
    KELLNR_TOKEN_SECRET: ${{ secrets.KELLNR_TOKEN }}
  run: |
    set -eu
    if [ -n "${KELLNR_TOKEN_SECRET:-}" ]; then
      echo "::add-mask::$KELLNR_TOKEN_SECRET"
      {
        echo "CARGO_REGISTRIES_GORDON_TOKEN=$KELLNR_TOKEN_SECRET"
        echo "KELLNR_TOKEN=$KELLNR_TOKEN_SECRET"
      } >> "$GITHUB_ENV"
      echo "kellnr token: sourced from repo secret"
    else
      echo "kellnr token: repo secret not forwarded — using runner ambient env"
    fi
- name: Run semantic-release
  id: semrel
  uses: cycjimmy/semantic-release-action@
  with:
    semantic_version: 24
    extra_plugins: |
      @semantic-release/changelog@6
      @semantic-release/exec@6
      @semantic-release/git@10
      conventional-changelog-conventionalcommits@8
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
""")


# What the forge does with each of RELEASE_SOURCE's steps: None drops it, "carry" keeps it as
# carry_step would, a dict replaces it.
RELEASE_ACTIONS = (None, RELEASE_CHECKOUT, None, "carry", None, RELEASE_STEP)


def _unversioned(step):
    if "uses" not in step:
        return step
    return {**step, "uses": re.sub(r"@[^@]+$", "@", step["uses"])}


def carry_release(src, inputs):
    unknown = set(src) - {"needs", "if", "runs-on", "env", "permissions", "outputs", "steps"}
    if unknown:
        raise RenderError(f"release: keys this render does not carry: {sorted(unknown)}")
    if str(src.get("if", "")).strip() != RELEASE_IF:
        raise RenderError(f"release: condition {src.get('if')!r} is not the one this render knows")
    steps = src["steps"]
    if len(steps) != len(RELEASE_SOURCE):
        raise RenderError(f"release: {len(steps)} steps, this render knows {len(RELEASE_SOURCE)}")
    out = []
    for i, (step, known, action) in enumerate(zip(steps, RELEASE_SOURCE, RELEASE_ACTIONS)):
        if _unversioned(step) != known:
            raise RenderError(f"release: step {i} ({step.get('name') or step.get('uses')}) is not the one this render knows")
        if action == "carry":
            out.append(carry_step(step, inputs))
        elif action is not None:
            out.append(dict(action))
    job = {"needs": src["needs"], "if": FORGE_RELEASE_IF, "runs-on": LABELS}
    if "env" in src:
        job["env"] = src["env"]
    job["steps"] = out
    return job


def carry_step(step, inputs):
    """One source step, transformed, or None when the step is dropped."""
    name = step.get("name", "")
    cond = step.get("if")
    cond = cond.strip() if isinstance(cond, str) else cond
    if cond in DROPPED:
        return None
    if cond in SIDECARS:
        if inputs[SIDECARS[cond]]:
            raise RenderError(f"{name}: sidecars are not carried over")
        return None
    if step.get("uses", "").startswith("actions/checkout@"):
        if set(step) - {"uses"}:
            raise RenderError("actions/checkout gained options this render does not carry")
        return dict(CHECKOUT)
    if "uses" in step:
        raise RenderError(f"{step['uses']}: a node action cannot run in host mode")
    out = {k: v for k, v in step.items() if k != "if"}
    if cond is not None and cond != PRIVATE_ONLY:
        raise RenderError(f"{name}: unknown condition {cond!r}")
    if "env" in out:
        out["env"] = {k: carry_env(v, inputs) for k, v in out["env"].items()}
    return substitute(out, inputs)


def carry_env(value, inputs):
    if value == VISIBILITY_EXPR:
        return "private"
    m = SIDECAR_ENV.fullmatch(str(value))
    if m:
        if inputs.get(m.group(1)) is not False:
            raise RenderError(f"{m.group(1)}: sidecars are not carried over")
        return ""
    return value


def substitute(node, inputs):
    if isinstance(node, dict):
        return {k: substitute(v, inputs) for k, v in node.items()}
    if isinstance(node, list):
        return [substitute(v, inputs) for v in node]
    if not isinstance(node, str):
        return node

    def literal(m):
        key = m.group(1)
        if key not in inputs:
            raise RenderError(f"input {key} has no value")
        value = inputs[key]
        return str(value).lower() if isinstance(value, bool) else str(value)

    return re.sub(r"\$\{\{\s*inputs\.([a-z_]+)\s*\}\}", literal, node)


def render(source, inputs, cron, primary=False):
    wf = yaml.safe_load(source)
    unknown = set(wf) - TOP_LEVEL
    if unknown:
        raise RenderError(f"workflow keys this render does not carry: {sorted(map(str, unknown))}")
    jobs = {}
    for job_id in PRIMARY_JOBS if primary else KEPT_JOBS:
        src = wf["jobs"][job_id]
        if job_id == "release":
            jobs[job_id] = carry_release(src, inputs)
            continue
        job = {}
        if "needs" in src:
            job["needs"] = src["needs"]
        job["runs-on"] = JOB_LABELS.get(job_id, LABELS)
        steps = [carry_step(s, inputs) for s in src["steps"]]
        job["steps"] = [s for s in steps if s is not None]
        unknown = set(src) - {"needs", "runs-on", "steps"}
        if unknown:
            raise RenderError(f"{job_id}: keys this render does not carry: {sorted(unknown)}")
        jobs[job_id] = job
    out = {
        "name": "CI (Forgejo)" if primary else "CI (Forgejo shadow)",
        "on": {
            "push": {"branches": ["main"]},
            "schedule": [{"cron": cron}],
            "workflow_dispatch": {},
        },
        "defaults": {"run": {"shell": GITHUB_DEFAULT_SHELL}},
        "jobs": jobs,
    }
    text = yaml.dump(out, Dumper=BlockDumper, sort_keys=False, width=1000, allow_unicode=True)
    if "inputs." in text or "visibility" in text:
        raise RenderError("an input or a visibility branch survived the render")
    transforms = (tuple(t for t in TRANSFORMS if t[0] != "drop-jobs") + PRIMARY_TRANSFORMS) if primary else TRANSFORMS
    header = PRIMARY_HEADER if primary else HEADER
    return header.format(transforms="\n".join(f"#   {t}: {w}" for t, w in transforms)) + text


HEADER = """\
# Generated by dlepaux/github-workflows forgejo/render.py from rust-crate.yml (HL-508).
# Do not edit: re-render with forgejo/sync.sh. Forgejo shadow CI, verify only: it never
# publishes, and GitHub's .github/workflows stays the CI of record until this crate's flip.
# Edits against rust-crate.yml:
{transforms}
"""
PRIMARY_HEADER = """\
# Generated by dlepaux/github-workflows forgejo/render.py from rust-crate.yml (HL-508, HL-657).
# Do not edit: re-render with forgejo/sync.sh. This crate is primary on the forge: this is its CI
# of record, and its release job publishes to Kellnr. GitHub only receives the push mirror.
# Edits against rust-crate.yml:
{transforms}
"""


class BlockDumper(yaml.SafeDumper):
    """Block literals for scripts, and no anchors: each job reads whole on its own."""

    def ignore_aliases(self, data):
        return True


def _str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


BlockDumper.add_representer(str, _str)


def main(argv):
    if len(argv) != 6:
        print(__doc__, file=sys.stderr)
        return 2
    crate, floor, cli, cron, mode = argv[1:]
    if (not re.fullmatch(r"[a-z0-9-]+", crate) or not floor.isdigit() or cli not in ("true", "false")
            or mode not in ("shadow", "primary")):
        print("render.py: bad arguments", file=sys.stderr)
        return 2
    inputs = {
        "crate_name": crate,
        "coverage_floor": int(floor),
        "is_cli_tool": cli == "true",
        "needs_postgres": False,
        "needs_nats": False,
    }
    # Callers resolve @v1, so the shadow renders what v1 points at, not the working tree.
    ref = os.environ.get("RENDER_REF", "v1")
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        source = subprocess.run(
            ["git", "-C", here, "show", f"{ref}:.github/workflows/{SOURCE}"],
            check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        print(f"render.py: cannot read {SOURCE} at {ref}: {e.stderr.strip()}", file=sys.stderr)
        return 1
    try:
        sys.stdout.write(render(source, inputs, cron, primary=mode == "primary"))
    except RenderError as e:
        print(f"render.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
