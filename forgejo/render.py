#!/usr/bin/env python3
"""Render a crate's Forgejo shadow workflow from rust-crate.yml (HL-508).

The forge cannot call this repo's reusable workflow: Forgejo resolves cross-repo `uses:` only
from a public repo, and the forge keeps every repo private. So each crate carries its own copy,
and the copy is derived here from rust-crate.yml's two verify jobs rather than written by hand,
so the shadow runs exactly what GitHub runs. Every difference is one entry of TRANSFORMS below:
that list is the edit count phase 3 costs per family (spec §1, Acceptance "Edits").

Verify only: trivy and release are dropped, so the forge never publishes (spec §1). Anything in
rust-crate.yml this script does not know how to carry over (a new `uses:` step, a new `if:`, an
input it cannot substitute) fails the render instead of being copied across silently.

Usage:
  render.py <crate_name> <coverage_floor> <is_cli_tool> <cron>   # print the workflow
  RENDER_REF=<ref> overrides the ref rust-crate.yml is read at (default v1).
"""
import os
import re
import subprocess
import sys

import yaml

SOURCE = "rust-crate.yml"
KEPT_JOBS = ("build-and-gate", "test-coverage")
LABELS = ["self-hosted", "arm64", "ci-heavy"]
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
)

FORK_GUARD = PRIVATE_ONLY + " && github.event.pull_request.head.repo.fork"
# Conditions matched whole, never by substring: a new condition that merely mentions one of
# these must reach the "unknown condition" refusal, not be dropped. A sidecar step names the
# input that switches it on, so asking for a sidecar fails the render instead of dropping it.
DROPPED = {FORK_GUARD: None, PUBLIC_ONLY: None}
SIDECARS = {
    "inputs.needs_postgres": "needs_postgres",
    "inputs.needs_nats": "needs_nats",
    "always() && inputs.needs_postgres": "needs_postgres",
    "always() && inputs.needs_nats": "needs_nats",
}
# A sidecar's URL env is `${{ inputs.needs_X && format(...) || '' }}`: '' when X is off, as on GitHub.
SIDECAR_ENV = re.compile(r"\$\{\{ inputs\.(needs_[a-z]+) && .* \|\| '' \}\}")
TOP_LEVEL = {"name", True, "permissions", "concurrency", "jobs"}  # PyYAML reads bare `on:` as True

# The token travels in git's env-config, never in argv (runner hosts expose /proc cmdline).
CHECKOUT = {
    "name": "Check out (plain git)",
    "env": {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
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


class RenderError(Exception):
    pass


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


def render(source, inputs, cron):
    wf = yaml.safe_load(source)
    unknown = set(wf) - TOP_LEVEL
    if unknown:
        raise RenderError(f"workflow keys this render does not carry: {sorted(map(str, unknown))}")
    jobs = {}
    for job_id in KEPT_JOBS:
        src = wf["jobs"][job_id]
        job = {}
        if "needs" in src:
            job["needs"] = src["needs"]
        job["runs-on"] = LABELS
        steps = [carry_step(s, inputs) for s in src["steps"]]
        job["steps"] = [s for s in steps if s is not None]
        unknown = set(src) - {"needs", "runs-on", "steps"}
        if unknown:
            raise RenderError(f"{job_id}: keys this render does not carry: {sorted(unknown)}")
        jobs[job_id] = job
    out = {
        "name": "CI (Forgejo shadow)",
        "on": {
            "push": {"branches": ["main"]},
            "schedule": [{"cron": cron}],
            "workflow_dispatch": {},
        },
        "jobs": jobs,
    }
    text = yaml.dump(out, Dumper=BlockDumper, sort_keys=False, width=1000, allow_unicode=True)
    if "inputs." in text or "visibility" in text:
        raise RenderError("an input or a visibility branch survived the render")
    return HEADER.format(transforms="\n".join(f"#   {t}: {w}" for t, w in TRANSFORMS)) + text


HEADER = """\
# Generated by dlepaux/github-workflows forgejo/render.py from rust-crate.yml (HL-508).
# Do not edit: re-render with forgejo/sync.sh. Forgejo shadow CI, verify only: it never
# publishes, and GitHub's .github/workflows stays the CI of record until this crate's flip.
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
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    crate, floor, cli, cron = argv[1:]
    if not re.fullmatch(r"[a-z0-9-]+", crate) or not floor.isdigit() or cli not in ("true", "false"):
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
        sys.stdout.write(render(source, inputs, cron))
    except RenderError as e:
        print(f"render.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
