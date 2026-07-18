"""Small, cursor-based bucket listing boundary for mount synchronization."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.tables import BucketMount, StoragePolicy
from app.storage.manager import StorageManager
from app.storage.key import is_managed_key


@dataclass(slots=True, frozen=True)
class BucketItem:
    key: str
    physical_key: str
    size: int
    etag: str | None
    last_modified: str | None
    storage_class: str | None
    physical_id: str = ""


@dataclass(slots=True, frozen=True)
class BucketPage:
    items: tuple[BucketItem, ...]
    next_cursor: str | None
    complete: bool


class BucketScanner:
    def __init__(self, storage_manager: StorageManager) -> None:
        self.storage_manager = storage_manager

    async def list_page(
        self,
        policy: StoragePolicy,
        mount: BucketMount,
        *,
        cursor: str | None,
        page_size: int,
    ) -> BucketPage:
        adapter = self.storage_manager.get_adapter(policy)
        page = await adapter.alist_objects(
            policy,
            mount,
            prefix="",
            marker=cursor or "",
            max_keys=page_size,
        )
        items: list[BucketItem] = []
        for raw in page.objects:
            physical_key = str(raw.storage_path).replace("\\", "/")
            if not raw.object_key or physical_key.endswith("/") or is_managed_key(raw.object_key):
                continue
            items.append(
                BucketItem(
                    key=raw.object_key,
                    physical_key=physical_key,
                    physical_id=adapter.physical_id(policy, mount, raw.object_key),
                    size=int(raw.size or 0),
                    etag=_optional_text(raw.etag, strip_quotes=True),
                    last_modified=_optional_text(raw.metadata.get("last_modified")),
                    storage_class=_optional_text(raw.metadata.get("storage_class")),
                )
            )
        if page.is_truncated and (not page.next_marker or page.next_marker == cursor):
            raise RuntimeError("COS returned a truncated page without a continuation marker")
        return BucketPage(
            items=tuple(items),
            next_cursor=page.next_marker,
            complete=not page.is_truncated,
        )

    async def exists(self, policy: StoragePolicy, mount: BucketMount, key: str) -> bool:
        adapter = self.storage_manager.get_adapter(policy)
        try:
            await adapter.ahead_object(policy, mount, key)
        except Exception:
            return False
        return True


def _optional_text(value: object, *, strip_quotes: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if strip_quotes:
        text = text.strip('"').strip()
    return text or None
