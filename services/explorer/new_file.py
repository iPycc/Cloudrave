"""Plan and write a new storage object without replacing an existing file."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException

from services.explorer.up_path import UploadPath
from store.call import call_adapter
from store.errors import storage_error_code
from store.key import format_object_key, managed_key


@dataclass(frozen=True)
class NewFilePath:
    key: str
    physical_id: str | None = None
    blob_id: str | None = None


async def new_file_path(nodes, objects, adapter, policy, mount, parent_id: int | None, filename: str) -> NewFilePath:
    extra = mount.extra or {}
    template = extra.get("object_key_template")
    if template is None:
        return NewFilePath(managed_key(mount.owner_id, filename, style=str(extra.get("object_key_style", "readable"))))
    if adapter.provider_type not in {"tencent_cos", "aliyun_oss"} or extra.get("legacy_prefixed_keys"):
        raise HTTPException(status_code=409, detail="当前存储配置不支持自定义对象路径")
    if not isinstance(template, str):
        raise HTTPException(status_code=422, detail="对象路径模板无效")

    paths = UploadPath(nodes)
    directory = await paths.relative(mount, await paths.parent(mount, parent_id))
    blob_id = uuid4().hex
    try:
        key = format_object_key(template, directory=directory or "", filename=filename, blob_id=blob_id)
        identity = await call_adapter(adapter, "physical_id", policy, mount, key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"对象路径无效：{exc}") from exc

    await objects.blobs.lock(identity)
    if await objects.has_physical_reference(identity) or await objects.has_migration_reservation(identity):
        raise HTTPException(status_code=409, detail="存储桶中的目标路径已被占用")
    return NewFilePath(key, identity, blob_id)


async def put_new_file(adapter, policy, mount, path: NewFilePath, body, **kwargs):
    if path.blob_id is not None:
        kwargs["no_overwrite"] = True
    try:
        return await call_adapter(adapter, "put_object", policy, mount, path.key, body, **kwargs)
    except Exception as exc:
        if path.blob_id is not None and (
            isinstance(exc, FileExistsError) or storage_error_code(exc) in {"FileAlreadyExists", "PreconditionFailed"}
        ):
            raise HTTPException(status_code=409, detail="存储桶中的目标路径已被占用") from exc
        raise
