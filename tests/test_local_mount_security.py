from types import SimpleNamespace

import pytest

from drives.registry import _validate_local_user_mount
from store.local.fs import LocalFsAdapter


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
        pytest.skip("Directory symlink creation is not permitted")
    mount = SimpleNamespace(id=1, mount_slug="disk", extra={
        "storage_root": "/upload/ATTAC/disk/", "user_managed": True, "local_owner_uid": "ATTAC"})
    with pytest.raises(ValueError):
        LocalFsAdapter(tmp_path).prepare_mount_root(mount)
