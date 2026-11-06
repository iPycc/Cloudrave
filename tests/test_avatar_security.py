from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.requests import Request

from models.enums import UserRole
from services.user import profile


@pytest.mark.asyncio
async def test_avatar_size_limit_reads_bounded_chunks(monkeypatch):
    monkeypatch.setattr(profile, "MAX_AVATAR_BYTES", 100)
    requested = []

    class File:
        async def read(self, size):
            requested.append(size)
            return b"x" * size

    with pytest.raises(HTTPException) as denied:
        await profile._avatar_payload(File())
    assert denied.value.status_code == 413
    assert requested == [101]


@pytest.mark.asyncio
async def test_avatar_invalid_image_preserves_existing_file(tmp_path):
    old = tmp_path / "data" / "avatar" / "USER1" / "old.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"existing")
    user = SimpleNamespace(uid="USER1", role=UserRole.USER, avatar_path="avatar/USER1/old.png")
    service = object.__new__(profile.UserProfileService)
    service.config = SimpleNamespace(root_dir=tmp_path)
    service._get_managed_user = AsyncMock(return_value=user)
    with pytest.raises(HTTPException) as denied:
        await service.upload_avatar(None, user, UploadFile(filename="fake.png", file=BytesIO(b"<html>bad</html>")))
    assert denied.value.status_code == 422
    assert old.read_bytes() == b"existing"


@pytest.mark.asyncio
async def test_avatar_reencodes_and_bounds_real_image():
    data = BytesIO()
    Image.new("RGB", (1024, 512)).save(data, "PNG")
    payload = await profile._avatar_payload(UploadFile(filename="photo.png", file=BytesIO(data.getvalue())))
    with Image.open(BytesIO(payload)) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.size == (512, 256)


@pytest.mark.asyncio
async def test_avatar_rejects_excessive_pixel_count(monkeypatch):
    data = BytesIO()
    Image.new("RGB", (20, 20)).save(data, "PNG")
    monkeypatch.setattr(profile, "MAX_AVATAR_PIXELS", 100)
    with pytest.raises(HTTPException) as denied:
        await profile._avatar_payload(UploadFile(filename="photo.png", file=BytesIO(data.getvalue())))
    assert denied.value.status_code == 422
