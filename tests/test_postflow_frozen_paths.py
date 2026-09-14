import pytest
from publishers import postflow_adapter as adapter

def test_frozen_ignores_old_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(adapter.sys, 'executable', str(tmp_path / 'MixcutAI.exe'))
    monkeypatch.setenv('MIXCUT_POSTFLOW_ROOT', 'D:/Mixcut/PostFlow')
    monkeypatch.setenv('MIXCUT_POSTFLOW_PYTHON', 'C:/Python314/python.exe')
    root = tmp_path / 'runtime/postflow'
    python = root / 'python/python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    assert adapter.resolve_postflow_root('ignored') == root
    assert adapter.resolve_postflow_python('ignored', 'ignored') == str(python)
    python.unlink()
    with pytest.raises(FileNotFoundError, match='POSTFLOW_PYTHON_NOT_FOUND'):
        adapter.resolve_postflow_python('D:/Mixcut/PostFlow')
