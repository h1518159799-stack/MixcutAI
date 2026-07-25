"""Create a portable, privacy-safe SQLite development snapshot.

The source database is opened read-only and copied with SQLite's backup API.
Machine-bound authorization data and local file paths are then removed from the
copy. The original database is never modified.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


SENSITIVE_SETTING_KEYS = {
    "ffmpeg_path", "ffprobe_path", "output_dir", "work_dir", "bgm_dir",
    "sfx_dir", "material_root", "license_server_url", "update_manifest_url",
    "default_bgm_id", "default_tts_voice_id", "title_main", "title_sub",
}


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def sanitize(source: Path, destination: Path, schema_output: Path) -> dict:
    if not source.is_file():
        raise FileNotFoundError(f"源数据库不存在: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)

    with sqlite3.connect(destination) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")

        # Authentication, licensing and publishing identity data.
        for table in (
            "oauth_tokens", "oauth_sessions", "publish_channels", "publish_tasks",
            "platform_test_runs", "publish_accounts", "license_keys",
        ):
            if table_exists(conn, table):
                conn.execute(f'DELETE FROM "{table}"')

        # Customer/company profiles are intentionally not portable.
        if table_exists(conn, "account_profiles"):
            conn.execute("DELETE FROM account_profiles")

        # Local media paths, hashes and render outputs are machine-specific.
        for table in ("generation_shot_materials", "materials", "bgm_library", "scan_tasks"):
            if table_exists(conn, table):
                conn.execute(f'DELETE FROM "{table}"')

        if table_exists(conn, "sound_effect_library"):
            conn.execute("DELETE FROM sound_effect_library")

        if table_exists(conn, "template_shots"):
            shot_cols = columns(conn, "template_shots")
            assignments = []
            for name, value in (
                ("voice_file_path", "''"), ("subtitle_file_path", "''"),
                ("voice_text_hash", "''"), ("subtitle_render_hash", "''"),
                ("voice_duration", "0"),
            ):
                if name in shot_cols:
                    assignments.append(f'"{name}"={value}')
            if assignments:
                conn.execute("UPDATE template_shots SET " + ",".join(assignments))

        # Keep one harmless test-task structure and remove all local output paths.
        if table_exists(conn, "generation_tasks"):
            conn.execute("DELETE FROM generation_tasks")

        for table in ("batch_task_items", "batch_tasks"):
            if table_exists(conn, table):
                conn.execute(f'DELETE FROM "{table}"')

        # Preserve provider/model structure, but remove endpoints and keys.
        if table_exists(conn, "ai_service_configs"):
            cols = columns(conn, "ai_service_configs")
            updates = [f'"{name}"=\'\'' for name in ("api_key", "base_url") if name in cols]
            if updates:
                conn.execute("UPDATE ai_service_configs SET " + ",".join(updates))
            if "is_enabled" in cols:
                conn.execute("UPDATE ai_service_configs SET is_enabled=0")

        if table_exists(conn, "tts_service_configs"):
            cols = columns(conn, "tts_service_configs")
            updates = [f'"{name}"=\'\'' for name in ("api_key", "base_url") if name in cols]
            if updates:
                conn.execute("UPDATE tts_service_configs SET " + ",".join(updates))

        if table_exists(conn, "app_settings"):
            marks = ",".join("?" for _ in SENSITIVE_SETTING_KEYS)
            conn.execute(
                f"UPDATE app_settings SET value='' WHERE key IN ({marks})",
                tuple(sorted(SENSITIVE_SETTING_KEYS)),
            )
            conn.execute("UPDATE app_settings SET value='0' WHERE key='admin_mode'")

        conn.execute("COMMIT")
        conn.execute("VACUUM")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        counts = {
            row[0]: conn.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        }
        schema_lines = [line for line in conn.iterdump() if line.startswith("CREATE ")]

    schema_output.parent.mkdir(parents=True, exist_ok=True)
    schema_output.write_text("\n".join(schema_lines) + "\n", encoding="utf-8")
    summary = {
        "source_name": source.name,
        "destination": destination.name,
        "schema_version": user_version,
        "integrity_check": integrity,
        "table_counts": counts,
        "removed": [
            "OAuth Token/Session", "\u53d1\u5e03\u8d26\u53f7", "Cookie \u4e0e\u6d4f\u89c8\u5668\u76ee\u5f55", "API Key",
            "\u5ba2\u6237\u8d26\u53f7\u6863\u6848", "\u672c\u5730\u7d20\u6750\u8def\u5f84", "\u5361\u5bc6\u548c\u8bb8\u53ef\u8bc1\u72b6\u6001", "\u751f\u6210\u8f93\u51fa\u8def\u5f84",
        ],
    }
    (destination.parent / "sanitization_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("data/mixcut.db"))
    parser.add_argument("--output", type=Path, default=Path("backup_data_sanitized/mixcut_sanitized.db"))
    parser.add_argument("--schema", type=Path, default=Path("backup_data_sanitized/schema.sql"))
    args = parser.parse_args()
    print(json.dumps(sanitize(args.source, args.output, args.schema), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

