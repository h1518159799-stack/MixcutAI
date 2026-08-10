"""Loopback OAuth callback listener shared by API platform authorization flows."""

from __future__ import annotations

import html
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


_LOCK = threading.Lock()
_SERVERS: dict[int, ThreadingHTTPServer] = {}
_PATH_PROVIDERS = {
    "/api/oauth/youtube/callback": "youtube",
    "/api/oauth/facebook-page/callback": "facebook_page",
    "/api/oauth/instagram-business/callback": "instagram_business",
    "/api/oauth/x/callback": "x",
}


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        provider = _PATH_PROVIDERS.get(parsed.path)
        title, message = "授权失败", "未知授权回调，已拒绝处理。"
        if provider:
            query = parse_qs(parsed.query)
            try:
                state = (query.get("state") or [""])[0]
                if query.get("error"):
                    from oauth_service import cancel_authorization
                    error_code = query["error"][0]
                    description = (query.get("error_description") or [""])[0]
                    message = cancel_authorization(state, error_code + ((": " + description) if description else ""))
                    title = "授权已取消" if error_code == "access_denied" else "授权失败"
                else:
                    from oauth_service import complete_authorization
                    result = complete_authorization(state, (query.get("code") or [""])[0], provider)
                    title = "授权信息已读取"
                    message = "请返回 MixcutAI 选择需要绑定的账号。此页面现在可以关闭。"
                    if result.get("status") == "authorized":
                        title, message = "授权成功", "你可以关闭此页面并返回 MixcutAI。"
            except Exception as exc:
                message = str(exc)
        body = (f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>{html.escape(title)}</title>"
                "<style>body{margin:0;background:#090d14;color:#e5e7eb;font:16px 'Microsoft YaHei UI',system-ui}"
                ".box{width:520px;max-width:calc(100vw - 48px);margin:14vh auto;padding:34px;background:#151c2b;"
                "border:1px solid #263044;border-radius:10px}h1{font-size:22px;color:#7da2ff;margin:0 0 14px}"
                "p{color:#aab4c6;line-height:1.8;margin:0}</style>"
                f"<div class='box'><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></div></html>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def callback_server_status(redirect_uri: str) -> dict:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or not parsed.port:
        return {"available": False, "message": "授权回调地址不是本机安全地址"}
    with _LOCK:
        if parsed.port in _SERVERS:
            return {"available": True, "running": True, "message": "回调服务已启动"}
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", parsed.port))
        return {"available": True, "running": False, "message": "回调端口可用"}
    except OSError:
        return {"available": False, "running": False, "message": "本机授权回调端口被占用"}
    finally:
        probe.close()


def ensure_callback_server(redirect_uri: str) -> bool:
    parsed = urlparse(redirect_uri)
    status = callback_server_status(redirect_uri)
    if not status["available"]:
        raise OSError(status["message"])
    with _LOCK:
        if parsed.port in _SERVERS:
            return True
        try:
            server = ThreadingHTTPServer(("127.0.0.1", parsed.port), CallbackHandler)
        except OSError as exc:
            raise OSError("本机授权回调端口被占用") from exc
        thread = threading.Thread(target=server.serve_forever, name="mixcut-oauth-callback", daemon=True)
        _SERVERS[parsed.port] = server
        thread.start()
    return True
