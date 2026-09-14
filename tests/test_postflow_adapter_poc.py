from pathlib import Path
from unittest.mock import patch

from publishers.postflow_adapter import WechatChannelsPostFlowAdapter, resolve_postflow_python


def _postflow_tree(tmp_path: Path) -> Path:
    target = tmp_path / "postflow"
    source = target / "uploader" / "tencent_uploader" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("# test fixture\n", encoding="utf-8")
    return target


def test_postflow_is_registered_by_default():
    from publishers import get_publisher

    assert type(get_publisher("wechat_channels")).__name__ == "WechatChannelsPostFlowPublisher"


def test_poc_adapter_requires_existing_video(tmp_path):
    adapter = WechatChannelsPostFlowAdapter(_postflow_tree(tmp_path))
    result = adapter.publish(str(tmp_path / "missing.mp4"), "title")
    assert result["error_code"] == "FILE_NOT_FOUND"

def test_source_mode_falls_back_to_sys_executable(tmp_path):
    assert resolve_postflow_python(_postflow_tree(tmp_path))

def test_postflow_venv_python_is_selected(tmp_path):
    root = _postflow_tree(tmp_path)
    py = root / 'venv' / 'Scripts' / 'python.exe'; py.parent.mkdir(parents=True); py.write_bytes(b'')
    assert resolve_postflow_python(root) == str(py.resolve())

def test_explicit_python_is_selected(tmp_path):
    root = _postflow_tree(tmp_path)
    py = tmp_path / 'custom-python.exe'; py.write_bytes(b'')
    assert resolve_postflow_python(root, str(py)) == str(py.resolve())

def test_frozen_mode_rejects_mixcut_exe_without_python(tmp_path, monkeypatch):
    root = _postflow_tree(tmp_path)
    monkeypatch.setattr('publishers.postflow_adapter.sys.frozen', True, raising=False)
    with patch.dict('publishers.postflow_adapter.os.environ', {}, clear=False):
        try:
            resolve_postflow_python(root)
        except FileNotFoundError as exc:
            assert 'POSTFLOW_PYTHON_NOT_FOUND' in str(exc)
        else:
            raise AssertionError('frozen mode must reject MixcutAI.exe')


def test_poc_adapter_requires_storage_state(tmp_path):
    adapter = WechatChannelsPostFlowAdapter(_postflow_tree(tmp_path))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    result = adapter.publish(str(video), "title")
    assert result["error_code"] == "ACCOUNT_FILE_REQUIRED"


def test_poc_adapter_preserves_result_contract(tmp_path):
    adapter = WechatChannelsPostFlowAdapter(_postflow_tree(tmp_path))
    video = tmp_path / "video.mp4"
    account = tmp_path / "account.json"
    video.write_bytes(b"video")
    account.write_text("{}", encoding="utf-8")
    completed = type("Completed", (), {"returncode": 0, "stderr": "", "stdout": '{"success": true, "remote_id": "", "remote_url": "", "verified": false, "error_code": "SUBMIT_NOT_EXECUTED", "error_message": "dry run", "screenshot": "shot.png", "traceback": ""}\n'})()
    with patch("publishers.postflow_adapter.subprocess.run", return_value=completed) as run:
        result = adapter.publish(str(video), "title", account={"postflow_account_file": str(account)})
    assert result["success"] is True
    assert result["error_code"] == "SUBMIT_NOT_EXECUTED"
    assert run.call_args.kwargs["timeout"] == 900
    command = run.call_args.args[0]
    assert command[-2] == "--job"
    job_file = Path(command[-1])
    assert job_file.name == "postflow_job.json"
