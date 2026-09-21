"""Storage manager that binds registered drives to encrypted policy credentials."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.cfg import CloudraveConfig
from drives.credentials import CredentialVault
from drives.ids import provider_id
from drives.registry import DriveRegistry, default_drive_registry
from models.tables import BucketMount, StoragePolicy
from store.types import join_object_key, normalize_object_key


class _PolicyContext:
    """Read-only policy view containing decrypted credentials for one call."""

    def __init__(self, policy: StoragePolicy, credentials: dict[str, str]) -> None:
        self._policy = policy
        self.credentials = credentials

    def __getattr__(self, name: str) -> Any:
        aliases = {
            "secret_id": "secret_id",
            "secret_key": "secret_key",
            "session_token": "session_token",
            "access_key_id": "access_key_id",
            "access_key_secret": "access_key_secret",
            "security_token": "security_token",
        }
        if name in aliases:
            return self.credentials.get(aliases[name])
        return getattr(self._policy, name)


class _PolicyBoundAdapter:
    """Replace the public ORM policy with its short-lived decrypted view."""

    def __init__(self, adapter: Any, policy: StoragePolicy, credentials: dict[str, str]) -> None:
        self._adapter = adapter
        self._policy = policy
        self._context = _PolicyContext(policy, credentials)
        self.provider_type = adapter.provider_type
        self.capabilities = adapter.capabilities

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._adapter, name)
        if not callable(value):
            return value

        def bound(*args: Any, **kwargs: Any) -> Any:
            if args and args[0] is self._policy:
                args = (self._context, *args[1:])
            return value(*args, **kwargs)

        return bound


class StorageManager:
    def __init__(
        self,
        *,
        config: CloudraveConfig,
        local_root: str | Path,
        registry: DriveRegistry | None = None,
    ) -> None:
        self.registry = registry or default_drive_registry()
        self.adapters = self.registry.create_all(config=config, local_root=Path(local_root))
        self.vault = CredentialVault(config.storage.credential_key)

    def get_adapter(self, policy: StoragePolicy):
        key = provider_id(policy.provider)
        adapter = self.adapters.get(key)
        if adapter is None:
            raise KeyError(f"Storage drive {key!r} is not registered")
        return _PolicyBoundAdapter(adapter, policy, self.credentials_for(policy))

    def definition_for(self, provider: object):
        return self.registry.require(provider)

    def validate_user_mount(
        self,
        provider: object,
        actor: Any,
        mount_slug: str,
        extra: dict[str, Any],
    ) -> None:
        self.definition_for(provider).validate_user_mount(actor, mount_slug, extra)

    def catalog(self) -> list[dict[str, Any]]:
        return self.registry.public_catalog(self.adapters)

    def credentials_for(self, policy: StoragePolicy) -> dict[str, str]:
        return self.vault.open(policy.credential_ciphertext)

    def credential_state(self, policy: StoragePolicy) -> tuple[bool, list[str]]:
        credentials = self.credentials_for(policy)
        return bool(credentials), sorted(credentials)

    def credentials_from_payload(
        self,
        provider: object,
        payload: Any,
        *,
        current: StoragePolicy | None = None,
    ) -> tuple[str | None, int]:
        definition = self.definition_for(provider)
        submitted = definition.normalize_submitted_credentials(payload)

        existing = self.credentials_for(current) if current is not None else {}
        if current is not None and not submitted:
            return current.credential_ciphertext, current.credential_version or self.vault.VERSION
        merged = {**existing, **definition.validate_credentials(submitted, require=False)}
        validated = definition.validate_credentials(merged, require=bool(definition.credential_fields))
        return (self.vault.seal(validated) if validated else None, self.vault.VERSION)

    def local_path(
        self,
        policy: StoragePolicy,
        mount: BucketMount,
        object_key: str,
    ) -> Path | None:
        return self.get_adapter(policy).local_path(mount, object_key)

    def build_object_key(self, mount: BucketMount, file_name: str, parent_blob_path: str | None = None) -> str:
        if parent_blob_path:
            return join_object_key(parent_blob_path, file_name)
        return normalize_object_key(file_name)
