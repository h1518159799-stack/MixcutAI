"""Prepare an isolated signed-license home for release acceptance tests."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-home", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="")
    parser.add_argument("--ffprobe", default="")
    parser.add_argument("--server-url", default="")
    parser.add_argument("--copy-db", type=Path)
    args = parser.parse_args()
    os.environ["MIXCUT_APP_HOME"] = str(args.app_home.resolve())
    if args.copy_db:
        target = args.app_home / "data" / "mixcut.db"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.copy_db, target)

    from admin_tools.issue_license import issue
    from db import connect, init_db, now
    from license_module import activate_license, machine_id

    init_db()
    settings = {
        "ffmpeg_path": args.ffmpeg,
        "ffprobe_path": args.ffprobe,
        "license_server_url": args.server_url,
        "output_dir": str((args.app_home / "outputs").resolve()),
        "work_dir": str((args.app_home / "data" / "work_cache").resolve()),
    }
    with connect() as conn:
        for key, value in settings.items():
            conn.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, value, now()),
            )
    state = activate_license(issue(machine_id(), "permanent"))
    print(f"QA_HOME_READY={args.app_home.resolve()}")
    print(f"LICENSE={state.status}:{state.license_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
