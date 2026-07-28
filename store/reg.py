"""Storage driver registry."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeAlias

from core.cfg import CloudraveConfig
from models.enums import StoragePolicyType
from store.types import StorageAdapter, StorageCapability

StoreFactory: TypeAlias = Callable[[CloudraveConfig, Path], StorageAdapter]


class StoreRegistry:
    """Create storage drivers without coupling services to providers."""

    def __init__(self) -> None:
        self._factories: dict[str, StoreFactory] = {}

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(self._factories)

    def register(self, provider: str, factory: StoreFactory) -> None:
        name = provider.strip()
        if not name:
            raise ValueError("storage provider is required")
        if name in self._factories:
            raise ValueError(f"storage provider {name!r} is already registered")
        self._factories[name] = factory

    def create(
        self,
        provider: str,
        *,
        config: CloudraveConfig,
        local_root: Path,
    ) -> StorageAdapter:
        factory = self._factories.get(provider)
        if factory is None:
            raise KeyError(f"storage provider {provider!r} is not registered")
        adapter = factory(config, local_root)
        if getattr(adapter, "provider_type", None) != provider:
            raise ValueError(
                f"storage driver {type(adapter).__name__} declares "
                f"{getattr(adapter, 'provider_type', None)!r}, expected {provider!r}"
            )
        if not isinstance(getattr(adapter, "capabilities", None), StorageCapability):
            raise TypeError(f"storage driver {type(adapter).__name__} has invalid capabilities")
        return adapter

    def create_all(
        self,
        *,
        config: CloudraveConfig,
        local_root: Path,
    ) -> dict[str, StorageAdapter]:
        return {
            provider: self.create(
                provider,
                config=config,
                local_root=local_root,
            )
            for provider in self.providers
        }


def default_registry() -> StoreRegistry:
    """Return a fresh registry containing the built-in drivers."""

    from store.local import LocalFsAdapter
    from store.cos import TencentCosAdapter

    registry = StoreRegistry()
    registry.register(
        StoragePolicyType.LOCAL_FS.value,
        lambda _config, local_root: LocalFsAdapter(local_root),
    )
    registry.register(
        StoragePolicyType.TENCENT_COS.value,
        lambda config, _local_root: TencentCosAdapter(config.tencent_cos),
    )
    return registry
