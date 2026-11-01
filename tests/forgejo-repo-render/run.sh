#!/usr/bin/env bash
# HL-711: forgejo/render_repo.py assembles each flipped repo's forge workflow. These cases hold what
# the render exists for: no job can test one commit and publish another, and a key or a family the
# render does not know fails it instead of being dropped.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import copy, re, sys
import yaml
root = sys.argv[1]
sys.path.insert(0, f"{root}/forgejo")
import render_repo as rr
from render import RenderError

table = rr.load_table()
DEPLOY = "deploy step from deploy.yml\n"
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def refuses(name, repo, entry, word):
    try:
        rr.render_repo(repo, entry, DEPLOY)
    except RenderError as e:
        check(f"{name}: refused for the right reason ({e})", word in str(e))
    else:
        check(f"{name}: refused", False)


def checkouts(job):
    return [s for s in job["steps"] if "git init" in (s.get("run") or "")]


rendered = {r: yaml.safe_load(rr.render_repo(r, e, DEPLOY)) for r, e in table.items()}

for repo, wf in rendered.items():
    for job_id, job in wf["jobs"].items():
        for step in checkouts(job):
            where = f"{repo}/{job_id}"
            if job_id == "docker":
                # The release commit sits past github.sha, so the image takes the tag it was given.
                check(f"{where} builds the release tag", step["env"]["REF"].startswith("refs/tags/v${{"))
                continue
            check(f"{where} pins the pushed commit", step["env"].get("SHA") == "${{ github.sha }}")
            check(f"{where} checks out that commit",
                  re.search(r'^git checkout -q (--detach|-B main) "\$SHA"$', step["run"], re.M) is not None)
            check(f"{where} proves HEAD is it", 'test "$(git rev-parse HEAD)" = "$SHA"' in step["run"])
        check(f"{repo}/{job_id} has a timeout", "timeout-minutes" in job)
    check(f"{repo} mails a failed run", wf.get("enable-email-notifications") is True)
    check(f"{repo}: every `checkout` placeholder was replaced",
          "checkout" not in [s for j in wf["jobs"].values() for s in j["steps"]])

# What holds a publish behind its gates.
for repo, wf in rendered.items():
    jobs = wf["jobs"]
    if "release" not in jobs:
        continue
    gates = [j for j in jobs if j not in ("release", "docker", "rebuild", "deploy", "image")]
    needs = jobs["release"].get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    check(f"{repo}: release waits for every gate", sorted(needs) == sorted(gates))
    check(f"{repo}: release runs on a push only", jobs["release"]["if"] == "github.event_name == 'push'")
    if "docker" in jobs:
        check(f"{repo}: the image waits for a release", jobs["docker"]["needs"] == "release"
              and jobs["docker"]["if"] == "needs.release.outputs.version != ''")
        check(f"{repo}: a rebuild runs only on the schedule or by hand",
              jobs["rebuild"]["if"] == "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'")
        check(f"{repo}: a rebuild waits for quality where there is one",
              ("quality" in gates) == (jobs["rebuild"].get("needs") == "quality"))

# A pull request gets gates and nothing that publishes; a host repo gets no check at all, which is
# what holds its updates to a human's merge (HL-130). The guards are pinned whole, since `always()`
# on a job would otherwise pass here and fire its write secret from a pull request.
PUBLISHERS = {
    "release": "github.event_name == 'push'",
    "docker": "needs.release.outputs.version != ''",
    "rebuild": "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'",
    "deploy": None,
}
LINT_ON = {
    "push": {"branches": ["main"], "paths": [
        "docker-compose*.yml",
        "loopback-bind.allowlist",
        "scripts/check-loopback-bindings.sh",
        ".forgejo/workflows/loopback-bindings-lint.yml",
    ]},
    "workflow_dispatch": {},
}
for repo, wf in rendered.items():
    if table[repo]["family"] == "lint":
        check(f"{repo}: a host repo's triggers put no status on a Renovate branch", wf["on"] == LINT_ON)
        continue
    check(f"{repo}: pull requests into main only", wf["on"]["pull_request"] == {"branches": ["main"]})
    for job_id, job in wf["jobs"].items():
        if job_id in PUBLISHERS:
            check(f"{repo}/{job_id}: guard pinned", job.get("if") == PUBLISHERS[job_id])
        elif table[repo]["family"] == "image" and job_id == "image":
            continue  # Its own cases are below: this job publishes, behind its gates.
        elif table[repo]["family"] != "pages":
            # A gate runs on the pull request, so only the read-only install token may reach it.
            names = set(re.findall(r"secrets\.([A-Z_]+)", yaml.safe_dump(job)))
            check(f"{repo}/{job_id}: a gate holds no write secret", "if" not in job and names <= {"NPM_TOKEN"})

# A site builds on a pull request and publishes on main only: the key sits on the guarded step, and
# what it uploads is what the gates in the same job just built.
for repo, wf in rendered.items():
    if table[repo]["family"] != "pages":
        continue
    job = wf["jobs"]["publish"]
    guarded = job["steps"][-2:]
    check(f"{repo}: publish and served close the job", [s["name"] for s in guarded] == ["Publish", "Served"])
    check(f"{repo}: both run on main only", all(s["if"] == "github.ref == 'refs/heads/main'" for s in guarded))
    built = job["steps"][:-2]
    check(f"{repo}: the build runs on a pull request", all("if" not in s for s in built) and "if" not in job)
    check(f"{repo}: only the guarded steps hold a secret", "secrets." not in yaml.safe_dump({**job, "steps": built}))
    check(f"{repo}: uploads what it built", f"tar -cz -C {table[repo]['site_dir']} ." in guarded[0]["run"])
    check(f"{repo}: the key is never in argv", "printf '%s\\n' \"$DEPLOY_KEY\"" in guarded[0]["run"])

# These scripts were written against GitHub's shell, without pipefail.
for repo, wf in rendered.items():
    if table[repo]["family"] in ("verify", "pages"):
        check(f"{repo}: shell pinned", wf["defaults"] == {"run": {"shell": "bash -e {0}"}})

# OpenSCAD comes from one pin, and its step is what each job's env feeds.
for repo, wf in rendered.items():
    for job in wf["jobs"].values():
        uses = any(s.get("name") == rr.OPENSCAD_STEP["name"] for s in job["steps"])
        if uses:
            check(f"{repo}: OpenSCAD is the one pin", job["env"]["OPENSCAD_SHA256"] == rr.OPENSCAD_SHA256)
            check(f"{repo}: its version is checked", "OpenSCAD version $OPENSCAD_VERSION" in rr.OPENSCAD_STEP["run"])

# Deploy comes from the docker job only, and carries deploy.yml's step whole.
for repo, wf in rendered.items():
    deploy = wf["jobs"].get("deploy")
    check(f"{repo}: deploy matches the table", (deploy is not None) == ("deploy_url" in table[repo]))
    if deploy:
        check(f"{repo}: deploy follows the image", deploy["needs"] == "docker")
        check(f"{repo}: deploy runs deploy.yml's step", deploy["steps"][0]["run"] == DEPLOY)

# The real deploy.yml still has the step the render takes.
# The image family: its publish sits behind every gate, and a pull request never holds the token.
for repo, wf in rendered.items():
    if table[repo]["family"] != "image":
        continue
    job = wf["jobs"]["image"]
    needs = [job["needs"]] if isinstance(job["needs"], str) else job["needs"]
    check(f"{repo}: image needs every gate", set(needs) == set(table[repo]["gates"]))
    env = job["steps"][-1]["env"]
    check(f"{repo}: a pull request holds no token", "github.event_name != 'pull_request' &&" in env["GHCR_WRITE_TOKEN"])
    check(f"{repo}: only main publishes", "refs/heads/main" in env["PUBLISH"] and "pull_request" in env["PUBLISH"])
    check(f"{repo}: image builds on a pull request only if asked",
          ("if" in job) != table[repo].get("image_on_pull_request", False))

check("deploy.yml's step is found at HEAD", "curl" in rr.deploy_step_source("HEAD"))

# Refusals.
service = next(r for r, e in table.items() if e["family"] == "service" and "gates" in e)
package = next(r for r, e in table.items() if e["family"] == "package")
lint = next(r for r, e in table.items() if e["family"] == "lint")
refuses("unknown family", service, {**table[service], "family": "nope"}, "unknown family")
refuses("unknown repo key", service, {**table[service], "automerge": True}, "does not carry")
refuses("a package key on a service", service, {**table[service], "release_install": True}, "does not carry")
pages = next(r for r, e in table.items() if e["family"] == "pages")
refuses("a site with no served proof", pages, {k: v for k, v in table[pages].items() if k != "served"}, "missing")
refuses("a site split over two jobs", pages, {**table[pages], "gates": {**table[pages]["gates"], "extra": table[pages]["gates"]["publish"]}}, "one job")
refuses("a lint repo with gates", lint, {"family": "lint", "gates": {}}, "does not carry")
refuses("a service with no image timeout", service,
        {k: v for k, v in table[service].items() if k != "image_timeout"}, "missing")
image = next(r for r, e in table.items() if e["family"] == "image")
refuses("an image repo with no timeout", image, {k: v for k, v in table[image].items() if k != "image_timeout"}, "missing")
refuses("a package with no gates", package, {"family": "package"}, "missing")
for name, mutate, word in [
    ("a gate with no checkout", lambda g: g["steps"].remove("checkout"), "exactly one checkout"),
    ("a gate with two checkouts", lambda g: g["steps"].append("checkout"), "exactly one checkout"),
    ("a gate with an unknown key", lambda g: g.update(needs="x"), "does not carry"),
    ("a gate with no timeout", lambda g: g.pop("timeout"), "missing"),
    ("a gate on an unknown class", lambda g: g.update({"class": "large"}), "unknown job class"),
]:
    entry = copy.deepcopy(table[service])
    mutate(next(iter(entry["gates"].values())))
    refuses(name, service, entry, word)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
PY

# sync-repos.sh against fake clones: written, checked, then a stale copy, then a missing clone.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export RENDER_REF=HEAD
repos="$(python3 "$ROOT/forgejo/render_repo.py" --list | cut -d' ' -f1)"
for repo in $repos; do mkdir -p "$WORK/$repo/.git"; done
"$ROOT/forgejo/sync-repos.sh" "$WORK" > /dev/null
"$ROOT/forgejo/sync-repos.sh" --check "$WORK"
victim="$(printf '%s\n' $repos | head -1)"
echo "# by hand" >> "$WORK/$victim/.forgejo/workflows/ci.yml"
if "$ROOT/forgejo/sync-repos.sh" --check "$WORK" > "$WORK/out"; then
  echo "FAIL: --check passed a hand-edited copy"; exit 1
fi
grep -q "stale: .*$victim/" "$WORK/out" || { echo "FAIL: --check named another file"; exit 1; }
rm -rf "$WORK/$victim"
if "$ROOT/forgejo/sync-repos.sh" --check "$WORK" > /dev/null 2>&1; then
  echo "FAIL: a missing clone passed"; exit 1
fi
echo "ok: sync-repos.sh writes the table, and --check names a hand edit and refuses a missing clone"
