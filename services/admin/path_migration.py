"""Copy storage objects and switch their Cloudrave index in a short transaction."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import case, or_, select, update

from db.base import session_scope
from models.enums import NodeType, UserRole, UserStatus
from models.tables import BucketMount, Node, PathMigrationJob, PathMigrationRecord, PathPlanEntry, PathPlanTask, User
from repositories.blobs import BlobRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from services.admin.path_plan import PathPlanWorker, _snapshot, apply_queued_path_change, plan_mapping
from store.call import call_adapter
from store.copy_guard import source_identity
from store.errors import is_not_found, storage_error_code

LOGGER = logging.getLogger("cloudrave.path_migration")


class MigrationConflict(ValueError):
    pass


async def start_migration(dep, session, mount_id: int, plan_id: str, owner_id: int) -> PathMigrationJob:
    """Queue the ready entries from a completed precheck."""
    mount = await dep.get_mount_repository(session).get_by_id_for_update(mount_id)
    if mount is None or mount.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Storage mount not found")
    plan = await session.scalar(select(PathPlanTask).where(
        PathPlanTask.id == plan_id, PathPlanTask.mount_id == mount.id, PathPlanTask.owner_id == owner_id,
    ).with_for_update().execution_options(populate_existing=True))
    if plan is None:
        raise HTTPException(status_code=404, detail="Path precheck not found")
    policy = await dep.get_policy_repository(session).get_by_id(mount.policy_id)
    if (not mount.is_enabled or mount.read_only or policy is None or policy.status != "active" or
        policy.provider not in {"aliyun_oss", "tencent_cos"} or
        plan.mapping != plan_mapping(plan, policy, mount)):
        raise HTTPException(status_code=409, detail="存储配置已更改或不可写，请重新检查")
    if plan.status != "completed" or plan.ready <= 0:
        raise HTTPException(status_code=409, detail="请先完成预检查，且至少有一项可迁移")
    missing_identity = await session.scalar(select(PathPlanEntry.id).where(
        PathPlanEntry.task_id == plan.id, PathPlanEntry.status == "ready", PathPlanEntry.kind == NodeType.FILE.value,
        or_(PathPlanEntry.source_identity_json.is_(None), PathPlanEntry.path_fingerprint.is_(None)),
    ).limit(1))
    if missing_identity is not None:
        raise HTTPException(status_code=409, detail="旧预检查缺少源对象身份，请重新检查")
    existing = await session.scalar(select(PathMigrationJob).where(PathMigrationJob.plan_id == plan.id))
    if existing is not None:
        return existing
    active = await session.scalar(select(PathMigrationJob.id).where(
        PathMigrationJob.mount_id == mount.id, PathMigrationJob.status.in_(("pending", "running", "paused", "failed")),
    ).limit(1))
    if active is not None:
        raise HTTPException(status_code=409, detail="该存储已有未完成的路径迁移")
    job = PathMigrationJob(plan_id=plan.id, mount_id=mount.id, owner_id=owner_id, conflicts=plan.conflicts)
    session.add(job)
    await session.flush()
    return job


async def control_migration(dep, session, mount_id: int, plan_id: str, owner_id: int, action: str) -> PathMigrationJob:
    mount = await dep.get_mount_repository(session).get_by_id_for_update(mount_id)
    if mount is None or mount.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Storage mount not found")
    job = await session.scalar(select(PathMigrationJob).where(
        PathMigrationJob.plan_id == plan_id, PathMigrationJob.mount_id == mount_id, PathMigrationJob.owner_id == owner_id,
    ).with_for_update())
    if job is None:
        raise HTTPException(status_code=404, detail="Path migration not found")
    if action == "pause" and job.status in {"pending", "running"}:
        job.status = "paused"
    elif (action == "resume" and job.status in {"paused", "failed"}) or (
        action == "cancel" and job.status in {"pending", "paused", "failed"}
    ):
        if job.lease_expires_at is not None and _utc(job.lease_expires_at) > datetime.now(UTC):
            raise HTTPException(status_code=409, detail="当前对象仍在处理，请稍后再试")
        job.status = "pending" if action == "resume" else "canceled"
        job.error = job.lease_token = job.lease_expires_at = None
        if action == "cancel":
            await session.execute(update(PathMigrationRecord).where(
                PathMigrationRecord.job_id == job.id, PathMigrationRecord.status == "reserved",
            ).values(status="canceled"))
            result = await session.execute(update(PathMigrationRecord).where(
                PathMigrationRecord.job_id == job.id, PathMigrationRecord.status.in_(("copying", "copied")),
            ).values(status="uncertain", error="目标可能已有副本，源文件已保留"))
            job.conflicts += result.rowcount
    else:
        raise HTTPException(status_code=409, detail="当前状态无法执行此操作")
    await session.flush()
    return job


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def reserve_file_copy(dep, job_id: str, entry_id: int, lease_token: str) -> PathMigrationRecord | None:
    """Reserve the destination before writing to the bucket."""
    async with session_scope(dep.session_factory) as session:
        job = await session.get(PathMigrationJob, job_id)
        if job is None or job.status != "running" or job.lease_token != lease_token:
            return None
        plan = await session.get(PathPlanTask, job.plan_id)
        entry = await session.get(PathPlanEntry, entry_id)
        mount = await session.get(BucketMount, job.mount_id)
        policy = await dep.get_policy_repository(session).get_by_id(mount.policy_id) if mount else None
        if (plan is None or entry is None or entry.task_id != plan.id or
            entry.status != "ready" or mount is None or policy is None):
            raise ValueError("Invalid migration entry")
        is_file = entry.kind == NodeType.FILE.value
        source_key, target_key = json.loads(entry.source_json or "null"), json.loads(entry.target_json or "null")
        if (is_file and not isinstance(source_key, str)) or not isinstance(target_key, str):
            raise ValueError("Missing source or target key")
        expected_identity_json = entry.source_identity_json
        identity = json.loads(expected_identity_json or "null")
        if is_file and (not isinstance(identity, dict) or
            source_identity(identity.get("size"), identity.get("etag"), identity.get("modified")) != identity or
            identity["size"] != entry.size):
            raise ValueError("Invalid precheck source identity")
        adapter = dep.storage_manager.get_adapter(policy)
        expected_mapping = plan.mapping
        expected_source_id = entry.source_physical_id
        expected_fingerprint = entry.path_fingerprint

    # Provider identity calculation is outside the transaction. The mapping is
    # rechecked under the mount lock before either identity is reserved.
    source_id = await call_adapter(adapter, "physical_id", policy, mount, source_key) if is_file else None
    target_id = await call_adapter(adapter, "physical_id", policy, mount, target_key) if is_file else None
    if is_file and (not isinstance(source_id, str) or not isinstance(target_id, str) or
        not re.fullmatch(r"[0-9a-f]{64}", source_id) or not re.fullmatch(r"[0-9a-f]{64}", target_id) or
        source_id == target_id or (expected_source_id and source_id != expected_source_id)):
        raise ValueError("Storage identity changed since precheck")

    async with session_scope(dep.session_factory) as session:
        locked_mount = await dep.get_mount_repository(session).get_by_id_for_update(mount.id)
        job = await session.scalar(select(PathMigrationJob).where(
            PathMigrationJob.id == job_id, PathMigrationJob.status == "running",
            PathMigrationJob.lease_token == lease_token, PathMigrationJob.lease_expires_at > datetime.now(UTC),
        ).with_for_update().execution_options(populate_existing=True))
        if job is None:
            return None
        plan = await session.get(PathPlanTask, job.plan_id)
        entry = await session.get(PathPlanEntry, entry_id)
        actor = await session.get(User, job.owner_id)
        current_policy = await dep.get_policy_repository(session).get_by_id(locked_mount.policy_id) if locked_mount else None
        if (actor is None or actor.status != UserStatus.ACTIVE or actor.role == UserRole.GUEST or
            locked_mount is None or locked_mount.owner_id != job.owner_id or not locked_mount.is_enabled or
            locked_mount.read_only or current_policy is None or current_policy.status != "active" or
            plan is None or plan.mount_id != job.mount_id or plan.owner_id != job.owner_id or
            plan.status != "completed" or plan.mapping != expected_mapping or
            plan_mapping(plan, current_policy, locked_mount) != expected_mapping or
            entry is None or entry.task_id != plan.id or entry.id <= job.cursor or
            entry.status != "ready" or entry.source_identity_json != expected_identity_json or
            entry.path_fingerprint != expected_fingerprint or
            json.loads(entry.source_json or "null") != source_key or json.loads(entry.target_json or "null") != target_key):
            raise ValueError("Migration state changed before reservation")
        existing = await session.scalar(select(PathMigrationRecord).where(
            PathMigrationRecord.job_id == job.id, PathMigrationRecord.plan_entry_id == entry.id,
        ))
        if existing is not None:
            return existing
        node = await session.get(Node, entry.node_id)
        await _require_snapshot(session, node, plan, entry)

        # GC also locks this row before inspecting reservations and deleting.
        if target_id:
            await BlobRepository(session).lock(target_id)
            objects = ObjectRepository(session)
            if await objects.has_migration_reservation(target_id) or await objects.has_physical_reference(target_id):
                raise MigrationConflict("目标已被其他文件登记或预留")
        reservation = PathMigrationRecord(job_id=job.id, plan_entry_id=entry.id,
            source_physical_id=source_id, target_physical_id=target_id,
            status="reserved", copy_token=str(uuid4()))
        session.add(reservation)
        await session.flush()
        return reservation


async def _require_snapshot(session, node, plan, entry):
    try:
        if node is not None and (await _snapshot(session, node, plan))[2] == entry.path_fingerprint:
            return
    except ValueError:
        pass
    raise MigrationConflict("文件或目录在预检查后发生变化")


async def copy_reserved_file(dep, job_id: str, record_id: int, lease_token: str) -> str:
    """Write one reserved target, recording uncertainty before any retry.

    A successful copy is only marked ``copied``; a later transaction must
    revalidate and switch the node mapping. The old source is never deleted.
    If the process dies after the provider write, a later worker must call
    ``mark_abandoned_copy`` instead of issuing the write again.
    """
    async with session_scope(dep.session_factory) as session:
        job = await session.get(PathMigrationJob, job_id)
        if job is None:
            return "stale"
        mount = await dep.get_mount_repository(session).get_by_id_for_update(job.mount_id)
        job = await session.scalar(select(PathMigrationJob).where(
            PathMigrationJob.id == job_id, PathMigrationJob.status == "running",
            PathMigrationJob.lease_token == lease_token, PathMigrationJob.lease_expires_at > datetime.now(UTC),
        ).with_for_update().execution_options(populate_existing=True))
        if job is None:
            return "stale"
        record = await session.scalar(select(PathMigrationRecord).where(
            PathMigrationRecord.id == record_id, PathMigrationRecord.job_id == job.id,
        ).with_for_update().execution_options(populate_existing=True))
        plan = await session.get(PathPlanTask, job.plan_id)
        entry = await session.get(PathPlanEntry, record.plan_entry_id) if record else None
        policy = await dep.get_policy_repository(session).get_by_id(mount.policy_id) if mount else None
        actor = await session.get(User, job.owner_id)
        if (record is None or record.status != "reserved" or not record.copy_token or entry is None or
            entry.task_id != job.plan_id or entry.status != "ready" or plan is None or plan.status != "completed" or
            mount is None or mount.owner_id != job.owner_id or not mount.is_enabled or mount.read_only or
            actor is None or actor.status != UserStatus.ACTIVE or actor.role == UserRole.GUEST or
            policy is None or policy.status != "active" or
            plan_mapping(plan, policy, mount) != plan.mapping):
            raise ValueError("Migration state changed before copying")
        node = await session.get(Node, entry.node_id)
        await _require_snapshot(session, node, plan, entry)
        source_key = json.loads(entry.source_json or "null")
        target_key = json.loads(entry.target_json or "null")
        identity = json.loads(entry.source_identity_json or "null")
        is_file = entry.kind == NodeType.FILE.value
        copy_token = record.copy_token
        adapter = dep.storage_manager.get_adapter(policy)
        record.status = "copying"
        record.error = None
        await session.flush()

    try:
        if is_file:
            stored = await call_adapter(adapter, "copy_object", policy, mount, source_key, target_key,
                                        no_overwrite=True, expected_source_identity=identity)
            if stored.size != entry.size or not stored.etag:
                raise ValueError("Provider did not verify the copied object")
            etag, version = stored.etag, stored.version_id
        else:
            await call_adapter(adapter, "put_directory_marker", policy, mount, target_key)
            etag, version = None, None
        if (etag and len(etag) > 255) or (version and len(version) > 255):
            raise ValueError("Provider returned an invalid object identity")
    except asyncio.CancelledError:
        # SDK calls may keep running after thread cancellation. The journal
        # remains copying and the target remains reserved for later review.
        raise
    except Exception as exc:
        if storage_error_code(exc) in {"FileAlreadyExists", "PreconditionFailed"}:
            return await _finish_copy_record(dep, job_id, record_id, lease_token, copy_token,
                                             status="conflict", error="目标已有对象，或源文件发生变化")
        return await _finish_copy_record(dep, job_id, record_id, lease_token, copy_token,
                                         status="uncertain", error="复制结果未确认，源文件已保留，请检查目标对象")
    return await _finish_copy_record(dep, job_id, record_id, lease_token, copy_token,
                                     status="copied", etag=etag, version=version)


async def _finish_copy_record(dep, job_id: str, record_id: int, lease_token: str, copy_token: str, *,
                              status: str, error: str | None = None, etag: str | None = None,
                              version: str | None = None) -> str:
    async with session_scope(dep.session_factory) as session:
        job = await session.scalar(select(PathMigrationJob).where(
            PathMigrationJob.id == job_id, PathMigrationJob.status.in_(("running", "paused")),
            PathMigrationJob.lease_token == lease_token, PathMigrationJob.lease_expires_at > datetime.now(UTC),
        ).with_for_update().execution_options(populate_existing=True))
        if job is None:
            return "stale"
        record = await session.scalar(select(PathMigrationRecord).where(
            PathMigrationRecord.id == record_id, PathMigrationRecord.job_id == job_id,
            PathMigrationRecord.status == "copying", PathMigrationRecord.copy_token == copy_token,
        ).with_for_update().execution_options(populate_existing=True))
        if record is None:
            return "stale"
        record.status, record.error = status, error
        record.target_etag, record.target_version_id = etag, version
        await session.flush()
        return status


async def mark_abandoned_copy(dep, job_id: str, record_id: int, lease_token: str) -> str:
    """A new lease cannot infer whether an interrupted provider write landed."""
    async with session_scope(dep.session_factory) as session:
        job = await session.scalar(select(PathMigrationJob).where(
            PathMigrationJob.id == job_id, PathMigrationJob.status == "running",
            PathMigrationJob.lease_token == lease_token, PathMigrationJob.lease_expires_at > datetime.now(UTC),
        ).with_for_update().execution_options(populate_existing=True))
        if job is None:
            return "stale"
        record = await session.scalar(select(PathMigrationRecord).where(
            PathMigrationRecord.id == record_id, PathMigrationRecord.job_id == job_id,
        ).with_for_update().execution_options(populate_existing=True))
        if record is None:
            return "stale"
        if record.status == "copying":
            record.status = "uncertain"
            record.error = "复制中断，源文件已保留，请检查目标对象"
            await session.flush()
        return record.status


async def apply_copy(dep, job_id: str, record_id: int, lease_token: str) -> None:
    async with session_scope(dep.session_factory) as session:
        job = await session.get(PathMigrationJob, job_id)
        record = await session.get(PathMigrationRecord, record_id)
        entry = await session.get(PathPlanEntry, record.plan_entry_id)
        plan = await session.get(PathPlanTask, job.plan_id)
        mount = await session.get(BucketMount, job.mount_id)
        policy = await dep.get_policy_repository(session).get_by_id(mount.policy_id)
        if not mount.is_enabled or mount.read_only or policy.status != "active" or (
            plan_mapping(plan, policy, mount) != plan.mapping
        ):
            raise ValueError("Storage mapping changed before switching the index")
        target_key = json.loads(entry.target_json)

    adapter = dep.storage_manager.get_adapter(policy)
    is_file = entry.kind == NodeType.FILE.value
    try:
        if is_file:
            stored = await call_adapter(adapter, "head_object", policy, mount, target_key)
            matches = stored.size == entry.size and stored.etag == record.target_etag and stored.version_id == record.target_version_id
        else:
            matches = await call_adapter(adapter, "object_key_exists", policy, mount, target_key, directory_marker=True)
    except Exception as exc:
        if not is_not_found(exc):
            raise
        matches = False

    async with session_scope(dep.session_factory) as session:
        mount = await dep.get_mount_repository(session).get_by_id_for_update(mount.id)
        job = await session.scalar(_owned_job(job_id, lease_token).with_for_update())
        if job is None:
            return
        current_policy = await dep.get_policy_repository(session).get_by_id(mount.policy_id) if mount else None
        actor = await session.get(User, job.owner_id)
        if (actor is None or actor.status != UserStatus.ACTIVE or actor.role == UserRole.GUEST or
            mount is None or mount.owner_id != job.owner_id or not mount.is_enabled or mount.read_only or
            current_policy is None or current_policy.status != "active" or
            plan_mapping(plan, current_policy, mount) != plan.mapping):
            raise ValueError("Storage mapping changed before switching the index")
        record = await session.get(PathMigrationRecord, record_id)
        if record.status != "copied":
            return
        node = await NodeRepository(session).get_by_id_for_update(entry.node_id)
        try:
            _, obj, fingerprint = await _snapshot(session, node, plan) if node is not None else (None, None, None)
        except ValueError:
            obj, fingerprint = None, None
        if not matches or fingerprint != entry.path_fingerprint or (is_file and (
            obj is None or obj.blob_path != json.loads(entry.source_json) or node.blob_path != obj.blob_path
        )):
            record.status = "uncertain"
            record.error = "复制后文件或目标发生变化，源文件已保留"
            job.conflicts += 1
        else:
            if is_file:
                objects = ObjectRepository(session)
                for identity in sorted((record.source_physical_id, record.target_physical_id)):
                    await objects.blobs.lock(identity)
                if await objects.has_physical_reference(record.target_physical_id):
                    record.status = "uncertain"
                    record.error = "目标已被其他文件登记，源文件已保留"
                    job.conflicts += 1
                    job.cursor = entry.id
                    return
                if plan.changes_node_path and not await apply_queued_path_change(session, plan, node):
                    record.status = "uncertain"
                    record.error = "目标位置已变化或有同名条目，源文件已保留"
                    job.conflicts += 1
                    job.cursor = entry.id
                    return
                await objects.blobs.ensure(record.target_physical_id, revive=True)
                node.blob_path = obj.blob_path = target_key
                obj.physical_id = record.target_physical_id
                extra = {**(obj.extra or {}), "etag": stored.etag, "version_id": stored.version_id}
                extra.pop("last_modified", None)
                if getattr(stored, "last_modified", None):
                    extra["last_modified"] = str(stored.last_modified)
                obj.extra = extra
            record.status = "applied"
            job.migrated += 1
        job.cursor = entry.id


def _owned_job(job_id: str, token: str):
    return select(PathMigrationJob).where(
        PathMigrationJob.id == job_id, PathMigrationJob.status == "running",
        PathMigrationJob.lease_token == token, PathMigrationJob.lease_expires_at > datetime.now(UTC),
    )


class PathMigrationWorker(PathPlanWorker):
    model = PathMigrationJob
    worker_name = "path-migration"

    async def _page(self, job_id, token):
        try:
            await self._migrate_next(job_id, token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("Path migration %s stopped (%s)", job_id, storage_error_code(exc) or type(exc).__name__)
            await self._release(job_id, token, "failed", "迁移已停止，请检查权限、文件和存储配置后重试")

    async def _migrate_next(self, job_id, token):
        async with session_scope(self.dep.session_factory) as session:
            job = await session.scalar(_owned_job(job_id, token))
            entry_id = await session.scalar(select(PathPlanEntry.id).where(
                PathPlanEntry.task_id == job.plan_id, PathPlanEntry.id > job.cursor, PathPlanEntry.status == "ready",
            ).order_by(PathPlanEntry.id).limit(1)) if job is not None else None
        if entry_id is None:
            await self._release(job_id, token, "completed")
            return
        try:
            record = await reserve_file_copy(self.dep, job_id, entry_id, token)
            if record is None:
                await self._release(job_id, token, "pending")
                return
            state = record.status
            if state == "reserved":
                state = await copy_reserved_file(self.dep, job_id, record.id, token)
            elif state == "copying":
                state = await mark_abandoned_copy(self.dep, job_id, record.id, token)
        except MigrationConflict as exc:
            await self._skip(job_id, token, entry_id, str(exc))
            await self._release(job_id, token, "pending")
            return
        if state == "copied":
            await apply_copy(self.dep, job_id, record.id, token)
        elif state in {"uncertain", "conflict"}:
            await self._skip(job_id, token, entry_id)
        await self._release(job_id, token, "pending")

    async def _skip(self, job_id, token, entry_id, error=None):
        async with session_scope(self.dep.session_factory) as session:
            job = await session.scalar(_owned_job(job_id, token).with_for_update())
            if job is None:
                return
            if error:
                record = await session.scalar(select(PathMigrationRecord).where(
                    PathMigrationRecord.job_id == job_id, PathMigrationRecord.plan_entry_id == entry_id,
                ))
                if record is None:
                    record = PathMigrationRecord(job_id=job_id, plan_entry_id=entry_id)
                    session.add(record)
                record.status, record.error = "conflict", error
            job.cursor = entry_id
            job.conflicts += 1

    async def _release(self, job_id, token, status, error=None):
        async with session_scope(self.dep.session_factory) as session:
            await session.execute(update(PathMigrationJob).where(
                PathMigrationJob.id == job_id, PathMigrationJob.status.in_(("running", "paused")),
                PathMigrationJob.lease_token == token,
            ).values(
                status=case((PathMigrationJob.status == "paused", "paused"), else_=status),
                error=error, lease_token=None, lease_expires_at=None,
            ))
