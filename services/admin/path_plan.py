"""Read-only, resumable preflight for a future storage-key migration."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
import hashlib
import json
import logging
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, update

from db.base import session_scope
from models.enums import NodeStatus, NodeType, UserRole, UserStatus
from models.tables import BucketMount, Node, ObjectRecord, PathMigrationJob, PathPlanEntry, PathPlanTask, User
from repositories.audit_logs import AuditLogRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from schemas.task import PathTemplatePreview
from services.admin.sync import MountSyncService
from services.admin.task_claim import LEASE_DURATION, claim_task
from services.explorer.path_write import path_template_examples
from store.call import call_adapter
from store.copy_guard import source_identity
from store.errors import is_not_found, storage_error_code
from store.key import format_directory_key, format_object_key

LOGGER = logging.getLogger("cloudrave.path_plan")
PAGE_SIZE = 25


def _json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _can_relocate_imports(task, mount) -> bool:
    extra = mount.extra or {}
    return (
        (task.rename_to is not None or task.move_to is not None)
        and mount.mode == "mirror"
        and extra.get("import_writeback") is True
    )


def plan_mapping(task, policy, mount):
    current = MountSyncService.mapping_signature(policy, mount)
    if task.is_path_operation:
        values = [current, (mount.extra or {}).get("object_key_template"), mount.read_only]
        if _can_relocate_imports(task, mount):
            values.append(True)
        return _digest(_json(values))
    return current


def copy_blob_id(task, node_id: int) -> str:
    seed = f"cloudrave:copy:{task.id}"
    if task.root_node_id is not None:
        seed += f":{node_id}"
    return uuid5(NAMESPACE_URL, seed).hex


async def queue_path_operation(
    session, mount, policy, node, template, *, rename_to=None, move_to=None, copy_to=None, copy_name=None, is_restore=False,
) -> PathPlanTask:
    job_status = ("pending", "running", "paused", "failed")
    active = await session.scalar(select(PathPlanTask.id).outerjoin(
        PathMigrationJob, PathMigrationJob.plan_id == PathPlanTask.id,
    ).where(
        PathPlanTask.mount_id == mount.id,
        or_(PathPlanTask.rename_to.is_not(None), PathPlanTask.move_to.is_not(None),
            PathPlanTask.copy_to.is_not(None), PathPlanTask.is_restore.is_(True)),
        (func.coalesce(PathPlanTask.root_node_id, PathPlanTask.max_node_id) == node.id),
        (PathPlanTask.status.in_(job_status) | PathMigrationJob.status.in_(job_status)),
    ).limit(1))
    if active is not None:
        raise HTTPException(status_code=409, detail="此条目已有未完成的路径任务")
    task = PathPlanTask(
        mount_id=mount.id, owner_id=mount.owner_id, template=template, rename_to=rename_to, move_to=move_to,
        copy_to=copy_to, copy_name=copy_name, is_restore=is_restore, cursor=node.id - 1, max_node_id=node.id,
    )
    if node.type == NodeType.FOLDER or is_restore:
        if node.parent_id is None:
            raise HTTPException(status_code=409, detail="不能改动存储根目录")
        if task.target_parent_id is not None:
            target = await session.get(Node, task.target_parent_id)
            try:
                destination = await _path_chain(session, target, task)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail="目标文件夹路径已变化") from exc
            if any(part[0] == node.id for part in destination):
                raise HTTPException(status_code=409, detail="目标目录不能位于源文件夹内")
        task.root_node_id = node.id
        task.cursor = 0
        task.max_node_id = await session.scalar(select(func.max(Node.id)).where(Node.mount_id == mount.id))
    task.mapping = plan_mapping(task, policy, mount)
    session.add(task)
    await session.flush()
    return task


async def apply_queued_operation(session, task, node, target_key) -> Node | None:
    nodes = NodeRepository(session)
    new_name = task.copy_name or task.rename_to or node.name
    parent_id = node.parent_id
    if task.target_parent_id is not None:
        parent = await nodes.get_by_id_for_update(task.target_parent_id)
        if parent is None or parent.deleted_at is not None or parent.status != NodeStatus.ACTIVE:
            return None
        parent_id = parent.id
    sibling = await nodes.get_child_any(task.mount_id, parent_id, new_name)
    if sibling is not None and (task.copy_to is not None or sibling.id != node.id):
        return None
    if task.copy_to is not None:
        target = dict(owner_id=task.owner_id, mount_id=task.mount_id, parent_id=parent_id, name=new_name)
        if node.type == NodeType.FOLDER:
            node = await nodes.create_folder(**target)
        else:
            node = await nodes.create_file(**target, blob_path=target_key, size=node.size)
        action = "node.copy"
    else:
        node.name = new_name
        node.parent_id = parent_id
        action = "node.restore" if task.is_restore else "node.move" if task.move_to is not None else "node.rename"
    detail = f"parent:{parent_id}" if task.target_parent_id is not None else node.name
    await AuditLogRepository(session).create(
        actor_id=task.owner_id, action=action, target=f"node:{node.id}", detail=detail,
        ip=None, user_agent=None, request_id=None,
    )
    return node


async def _path_plan_mount(dep, session, mount_id, owner_id, *, lock: bool):
    mounts = dep.get_mount_repository(session)
    mount = await (mounts.get_by_id_for_update(mount_id) if lock else mounts.get_by_id(mount_id))
    if mount is None or mount.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Bucket mount not found")
    policy = await dep.get_policy_repository(session).get_by_id(mount.policy_id)
    if not mount.is_enabled or policy is None or policy.status != "active":
        raise HTTPException(status_code=409, detail="存储当前不可用")
    if policy.provider not in {"tencent_cos", "aliyun_oss"}:
        raise HTTPException(status_code=400, detail="当前仅支持 COS 和 OSS 路径预检查")
    if (mount.extra or {}).get("legacy_prefixed_keys"):
        raise HTTPException(status_code=409, detail="旧版前缀映射暂不支持路径预检查")
    return mount, policy


async def preview_path_template(dep, session, mount_id: int, owner_id: int, template: str) -> PathTemplatePreview:
    mount, policy = await _path_plan_mount(dep, session, mount_id, owner_id, lock=False)
    return path_template_examples(dep.storage_manager.get_adapter(policy), policy, mount, template)


async def start_path_plan(dep, session, mount_id: int, owner_id: int, template: str):
    mount, policy = await _path_plan_mount(dep, session, mount_id, owner_id, lock=True)
    path_template_examples(dep.storage_manager.get_adapter(policy), policy, mount, template)
    active = await session.scalar(select(PathPlanTask).where(
        PathPlanTask.mount_id == mount.id, PathPlanTask.status.in_(("pending", "running", "paused", "failed")),
        PathPlanTask.rename_to.is_(None), PathPlanTask.move_to.is_(None), PathPlanTask.copy_to.is_(None),
        PathPlanTask.is_restore.is_(False),
    ).order_by(PathPlanTask.created_at.desc()).limit(1))
    if active:
        if active.template == template and active.status in {"pending", "running"}:
            return active
        raise HTTPException(status_code=409, detail="该存储已有未完成的路径预检查，请先取消")
    maximum = await session.scalar(select(func.max(Node.id)).where(Node.mount_id == mount.id))
    task = PathPlanTask(mount_id=mount.id, owner_id=owner_id, template=template,
                        mapping=MountSyncService.mapping_signature(policy, mount), max_node_id=maximum or 0)
    session.add(task)
    await session.flush()
    return task


async def control_path_plan(dep, session, mount_id, task_id, owner_id, action):
    mount = await dep.get_mount_repository(session).get_by_id_for_update(mount_id)
    if mount is None or mount.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Task not found")
    task = await session.scalar(select(PathPlanTask).where(
        PathPlanTask.id == task_id, PathPlanTask.mount_id == mount_id, PathPlanTask.owner_id == owner_id,
    ).with_for_update().execution_options(populate_existing=True))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if action == "resume" and task.status in {"paused", "failed"}:
        policy = await dep.get_policy_repository(session).get_by_id(mount.policy_id)
        if not mount.is_enabled or policy is None or policy.status != "active" or task.mapping != plan_mapping(task, policy, mount):
            raise HTTPException(status_code=409, detail="存储配置已更改，请取消后重新检查")
        task.status = "pending"
        task.error = None
    elif action in {"pause", "cancel"} and task.status in {"pending", "running", "paused", "failed"}:
        task.status = "paused" if action == "pause" else "canceled"
    else:
        raise HTTPException(status_code=409, detail="任务状态不允许此操作")
    task.lease_token = None
    task.lease_expires_at = None
    await session.flush()
    return task


async def _path_chain(session, current, task, *, from_recycle=False):
    chain = []
    seen = set()
    while current is not None:
        if current.id in seen or current.mount_id != task.mount_id or current.owner_id != task.owner_id:
            raise ValueError("invalid_tree")
        seen.add(current.id)
        chain.append([current.id, current.parent_id, current.name, str(current.status), str(current.updated_at)])
        expected = NodeStatus.DELETED if from_recycle else NodeStatus.ACTIVE
        if current.status != expected or (current.deleted_at is not None) != from_recycle:
            raise ValueError("invalid_tree")
        if current.id == task.root_node_id:
            from_recycle = False
        if current.parent_id is None:
            break
        current = await session.get(Node, current.parent_id)
    if current is None:
        raise ValueError("invalid_tree")
    return chain


def path_tree_nodes(task):
    state = and_(Node.status == NodeStatus.DELETED, Node.deleted_at.is_not(None)) if task.is_restore else (
        and_(Node.status == NodeStatus.ACTIVE, Node.deleted_at.is_(None))
    )
    visible = and_(Node.mount_id == task.mount_id, Node.owner_id == task.owner_id, state)
    tree = select(Node.id).where(Node.id == task.root_node_id, visible).cte(recursive=True)
    tree = tree.union(select(Node.id).where(Node.parent_id == tree.c.id, visible))
    return select(tree.c.id)


async def _snapshot(session, node, task):
    """Build the target path and snapshot the source and destination ancestry."""
    chain = await _path_chain(session, node, task, from_recycle=task.is_restore)
    if len(chain) < 2:
        raise ValueError("invalid_tree")
    components = [item[2] for item in reversed(chain[:-1])]
    record = await session.scalar(select(ObjectRecord).where(ObjectRecord.node_id == node.id)) if node.type == NodeType.FILE else None
    record_state = None if record is None else [record.id, record.policy_id, record.blob_path, record.physical_id, record.extra, str(record.updated_at)]
    snapshot = [chain, node.size, record_state]
    destination_chain = None
    if task.target_parent_id is not None:
        target = await session.get(Node, task.target_parent_id)
        if target is None or target.type != NodeType.FOLDER:
            raise ValueError("invalid_tree")
        destination_chain = await _path_chain(session, target, task)
        components = [part[2] for part in reversed(destination_chain[:-1])] + [node.name]
        snapshot.append(destination_chain)
    if task.root_node_id is not None:
        root_index = next((index for index, part in enumerate(chain) if part[0] == task.root_node_id), None)
        if root_index is None or root_index == len(chain) - 1:
            raise ValueError("invalid_tree")
        if destination_chain is not None and any(part[0] == task.root_node_id for part in destination_chain):
            raise ValueError("invalid_tree")
        prefix = destination_chain[:-1] if destination_chain is not None else chain[root_index + 1:-1]
        name = task.copy_name or task.rename_to or chain[root_index][2]
        components = [part[2] for part in reversed(prefix)] + [name] + [part[2] for part in reversed(chain[:root_index])]
    return components, record, _digest(_json(snapshot))


async def _exists(adapter, policy, mount, key, *, folder=False):
    return await call_adapter(adapter, "object_key_exists", policy, mount, key, directory_marker=folder)


class PathPlanWorker:
    model = PathPlanTask
    worker_name = "path-plan"

    def __init__(self, dep, manager=None, *, interval=1.0):
        self.dep, self.manager, self.interval = dep, manager, interval
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name=f"{self.worker_name}-worker")

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _loop(self):
        while True:
            tracked = False
            claimed = None
            try:
                if self.manager is None or (tracked := self.manager.begin_business_write()):
                    claimed = await claim_task(self.dep.session_factory, self.model)
                    if claimed:
                        heartbeat = asyncio.create_task(self._heartbeat(*claimed))
                        try:
                            await self._page(*claimed)
                        finally:
                            heartbeat.cancel()
                            with suppress(asyncio.CancelledError):
                                await heartbeat
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("%s worker failed", self.worker_name)
            finally:
                if tracked:
                    self.manager.end_business_write()
            if claimed is None:
                await asyncio.sleep(self.interval)

    async def _heartbeat(self, task_id, token):
        while True:
            await asyncio.sleep(60)
            try:
                now = datetime.now(UTC)
                async with session_scope(self.dep.session_factory) as session:
                    model = self.model
                    result = await session.execute(update(model).where(
                        model.id == task_id, model.status == "running",
                        model.lease_token == token, model.lease_expires_at > now,
                    ).values(lease_expires_at=now + LEASE_DURATION))
                    if result.rowcount != 1:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Could not renew path precheck lease")

    async def _page(self, task_id, token):
        try:
            await self._scan_page(task_id, token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = storage_error_code(exc)
            LOGGER.warning("Path precheck failed for task %s (provider code %s)", task_id, code or "unknown")
            if code in {"AccessDenied", "Forbidden", "Unauthorized", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                message = "预检查需要读取对象、软链接、ACL、标签和存储桶版本状态，请检查权限或重新授权"
            elif isinstance(exc, ValueError) and "versioning" in str(exc).lower():
                message = "无法确认存储桶未开启版本控制；安全复制要求版本控制处于未开启状态"
            else:
                message = "预检查未完成，请检查存储连接、权限和配置后重试"
            async with session_scope(self.dep.session_factory) as session:
                await session.execute(update(PathPlanTask).where(
                    PathPlanTask.id == task_id, PathPlanTask.status == "running", PathPlanTask.lease_token == token,
                    PathPlanTask.lease_expires_at > datetime.now(UTC),
                ).values(status="failed", lease_token=None, lease_expires_at=None,
                         error=message))

    async def _scan_page(self, task_id, token):
        items = []
        async with session_scope(self.dep.session_factory) as session:
            task = await session.get(PathPlanTask, task_id)
            if task is None or task.status != "running" or task.lease_token != token:
                return
            actor = await session.get(User, task.owner_id)
            mount = await session.get(BucketMount, task.mount_id)
            policy = await self.dep.get_policy_repository(session).get_by_id(mount.policy_id) if mount else None
            if (actor is None or actor.status != UserStatus.ACTIVE or actor.role == UserRole.GUEST or
                mount is None or mount.owner_id != task.owner_id or not mount.is_enabled or policy is None or
                policy.status != "active" or task.mapping != plan_mapping(task, policy, mount)):
                raise RuntimeError("Storage or account changed")
            query = select(Node).where(
                Node.mount_id == task.mount_id, Node.id > task.cursor, Node.id <= task.max_node_id,
                Node.parent_id.is_not(None),
            )
            if task.root_node_id is not None:
                query = query.where(Node.id.in_(path_tree_nodes(task)))
            else:
                query = query.where(Node.status == NodeStatus.ACTIVE, Node.deleted_at.is_(None))
            rows = list(await session.scalars(query.order_by(Node.id).limit(PAGE_SIZE)))
            if task.is_path_operation and not rows and task.scanned == 0:
                raise ValueError("Path-change source no longer exists")
            for node in rows:
                item = {"id": node.id, "kind": node.type.value, "size": node.size, "source": None,
                        "target": None, "fingerprint": None, "physical_id": None, "reason": None}
                try:
                    components, record, fingerprint = await _snapshot(session, node, task)
                    item["fingerprint"] = fingerprint
                    if node.type == NodeType.FILE:
                        if record is None or record.policy_id != policy.id:
                            raise ValueError("source_missing")
                        item["source"], item["physical_id"] = record.blob_path, record.physical_id
                        extra = record.extra or {}
                        item["etag"] = extra.get("etag")
                        blob_id = extra.get("path_blob_id") or uuid5(NAMESPACE_URL, f"cloudrave:{mount.id}:{node.id}").hex
                        if task.copy_to is not None:
                            blob_id = copy_blob_id(task, node.id)
                        filename = components[-1]
                        if task.root_node_id is None:
                            filename = task.copy_name or task.rename_to or filename
                        item["target"] = format_object_key(task.template, directory="/".join(components[:-1]),
                            filename=filename, blob_id=blob_id)
                        item["external"] = extra.get("origin") == "bucket_import"
                    else:
                        item["target"] = format_directory_key(task.template, "/".join(components))
                        item["source"] = node.blob_path
                except ValueError as exc:
                    item["reason"] = str(exc) if str(exc) in {"source_missing", "external_readonly", "invalid_tree"} else "invalid_path"
                items.append(item)
            cursor = task.cursor
            adapter = self.dep.storage_manager.get_adapter(policy)

        # All provider calls happen outside the database transaction.
        if not task.is_restore:
            await call_adapter(adapter, "_require_unversioned", policy)
        can_relocate = _can_relocate_imports(task, mount)
        for item in items:
            if item["reason"]:
                continue
            try:
                physical_target = adapter._validate_physical_key(adapter._key(policy, mount, item["target"]) + ("/" if item["kind"] == "folder" else ""))
                item["target_hash"] = _digest(physical_target)
                source_key = adapter._key(policy, mount, item["source"]) if item["kind"] == "file" else None
                if task.root_node_id is not None and item["kind"] == "folder" and item["source"]:
                    item["physical_id"] = _digest(adapter._key(policy, mount, item["source"]) + "/")
            except ValueError:
                item["reason"] = "invalid_path"
                continue
            if item["kind"] == "file":
                reuse_source = task.is_restore and source_key == physical_target
                if item["external"] and task.copy_to is None and not reuse_source and not can_relocate:
                    item["reason"] = "external_readonly"
                    continue
                try:
                    method = "head_object" if reuse_source else "inspect_copy_source"
                    source = await call_adapter(adapter, method, policy, mount, item["source"])
                except Exception as exc:
                    if is_not_found(exc):
                        item["reason"] = "source_changed"
                        continue
                    if isinstance(exc, ValueError):
                        item["reason"] = "source_unsupported"
                        continue
                    raise
                expected_etag = str(item.get("etag") or "").strip('"')
                actual_etag = str(source.etag or "").strip('"')
                if source.size is None or int(source.size) != item["size"] or (expected_etag and expected_etag != actual_etag):
                    item["reason"] = "source_changed"
                    continue
                try:
                    modified = getattr(source, "last_modified", None) if reuse_source else source.modified
                    item["source_identity"] = source_identity(source.size, source.etag, modified)
                except (ValueError, TypeError, OverflowError):
                    item["reason"] = "source_identity_unavailable"
                    continue
                if source_key == physical_target:
                    if task.copy_to is not None:
                        item["reason"] = "target_exists"
                    else:
                        item["unchanged"] = True
                    continue
            exists = await _exists(adapter, policy, mount, item["target"], folder=item["kind"] == "folder")
            if exists:
                if task.copy_to is None and item["kind"] == "folder" and item["source"] == item["target"]:
                    item["unchanged"] = True
                else:
                    item["reason"] = "target_exists"

        async with session_scope(self.dep.session_factory) as session:
            locked_mount = await self.dep.get_mount_repository(session).get_by_id_for_update(mount.id)
            task = await session.scalar(select(PathPlanTask).where(
                PathPlanTask.id == task_id, PathPlanTask.status == "running", PathPlanTask.lease_token == token,
                PathPlanTask.lease_expires_at > datetime.now(UTC),
            ).with_for_update().execution_options(populate_existing=True))
            if task is None or task.cursor != cursor:
                return
            current_policy = await self.dep.get_policy_repository(session).get_by_id(locked_mount.policy_id) if locked_mount else None
            if (locked_mount is None or locked_mount.owner_id != task.owner_id or not locked_mount.is_enabled or
                current_policy is None or current_policy.status != "active" or task.mapping != plan_mapping(task, current_policy, locked_mount)):
                raise RuntimeError("Storage configuration changed")
            for item in items:
                node = (
                    await NodeRepository(session).get_by_id_for_update(item["id"])
                    if task.is_path_operation else await session.get(Node, item["id"])
                )
                if item["fingerprint"] is not None:
                    try:
                        if node is None or (await _snapshot(session, node, task))[2] != item["fingerprint"]:
                            item["reason"] = "changed_during_scan"
                    except ValueError:
                        item["reason"] = "changed_during_scan"
                target_hash = item.get("target_hash")
                if task.copy_to is None and target_hash and item["kind"] == "folder" and item["reason"] == "target_exists":
                    if await ObjectRepository(session).has_migration_marker(mount.id, target_hash, node_id=item["id"]):
                        item["reason"] = None
                        item["unchanged"] = True
                if target_hash:
                    prior = list(await session.scalars(select(PathPlanEntry).where(
                        PathPlanEntry.task_id == task.id, PathPlanEntry.target_hash == target_hash)))
                    if prior:
                        item["reason"] = "duplicate_target"
                        for previous in prior:
                            if previous.status == "ready":
                                previous.status, previous.reason = "conflict", "duplicate_target"
                                task.ready -= 1
                                task.conflicts += 1
                                task.bytes_to_copy -= previous.size
                if task.is_path_operation and task.root_node_id is None and item.get("unchanged") and item["reason"] is None:
                    if not await apply_queued_operation(session, task, node, item["target"]):
                        item["reason"] = "name_conflict"
                status = "conflict" if item["reason"] else "unchanged" if item.get("unchanged") else "ready"
                task.scanned += 1
                if status == "conflict": task.conflicts += 1
                elif status == "unchanged": task.unchanged += 1
                else:
                    task.ready += 1
                    task.bytes_to_copy += item["size"]
                session.add(PathPlanEntry(task_id=task.id, node_id=item["id"], kind=item["kind"],
                    source_json=_json(item["source"]), target_json=_json(item["target"]), target_hash=target_hash,
                    path_fingerprint=item["fingerprint"], source_physical_id=item["physical_id"],
                    source_identity_json=_json(item["source_identity"]) if item.get("source_identity") else None,
                    size=item["size"], status=status, reason=item["reason"]))
                # autoflush is disabled; subsequent duplicate checks must see this entry.
                await session.flush()
            if items:
                task.cursor = items[-1]["id"]
            if len(items) < PAGE_SIZE:
                task.status = "completed"
                can_start = (task.conflicts == 0 and task.scanned > 0) if task.root_node_id is not None else task.ready > 0
                if task.is_path_operation and can_start:
                    session.add(PathMigrationJob(plan_id=task.id, mount_id=task.mount_id, owner_id=task.owner_id))
            else:
                task.status = "pending"
            task.lease_token = None
            task.lease_expires_at = None
