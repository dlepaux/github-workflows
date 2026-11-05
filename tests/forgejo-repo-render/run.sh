#!/usr/bin/env bash
# HL-711: forgejo/render_repo.py assembles each flipped repo's forge workflow. These cases hold what
# the render exists for: no job can test one commit and publish another, and a key or a family the
# render does not know fails it instead of being dropped.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import copy, os, re, subprocess, sys, tempfile, textwrap
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


def plain(job_id):
    """A leg, or one of several images, answers to the rules of the job it builds for."""
    return "docker" if job_id.endswith("-image") else re.sub(r"-(arm64|x64)$", "", job_id)


def checkouts(job):
    return [s for s in job["steps"] if "git init" in (s.get("run") or "")]


rendered = {r: yaml.safe_load(rr.render_repo(r, e, DEPLOY)) for r, e in table.items()}

for repo, wf in rendered.items():
    for job_id, job in wf["jobs"].items():
        for step in checkouts(job):
            where = f"{repo}/{job_id}"
            # A hand dispatch has no release to build, so it takes the commit, as a gate does.
            if plain(job_id) == "docker" and step.get("if") != "needs.release.outputs.version == ''":
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
    gates = [j for j in jobs if plain(j) not in ("release", "docker", "rebuild", "deploy", "image", "smoke", "publish-site")]
    needs = jobs["release"].get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    check(f"{repo}: release waits for every gate", sorted(needs) == sorted(gates))
    on_push = rr.TAG_PUSH if table[repo]["family"] == "tag-release" else "github.event_name == 'push'"
    check(f"{repo}: release runs on a push only", jobs["release"]["if"] == on_push)
    if "docker" in jobs and table[repo].get("multi_arch"):
        for job_id, first in (("docker", ["release"]), ("rebuild", gates)):
            legs = [f"{job_id}-arm64", f"{job_id}-x64"]
            check(f"{repo}: {job_id} merges only after both legs", jobs[job_id]["needs"] == first + legs)
            leg_needs = first[0] if len(first) == 1 else first
            check(f"{repo}: {job_id}'s legs follow {first}", all(jobs[l]["needs"] == leg_needs for l in legs))
            # The legs carry the guard; the merge needs them, so the server skips it past a skipped leg.
            check(f"{repo}: {job_id}'s legs carry its guard and the merge has none",
                  len({jobs[l].get("if") for l in legs}) == 1 and jobs[legs[0]].get("if") and "if" not in jobs[job_id])
            check(f"{repo}: each leg builds on its own hardware",
                  jobs[legs[0]]["runs-on"] == ["self-hosted", "arm64", "ci-heavy"]
                  and jobs[legs[1]]["runs-on"] == ["self-hosted", "x64", "ci-heavy"])
            platforms = [jobs[l]["steps"][-1]["env"]["PLATFORM"] for l in legs]
            check(f"{repo}: each leg asks for its own platform", platforms == ["linux/arm64", "linux/amd64"])
            merge_env = jobs[job_id]["steps"][-1]["env"]
            check(f"{repo}: {job_id} merges each leg's digest",
                  all(merge_env[f"DIGEST_{a}"] == f"${{{{ needs.{job_id}-{a.lower()}.outputs.digest }}}}"
                      for a in ("ARM64", "X64")))
        check(f"{repo}: a rebuild claims no version", all("VERSION" not in jobs[j]["steps"][-1]["env"]
              for j in ("rebuild", "rebuild-arm64", "rebuild-x64")))
    elif "docker" in jobs:
        check(f"{repo}: the image waits for a release", jobs["docker"]["needs"] == "release"
              and jobs["docker"]["if"] == "needs.release.outputs.version != ''")
        rebuild_needs = jobs["rebuild"].get("needs", [])
        rebuild_needs = [rebuild_needs] if isinstance(rebuild_needs, str) else rebuild_needs
        if table[repo].get("image_on_every_push"):
            check(f"{repo}: a rebuild waits for every gate and the release", sorted(rebuild_needs) == sorted([*gates, "release"]))
            continue
        check(f"{repo}: a rebuild runs only on the schedule or by hand",
              jobs["rebuild"]["if"] == "github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main')")
        check(f"{repo}: a rebuild waits for every gate", sorted(rebuild_needs) == sorted(gates))

# A pull request gets gates and nothing that publishes; a host repo gets no check at all, which is
# what holds its updates to a human's merge (HL-130). The guards are pinned whole, since `always()`
# on a job would otherwise pass here and fire its write secret from a pull request.
PUBLISHERS = {
    "release": "github.event_name == 'push'",
    "docker": "needs.release.outputs.version != ''",
    # Needs docker, which succeeds only on a release (HL-798).
    "smoke": None,
    "rebuild": "github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main')",
    "deploy": None,
    "publish-site": "github.ref == 'refs/heads/main'",
}
EVERY_PUSH_REBUILD = (
    "always() && needs.quality.result == 'success' && (github.event_name == 'schedule' || "
    "(github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main') || (github.event_name == 'push' && "
    "needs.release.result == 'success' && needs.release.outputs.version == ''))"
)
DEPLOY_REBUILD = "always() && (needs.docker.result == 'success' || needs.rebuild.result == 'success')"
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
        if plain(job_id) in PUBLISHERS:
            pinned = {
                **({"rebuild": EVERY_PUSH_REBUILD} if table[repo].get("image_on_every_push") else {}),
                **({"deploy": DEPLOY_REBUILD} if table[repo].get("deploy_rebuild") else {}),
                # The image job's PUBLISH condition: a pull request's or a branch's build is never published.
                **({"deploy": "github.ref == 'refs/heads/main' && github.event_name != 'pull_request'"}
                   if table[repo]["family"] == "image" else {}),
                # Its own cases are below: one image's guard, and a deploy past all of them.
                **({"docker": job.get("if"), "deploy": job.get("if")} if table[repo]["family"] == "images" else {}),
                **({"release": rr.TAG_PUSH} if table[repo]["family"] == "tag-release" else {}),
                # A multi-arch merge needs its legs, which carry the guard (HL-798).
                **({job_id: None} if table[repo].get("multi_arch") and job_id in ("docker", "rebuild") else {}),
            }
            check(f"{repo}/{job_id}: guard pinned", job.get("if") == pinned.get(plain(job_id), PUBLISHERS[plain(job_id)]))
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

# A service's docs publish on main only, and a release never waits for them.
for repo, wf in rendered.items():
    if "site" not in table[repo]:
        continue
    job = wf["jobs"]["publish-site"]
    check(f"{repo}: publish and served close its site job", [s["name"] for s in job["steps"][-2:]] == ["Publish", "Served"])
    check(f"{repo}: its site uploads what it built", f"tar -cz -C {table[repo]['site']['dir']} ." in job["steps"][-2]["run"])
    check(f"{repo}: only the publish step holds a secret", "secrets." not in yaml.safe_dump({**job, "steps": job["steps"][:-2]}))
    check(f"{repo}: a release does not wait for its site", "publish-site" not in wf["jobs"]["release"]["needs"])
    names = [s["name"] for s in job["steps"]]
    check(f"{repo}: its site dates pages from the whole history",
          names.index(rr.FULL_HISTORY_STEP["name"]) == names.index(rr.CHECKOUT["name"]) + 1)

# A page last touched past the checkout's depth keeps its own commit once the history step ran, and
# the step does not refuse a repo that already fits in that depth.
with tempfile.TemporaryDirectory() as tmp:
    def git(cwd, *args, **env):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                              text=True, capture_output=True, env={**os.environ, **env}).stdout.strip()
    def step(step_def, cwd):
        return subprocess.run(["bash", "-c", step_def["run"]], cwd=cwd, text=True, capture_output=True, env={
            **os.environ, "REPO_URL": f"file://{tmp}/src", "REF": "refs/heads/main", "SHA": git(f"{tmp}/src", "rev-parse", "HEAD")})
    os.makedirs(f"{tmp}/src")
    git(f"{tmp}/src", "init", "-q", "-b", "main")
    open(f"{tmp}/src/page.md", "w").close()
    git(f"{tmp}/src", "add", "page.md")
    git(f"{tmp}/src", "commit", "-qm", "page", GIT_COMMITTER_DATE="2020-01-01T00:00:00Z")
    page_commit = git(f"{tmp}/src", "rev-parse", "HEAD")
    os.makedirs(f"{tmp}/small")
    check("the checkout takes a repo that fits in its depth", step(rr.CHECKOUT, f"{tmp}/small").returncode == 0)
    check("the history step does not refuse one", step(rr.FULL_HISTORY_STEP, f"{tmp}/small").returncode == 0)
    for i in range(60):
        git(f"{tmp}/src", "commit", "-q", "--allow-empty", "-m", str(i))
    os.makedirs(f"{tmp}/big")
    check("the checkout takes a deep repo", step(rr.CHECKOUT, f"{tmp}/big").returncode == 0)
    check("the shallow checkout loses the page's commit (the bug this step fixes)",
          git(f"{tmp}/big", "log", "-1", "--format=%H", "--", "page.md") != page_commit)
    check("the history step fetches", step(rr.FULL_HISTORY_STEP, f"{tmp}/big").returncode == 0)
    check("the page dates from its own commit", git(f"{tmp}/big", "log", "-1", "--format=%H", "--", "page.md") == page_commit)

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
        expected = ["docker", "rebuild"] if table[repo].get("deploy_rebuild") else "docker"
        expected = "image" if table[repo]["family"] == "image" else expected
        expected = [f"{n}-image" for n in table[repo]["images"]] if table[repo]["family"] == "images" else expected
        check(f"{repo}: deploy follows the image", deploy["needs"] == expected)
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

# The images family: nothing builds on a pull request or a push without a release, and the deploy
# waits for every image, since the app's start requeues failures onto the worker's image (dinle).
for repo, wf in rendered.items():
    if table[repo]["family"] != "images":
        continue
    jobs = wf["jobs"]
    built = [f"{n}-image" for n in table[repo]["images"]]
    for job_id in built:
        job = jobs[job_id]
        check(f"{repo}/{job_id}: follows the release", "release" in job["needs"])
        check(f"{repo}/{job_id}: guard pinned", job["if"] == (
            "always() && github.ref == 'refs/heads/main' && needs.app.result == 'success' && "
            "needs.worker.result == 'success' && (github.event_name == 'workflow_dispatch' || "
            "(github.event_name == 'push' && needs.release.result == 'success' && needs.release.outputs.version != ''))"
        ))
        for gate in table[repo]["gates"]:
            check(f"{repo}/{job_id}: {gate} must be green", f"needs.{gate}.result == 'success'" in job["if"])
        check(f"{repo}/{job_id}: main only", "refs/heads/main" in job["if"])
        check(f"{repo}/{job_id}: a push needs a release", "needs.release.outputs.version != ''" in job["if"])
        check(f"{repo}/{job_id}: pull_request is not among the events", "pull_request" not in job["if"])
        check(f"{repo}/{job_id}: a release builds its tag",
              job["steps"][1]["env"]["REF"].startswith("refs/tags/v${{") and "!= ''" in job["steps"][1]["if"])
        check(f"{repo}/{job_id}: a dispatch builds its commit",
              job["steps"][0]["env"]["SHA"] == rr.CHECKOUT["env"]["SHA"] and "== ''" in job["steps"][0]["if"])
        check(f"{repo}/{job_id}: the script is told which image",
              job["steps"][2]["run"] == f"{rr.BUILD_IMAGE} {job_id.removesuffix('-image')}")
    for job_id in built:
        check(f"{repo}: deploy needs {job_id} green", f"needs.{job_id}.result == 'success'" in jobs["deploy"]["if"])
    check(f"{repo}: a checkout holds no token",
          all("GHCR_WRITE_TOKEN" not in jobs[j]["steps"][i].get("env", {}) for j in built for i in (0, 1)))

# The tag-release family: only a pushed tag publishes, past every gate, and a tag runs the gates.
for repo, wf in rendered.items():
    if table[repo]["family"] != "tag-release":
        continue
    job = wf["jobs"]["release"]
    needs = [job["needs"]] if isinstance(job["needs"], str) else job["needs"]
    check(f"{repo}: release needs every gate", set(needs) == set(table[repo]["gates"]))
    check(f"{repo}: a tag starts the run", wf["on"]["push"]["tags"] == ["v*"])
    check(f"{repo}: main still starts the run", wf["on"]["push"]["branches"] == ["main"])
    check(f"{repo}: only a tag's push publishes", job["if"] == "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')")
    check(f"{repo}: the token is the publish step's alone", list(job["steps"][-1]["env"]) == ["FORGEJO_TOKEN"])

check("deploy.yml's step is found at HEAD", "curl" in rr.deploy_step_source("HEAD"))

# The multi-arch route: the shared scripts parse, and a leg that left no digest makes no manifest.
arch_repo = next(r for r, e in table.items()
                 if e["family"] == "service" and "quality" in e.get("gates", {}) and "image_env" not in e)
arch_wf = yaml.safe_load(rr.render_repo(arch_repo, {**table[arch_repo], "multi_arch": True}, DEPLOY))
check("multi_arch adds both legs of both publishers",
      {"docker", "docker-arm64", "docker-x64", "rebuild", "rebuild-arm64", "rebuild-x64"} <= set(arch_wf["jobs"]))
check("a release's leg builds the tag", arch_wf["jobs"]["docker-x64"]["steps"][0]["env"]["REF"].startswith("refs/tags/v${{"))
check("a rebuild's leg builds the pushed commit", arch_wf["jobs"]["rebuild-x64"]["steps"][0]["env"]["SHA"] == "${{ github.sha }}")
check("deploy still follows the manifest", arch_wf["jobs"].get("deploy", {"needs": "docker"})["needs"] == "docker")
for name, script in (("leg", rr.BUILD_LEG), ("merge", rr.MERGE_LEGS)):
    check(f"the {name} script parses", subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0)
refused = subprocess.run(["bash", "-c", rr.MERGE_LEGS], text=True, capture_output=True, env={
    "PATH": os.environ["PATH"], "GHCR_WRITE_TOKEN": "x", "IMAGE": "ghcr.io/x/y", "GITHUB_SHA": "a" * 40,
    "DOCKER_HOST": "unix:///nonexistent", "DIGEST_ARM64": "", "DIGEST_X64": "sha256:" + "0" * 64,
})
check("the manifest is checked before any tag moves",
      rr.MERGE_LEGS.index("--dry-run") < rr.MERGE_LEGS.index('create "${tags[@]}"'))
smoke_entry = {**table[arch_repo], "multi_arch": True, "image_smoke": "true\n"}
smoke_wf = yaml.safe_load(rr.render_repo(arch_repo, smoke_entry, DEPLOY))
for arch, labels in (("arm64", "arm64"), ("x64", "x64")):
    job = smoke_wf["jobs"][f"smoke-{arch}"]
    check(f"smoke-{arch} runs on its own hardware", job["runs-on"] == ["self-hosted", labels, "ci-heavy"])
    check(f"smoke-{arch} follows the manifest", job["needs"] == ["release", "docker"])
    check(f"the smoke script parses ({arch})",
          subprocess.run(["bash", "-n"], input=job["steps"][0]["run"], text=True).returncode == 0)
# epubarr's real smoke against a stub docker: a typo in it would only show after the version is out.
with tempfile.TemporaryDirectory() as stub_dir:
    stub = os.path.join(stub_dir, "docker")
    with open(stub, "w") as f:
        f.write(textwrap.dedent("""\
            #!/bin/bash
            case "$1 $2" in
              "run --rm") echo "epubarr ${STUB_PRINTS:-1.2.3}" ;;
              "image inspect") echo "${STUB_LABEL:-1.2.3}" ;;
              "buildx imagetools") case "$4" in *:1.2) echo "${STUB_MINOR:-same}" ;; *) echo same ;; esac ;;
              *) echo "unexpected docker $*" >&2; exit 9 ;;
            esac
            """))
    os.chmod(stub, 0o755)
    def smoke(**env):
        return subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + table["epubarr"]["image_smoke"]],
            env={"PATH": f"{stub_dir}:{os.environ['PATH']}", "IMAGE": "img", "VERSION": "1.2.3",
                 "PLATFORM": "linux/arm64", **env}, capture_output=True, text=True).returncode
    check("epubarr's smoke passes a right image", smoke() == 0)
    check("epubarr's smoke fails a wrong --version", smoke(STUB_PRINTS="1.2.2") == 1)
    check("epubarr's smoke fails a wrong label", smoke(STUB_LABEL="1.2.2") == 1)
    check("epubarr's smoke fails a minor tag on another digest", smoke(STUB_MINOR="other") == 1)
for repo, entry in table.items():
    if "image_smoke" in entry:
        check(f"{repo}: its smoke script parses",
              subprocess.run(["bash", "-n"], input=entry["image_smoke"], text=True).returncode == 0)
refuses("image_arch with multi_arch", arch_repo, {**table[arch_repo], "multi_arch": True, "image_arch": "x64"}, "image_arch")
refuses("every push with multi_arch", arch_repo, {**table[arch_repo], "multi_arch": True, "image_on_every_push": True}, "image_on_every_push")

# One image for the other arch: both its publishers build there, and nothing else moves.
single = next(r for r, e in table.items() if e["family"] == "service" and not e.get("multi_arch") and "image_arch" not in e)
refuses("an arch the render does not know", single, {**table[single], "image_arch": "riscv"}, "image_arch")
x64_wf = yaml.safe_load(rr.render_repo(single, {**table[single], "image_arch": "x64"}, DEPLOY))
arm_wf = yaml.safe_load(rr.render_repo(single, table[single], DEPLOY))
check("image_arch moves docker", x64_wf["jobs"]["docker"]["runs-on"] == ["self-hosted", "x64", "ci-heavy"])
check("image_arch moves rebuild", x64_wf["jobs"]["rebuild"]["runs-on"] == ["self-hosted", "x64", "ci-heavy"])
check("image_arch moves nothing else", {
    j: v for j, v in x64_wf["jobs"].items() if j not in ("docker", "rebuild")
} == {j: v for j, v in arm_wf["jobs"].items() if j not in ("docker", "rebuild")})
check("the default arch stays arm64", arm_wf["jobs"]["docker"]["runs-on"] == ["self-hosted", "arm64", "ci-heavy"])

# Every push: a commit with no release builds, past green gates only, and a pull request never does.
every_entry = {**table[single], "image_on_every_push": True, "deploy_rebuild": True, "deploy_url": "https://example.test/deploy"}
refuses("every push that never ships", single, {k: v for k, v in every_entry.items() if k != "deploy_rebuild"}, "deploy_rebuild")
refuses("a rebuild's deploy with nowhere to go", single,
        {**{k: v for k, v in table[single].items() if k != "deploy_url"}, "deploy_rebuild": True}, "deploy_url")
every_wf = yaml.safe_load(rr.render_repo(single, every_entry, DEPLOY))
guard = every_wf["jobs"]["rebuild"]["if"]
every_gates = [j for j in every_wf["jobs"] if j not in ("release", "docker", "rebuild", "deploy")]
check("every push: rebuild waits for the release that decides it", "release" in every_wf["jobs"]["rebuild"]["needs"])
check("every push: always() is what lets a skipped release through", guard.startswith("always() && "))
for g in every_gates:
    check(f"every push: {g} must be green", f"needs.{g}.result == 'success'" in guard)
check("every push: a release's own commit stays the docker job's", "needs.release.outputs.version == ''" in guard)
check("every push: a failed release builds nothing", "needs.release.result == 'success'" in guard)
check("every push: pull_request is not among the events", "pull_request" not in guard and "!= 'push'" not in guard)
check("every push: the release job still builds its tag",
      every_wf["jobs"]["docker"]["steps"][0]["env"]["REF"].startswith("refs/tags/v${{"))
check("every push: deploy follows either build", every_wf["jobs"]["deploy"]["needs"] == ["docker", "rebuild"]
      and "needs.docker.result == 'success' || needs.rebuild.result == 'success'" in every_wf["jobs"]["deploy"]["if"])


def guard_is(expr, event, quality, release, version, ref="refs/heads/main"):
    """The guard's truth for one run: the expression is only these names, `&&`, `||` and parentheses."""
    for word, value in (
        ("github.event_name", repr(event)), ("github.ref", repr(ref)), ("needs.quality.result", repr(quality)),
        ("needs.release.result", repr(release)), ("needs.release.outputs.version", repr(version)),
        ("always()", "True"), ("&&", " and "), ("||", " or "),
    ):
        expr = expr.replace(word, value)
    return eval(expr, {"__builtins__": {}})


for event, quality, release, version, builds in (
    ("push", "success", "success", "", True),
    ("push", "success", "success", "1.2.3", False),
    ("push", "success", "failure", "", False),
    ("push", "failure", "skipped", "", False),
    ("push", "cancelled", "skipped", "", False),
    ("schedule", "success", "skipped", "", True),
    ("schedule", "failure", "skipped", "", False),
    ("workflow_dispatch", "success", "skipped", "", True),
    ("pull_request", "success", "skipped", "", False),
    ("pull_request", "success", "success", "", False),
):
    check(f"every push guard: {event} quality={quality} release={release} version={version!r} -> {builds}",
          guard_is(guard, event, quality, release, version) is builds)
check("every push guard: a dispatch on a branch builds nothing",
      guard_is(guard, "workflow_dispatch", "success", "skipped", "", ref="refs/heads/forge-ci") is False)
# Read from Forgejo's action_run: a schedule's ref is `main`, where a dispatch's is refs/heads/main.
check("every push guard: a schedule builds on its bare ref",
      guard_is(guard, "schedule", "success", "skipped", "", ref="main") is True)
# A queue strands its first job until ci-dispatch can retry one (HL-738).
for repo, wf in rendered.items():
    check(f"{repo}: no queue yet", "concurrency" not in wf)
check("the note says a rebuild deploys", "The rebuild deploys too" in rr.render_repo("nilufer", table["nilufer"], DEPLOY))
check("the note says a rebuild does not", "The rebuild does not deploy" in rr.render_repo(single, table[single], DEPLOY))
check("without it, deploy follows docker alone", arm_wf["jobs"].get("deploy", {"needs": "docker"})["needs"] == "docker")

refuses("a smoke with no manifest to check", arch_repo, {**table[arch_repo], "image_smoke": "true\n"}, "needs multi_arch")
refuses("multi_arch with image_env", arch_repo, {**table[arch_repo], "multi_arch": True, "image_env": {"X": "1"}}, "image_env")
check("a missing digest stops the merge before any login", refused.returncode == 1 and "left no digest" in refused.stdout)

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
sited = next(r for r, e in table.items() if "site" in e)
refuses("a service's site with no served proof", sited,
        {**table[sited], "site": {k: v for k, v in table[sited]["site"].items() if k != "served"}}, "missing")
refuses("a service's site with no directory", sited,
        {**table[sited], "site": {k: v for k, v in table[sited]["site"].items() if k != "dir"}}, "missing")
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
    ("a gate on an unknown class", lambda g: g.update({"class": "huge"}), "unknown job class"),
]:
    entry = copy.deepcopy(table[service])
    mutate(next(iter(entry["gates"].values())))
    refuses(name, service, entry, word)

# HL-798: a service ships as SERVICE_FILES, the model above split by trigger. The split must run the
# same jobs as the model on every run, and leave no job holding a runner to skip itself on an event
# its file already rules out. Forgejo 15.0.11's rule (services/actions/job_emitter.go): a job with
# an `if:` always goes to a runner, which evaluates it; a job with none runs when every need succeeded
# and is skipped by the server otherwise. A condition with no status function is success() && it.
import itertools
import schedule


def needs_of(job):
    n = job.get("needs", [])
    return [n] if isinstance(n, str) else list(n)


def evaluate(expr, event, ref, needs, results, versions):
    """The conditions' whole vocabulary; any other word is a NameError, so the case fails loudly."""
    def need(m, value):
        if m.group(1) not in needs:
            raise KeyError(f"{expr!r} reads needs.{m.group(1)}, which the job does not need")
        return repr(value(m.group(1)))
    py = re.sub(r"needs\.([a-z0-9-]+)\.result", lambda m: need(m, results.get), expr)
    py = re.sub(r"needs\.([a-z0-9-]+)\.outputs\.version", lambda m: need(m, lambda j: versions.get(j, "")), py)
    for word, value in (("github.event_name", repr(event)), ("github.ref", repr(ref)), ("always()", "True"),
                        ("&&", " and "), ("||", " or ")):
        py = py.replace(word, value)
    return eval(py, {"__builtins__": {}})


def run(wf, event, ref, outcome):
    """Each job's result, and the jobs a runner took only to skip."""
    jobs, results, versions, wasted = wf["jobs"], {}, {}, set()
    pending = list(jobs)
    while pending:
        job_id = next(j for j in pending if all(n in results for n in needs_of(jobs[j])))
        pending.remove(job_id)
        job, needs = jobs[job_id], needs_of(jobs[job_id])
        green = all(results[n] == "success" for n in needs)
        if "if" not in job:
            runs = green
        else:
            value = evaluate(job["if"], event, ref, needs, results, versions)
            runs = value if "always()" in job["if"] else green and value
            if not runs:
                wasted.add(job_id)
        results[job_id], version = outcome(job_id) if runs else ("skipped", "")
        if version:
            versions[job_id] = version
    return results, wasted


RUNS = (("push", "refs/heads/main"), ("pull_request", "refs/pull/7/head"), ("schedule", "main"),
        ("workflow_dispatch", "refs/heads/main"), ("workflow_dispatch", "refs/heads/forge-ci"))
OUTCOMES = list(itertools.product(("success", "failure"), (("success", "1.2.3"), ("success", ""), ("failure", "")),
                                  ("success", "failure")))


def outcome_of(gates, gate, release, build):
    def outcome(job_id):
        if job_id == "release":
            return release
        if job_id in gates:
            return (gate if job_id == gates[0] else "success"), ""
        return (build if plain(job_id) in ("docker", "rebuild") else "success"), ""
    return outcome


def split_cases(name, repo, entry):
    model = yaml.safe_load(rr.render_repo(repo, entry, DEPLOY))
    files = {f: yaml.safe_load(rr.render_file(repo, entry, f, DEPLOY)) for f in rr.files_of(entry)}
    check(f"{name}: its files are SERVICE_FILES", list(files) == list(rr.SERVICE_FILES))
    check(f"{name}: each event in exactly one file",
          sorted(e for wf in files.values() for e in wf["on"]) == sorted(model["on"]))
    check(f"{name}: each file triggers as the model does", all(wf["on"][e] == model["on"][e] for wf in files.values() for e in wf["on"]))
    check(f"{name}: every file keeps the name required checks are keyed on", all(wf["name"] == "CI/CD" for wf in files.values()))
    check(f"{name}: every file mails a failed run", all(wf["enable-email-notifications"] is True for wf in files.values()))
    check(f"{name}: no file has a queue", all("concurrency" not in wf for wf in files.values()))
    for f, wf in files.items():
        for job_id, job in wf["jobs"].items():
            same = {k: v for k, v in job.items() if k not in ("needs", "if")}
            check(f"{name}/{f}/{job_id}: the model's job but for needs and if",
                  job_id in model["jobs"] and same == {k: v for k, v in model["jobs"][job_id].items() if k not in ("needs", "if")})
            check(f"{name}/{f}/{job_id}: needs only jobs in its file", set(needs_of(job)) <= set(wf["jobs"]))
    gates = [j for j in model["jobs"] if "if" not in model["jobs"][j] and not needs_of(model["jobs"][j])]
    wasted_model = wasted_split = 0
    seen = {}  # per file and job: (runs, every need green) over the file's runs
    for (event, ref), (gate, release, build) in itertools.product(RUNS, OUTCOMES):
        outcome = outcome_of(gates, gate, release, build)
        results, wasted = run(model, event, ref, outcome)
        ran_model = {j for j, r in results.items() if r != "skipped"}
        wasted_model += len(wasted)
        ran = set()
        for f, wf in files.items():
            if event not in wf["on"]:
                continue
            results, wasted = run(wf, event, ref, outcome)
            ran |= {j for j, r in results.items() if r != "skipped"}
            wasted_split += len(wasted)
            for job_id, job in wf["jobs"].items():
                if "if" in job:
                    green = all(results[n] == "success" for n in needs_of(job))
                    seen.setdefault((f, job_id), set()).add((results[job_id] != "skipped", green))
        if ran != ran_model:
            check(f"{name}: {event} {ref} gate={gate} release={release} build={build} runs as the model", False)
            break
    else:
        check(f"{name}: every run runs exactly the model's jobs", True)
    # A condition that never holds in its file, or holds exactly when every need is green, is one the
    # trigger already decided: the job holds a runner for nothing, or should have no `if:` at all.
    for (f, job_id), pairs in seen.items():
        check(f"{name}/{f}/{job_id}: its condition can hold", any(r for r, _ in pairs))
        check(f"{name}/{f}/{job_id}: its condition decides something", any(r != g for r, g in pairs))
    check(f"{name}: fewer runners taken to skip ({wasted_model} -> {wasted_split})", wasted_split < wasted_model)
    return wasted_model, wasted_split


variants = [(r, r, e) for r, e in table.items() if e["family"] == "service"] + [
    ("multi_arch", arch_repo, {**table[arch_repo], "multi_arch": True}),
    ("multi_arch with a smoke", arch_repo, smoke_entry),
    ("an x64 image", single, {**table[single], "image_arch": "x64"}),
    ("every push, deployed", single, every_entry),
    ("a rebuild deployed", single, {**table[single], "deploy_rebuild": True, "deploy_url": "https://example.test/deploy"}),
]
covered = {k for _, _, e in variants for k in ("multi_arch", "image_smoke", "image_on_every_push", "deploy_rebuild", "site", "deploy_url") if e.get(k)}
check("the split's cases cover every option that moves a job", covered == {
    "multi_arch", "image_smoke", "image_on_every_push", "deploy_rebuild", "site", "deploy_url"})
totals = [split_cases(*v) for v in variants]
print(f"split: runners taken to skip over {len(RUNS) * len(OUTCOMES)} runs per variant: "
      f"{sum(t[0] for t in totals)} -> {sum(t[1] for t in totals)}")

# What the split moves, named: a pull request runs the gates alone, and only a push can release.
for repo, entry in table.items():
    if entry["family"] != "service":
        continue
    files = {f: yaml.safe_load(rr.render_file(repo, entry, f, DEPLOY)) for f in rr.files_of(entry)}
    check(f"{repo}: a pull request runs its gates and nothing else",
          set(files["pull-request.yml"]["jobs"]) == set(entry.get("gates", {})))
    check(f"{repo}: the push file releases with no condition", "if" not in files["ci.yml"]["jobs"]["release"])
    check(f"{repo}: nothing else releases", all("release" not in wf["jobs"] for f, wf in files.items() if f != "ci.yml"))
    check(f"{repo}: the weekly line is its own minute, in rebuild.yml alone",
          files["rebuild.yml"]["on"]["schedule"] == [{"cron": schedule.cron(repo, "1")}]
          and all("schedule" not in wf["on"] for f, wf in files.items() if f != "rebuild.yml"))
    check(f"{repo}: each file says why it is split", all("One of three files" in rr.render_file(repo, entry, f, DEPLOY)
                                                          for f in files))
check("the model does not claim to be split", "One of three files" not in rr.render_repo(single, table[single], DEPLOY))
check("--list names a service's three files and one for the rest", [
    line.split(" ") for line in subprocess.run([sys.executable, f"{root}/forgejo/render_repo.py", "--list"],
                                               text=True, capture_output=True, check=True).stdout.splitlines()
] == [[r, f] for r, e in table.items() for f in rr.files_of(e)])
for name, call, word in (
    ("a file the service does not have", lambda: rr.render_file(single, table[single], "other.yml", DEPLOY), "not other.yml"),
    ("a split for a package", lambda: rr.render_repo(package, table[package], DEPLOY, ("push",)), "only a service"),
    ("an event a service does not take", lambda: rr.render_repo(single, table[single], DEPLOY, ("release",)), "not a service's"),
    ("no events at all", lambda: rr.render_repo(single, table[single], DEPLOY, ()), "not a service's"),
):
    try:
        call()
    except RenderError as e:
        check(f"refused: {name} ({e})", word in str(e))
    else:
        check(f"refused: {name}", False)

# The forge registry (homelab HL-787 item 6). FORGE_REGISTRY_TOKEN is owner-wide write:package, so
# it may sit in the env of a pushing step alone, and only behind FORGE_PUBLISHES: a pull request's or
# a branch's run reads it empty whatever its job's condition. Each GHCR render stays as it was.
FORGE_STDIN_LOGIN = """printf '%s' "$FORGE_REGISTRY_TOKEN" | docker login forgejo.lepaux.com -u "$FORGE_REGISTRY_USER" --password-stdin"""


def ghcr_twin(repo):
    return copy.deepcopy(table[repo])


single = next(r for r, e in table.items() if e["family"] == "service" and not e.get("multi_arch") and "image_env" not in e)
multi = next(r for r, e in table.items() if e["family"] == "service" and e.get("multi_arch") and "image_smoke" not in e)
with_env = next(r for r, e in table.items() if e["family"] == "service" and "image_env" in e
                and not e.get("image_on_every_push") and "image_smoke" not in e)
# The real forge entries too, once repos.yml has any, so a flip is checked as written.
real_forge = [r for r, e in table.items() if rr.registry_of(e) == "forge"]
forge_renders = {}
for repo in dict.fromkeys((single, multi, with_env, *real_forge)):
    entry = {**ghcr_twin(repo), "registry": "forge"}
    for events in [rr.SERVICE_EVENTS, *rr.SERVICE_FILES.values()]:
        forge_renders[(repo, tuple(events))] = rr.render_repo(repo, entry, DEPLOY, events=events)
    check(f"{repo}: registry ghcr renders as no registry key",
          rr.render_repo(repo, {**ghcr_twin(repo), "registry": "ghcr"}, DEPLOY) == rr.render_repo(repo, ghcr_twin(repo), DEPLOY))


def token_steps(wf):
    for job_id, job in wf["jobs"].items():
        for step in job.get("steps", []):
            if "FORGE_REGISTRY_TOKEN" in yaml.safe_dump(step):
                yield job_id, step


rendered_tokens = set()
for (repo, events), text in forge_renders.items():
    where = f"{repo} forge {'+'.join(events)}"
    wf = yaml.safe_load(text)
    check(f"{where}: no GHCR credential left", "GHCR_WRITE_TOKEN" not in text and "docker login ghcr.io" not in text)
    # Each mention of the secret is the guarded expression, so no step reads it bare.
    check(f"{where}: the secret is read only behind FORGE_PUBLISHES",
          text.count("secrets.FORGE_REGISTRY_TOKEN") == text.count(rr.FORGE_TOKEN))
    steps = list(token_steps(wf))
    for job_id, step in steps:
        env = step.get("env", {})
        check(f"{where}/{job_id}: the token is guarded", env.get("FORGE_REGISTRY_TOKEN") == rr.FORGE_TOKEN)
        rendered_tokens.add(env.get("FORGE_REGISTRY_TOKEN"))
        check(f"{where}/{job_id}: the image is the forge's", env.get("IMAGE") == f"forgejo.lepaux.com/dlepaux/{repo}")
        check(f"{where}/{job_id}: the user is the repo var", env.get("FORGE_REGISTRY_USER") == "${{ vars.FORGE_REGISTRY_USER }}")
        run = step.get("run", "")
        # A step that holds it pushes: the inline login, or the repo's build script.
        check(f"{where}/{job_id}: only a pushing step holds the token",
              run == rr.BUILD_IMAGE or (rr.FORGE_LOGIN_LINE in run and "--password-stdin" in run))
        # Past its emptiness check and its stdin login, a script names the token nowhere: an echo, a
        # `docker login -p` or a `-e` beside them would leak it.
        # The two lines are written out here, not read from the generator, so a constant that gains a
        # leak fails this check instead of redefining it.
        named = [line for line in run.splitlines() if "FORGE_REGISTRY_TOKEN" in line]
        allowed = [line for line in named if re.fullmatch(r': "\$\{FORGE_REGISTRY_TOKEN:\?[^}$`]*\}"', line)
                   or line == FORGE_STDIN_LOGIN]
        check(f"{where}/{job_id}: the token is in no argv or log",
              named == allowed and (run == rr.BUILD_IMAGE or len(named) == 2))
    builders = [j for j in wf["jobs"] if plain(j) in ("docker", "rebuild")]
    check(f"{where}: every build job pushes with the forge token",
          {j for j, _ in steps} == set(builders))
    for job_id, job in wf["jobs"].items():
        if job_id not in builders:
            check(f"{where}/{job_id}: holds no registry token", "FORGE_REGISTRY_TOKEN" not in yaml.safe_dump(job))

# The token expression the renders carry, evaluated here: what a step reads on each event. It is
# the rendered string, not FORGE_PUBLISHES, so a constant that drops the guard fails the table.
# Python's and/or mirror the expression language's &&/|| on these strings; this checks the logic,
# not how Forgejo's parser reads it (GitHub, for one, compares strings case-insensitively).
check("every forge step carries one token expression", len(rendered_tokens) == 1)
token_expr = next(iter(rendered_tokens))
check("the token expression is a ${{ }} block", token_expr.startswith("${{ ") and token_expr.endswith(" }}"))


def step_reads(event, ref):
    expr = token_expr[4:-3].replace("&&", " and ").replace("||", " or ")
    names = {"github": type("G", (), {"event_name": event, "ref": ref}),
             "secrets": type("S", (), {"FORGE_REGISTRY_TOKEN": "the-secret"})}
    return eval(expr, {"__builtins__": {}}, names)


for event, ref, want in [
    ("push", "refs/heads/main", True),
    ("workflow_dispatch", "refs/heads/main", True),
    ("schedule", "main", True),  # Forgejo hands a schedule the bare branch
    ("schedule", "refs/heads/main", True),
    ("workflow_dispatch", "refs/heads/feature", False),
    ("workflow_dispatch", "main", False),
    ("push", "refs/heads/feature", False),
    ("push", "refs/tags/v1.0.0", False),
    ("pull_request", "refs/pull/3/head", False),
    ("pull_request", "refs/heads/main", False),
    ("pull_request_target", "refs/heads/main", False),
    ("release", "refs/heads/main", False),
]:
    check(f"a step on {event} {ref} reads {'the token' if want else 'nothing'}",
          step_reads(event, ref) == ("the-secret" if want else ""))

refuses("an unknown registry", single, {**ghcr_twin(single), "registry": "quay"}, "registry must be one of")
for registry in rr.REGISTRIES:
    for key in ("FORGE_REGISTRY_TOKEN", "GHCR_WRITE_TOKEN", "IMAGE"):
        refuses(f"a {registry} image_env setting {key}", single,
                {**ghcr_twin(single), "registry": registry, "image_env": {key: "${{ secrets.FORGE_REGISTRY_TOKEN }}"}},
                "image_env may not set")
# The forge build script's contract, on scripts written the ways a port could go wrong.
GOOD_SCRIPT = (
    '#!/usr/bin/env bash\nset -euo pipefail\n'
    ': "${FORGE_REGISTRY_TOKEN:?FORGE_REGISTRY_TOKEN is empty}"\n'
    'DOCKER_CONFIG=$(mktemp -d)\nexport DOCKER_CONFIG\n'
    + FORGE_STDIN_LOGIN + '\n'
    'docker buildx build --push -t "$IMAGE:sha-${GIT_SHA:0:7}" .\n'
)
check("a forge build script that keeps the contract passes", rr.forge_script_faults(GOOD_SCRIPT) == [])
for name, script, word in [
    ("still on GHCR", GOOD_SCRIPT.replace('"$IMAGE', 'ghcr.io/dlepaux/x'), "names GHCR"),
    ("a hardcoded image", GOOD_SCRIPT.replace('"$IMAGE', '"forgejo.lepaux.com/dlepaux/x'), "does not push to $IMAGE"),
    ("no stdin login", GOOD_SCRIPT.replace(FORGE_STDIN_LOGIN, 'docker login forgejo.lepaux.com -u u -p "$FORGE_REGISTRY_TOKEN"'), "on stdin"),
    ("the token lent to trivy", GOOD_SCRIPT + 'docker run -e TRIVY_PASSWORD="$FORGE_REGISTRY_TOKEN" aquasec/trivy image "$IMAGE"\n', "hands the token on"),
    ("the token passed into a container", GOOD_SCRIPT + 'docker run -e FORGE_REGISTRY_TOKEN alpine true\n', "hands the token on"),
    ("the token copied first", GOOD_SCRIPT + 'tok=$FORGE_REGISTRY_TOKEN\n', "hands the token on"),
    ("the token echoed", GOOD_SCRIPT + 'echo "$FORGE_REGISTRY_TOKEN"\n', "hands the token on"),
]:
    faults = rr.forge_script_faults(script)
    check(f"a forge build script with {name} is refused", any(word in f for f in faults))

every_push = next((r for r, e in table.items() if e.get("image_on_every_push")), None)
if every_push:
    refuses("a forge image on every push", every_push, {**ghcr_twin(every_push), "registry": "forge"},
            "image_on_every_push builds on branches")
smoked = next((r for r, e in table.items() if "image_smoke" in e), None)
if smoked:
    refuses("a forge smoke", smoked, {**ghcr_twin(smoked), "registry": "forge"}, "image_smoke pulls with the push token")
image_family = next((r for r, e in table.items() if e["family"] == "image"), None)
if image_family:
    refuses("a registry on the image family", image_family, {**ghcr_twin(image_family), "registry": "forge"}, "registry")

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
needs_script="$(grep -l 'forgejo/build-image\.sh' "$WORK"/*/.forgejo/workflows/* | sed -E 's|/\.forgejo/.*||')"
[ -n "$needs_script" ] || { echo "FAIL: no repo renders the build script, so the check below proves nothing"; exit 1; }
for dir in $needs_script; do printf '#!/bin/sh\n' > "$dir/.forgejo/build-image.sh"; chmod +x "$dir/.forgejo/build-image.sh"; done
"$ROOT/forgejo/sync-repos.sh" --check "$WORK"
script_victim="$(printf '%s\n' $needs_script | head -1)"
chmod -x "$script_victim/.forgejo/build-image.sh"
if "$ROOT/forgejo/sync-repos.sh" --check "$WORK" > "$WORK/out"; then
  echo "FAIL: --check passed a repo whose build script cannot run"; exit 1
fi
grep -q "no build script: $script_victim/" "$WORK/out" || { echo "FAIL: --check did not name the missing build script"; exit 1; }
chmod +x "$script_victim/.forgejo/build-image.sh"
victim="$(printf '%s\n' $repos | head -1)"
echo "# by hand" >> "$WORK/$victim/.forgejo/workflows/ci.yml"
if "$ROOT/forgejo/sync-repos.sh" --check "$WORK" > "$WORK/out"; then
  echo "FAIL: --check passed a hand-edited copy"; exit 1
fi
grep -q "stale: .*$victim/" "$WORK/out" || { echo "FAIL: --check named another file"; exit 1; }
service="$(python3 "$ROOT/forgejo/render_repo.py" --list | awk '$2 == "rebuild.yml" { print $1; exit }')"
for f in ci.yml pull-request.yml rebuild.yml; do
  [ -s "$WORK/$service/.forgejo/workflows/$f" ] || { echo "FAIL: $service: $f was not written"; exit 1; }
done
# Each file as written triggers on its own events, read from the file and not from the renderer.
wf="$WORK/$service/.forgejo/workflows"
triggers() { python3 -c 'import sys, yaml; print(" ".join(sorted(map(str, (lambda d: d.get("on", d.get(True, {})))(yaml.safe_load(open(sys.argv[1])))))))' "$1"; }
[ "$(triggers "$wf/ci.yml")" = "push" ] || { echo "FAIL: $service: ci.yml triggers on $(triggers "$wf/ci.yml")"; exit 1; }
[ "$(triggers "$wf/pull-request.yml")" = "pull_request" ] || { echo "FAIL: $service: pull-request.yml triggers wrong"; exit 1; }
[ "$(triggers "$wf/rebuild.yml")" = "schedule workflow_dispatch" ] || { echo "FAIL: $service: rebuild.yml triggers wrong"; exit 1; }
"$ROOT/forgejo/sync-repos.sh" "$WORK" > /dev/null
echo "# by hand" >> "$WORK/$service/.forgejo/workflows/rebuild.yml"
if "$ROOT/forgejo/sync-repos.sh" --check "$WORK" > "$WORK/out"; then
  echo "FAIL: --check passed a hand-edited rebuild.yml"; exit 1
fi
grep -q "stale: .*$service/.forgejo/workflows/rebuild.yml" "$WORK/out" || { echo "FAIL: --check did not name rebuild.yml"; exit 1; }
rm -rf "$WORK/$victim"
if "$ROOT/forgejo/sync-repos.sh" --check "$WORK" > /dev/null 2>&1; then
  echo "FAIL: a missing clone passed"; exit 1
fi
# A forge render's build script is held to its contract: a scratch copy of this repo with one
# service flipped, its script a GHCR one (a port not yet done), then the contract's own.
FORGE_COPY="$(mktemp -d)"
trap 'rm -rf "$WORK" "$FORGE_COPY"' EXIT
cp -R "$ROOT/." "$FORGE_COPY/"
flipped="$(python3 - "$FORGE_COPY/forgejo/repos.yml" <<'FLIP'
import sys, yaml
path = sys.argv[1]
table = yaml.safe_load(open(path))
repos = table["repos"] if "repos" in table else table
repo = next(r for r, e in repos.items() if e.get("family") == "service" and not e.get("multi_arch")
            and "image_smoke" not in e and not e.get("image_on_every_push"))
repos[repo]["registry"] = "forge"
yaml.safe_dump(table, open(path, "w"), sort_keys=False)
print(repo)
FLIP
)"
FORGE_WORK="$FORGE_COPY/clones"
mkdir -p "$FORGE_WORK/$flipped/.git"
for repo in $repos; do mkdir -p "$FORGE_WORK/$repo/.git"; done
"$FORGE_COPY/forgejo/sync-repos.sh" "$FORGE_WORK" > /dev/null
for dir in $(grep -l 'forgejo/build-image\.sh' "$FORGE_WORK"/*/.forgejo/workflows/* | sed -E 's|/\.forgejo/.*||'); do
  printf '#!/bin/sh\n' > "$dir/.forgejo/build-image.sh"; chmod +x "$dir/.forgejo/build-image.sh"
done
script="$FORGE_WORK/$flipped/.forgejo/build-image.sh"
printf '#!/bin/sh\nIMAGE=ghcr.io/dlepaux/%s\nprintf %%s "$GHCR_WRITE_TOKEN" | docker login ghcr.io -u dlepaux --password-stdin\n' "$flipped" > "$script"
if "$FORGE_COPY/forgejo/sync-repos.sh" --check "$FORGE_WORK" > "$FORGE_WORK/out"; then
  echo "FAIL: --check passed $flipped's forge render with a GHCR build script"; exit 1
fi
grep -q "forge build script $script: still names GHCR" "$FORGE_WORK/out" \
  || { echo "FAIL: --check did not name $flipped's GHCR build script:"; cat "$FORGE_WORK/out"; exit 1; }
python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import render_repo as rr; open(sys.argv[2], "w").write(
  "#!/bin/sh\n: \"${FORGE_REGISTRY_TOKEN:?empty}\"\n" + rr.FORGE_SCRIPT_LOGIN + "\ndocker push \"$IMAGE\"\n")' \
  "$FORGE_COPY/forgejo" "$script"
"$FORGE_COPY/forgejo/sync-repos.sh" --check "$FORGE_WORK" > "$FORGE_WORK/out" \
  || { echo "FAIL: --check refused $flipped's contract-keeping build script:"; cat "$FORGE_WORK/out"; exit 1; }
echo "ok: sync-repos.sh writes the table, and --check names a hand edit, a missing build script, a forge build script off its contract, and refuses a missing clone"
