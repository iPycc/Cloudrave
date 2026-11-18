"""Small, cursor-based bucket listing boundary for mount synchronization."""

from __future__ import annotations

from dataclasses import dataclass

from models.tables import BucketMount, StoragePolicy
from store.manager import StorageManager
from store.key import LEGACY_MANAGED_PREFIX
from store.call import call_adapter
from store.errors import is_not_found


@dataclass(slots=True, frozen=True)
class BucketItem:
    key: str
    physical_key: str
    size: int
    etag: str | None
    last_modified: str | None
    storage_class: str | None
    physical_id: str = ""
    is_directory: bool = False


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
        page = await call_adapter(
            adapter,
            "list_objects",
            policy,
            mount,
            prefix="",
            marker=cursor or "",
            max_keys=page_size,
        )
        items: list[BucketItem] = []
        for raw in page.objects:
            physical_key = str(raw.storage_path).replace("\\", "/")
            if (
                not raw.object_key
                or raw.object_key == LEGACY_MANAGED_PREFIX
                or raw.object_key.startswith(f"{LEGACY_MANAGED_PREFIX}/")
            ):
                continue
            is_directory = raw.metadata.get("is_directory_marker") is True
            logical = str(raw.object_key)
            candidate = logical[:-1] if is_directory and logical.endswith("/") else logical
            invalid = (logical.startswith("/") or "\\" in logical or "\x00" in logical or not candidate
                       or any(not part or part in {".", ".."} or len(part) > 255 for part in candidate.split("/")))
            if is_directory or invalid:
                identity = ""
            elif hasattr(adapter, "listed_physical_id"):
                identity = await call_adapter(adapter, "listed_physical_id", policy, mount, raw)
            else:
                identity = await call_adapter(adapter, "physical_id", policy, mount, raw.object_key)
            items.append(
                BucketItem(
                    key=raw.object_key,
                    physical_key=physical_key,
                    physical_id=identity,
                    size=int(raw.size or 0),
                    etag=_optional_text(raw.etag, strip_quotes=True),
                    last_modified=_optional_text(raw.metadata.get("last_modified")),
                    storage_class=_optional_text(raw.metadata.get("storage_class")),
                    is_directory=is_directory,
                )
            )
        if page.is_truncated and (not page.next_marker or page.next_marker == cursor):
            raise RuntimeError("Storage drive returned a truncated page without a continuation marker")
        return BucketPage(
            items=tuple(items),
            next_cursor=page.next_marker,
            complete=not page.is_truncated,
        )

    async def exists(self, policy: StoragePolicy, mount: BucketMount, key: str) -> bool:
        adapter = self.storage_manager.get_adapter(policy)
        try:
            await call_adapter(adapter, "head_object", policy, mount, key)
        except Exception as exc:
            if is_not_found(exc):
                return False
            raise
        return True


def _optional_text(value: object, *, strip_quotes: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if strip_quotes:
        text = text.strip('"').strip()
    return text or None
