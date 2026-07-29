CREATE TABLE account_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            industry TEXT DEFAULT '',
            product_info TEXT DEFAULT '',
            brand_intro TEXT DEFAULT '',
            persona_info TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE ai_service_configs (
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
CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        );
CREATE TABLE batch_task_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                index_no INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                output_path TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES batch_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES generation_tasks(id) ON DELETE CASCADE,
                UNIQUE(batch_id,index_no)
            );
CREATE TABLE batch_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                template_id INTEGER,
                total_count INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                material_strategy TEXT NOT NULL DEFAULT 'random',
                category_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT DEFAULT '',
                FOREIGN KEY(template_id) REFERENCES edit_templates(id) ON DELETE SET NULL
            );
CREATE TABLE bgm_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE,
                duration REAL DEFAULT 0,
                volume_default REAL DEFAULT 0.15,
                created_at TEXT NOT NULL
            );
CREATE TABLE edit_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            output_width INTEGER DEFAULT 1080,
            output_height INTEGER DEFAULT 1920,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        , is_hidden INTEGER DEFAULT 0, source_template_id INTEGER);
CREATE TABLE generation_shot_materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shot_id INTEGER NOT NULL,
                material_id INTEGER NOT NULL,
                voice_duration REAL DEFAULT 0,
                material_start_time REAL DEFAULT 0,
                material_end_time REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, task_id INTEGER,
                FOREIGN KEY(shot_id) REFERENCES template_shots(id) ON DELETE CASCADE,
                FOREIGN KEY(material_id) REFERENCES materials(id) ON DELETE CASCADE
            );
CREATE TABLE generation_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER DEFAULT 0,
            output_path TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        , template_id INTEGER, current_shot TEXT DEFAULT '', total_shots INTEGER DEFAULT 0);
CREATE TABLE license_keys (
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
CREATE TABLE material_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE materials (
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
            thumbnail_path TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            remark TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(category_id) REFERENCES material_categories(id) ON DELETE SET NULL
        );
CREATE TABLE oauth_sessions (
                state TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL,
                code_verifier TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES publish_accounts(id) ON DELETE CASCADE
            );
CREATE TABLE oauth_tokens (
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
CREATE TABLE platform_test_runs (
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
CREATE TABLE platforms (
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            , integration_type TEXT NOT NULL DEFAULT '', adapter_code TEXT NOT NULL DEFAULT '', capability_status TEXT NOT NULL DEFAULT 'untested', capability_status_reason TEXT NOT NULL DEFAULT '', last_tested_at TEXT NOT NULL DEFAULT '');
CREATE TABLE publish_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_id INTEGER NOT NULL,
                account_name TEXT NOT NULL,
                login_status TEXT NOT NULL DEFAULT 'not_logged_in',
                browser_profile_path TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                publishing_mode TEXT NOT NULL DEFAULT 'browser',
                api_config_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, authorization_status TEXT NOT NULL DEFAULT 'not_authorized', channel_id TEXT NOT NULL DEFAULT '', channel_title TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(platform_id) REFERENCES platforms(id) ON DELETE RESTRICT,
                UNIQUE(platform_id,account_name)
            );
CREATE TABLE publish_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publish_task_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                platform_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT NOT NULL DEFAULT '',
                remote_post_id TEXT NOT NULL DEFAULT '',
                remote_url TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '', technical_log_id TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL DEFAULT '', video_file_hash TEXT NOT NULL DEFAULT '', title_hash TEXT NOT NULL DEFAULT '', result_evidence_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(publish_task_id) REFERENCES publish_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(account_id) REFERENCES publish_accounts(id) ON DELETE RESTRICT,
                FOREIGN KEY(platform_id) REFERENCES platforms(id) ON DELETE RESTRICT,
                UNIQUE(publish_task_id,account_id)
            );
CREATE TABLE publish_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generation_task_id INTEGER,
                video_path TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                cover_path TEXT NOT NULL DEFAULT '',
                scheduled_at TEXT NOT NULL DEFAULT '',
                delete_after_publish INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                total_channels INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '', ai_title TEXT NOT NULL DEFAULT '', ai_description TEXT NOT NULL DEFAULT '', ai_hashtags TEXT NOT NULL DEFAULT '[]', cover_text TEXT NOT NULL DEFAULT '', ai_generated_time TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '', technical_log_id TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(generation_task_id) REFERENCES generation_tasks(id) ON DELETE SET NULL
            );
CREATE TABLE scan_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_path TEXT NOT NULL,
            status TEXT NOT NULL,
            total_files INTEGER DEFAULT 0,
            scanned_files INTEGER DEFAULT 0,
            added_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0,
            error_message TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            finished_at TEXT
        );
CREATE TABLE shot_transitions (
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
CREATE TABLE sound_effect_library (
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
CREATE TABLE template_shots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL,
            shot_name TEXT NOT NULL,
            original_text TEXT DEFAULT '',
            ai_text TEXT DEFAULT '',
            material_category_id INTEGER,
            duration REAL DEFAULT 3,
            mute_original_audio INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, enable_subtitle INTEGER DEFAULT 1, enable_tts INTEGER DEFAULT 1, estimated_duration REAL DEFAULT 3, voice_file_path TEXT DEFAULT '', voice_duration REAL DEFAULT 0, subtitle_file_path TEXT DEFAULT '', tts_voice_id INTEGER, voice_text_hash TEXT DEFAULT '', subtitle_render_hash TEXT DEFAULT '',
            FOREIGN KEY(template_id) REFERENCES edit_templates(id) ON DELETE CASCADE,
            FOREIGN KEY(material_category_id) REFERENCES material_categories(id) ON DELETE SET NULL
        );
CREATE TABLE tts_service_configs (
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
CREATE TABLE tts_voices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                voice_code TEXT NOT NULL,
                is_default INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(service_id) REFERENCES tts_service_configs(id) ON DELETE CASCADE
            );
CREATE INDEX idx_generation_shot_materials_task_shot ON generation_shot_materials(task_id, shot_id);
CREATE INDEX idx_batch_task_items_batch ON batch_task_items(batch_id,index_no);
CREATE INDEX idx_batch_task_items_task ON batch_task_items(task_id);
CREATE INDEX idx_publish_accounts_platform ON publish_accounts(platform_id,status);
CREATE INDEX idx_publish_tasks_status ON publish_tasks(status,scheduled_at,id);
CREATE INDEX idx_publish_channels_task ON publish_channels(publish_task_id,status,id);
CREATE INDEX idx_shot_transitions_pair ON shot_transitions(from_shot_id,to_shot_id);
CREATE INDEX idx_sound_effect_category ON sound_effect_library(category,id);
CREATE INDEX idx_platform_test_runs_platform ON platform_test_runs(platform_id,status,id);
