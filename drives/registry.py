"""Definitions and factories for Cloudrave's built-in drives."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.cfg import CloudraveConfig
from drives.ids import (
    ALIYUN_OSS,
    DRIVE_139,
    DRIVE_189,
    DRIVE_WO,
    LOCAL_FS,
    TENCENT_COS,
)
from store.types import StorageAdapter, StorageCapability


DriveFactory = Callable[[CloudraveConfig, Path], StorageAdapter]
UserMountValidator = Callable[[Any, str, dict[str, Any]], None]


def _validate_local_user_mount(actor: Any, mount_slug: str, extra: dict[str, Any]) -> None:
    raw_root = str(extra.get("storage_root") or f"/upload/{actor.uid}/{mount_slug}/")
    normalized = "/" + raw_root.replace("\\", "/").strip("/")
    owner_root = f"/upload/{actor.uid}"
    if normalized != owner_root and not normalized.startswith(f"{owner_root}/"):
        raise ValueError("Local storage path must stay inside the current user's upload directory")
    extra["storage_root"] = f"{normalized}/"


@dataclass(frozen=True, slots=True)
class DriverDefinition:
    id: str
    label: str
    kind: str
    icon: str
    factory: DriveFactory
    credential_fields: tuple[str, ...] = ()
    optional_credential_fields: tuple[str, ...] = ()
    legacy_credential_aliases: Mapping[str, str] | None = None
    user_mount_validator: UserMountValidator | None = None

    def validate_credentials(
        self,
        credentials: Mapping[str, object] | None,
        *,
        require: bool,
    ) -> dict[str, str]:
        values = {
            str(key): str(value).strip()
            for key, value in (credentials or {}).items()
            if value is not None and str(value).strip()
        }
        allowed = set(self.credential_fields) | set(self.optional_credential_fields)
        unknown = sorted(set(values).difference(allowed))
        if unknown:
            raise ValueError(f"Unsupported credential field(s) for {self.id}: {', '.join(unknown)}")
        missing = [field for field in self.credential_fields if not values.get(field)]
        if require and missing:
            raise ValueError(f"Missing credential field(s) for {self.id}: {', '.join(missing)}")
        return values

    def normalize_submitted_credentials(self, payload: Any) -> dict[str, str]:
        values = dict(getattr(payload, "credentials", None) or {})
        for legacy_field, target_field in (self.legacy_credential_aliases or {}).items():
            value = getattr(payload, legacy_field, None)
            if value is not None and str(value).strip():
                values[target_field] = str(value).strip()
        return values

    def validate_user_mount(self, actor: Any, mount_slug: str, extra: dict[str, Any]) -> None:
        if self.user_mount_validator is not None:
            self.user_mount_validator(actor, mount_slug, extra)


class DriveRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, DriverDefinition] = {}

    @property
    def definitions(self) -> tuple[DriverDefinition, ...]:
        return tuple(self._definitions.values())

    def register(self, definition: DriverDefinition) -> None:
        if not definition.id or definition.id in self._definitions:
            raise ValueError(f"Drive {definition.id!r} is invalid or already registered")
        self._definitions[definition.id] = definition

    def require(self, provider: object) -> DriverDefinition:
        from drives.ids import provider_id

        normalized = provider_id(provider)
        definition = self._definitions.get(normalized)
        if definition is None:
            raise KeyError(f"Drive {normalized!r} is not registered")
        return definition

    def create_all(self, *, config: CloudraveConfig, local_root: Path) -> dict[str, StorageAdapter]:
        result: dict[str, StorageAdapter] = {}
        for definition in self.definitions:
            adapter = definition.factory(config, local_root)
            if getattr(adapter, "provider_type", None) != definition.id:
                raise ValueError(f"Drive adapter {type(adapter).__name__} declares the wrong provider ID")
            if not isinstance(getattr(adapter, "capabilities", None), StorageCapability):
                raise TypeError(f"Drive adapter {type(adapter).__name__} has invalid capabilities")
            result[definition.id] = adapter
        return result

    def public_catalog(self, adapters: Mapping[str, StorageAdapter]) -> list[dict[str, Any]]:
        return [
            {
                "id": definition.id,
                "label": definition.label,
                "kind": definition.kind,
                "icon_key": definition.icon,
                "enabled": definition.id in adapters,
                "capabilities": list(adapters[definition.id].capabilities.names())
                if definition.id in adapters
                else [],
            }
            for definition in self.definitions
        ]


def default_drive_registry() -> DriveRegistry:
    from drives.drive_139 import Drive139Client
    from drives.drive_189 import Drive189Client
    from drives.drive_wo import DriveWoClient
    from drives.remote_drive import RemoteDirectAdapter
    from store.cos import TencentCosAdapter
    from store.local import LocalFsAdapter
    from store.oss import AliyunOssAdapter

    registry = DriveRegistry()
    registry.register(
        DriverDefinition(
            id=LOCAL_FS,
            label="Local Storage",
            kind="local",
            icon="local_fs",
            factory=lambda _config, local_root: LocalFsAdapter(local_root),
            user_mount_validator=_validate_local_user_mount,
        )
    )
    registry.register(
        DriverDefinition(
            id=TENCENT_COS,
            label="Tencent COS",
            kind="object_storage",
            icon="tencent_cos",
            credential_fields=("secret_id", "secret_key"),
            optional_credential_fields=("session_token",),
            legacy_credential_aliases={
                "secret_id": "secret_id",
                "secret_key": "secret_key",
                "session_token": "session_token",
            },
            factory=lambda _config, _local_root: TencentCosAdapter(),
        )
    )
    registry.register(
        DriverDefinition(
            id=ALIYUN_OSS,
            label="Aliyun OSS",
            kind="object_storage",
            icon="aliyun_oss",
            credential_fields=("access_key_id", "access_key_secret"),
            optional_credential_fields=("security_token",),
            factory=lambda _config, _local_root: AliyunOssAdapter(),
        )
    )
    registry.register(
        DriverDefinition(
            id=DRIVE_139,
            label="139zhey",
            kind="consumer_cloud",
            icon="drive_139",
            credential_fields=("authorization", "user_domain_id"),
            optional_credential_fields=("root_id",),
            factory=lambda _config, local_root: RemoteDirectAdapter(DRIVE_139, Drive139Client, local_root),
        )
    )
    registry.register(
        DriverDefinition(
            id=DRIVE_189,
            label="189 Cloud",
            kind="consumer_cloud",
            icon="drive_189",
            credential_fields=("session",),
            optional_credential_fields=("root_id",),
            factory=lambda _config, local_root: RemoteDirectAdapter(DRIVE_189, Drive189Client, local_root),
        )
    )
    registry.register(
        DriverDefinition(
            id=DRIVE_WO,
            label="Wo Cloud",
            kind="consumer_cloud",
            icon="drive_wo",
            credential_fields=("token",),
            optional_credential_fields=("root_id",),
            factory=lambda _config, local_root: RemoteDirectAdapter(DRIVE_WO, DriveWoClient, local_root),
        )
    )
    return registry
