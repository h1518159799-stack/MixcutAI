from pathlib import Path

import commercial_api as api


def test_material_root_validation_accepts_empty_and_unicode_paths(tmp_path: Path):
    roots = [
        tmp_path / "素材",
        tmp_path / "视频素材" / "测试",
        tmp_path / "Desktop Videos",
        tmp_path / "特殊 字符 [01]",
    ]
    for root in roots:
        root.mkdir(parents=True)
        state = api._inspect_material_directory(root)
        assert state["exists"] is True
        assert state["readable"] is True
        assert state["error_code"] == ""


def test_material_root_validation_does_not_require_video_files(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    state = api._inspect_material_directory(root)
    assert state["exists"] is True
    assert state["readable"] is True


def test_material_root_validation_distinguishes_missing_directory(tmp_path: Path):
    state = api._inspect_material_directory(tmp_path / "does-not-exist")
    assert state["exists"] is False
    assert state["error_code"] == "MATERIAL_ROOT_NOT_FOUND"
    assert state["message"] == "素材目录不存在，请重新选择"


def test_material_root_status_reports_no_video_without_invalidating_directory(tmp_path, monkeypatch):
    root = tmp_path / "中文目录"
    root.mkdir()
    monkeypatch.setattr(api, "settings", lambda: {"material_root": str(root)})
    state = api.material_root_status()
    assert state["exists"] is True
    assert state["readable"] is True
    assert state["has_videos"] is False
    assert state["message"] == "该目录暂无可用视频素材"


def test_material_root_validation_reports_permission_error(monkeypatch, tmp_path):
    root = tmp_path / "protected"
    root.mkdir()
    original = Path.iterdir

    def denied(self):
        if self == root:
            raise PermissionError("denied")
        return original(self)

    monkeypatch.setattr(Path, "iterdir", denied)
    state = api._inspect_material_directory(root)
    assert state["exists"] is True
    assert state["readable"] is False
    assert state["error_code"] == "MATERIAL_ROOT_PERMISSION_DENIED"
    assert state["message"] == "当前目录没有读取权限，请更换目录"
