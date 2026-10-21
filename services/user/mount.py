"""Owner-scoped storage mount management."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import NodeType, UploadState
from models.gc import GcTask
from models.tables import BucketMount, Node, UploadSessionRecord, User
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.policies import StoragePolicyRepository
from repositories.uploads import UploadRepository
from schemas.mount import BucketMountCreate, BucketMountRead, BucketMountUpdate, mount_read
from schemas.policy import StoragePolicyCreate, StoragePolicyRead, StoragePolicyUpdate
from schemas.user_mount import (
    UserBucketMountCreate,
    UserMountCreate,
    UserMountDeletePreview,
    UserMountDeleteResult,
    UserMountRead,
    UserMountUpdate,
)
from services.explorer.upload_cleanup import UploadCleanup
from store.call import call_adapter
from store.manager import StorageManager
from drives.ids import provider_id


LOGGER = logging.getLogger("cloudrave.store")
OWNER_KEY = "owner_user_id"
USER_MANAGED_KEY = "user_managed"


class UserMountService:
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

    async def list_mounts(self, actor: User) -> list[BucketMountRead]:
        mounts = await self.mounts.list_for_owner(actor.id)
        policies = {
            policy.id: policy
            for policy in await self.policies.list_by_ids([mount.policy_id for mount in mounts])
        }
        return [mount_read(mount, policies.get(mount.policy_id), self.storage_manager) for mount in mounts]

    async def get_mount(self, actor: User, mount_id: int) -> UserMountRead:
        mount = await self._owned_mount(actor, mount_id)
        policy = await self._policy(mount.policy_id)
        return self._read(mount, policy)

    async def create_mount(self, request: Request, actor: User, payload: UserMountCreate) -> UserMountRead:
        policy_payload = self._owned_policy_create(actor, payload.policy)
        mount_payload = self._mount_create(actor, payload.mount, policy_id=0, provider=policy_payload.provider)

        try:
            self.storage_manager.definition_for(policy_payload.provider)
            ciphertext, version = self.storage_manager.credentials_from_payload(
                policy_payload.provider,
                policy_payload,
            )
            policy = await self.policies.create(policy_payload)
            policy.credential_ciphertext = ciphertext
            policy.credential_version = version
            mount_payload.policy_id = policy.id
            mount = await self.mounts.create(actor.id, mount_payload)
            await self.nodes.ensure_mount_root(owner_id=actor.id, mount_id=mount.id, name=mount.name)
            await self._validate_storage(policy, mount)
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A mount with the same name or slug already exists",
            ) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

        await self._audit(request, actor, "mount.create", mount, provider_id(policy.provider))
        LOGGER.info("Created user mount %s for %s", mount.name, actor.uid)
        return self._read(mount, policy)

    async def update_mount(
        self,
        request: Request,
        actor: User,
        mount_id: int,
        payload: UserMountUpdate,
    ) -> UserMountRead:
        mount = await self._owned_mount(actor, mount_id, for_update=True)
        policy = await self._policy(mount.policy_id)
        policy_patch = payload.policy
        mount_patch = payload.mount

        if policy_patch is not None:
            self._require_owned_policy(actor, policy)
            policy_patch = self._owned_policy_update(actor, policy, mount, policy_patch)
        if mount_patch is not None:
            mount_patch = self._owned_mount_update(actor, policy, mount, mount_patch)

        mapping_changed = self._mapping_changed(policy, mount, policy_patch, mount_patch)
        connection_changed = self._connection_changed(policy, policy_patch)
        if mapping_changed and await self._mount_is_busy(mount.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Mount contains data, active uploads, or pending cleanup; cannot change {', '.join(mapping_changed)}",
            )

        try:
            if policy_patch is not None:
                ciphertext, version = self.storage_manager.credentials_from_payload(
                    policy.provider,
                    policy_patch,
                    current=policy,
                )
                policy = await self.policies.update(policy, policy_patch)
                policy.credential_ciphertext = ciphertext
                policy.credential_version = version
            if mount_patch is not None:
                mount = await self.mounts.update(mount, mount_patch)
            if mapping_changed or connection_changed:
                await self._validate_storage(policy, mount)
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A mount or policy with the same name already exists",
            ) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

        await self._audit(request, actor, "mount.update", mount, ",".join(mapping_changed) or "settings")
        return self._read(mount, policy)

    async def delete_preview(self, actor: User, mount_id: int) -> UserMountDeletePreview:
        mount = await self._owned_mount(actor, mount_id)
        policy = await self._policy(mount.policy_id)
        file_count, folder_count = await self._node_counts(mount.id)
        active_upload_count = await self._active_upload_count(mount.id)
        pending_cleanup_count = await self._pending_cleanup_count(mount.id)
        remote_objects = await self._remote_has_objects(policy, mount)
        prefix = "/".join(
            part.strip("/\\")
            for part in (policy.base_prefix, mount.root_path)
            if part and part.strip("/\\")
        )
        return UserMountDeletePreview(
            mount_id=mount.id,
            mount_name=mount.name,
            provider=provider_id(policy.provider),
            bucket_name=policy.bucket_name,
            prefix=prefix,
            file_count=file_count,
            folder_count=folder_count,
            active_upload_count=active_upload_count,
            pending_cleanup_count=pending_cleanup_count,
            remote_objects=remote_objects,
            has_contents=bool(
                file_count
                or folder_count
                or active_upload_count
                or pending_cleanup_count
                or remote_objects
            ),
        )

    async def delete_mount(
        self,
        request: Request,
        actor: User,
        mount_id: int,
        *,
        delete_objects: bool = False,
    ) -> UserMountDeleteResult:
        mount = await self._owned_mount(actor, mount_id, for_update=True)
        policy = await self._policy(mount.policy_id)
        busy = await self._mount_is_busy(mount.id)
        deleted_objects = 0

        if delete_objects:
            adapter = self.storage_manager.get_adapter(policy)
            if not hasattr(adapter, "purge_mount") and not hasattr(adapter, "apurge_mount"):
                if busy:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="This storage adapter cannot purge a non-empty mount",
                    )
            else:
                try:
                    await UploadCleanup(
                        uploads=self.uploads,
                        mounts=self.mounts,
                        policies=self.policies,
                        storage_manager=self.storage_manager,
                    ).cleanup_mount(mount.id)
                    deleted_objects = int(await call_adapter(adapter, "purge_mount", policy, mount))
                except Exception as exc:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Failed to delete storage objects: {exc}",
                    ) from exc
        owned_policy = self._is_owned_policy(actor, policy)
        await self.mounts.delete(mount)
        policy_deleted = False
        if owned_policy and not await self.policies.has_mounts(policy.id):
            await self.policies.delete(policy)
            policy_deleted = True

        await self._audit(
            request,
            actor,
            "mount.delete",
            mount,
            f"delete_objects={str(delete_objects).lower()}; deleted={deleted_objects}",
        )
        return UserMountDeleteResult(
            message="Bucket mount deleted",
            deleted_objects=deleted_objects,
            policy_deleted=policy_deleted,
        )

    async def configure_cors(self, request: Request, actor: User, mount_id: int) -> dict[str, object]:
        mount = await self._owned_mount(actor, mount_id)
        policy = await self._policy(mount.policy_id)
        self._require_owned_policy(actor, policy)

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
        await self._audit(request, actor, "mount.cors", mount, str(result.get("status", "configured")))
        return result

    async def require_owned_mount(self, actor: User, mount_id: int) -> BucketMount:
        return await self._owned_mount(actor, mount_id)

    async def _owned_mount(self, actor: User, mount_id: int, *, for_update: bool = False) -> BucketMount:
        mount = (
            await self.mounts.get_by_id_for_update(mount_id)
            if for_update
            else await self.mounts.get_by_id(mount_id)
        )
        if mount is None or mount.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        return mount

    async def _policy(self, policy_id: int):
        policy = await self.policies.get_by_id(policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return policy

    def _owned_policy_create(self, actor: User, payload: StoragePolicyCreate) -> StoragePolicyCreate:
        extra = {
            **payload.extra,
            OWNER_KEY: actor.id,
            USER_MANAGED_KEY: True,
        }
        name = f"user-{actor.uid}-{uuid4().hex[:12]}-{payload.name}"[:128]
        return payload.model_copy(update={"name": name, "is_default": False, "extra": extra})

    def _mount_create(
        self,
        actor: User,
        payload: UserBucketMountCreate,
        *,
        policy_id: int,
        provider: str,
    ) -> BucketMountCreate:
        extra = {
            **payload.extra,
            OWNER_KEY: actor.id,
            USER_MANAGED_KEY: True,
        }
        self._validate_user_mount(actor, provider, payload.mount_slug, extra)
        return BucketMountCreate(policy_id=policy_id, **payload.model_dump(exclude={"extra"}), extra=extra)

    def _owned_policy_update(
        self,
        actor: User,
        policy,
        mount: BucketMount,
        payload: StoragePolicyUpdate,
    ) -> StoragePolicyUpdate:
        values = payload.model_dump(exclude_unset=True)
        # Pydantic intentionally excludes credentials from serialization so
        # they can never leak into API responses or logs. Preserve explicitly
        # submitted write-only fields while constructing the owner-scoped
        # patch; an omitted credentials object still means "keep existing".
        for field in ("credentials", "secret_id", "secret_key", "session_token"):
            if field in payload.model_fields_set:
                values[field] = getattr(payload, field)
        values.pop("name", None)
        if "is_default" in values:
            values["is_default"] = False
        if "extra" in values:
            values["extra"] = {
                **(policy.extra or {}),
                **(values["extra"] or {}),
                OWNER_KEY: actor.id,
                USER_MANAGED_KEY: True,
            }
            self._validate_local_root(actor, policy.provider, mount.mount_slug, values["extra"])
        return StoragePolicyUpdate.model_validate(values)

    def _owned_mount_update(
        self,
        actor: User,
        policy,
        mount: BucketMount,
        payload: BucketMountUpdate,
    ) -> BucketMountUpdate:
        values = payload.model_dump(exclude_unset=True)
        if "extra" in values:
            values["extra"] = {
                **(mount.extra or {}),
                **(values["extra"] or {}),
                OWNER_KEY: actor.id,
                USER_MANAGED_KEY: True,
            }
            slug = str(values.get("mount_slug") or mount.mount_slug)
            self._validate_user_mount(actor, policy.provider, slug, values["extra"])
        return BucketMountUpdate.model_validate(values)

    def _validate_user_mount(
        self,
        actor: User,
        provider: str,
        mount_slug: str,
        extra: dict,
    ) -> None:
        try:
            self.storage_manager.validate_user_mount(provider, actor, mount_slug, extra)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    def _require_owned_policy(self, actor: User, policy) -> None:
        if not self._is_owned_policy(actor, policy):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This mount's storage connection is managed by an administrator",
            )

    @staticmethod
    def _is_owned_policy(actor: User, policy) -> bool:
        extra = policy.extra or {}
        try:
            owner_id = int(extra.get(OWNER_KEY))
        except (TypeError, ValueError):
            return False
        return owner_id == actor.id and extra.get(USER_MANAGED_KEY) is True

    async def _validate_storage(self, policy, mount: BucketMount) -> None:
        adapter = self.storage_manager.get_adapter(policy)
        definition = self.storage_manager.definition_for(policy.provider)
        if definition.kind == "local":
            await call_adapter(adapter, "prepare_mount_root", mount)
            return
        try:
            await call_adapter(adapter, "validate_mount", policy, mount)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Storage drive validation failed: {exc}",
            ) from exc

    async def _mount_is_busy(self, mount_id: int) -> bool:
        return bool(
            await self.nodes.has_mount_data(mount_id)
            or await self.mounts.has_pending_gc(mount_id)
            or await self.mounts.has_active_uploads(mount_id)
        )

    @staticmethod
    def _mapping_changed(policy, mount: BucketMount, policy_patch, mount_patch) -> list[str]:
        changed: list[str] = []
        if policy_patch is not None:
            for field in {"bucket_name", "region", "endpoint", "base_prefix"} & policy_patch.model_fields_set:
                if getattr(policy_patch, field) != getattr(policy, field):
                    changed.append(field)
        if mount_patch is not None:
            for field in {"root_path", "mount_slug", "mode", "legacy_prefixed_keys"} & mount_patch.model_fields_set:
                if getattr(mount_patch, field) != getattr(mount, field):
                    changed.append(field)
            if "extra" in mount_patch.model_fields_set:
                old_root = str((mount.extra or {}).get("storage_root") or "").strip()
                new_root = str((mount_patch.extra or {}).get("storage_root") or "").strip()
                if new_root != old_root:
                    changed.append("storage_root")
        return sorted(set(changed))

    @staticmethod
    def _connection_changed(policy, policy_patch) -> bool:
        if policy_patch is None:
            return False
        return bool(
            {"credentials", "secret_id", "secret_key", "session_token"}
            & policy_patch.model_fields_set
        )

    async def _node_counts(self, mount_id: int) -> tuple[int, int]:
        rows = await self.session.execute(
            select(Node.type, func.count(Node.id))
            .where(Node.mount_id == mount_id, Node.parent_id.is_not(None))
            .group_by(Node.type)
        )
        counts = {node_type: int(count) for node_type, count in rows.all()}
        return counts.get(NodeType.FILE, 0), counts.get(NodeType.FOLDER, 0)

    async def _active_upload_count(self, mount_id: int) -> int:
        terminal = (UploadState.COMPLETED, UploadState.ABORTED, UploadState.EXPIRED)
        count = await self.session.scalar(
            select(func.count(UploadSessionRecord.id)).where(
                UploadSessionRecord.mount_id == mount_id,
                UploadSessionRecord.state.not_in(terminal),
            )
        )
        return int(count or 0)

    async def _pending_cleanup_count(self, mount_id: int) -> int:
        count = await self.session.scalar(
            select(func.count(GcTask.id)).where(
                GcTask.mount_id == mount_id,
                GcTask.state.in_(("pending", "processing")),
            )
        )
        return int(count or 0)

    async def _remote_has_objects(self, policy, mount: BucketMount) -> bool | None:
        adapter = self.storage_manager.get_adapter(policy)
        if not hasattr(adapter, "mount_has_objects") and not hasattr(adapter, "amount_has_objects"):
            return None
        try:
            return bool(await call_adapter(adapter, "mount_has_objects", policy, mount))
        except Exception as exc:
            LOGGER.warning("Unable to inspect user mount %s before deletion: %s", mount.id, exc)
            return None

    async def _audit(self, request: Request, actor: User, action: str, mount: BucketMount, detail: str) -> None:
        await self.audits.create(
            actor_id=actor.id,
            action=action,
            target=f"mount:{mount.id}",
            detail=detail,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )

    def _read(self, mount: BucketMount, policy) -> UserMountRead:
        return UserMountRead(
            policy=self._policy_read(policy),
            mount=mount_read(mount, policy, self.storage_manager),
        )

    def _policy_read(self, policy) -> StoragePolicyRead:
        configured, fields = self.storage_manager.credential_state(policy)
        return StoragePolicyRead.model_validate({
            **StoragePolicyRead.model_validate(policy).model_dump(),
            "credentials_configured": configured,
            "credential_fields": fields,
        })
