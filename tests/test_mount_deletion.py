from __future__ import annotations

from types import SimpleNamespace

import pytest

from models.enums import StoragePolicyType
from services.admin.mount import MountAdminService
from store.cos.obj import CosObjectMixin


class _Mounts:
    def __init__(self, mount) -> None:
        self.mount = mount
        self.deleted = False

    async def get_by_id(self, mount_id: int):
        return self.mount if mount_id == self.mount.id else None

    async def get_by_id_for_update(self, mount_id: int):
        return await self.get_by_id(mount_id)

    async def active_upload_count(self, mount_id: int) -> int:
        return 2

    async def pending_gc_count(self, mount_id: int) -> int:
        return 1

    async def delete(self, mount) -> None:
        assert mount is self.mount
        self.deleted = True


class _Policies:
    def __init__(self, policy) -> None:
        self.policy = policy

    async def get_by_id(self, policy_id: int):
        return self.policy if policy_id == self.policy.id else None


class _Nodes:
    async def mount_content_counts(self, mount_id: int) -> tuple[int, int]:
        return 4, 3


class _Uploads:
    async def list_active_for_mount(self, mount_id: int) -> list:
        return []


class _Audits:
    def __init__(self) -> None:
        self.details: list[str] = []

    async def create(self, **values) -> None:
        self.details.append(values["detail"])


class _Adapter:
    def __init__(self) -> None:
        self.purged = False

    def mount_has_objects(self, policy, mount) -> bool:
        return True

    def purge_mount(self, policy, mount) -> int:
        self.purged = True
        return 9


class _StorageManager:
    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def get_adapter(self, policy):
        return self.adapter


def _service():
    mount = SimpleNamespace(
        id=3,
        policy_id=7,
        name="COS mount",
        mount_slug="cos-mount",
        root_path="team/files",
    )
    policy = SimpleNamespace(
        id=7,
        provider=StoragePolicyType.TENCENT_COS,
        bucket_name="bucket-123",
        base_prefix="cloudrave",
    )
    mounts = _Mounts(mount)
    adapter = _Adapter()
    audits = _Audits()
    service = MountAdminService(
        session=SimpleNamespace(),
        mounts=mounts,
        policies=_Policies(policy),
        nodes=_Nodes(),
        uploads=_Uploads(),
        audits=audits,
        storage_manager=_StorageManager(adapter),
    )
    return service, mounts, adapter, audits


@pytest.mark.asyncio
async def test_delete_preview_reports_tracked_and_remote_content() -> None:
    service, _mounts, _adapter, _audits = _service()

    preview = await service.delete_preview(3)

    assert preview.file_count == 4
    assert preview.folder_count == 3
    assert preview.active_upload_count == 2
    assert preview.pending_cleanup_count == 1
    assert preview.remote_objects is True
    assert preview.has_contents is True
    assert preview.prefix == "cloudrave/team/files"


@pytest.mark.asyncio
async def test_detach_does_not_touch_storage_objects() -> None:
    service, mounts, adapter, audits = _service()
    request = SimpleNamespace(client=None, headers={}, state=SimpleNamespace(request_id="test"))
    actor = SimpleNamespace(id=1, email="admin@example.com")

    result = await service.delete_mount(request, actor, 3, delete_objects=False)

    assert mounts.deleted is True
    assert adapter.purged is False
    assert result.deleted_objects == 0
    assert "delete_objects=false" in audits.details[0]


@pytest.mark.asyncio
async def test_purge_deletes_storage_objects_before_mount() -> None:
    service, mounts, adapter, audits = _service()
    request = SimpleNamespace(client=None, headers={}, state=SimpleNamespace(request_id="test"))
    actor = SimpleNamespace(id=1, email="admin@example.com")

    result = await service.delete_mount(request, actor, 3, delete_objects=True)

    assert adapter.purged is True
    assert mounts.deleted is True
    assert result.deleted_objects == 9
    assert "delete_objects=true" in audits.details[0]


class _CosClient:
    def __init__(self) -> None:
        self.deleted: list[list[dict[str, str]]] = []
        self.aborted: list[tuple[str, str]] = []

    def list_objects_versions(self, **kwargs):
        return {
            "Version": [{"Key": "root/file.txt", "VersionId": "v1"}],
            "DeleteMarker": [{"Key": "root/old.txt", "VersionId": "d1"}],
            "IsTruncated": "false",
        }

    def list_objects(self, **kwargs):
        return {
            "Contents": [{"Key": "root/current.txt"}],
            "IsTruncated": "false",
        }

    def list_multipart_uploads(self, **kwargs):
        return {
            "Upload": [{"Key": "root/large.bin", "UploadId": "upload-1"}],
            "IsTruncated": "false",
        }

    def delete_objects(self, *, Bucket, Delete):
        self.deleted.append(Delete["Object"])
        return {}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self.aborted.append((Key, UploadId))


class _CosAdapter(CosObjectMixin):
    def __init__(self, client: _CosClient) -> None:
        self.client = client

    def _client_for(self, policy):
        return self.client

    def _bucket(self, policy) -> str:
        return "bucket-123"

    def _list_prefix(self, policy, mount) -> str:
        return "root/"


def test_cos_purge_removes_versions_current_objects_and_multipart_uploads() -> None:
    client = _CosClient()
    adapter = _CosAdapter(client)

    deleted = adapter.purge_mount(SimpleNamespace(), SimpleNamespace())

    assert deleted == 4
    assert client.deleted == [
        [
            {"Key": "root/file.txt", "VersionId": "v1"},
            {"Key": "root/old.txt", "VersionId": "d1"},
        ],
        [{"Key": "root/current.txt"}],
    ]
    assert client.aborted == [("root/large.bin", "upload-1")]
