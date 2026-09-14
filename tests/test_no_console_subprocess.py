import ast
import json
import os
import subprocess
from pathlib import Path

from subprocess_utils import CREATE_NO_WINDOW, background_process_kwargs, visible_gui_process_kwargs


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_external_processes_hide_windows_and_do_not_use_shell():
    violations = []
    for path in ROOT.glob("*.py"):
        if path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    if node.func.value.id == "subprocess" and node.func.attr in {"run", "Popen", "call"}:
                        segment = ast.get_source_segment(source, node) or ""
                        if not any(token in segment for token in ("background_process_kwargs", "visible_gui_process_kwargs", "creationflags", "startupinfo")):
                            violations.append(f"{path.name}:{node.lineno}: hidden-window args missing")
                    if node.func.value.id == "os" and node.func.attr in {"system", "popen"}:
                        violations.append(f"{path.name}:{node.lineno}: os.{node.func.attr} is forbidden")
                if isinstance(node.func, ast.Attribute) and node.func.attr in {"run", "Popen", "call"}:
                    if any(keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in node.keywords):
                        violations.append(f"{path.name}:{node.lineno}: shell=True is forbidden")
    assert violations == [], "\n".join(violations)


def test_background_launch_is_hidden_and_writes_redacted_diagnostic(tmp_path, monkeypatch):
    log_path = tmp_path / "process-launch.log"
    monkeypatch.setenv("MIXCUT_PROCESS_LAUNCH_LOG", str(log_path))
    command = ["ffprobe.exe", "-version", "--token", "top-secret"]

    options = background_process_kwargs(command, "export_validation")

    if os.name == "nt":
        assert options["creationflags"] == CREATE_NO_WINDOW
        assert options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["stage"] == "export_validation"
    assert record["hidden_window"] is (os.name == "nt")
    assert "top-secret" not in record["command"]
    assert "<redacted>" in record["command"]


def test_visible_gui_launch_suppresses_console_without_hiding_window():
    options = visible_gui_process_kwargs(["chrome.exe", "--app=http://127.0.0.1/"])
    if os.name == "nt":
        assert options == {"creationflags": CREATE_NO_WINDOW}
        assert "startupinfo" not in options
    else:
        assert options == {}
