"""Small loopback OAuth callback listener, independent from the UI's random port."""

from __future__ import annotations

import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


_LOCK = threading.Lock()
_SERVERS: dict[int, ThreadingHTTPServer] = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if query.get("error"):
                from oauth_service import cancel_authorization
                state = (query.get("state") or [""])[0]
                error_code = query["error"][0]
                description = (query.get("error_description") or [""])[0]
                detail = error_code + ((": " + description) if description else "")
                raise ValueError(cancel_authorization(state, detail))
            from oauth_service import complete_authorization

            result = complete_authorization((query.get("state") or [""])[0], (query.get("code") or [""])[0])
            title, message = "授权成功", f"{result['platform']} · {result['account']} 已完成授权，可以关闭此页面。"
        except Exception as exc:
            title, message = "授权失败", str(exc)
        body = (f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
                f"<style>body{{font:16px system-ui;padding:50px;color:#27364d}}h1{{color:#367fd8}}</style>"
                f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def ensure_callback_server(redirect_uri: str) -> bool:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or not parsed.port:
        return False
    with _LOCK:
        if parsed.port in _SERVERS:
            return True
        server = ThreadingHTTPServer(("127.0.0.1", parsed.port), CallbackHandler)
        thread = threading.Thread(target=server.serve_forever, name="mixcut-oauth-callback", daemon=True)
        _SERVERS[parsed.port] = server
        thread.start()
    return True
