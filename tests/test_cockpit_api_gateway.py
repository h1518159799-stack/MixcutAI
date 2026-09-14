import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from cockpit_api_gateway import GatewayHandler, GatewayState


class UpstreamHandler(BaseHTTPRequestHandler):
    seen_auth = ""

    def do_POST(self):
        UpstreamHandler.seen_auth = self.headers.get("Authorization", "")
        body = json.dumps({"id": "ok", "usage": {"total_tokens": 7}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_gateway_auth_rate_limit_and_redacted_log(tmp_path: Path):
    upstream = _server(UpstreamHandler)
    try:
        token = "customer_test_token"
        config = tmp_path / "customers.json"
        config.write_text(json.dumps({"customers": [{
            "id": "customer_001",
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "requests_per_minute": 1,
            "enabled": True,
        }]}), encoding="utf-8")
        state = GatewayState(config, tmp_path / "access.jsonl", f"http://127.0.0.1:{upstream.server_port}/v1", "internal-key")
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        gateway.gateway_state = state
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{gateway.server_port}/v1/chat/completions",
                data=b'{"model":"test-model"}',
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                assert response.status == 200
            assert UpstreamHandler.seen_auth == "Bearer internal-key"
            try:
                urlopen(request)
                assert False, "second request should be rate limited"
            except HTTPError as exc:
                assert exc.code == 429
            log = (tmp_path / "access.jsonl").read_text(encoding="utf-8")
            assert token not in log
            assert "customer_001" in log
            assert "test-model" in log
        finally:
            gateway.shutdown()
    finally:
        upstream.shutdown()

