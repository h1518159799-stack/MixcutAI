"""Unified publishing entry point reserved for the next delivery phase."""

from __future__ import annotations

from platforms import get_adapter


def publish(adapter_code: str, video: str, title: str, tags: list[str] | None = None, schedule: str | None = None):
    """Unified signature. Concrete upload/publish operations are intentionally disabled."""
    adapter = get_adapter(adapter_code)
    return adapter.publish(video, title, tags or [], schedule)
