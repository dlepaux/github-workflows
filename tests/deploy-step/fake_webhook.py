"""A scripted homelab-webhook for tests/deploy-step/run.sh (HL-135). Usage: fake_webhook.py <port> <scenario> <log>.

Every request is appended to <log> as "METHOD PATH". Scenarios answer POST /deploy and GET /deploy/<id>
the way the webhook would in that situation."""
import http.server
import json
import sys
import time

PORT, SCENARIO, LOG = int(sys.argv[1]), sys.argv[2], sys.argv[3]
ID = "ab" * 32
polls = 0


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def record(self):
        with open(LOG, "a") as f:
            f.write(f"{self.command} {self.path} prefer={self.headers.get('Prefer', '')}\n")

    def do_POST(self):
        self.record()
        running = {"service": "svc", "status": "running", "deploy_id": ID}
        if SCENARIO in ("sync200", "refused_then_ok"):
            return self.reply(200, {"service": "svc", "status": "deployed", "deploy_id": ID})
        if SCENARIO == "sync500":
            return self.reply(500, {"status": "error", "message": "ROLLED BACK to image=sha256:old"})
        if SCENARIO == "running_no_id":
            return self.reply(202, {"service": "svc", "status": "running"})
        if SCENARIO == "compact_json":
            data = b'{"service":"svc","status":"running","deploy_id":"' + ID.encode() + b'"}'
            self.send_response(202)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if SCENARIO == "gated":
            return self.reply(202, {"service": "svc", "status": "accepted"})
        if SCENARIO == "post_timeout":
            time.sleep(5)
            return self.reply(200, {"status": "deployed"})
        return self.reply(202, running)

    def do_GET(self):
        global polls
        self.record()
        polls += 1
        if not self.path.startswith(f"/deploy/{ID}?wait="):
            return self.reply(400, {"status": "error", "message": f"unexpected path {self.path}"})
        if SCENARIO in ("async_ok", "compact_json"):
            return self.reply(202 if polls < 3 else 200, {"status": "running" if polls < 3 else "deployed", "deploy_id": ID})
        if SCENARIO == "async_err":
            return self.reply(500, {"status": "error", "message": "handover FAILED", "deploy_id": ID})
        if SCENARIO == "async_budget":
            time.sleep(0.5)
            return self.reply(202, {"status": "running", "deploy_id": ID})
        if SCENARIO == "async_404":
            return self.reply(404, {"status": "error", "message": "no such deploy for this key"})
        if SCENARIO == "get_transport":
            if polls == 1:
                self.close_connection = True
                return  # no response at all: curl exit 52
            return self.reply(200, {"status": "deployed", "deploy_id": ID})
        return self.reply(418, {})


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
