"""Local secret storage for AI/TTS provider credentials.

The desktop application is Windows-only.  Values are encrypted with the
existing DPAPI helper and the API layer only exposes a configured flag.
"""

from __future__ import annotations

from oauth_token_store import protect, unprotect


PREFIX = "dpapi:"


def encrypt_secret(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith(PREFIX):
        return value
    return PREFIX + protect(value)


def decrypt_secret(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if value.startswith(PREFIX):
        return unprotect(value[len(PREFIX):])
    # Legacy databases stored provider keys as plaintext.  The startup
    # migration rewrites them immediately; accepting them here keeps upgrades
    # recoverable if a migration was interrupted.
    return value


def secret_is_configured(value: str) -> bool:
    return bool(str(value or "").strip())


def public_service_config(config: dict | None) -> dict:
    result = dict(config or {})
    configured = secret_is_configured(result.get("api_key"))
    result["api_key"] = ""
    result["api_key_configured"] = configured
    result["api_key_masked"] = "••••••••••••••••" if configured else ""
    return result


def runtime_service_config(config: dict | None) -> dict:
    result = dict(config or {})
    result["api_key"] = decrypt_secret(result.get("api_key", ""))
    return result
