#!/usr/bin/env bash
# HL-135: runs the real "Trigger deploy webhook" step of .github/workflows/deploy.yml — its `run:` block,
# extracted from the file at test time — against a scripted fake webhook, one scenario per case.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'kill $(jobs -p) 2>/dev/null || true; rm -rf "$WORK"' EXIT

python3 - "$ROOT/.github/workflows/deploy.yml" "$WORK/step.sh" <<'PY'
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1]))
step = next(s for s in wf["jobs"]["deploy"]["steps"] if s.get("name") == "Trigger deploy webhook")
assert set(step["env"]) == {"URL", "KEY", "RETRIES", "DELAY", "DEPLOY_TIMEOUT"}, step["env"]
assert "${{" not in step["run"], "the script must read env only"
open(sys.argv[2], "w").write(step["run"])
PY

pass=0 fail=0
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }

# case <name> <scenario> <expected exit> <expected output substring> <expected POSTs> [server start delay]
case_() {
  local name=$1 scenario=$2 want_exit=$3 want_text=$4 want_posts=$5 delay=${6:-0}
  local port log out code posts
  port=$(free_port); log="$WORK/$name.log"; out="$WORK/$name.out"; : > "$log"
  ( sleep "$delay"; exec python3 "$HERE/fake_webhook.py" "$port" "$scenario" "$log" ) &
  local server=$!
  [ "$delay" = 0 ] && for _ in $(seq 50); do (echo > "/dev/tcp/127.0.0.1/$port") 2>/dev/null && break; sleep 0.1; done
  code=0
  URL="http://127.0.0.1:$port/deploy" KEY=test RETRIES=5 DELAY=1 DEPLOY_TIMEOUT=4 \
    WAIT=1 POST_MAX_TIME=2 POLL_PAUSE=0 bash "$WORK/step.sh" > "$out" 2>&1 || code=$?
  kill "$server" 2>/dev/null || true; wait "$server" 2>/dev/null || true
  posts=$(grep -c '^POST ' "$log" || true)
  if [ "$code" = "$want_exit" ] && grep -qF -- "$want_text" "$out" && [ "$posts" = "$want_posts" ] \
     && { [ "$posts" = 0 ] || grep -q 'prefer=respond-async' "$log"; }; then
    echo "ok   $name"; pass=$((pass + 1))
  else
    echo "FAIL $name: exit $code (want $want_exit), POSTs $posts (want $want_posts), want output: $want_text"
    sed 's/^/     | /' "$out"; fail=$((fail + 1))
  fi
}

case_ sync_200            sync200        0 "Deploy successful (HTTP 200)"             1
case_ sync_500            sync500        1 "ROLLED BACK"                              1
case_ gated_202_no_id     gated          0 "accepted, not run"                        1
case_ running_202_no_id   running_no_id  1 "202 without a deploy_id"                  1
case_ compact_json_id     compact_json   0 "successful"                               1
case_ async_then_deployed async_ok       0 "successful"                               1
case_ async_then_error    async_err      1 "handover FAILED"                          1
case_ async_budget        async_budget   1 "still running after 4s. Not re-POSTed"    1
case_ async_then_404      async_404      1 "outcome unknown"                          1
case_ post_timeout_not_retried post_timeout 1 "NOT retried"                           1
case_ get_transport_retried    get_transport 0 "successful"                           1
case_ refused_then_retried     refused_then_ok 0 "Deploy successful (HTTP 200)"       1 2.5

echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
