#!/usr/bin/env bash
# HL-135: runs the real "Trigger deploy webhook" step of .github/workflows/deploy.yml — its `run:` block,
# extracted from the file at test time — against a scripted fake webhook, one scenario per case.
#
# Also (2026-10-05): the key never goes on curl's command line. On a self-hosted runner
# /proc/<pid>/cmdline is readable by every user, and the step ran `curl -H "Authorization: Bearer
# <key>"` for the POST and each poll. A `curl` wrapper first on PATH logs every argument list it is
# given; no list may hold the key, and every request the webhook receives must still carry it.
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
# The credentials step must not paste the secret into its script's text either: the runner writes
# that text to a file, and an expression there is also an injection point.
creds = next(s for s in wf["jobs"]["deploy"]["steps"] if s.get("id") == "creds")
assert "${{" not in creds["run"], "the credentials step must take the secret and the input from its env"
PY

# A distinctive key, and a curl that logs its arguments before it runs.
KEY_VALUE="k3y$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
REAL_CURL="$(command -v curl)"
mkdir "$WORK/bin"
cat > "$WORK/bin/curl" <<WRAP
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$WORK/curl-argv.log"
exec "$REAL_CURL" "\$@"
WRAP
chmod +x "$WORK/bin/curl"

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
  PATH="$WORK/bin:$PATH" URL="http://127.0.0.1:$port/deploy" KEY="${CASE_KEY:-$KEY_VALUE}" RETRIES=5 DELAY=1 DEPLOY_TIMEOUT=4 \
    WAIT=1 POST_MAX_TIME=2 POLL_PAUSE=0 bash "$WORK/step.sh" > "$out" 2>&1 || code=$?
  kill "$server" 2>/dev/null || true; wait "$server" 2>/dev/null || true
  posts=$(grep -c '^POST ' "$log" || true)
  # Every request that reached the webhook carried the key.
  local unkeyed; unkeyed=$(grep -cvF "auth=Bearer ${CASE_KEY:-$KEY_VALUE}" "$log" || true)
  if [ "$code" = "$want_exit" ] && grep -qF -- "$want_text" "$out" && [ "$posts" = "$want_posts" ] \
     && { [ "$posts" = 0 ] || grep -q 'prefer=respond-async' "$log"; } && [ "$unkeyed" = 0 ]; then
    echo "ok   $name"; pass=$((pass + 1))
  else
    echo "FAIL $name: exit $code (want $want_exit), POSTs $posts (want $want_posts), requests without the key $unkeyed, want output: $want_text"
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
# A key curl's config syntax cannot carry is refused before any request (the vault's keys are hex).
CASE_KEY='bad"key' case_ unquotable_key_refused sync200 1 "cannot be sent safely" 0

# Across every case above: the key was never an argument of curl.
if [ -s "$WORK/curl-argv.log" ] && ! grep -qF -- "$KEY_VALUE" "$WORK/curl-argv.log"; then
  echo "ok   key_never_in_curl_argv ($(wc -l < "$WORK/curl-argv.log" | tr -d ' ') curl calls read)"; pass=$((pass + 1))
else
  echo "FAIL key_never_in_curl_argv: the key appeared in curl's arguments:"
  grep -F -- "$KEY_VALUE" "$WORK/curl-argv.log" | cut -c1-60 | sed 's/^/     | /'; fail=$((fail + 1))
fi

echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
