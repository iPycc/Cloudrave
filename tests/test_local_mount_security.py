from types import SimpleNamespace
import os

import pytest

from drives.registry import _validate_local_user_mount
from store.local.fs import LocalFsAdapter
from test_write_security import workspace


@pytest.mark.parametrize("path", [
    "/upload/ATTAC/../VICTIM/", r"\upload\ATTAC\..\VICTIM",
    "/upload/ATTAC/./disk/", "/upload/ATTAC/C:/disk", "/upload/ATTAC/disk. /",
    "/upload/ATTAC2/disk/", "/upload/VICTIM/", "/upload/ATTAC/CON/",
])
def test_user_mount_rejects_ambiguous_or_foreign_paths(path):
    with pytest.raises(ValueError):
        _validate_local_user_mount(SimpleNamespace(uid="ATTAC"), "disk", {"storage_root": path})


def test_legacy_traversal_mount_cannot_read_or_purge_other_user(tmp_path):
    victim = tmp_path / "VICTIM" / "objects" / "proof.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("keep")
    adapter = LocalFsAdapter(tmp_path)
    mount = SimpleNamespace(id=1, mount_slug="disk", extra={
        "storage_root": "/upload/ATTAC/../VICTIM/", "user_managed": True})
    for operation in (lambda: adapter.open_object(None, mount, "proof.txt"),
                      lambda: adapter.purge_mount(None, mount)):
        with pytest.raises(ValueError):
            operation()
    assert victim.read_text() == "keep"


def test_user_mount_valid_path_supports_write_and_read(tmp_path):
    extra = {"user_managed": True}
    _validate_local_user_mount(SimpleNamespace(uid="ATTAC"), "disk", extra)
    adapter = LocalFsAdapter(tmp_path)
    mount = SimpleNamespace(id=1, mount_slug="disk", extra=extra)
    adapter.put_object(None, mount, "proof.txt", b"owned")
    with adapter.open_object(None, mount, "proof.txt") as stream:
        assert stream.read() == b"owned"


@pytest.mark.parametrize("link_at", ["mount", "objects"])
def test_user_mount_rejects_symlink_into_other_tenant(tmp_path, link_at):
    victim = tmp_path / "VICTIM"
    victim.mkdir()
    owner = tmp_path / "ATTAC"
    owner.mkdir()
    disk = owner / "disk"
    if link_at == "objects":
        disk.mkdir()
    link = disk if link_at == "mount" else disk / "objects"
    try:
        link.symlink_to(victim, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Directory symlink creation is not permitted")
        # Junctions need no symlink privilege and exercise Windows resolution.
        import _winapi
        _winapi.CreateJunction(str(victim), str(link))
    mount = SimpleNamespace(id=1, mount_slug="disk", extra={
        "storage_root": "/upload/ATTAC/disk/", "user_managed": True, "local_owner_uid": "ATTAC"})
    with pytest.raises(ValueError):
        LocalFsAdapter(tmp_path).prepare_mount_root(mount)


@pytest.mark.asyncio
async def test_mount_service_rejects_traversal_and_legacy_purge_keeps_victim(workspace):
    from fastapi import HTTPException
    from starlette.requests import Request
    from repositories.audit_logs import AuditLogRepository
    from repositories.mounts import BucketMountRepository
    from repositories.policies import StoragePolicyRepository
    from repositories.uploads import UploadRepository
    from schemas.policy import StoragePolicyCreate
    from schemas.user_mount import UserBucketMountCreate, UserMountCreate, UserMountUpdate
    from services.user.mount import UserMountService

    w = workspace
    victim = w.storage.adapters["local_fs"].base_root / "VICTIM" / "objects" / "proof.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("keep")
    service = UserMountService(session=w.session, mounts=BucketMountRepository(w.session),
        policies=StoragePolicyRepository(w.session), nodes=w.nodes, uploads=UploadRepository(w.session),
        audits=AuditLogRepository(w.session), storage_manager=w.storage)
    request = Request({"type": "http", "method": "POST", "path": "/mounts", "headers": [],
                       "client": ("127.0.0.1", 1234), "scheme": "http", "query_string": b""})
    payload = UserMountCreate(policy=StoragePolicyCreate(name="owned", provider="local_fs", bucket_name="owned"),
        mount=UserBucketMountCreate(name="owned", mount_slug="owned",
            extra={"storage_root": "/upload/USER1/../VICTIM/"}))
    with pytest.raises(HTTPException) as denied:
        await service.create_mount(request, w.actor, payload)
    assert denied.value.status_code == 403
    payload.mount.extra = {"storage_root": "/upload/USER1/owned/"}
    created = await service.create_mount(request, w.actor, payload)
    with pytest.raises(HTTPException) as denied:
        await service.update_mount(request, w.actor, created.mount.id,
            UserMountUpdate(mount={"extra": {"storage_root": "/upload/USER1/../VICTIM/"}}))
    assert denied.value.status_code == 403
    mount = await BucketMountRepository(w.session).get_by_id(created.mount.id)
    mount.extra = {**mount.extra, "storage_root": "/upload/USER1/../VICTIM/"}
    await w.session.flush()
    with pytest.raises(HTTPException) as denied:
        await service.delete_mount(request, w.actor, mount.id, delete_objects=True)
    assert denied.value.status_code == 502
    assert victim.read_text() == "keep"
    assert await BucketMountRepository(w.session).get_by_id(mount.id) is not None
