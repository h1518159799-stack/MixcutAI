"""Release gate for MixcutAI/Updater/manifest version consistency."""

from __future__ import annotations

import argparse
from pathlib import Path

from shared.update_manifest import UpdateManifest


def clean(value: str) -> str:
    return str(value or "").strip().lower().lstrip("v").split("-", 1)[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-version", required=True)
    parser.add_argument("--updater-version", required=True)
    parser.add_argument("--updater-runtime-version", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--source-version", required=True)
    args = parser.parse_args()

    manifest = UpdateManifest.from_file(args.manifest)
    versions = {
        "MixcutAI.exe": clean(args.main_version),
        "MixcutUpdater.exe": clean(args.updater_version),
        "MixcutUpdater runtime": clean(args.updater_runtime_version),
        "update.json": clean(manifest.version),
        "update.json.updater_version": clean(manifest.updater_version),
        "VERSION.txt": clean(Path(args.source_version).read_text(encoding="utf-8-sig")),
    }
    expected = versions["VERSION.txt"]
    mismatched = {name: value for name, value in versions.items() if value != expected}
    if mismatched:
        details = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise SystemExit(f"UPDATE_PROTOCOL_VERSION_MISMATCH expected={expected}; {details}")
    print("UPDATE_PROTOCOL_VERSION_GATE_OK " + ", ".join(f"{name}={value}" for name, value in versions.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
