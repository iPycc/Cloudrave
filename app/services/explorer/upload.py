"""Upload services."""

from __future__ import annotations

from math import ceil
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NodeType, StoragePolicyType, UploadMode, UploadState, UserRole
from app.models.tables import BucketMount, StoragePolicy, User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.objects import ObjectRepository
from app.repositories.policies import StoragePolicyRepository
from app.repositories.uploads import UploadRepository
from app.schemas.node import NodeRead
from app.schemas.upload import (
    UploadPartComplete,
    UploadPartPlan,
    UploadPartRead,
    UploadSessionAbortRequest,
    UploadSessionCompleteRequest,
    UploadSessionCreate,
    UploadSessionPlan,
    UploadSessionRead,
)
from app.storage.manager import StorageManager
from app.storage.types import MultipartPart, MultipartUploadState


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

    async def create(self, request: Request, actor: User, payload: UploadSessionCreate) -> UploadSessionPlan:
        mount, policy = await self._require_mount_access(actor, payload.mount_id)
        parent = await self._resolve_parent_folder(mount, payload.parent_id)
        object_key = payload.object_key or self.storage_manager.build_object_key(
            mount,
            payload.file_name,
            await self._build_parent_relative_path(mount, parent),
        )
        part_size = payload.part_size or policy.part_size_mb * 1024 * 1024
        adapter = self.storage_manager.get_adapter(policy)

        upload_mode = payload.mode
        if upload_mode == UploadMode.SINGLE and policy.provider == StoragePolicyType.LOCAL_FS:
            upload_mode = UploadMode.MULTIPART

        if policy.provider == StoragePolicyType.TENCENT_COS and upload_mode == UploadMode.SINGLE:
            presigned = adapter.presign_put(policy, mount, object_key)
            session_record = await self.uploads.create_session(
                owner_id=mount.owner_id,
                mount_id=mount.id,
                node_id=parent.id if parent else None,
                path=object_key,
                mode=upload_mode,
                upload_id=None,
                file_name=payload.file_name,
                content_type=payload.content_type,
                size=payload.size,
                part_size=part_size,
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
            return UploadSessionPlan(
                session_id=session_record.id,
                upload_mode=upload_mode,
                object_key=object_key,
                part_size=part_size,
                upload_urls=[
                    UploadPartPlan(
                        part_number=1,
                        method=presigned.method,
                        url=presigned.url,
                        headers=presigned.headers,
                        expires_at=presigned.expires_at,
                    )
                ],
                expires_at=presigned.expires_at,
            )

        upload_state = adapter.create_multipart_upload(
            policy,
            mount,
            object_key,
            total_size=payload.size,
            mime_type=payload.content_type,
        )
        session_record = await self.uploads.create_session(
            owner_id=mount.owner_id,
            mount_id=mount.id,
            node_id=parent.id if parent else None,
            path=object_key,
            mode=UploadMode.MULTIPART,
            upload_id=upload_state.upload_id,
            file_name=payload.file_name,
            content_type=payload.content_type,
            size=payload.size,
            part_size=part_size,
        )
        part_count = max(1, ceil(payload.size / part_size)) if payload.size else 1
        upload_urls = []
        if policy.provider == StoragePolicyType.LOCAL_FS:
            upload_urls = [
                UploadPartPlan(
                    part_number=part_number,
                    method="POST",
                    url=f"/api/v1/explorer/upload/{session_record.id}/part",
                )
                for part_number in range(1, part_count + 1)
            ]
        else:
            upload_urls = [
                UploadPartPlan(
                    part_number=part_number,
                    method=presigned.method,
                    url=presigned.url,
                    headers=presigned.headers,
                    expires_at=presigned.expires_at,
                )
                for part_number in range(1, part_count + 1)
                for presigned in [adapter.presign_part(policy, mount, upload_state, part_number)]
            ]
        await self.audits.create(
            actor_id=actor.id,
            action="upload.create",
            target=f"upload:{session_record.id}",
            detail=object_key,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return UploadSessionPlan(
            session_id=session_record.id,
            upload_mode=UploadMode.MULTIPART,
            upload_id=upload_state.upload_id,
            object_key=object_key,
            part_size=part_size,
            upload_urls=upload_urls,
            expires_at=upload_state.expires_at,
        )

    async def get(self, actor: User, session_id: str) -> UploadSessionRead:
        record = await self._require_upload_access(actor, session_id)
        return await self._to_read(record)

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
        mount, policy = await self._require_mount_access(actor, record.mount_id)
        adapter = self.storage_manager.get_adapter(policy)

        resolved_etag = etag
        resolved_size = size
        if policy.provider == StoragePolicyType.LOCAL_FS:
            if file is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Local upload requires file content")
            payload = await file.read()
            part = adapter.upload_part(
                policy,
                mount,
                self._build_upload_state(record, policy, mount),
                part_number,
                payload,
            )
            resolved_etag = part.etag
            resolved_size = part.size or len(payload)

        part_record = await self.uploads.upsert_part(
            upload_session_id=record.id,
            part_number=part_number,
            etag=resolved_etag,
            size=resolved_size,
        )
        await self.uploads.set_state(record.id, UploadState.UPLOADING)
        return UploadPartRead.model_validate(part_record)

    async def complete(
        self,
        request: Request,
        actor: User,
        session_id: str,
        payload: UploadSessionCompleteRequest,
    ) -> UploadSessionRead:
        record = await self._require_upload_access(actor, session_id)
        mount, policy = await self._require_mount_access(actor, record.mount_id)
        adapter = self.storage_manager.get_adapter(policy)
        parts = await self.uploads.list_parts(record.id)

        if payload.parts:
            for part in payload.parts:
                await self.uploads.upsert_part(
                    upload_session_id=record.id,
                    part_number=part.part_number,
                    etag=part.etag,
                    size=part.size,
                )
            parts = await self.uploads.list_parts(record.id)

        if policy.provider == StoragePolicyType.LOCAL_FS:
            upload_state = self._build_upload_state(record, policy, mount)
            storage_parts = adapter.list_parts(policy, mount, upload_state)
            adapter.complete_multipart_upload(
                policy,
                mount,
                upload_state,
                storage_parts,
                mime_type=record.content_type,
            )
        else:
            adapter.complete_multipart_upload(
                policy,
                mount,
                self._build_upload_state(record, policy, mount),
                [MultipartPart(part_number=part.part_number, etag=part.etag or "", size=part.size) for part in parts],
                mime_type=record.content_type,
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
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A file with the same name already exists") from exc

        await self.objects.create_or_update(
            node_id=file_node.id,
            policy_id=policy.id,
            blob_path=record.path,
            content_type=record.content_type,
        )
        await self.uploads.bind_node(record.id, file_node.id)
        await self.uploads.set_state(record.id, UploadState.COMPLETED)
        await self.audits.create(
            actor_id=actor.id,
            action="upload.complete",
            target=f"upload:{record.id}",
            detail=record.path,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return await self._to_read(record)

    async def abort(
        self,
        request: Request,
        actor: User,
        session_id: str,
        payload: UploadSessionAbortRequest | None = None,
    ) -> UploadSessionRead:
        _ = payload
        record = await self._require_upload_access(actor, session_id)
        mount, policy = await self._require_mount_access(actor, record.mount_id)
        adapter = self.storage_manager.get_adapter(policy)
        if policy.provider == StoragePolicyType.LOCAL_FS and record.upload_id:
            adapter.abort_multipart_upload(policy, mount, self._build_upload_state(record, policy, mount))
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
        if actor.role != UserRole.ADMIN and mount.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Mount access denied")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return mount, policy

    async def _resolve_parent_folder(self, mount: BucketMount, parent_id: int | None):
        if parent_id is None:
            return await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
        parent = await self.nodes.get_by_id(parent_id)
        if parent is None or parent.mount_id != mount.id or parent.type != NodeType.FOLDER:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found")
        return parent

    async def _build_parent_relative_path(self, mount: BucketMount, parent_node) -> str | None:
        root = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
        if parent_node.id == root.id:
            return None
        segments: list[str] = []
        current = parent_node
        while current is not None and current.id != root.id:
            segments.append(current.name)
            if current.parent_id is None:
                break
            current = await self.nodes.get_by_id(current.parent_id)
        return "/".join(reversed(segments)) if segments else None

    async def _require_upload_access(self, actor: User, session_id: str):
        record = await self.uploads.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
        if actor.role != UserRole.ADMIN and record.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload session access denied")
        return record

    def _build_upload_state(self, record, policy, mount) -> MultipartUploadState:
        adapter = self.storage_manager.get_adapter(policy)
        mount_root = adapter.prepare_mount_root(mount)
        upload_id = record.upload_id or record.id
        return MultipartUploadState(
            upload_id=upload_id,
            provider_type=policy.provider.value,
            mount_id=str(mount.id),
            object_key=record.path,
            upload_root=mount_root,
            upload_dir=Path(mount_root) / ".multipart" / upload_id,
            part_size_bytes=record.part_size,
            total_size=record.size,
            provider_upload_id=record.upload_id,
        )

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
        if actor.role != UserRole.ADMIN and record.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload session access denied")
        record.state = UploadState.ABORTED
        await self.uploads.session.flush()
        return True
