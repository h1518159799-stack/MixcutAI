"""Publisher contracts. Upload execution is intentionally disabled in phase one."""

from __future__ import annotations

from typing import Any


class PublishingNotEnabledError(RuntimeError):
    pass


class Publisher:
    platform_code = ""
    integration_type = "browser"

    def publish(self, video: str, account: dict, metadata: dict) -> dict[str, Any]:
        raise PublishingNotEnabledError(f"{self.platform_code} 上传发布尚未启用")
