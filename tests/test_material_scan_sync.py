from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_incremental_material_scan_end_to_end(tmp_path: Path):
    app_home = tmp_path / "app-home"
    material_root = tmp_path / "materials"
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        os.environ['MIXCUT_APP_HOME'] = r'{app_home}'

        import commercial_api as api
        from db import THUMB_DIR, connect, init_db

        root = Path(r'{material_root}')
        (root / '产品').mkdir(parents=True)
        (root / '门头').mkdir(parents=True)
        files = [root/'产品'/'a.mp4', root/'门头'/'b.mp4', root/'门头'/'c.mp4']
        for index, path in enumerate(files, 1):
            path.write_bytes((f'video-{{index}}'.encode()) * (20 + index))

        def fake_probe(path, _configured=''):
            if path.name == 'bad.mp4':
                return {{'duration':0.0,'width':0,'height':0,'fps':0.0,'size':path.stat().st_size,'codec':'','error':'素材无法读取或视频格式异常'}}
            size = path.stat().st_size
            return {{'duration':float(size)/10,'width':1080,'height':1920,'fps':30.0,'size':size,'codec':'h264','error':''}}

        def fake_thumb(path, digest, _configured=''):
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            target = THUMB_DIR / f'{{digest[:16]}}.jpg'
            target.write_bytes(b'jpg-' + digest.encode())
            return str(target), ''

        api.probe_video = fake_probe
        api.make_thumbnail_with_status = fake_thumb
        init_db()

        first = api.scan_materials(str(root))
        assert first['added'] == 3 and first['total'] == 3
        with connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM materials WHERE status='active'").fetchone()[0] == 3
            custom = conn.execute("SELECT id FROM material_categories WHERE name='产品'").fetchone()[0]
            a_id = conn.execute("SELECT id FROM materials WHERE file_name='a.mp4'").fetchone()[0]
            conn.execute("UPDATE materials SET category_id=?,remark='人工备注' WHERE id=?", (custom, a_id))

        again = api.scan_materials(str(root))
        assert again['added'] == 0 and again['updated'] == 0 and again['unchanged'] == 3

        (root/'门头'/'d.mp4').write_bytes(b'new-d' * 37)
        (root/'门头'/'e.mp4').write_bytes(b'new-e' * 41)
        added = api.scan_materials(str(root))
        assert added['added'] == 2 and added['total'] == 5

        old_duration = None
        with connect() as conn:
            old_duration = conn.execute("SELECT duration FROM materials WHERE file_name='a.mp4'").fetchone()[0]
        files[0].write_bytes(b'replaced-video-content' * 55)
        replaced = api.scan_materials(str(root))
        assert replaced['updated'] == 1
        with connect() as conn:
            row = conn.execute("SELECT duration,remark,category_id,codec FROM materials WHERE file_name='a.mp4'").fetchone()
            assert row['duration'] != old_duration
            assert row['remark'] == '人工备注'
            assert row['category_id'] == custom
            assert row['codec'] == 'h264'

        files[1].unlink()
        removed = api.scan_materials(str(root))
        assert removed['missing'] == 1
        with connect() as conn:
            assert conn.execute("SELECT status FROM materials WHERE file_name='b.mp4'").fetchone()[0] == 'missing'
            assert conn.execute("SELECT COUNT(*) FROM materials WHERE status='active'").fetchone()[0] == 4

        (root/'门头'/'bad.mp4').write_bytes(b'not-a-video')
        damaged = api.scan_materials(str(root))
        assert damaged['errors'] == 1
        with connect() as conn:
            assert conn.execute("SELECT status FROM materials WHERE file_name='bad.mp4'").fetchone()[0] == 'error'
            assert conn.execute("SELECT COUNT(*) FROM materials WHERE status='active'").fetchone()[0] == 4
            assert conn.execute("SELECT value FROM app_settings WHERE key='material_root'").fetchone()[0] == str(root.resolve())

        assert api.MATERIAL_SCAN_LOCK.acquire(blocking=False)
        try:
            try:
                api.scan_materials(str(root))
                raise AssertionError('concurrent scan was not blocked')
            except ValueError as exc:
                assert '正在扫描' in str(exc)
        finally:
            api.MATERIAL_SCAN_LOCK.release()
        """
    )
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr


def test_assets_page_has_distinct_scan_and_change_directory_actions():
    source = (Path(__file__).resolve().parents[1] / "ui-commercial" / "src" / "main.jsx").read_text(encoding="utf-8")
    assert 'onClick={scanCurrent}' in source
    assert 'onClick={chooseRoot}' in source
    assert 'await refresh();' in source
    assert '正在扫描素材，请稍候' in source
    assert '查看异常' in source
