"""Single source of truth for the Mixcut online-update protocol.

The six canonical manifest fields are deliberately stable.  Optional updater
and runtime artifacts are extensions, not alternate manifest models.  The
parser accepts the legacy ``size_bytes``/``content`` keys so already published
clients can migrate without weakening integrity checks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class UpdateManifestError(ValueError):
    pass


def _https(value: Any, label: str) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise UpdateManifestError(f"{label}必须使用有效的 HTTPS 地址")
    return text


def _sha256(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise UpdateManifestError(f"{label}中的 SHA256 无效")
    return text


def _positive_int(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise UpdateManifestError(f"{label}无效") from exc
    if number <= 0:
        raise UpdateManifestError(f"{label}必须大于0")
    return number


def _version(value: Any, label: str = "version") -> str:
    text = str(value or "").strip().lower().lstrip("v")
    core = text.split("-", 1)[0]
    if not core or any(not part.isdigit() for part in core.split(".")):
        raise UpdateManifestError(f"{label}无效：{value}")
    return text


@dataclass(frozen=True)
class UpdateArtifact:
    url: str
    sha256: str
    file_size: int

    @classmethod
    def from_dict(cls, payload: dict[str, Any], label: str) -> "UpdateArtifact":
        return cls(
            url=_https(payload.get("url"), f"{label}下载地址"),
            sha256=_sha256(payload.get("sha256"), label),
            file_size=_positive_int(payload.get("file_size", payload.get("size_bytes")), f"{label}文件大小"),
        )

    def to_dict(self, *, include_legacy_aliases: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {"url": self.url, "sha256": self.sha256, "file_size": self.file_size}
        if include_legacy_aliases:
            result["size_bytes"] = self.file_size
        return result


@dataclass(frozen=True)
class UpdateManifest:
    # Canonical protocol fields shared by MixcutAI.exe, MixcutUpdater.exe and update.json.
    version: str
    download_url: str
    sha256: str
    file_size: int
    release_notes: str
    updater_version: str

    # Backward-compatible protocol extensions.
    force_update: bool = False
    updater_artifact: UpdateArtifact | None = None
    runtime_version: str | None = None
    runtime_url: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "UpdateManifest":
        if not isinstance(payload, dict):
            raise UpdateManifestError("update.json 顶层必须是对象")
        missing = [key for key in ("version", "download_url", "sha256") if key not in payload]
        if "file_size" not in payload and "size_bytes" not in payload:
            missing.append("file_size")
        if missing:
            raise UpdateManifestError("update.json 缺少字段：" + ", ".join(missing))

        version = _version(payload.get("version"))
        updater_version = _version(payload.get("updater_version") or version, "updater_version")
        updater_artifact = None
        updater_payload = payload.get("updater")
        if isinstance(updater_payload, dict):
            try:
                updater_artifact = UpdateArtifact.from_dict(updater_payload, "更新器")
            except UpdateManifestError:
                # Incomplete legacy metadata disables bootstrap repair only.
                updater_artifact = None

        runtime_version = str(payload.get("runtime_version") or "").strip() or None
        runtime_url = str(payload.get("runtime_url") or "").strip() or None
        if runtime_url:
            runtime_url = _https(runtime_url, "运行时清单地址")

        return cls(
            version=version,
            download_url=_https(payload.get("download_url"), "新版下载地址"),
            sha256=_sha256(payload.get("sha256"), "update.json"),
            file_size=_positive_int(payload.get("file_size", payload.get("size_bytes")), "新版文件大小"),
            release_notes=str(payload.get("release_notes", payload.get("content", "")) or "").strip(),
            updater_version=updater_version,
            force_update=bool(payload.get("force_update", False)),
            updater_artifact=updater_artifact,
            runtime_version=runtime_version,
            runtime_url=runtime_url,
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> "UpdateManifest":
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateManifestError("update.json 格式无效") from exc
        return cls.from_dict(payload)

    @classmethod
    def from_file(cls, path: str | Path) -> "UpdateManifest":
        try:
            return cls.from_json_bytes(Path(path).read_bytes())
        except OSError as exc:
            raise UpdateManifestError(f"无法读取更新清单：{exc}") from exc

    def to_dict(self, *, include_legacy_aliases: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "version": self.version,
            "download_url": self.download_url,
            "sha256": self.sha256,
            "file_size": self.file_size,
            "release_notes": self.release_notes,
            "updater_version": self.updater_version,
            "force_update": self.force_update,
        }
        if self.updater_artifact:
            result["updater"] = self.updater_artifact.to_dict(include_legacy_aliases=include_legacy_aliases)
        if self.runtime_version and self.runtime_url:
            result["runtime_version"] = self.runtime_version
            result["runtime_url"] = self.runtime_url
        if include_legacy_aliases:
            result.update(
                title=f"Mixcut AI v{self.version}",
                content=self.release_notes,
                size_bytes=self.file_size,
            )
        return result

    def to_json(self, *, include_legacy_aliases: bool = False) -> str:
        return json.dumps(self.to_dict(include_legacy_aliases=include_legacy_aliases), ensure_ascii=False, indent=2) + "\n"

    # Read-only compatibility properties for existing download/update code.
    @property
    def size_bytes(self) -> int:
        return self.file_size

    @property
    def content(self) -> str:
        return self.release_notes

    @property
    def title(self) -> str:
        return f"Mixcut AI v{self.version}"

    @property
    def updater_url(self) -> str | None:
        return self.updater_artifact.url if self.updater_artifact else None

    @property
    def updater_sha256(self) -> str | None:
        return self.updater_artifact.sha256 if self.updater_artifact else None

    @property
    def updater_size_bytes(self) -> int | None:
        return self.updater_artifact.file_size if self.updater_artifact else None

    def public_dict(self) -> dict[str, Any]:
        return self.to_dict()
