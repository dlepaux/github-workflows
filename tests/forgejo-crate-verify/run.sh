#!/usr/bin/env bash
# HL-508: forgejo/render.py turns rust-crate.yml's verify jobs into each crate's Forgejo shadow
# workflow. These cases hold the three promises the spec makes of it: the shadow runs what GitHub
# runs, it never publishes, and anything it cannot carry over fails the render instead of being
# copied silently. sync.sh's write and --check modes run against fake clones.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 - "$ROOT" <<'PY'
import copy, re, sys
import yaml
root = sys.argv[1]
sys.path.insert(0, f"{root}/forgejo")
import render as r

src_text = open(f"{root}/.github/workflows/rust-crate.yml", encoding="utf-8").read()
src = yaml.safe_load(src_text)
passed = failed = 0

def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")

def inputs(**kw):
    base = dict(crate_name="demo", coverage_floor=40, is_cli_tool=False,
                needs_postgres=False, needs_nats=False)
    base.update(kw)
    return base

def refuses(name, mutate, ins=None):
    wf = copy.deepcopy(src)
    mutate(wf)
    try:
        r.render(yaml.safe_dump(wf, sort_keys=False), ins or inputs(), "0 1 * * *")
    except r.RenderError:
        check(name, True)
        return
    check(name, False)

out_text = r.render(src_text, inputs(), "30 0 * * *")
out = yaml.safe_load(out_text)

# Verify only: the two verify jobs and nothing that can publish.
check("only the verify jobs", list(out["jobs"]) == ["build-and-gate", "test-coverage"])
body = "".join(l for l in out_text.splitlines(True) if not l.startswith("#"))
for word in ("semantic-release", "cargo publish", "trivy", "packages: write", "contents: write",
             "git push", "git tag", "releases", "--registry", "npm publish"):
    check(f"no {word!r} in the workflow body", word not in body)
check("no permissions block", "permissions" not in out)

# GitHub runs a step with no `shell:` as `bash -e {0}`; forgejo-runner adds pipefail, which
# failed cargo-boundary's module-size gate on a `find` over a glob with no match. Pinned whole.
check("shell pinned to GitHub's default", out.get("defaults") == {"run": {"shell": "bash -e {0}"}})
check("shell pin documented", "shell-default" in out_text.split("\nname:")[0])

# Triggers: push to main, the schedule, a manual dispatch; never a pull request.
on = out["on"]
check("triggers", set(on) == {"push", "schedule", "workflow_dispatch"})
check("push names main only", on["push"] == {"branches": ["main"]})
check("schedule carried", on["schedule"] == [{"cron": "30 0 * * *"}])

steps = [s for j in out["jobs"].values() for s in j["steps"]]
check("labels on every job", all(j["runs-on"] == ["self-hosted", "arm64", "ci-heavy"] for j in out["jobs"].values()))
check("no node action", all("uses" not in s for s in steps))
check("no conditions left", all("if" not in s for s in steps))
check("needs kept", out["jobs"]["test-coverage"].get("needs") == "build-and-gate")

# The job token reaches git through env-config only, never a script's text (argv on the host).
check("no expression in any run", all("${{" not in s.get("run", "") for s in steps))
check("token in env only", out_text.count("${{ github.token }}") == 2
      and all("github.token" not in s.get("run", "") for s in steps))

# Same checks as GitHub: every carried step's script is the source's, inputs inlined.
def literal(text, ins):
    return re.sub(r"\$\{\{\s*inputs\.([a-z_]+)\s*\}\}",
                  lambda m: str(ins[m.group(1)]).lower() if isinstance(ins[m.group(1)], bool) else str(ins[m.group(1)]),
                  text)
for job in r.KEPT_JOBS:
    want = {s["name"]: literal(s["run"], inputs()) for s in src["jobs"][job]["steps"]
            if "run" in s and "name" in s}
    for s in out["jobs"][job]["steps"]:
        if s["name"] in want:
            check(f"{job}/{s['name']} script unchanged", s["run"] == want[s["name"]])
gate = next(s for s in out["jobs"]["build-and-gate"]["steps"] if s["name"] == "Grep-based lints")
check("is_cli_tool inlined", '[ "false" = "true" ]' in gate["run"])
cov = next(s for s in out["jobs"]["test-coverage"]["steps"] if s["name"].startswith("Coverage"))
check("coverage floor inlined", "COV_FLOOR=40\n" in cov["run"])
check("visibility pinned private, sidecar URLs empty as on GitHub", cov["env"] == {
    "GORDON_TEST_DATABASE_URL": "", "GORDON_TEST_NATS_URL": "", "REPO_VISIBILITY": "private"})
# Exactly these steps are dropped, by name: a rename or a new drop must be seen here.
for job, want in {
    "build-and-gate": ["Refuse fork pull requests on self-hosted runners", "Install cargo-deny (GitHub-hosted only)"],
    "test-coverage": ["Refuse fork pull requests on self-hosted runners", "Install cargo-llvm-cov (GitHub-hosted only)",
                      "Start test Postgres", "Start test NATS", "Stop test Postgres", "Stop test NATS"],
}.items():
    kept = {s["name"] for s in out["jobs"][job]["steps"]}
    named = [s["name"] for s in src["jobs"][job]["steps"] if "name" in s]  # checkout has none
    check(f"{job} drops exactly its known steps", sorted(n for n in named if n not in kept) == sorted(want))

# Fail closed on what the render does not know.
refuses("a new node action", lambda wf: wf["jobs"]["build-and-gate"]["steps"].append(
    {"name": "x", "uses": "actions/cache@v4"}))
refuses("an unknown condition", lambda wf: wf["jobs"]["build-and-gate"]["steps"].append(
    {"name": "x", "if": "github.event_name == 'push'", "run": "true"}))
refuses("checkout options", lambda wf: wf["jobs"]["test-coverage"]["steps"].__setitem__(
    2, {"uses": "actions/checkout@v7", "with": {"fetch-depth": 0}}))
refuses("an unknown job key", lambda wf: wf["jobs"]["build-and-gate"].__setitem__("timeout-minutes", 30))
refuses("an input with no value", lambda wf: wf["jobs"]["build-and-gate"]["steps"].append(
    {"name": "x", "run": "echo ${{ inputs.brand_new }}"}))
refuses("sidecars asked for", lambda wf: None, inputs(needs_postgres=True))
refuses("a workflow-level env", lambda wf: wf.__setitem__("env", {"RUSTFLAGS": "-D warnings"}))
refuses("workflow-level defaults", lambda wf: wf.__setitem__("defaults", {"run": {"shell": "bash"}}))
refuses("a condition mentioning the fork guard", lambda wf: wf["jobs"]["build-and-gate"]["steps"].append(
    {"name": "x", "if": "${{ !github.event.pull_request.head.repo.fork }}", "run": "true"}))
refuses("a condition widening public-only", lambda wf: wf["jobs"]["build-and-gate"]["steps"].append(
    {"name": "x", "if": "github.event.repository.visibility == 'public' || github.event_name == 'push'", "run": "true"}))
# Dropping goes by condition, never by name: an unconditional step named like a sidecar runs.
wf = copy.deepcopy(src)
wf["jobs"]["test-coverage"]["steps"].append({"name": "Start test NATS", "run": "echo kept"})
again = yaml.safe_load(r.render(yaml.safe_dump(wf, sort_keys=False), inputs(), "0 1 * * *"))
check("a sidecar's name alone drops nothing",
      any(s.get("run") == "echo kept" for s in again["jobs"]["test-coverage"]["steps"]))

# Primary (HL-657): the same verify jobs, plus trivy and a release job rewritten for host mode.
prim_text = r.render(src_text, inputs(), "30 0 * * *", primary=True)
prim = yaml.safe_load(prim_text)
check("primary: verify, trivy and release", list(prim["jobs"]) == ["build-and-gate", "test-coverage", "trivy", "release"])
check("primary: verify jobs as the shadow's", all(prim["jobs"][j] == out["jobs"][j] for j in r.KEPT_JOBS))
check("primary: named as the CI of record", prim["name"] == "CI (Forgejo)" and "primary on the forge" in prim_text.split("\nname:")[0])
check("primary: header names the release edits, not drop-jobs",
      "release-driver" in prim_text.split("\nname:")[0] and "drop-jobs" not in prim_text.split("\nname:")[0])
check("shadow: still no release", "release" not in out["jobs"] and "forge-release" not in out_text)
trivy = prim["jobs"]["trivy"]
check("primary trivy: the medium class's labels, the others the heavy's",
      trivy["runs-on"] == ["self-hosted", "arm64", "ci-medium"]
      and all(prim["jobs"][j]["runs-on"] == r.LABELS for j in ("build-and-gate", "test-coverage", "release")))
check("primary trivy: plain-git checkout, no action, no condition",
      trivy["steps"][0] == r.CHECKOUT and all("uses" not in s and "if" not in s for s in trivy["steps"]))
rel = prim["jobs"]["release"]
check("release: needs every gate", rel["needs"] == ["build-and-gate", "test-coverage", "trivy"])
check("release: main pushes only", rel["if"] == "github.ref == 'refs/heads/main' && github.event_name == 'push'")
check("release: no permissions or outputs", "permissions" not in rel and "outputs" not in rel)
check("release: host labels", rel["runs-on"] == ["self-hosted", "arm64", "ci-heavy"])
check("release: steps are checkout, cargo-edit, driver",
      [s["name"] for s in rel["steps"]] == [r.RELEASE_CHECKOUT["name"], "Ensure cargo-edit", r.RELEASE_STEP["name"]])
check("release: no action, no condition, no GITHUB_ENV", all("uses" not in s and "if" not in s for s in rel["steps"])
      and "GITHUB_ENV" not in yaml.safe_dump(rel))
check("release: no expression in any run", all("${{" not in s.get("run", "") for s in rel["steps"]))
drive = rel["steps"][2]
check("release: the driver runs from the toolchain", "node /usr/local/lib/ci-toolchain/semantic-release/forge-release.mjs" in drive["run"])
check("release: Kellnr token is the publish secret, in the driver's env only",
      drive["env"]["CARGO_REGISTRIES_GORDON_TOKEN"] == "${{ secrets.KELLNR_PUBLISH_TOKEN }}"
      and prim_text.count("secrets.KELLNR_PUBLISH_TOKEN") == 1 and "secrets." not in yaml.safe_dump(rel["steps"][:2]))
check("release: an empty Kellnr token fails before the driver",
      drive["run"].index("CARGO_REGISTRIES_GORDON_TOKEN") < drive["run"].index("node "))
check("release: no token variable semantic-release would put in a URL",
      not {"GITHUB_TOKEN", "GH_TOKEN", "GIT_CREDENTIALS"} & {k for s in rel["steps"] for k in s.get("env", {})})
# Six: the three verify checkouts, the release checkout, and the driver's header and FORGEJO_TOKEN.
check("primary: job token in env only", prim_text.count("${{ github.token }}") == 6
      and all("github.token" not in s.get("run", "") for j in prim["jobs"].values() for s in j["steps"]))

def refuses_primary(name, mutate):
    wf = copy.deepcopy(src)
    mutate(wf)
    try:
        r.render(yaml.safe_dump(wf, sort_keys=False), inputs(), "0 1 * * *", primary=True)
    except r.RenderError:
        check(name, True)
        return
    check(name, False)

refuses_primary("release: a new step", lambda wf: wf["jobs"]["release"]["steps"].insert(3, {"name": "x", "run": "true"}))
refuses_primary("release: a changed semantic-release step", lambda wf: wf["jobs"]["release"]["steps"][-1].__setitem__(
    "uses", "some/other-action@v1"))
refuses_primary("release: a changed token source", lambda wf: wf["jobs"]["release"]["steps"][4].__setitem__(
    "env", {"KELLNR_TOKEN_SECRET": "${{ secrets.OTHER }}"}))
refuses_primary("release: a widened condition", lambda wf: wf["jobs"]["release"].__setitem__("if", "always()"))
refuses_primary("release: an unknown key", lambda wf: wf["jobs"]["release"].__setitem__("timeout-minutes", 30))
refuses_primary("release: shallow checkout", lambda wf: wf["jobs"]["release"]["steps"][1]["with"].__setitem__("fetch-depth", 1))
refuses_primary("release: a changed semantic-release version", lambda wf: wf["jobs"]["release"]["steps"][5]["with"].__setitem__(
    "semantic_version", 25))
refuses_primary("release: a changed setup-node", lambda wf: wf["jobs"]["release"]["steps"][2].__setitem__(
    "with", {"node-version": "22"}))
refuses_primary("release: a changed fork guard", lambda wf: wf["jobs"]["release"]["steps"][0].__setitem__("run", "true"))
wf = copy.deepcopy(src)
wf["jobs"]["release"]["steps"][5]["uses"] = "cycjimmy/semantic-release-action@v6"
check("release: an action's version alone is not a change",
      "release" in yaml.safe_load(r.render(yaml.safe_dump(wf, sort_keys=False), inputs(), "0 1 * * *", primary=True))["jobs"])
check("the job token's header is scoped to the forge",
      all(s["env"].get("GIT_CONFIG_KEY_0") == r.CHECKOUT["env"]["GIT_CONFIG_KEY_0"]
          for j in prim["jobs"].values() for s in j["steps"] if "GIT_CONFIG_VALUE_0" in s.get("env", {})))
refuses_primary("release: a changed cargo-edit step", lambda wf: wf["jobs"]["release"]["steps"][3].__setitem__(
    "run", "curl -s x | sh"))

print(f"render: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
PY

pass=0 fail=0
ok() { pass=$((pass + 1)); }
no() { fail=$((fail + 1)); echo "FAIL: $1"; }

# Arguments are validated before anything is read.
for bad in "Bad_Name 40 true x shadow" "demo forty true x shadow" "demo 40 yes x shadow" "demo 40 true x publish" "demo 40 true x"; do
  # shellcheck disable=SC2086
  if python3 "$ROOT/forgejo/render.py" $bad >/dev/null 2>&1; then no "accepted: $bad"; else ok; fi
done

# The real checkout script, against a local repo standing in for the forge.
python3 - "$ROOT" "$WORK/checkout.sh" <<'PY'
import sys
sys.path.insert(0, f"{sys.argv[1]}/forgejo")
import render
open(sys.argv[2], "w").write(render.CHECKOUT["run"])
open(sys.argv[2].replace("checkout", "release-checkout"), "w").write(render.RELEASE_CHECKOUT["run"])
PY
src_repo="$WORK/src"
git init -q -b main "$src_repo"
git -C "$src_repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m one
sha=$(git -C "$src_repo" rev-parse HEAD)
checkout() {  # <dir> -> runs the step there, as the runner would
  (cd "$1" && REPO_URL="$src_repo" REF=refs/heads/main SHA=$sha GIT_CONFIG_COUNT=0 bash "$WORK/checkout.sh") >/dev/null 2>&1
}
mkdir -p "$WORK/ws-empty" "$WORK/ws-dirty"; : > "$WORK/ws-dirty/leftover"
if checkout "$WORK/ws-empty" && [ "$(git -C "$WORK/ws-empty" rev-parse HEAD)" = "$sha" ]; then ok; else no "checkout into an empty workspace"; fi
if checkout "$WORK/ws-dirty"; then no "checkout over a non-empty workspace"; else ok; fi

# The release checkout: on a local main at the pushed sha, with every tag, as semantic-release needs.
git -C "$src_repo" tag v1.0.0 "$sha"
git -C "$src_repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m two
sha2=$(git -C "$src_repo" rev-parse HEAD)
release_checkout() {  # <dir> <ref> <sha>
  (cd "$1" && REPO_URL="$src_repo" REF=$2 SHA=$3 GIT_CONFIG_COUNT=0 bash "$WORK/release-checkout.sh") >/dev/null 2>&1
}
mkdir -p "$WORK/rel-ok" "$WORK/rel-ref" "$WORK/rel-dirty"; : > "$WORK/rel-dirty/leftover"
if release_checkout "$WORK/rel-ok" refs/heads/main "$sha2" \
   && [ "$(git -C "$WORK/rel-ok" rev-parse HEAD)" = "$sha2" ] \
   && [ "$(git -C "$WORK/rel-ok" symbolic-ref --short HEAD)" = main ] \
   && [ "$(git -C "$WORK/rel-ok" tag)" = v1.0.0 ] \
   && [ "$(git -C "$WORK/rel-ok" rev-list --count HEAD)" = 2 ]; then ok; else no "release checkout: main, sha, tags, history"; fi
if release_checkout "$WORK/rel-ref" refs/heads/other "$sha2"; then no "release checkout off main"; else ok; fi
if release_checkout "$WORK/rel-dirty" refs/heads/main "$sha2"; then no "release checkout over a non-empty workspace"; else ok; fi

# The header's key, as rendered, reaches the forge with or without server_url's trailing slash,
# and no other host.
key=$(python3 -c "import sys; sys.path.insert(0, '$ROOT/forgejo'); import render; print(render.CHECKOUT['env']['GIT_CONFIG_KEY_0'])")
for server in https://forge.example https://forge.example/; do
  k=${key//'${{ github.server_url }}'/$server}
  match() { GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="$k" GIT_CONFIG_VALUE_0=v git config --get-urlmatch http.extraHeader "$1"; }
  if [ "$(match "${server}/o/r.git")" = v ] && [ "$(match https://forge.example/o/r.git)" = v ] \
     && [ -z "$(match https://github.com/o/r.git)" ] && [ -z "$(match https://forge.example.evil/o/r.git)" ]; then ok
  else no "header scope with server_url $server"; fi
done

# sync.sh against fake clones: write, then --check clean, then --check catches a hand edit.
clones="$WORK/clones"
while IFS=$'\t' read -r repo _; do
  case "$repo" in ''|'#'*) continue ;; esac
  mkdir -p "$clones/$repo/.git"
done < "$ROOT/forgejo/crates.tsv"
export RENDER_REF=HEAD  # CI's checkout is shallow and carries no v1 tag
if "$ROOT/forgejo/sync.sh" "$clones" >/dev/null; then ok; else no "sync.sh write"; fi
if "$ROOT/forgejo/sync.sh" --check "$clones" >/dev/null; then ok; else no "--check after write"; fi
# One nightly line for every crate (spec §1 Triggers): the in-flight cap serialises them.
crons=$(grep -h -A1 'schedule:' "$clones"/*/.forgejo/workflows/ci.yml | grep cron | sed 's/^ *//' | sort -u)
ncrates=$(find "$clones" -mindepth 1 -maxdepth 1 -type d | wc -l)
nscheduled=$(grep -l -A1 'schedule:' "$clones"/*/.forgejo/workflows/ci.yml | wc -l)
if [ "$crons" = "- cron: 30 0 * * *" ] && [ "$nscheduled" -eq "$ncrates" ]; then ok; else no "not one 00:30 line for every crate: $crons"; fi
first=$(ls "$clones" | head -1)
echo '# hand edit' >> "$clones/$first/.forgejo/workflows/ci.yml"
if out=$("$ROOT/forgejo/sync.sh" --check "$clones"); then no "--check missed a hand edit"; else
  case "$out" in *"stale: $clones/$first/"*) ok ;; *) no "--check named the wrong file: $out" ;; esac
fi
mkdir -p "$WORK/empty"
if "$ROOT/forgejo/sync.sh" "$WORK/empty" >/dev/null 2>&1; then no "sync.sh wrote into a non-clone"; else ok; fi

echo "sync: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
