"""Upload services."""

from __future__ import annotations

import asyncio
import hashlib

from fastapi import HTTPException, Request, UploadFile, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import StoragePolicyType, UploadMode, UploadState
from models.tables import BucketMount, StoragePolicy, User
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from repositories.policies import StoragePolicyRepository
from repositories.uploads import UploadRepository
from schemas.upload import (
    UploadPartPlan,
    UploadPartRead,
    UploadSessionAbortRequest,
    UploadSessionCompleteRequest,
    UploadSessionCreate,
    UploadSessionPlan,
    UploadSessionRead,
)
from services.explorer.up_check import (
    MIB,
    build_upload_state,
    expected_part_size,
    part_count,
    validate_complete_parts,
    validate_part_number,
)
from services.explorer.up_abort import cleanup_upload
from services.explorer.up_path import UploadPath
from services.explorer.up_plan import make_plan, sign_part, sign_put
from store.manager import StorageManager
from store.call import call_adapter
from store.errors import is_not_found
from store.key import managed_key
from store.types import MultipartPart


class CreateUploadSessionService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        mounts: BucketMountRepository,
        policies: StoragePolicyRepository,
        nodes: NodeRepository,
        objects: ObjectRepository,
        uploads: UploadRepository,
        audits: AuditLogRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.session = session
        self.mounts = mounts
        self.policies = policies
        self.nodes = nodes
        self.objects = objects
        self.uploads = uploads
        self.audits = audits
        self.storage_manager = storage_manager
        self.paths = UploadPath(nodes)

    async def create(self, request: Request, actor: User, payload: UploadSessionCreate) -> UploadSessionPlan:
        mount, policy = await self._require_mount_write(actor, payload.mount_id)
        adapter = self.storage_manager.get_adapter(policy)
        parent, file_name = await self.paths.target(
            mount,
            payload.parent_id,
            payload.file_name,
            payload.relative_path,
        )
        if await self.nodes.get_child_any(mount.id, parent.id, file_name) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="目标位置已存在同名文件或文件夹")

        if payload.checksum:
            existing_object = await self.objects.get_by_checksum(
                payload.checksum,
                policy_id=policy.id,
                mount_id=mount.id,
            )
            existing_node = await self.nodes.get_by_id(existing_object.node_id) if existing_object else None
            if existing_object is not None and existing_node is not None and existing_node.size == payload.size:
                file_node = await self.nodes.create_file(
                    owner_id=mount.owner_id,
                    mount_id=mount.id,
                    parent_id=parent.id,
                    name=file_name,
                    blob_path=existing_object.blob_path,
                    size=payload.size,
                )
                await self.objects.create_or_update(
                    node_id=file_node.id,
                    policy_id=existing_object.policy_id,
                    blob_path=existing_object.blob_path,
                    physical_id=(
                        getattr(existing_object, "physical_id", None)
                        or (
                            adapter.physical_id(policy, mount, existing_object.blob_path)
                            if hasattr(adapter, "physical_id")
                            else None
                        )
                    ),
                    checksum=payload.checksum,
                    content_type=payload.content_type,
                )
                session_record = await self.uploads.create_session(
                    owner_id=mount.owner_id,
                    mount_id=mount.id,
                    node_id=parent.id if parent else None,
                    path=existing_object.blob_path,
                    mode=UploadMode.SINGLE,
                    upload_id=None,
                    file_name=file_name,
                    content_type=payload.content_type,
                    size=payload.size,
                    part_size=payload.size or 1,
                    checksum=payload.checksum,
                    state=UploadState.COMPLETED,
                )
                await self.uploads.bind_node(session_record.id, file_node.id)
                await self.audits.create(
                    actor_id=actor.id,
                    action="upload.dedup",
                    target=f"node:{file_node.id}",
                    detail=file_node.name,
                    ip=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    request_id=getattr(request.state, "request_id", None),
                )
                return UploadSessionPlan(
                    session_id=session_record.id,
                    upload_mode=UploadMode.SINGLE,
                    object_key=existing_object.blob_path,
                    part_size=payload.size or 1,
                    part_count=1,
                    upload_urls=[],
                    is_duplicate=True,
                    node_id=file_node.id,
                )

        if payload.object_key is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Custom object_key is not accepted for managed uploads",
            )
        object_key = managed_key(
            actor.id,
            file_name,
            style=str((mount.extra or {}).get("object_key_style", "readable")),
        )
        session_record, plan = await make_plan(
            uploads=self.uploads,
            storage_manager=self.storage_manager,
            policy=policy,
            mount=mount,
            parent=parent,
            file_name=file_name,
            object_key=object_key,
            payload=payload,
        )
        await self.audits.create(
            actor_id=actor.id,
            action="upload.create",
            target=f"upload:{session_record.id}",
            detail=object_key,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return plan

    async def get(self, actor: User, session_id: str) -> UploadSessionRead:
        record = await self._require_upload_access(actor, session_id)
        return await self._to_read(record)

    async def part_url(self, actor: User, session_id: str, part_number: int) -> UploadPartPlan:
        record = await self._require_upload_access(actor, session_id)
        if record.state not in (UploadState.CREATED, UploadState.UPLOADING):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload session cannot issue part URLs")
        self._validate_part_number(record, part_number)
        mount, policy = await self._require_mount_write(actor, record.mount_id)
        adapter = self.storage_manager.get_adapter(policy)
        if record.mode == UploadMode.SINGLE:
            return await sign_put(
                adapter,
                policy,
                mount,
                record.path,
                record.content_type,
                getattr(record, "checksum", None),
            )
        if record.mode != UploadMode.MULTIPART:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload session cannot issue part URLs")
        if policy.provider == StoragePolicyType.LOCAL_FS:
            return UploadPartPlan(
                part_number=part_number,
                method="POST",
                url=f"/api/v1/explorer/upload/{record.id}/part",
            )
        return await sign_part(
            adapter,
            policy,
            mount,
            self._build_upload_state(record, policy, mount),
            part_number,
        )

    async def record_part(
        self,
        actor: User,
        session_id: str,
        part_number: int,
        file: UploadFile | None,
        etag: str | None,
        size: int,
    ) -> UploadPartRead:
        record = await self._require_upload_access(actor, session_id)
        if record.state not in (UploadState.CREATED, UploadState.UPLOADING):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload session cannot accept parts")
        expected_size = self._expected_part_size(record, part_number)
        mount, policy = await self._require_mount_write(actor, record.mount_id)
        adapter = self.storage_manager.get_adapter(policy)

        resolved_etag = etag
        resolved_size = size
        if policy.provider == StoragePolicyType.LOCAL_FS:
            if file is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Local upload requires file content")
            await file.seek(0)
            part = await call_adapter(
                adapter,
                "upload_part",
                policy,
                mount,
                self._build_upload_state(record, policy, mount),
                part_number,
                file.file,
            )
            resolved_etag = part.etag
            resolved_size = part.size or 0
        elif not resolved_etag:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Remote upload part requires an ETag")

        if resolved_size != expected_size:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Upload part has invalid size; expected {expected_size} bytes",
            )

        accepted = await self.uploads.transition_state(
            record.id,
            expected=(UploadState.CREATED, UploadState.UPLOADING),
            target=UploadState.UPLOADING,
        )
        if not accepted:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload session no longer accepts parts")
        part_record = await self.uploads.upsert_part(
            upload_session_id=record.id,
            part_number=part_number,
            etag=resolved_etag,
            size=resolved_size,
        )
        return UploadPartRead.model_validate(part_record)

    async def complete(
        self,
        request: Request,
        actor: User,
        session_id: str,
        payload: UploadSessionCompleteRequest,
    ) -> UploadSessionRead:
        record = await self._require_upload_access(actor, session_id)
        if record.state == UploadState.COMPLETED:
            return await self._to_read(record)
        claimed = await self.uploads.transition_state(
            record.id,
            expected=(UploadState.CREATED, UploadState.UPLOADING, UploadState.FAILED),
            target=UploadState.COMPLETING,
        )
        if not claimed:
            await self.session.refresh(record)
            if record.state == UploadState.COMPLETED:
                return await self._to_read(record)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload session is not in a completable state")

        try:
            mount, policy = await self._require_mount_write(actor, record.mount_id)
            adapter = self.storage_manager.get_adapter(policy)
            declared_checksum = getattr(record, "checksum", None)

            for part in payload.parts:
                expected_size = self._expected_part_size(record, part.part_number)
                if part.size != expected_size:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=f"Upload part {part.part_number} has invalid size",
                    )
                await self.uploads.upsert_part(
                    upload_session_id=record.id,
                    part_number=part.part_number,
                    etag=part.etag,
                    size=part.size,
                )

            stored_object = None
            if record.state == UploadState.FAILED or record.mode == UploadMode.SINGLE:
                try:
                    stored_object = await call_adapter(
                        adapter,
                        "head_object",
                        policy,
                        mount,
                        record.path,
                        **({"verify_encryption": True} if policy.provider == StoragePolicyType.TENCENT_COS else {}),
                    )
                except Exception as exc:
                    if not is_not_found(exc):
                        raise
                    stored_object = None
                if stored_object is not None and stored_object.size != record.size:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Uploaded object size does not match session")

            if stored_object is None and record.mode == UploadMode.MULTIPART:
                parts = await self.uploads.list_parts(record.id)
                self._validate_complete_parts(record, parts)
                upload_state = self._build_upload_state(record, policy, mount)
                if policy.provider == StoragePolicyType.LOCAL_FS:
                    storage_parts = await call_adapter(adapter, "list_parts", policy, mount, upload_state)
                    self._validate_complete_parts(record, storage_parts)
                    stored_object = await call_adapter(
                        adapter,
                        "complete_multipart_upload",
                        policy,
                        mount,
                        upload_state,
                        storage_parts,
                        mime_type=record.content_type,
                    )
                else:
                    stored_object = await call_adapter(
                        adapter,
                        "complete_multipart_upload",
                        policy,
                        mount,
                        upload_state,
                        [MultipartPart(part_number=part.part_number, etag=part.etag or "", size=part.size) for part in parts],
                        mime_type=record.content_type,
                    )
            elif stored_object is None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Uploaded object was not found")

            if declared_checksum and policy.provider == StoragePolicyType.TENCENT_COS:
                metadata = getattr(stored_object, "metadata", {}) or {}
                if metadata.get("sha256") != declared_checksum:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Uploaded object checksum metadata does not match the session",
                    )

            try:
                file_node = await self.nodes.create_file(
                    owner_id=mount.owner_id,
                    mount_id=mount.id,
                    parent_id=record.node_id,
                    name=record.file_name,
                    blob_path=record.path,
                    size=record.size,
                )
            except IntegrityError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="目标位置已存在同名文件") from exc

            checksum = None
            if policy.provider == StoragePolicyType.LOCAL_FS:
                checksum = await asyncio.to_thread(self._compute_sha256_from_adapter, adapter, policy, mount, record.path)
                if declared_checksum and checksum != declared_checksum:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Uploaded object checksum mismatch")
            else:
                checksum = declared_checksum

            await self.objects.create_or_update(
                node_id=file_node.id,
                policy_id=policy.id,
                blob_path=record.path,
                physical_id=adapter.physical_id(policy, mount, record.path),
                revive_physical=True,
                checksum=checksum,
                content_type=record.content_type,
                extra={
                    "etag": getattr(stored_object, "etag", None),
                    "version_id": getattr(stored_object, "version_id", None),
                    "origin": "managed_upload",
                },
            )
            await self.uploads.bind_node(record.id, file_node.id)
            await self.uploads.set_state(record.id, UploadState.COMPLETED)
            await self.audits.create(
                actor_id=actor.id,
                action="upload.complete",
                target=f"node:{file_node.id}",
                detail=file_node.name,
                ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                request_id=getattr(request.state, "request_id", None),
            )
            return await self._to_read(record)
        except Exception:
            await self.session.rollback()
            failed = await self.uploads.get_session(session_id)
            if failed is not None and failed.state not in (UploadState.COMPLETED, UploadState.ABORTED, UploadState.EXPIRED):
                await self.uploads.set_state(session_id, UploadState.FAILED)
                await self.session.commit()
            raise

    async def abort(
        self,
        request: Request,
        actor: User,
        session_id: str,
        payload: UploadSessionAbortRequest | None = None,
    ) -> UploadSessionRead:
        _ = payload
        record = await self._require_upload_access(actor, session_id)
        if record.state == UploadState.ABORTED:
            return await self._to_read(record)
        claimed = record.state == UploadState.ABORTING or await self.uploads.transition_state(
            record.id,
            expected=(UploadState.CREATED, UploadState.UPLOADING, UploadState.FAILED),
            target=UploadState.ABORTING,
        )
        if not claimed:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload session cannot be aborted")
        mount, policy = await self._require_mount_cleanup(actor, record.mount_id)
        adapter = self.storage_manager.get_adapter(policy)
        await cleanup_upload(
            adapter,
            policy,
            mount,
            self._build_upload_state(record, policy, mount),
            record.mode,
        )
        await self.uploads.set_state(record.id, UploadState.ABORTED)
        await self.audits.create(
            actor_id=actor.id,
            action="upload.abort",
            target=f"upload:{record.id}",
            detail=record.path,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return await self._to_read(record)

    async def _require_mount_access(self, actor: User, mount_id: int) -> tuple[BucketMount, StoragePolicy]:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        if mount.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Mount access denied")
        if not mount.is_enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bucket mount is disabled")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None or policy.status != "active":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return mount, policy

    async def _require_mount_write(self, actor: User, mount_id: int) -> tuple[BucketMount, StoragePolicy]:
        mount, policy = await self._require_mount_access(actor, mount_id)
        if mount.read_only:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bucket mount is read-only")
        return mount, policy

    async def _require_mount_cleanup(self, actor: User, mount_id: int) -> tuple[BucketMount, StoragePolicy]:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        if mount.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Mount access denied")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return mount, policy

    async def _require_upload_access(self, actor: User, session_id: str):
        record = await self.uploads.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
        if record.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload session access denied")
        return record

    def _compute_sha256_from_adapter(
        self,
        adapter,
        policy,
        mount,
        object_key: str,
    ) -> str:
        hasher = hashlib.sha256()
        with adapter.open_object(policy, mount, object_key) as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _part_count(record) -> int:
        return part_count(record.size, record.part_size)

    def _validate_part_number(self, record, part_number: int) -> None:
        validate_part_number(record, part_number)

    def _expected_part_size(self, record, part_number: int) -> int:
        return expected_part_size(record, part_number)

    def _validate_complete_parts(self, record, parts) -> None:
        validate_complete_parts(record, parts)

    def _build_upload_state(self, record, policy, mount):
        return build_upload_state(record, policy, mount, self.storage_manager)

    async def _to_read(self, record) -> UploadSessionRead:
        parts = await self.uploads.list_parts(record.id)
        return UploadSessionRead(
            id=record.id,
            mount_id=record.mount_id,
            node_id=record.node_id,
            path=record.path,
            mode=record.mode,
            state=record.state,
            upload_id=record.upload_id,
            file_name=record.file_name,
            content_type=record.content_type,
            size=record.size,
            part_size=record.part_size,
            created_at=record.created_at,
            updated_at=record.updated_at,
            parts=[UploadPartRead.model_validate(part) for part in parts],
        )


class DeleteUploadSessionService:
    def __init__(self, *, uploads: UploadRepository) -> None:
        self.uploads = uploads

    async def delete(self, actor: User, session_id: str) -> bool:
        record = await self.uploads.get_session(session_id)
        if record is None:
            return False
        if record.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload session access denied")
        record.state = UploadState.ABORTED
        await self.uploads.session.flush()
        return True
