"""Shared protocol models used by both MixcutAI and MixcutUpdater."""

from .update_manifest import UpdateArtifact, UpdateManifest, UpdateManifestError

__all__ = ["UpdateArtifact", "UpdateManifest", "UpdateManifestError"]
