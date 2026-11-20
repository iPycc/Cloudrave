"""Storage paths for newly created files and folders."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException

from drives.ids import provider_id
from services.explorer.up_path import UploadPath
from store.call import call_adapter
from store.errors import storage_error_code
from store.key import format_directory_key, format_object_key, managed_key


@dataclass(frozen=True)
class NewFilePath:
    key: str
    physical_id: str | None = None
    blob_id: str | None = None


def object_path_template(policy, mount) -> str | None:
    extra = mount.extra or {}
    template = extra.get("object_key_template")
    if template is None:
        return None
    if provider_id(policy.provider) not in {"tencent_cos", "aliyun_oss"} or extra.get("legacy_prefixed_keys"):
        raise HTTPException(status_code=409, detail="当前存储配置不支持自定义对象路径")
    if not isinstance(template, str):
        raise HTTPException(status_code=422, detail="对象路径模板无效")
    return template


async def new_file_path(nodes, objects, adapter, policy, mount, parent_id: int | None, filename: str) -> NewFilePath:
    template = object_path_template(policy, mount)
    if template is None:
        style = str((mount.extra or {}).get("object_key_style", "readable"))
        return NewFilePath(managed_key(mount.owner_id, filename, style=style))

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
        if path.blob_id is not None and _already_exists(exc):
            raise HTTPException(status_code=409, detail="存储桶中的目标路径已被占用") from exc
        raise


async def put_new_folder(nodes, storage_manager, policy, mount, folder) -> None:
    template = object_path_template(policy, mount)
    if template is None:
        return
    paths = UploadPath(nodes)
    parent = await paths.parent(mount, folder.parent_id)
    directory = await paths.relative(mount, parent)
    folder_path = f"{directory}/{folder.name}" if directory else folder.name
    adapter = storage_manager.get_adapter(policy)
    try:
        key = format_directory_key(template, folder_path)
        await call_adapter(adapter, "put_directory_marker", policy, mount, key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"无法在存储桶创建目录：{exc}") from exc
    except Exception as exc:
        if _already_exists(exc):
            raise HTTPException(status_code=409, detail="存储桶中的目标目录已存在") from exc
        raise
    folder.blob_path = key


def _already_exists(exc: Exception) -> bool:
    return isinstance(exc, FileExistsError) or storage_error_code(exc) in {"FileAlreadyExists", "PreconditionFailed"}
