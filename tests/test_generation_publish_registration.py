from pathlib import Path

import pytest

import db
import generation_success_service as success_service
import publish_metadata_service


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mixcut.db")
    monkeypatch.setattr(success_service, "append_task_log", lambda *_args, **_kwargs: None)
    db.init_db()
    return tmp_path


def create_task(name="测试成片", status="verifying"):
    stamp = db.now()
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO generation_tasks(task_name,status,created_at,updated_at) VALUES(?,?,?,?)",
            (name, status, stamp, stamp),
        )
        return int(cursor.lastrowid)


def create_file(root: Path, name="result.mp4") -> Path:
    path = root / name
    path.write_bytes(b"real-video-placeholder")
    return path


def test_success_creates_one_pending_publish_item(isolated_db):
    task_id = create_task()
    output = create_file(isolated_db)
    result = success_service.on_generation_succeeded(
        task_id, output, verified_duration=8.5, schedule_metadata=False
    )
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM generation_tasks WHERE id=?", (task_id,)).fetchone()
        draft = conn.execute(
            "SELECT * FROM publish_draft_metadata WHERE generation_task_id=?", (task_id,)
        ).fetchone()
    assert result["created"] is True
    assert task["status"] == "succeeded"
    assert Path(task["output_path"]).resolve() == output.resolve()
    assert draft["publish_status"] == "pending"
    assert draft["ai_status"] == "not_started"
    assert draft["video_filename"] == output.name
    assert draft["video_duration"] == pytest.approx(8.5)


def test_source_task_id_is_idempotent(isolated_db):
    task_id = create_task()
    output = create_file(isolated_db)
    first = success_service.on_generation_succeeded(
        task_id, output, verified_duration=3.0, schedule_metadata=False
    )
    second = success_service.on_generation_succeeded(
        task_id, output, verified_duration=3.0, schedule_metadata=False
    )
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM publish_draft_metadata WHERE generation_task_id=?", (task_id,)
        ).fetchone()[0]
    assert first["publish_item_id"] == second["publish_item_id"]
    assert second["created"] is False
    assert count == 1


def test_batch_three_outputs_create_three_items(isolated_db):
    task_ids = []
    stamp = db.now()
    with db.connect() as conn:
        batch_id = int(
            conn.execute(
                "INSERT INTO batch_tasks(name,total_count,created_at,updated_at) VALUES('批量3条',3,?,?)",
                (stamp, stamp),
            ).lastrowid
        )
    for index in range(1, 4):
        task_id = create_task(f"批量3条 · {index:03d}/003")
        task_ids.append(task_id)
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO batch_task_items(batch_id,task_id,index_no,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (batch_id, task_id, index, stamp, stamp),
            )
        success_service.on_generation_succeeded(
            task_id,
            create_file(isolated_db, f"batch-{index}.mp4"),
            verified_duration=2.0 + index,
            schedule_metadata=False,
        )
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT generation_task_id,source_batch_id FROM publish_draft_metadata ORDER BY generation_task_id"
        ).fetchall()
    assert [row["generation_task_id"] for row in rows] == task_ids
    assert {row["source_batch_id"] for row in rows} == {batch_id}


def test_ai_schedule_failure_does_not_remove_publish_item(isolated_db, monkeypatch):
    task_id = create_task()
    output = create_file(isolated_db)

    def fail_schedule(_task_id):
        raise RuntimeError("simulated AI failure")

    monkeypatch.setattr(publish_metadata_service, "queue_auto_generation", fail_schedule)
    result = success_service.on_generation_succeeded(
        task_id, output, verified_duration=4.0, schedule_metadata=True
    )
    with db.connect() as conn:
        draft = conn.execute(
            "SELECT * FROM publish_draft_metadata WHERE generation_task_id=?", (task_id,)
        ).fetchone()
    assert "simulated AI failure" in result["metadata_error"]
    assert draft is not None
    assert draft["publish_status"] == "pending"


def test_missing_output_cannot_create_publish_item(isolated_db):
    task_id = create_task()
    with pytest.raises(success_service.GenerationRegistrationError):
        success_service.on_generation_succeeded(
            task_id, isolated_db / "missing.mp4", schedule_metadata=False
        )
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM publish_draft_metadata WHERE generation_task_id=?", (task_id,)
        ).fetchone()[0] == 0


def test_ffprobe_failure_cannot_create_publish_item(isolated_db, monkeypatch):
    task_id = create_task()
    output = create_file(isolated_db)
    monkeypatch.setattr(
        success_service,
        "probe_video",
        lambda *_args, **_kwargs: {"duration": 0, "width": 0, "error": "invalid"},
    )
    with pytest.raises(success_service.GenerationRegistrationError):
        success_service.on_generation_succeeded(task_id, output, schedule_metadata=False)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM publish_draft_metadata WHERE generation_task_id=?", (task_id,)
        ).fetchone()[0] == 0


def test_repair_is_idempotent_and_ignores_failed_tasks(isolated_db, monkeypatch):
    good_id = create_task("历史成功", "succeeded")
    failed_id = create_task("历史失败", "failed")
    good_output = create_file(isolated_db, "history.mp4")
    failed_output = create_file(isolated_db, "failed.mp4")
    with db.connect() as conn:
        conn.execute("UPDATE generation_tasks SET output_path=? WHERE id=?", (str(good_output), good_id))
        conn.execute("UPDATE generation_tasks SET output_path=? WHERE id=?", (str(failed_output), failed_id))
    monkeypatch.setattr(
        success_service,
        "verify_generated_output",
        lambda path, **_kwargs: {
            "path": str(Path(path).resolve()), "filename": Path(path).name, "duration": 5.0, "size": 10
        },
    )
    first = success_service.repair_missing_publish_records(schedule_metadata=False)
    second = success_service.repair_missing_publish_records(schedule_metadata=False)
    assert first["repaired_task_ids"] == [good_id]
    assert second["repaired_count"] == 0
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM publish_draft_metadata WHERE generation_task_id=?", (failed_id,)
        ).fetchone()[0] == 0


def test_publish_item_survives_database_reopen(isolated_db):
    task_id = create_task()
    output = create_file(isolated_db)
    success_service.on_generation_succeeded(
        task_id, output, verified_duration=7.0, schedule_metadata=False
    )
    with db.connect() as conn:
        first = conn.execute(
            "SELECT id FROM publish_draft_metadata WHERE generation_task_id=?", (task_id,)
        ).fetchone()[0]
    with db.connect() as conn:
        second = conn.execute(
            "SELECT id FROM publish_draft_metadata WHERE generation_task_id=?", (task_id,)
        ).fetchone()[0]
    assert first == second
