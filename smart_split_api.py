"""HTTP-facing orchestration for the standalone smart split module."""

from __future__ import annotations

import re
from pathlib import Path

from db import connect, now
from smart_split_service import (
    DEFAULT_MIN_SEGMENT_DURATION, SmartSplitError, cancel_job, create_detection_job, get_job, list_jobs,
    merge_segments, open_output_directory, probe_video, select_video_file,
    set_all_selected, set_selected_category, split_segment, start_export,
    update_segment,
)


def smart_split_get(path: str, query: dict) -> dict | None:
    if path == "/api/smart-split/jobs":
        limit = int((query.get("limit") or [20])[0] or 20)
        return {"jobs": list_jobs(limit)}
    match = re.fullmatch(r"/api/smart-split/jobs/(\d+)", path)
    if match:
        return get_job(int(match.group(1)))
    return None


def smart_split_post(path: str, data: dict, scan_materials) -> dict | None:
    if path == "/api/smart-split/select-video":
        selected = select_video_file()
        return {"cancelled": not bool(selected), "path": selected, "video": probe_video(selected) if selected else None}
    if path == "/api/smart-split/detect":
        return create_detection_job(
            str(data.get("source_path") or ""),
            str(data.get("sensitivity") or "standard"),
            float(data["threshold"]) if data.get("threshold") not in (None, "") else None,
            float(data.get("min_segment_duration") or DEFAULT_MIN_SEGMENT_DURATION),
            bool(data.get("force", False)),
        )
    match = re.fullmatch(r"/api/smart-split/jobs/(\d+)/(export|cancel|open-output|register-materials|select-all|category|merge|split)", path)
    if match:
        job_id, action = int(match.group(1)), match.group(2)
        if action == "export":
            return start_export(job_id, str(data.get("output_root") or ""))
        if action == "cancel":
            return cancel_job(job_id)
        if action == "open-output":
            return open_output_directory(job_id)
        if action == "select-all":
            return set_all_selected(job_id, bool(data.get("selected", True)))
        if action == "category":
            return set_selected_category(job_id, str(data.get("category") or ""), data.get("segment_ids"))
        if action == "merge":
            return merge_segments(job_id, list(data.get("segment_ids") or []))
        if action == "split":
            return split_segment(job_id, float(data.get("cut_time") or 0))
        return register_outputs(job_id, scan_materials)
    segment_match = re.fullmatch(r"/api/smart-split/jobs/(\d+)/segments/(\d+)", path)
    if segment_match:
        return update_segment(int(segment_match.group(1)), int(segment_match.group(2)), data)
    return None


def register_outputs(job_id: int, scan_materials) -> dict:
    """Reuse the authoritative material scanner, then apply requested categories."""
    job = get_job(job_id)
    completed = [item for item in job["segments"] if item.get("status") == "completed" and item.get("output_path") and Path(item["output_path"]).is_file()]
    if not completed:
        raise SmartSplitError("没有可加入素材中心的分割视频")
    scan_result = scan_materials("")
    stamp = now()
    registered = 0
    with connect() as conn:
        for segment in completed:
            output_path = str(Path(segment["output_path"]).resolve())
            material = conn.execute("SELECT id FROM materials WHERE file_path=? AND status='active'", (output_path,)).fetchone()
            if not material:
                continue
            category = str(segment.get("category") or "未分类").strip() or "未分类"
            row = conn.execute("SELECT id FROM material_categories WHERE name=?", (category,)).fetchone()
            if row:
                category_id = int(row["id"])
            else:
                order = int(conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM material_categories").fetchone()[0])
                cursor = conn.execute(
                    "INSERT INTO material_categories(name,description,sort_order,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (category, "智能分割素材", order, stamp, stamp),
                )
                category_id = int(cursor.lastrowid)
            conn.execute("UPDATE materials SET category_id=?,updated_at=? WHERE id=?", (category_id, stamp, int(material["id"])))
            registered += 1
    return {"registered": registered, "total": len(completed), "scan": scan_result, "job": get_job(job_id)}
