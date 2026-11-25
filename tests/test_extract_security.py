import asyncio
import io
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from datetime import UTC, datetime

import py7zr
import pytest
from fastapi import HTTPException

from test_write_security import workspace
from models.enums import UserRole
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from repositories.policies import StoragePolicyRepository
from repositories.users import UserRepository
from services.explorer import extract
from services.explorer.archive_limits import BoundedArchiveFactory


async def prepare(w, *, suffix="zip", quota_extra=100, role=UserRole.USER, read_only=False):
    buffer = io.BytesIO()
    if suffix == "zip":
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("inside.txt", b"hello")
    else:
        with py7zr.SevenZipFile(buffer, "w") as archive:
            archive.writestr(b"hello", "inside.txt")
    content = buffer.getvalue()
    w.node.name = f"proof.{suffix}"
    w.node.size = len(content)
    w.mount.quota_bytes = len(content) + quota_extra
    w.mount.extra = {**w.mount.extra, "read_only": read_only}
    w.actor.role = role
    w.storage.get_adapter(w.policy).put_object(w.policy, w.mount, w.node.blob_path, content)
    await w.session.commit()

    @asynccontextmanager
    async def scope():
        async with w.factory.begin() as session:
            yield session

    return SimpleNamespace(session_scope=scope, storage_manager=w.storage,
        get_user_repository=UserRepository, get_mount_repository=BucketMountRepository,
        get_policy_repository=StoragePolicyRepository, get_node_repository=NodeRepository,
        get_object_repository=ObjectRepository, get_audit_repository=AuditLogRepository)


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [{"role": UserRole.GUEST}, {"read_only": True}])
async def test_extraction_rejects_guest_and_read_only_mount_before_queueing(workspace, settings):
    w = workspace
    dep = await prepare(w, **settings)
    before = set(extract._tasks)
    with pytest.raises(HTTPException) as denied:
        await extract.start_extract(dep, w.actor.id, w.node.id, None)
    assert denied.value.status_code == 403
    assert set(extract._tasks) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["zip", "7z"])
@pytest.mark.parametrize("quota_extra,expected", [(4, "failed"), (5, "completed")])
async def test_extraction_enforces_quota_for_zip_and_7z(workspace, suffix, quota_extra, expected):
    w = workspace
    dep = await prepare(w, suffix=suffix, quota_extra=quota_extra)
    started = await extract.start_extract(dep, w.actor.id, w.node.id, None)
    await asyncio.wait_for(asyncio.gather(*list(extract._running)), timeout=15)
    task = extract._tasks[started.id]
    assert task.status == expected
    async with w.factory() as session:
        used = await NodeRepository(session).mount_file_bytes(w.mount.id)
        assert used <= w.mount.quota_bytes
        if expected == "failed":
            assert task.result_node_id is None
        else:
            children = await NodeRepository(session).list_children(w.mount.id, task.result_node_id)
            assert [node.name for node in children] == ["inside.txt"]
            with w.storage.get_adapter(w.policy).open_object(w.policy, w.mount, children[0].blob_path) as stream:
                assert stream.read() == b"hello"


@pytest.mark.asyncio
async def test_background_extraction_rechecks_mount_state_before_reading(workspace):
    w = workspace
    dep = await prepare(w)
    task = SimpleNamespace(owner_id=w.actor.id)
    w.mount.is_enabled = False
    await w.session.commit()
    with pytest.raises(HTTPException) as denied:
        await extract._load_extract_source(dep, task, w.node.id, None)
    assert denied.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("same_user", [True, False])
async def test_extraction_rejects_jobs_over_per_user_and_process_limits(workspace, monkeypatch, same_user):
    w = workspace
    dep = await prepare(w)
    count = 1 if same_user else extract.MAX_RUNNING_TASKS
    active = {str(i): SimpleNamespace(owner_id=w.actor.id if same_user else 100 + i, status="running")
              for i in range(count)}
    monkeypatch.setattr(extract, "_tasks", active)
    with pytest.raises(HTTPException) as denied:
        await extract.start_extract(dep, w.actor.id, w.node.id, None)
    assert denied.value.status_code == 429
    assert len(active) == count


@pytest.mark.asyncio
async def test_cancelled_job_releases_task_slot(workspace, monkeypatch):
    dep = await prepare(workspace)
    monkeypatch.setattr(extract, "_tasks", {})
    monkeypatch.setattr(extract, "_running", set())
    async def blocked(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(extract, "_run_extract", blocked)
    started = await extract.start_extract(dep, workspace.actor.id, workspace.node.id, None)
    runners = list(extract._running)
    runners[0].cancel()
    await asyncio.gather(*runners, return_exceptions=True)
    assert not extract._running
    assert extract._tasks[started.id].status == "failed"


def test_completed_task_history_is_bounded_without_removing_active_jobs(monkeypatch):
    now = datetime.now(UTC)
    tasks = {str(i): SimpleNamespace(id=str(i), owner_id=i % 2, status="completed", updated_at=now)
             for i in range(20)}
    tasks["active"] = SimpleNamespace(id="active", owner_id=1, status="running")
    monkeypatch.setattr(extract, "_tasks", tasks)
    monkeypatch.setattr(extract, "MAX_TASK_HISTORY", 3)
    monkeypatch.setattr(extract, "MAX_USER_HISTORY", 2)
    extract._trim_history()
    assert "active" in tasks and len(tasks) <= 4
    assert sum(task.owner_id == 0 for task in tasks.values()) <= 2


def test_7z_output_writer_enforces_actual_output_and_safe_paths(tmp_path):
    factory = BoundedArchiveFactory(tmp_path, max_total=5, max_entry=4, max_entries=2, check_deadline=lambda: None)
    writer = factory.create("a.txt")
    writer.write(b"abc")
    with pytest.raises(ValueError):
        writer.write(b"de")
    other = factory.create("b.txt")
    with pytest.raises(ValueError):
        other.write(b"xyz")
    with pytest.raises(ValueError):
        factory.create("../outside.txt")
    assert (tmp_path / "a.txt").read_bytes() == b"abc"


@pytest.mark.asyncio
async def test_archive_source_read_is_bounded(monkeypatch):
    monkeypatch.setattr(extract, "MAX_ARCHIVE_BYTES", 5)
    output = io.BytesIO()
    with pytest.raises(ValueError):
        await extract._copy_stream(io.BytesIO(b"123456"), output)
    assert not output.getvalue()
