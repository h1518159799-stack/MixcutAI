"""Generate update.json through the shared protocol model."""

from __future__ import annotations

import argparse
from pathlib import Path

from shared.update_manifest import UpdateArtifact, UpdateManifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--main-url", required=True)
    parser.add_argument("--main-sha256", required=True)
    parser.add_argument("--main-size", required=True, type=int)
    parser.add_argument("--updater-url", required=True)
    parser.add_argument("--updater-sha256", required=True)
    parser.add_argument("--updater-size", required=True, type=int)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--runtime-url", required=True)
    parser.add_argument("--release-notes", default="正式授权与更新域名迁移，并包含当前稳定功能修复。")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    manifest = UpdateManifest(
        version=args.version,
        download_url=args.main_url,
        sha256=args.main_sha256.lower(),
        file_size=args.main_size,
        release_notes=args.release_notes,
        updater_version=args.version,
        force_update=False,
        updater_artifact=UpdateArtifact(args.updater_url, args.updater_sha256.lower(), args.updater_size),
        runtime_version=args.runtime_version,
        runtime_url=args.runtime_url,
    )
    manifest = UpdateManifest.from_dict(manifest.to_dict())
    # Legacy aliases remain during the bootstrap migration so old clients can
    # still install the first release that understands the canonical fields.
    Path(args.output).write_text(manifest.to_json(include_legacy_aliases=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
