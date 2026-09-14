"""Token-authenticated public gateway for the local Cockpit API service.

The Cockpit sidecar remains bound to loopback.  This process is the only
component that may listen on a public interface and it exposes only /v1/*.
Customer tokens are stored as SHA-256 digests, never written to access logs.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Cockpit Tools" / "gateway" / "customers.json"
DEFAULT_LOG = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Cockpit Tools" / "gateway" / "access.jsonl"
DEFAULT_UPSTREAM = "http://127.0.0.1:53417/v1"
MAX_BODY_BYTES = 20 * 1024 * 1024


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token_fingerprint(value: str) -> str:
    return _sha256(value)[:12]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"客户Token配置不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("customers"), list):
        raise RuntimeError("客户Token配置格式无效")
    return data


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}

    def allow(self, customer_id: str, rpm: int) -> bool:
        now = time.monotonic()
        limit = max(1, int(rpm))
        with self._lock:
            events = self._events.setdefault(customer_id, deque())
            while events and now - events[0] >= 60:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True


class GatewayState:
    def __init__(self, config_path: Path, log_path: Path, upstream_url: str, upstream_key: str) -> None:
        self.config_path = config_path
        self.log_path = log_path
        self.upstream_url = upstream_url.rstrip("/")
        self.upstream_key = upstream_key
        self.rate_limiter = RateLimiter()
        self.log_lock = threading.Lock()

    def customer_for_token(self, token: str) -> dict[str, Any] | None:
        digest = _sha256(token)
        config = load_config(self.config_path)
        for customer in config["customers"]:
            if not isinstance(customer, dict) or not customer.get("enabled", True):
                continue
            stored = str(customer.get("token_sha256") or "").lower()
            if stored and hmac.compare_digest(stored, digest):
                return customer
        return None

    def write_log(self, *, customer: dict[str, Any] | None, token: str, model: str, usage: Any, status: int) -> None:
        record = {
            "time": _utc_now(),
            "customer_id": str((customer or {}).get("id") or "unknown"),
            "token_fingerprint": _token_fingerprint(token) if token else "",
            "model": model,
            "usage": usage,
            "status": status,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self.log_lock:
                with self.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
        except OSError:
            pass


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "CockpitAPIGateway/1.0"

    @property
    def state(self) -> GatewayState:
        return self.server.gateway_state  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        if not self.path.startswith("/v1/"):
            self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})
            return
        self._proxy(b"")

    def do_POST(self) -> None:
        if not self.path.startswith("/v1/"):
            self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": {"message": "请求体过大", "type": "invalid_request_error"}})
            return
        self._proxy(self.rfile.read(length))

    def _proxy(self, body: bytes) -> None:
        auth = str(self.headers.get("Authorization") or "")
        if not auth.startswith("Bearer "):
            self._send_json(401, {"error": {"message": "缺少API Token", "type": "authentication_error"}})
            return
        token = auth[7:].strip()
        customer = self.state.customer_for_token(token)
        if not customer:
            self._send_json(401, {"error": {"message": "API Token无效", "type": "authentication_error"}})
            return
        rpm = int(customer.get("requests_per_minute") or 60)
        if not self.state.rate_limiter.allow(str(customer.get("id")), rpm):
            self.state.write_log(customer=customer, token=token, model="", usage=None, status=429)
            self._send_json(429, {"error": {"message": "请求频率过高，请稍后再试", "type": "rate_limit_error"}})
            return

        upstream = self.state.upstream_url + self.path[3:] if self.path.startswith("/v1") else self.state.upstream_url + self.path
        headers = {"Authorization": f"Bearer {self.state.upstream_key}", "Accept": str(self.headers.get("Accept") or "application/json")}
        content_type = self.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(upstream, data=body or None, headers=headers, method=self.command)
        model = ""
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
            model = str(parsed.get("model") or "") if isinstance(parsed, dict) else ""
        except (ValueError, UnicodeDecodeError):
            pass
        status = 502
        response_body = b""
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                status = int(response.status)
                response_body = response.read()
                response_headers = response.headers
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            response_body = exc.read()
            response_headers = exc.headers
        except (urllib.error.URLError, TimeoutError, OSError):
            self.state.write_log(customer=customer, token=token, model=model, usage=None, status=502)
            self._send_json(502, {"error": {"message": "上游AI服务无法访问", "type": "upstream_error"}})
            return
        usage = None
        try:
            payload = json.loads(response_body.decode("utf-8"))
            usage = payload.get("usage") if isinstance(payload, dict) else None
        except (ValueError, UnicodeDecodeError):
            pass
        self.state.write_log(customer=customer, token=token, model=model, usage=usage, status=status)
        self.send_response(status)
        self.send_header("Content-Type", response_headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, *_args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Cockpit external API gateway")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("COCKPIT_GATEWAY_CONFIG", DEFAULT_CONFIG)))
    parser.add_argument("--log", type=Path, default=Path(os.environ.get("COCKPIT_GATEWAY_LOG", DEFAULT_LOG)))
    parser.add_argument("--host", default=os.environ.get("COCKPIT_GATEWAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("COCKPIT_GATEWAY_PORT", "53418")))
    parser.add_argument("--upstream", default=os.environ.get("COCKPIT_UPSTREAM_URL", DEFAULT_UPSTREAM))
    parser.add_argument("--upstream-key", default=os.environ.get("COCKPIT_UPSTREAM_API_KEY", ""))
    args = parser.parse_args()
    if not args.upstream_key:
        raise SystemExit("缺少 COCKPIT_UPSTREAM_API_KEY；不会尝试暴露内部账号池密钥")
    state = GatewayState(args.config, args.log, args.upstream, args.upstream_key)
    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    server.gateway_state = state  # type: ignore[attr-defined]
    print(f"Cockpit API Gateway listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
