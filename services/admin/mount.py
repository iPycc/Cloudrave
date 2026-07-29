"""Bucket mount admin service."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import StoragePolicyType
from models.tables import User
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.policies import StoragePolicyRepository
from repositories.uploads import UploadRepository
from schemas.mount import (
    BucketMountCreate,
    BucketMountDeletePreview,
    BucketMountDeleteResult,
    BucketMountRead,
    BucketMountUpdate,
    mount_read,
)
from services.explorer.upload_cleanup import UploadCleanup
from store.manager import StorageManager
from store.call import call_adapter
from core.log import LogOperation, LogOutcome


LOGGER = logging.getLogger("cloudrave.store")


class MountAdminService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        mounts: BucketMountRepository,
        policies: StoragePolicyRepository,
        nodes: NodeRepository,
        uploads: UploadRepository,
        audits: AuditLogRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.session = session
        self.mounts = mounts
        self.policies = policies
        self.nodes = nodes
        self.uploads = uploads
        self.audits = audits
        self.storage_manager = storage_manager

    async def list_mounts(self) -> list[BucketMountRead]:
        mounts = await self.mounts.list_all()
        policies = {
            policy.id: policy
            for policy in await self.policies.list_by_ids([mount.policy_id for mount in mounts])
        }
        return [mount_read(mount, policies.get(mount.policy_id)) for mount in mounts]

    async def get_mount(self, mount_id: int) -> BucketMountRead:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        return mount_read(mount, await self.policies.get_by_id(mount.policy_id))

    async def create_mount(self, request: Request, actor: User, payload: BucketMountCreate) -> BucketMountRead:
        owner_id = payload.owner_id or actor.id
        policy = await self.policies.get_by_id(payload.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")

        mount = await self.mounts.create(owner_id, payload)
        await self.nodes.ensure_mount_root(owner_id=owner_id, mount_id=mount.id, name=mount.name)
        if policy.provider == StoragePolicyType.LOCAL_FS:
            await call_adapter(self.storage_manager.get_adapter(policy), "prepare_mount_root", mount)
        else:
            try:
                await call_adapter(self.storage_manager.get_adapter(policy), "validate_mount", policy, mount)
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"COS mount validation failed: {exc}",
                ) from exc

        await self.audits.create(
            actor_id=actor.id,
            action="mount.create",
            target=f"mount:{mount.id}",
            detail=mount.mount_slug,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Created mount {mount.name}",
            extra={
                "operation": LogOperation.CREATE,
                "resource": "mount",
                "outcome": LogOutcome.SUCCESS,
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )
        return mount_read(mount, policy)

    async def update_mount(
        self,
        request: Request,
        actor: User,
        mount_id: int,
        payload: BucketMountUpdate,
    ) -> BucketMountRead:
        mount = await self.mounts.get_by_id_for_update(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        changed_mapping = []
        if "root_path" in payload.model_fields_set and payload.root_path != mount.root_path:
            changed_mapping.append("root_path")
        if "mode" in payload.model_fields_set and payload.mode != mount.mode:
            changed_mapping.append("mode")
        if "mount_slug" in payload.model_fields_set and payload.mount_slug != mount.mount_slug:
            changed_mapping.append("mount_slug")
        if "extra" in payload.model_fields_set:
            old_root = str((mount.extra or {}).get("storage_root") or "").strip()
            new_root = str((payload.extra or {}).get("storage_root") or "").strip()
            if new_root != old_root:
                changed_mapping.append("storage_root")
        current_legacy = bool((mount.extra or {}).get("legacy_prefixed_keys", False))
        legacy_changed = (
            "legacy_prefixed_keys" in payload.model_fields_set
            and payload.legacy_prefixed_keys is not None
            and payload.legacy_prefixed_keys != current_legacy
        )
        if (
            legacy_changed
            and current_legacy
            and payload.legacy_prefixed_keys is False
        ):
            changed_mapping.append("legacy_prefixed_keys")
        active_uploads = (
            await self.mounts.has_active_uploads(mount.id)
            if changed_mapping or legacy_changed
            else False
        )
        mapping_busy = changed_mapping and (
            await self.nodes.has_mount_data(mount.id)
            or await self.mounts.has_pending_gc(mount.id)
            or active_uploads
        )
        if legacy_changed and active_uploads:
            mapping_busy = True
            if "legacy_prefixed_keys" not in changed_mapping:
                changed_mapping.append("legacy_prefixed_keys")
        if mapping_busy:
            fields = ", ".join(changed_mapping)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Mount contains data, active uploads, or pending cleanup; cannot change {fields}",
            )
        updated = await self.mounts.update(mount, payload)
        await self.audits.create(
            actor_id=actor.id,
            action="mount.update",
            target=f"mount:{updated.id}",
            detail=updated.mount_slug,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return mount_read(updated, await self.policies.get_by_id(updated.policy_id))

    async def delete_preview(self, mount_id: int) -> BucketMountDeletePreview:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")

        file_count, folder_count = await self.nodes.mount_content_counts(mount.id)
        active_upload_count = await self.mounts.active_upload_count(mount.id)
        pending_cleanup_count = await self.mounts.pending_gc_count(mount.id)
        remote_objects: bool | None = None
        try:
            remote_objects = bool(
                await call_adapter(
                    self.storage_manager.get_adapter(policy),
                    "mount_has_objects",
                    policy,
                    mount,
                )
            )
        except Exception as exc:
            LOGGER.warning("Unable to inspect mount %s before deletion: %s", mount.id, exc)

        prefix = "/".join(
            part.strip("/\\")
            for part in (policy.base_prefix, mount.root_path)
            if part and part.strip("/\\")
        )
        has_contents = bool(
            file_count
            or folder_count
            or active_upload_count
            or pending_cleanup_count
            or remote_objects
        )
        return BucketMountDeletePreview(
            mount_id=mount.id,
            mount_name=mount.name,
            provider=policy.provider.value,
            bucket_name=policy.bucket_name,
            prefix=prefix,
            file_count=file_count,
            folder_count=folder_count,
            active_upload_count=active_upload_count,
            pending_cleanup_count=pending_cleanup_count,
            remote_objects=remote_objects,
            has_contents=has_contents,
        )

    async def delete_mount(
        self,
        request: Request,
        actor: User,
        mount_id: int,
        *,
        delete_objects: bool = False,
    ) -> BucketMountDeleteResult:
        mount = await self.mounts.get_by_id_for_update(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        deleted_objects = 0
        if delete_objects:
            policy = await self.policies.get_by_id(mount.policy_id)
            if policy is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
            try:
                await UploadCleanup(
                    uploads=self.uploads,
                    mounts=self.mounts,
                    policies=self.policies,
                    storage_manager=self.storage_manager,
                ).cleanup_mount(mount.id)
                deleted_objects = int(
                    await call_adapter(
                        self.storage_manager.get_adapter(policy),
                        "purge_mount",
                        policy,
                        mount,
                    )
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Failed to delete storage objects: {exc}",
                ) from exc
        await self.mounts.delete(mount)
        await self.audits.create(
            actor_id=actor.id,
            action="mount.delete",
            target=f"mount:{mount_id}",
            detail=f"{mount.mount_slug}; delete_objects={str(delete_objects).lower()}; deleted={deleted_objects}",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Deleted mount {mount.name}",
            extra={
                "operation": LogOperation.DELETE,
                "resource": "mount",
                "outcome": LogOutcome.SUCCESS,
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )
        return BucketMountDeleteResult(
            message="Bucket mount and storage objects deleted" if delete_objects else "Bucket mount detached",
            deleted_objects=deleted_objects,
        )

    async def configure_cors(self, request: Request, actor: User, mount_id: int) -> dict[str, object]:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")

        extra = dict(policy.extra or {})
        if not extra.get("cors_origins"):
            origin = (request.headers.get("origin") or "").strip().rstrip("/")
            parsed = urlsplit(origin)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                extra["cors_origins"] = [origin]
                policy.extra = extra
        try:
            result = await call_adapter(self.storage_manager.get_adapter(policy), "configure_cors", policy, mount)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        policy.cors_auto_configured = True
        await self.session.flush()
        await self.audits.create(
            actor_id=actor.id,
            action="mount.cors",
            target=f"mount:{mount.id}",
            detail=str(result.get("status", "configured")),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return result
