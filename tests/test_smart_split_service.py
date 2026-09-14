from __future__ import annotations

import sqlite3
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

import smart_split_api as api
import smart_split_service as service


def sample_segments():
    return service.build_segments(
        [{"time": 3.0, "score": 0.8}, {"time": 7.0, "score": 0.7}], 10.0
    )


def test_normalize_sorts_and_rejects_invalid_times():
    cuts = service.normalize_cut_points([8, -1, 3, 12], 10, 0.5)
    assert [item["time"] for item in cuts] == [3, 8]


def test_nearby_cut_cluster_keeps_highest_score():
    cuts = service.normalize_cut_points(
        [{"time": 4.31, "score": .2}, {"time": 4.42, "score": .9}, {"time": 4.58, "score": .4}], 10, .65
    )
    assert cuts == [{"time": 4.42, "score": .9}]


def test_build_segments_reaches_video_end():
    segments = sample_segments()
    assert segments[-1]["end_time"] == 10
    assert sum(item["duration"] for item in segments) == 10


def test_no_cut_returns_one_continuous_segment():
    segments = service.build_segments([], 6.5)
    assert len(segments) == 1 and segments[0]["duration"] == 6.5


def test_short_first_segment_merges_forward():
    result = service.merge_short_segments(service.build_segments([{"time": .4, "score": .7}], 4), 1.3)
    assert len(result) == 1 and result[0]["duration"] == 4


def test_short_last_segment_merges_backward():
    result = service.merge_short_segments(service.build_segments([{"time": 3.7, "score": .7}], 4), 1.3)
    assert len(result) == 1 and result[0]["duration"] == 4


def test_short_middle_segment_merges_to_longer_neighbor():
    source = service.build_segments([{"time": 3, "score": .8}, {"time": 3.4, "score": .5}], 8)
    result = service.merge_short_segments(source, 1.3)
    assert len(result) == 2 and result[0]["end_time"] == 3


def test_standard_defaults_preserve_one_second_real_shot():
    assert service.SENSITIVITY_THRESHOLDS["standard"] == 0.30
    assert service.DEFAULT_MIN_SEGMENT_DURATION == 1.0
    source = service.build_segments(
        [{"time": 3.0, "score": .8}, {"time": 4.1, "score": .7}],
        8.0,
    )
    result = service.merge_short_segments(source)
    assert [round(item["duration"], 3) for item in result] == [3.0, 1.1, 3.9]


def test_safe_stem_removes_windows_illegal_characters():
    assert service._safe_stem('a<b>:c/"d|e?f*') == "a_b_c_d_e_f"


def test_4k_export_profile_limits_x264_threads():
    options = service._export_video_options(3840, 2160)
    assert options[options.index("-preset") + 1] == "veryfast"
    assert options[options.index("-threads") + 1] == "4"


def test_1080p_export_profile_keeps_quality_preset():
    options = service._export_video_options(1920, 1080)
    assert options[options.index("-preset") + 1] == "medium"
    assert "-threads" not in options


@pytest.fixture()
def split_db(tmp_path, monkeypatch):
    database = tmp_path / "split.db"

    class ClosingConnection(sqlite3.Connection):
        def __exit__(self, exc_type, exc_value, traceback):
            try:
                return super().__exit__(exc_type, exc_value, traceback)
            finally:
                self.close()

    def connect():
        conn = sqlite3.connect(database, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT);
            CREATE TABLE smart_split_jobs(
              id INTEGER PRIMARY KEY,source_path TEXT,source_hash TEXT DEFAULT '',source_size INTEGER DEFAULT 0,
              source_mtime_ns INTEGER DEFAULT 0,source_duration REAL DEFAULT 10,source_width INTEGER DEFAULT 1280,
              source_height INTEGER DEFAULT 720,source_fps REAL DEFAULT 30,has_audio INTEGER DEFAULT 1,
              sensitivity TEXT DEFAULT 'standard',threshold REAL DEFAULT .32,min_segment_duration REAL DEFAULT 1.3,
              min_cut_gap REAL DEFAULT .65,status TEXT DEFAULT 'waiting_confirmation',stage TEXT DEFAULT '',
              progress INTEGER DEFAULT 100,output_dir TEXT DEFAULT '',exported_count INTEGER DEFAULT 0,
              failed_count INTEGER DEFAULT 0,cancel_requested INTEGER DEFAULT 0,error_message TEXT DEFAULT '',
              created_at TEXT,updated_at TEXT,finished_at TEXT DEFAULT '');
            CREATE TABLE smart_split_segments(
              id INTEGER PRIMARY KEY AUTOINCREMENT,job_id INTEGER,segment_index INTEGER,start_time REAL,end_time REAL,
              duration REAL,scene_score REAL,selected INTEGER,category TEXT,thumbnail_path TEXT,output_path TEXT,
              status TEXT,error_message TEXT,created_at TEXT,updated_at TEXT,
              UNIQUE(job_id,segment_index),FOREIGN KEY(job_id) REFERENCES smart_split_jobs(id) ON DELETE CASCADE);
            CREATE TABLE material_categories(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE,description TEXT,sort_order INTEGER,created_at TEXT,updated_at TEXT);
            CREATE TABLE materials(id INTEGER PRIMARY KEY AUTOINCREMENT,category_id INTEGER,file_path TEXT UNIQUE,status TEXT,updated_at TEXT);
            INSERT INTO smart_split_jobs(id,source_path,status,created_at,updated_at) VALUES(1,'source.mp4','waiting_confirmation','x','x');
            """
        )
        for item in sample_segments():
            conn.execute(
                """INSERT INTO smart_split_segments(job_id,segment_index,start_time,end_time,duration,scene_score,selected,category,thumbnail_path,output_path,status,error_message,created_at,updated_at)
                   VALUES(1,?,?,?,?,?,1,'','','','detected','','x','x')""",
                (item["segment_index"], item["start_time"], item["end_time"], item["duration"], item["scene_score"]),
            )
    monkeypatch.setattr(service, "connect", connect)
    monkeypatch.setattr(api, "connect", connect)
    monkeypatch.setattr(service, "generate_preview_thumbnails", lambda _job, _source, values, *_: values)
    return database, connect


def test_job_reload_contains_all_segments(split_db):
    assert service.get_job(1)["segment_count"] == 3


def test_set_all_selected_off(split_db):
    assert service.set_all_selected(1, False)["selected_count"] == 0


def test_set_all_selected_on(split_db):
    service.set_all_selected(1, False)
    assert service.set_all_selected(1, True)["selected_count"] == 3


def test_segment_selection_persists(split_db):
    segment = service.get_job(1)["segments"][0]
    assert service.update_segment(1, segment["id"], {"selected": False})["segments"][0]["selected"] == 0


def test_segment_category_persists(split_db):
    segment = service.get_job(1)["segments"][0]
    assert service.update_segment(1, segment["id"], {"category": "产品"})["segments"][0]["category"] == "产品"


def test_bulk_category_only_updates_selected(split_db):
    segments = service.get_job(1)["segments"]
    service.update_segment(1, segments[1]["id"], {"selected": False})
    result = service.set_selected_category(1, "空镜")
    assert [item["category"] for item in result["segments"]] == ["空镜", "", "空镜"]


def test_manual_split_creates_two_segments(split_db):
    result = service.split_segment(1, 1.5)
    assert result["segment_count"] == 4
    assert result["segments"][0]["end_time"] == 1.5


def test_split_rejects_boundary_position(split_db):
    with pytest.raises(service.SmartSplitError):
        service.split_segment(1, .01)


def test_merge_two_adjacent_segments(split_db):
    segments = service.get_job(1)["segments"]
    result = service.merge_segments(1, [segments[0]["id"], segments[1]["id"]])
    assert result["segment_count"] == 2 and result["segments"][0]["end_time"] == 7


def test_merge_rejects_non_adjacent_segments(split_db):
    segments = service.get_job(1)["segments"]
    with pytest.raises(service.SmartSplitError):
        service.merge_segments(1, [segments[0]["id"], segments[2]["id"]])


def test_edit_boundary_updates_adjacent_segment(split_db):
    segments = service.get_job(1)["segments"]
    result = service.update_segment(1, segments[1]["id"], {"start_time": 3.2})
    assert result["segments"][0]["end_time"] == 3.2
    assert result["segments"][1]["start_time"] == 3.2


def test_invalid_edit_overlap_is_rejected(split_db):
    segment = service.get_job(1)["segments"][1]
    with pytest.raises(service.SmartSplitError):
        service.update_segment(1, segment["id"], {"start_time": 8})


def test_cancel_marks_request_and_terminates_process(split_db, monkeypatch):
    class Process:
        terminated = False
        def poll(self): return None
        def terminate(self): self.terminated = True
    process = Process()
    monkeypatch.setitem(service._PROCESSES, 1, process)
    result = service.cancel_job(1)
    assert result["cancel_requested"] == 1 and process.terminated


def test_cancelled_export_can_be_started_again(split_db, monkeypatch):
    _, connect = split_db
    with connect() as conn:
        conn.execute("UPDATE smart_split_jobs SET status='cancelled',cancel_requested=1 WHERE id=1")

    class Thread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(service.threading, "Thread", Thread)
    result = service.start_export(1)
    assert result["status"] == "exporting"
    assert result["cancel_requested"] == 0


def test_open_output_directory_is_non_blocking_and_logged(split_db, tmp_path, monkeypatch):
    _, connect = split_db
    output = tmp_path / "split output"
    output.mkdir()
    with connect() as conn:
        conn.execute("UPDATE smart_split_jobs SET output_dir=?,status='completed' WHERE id=1", (str(output),))
    launched = Mock()
    monkeypatch.setattr(service.subprocess, "Popen", launched)
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(service.os, "name", "nt")

    result = service.open_output_directory(1)

    assert result == {"opened": True, "path": str(output.resolve())}
    command = launched.call_args.args[0]
    assert command == ["explorer.exe", str(output.resolve())]
    assert launched.call_args.kwargs["creationflags"] == getattr(service.subprocess, "CREATE_NO_WINDOW", 0)
    record = json.loads((tmp_path / "logs" / "smart-split-open-folder.log").read_text(encoding="utf-8"))
    assert record["event"] == "open_folder_called"
    assert record["folder_path"] == str(output.resolve())
    assert record["result"] == "opened"
    assert record["exception"] == ""


def test_open_output_directory_failure_is_logged(split_db, tmp_path, monkeypatch):
    _, connect = split_db
    output = tmp_path / "split output"
    output.mkdir()
    with connect() as conn:
        conn.execute("UPDATE smart_split_jobs SET output_dir=?,status='completed' WHERE id=1", (str(output),))
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(service.os, "name", "nt")
    monkeypatch.setattr(service.subprocess, "Popen", Mock(side_effect=OSError("Explorer unavailable")))

    with pytest.raises(service.SmartSplitError, match="无法打开输出目录"):
        service.open_output_directory(1)

    record = json.loads((tmp_path / "logs" / "smart-split-open-folder.log").read_text(encoding="utf-8"))
    assert record["result"] == "failed"
    assert "Explorer unavailable" in record["exception"]


def test_smart_split_open_output_keeps_react_job_state():
    source = (Path(__file__).resolve().parents[1] / "ui-commercial" / "src" / "SmartSplitPage.jsx").read_text(encoding="utf-8")
    assert 'if(name==="open-output"){notify("已打开输出目录");return}setJob(result.job||result)' in source
    assert "window.open" not in source
    assert "window.location" not in source
    assert "location.href" not in source


def test_recovery_marks_interrupted_job_failed(split_db):
    _, connect = split_db
    with connect() as conn:
        conn.execute("UPDATE smart_split_jobs SET status='detecting' WHERE id=1")
    assert service.recover_smart_split_jobs() == 1
    assert service.get_job(1)["status"] == "failed"


def test_register_outputs_uses_existing_material_scanner(split_db, tmp_path):
    _, connect = split_db
    output = tmp_path / "clip.mp4"
    output.write_bytes(b"video")
    with connect() as conn:
        segment_id = conn.execute("SELECT id FROM smart_split_segments WHERE job_id=1 ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE smart_split_segments SET output_path=?,status='completed',category='产品' WHERE id=?", (str(output), segment_id))
    def scanner(_folder):
        with connect() as conn:
            conn.execute("INSERT OR IGNORE INTO materials(file_path,status,updated_at) VALUES(?,'active','x')", (str(output.resolve()),))
        return {"added": 1}
    result = api.register_outputs(1, scanner)
    assert result["registered"] == 1


def test_register_outputs_is_idempotent(split_db, tmp_path):
    _, connect = split_db
    output = tmp_path / "clip.mp4"
    output.write_bytes(b"video")
    with connect() as conn:
        segment_id = conn.execute("SELECT id FROM smart_split_segments WHERE job_id=1 ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE smart_split_segments SET output_path=?,status='completed' WHERE id=?", (str(output), segment_id))
    def scanner(_folder):
        with connect() as conn:
            conn.execute("INSERT OR IGNORE INTO materials(file_path,status,updated_at) VALUES(?,'active','x')", (str(output.resolve()),))
        return {"added": 0}
    api.register_outputs(1, scanner)
    api.register_outputs(1, scanner)
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM materials WHERE file_path=?", (str(output.resolve()),)).fetchone()[0] == 1
