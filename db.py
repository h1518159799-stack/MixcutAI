import sqlite3
import json
import os
import re
import shutil
from contextlib import closing
from datetime import datetime
from pathlib import Path

from runtime_paths import APP_HOME, BROWSER_PROFILE_DIR, CONFIG_DIR, DATA_DIR, EXECUTABLE_DIR, OUTPUT_DIR, RESOURCE_DIR, ensure_runtime_dirs


APP_DIR = APP_HOME
THUMB_DIR = DATA_DIR / "thumbnails"
VOICE_DIR = DATA_DIR / "voices"
DB_PATH = DATA_DIR / "mixcut.db"
SCHEMA_VERSION = 26

PRODUCTION_LICENSE_URL = "https://license.ghwc.cn"
PRODUCTION_UPDATE_MANIFEST_URL = "https://update.ghwc.cn/update.json"
LEGACY_LICENSE_URLS = (
    "".join(("https://47-107-104-106.", "ss", "lip.io")),
    "".join(("https://license.", "xn", "--fiq959cg4n10q.com")),
)
LEGACY_UPDATE_URLS = (
    "".join(("https://47-107-104-106.", "ss", "lip.io/updates/update.json")),
    "".join(("https://update.", "xn", "--fiq959cg4n10q.com/updates/update.json")),
    "".join(("https://update.", "xn", "--fiq959cg4n10q.com/update.json")),
)


_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def decode_legacy_display_text(value: str) -> str:
    """Decode one legacy UI-storage layer without touching normal text."""
    text = str(value or "")
    if "\\u" in text:
        text = _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)
    original_cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    try:
        raw = text.encode("latin1")
    except UnicodeEncodeError:
        return text
    for encoding in ("gb18030", "utf-8"):
        try:
            candidate = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        candidate_cjk = sum("\u3400" <= char <= "\u9fff" for char in candidate)
        if candidate_cjk > original_cjk:
            return candidate
    return text

DEFAULT_CATEGORIES = [
    ("产品", "产品本体、包装、细节"),
    ("产品展示", "摆拍、展示、成品效果"),
    ("操作过程", "生产、演示、使用步骤"),
    ("工厂环境", "车间、设备、仓库、环境"),
    ("服务过程", "沟通、交付、售后、服务场景"),
    ("空镜", "转场、环境、氛围、补位镜头"),
]


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3.Connection and always release the file."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _seed_default_sfx(conn) -> None:
    """Copy bundled short sound effects into writable app data and upsert metadata."""
    resource_dir = RESOURCE_DIR / "default_sfx"
    manifest_path = resource_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    target_dir = DATA_DIR / "sound_effects" / "jianying_short"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = now()
    for item in manifest:
        source = resource_dir / str(item.get("file") or "")
        if not source.is_file():
            continue
        target = target_dir / source.name
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
        values = (
            str(item.get("name") or source.stem),
            str(item.get("category") or "general"),
            str(item.get("keywords") or ""),
            str(target),
            float(item.get("duration") or 0),
            float(item.get("volume") or 0.25),
        )
        existing = conn.execute(
            "SELECT id FROM sound_effect_library WHERE file_path=?", (str(target),)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE sound_effect_library
                   SET name=?,category=?,keywords=?,duration=?,volume_default=?,updated_at=?
                   WHERE id=?""",
                (values[0], values[1], values[2], values[4], values[5], stamp, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO sound_effect_library
                   (name,category,keywords,file_path,duration,volume_default,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                values + (stamp, stamp),
            )


def connect():
    ensure_runtime_dirs()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    # Each caller/thread receives a fresh connection; wait for transient
    # worker/status-write bursts instead of surfacing a lock to the UI.
    conn = sqlite3.connect(DB_PATH, timeout=30.0, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def generation_queue_seq_supported(conn: sqlite3.Connection) -> bool:
    return "queue_seq" in {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(generation_tasks)")
    }


def reserve_generation_queue_seqs(conn: sqlite3.Connection, count: int = 1) -> list[int]:
    """Reserve durable FIFO sequence numbers inside the caller's write transaction.

    Callers creating tasks use ``BEGIN IMMEDIATE`` before this helper.  SQLite's
    database write lock then makes MAX(queue_seq)+1 allocation atomic across the
    single-task and batch-task creation paths.
    """
    count = max(1, int(count))
    if not generation_queue_seq_supported(conn):
        return [0 for _ in range(count)]
    current = int(conn.execute(
        "SELECT COALESCE(MAX(queue_seq),0) FROM generation_tasks"
    ).fetchone()[0] or 0)
    return list(range(current + 1, current + count + 1))


def _init_db_impl():
    ensure_runtime_dirs()
    bundled_config = RESOURCE_DIR / "default_config.json"
    external_config = EXECUTABLE_DIR / "default_config.json"
    local_config = CONFIG_DIR / "default_config.json"
    if not local_config.exists():
        source_config = external_config if external_config.exists() else bundled_config
        if source_config.exists():
            shutil.copy2(source_config, local_config)
    try:
        default_config = json.loads(local_config.read_text(encoding="utf-8")) if local_config.exists() else {}
    except (OSError, ValueError):
        default_config = {}
    with connect() as conn:
        previous_schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS account_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                industry TEXT DEFAULT '',
                product_info TEXT DEFAULT '',
                brand_intro TEXT DEFAULT '',
                persona_info TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS material_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER,
                file_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                file_hash TEXT DEFAULT '',
                duration REAL DEFAULT 0,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                fps REAL DEFAULT 0,
                size INTEGER DEFAULT 0,
                file_mtime_ns INTEGER DEFAULT 0,
                codec TEXT DEFAULT '',
                thumbnail_path TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                scan_error TEXT DEFAULT '',
                last_scanned_at TEXT DEFAULT '',
                remark TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(category_id) REFERENCES material_categories(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS scan_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_path TEXT NOT NULL,
                status TEXT NOT NULL,
                total_files INTEGER DEFAULT 0,
                scanned_files INTEGER DEFAULT 0,
                added_count INTEGER DEFAULT 0,
                updated_count INTEGER DEFAULT 0,
                missing_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                unchanged_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                error_message TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS smart_split_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                source_hash TEXT NOT NULL DEFAULT '',
                source_size INTEGER NOT NULL DEFAULT 0,
                source_mtime_ns INTEGER NOT NULL DEFAULT 0,
                source_duration REAL NOT NULL DEFAULT 0,
                source_width INTEGER NOT NULL DEFAULT 0,
                source_height INTEGER NOT NULL DEFAULT 0,
                source_fps REAL NOT NULL DEFAULT 0,
                has_audio INTEGER NOT NULL DEFAULT 0,
                sensitivity TEXT NOT NULL DEFAULT 'standard',
                threshold REAL NOT NULL DEFAULT 0.30,
                min_segment_duration REAL NOT NULL DEFAULT 1.0,
                min_cut_gap REAL NOT NULL DEFAULT 0.65,
                status TEXT NOT NULL DEFAULT 'pending',
                stage TEXT NOT NULL DEFAULT '',
                progress INTEGER NOT NULL DEFAULT 0,
                output_dir TEXT NOT NULL DEFAULT '',
                exported_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_smart_split_jobs_source
                ON smart_split_jobs(source_hash,source_size,source_mtime_ns,status,id);
            CREATE TABLE IF NOT EXISTS smart_split_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                segment_index INTEGER NOT NULL DEFAULT 0,
                start_time REAL NOT NULL DEFAULT 0,
                end_time REAL NOT NULL DEFAULT 0,
                duration REAL NOT NULL DEFAULT 0,
                scene_score REAL NOT NULL DEFAULT 0,
                selected INTEGER NOT NULL DEFAULT 1,
                category TEXT NOT NULL DEFAULT '',
                thumbnail_path TEXT NOT NULL DEFAULT '',
                output_path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'detected',
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES smart_split_jobs(id) ON DELETE CASCADE,
                UNIQUE(job_id,segment_index)
            );
            CREATE INDEX IF NOT EXISTS idx_smart_split_segments_job
                ON smart_split_segments(job_id,segment_index);
            CREATE TABLE IF NOT EXISTS edit_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                output_width INTEGER DEFAULT 1080,
                output_height INTEGER DEFAULT 1920,
                is_hidden INTEGER DEFAULT 0,
                source_template_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS template_shots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL,
                shot_name TEXT NOT NULL,
                original_text TEXT DEFAULT '',
                ai_text TEXT DEFAULT '',
                material_category_id INTEGER,
                duration REAL DEFAULT 3,
                estimated_duration REAL DEFAULT 3,
                mute_original_audio INTEGER DEFAULT 1,
                enable_subtitle INTEGER DEFAULT 1,
                enable_tts INTEGER DEFAULT 1,
                voice_file_path TEXT DEFAULT '',
                voice_duration REAL DEFAULT 0,
                voice_text_hash TEXT DEFAULT '',
                subtitle_file_path TEXT DEFAULT '',
                subtitle_render_hash TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(template_id) REFERENCES edit_templates(id) ON DELETE CASCADE,
                FOREIGN KEY(material_category_id) REFERENCES material_categories(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS generation_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER,
                task_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                queue_seq INTEGER NOT NULL DEFAULT 0,
                progress INTEGER DEFAULT 0,
                current_shot TEXT DEFAULT '',
                total_shots INTEGER DEFAULT 0,
                output_path TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(template_id) REFERENCES edit_templates(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS generation_shot_materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                shot_id INTEGER NOT NULL,
                material_id INTEGER NOT NULL,
                voice_duration REAL DEFAULT 0,
                material_start_time REAL DEFAULT 0,
                material_end_time REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES generation_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(shot_id) REFERENCES template_shots(id) ON DELETE CASCADE,
                FOREIGN KEY(material_id) REFERENCES materials(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS batch_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                template_id INTEGER,
                total_count INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                material_strategy TEXT NOT NULL DEFAULT 'random',
                category_ids_json TEXT NOT NULL DEFAULT '[]',
                continue_on_child_error INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT DEFAULT '',
                FOREIGN KEY(template_id) REFERENCES edit_templates(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS batch_task_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                index_no INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                output_path TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES batch_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES generation_tasks(id) ON DELETE CASCADE,
                UNIQUE(batch_id,index_no)
            );
            CREATE TABLE IF NOT EXISTS bgm_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE,
                duration REAL DEFAULT 0,
                volume_default REAL DEFAULT 0.15,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_service_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'openai_compatible',
                base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                timeout_seconds INTEGER DEFAULT 60,
                is_enabled INTEGER DEFAULT 1,
                is_default INTEGER DEFAULT 0,
                system_prompt TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tts_service_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'edge_tts',
                base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                response_format TEXT DEFAULT 'mp3',
                timeout_seconds INTEGER DEFAULT 120,
                is_enabled INTEGER DEFAULT 1,
                is_default INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tts_voices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                voice_code TEXT NOT NULL,
                is_default INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(service_id) REFERENCES tts_service_configs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS shot_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_shot_id INTEGER NOT NULL,
                to_shot_id INTEGER NOT NULL,
                effect TEXT NOT NULL DEFAULT 'fade',
                duration REAL NOT NULL DEFAULT 0.35,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(from_shot_id) REFERENCES template_shots(id) ON DELETE CASCADE,
                FOREIGN KEY(to_shot_id) REFERENCES template_shots(id) ON DELETE CASCADE,
                UNIQUE(from_shot_id,to_shot_id)
            );
            CREATE TABLE IF NOT EXISTS sound_effect_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                keywords TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL UNIQUE,
                duration REAL NOT NULL DEFAULT 0,
                volume_default REAL NOT NULL DEFAULT 0.25,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shot_transitions_pair ON shot_transitions(from_shot_id,to_shot_id);
            CREATE INDEX IF NOT EXISTS idx_sound_effect_category ON sound_effect_library(category,id);
            CREATE TABLE IF NOT EXISTS platforms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                region TEXT NOT NULL DEFAULT 'domestic',
                network_requirement TEXT DEFAULT '',
                login_type TEXT NOT NULL DEFAULT 'browser_profile',
                enabled INTEGER NOT NULL DEFAULT 1,
                login_url TEXT NOT NULL DEFAULT '',
                cookie_domains_json TEXT NOT NULL DEFAULT '[]',
                login_cookie_names_json TEXT NOT NULL DEFAULT '[]',
                integration_type TEXT NOT NULL DEFAULT '',
                adapter_code TEXT NOT NULL DEFAULT '',
                capability_status TEXT NOT NULL DEFAULT 'untested',
                capability_status_reason TEXT NOT NULL DEFAULT '',
                last_tested_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publish_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_id INTEGER NOT NULL,
                account_name TEXT NOT NULL,
                login_status TEXT NOT NULL DEFAULT 'not_logged_in',
                browser_profile_path TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                publishing_mode TEXT NOT NULL DEFAULT 'browser',
                api_config_json TEXT NOT NULL DEFAULT '{}',
                authorization_status TEXT NOT NULL DEFAULT 'not_authorized',
                channel_id TEXT NOT NULL DEFAULT '',
                channel_title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(platform_id) REFERENCES platforms(id) ON DELETE RESTRICT,
                UNIQUE(platform_id,account_name)
            );
            CREATE INDEX IF NOT EXISTS idx_publish_accounts_platform ON publish_accounts(platform_id,status);
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                access_token_encrypted TEXT NOT NULL DEFAULT '',
                refresh_token_encrypted TEXT NOT NULL DEFAULT '',
                token_type TEXT NOT NULL DEFAULT 'Bearer',
                scope TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'authorized',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES publish_accounts(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS oauth_sessions (
                state TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL,
                code_verifier TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES publish_accounts(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS oauth_provider_configs (
                provider TEXT PRIMARY KEY,
                client_id TEXT NOT NULL DEFAULT '',
                client_secret_encrypted TEXT NOT NULL DEFAULT '',
                extra_config_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'not_configured',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publish_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generation_task_id INTEGER,
                video_path TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                cover_path TEXT NOT NULL DEFAULT '',
                ai_title TEXT NOT NULL DEFAULT '',
                ai_description TEXT NOT NULL DEFAULT '',
                ai_hashtags TEXT NOT NULL DEFAULT '[]',
                cover_text TEXT NOT NULL DEFAULT '',
                ai_generated_time TEXT NOT NULL DEFAULT '',
                scheduled_at TEXT NOT NULL DEFAULT '',
                delete_after_publish INTEGER NOT NULL DEFAULT 0,
                platform TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                total_channels INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                technical_log_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(generation_task_id) REFERENCES generation_tasks(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS publish_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publish_task_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                platform_id INTEGER NOT NULL,
                selected_platform TEXT NOT NULL DEFAULT '',
                account_platform TEXT NOT NULL DEFAULT '',
                publisher_adapter TEXT NOT NULL DEFAULT '',
                publish_url TEXT NOT NULL DEFAULT '',
                error_stage TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                technical_log_id TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL DEFAULT '',
                video_file_hash TEXT NOT NULL DEFAULT '',
                title_hash TEXT NOT NULL DEFAULT '',
                result_evidence_json TEXT NOT NULL DEFAULT '{}',
                remote_post_id TEXT NOT NULL DEFAULT '',
                remote_url TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(publish_task_id) REFERENCES publish_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(account_id) REFERENCES publish_accounts(id) ON DELETE RESTRICT,
                FOREIGN KEY(platform_id) REFERENCES platforms(id) ON DELETE RESTRICT,
                UNIQUE(publish_task_id,account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_publish_tasks_status ON publish_tasks(status,scheduled_at,id);
            CREATE INDEX IF NOT EXISTS idx_publish_channels_task ON publish_channels(publish_task_id,status,id);
            CREATE TABLE IF NOT EXISTS publish_draft_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generation_task_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                hashtags TEXT NOT NULL DEFAULT '',
                cover_text TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                ai_status TEXT NOT NULL DEFAULT 'not_started',
                title_state TEXT NOT NULL DEFAULT 'not_started',
                hashtags_state TEXT NOT NULL DEFAULT 'not_started',
                cover_text_state TEXT NOT NULL DEFAULT 'not_started',
                auto_generation_attempted INTEGER NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                technical_log_id TEXT NOT NULL DEFAULT '',
                ai_generated_at TEXT NOT NULL DEFAULT '',
                source_batch_id INTEGER,
                project_id INTEGER,
                video_path TEXT NOT NULL DEFAULT '',
                video_filename TEXT NOT NULL DEFAULT '',
                video_duration REAL NOT NULL DEFAULT 0,
                publish_status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(generation_task_id) REFERENCES generation_tasks(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_publish_draft_metadata_status
                ON publish_draft_metadata(ai_status,generation_task_id);
            CREATE TABLE IF NOT EXISTS publish_account_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_used_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(name)
            );
            CREATE TABLE IF NOT EXISTS publish_account_group_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(group_id) REFERENCES publish_account_groups(id) ON DELETE CASCADE,
                FOREIGN KEY(account_id) REFERENCES publish_accounts(id) ON DELETE CASCADE,
                UNIQUE(group_id,account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_publish_group_members_group ON publish_account_group_members(group_id,enabled,sort_order,id);
            CREATE INDEX IF NOT EXISTS idx_publish_group_members_account ON publish_account_group_members(account_id);
            CREATE TABLE IF NOT EXISTS publish_group_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                total_count INTEGER NOT NULL DEFAULT 0,
                queued_count INTEGER NOT NULL DEFAULT 0,
                running_count INTEGER NOT NULL DEFAULT 0,
                succeeded_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                needs_action_count INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(group_id) REFERENCES publish_account_groups(id) ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_publish_group_batches_status ON publish_group_batches(status,created_at,id);
            CREATE TABLE IF NOT EXISTS publish_group_batch_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                task_id INTEGER,
                account_id INTEGER,
                group_id INTEGER,
                group_batch_id INTEGER,
                account_name_snapshot TEXT NOT NULL DEFAULT '',
                platform_name_snapshot TEXT NOT NULL DEFAULT '',
                platform_code_snapshot TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                blocked_reason TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES publish_group_batches(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES publish_tasks(id) ON DELETE SET NULL,
                FOREIGN KEY(account_id) REFERENCES publish_accounts(id) ON DELETE SET NULL,
                UNIQUE(batch_id,account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_publish_group_batch_items_batch ON publish_group_batch_items(batch_id,status,id);
            CREATE TABLE IF NOT EXISTS platform_test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                video_path TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                cover_path TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'private',
                test_mode TEXT NOT NULL DEFAULT 'private',
                scheduled_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                current_step TEXT NOT NULL DEFAULT '',
                report_path TEXT NOT NULL DEFAULT '',
                remote_post_id TEXT NOT NULL DEFAULT '',
                remote_url TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(platform_id) REFERENCES platforms(id) ON DELETE RESTRICT,
                FOREIGN KEY(account_id) REFERENCES publish_accounts(id) ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_platform_test_runs_platform ON platform_test_runs(platform_id,status,id);
            CREATE TABLE IF NOT EXISTS license_keys (
                key_code TEXT PRIMARY KEY,
                type TEXT NOT NULL DEFAULT 'month',
                expire_date TEXT DEFAULT '',
                machine_id TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unused',
                server_token TEXT DEFAULT '',
                activated_at TEXT DEFAULT '',
                last_check_at TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        ensure_column(conn, "template_shots", "enable_subtitle", "INTEGER DEFAULT 1")
        ensure_column(conn, "materials", "file_mtime_ns", "INTEGER DEFAULT 0")
        ensure_column(conn, "materials", "codec", "TEXT DEFAULT ''")
        ensure_column(conn, "materials", "scan_error", "TEXT DEFAULT ''")
        ensure_column(conn, "materials", "last_scanned_at", "TEXT DEFAULT ''")
        ensure_column(conn, "scan_tasks", "updated_count", "INTEGER DEFAULT 0")
        ensure_column(conn, "scan_tasks", "missing_count", "INTEGER DEFAULT 0")
        ensure_column(conn, "scan_tasks", "error_count", "INTEGER DEFAULT 0")
        ensure_column(conn, "scan_tasks", "unchanged_count", "INTEGER DEFAULT 0")
        ensure_column(conn, "template_shots", "enable_tts", "INTEGER DEFAULT 1")
        ensure_column(conn, "template_shots", "estimated_duration", "REAL DEFAULT 3")
        ensure_column(conn, "template_shots", "voice_file_path", "TEXT DEFAULT ''")
        ensure_column(conn, "template_shots", "voice_duration", "REAL DEFAULT 0")
        ensure_column(conn, "template_shots", "voice_text_hash", "TEXT DEFAULT ''")
        ensure_column(conn, "template_shots", "subtitle_file_path", "TEXT DEFAULT ''")
        ensure_column(conn, "template_shots", "subtitle_render_hash", "TEXT DEFAULT ''")
        ensure_column(conn, "template_shots", "tts_voice_id", "INTEGER")
        ensure_column(conn, "edit_templates", "is_hidden", "INTEGER DEFAULT 0")
        ensure_column(conn, "edit_templates", "source_template_id", "INTEGER")
        ensure_column(conn, "generation_tasks", "template_id", "INTEGER")
        ensure_column(conn, "generation_tasks", "current_shot", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "total_shots", "INTEGER DEFAULT 0")
        ensure_column(conn, "generation_tasks", "started_at", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "last_heartbeat_at", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "current_stage", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "current_step", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "current_item", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "current_progress", "INTEGER DEFAULT 0")
        ensure_column(conn, "generation_tasks", "worker_id", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "process_id", "INTEGER DEFAULT 0")
        ensure_column(conn, "generation_tasks", "last_log_at", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "cancel_requested", "INTEGER DEFAULT 0")
        ensure_column(conn, "generation_tasks", "retry_count", "INTEGER DEFAULT 0")
        ensure_column(conn, "generation_tasks", "diagnostic_log", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "error_code", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "failed_stage", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "technical_log_id", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "log_path", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "finished_at", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "retry_of_task_id", "INTEGER")
        ensure_column(conn, "generation_tasks", "process_name", "TEXT DEFAULT ''")
        ensure_column(conn, "generation_tasks", "ffmpeg_pid", "INTEGER DEFAULT 0")
        ensure_column(conn, "generation_tasks", "ffprobe_pid", "INTEGER DEFAULT 0")
        ensure_column(conn, "generation_tasks", "attempt_no", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "generation_tasks", "queue_seq", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "batch_tasks", "continue_on_child_error", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "generation_shot_materials", "task_id", "INTEGER")
        ensure_column(conn, "platforms", "integration_type", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "platforms", "adapter_code", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "platforms", "capability_status", "TEXT NOT NULL DEFAULT 'untested'")
        ensure_column(conn, "platforms", "capability_status_reason", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "platforms", "last_tested_at", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_accounts", "authorization_status", "TEXT NOT NULL DEFAULT 'not_authorized'")
        ensure_column(conn, "publish_accounts", "channel_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_accounts", "channel_title", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "oauth_sessions", "provider", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "oauth_sessions", "status", "TEXT NOT NULL DEFAULT 'waiting_callback'")
        ensure_column(conn, "oauth_sessions", "authorization_url", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "oauth_sessions", "result_encrypted", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "oauth_sessions", "error_code", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "oauth_sessions", "error_message", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "oauth_sessions", "completed_at", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "oauth_sessions", "consumed_at", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_tasks", "delete_after_publish", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "publish_tasks", "platform", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_tasks", "ai_title", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_tasks", "ai_description", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_tasks", "ai_hashtags", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(conn, "publish_tasks", "cover_text", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_tasks", "ai_generated_time", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_tasks", "error_code", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_tasks", "technical_log_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_tasks", "group_id", "INTEGER")
        ensure_column(conn, "publish_tasks", "group_batch_id", "INTEGER")
        ensure_column(conn, "publish_group_batches", "queued_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "publish_group_batches", "running_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "publish_group_batches", "succeeded_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "publish_group_batches", "skipped_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "publish_group_batches", "needs_action_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "publish_group_batches", "idempotency_key", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_group_batch_items", "group_id", "INTEGER")
        ensure_column(conn, "publish_group_batch_items", "group_batch_id", "INTEGER")
        ensure_column(conn, "publish_group_batch_items", "account_name_snapshot", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_group_batch_items", "platform_name_snapshot", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_group_batch_items", "platform_code_snapshot", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_group_batch_items", "blocked_reason", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_group_batch_items", "error_code", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_group_batch_items", "retry_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "publish_draft_metadata", "source_batch_id", "INTEGER")
        ensure_column(conn, "publish_draft_metadata", "project_id", "INTEGER")
        ensure_column(conn, "publish_draft_metadata", "video_path", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_draft_metadata", "video_filename", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_draft_metadata", "video_duration", "REAL NOT NULL DEFAULT 0")
        ensure_column(conn, "publish_draft_metadata", "publish_status", "TEXT NOT NULL DEFAULT 'pending'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_publish_draft_pending "
            "ON publish_draft_metadata(publish_status,ai_status,generation_task_id)"
        )
        migrate_publish_group_batch_items_v15(conn)
        ensure_column(conn, "publish_channels", "error_code", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_channels", "technical_log_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_channels", "idempotency_key", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_channels", "video_file_hash", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_channels", "title_hash", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_channels", "result_evidence_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, "publish_channels", "selected_platform", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_channels", "account_platform", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_channels", "publisher_adapter", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_channels", "publish_url", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "publish_channels", "error_stage", "TEXT NOT NULL DEFAULT ''")
        # Backfill routing evidence for pre-audit rows without changing their
        # status or execution history.  Unresolvable legacy rows remain blank.
        conn.execute(
            """UPDATE publish_tasks SET platform=(
                   SELECT p.code FROM publish_channels c JOIN platforms p ON p.id=c.platform_id
                   WHERE c.publish_task_id=publish_tasks.id ORDER BY c.id LIMIT 1
               ) WHERE COALESCE(platform,'')=''"""
        )
        conn.execute(
            """UPDATE publish_channels SET selected_platform=(SELECT p.code FROM platforms p WHERE p.id=publish_channels.platform_id),
                   account_platform=(SELECT p.code FROM platforms p JOIN publish_accounts a ON a.platform_id=p.id WHERE a.id=publish_channels.account_id)
               WHERE COALESCE(selected_platform,'')='' OR COALESCE(account_platform,'')=''"""
        )
        if previous_schema_version < 20:
            # A fresh process cannot own a worker from the previous executable.
            # Preserve interrupted work as stalled instead of pretending it is
            # still running or silently submitting it again.
            conn.execute(
                """UPDATE generation_tasks SET status=CASE
                     WHEN status='pending' THEN 'queued'
                     WHEN status='completed' THEN 'succeeded'
                     WHEN status IN ('preparing','rendering','verifying','running','processing','processing_shot','merging') THEN 'stalled'
                     ELSE status END,
                   current_stage=CASE WHEN status IN ('preparing','rendering','verifying','running','processing','processing_shot','merging') THEN 'stalled' ELSE current_stage END,
                   current_step=CASE WHEN status IN ('preparing','rendering','verifying','running','processing','processing_shot','merging') THEN '软件升级后检测到旧 Worker 已退出' ELSE current_step END,
                   error_message=CASE WHEN status IN ('preparing','rendering','verifying','running','processing','processing_shot','merging')
                      THEN '软件上次退出时任务仍在执行，旧 Worker 已不存在，请诊断后重试' ELSE error_message END,
                   worker_id='',process_id=0,process_name='',ffmpeg_pid=0,ffprobe_pid=0"""
            )
            conn.execute("UPDATE batch_task_items SET status='queued' WHERE status='pending'")
            conn.execute("UPDATE batch_task_items SET status='succeeded' WHERE status='completed'")
            conn.execute(
                """UPDATE batch_task_items SET status='stalled',
                   error_message=COALESCE(NULLIF((SELECT error_message FROM generation_tasks g WHERE g.id=batch_task_items.task_id),''),error_message)
                   WHERE task_id IN (SELECT id FROM generation_tasks WHERE status='stalled')"""
            )
            for batch in conn.execute("SELECT id,total_count FROM batch_tasks ORDER BY id").fetchall():
                item_rows = conn.execute(
                    "SELECT status FROM batch_task_items WHERE batch_id=?", (int(batch["id"]),)
                ).fetchall()
                statuses = [str(item["status"] or "queued") for item in item_rows]
                succeeded = statuses.count("succeeded")
                failed = statuses.count("failed")
                stalled = statuses.count("stalled")
                queued = statuses.count("queued")
                if statuses and succeeded == len(statuses):
                    batch_status = "succeeded"
                elif stalled:
                    batch_status = "needs_action"
                elif queued:
                    batch_status = "queued"
                elif succeeded and failed:
                    batch_status = "partially_succeeded"
                elif failed and failed == len(statuses):
                    batch_status = "failed"
                else:
                    batch_status = "queued"
                conn.execute(
                    "UPDATE batch_tasks SET status=?,completed_count=?,failed_count=?,finished_at=CASE WHEN ? IN ('succeeded','partially_succeeded','failed') THEN finished_at ELSE '' END WHERE id=?",
                    (batch_status, succeeded, failed, batch_status, int(batch["id"])),
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_shot_materials_task_shot "
            "ON generation_shot_materials(task_id, shot_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_task_items_batch ON batch_task_items(batch_id,index_no)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_task_items_task ON batch_task_items(task_id)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_single_active_generation_slot "
            "ON generation_tasks((1)) WHERE status IN ('preparing','rendering','verifying')"
        )
        # Existing task ids already represent their durable creation order.
        # Backfill once so upgrades keep the exact historic FIFO order, then
        # use queue_seq for every new single or batch generation task.
        conn.execute("UPDATE generation_tasks SET queue_seq=id WHERE COALESCE(queue_seq,0)<=0")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_generation_tasks_queue_seq "
            "ON generation_tasks(queue_seq) WHERE queue_seq>0"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_tasks_queue_status "
            "ON generation_tasks(status,queue_seq,created_at,id)"
        )
        # Upgrade legacy databases where material matches belonged only to the
        # reusable template shot.  Preserve the latest completed task's actual
        # selection so the first task created after this migration can avoid it.
        conn.execute(
            """
            INSERT INTO generation_shot_materials(
                task_id,shot_id,material_id,voice_duration,
                material_start_time,material_end_time,created_at,updated_at
            )
            SELECT t.id,gsm.shot_id,gsm.material_id,gsm.voice_duration,
                   gsm.material_start_time,gsm.material_end_time,gsm.created_at,gsm.updated_at
            FROM generation_shot_materials gsm
            JOIN template_shots s ON s.id=gsm.shot_id
            JOIN generation_tasks t ON t.id=(
                SELECT MAX(t2.id) FROM generation_tasks t2
                WHERE t2.template_id=s.template_id AND t2.status IN ('completed','succeeded')
            )
            WHERE gsm.task_id IS NULL
              AND gsm.id=(
                  SELECT MAX(g2.id) FROM generation_shot_materials g2
                  WHERE g2.shot_id=gsm.shot_id AND g2.task_id IS NULL
              )
              AND NOT EXISTS(
                  SELECT 1 FROM generation_shot_materials existing
                  WHERE existing.task_id=t.id AND existing.shot_id=gsm.shot_id
              )
            """
        )
        conn.execute(
            "UPDATE template_shots SET estimated_duration=duration WHERE (estimated_duration IS NULL OR estimated_duration=0) AND duration IS NOT NULL"
        )
        conn.execute("DELETE FROM material_categories WHERE instr(name, '?') > 0")
        # Material categories are owned by the user's scanned folder structure.
        # Do not inject system categories such as "工厂环境" on startup.
        for key, value in {
            "output_dir": str((OUTPUT_DIR).resolve()),
            "work_dir": str((DATA_DIR / "work_cache").resolve()),
            "ffmpeg_path": str(default_config.get("ffmpeg_path") or ""),
            "ffprobe_path": str(default_config.get("ffprobe_path") or ""),
            "license_server_url": str(default_config.get("license_server_url") or ""),
            "license_offline_grace_days": str(default_config.get("license_offline_grace_days") or 3),
            "license_expire_reminder_days": str(default_config.get("license_expire_reminder_days") or 7),
            "auto_check_update": "1" if default_config.get("auto_check_update", True) else "0",
            "update_manifest_url": str(default_config.get("update_manifest_url") or ""),
            # The real-publication acceptance console is intentionally hidden
            # in customer builds. Administrators can enable it locally or set
            # MIXCUT_ADMIN_MODE=1 without changing any publishing capability.
            "admin_mode": "0",
            "bgm_dir": "",
            "sfx_dir": "",
            "material_root": "",
            "default_bgm_id": "",
            "bgm_enabled": "0",
            "sfx_enabled": "1",
            "natural_color_filter_enabled": "1",
            "natural_color_filter_strength": "standard",
            "end_hold_duration": "0.8",
            "inter_shot_audio_safety": "0.15",
            "tts_enabled": "1",
            "default_tts_voice_id": "",
            "tts_speed": "1.0",
            "tts_pitch": "0",
            "tts_volume": "1.0",
            "tts_segment_gap_ms": "60",
            "aliyun_bailian_sample_rate": "24000",
            "voice_volume": "1.0",
            "bgm_volume": "0.15",
            "subtitle_enabled": "1",
            "title_main": "",
            "title_sub": "",
            "title_font_dir": "",
            "title_font": "Microsoft YaHei",
            "title_font_size": "118",
            "title_color": "#FFE67A",
            "title_auto_fit": "0",
            "title_background_enabled": "0",
            "title_background_color": "#16B8B8",
            "title_background_opacity": "72",
            "title_background_padding_y": "22",
            "title_center": "1",
            "title_outline_width": "1",
            "title_outline_color": "#000000",
            "title_shadow_color": "#F15A24",
            "title_shadow_x": "2",
            "title_shadow_y": "2",
            "title_main_x": "50",
            "title_main_y": "13",
            "title_sub_x": "50",
            "title_sub_y": "21",
            "subtitle_font": "Microsoft YaHei",
            "subtitle_font_size": "78",
            "subtitle_color": "#FFFFFF",
            "subtitle_outline_width": "1",
            "subtitle_outline_color": "#000000",
            "subtitle_margin_bottom": "446",
            "subtitle_max_chars_per_line": "14",
            "subtitle_effect": "none",
            "subtitle_effect_duration": "160",
            "title_style_preset": "domestic",
        }.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value, updated_at) VALUES(?,?,?)",
                (key, value, now()),
            )
        if previous_schema_version < 25:
            # Migrate only known retired production endpoints. Explicit
            # localhost/development overrides remain untouched, and runtime
            # code never falls back to these legacy domains.
            license_placeholders = ",".join("?" for _ in LEGACY_LICENSE_URLS)
            update_placeholders = ",".join("?" for _ in LEGACY_UPDATE_URLS)
            conn.execute(
                f"UPDATE app_settings SET value=?,updated_at=? "
                f"WHERE key='license_server_url' AND value IN ({license_placeholders})",
                (PRODUCTION_LICENSE_URL, now(), *LEGACY_LICENSE_URLS),
            )
            conn.execute(
                f"UPDATE app_settings SET value=?,updated_at=? "
                f"WHERE key='update_manifest_url' AND value IN ({update_placeholders})",
                (PRODUCTION_UPDATE_MANIFEST_URL, now(), *LEGACY_UPDATE_URLS),
            )
        # Only tasks completed after this feature is first installed may start
        # automatic publishing-metadata generation. Existing historical videos
        # remain untouched and can still be regenerated manually.
        conn.execute(
            "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
            ("publish_metadata_autogen_cutoff", now(), now()),
        )
        # Two independent title/subtitle presets.  The old implementation did
        # not include title_main/title_sub in the scoped keys, so saving the
        # overseas preset could still overwrite the domestic title text.  Keep
        # every field used by the dialog in the scoped object.
        style_keys = (
            "title_main", "title_sub", "title_font_dir", "title_font", "title_font_size", "title_color",
            "title_auto_fit", "title_background_enabled", "title_background_color",
            "title_background_opacity", "title_background_padding_y", "title_center",
            "title_outline_width", "title_outline_color", "title_shadow_color",
            "title_shadow_x", "title_shadow_y", "title_main_x", "title_main_y",
            "title_sub_x", "title_sub_y", "subtitle_enabled", "subtitle_font",
            "subtitle_font_size", "subtitle_color", "subtitle_outline_width",
            "subtitle_outline_color", "subtitle_margin_bottom",
            "subtitle_max_chars_per_line", "subtitle_effect", "subtitle_effect_duration",
        )
        for key in style_keys:
            current = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if current:
                conn.execute(
                    "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
                    ("domestic_" + key, current["value"], now()),
                )
        overseas_defaults = {
            "title_font": "Arial", "title_font_size": "92", "title_auto_fit": "1",
            "subtitle_font": "Arial", "subtitle_font_size": "58",
            "subtitle_max_chars_per_line": "24", "subtitle_outline_width": "2",
        }
        for key in style_keys:
            current = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            legacy = conn.execute(
                "SELECT value FROM app_settings WHERE key=?", ("international_" + key,)
            ).fetchone()
            value = overseas_defaults.get(
                key, legacy["value"] if legacy else (current["value"] if current else "")
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
                ("overseas_" + key, value, now()),
            )
        # Marker makes the legacy-global -> domestic migration explicit and
        # prevents later starts from treating global values as fresh defaults.
        conn.execute(
            "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
            ("title_template_scope_migration_v2", "1", now()),
        )
        if not conn.execute("SELECT 1 FROM tts_service_configs LIMIT 1").fetchone():
            conn.execute(
                "INSERT INTO tts_service_configs(name,provider,model,response_format,is_enabled,is_default,created_at,updated_at) VALUES(?,?,?,?,1,1,?,?)",
                ("Edge TTS 本地服务", "edge_tts", "edge-tts", "mp3", now(), now()),
            )
            service_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for idx, (name, code) in enumerate([
                ("晓晓（女声）", "zh-CN-XiaoxiaoNeural"),
                ("云希（男声）", "zh-CN-YunxiNeural"),
                ("晓伊（女声）", "zh-CN-XiaoyiNeural"),
                ("云健（男声）", "zh-CN-YunjianNeural"),
            ]):
                conn.execute(
                    "INSERT INTO tts_voices(service_id,name,voice_code,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (service_id, name, code, 1 if idx == 0 else 0, now(), now()),
                )
        edge_service = conn.execute(
            "SELECT id FROM tts_service_configs WHERE provider='edge_tts' ORDER BY is_default DESC,id LIMIT 1"
        ).fetchone()
        if edge_service:
            popular_voices = [
                ("晓晓｜温暖女声", "zh-CN-XiaoxiaoNeural"),
                ("晓伊｜活力女声", "zh-CN-XiaoyiNeural"),
                ("云希｜阳光男声", "zh-CN-YunxiNeural"),
                ("云健｜激情男声", "zh-CN-YunjianNeural"),
                ("云夏｜可爱男声", "zh-CN-YunxiaNeural"),
                ("云扬｜专业男声", "zh-CN-YunyangNeural"),
                ("晓北｜东北女声", "zh-CN-liaoning-XiaobeiNeural"),
                ("晓妮｜陕西女声", "zh-CN-shaanxi-XiaoniNeural"),
                ("English | Aria Female", "en-US-AriaNeural"),
                ("English | Guy Male", "en-US-GuyNeural"),
                ("Spanish | Elvira Female", "es-ES-ElviraNeural"),
                ("Spanish | Alvaro Male", "es-ES-AlvaroNeural"),
                ("Vietnamese | HoaiMy Female", "vi-VN-HoaiMyNeural"),
                ("Vietnamese | NamMinh Male", "vi-VN-NamMinhNeural"),
            ]
            for name, code in popular_voices:
                existing = conn.execute(
                    "SELECT id FROM tts_voices WHERE service_id=? AND voice_code=? ORDER BY id LIMIT 1",
                    (edge_service["id"], code),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE tts_voices SET name=?,updated_at=? WHERE id=?",
                        (name, now(), existing["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO tts_voices(service_id,name,voice_code,is_default,created_at,updated_at)
                           VALUES(?,?,?,0,?,?)""",
                        (edge_service["id"], name, code, now(), now()),
                    )
        # Repair legacy display strings that were stored as literal \uXXXX or
        # GBK bytes decoded as Latin-1. This changes values only, never schema.
        for table in ("tts_service_configs", "tts_voices"):
            for item in conn.execute(f"SELECT id,name FROM {table}").fetchall():
                repaired = decode_legacy_display_text(item["name"])
                if repaired != item["name"]:
                    conn.execute(
                        f"UPDATE {table} SET name=?,updated_at=? WHERE id=?",
                        (repaired, now(), item["id"]),
                    )
        default_platforms = [
            ("douyin", "抖音", "domestic", "", "https://creator.douyin.com/",
             ["douyin.com"], ["sessionid", "sessionid_ss"]),
            ("xiaohongshu", "小红书", "domestic", "", "https://creator.xiaohongshu.com/",
             ["xiaohongshu.com"], ["web_session"]),
            ("kuaishou", "快手", "domestic", "", "https://cp.kuaishou.com/",
             ["kuaishou.com"], ["kuaishou.server.web_st", "userId"]),
            ("wechat_channels", "视频号", "domestic", "", "https://channels.weixin.qq.com/",
             ["weixin.qq.com", "qq.com"], ["wxuin", "sessionid"]),
            ("bilibili", "B站", "domestic", "", "https://member.bilibili.com/",
             ["bilibili.com"], ["SESSDATA", "DedeUserID"]),
            ("tiktok", "TikTok", "overseas", "该平台需要海外网络环境", "https://www.tiktok.com/login",
             ["tiktok.com"], ["sessionid", "sid_tt"]),
            ("youtube", "YouTube", "overseas", "该平台需要海外网络环境", "https://accounts.google.com/ServiceLogin?service=youtube",
             ["youtube.com", "google.com"], ["SAPISID", "LOGIN_INFO"]),
            ("instagram", "Instagram Business", "overseas", "该平台需要海外网络环境", "https://www.instagram.com/accounts/login/",
             ["instagram.com"], ["sessionid", "ds_user_id"]),
            ("facebook", "Facebook Page", "overseas", "该平台需要海外网络环境", "https://www.facebook.com/login/",
             ["facebook.com"], ["c_user", "xs"]),
            ("x", "X", "overseas", "该平台需要海外网络环境", "https://x.com/i/flow/login",
             ["x.com", "twitter.com"], ["auth_token", "ct0"]),
        ]
        for code, name, region, network, login_url, domains, cookie_names in default_platforms:
            conn.execute(
                """INSERT OR IGNORE INTO platforms
                   (code,name,region,network_requirement,login_type,enabled,login_url,
                    cookie_domains_json,login_cookie_names_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,1,?,?,?,?,?)""",
                (code, name, region, network, "browser_profile", login_url,
                 json.dumps(domains, ensure_ascii=False), json.dumps(cookie_names, ensure_ascii=False), now(), now()),
            )
        api_adapters = {"youtube": "youtube", "facebook": "facebook", "instagram": "instagram", "x": "x"}
        browser_adapters = {
            "tiktok": "tiktok", "douyin": "douyin", "xiaohongshu": "xiaohongshu",
            "kuaishou": "kuaishou", "wechat_channels": "wechat_channels", "bilibili": "bilibili",
        }
        for code, adapter_code in api_adapters.items():
            conn.execute(
                "UPDATE platforms SET integration_type='api',adapter_code=?,login_type='oauth' WHERE code=? AND adapter_code=''",
                (adapter_code, code),
            )
        conn.execute("UPDATE platforms SET name='Facebook Page' WHERE code='facebook' AND name='Facebook'")
        conn.execute("UPDATE platforms SET name='Instagram Business' WHERE code='instagram' AND name='Instagram'")
        for code, adapter_code in browser_adapters.items():
            conn.execute(
                "UPDATE platforms SET integration_type='browser',adapter_code=?,login_type='browser_profile' WHERE code=? AND adapter_code=''",
                (adapter_code, code),
            )
        canonical_platform_names = {
            "douyin": "抖音", "xiaohongshu": "小红书", "kuaishou": "快手",
            "wechat_channels": "视频号", "bilibili": "B站", "tiktok": "TikTok",
            "youtube": "YouTube", "instagram": "Instagram Business",
            "facebook": "Facebook Page", "x": "X",
        }
        for code, name in canonical_platform_names.items():
            conn.execute("UPDATE platforms SET name=? WHERE code=?", (name, code))
        valid_capability_statuses = ("not_implemented", "untested", "testing", "passed", "failed", "limited")
        marks = ",".join("?" for _ in valid_capability_statuses)
        conn.execute(
            f"UPDATE platforms SET capability_status='untested' WHERE capability_status NOT IN ({marks}) OR capability_status=''",
            valid_capability_statuses,
        )
        # Preserve future test results. Only initialize the historical Douyin
        # acceptance once, before any platform test has recorded a timestamp.
        conn.execute(
            """UPDATE platforms SET capability_status='passed',
               capability_status_reason='既有真实发布验收已通过'
               WHERE code='douyin' AND capability_status='untested' AND last_tested_at=''"""
        )
        initial_acceptance_reasons = {
            "youtube": "未配置测试账号与 OAuth 令牌",
            "tiktok": "官方 API 测试账号与应用审核状态尚未配置",
            "instagram": "未配置 Instagram Business OAuth 测试账号",
            "facebook": "未配置 Facebook Page OAuth 测试账号",
            "xiaohongshu": "未配置扫码登录测试账号",
            "kuaishou": "未配置扫码登录测试账号",
            "wechat_channels": "未配置扫码登录测试账号",
            "bilibili": "未列入当前验收批次",
            "x": "未列入当前验收批次",
        }
        for code, reason in initial_acceptance_reasons.items():
            conn.execute(
                """UPDATE platforms SET capability_status_reason=?
                   WHERE code=? AND capability_status='untested' AND capability_status_reason=''""",
                (reason, code),
            )
        # These four browser publishers have not completed a real publication
        # acceptance run. Keep them gated regardless of adapter availability.
        # Douyin is intentionally excluded and its passed status is untouched.
        guarded_reasons = {
            "kuaishou": "浏览器适配器已加异常保护，尚未完成真实发布验收",
            "xiaohongshu": "浏览器适配器已加异常保护，尚未完成真实发布验收",
            "wechat_channels": "浏览器适配器已加异常保护，尚未完成真实发布验收",
            "bilibili": "浏览器适配器已加异常保护，尚未完成真实发布验收",
        }
        for code, reason in guarded_reasons.items():
            conn.execute(
                """UPDATE platforms SET capability_status='untested',capability_status_reason=?
                   WHERE code=? AND last_tested_at='' AND capability_status<>'passed'""",
                (reason, code),
            )
        _seed_default_sfx(conn)


def database_user_version(path: Path = DB_PATH) -> int:
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def create_migration_backup(path: Path = DB_PATH, target_version: int = SCHEMA_VERSION) -> Path | None:
    if not path.exists() or database_user_version(path) >= target_version:
        return None
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = backup_dir / f"{path.stem}_schema_{database_user_version(path)}_to_{target_version}_{stamp}.db"
    with closing(sqlite3.connect(path)) as source:
        with closing(sqlite3.connect(backup_path)) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("数据库升级备份完整性检查失败")
    return backup_path


def restore_migration_backup(backup_path: Path, path: Path = DB_PATH) -> None:
    if not backup_path.exists():
        raise FileNotFoundError(backup_path)
    failed_path = path.with_suffix(path.suffix + ".migration_failed")
    if path.exists():
        shutil.copy2(path, failed_path)
    temp_restore = path.with_suffix(path.suffix + ".restore_tmp")
    shutil.copy2(backup_path, temp_restore)
    os.replace(temp_restore, path)


def init_db():
    """Run idempotent migrations with backup, integrity check and rollback."""
    ensure_runtime_dirs()
    backup_path = create_migration_backup()
    try:
        _init_db_impl()
        with connect() as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("数据库升级后完整性检查失败")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    except Exception:
        if backup_path:
            restore_migration_backup(backup_path)
        raise


def ensure_column(conn, table, column, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def migrate_publish_group_batch_items_v15(conn) -> None:
    """Allow blocked rows without tasks and retain readable account snapshots."""
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(publish_group_batch_items)")}
    if not columns or "task_id" not in columns or not int(columns["task_id"]["notnull"]):
        return
    conn.execute("DROP TABLE IF EXISTS publish_group_batch_items_v15")
    conn.execute(
        """CREATE TABLE publish_group_batch_items_v15 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            task_id INTEGER,
            account_id INTEGER,
            group_id INTEGER,
            group_batch_id INTEGER,
            account_name_snapshot TEXT NOT NULL DEFAULT '',
            platform_name_snapshot TEXT NOT NULL DEFAULT '',
            platform_code_snapshot TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            blocked_reason TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES publish_group_batches(id) ON DELETE CASCADE,
            FOREIGN KEY(task_id) REFERENCES publish_tasks(id) ON DELETE SET NULL,
            FOREIGN KEY(account_id) REFERENCES publish_accounts(id) ON DELETE SET NULL,
            UNIQUE(batch_id,account_id)
        )"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO publish_group_batch_items_v15(
               id,batch_id,task_id,account_id,group_id,group_batch_id,
               account_name_snapshot,platform_name_snapshot,platform_code_snapshot,
               status,blocked_reason,error_code,retry_count,created_at,updated_at)
           SELECT i.id,i.batch_id,i.task_id,i.account_id,
                  COALESCE(i.group_id,b.group_id),COALESCE(i.group_batch_id,i.batch_id),
                  COALESCE(NULLIF(i.account_name_snapshot,''),a.account_name,''),
                  COALESCE(NULLIF(i.platform_name_snapshot,''),p.name,''),
                  COALESCE(NULLIF(i.platform_code_snapshot,''),p.code,''),
                  i.status,COALESCE(i.blocked_reason,''),COALESCE(i.error_code,''),
                  COALESCE(i.retry_count,0),i.created_at,i.updated_at
           FROM publish_group_batch_items i
           LEFT JOIN publish_group_batches b ON b.id=i.batch_id
           LEFT JOIN publish_accounts a ON a.id=i.account_id
           LEFT JOIN platforms p ON p.id=a.platform_id"""
    )
    conn.execute("DROP TABLE publish_group_batch_items")
    conn.execute("ALTER TABLE publish_group_batch_items_v15 RENAME TO publish_group_batch_items")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publish_group_batch_items_batch ON publish_group_batch_items(batch_id,status,id)")
