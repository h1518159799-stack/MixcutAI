import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import commercial_launcher


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        payload = json.dumps({"ok": self.path == "/api/health"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_backend_wait_uses_real_health_endpoint(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    events = []
    monkeypatch.setattr(commercial_launcher, "startup_log", lambda event, **_fields: events.append(event))
    thread.start()
    try:
        commercial_launcher.wait_for_backend_health(server.server_port, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
    assert "backend_healthy" in events


def test_backend_wait_has_bounded_failure(monkeypatch):
    monkeypatch.setattr(commercial_launcher.time, "sleep", lambda _seconds: None)
    try:
        commercial_launcher.wait_for_backend_health(1, timeout=0.01)
    except RuntimeError as exc:
        assert "健康检查超时" in str(exc)
    else:
        raise AssertionError("unavailable backend was accepted")


def test_ui_browser_profile_is_unique_per_launcher_process():
    source = open(commercial_launcher.__file__, encoding="utf-8").read()
    assert 'f"commercial-ui-chromium-profile-{profile_key}-{os.getpid()}"' in source
    assert "FRONTEND_CONNECTED_EVENT.wait" in source
    assert 'visible_gui_process_kwargs(command, "application_browser")' in source
    assert "close_stale_mixcut_ui_processes()" in source


def test_launcher_first_start_succeeds_ten_times(tmp_path, monkeypatch):
    class _Row:
        def fetchone(self):
            return (7,)

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            return _Row()

    class _BrowserProcess:
        pid = 1234
        returncode = None

        def poll(self):
            return None

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(commercial_launcher, "DIST_DIR", dist)
    monkeypatch.setattr(commercial_launcher, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(commercial_launcher, "APP_DIR", tmp_path)
    monkeypatch.setattr(commercial_launcher, "SplashWindow", lambda: None)
    monkeypatch.setattr(commercial_launcher, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(commercial_launcher, "ensure_material_workspace", lambda: None)
    monkeypatch.setattr(commercial_launcher, "prepare", lambda: None)
    monkeypatch.setattr(commercial_launcher, "require_activation", lambda: True)
    monkeypatch.setattr(commercial_launcher, "show_expiry_reminder", lambda _days: None)
    monkeypatch.setattr(commercial_launcher, "connect", lambda: _Connection())
    monkeypatch.setattr(commercial_launcher, "find_chromium", lambda: tmp_path / "chrome.exe")
    monkeypatch.setattr(commercial_launcher, "wait_for_browser_exit", lambda *_args: 0)
    monkeypatch.setattr(commercial_launcher, "startup_log", lambda *_args, **_kwargs: None)

    def open_and_connect(*_args, **_kwargs):
        commercial_launcher.FRONTEND_CONNECTED_EVENT.set()
        return _BrowserProcess()

    monkeypatch.setattr(commercial_launcher.subprocess, "Popen", open_and_connect)
    for _ in range(10):
        assert commercial_launcher.main() == 0


def test_launcher_fatal_error_is_visible_and_logged(monkeypatch):
    events = []
    dialogs = []
    monkeypatch.setattr(commercial_launcher, "SplashWindow", lambda: None)
    monkeypatch.setattr(
        commercial_launcher,
        "ensure_runtime_dirs",
        lambda: (_ for _ in ()).throw(RuntimeError("测试启动错误")),
    )
    monkeypatch.setattr(
        commercial_launcher,
        "startup_exception",
        lambda event, exc, **_fields: events.append((event, str(exc))),
    )
    monkeypatch.setattr(
        commercial_launcher,
        "show_startup_failure",
        lambda exc: dialogs.append(str(exc)),
    )

    assert commercial_launcher.main() == 1
    assert events == [("launcher_fatal", "测试启动错误")]
    assert dialogs == ["测试启动错误"]


def test_stale_ui_cleanup_only_uses_scoped_scan_results(monkeypatch):
    closed = []
    monkeypatch.setattr(commercial_launcher, "stale_mixcut_ui_processes", lambda: [101, 202])

    class _Kernel:
        def OpenProcess(self, _access, _inherit, pid):
            return pid

        def TerminateProcess(self, handle, _code):
            closed.append(handle)
            return 1

        def WaitForSingleObject(self, _handle, _timeout):
            return 0

        def CloseHandle(self, _handle):
            return 1

    monkeypatch.setattr(commercial_launcher.ctypes.windll, "kernel32", _Kernel())
    monkeypatch.setattr(commercial_launcher, "startup_log", lambda *_args, **_kwargs: None)

    assert commercial_launcher.close_stale_mixcut_ui_processes() == [101, 202]
    assert closed == [101, 202]
