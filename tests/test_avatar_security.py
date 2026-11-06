from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image

from models.enums import UserRole
from services.user import profile
from web.mid.upload import AvatarBodyLimitMiddleware


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
@pytest.mark.parametrize("size,expected", [(256, 200), (2048, 413)])
async def test_avatar_body_limit_handles_missing_content_length_before_handler(size, expected):
    from fastapi import FastAPI, File
    app = FastAPI()
    handled = []
    @app.post("/api/v1/user/me/avatar")
    async def upload(file: UploadFile = File(...)):
        handled.append(True)
        return {"ok": True}
    limited = AvatarBodyLimitMiddleware(app, max_bytes=1024)
    body = (b'--audit\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\n'
            b'Content-Type: image/png\r\n\r\n' + b'x' * size + b'\r\n--audit--\r\n')
    messages = [{"type": "http.request", "body": body[i:i+256], "more_body": i+256 < len(body)}
                for i in range(0, len(body), 256)]
    async def receive():
        return messages.pop(0)
    sent = []
    async def send(message):
        sent.append(message)
    await limited({"type": "http", "method": "POST", "path": "/api/v1/user/me/avatar",
        "query_string": b"", "headers": [(b"content-type", b"multipart/form-data; boundary=audit")],
        "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1), "http_version": "1.1"}, receive, send)
    assert sent[0]["status"] == expected
    assert handled == ([True] if expected == 200 else [])


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
