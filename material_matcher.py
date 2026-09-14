"""Material candidate loading and fair selection for generation tasks.

The material registry remains the ``materials`` table. Historical usage is
derived from successful generation relations instead of maintaining a second,
drift-prone counter on every material row.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence


SUPPORTED_STRATEGIES = {"random", "prefer_unused", "avoid_repeat"}
MIN_HD_SHORT_EDGE = 720


def material_pool_diagnostics(conn, category_id: int, required_duration: float) -> dict:
    """Return stage counts from the same registry used by generation."""
    record = conn.execute(
        """
        SELECT COUNT(*) AS registry_count,
               SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active_count,
               SUM(CASE WHEN status='active' AND category_id=? THEN 1 ELSE 0 END) AS category_count,
               SUM(CASE WHEN status='active' AND category_id=? AND duration>=? THEN 1 ELSE 0 END) AS duration_count,
               SUM(CASE WHEN status='active' AND category_id=? AND duration>=?
                             AND width>0 AND height>0 THEN 1 ELSE 0 END) AS valid_count,
               SUM(CASE WHEN status='active' AND category_id=? AND duration>=?
                             AND width>0 AND height>0 AND MIN(width,height)>=? THEN 1 ELSE 0 END) AS hd_count
          FROM materials
        """,
        (
            int(category_id), int(category_id), float(required_duration),
            int(category_id), float(required_duration),
            int(category_id), float(required_duration), MIN_HD_SHORT_EDGE,
        ),
    ).fetchone()
    keys = ("registry_count", "active_count", "category_count", "duration_count", "valid_count", "hd_count")
    return {key: int(record[index] or 0) for index, key in enumerate(keys)}


def load_material_candidates(conn, category_id: int, required_duration: float) -> list[dict]:
    """Load the complete eligible pool with successful-use metadata.

    There is deliberately no ``ORDER BY id ... LIMIT`` and no maximum-resolution
    preselection. A valid 1080x1920 source therefore competes fairly with 4K.
    """
    rows = conn.execute(
        """
        SELECT m.*,
               COALESCE((
                   SELECT COUNT(DISTINCT CAST(gsm.task_id AS TEXT) || ':' || CAST(gsm.shot_id AS TEXT))
                     FROM generation_shot_materials gsm
                     JOIN generation_tasks g ON g.id=gsm.task_id
                    WHERE gsm.material_id=m.id
                      AND g.status IN ('completed','succeeded')
               ),0) AS usage_count,
               (
                   SELECT MAX(COALESCE(NULLIF(g.finished_at,''),NULLIF(g.updated_at,''),g.created_at))
                     FROM generation_shot_materials gsm
                     JOIN generation_tasks g ON g.id=gsm.task_id
                    WHERE gsm.material_id=m.id
                      AND g.status IN ('completed','succeeded')
               ) AS last_used_at
          FROM materials m
         WHERE m.status='active'
           AND m.category_id=?
           AND m.duration>=?
           AND m.width>0
           AND m.height>0
        """,
        (int(category_id), float(required_duration)),
    ).fetchall()
    return [dict(item) for item in rows]


def _quality_pool(candidates: Sequence[dict]) -> list[dict]:
    """Use HD-or-better sources when available, without ranking 4K over 1080p."""
    valid = [item for item in candidates if int(item.get("width") or 0) > 0 and int(item.get("height") or 0) > 0]
    hd = [item for item in valid if min(int(item.get("width") or 0), int(item.get("height") or 0)) >= MIN_HD_SHORT_EDGE]
    return hd or valid


def _oldest_usage_tier(candidates: Sequence[dict]) -> list[dict]:
    """Return unused first, then least-used and least-recently-used candidates."""
    unused = [item for item in candidates if int(item.get("usage_count") or 0) == 0]
    if unused:
        return unused
    least_count = min(int(item.get("usage_count") or 0) for item in candidates)
    least_used = [item for item in candidates if int(item.get("usage_count") or 0) == least_count]
    oldest_value = min(str(item.get("last_used_at") or "") for item in least_used)
    return [item for item in least_used if str(item.get("last_used_at") or "") == oldest_value]


def select_material(
    candidates: Sequence[dict],
    *,
    strategy: str,
    exclude_ids: Iterable[int] = (),
    avoid_ids: Iterable[int] = (),
    rng: random.Random | None = None,
) -> tuple[dict, dict]:
    """Select one material and return selection diagnostics."""
    normalized = str(strategy or "random").strip().lower()
    if normalized not in SUPPORTED_STRATEGIES:
        normalized = "random"
    randomizer = rng or random.SystemRandom()
    excluded = {int(value) for value in exclude_ids}
    avoided = {int(value) for value in avoid_ids}
    quality = _quality_pool(candidates)
    without_current = [item for item in quality if int(item["id"]) not in excluded]
    if not without_current:
        without_current = list(quality)
    without_batch = [item for item in without_current if int(item["id"]) not in avoided]
    batch_fallback = False
    # ``avoid_ids`` means already used by the current task/batch. Honour it for
    # every strategy whenever another candidate exists.
    if without_batch:
        available = without_batch
    else:
        available = without_current
        batch_fallback = bool(avoided)
    if not available:
        raise ValueError("没有符合分类、时长和质量要求的素材")

    if normalized in {"prefer_unused", "avoid_repeat"}:
        selection_tier = _oldest_usage_tier(available)
        reason = "priority_unused" if int(selection_tier[0].get("usage_count") or 0) == 0 else "least_used_oldest"
    else:
        selection_tier = list(available)
        reason = "random"
    selected = randomizer.choice(selection_tier)
    diagnostics = {
        "strategy": normalized,
        "selection_reason": reason,
        "total_candidates": len(candidates),
        "quality_candidates": len(quality),
        "after_current_exclusion": len(without_current),
        "after_batch_exclusion": len(without_batch),
        "unused_candidates": sum(int(item.get("usage_count") or 0) == 0 for item in available),
        "selection_tier_count": len(selection_tier),
        "batch_reuse_fallback": batch_fallback,
        "selected_usage_count": int(selected.get("usage_count") or 0),
        "selected_last_used_at": str(selected.get("last_used_at") or ""),
    }
    return selected, diagnostics
