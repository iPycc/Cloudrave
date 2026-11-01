"""China Unicom WoPan token client.

Protocol details are adapted from wopan-open (MIT, Copyright 2026 OpenWoPan
contributors). The implementation is kept UI-free and scoped to Cloudrave's
encrypted provider boundary.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import time
from http.cookies import SimpleCookie
from secrets import randbelow
from pathlib import Path
from threading import Lock
from typing import BinaryIO
from urllib.parse import unquote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from drives.remote_drive.common import (
    RemoteAuthenticationError,
    RemoteProtocolError,
    decode_upload_state,
    encode_upload_state,
    parse_datetime,
    require_mapping,
)
from drives.remote_drive.models import RemoteAccount, RemoteEntry
from store.types import MultipartPart, MultipartUploadState, PresignedURL


BASE_URL = "https://panservice.mail.wo.cn"
CLIENT_ID = "1001000021"
CLIENT_SECRET = "XFmi9GS2hzk98jGX"
IV = "wNSOYIB1k1DjY5lA"


class DriveWoClient:
    ROOT_ID = "0"
    UPLOAD_TRANSPORT = "presigned"
    EAGER_PART_PLANS = False
    MIN_PART_SIZE = 5 * 1024 * 1024
    MAX_PART_SIZE = 16 * 1024 * 1024
    MAX_PARTS = 10_000
    MAX_OBJECT_SIZE = 160 * 1024 * 1024 * 1024
    FOLDER_EXISTS_CODE = "130007"
    _ZONE_CACHE_TTL_SECONDS = 30 * 60
    _zone_cache: dict[str, tuple[float, str]] = {}
    _zone_lock = Lock()

    def __init__(self, credentials: dict[str, str]) -> None:
        supplied = credentials.get("token", "").strip()
        if not supplied:
            raise RemoteAuthenticationError("联通云盘 Token 不能为空")
        self.token = self._extract_token(supplied)
        self.root_id = credentials.get("root_id", "").strip() or self.ROOT_ID
        if len(self.token) < 16:
            raise RemoteAuthenticationError("联通云盘 Token 长度无效")
        self._http = httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"Origin": "https://pan.wo.cn", "Referer": "https://pan.wo.cn/"},
        )

    @staticmethod
    def _normalize_token(value: str) -> str:
        token = value.strip()
        for _ in range(2):
            decoded = unquote(token).strip()
            if decoded == token:
                break
            token = decoded
        return token.strip('"\'')

    @classmethod
    def _extract_token(cls, value: str) -> str:
        if "WoCloud-Web-Token=" not in value:
            return cls._normalize_token(value)
        cookie = SimpleCookie()
        cookie.load(value)
        morsel = cookie.get("WoCloud-Web-Token")
        if morsel is None:
            raise RemoteAuthenticationError("Cookie 中未找到 WoCloud-Web-Token")
        return cls._normalize_token(morsel.value)

    @staticmethod
    def _sign(key: str, now: int, sequence: int, channel: str) -> str:
        return hashlib.md5(f"{key}{now}{sequence}{channel}".encode()).hexdigest()

    @staticmethod
    def _crypt(value: dict, key: str) -> str:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        pad = 16 - len(raw) % 16
        cipher = Cipher(algorithms.AES(key.encode()), modes.CBC(IV.encode()))
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(raw + bytes([pad]) * pad) + encryptor.finalize()
        return base64.b64encode(encrypted).decode()

    @staticmethod
    def _decrypt(value: str, key: str):
        cipher = Cipher(algorithms.AES(key.encode()), modes.CBC(IV.encode()))
        decryptor = cipher.decryptor()
        decoded = decryptor.update(base64.b64decode(value)) + decryptor.finalize()
        pad = decoded[-1]
        if pad < 1 or pad > 16 or decoded[-pad:] != bytes([pad]) * pad:
            raise RemoteProtocolError("联通云盘响应解密失败")
        return json.loads(decoded[:-pad].decode())

    def _dispatch(
        self,
        channel: str,
        key: str,
        params: dict,
        *,
        secret: str,
        token: str,
        body_extra: dict | None = None,
    ):
        now = int(time.time() * 1000)
        sequence = randbelow(8999) + 100_000
        body = {"secret": True, "param": self._crypt(params, secret)}
        if channel == "api-user":
            body["clientId"] = CLIENT_ID
        if body_extra:
            body.update(body_extra)
        payload = {
            "header": {
                "key": key,
                "resTime": now,
                "reqSeq": sequence,
                "channel": channel,
                "sign": self._sign(key, now, sequence, channel),
                "version": "",
            },
            "body": body,
        }
        response = self._http.post(
            f"{BASE_URL}/{channel}/dispatcher",
            json=payload,
            headers={"Accesstoken": token, "Content-Type": "application/json"},
        )
        if response.status_code == 401:
            raise RemoteAuthenticationError("联通云盘登录状态已失效")
        response.raise_for_status()
        raw = require_mapping(response.json(), "联通云盘")
        if str(raw.get("STATUS")) == "401":
            raise RemoteAuthenticationError("联通云盘登录状态已失效")
        if str(raw.get("STATUS")) != "200":
            raise RemoteProtocolError(str(raw.get("MSG") or "联通云盘服务请求失败"))
        rsp = require_mapping(raw.get("RSP"), "联通云盘")
        code = str(rsp.get("RSP_CODE") or "")
        if code == "1001":
            raise RemoteAuthenticationError(
                str(rsp.get("RSP_DESC") or "联通云盘登录状态已失效"),
                code=code,
            )
        if code != "0000":
            raise RemoteProtocolError(
                f"[{code}] {rsp.get('RSP_DESC') or '联通云盘请求失败'}",
                code=code,
            )
        data = rsp.get("DATA")
        if isinstance(data, (dict, list)):
            return data
        if isinstance(data, str) and data:
            try:
                return self._decrypt(data, secret)
            except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RemoteProtocolError("联通云盘响应无法解码") from exc
        return {}

    def _api_user(self, key: str, params: dict):
        return self._dispatch("api-user", key, params, secret=CLIENT_SECRET, token=self.token)

    def _wohome(self, key: str, params: dict, *, body_extra: dict | None = None):
        return self._dispatch(
            "wohome", key, params, secret=self.token[:16], token=self.token, body_extra=body_extra
        )

    def inspect(self) -> RemoteAccount:
        user = require_mapping(self._api_user("AppQueryUser", {"accessToken": self.token}), "联通云盘账号")
        account_id = str(user.get("userId") or "")
        if not account_id:
            raise RemoteAuthenticationError("联通云盘 Token 未返回账号信息")
        usage = require_mapping(
            self._wohome("QueryCloudUsageInfo", {"phoneNum": account_id, "clientId": CLIENT_ID}),
            "联通云盘容量",
        )
        info = require_mapping(usage.get("usageInfo"), "联通云盘容量")
        total = int(info.get("byteTotalSize") or 0)
        used = int(info.get("byteUsedSize") or 0)
        if total <= 0 or used < 0:
            raise RemoteProtocolError("联通云盘未返回有效容量信息")
        self.list_children(self.root_id)
        return RemoteAccount(account_id, str(user.get("userName") or "") or None, self.root_id, used, total)

    def list_children(self, parent_id: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        page = 0
        while True:
            data = require_mapping(
                self._wohome(
                    "QueryAllFiles",
                    {
                        "spaceType": "0", "parentDirectoryId": parent_id, "pageNum": page,
                        "pageSize": 100, "sortRule": 6, "clientId": CLIENT_ID,
                    },
                ),
                "联通云盘文件列表",
            )
            page_items: list[dict] = []
            for group in ("systemDirs", "files"):
                for item in data.get(group) or []:
                    if isinstance(item, dict):
                        page_items.append(item)
            for item in page_items:
                if not item.get("id") or not item.get("name"):
                    continue
                # WoPan returns directory type as the JSON number 0.  Using
                # ``value or ""`` discarded that valid value, so an existing
                # directory could never be rediscovered after CreateDirectory
                # reported code 130007 (already exists).
                item_type = item.get("type")
                raw_type = "" if item_type is None else str(item_type)
                if raw_type not in {"0", "1"}:
                    continue
                entries.append(RemoteEntry(
                    remote_id=str(item["id"]), parent_id=parent_id, name=str(item["name"]),
                    is_dir=raw_type == "0", size=0 if raw_type == "0" else int(item.get("size") or 0),
                    modified_at=parse_datetime(item.get("updateTime") or item.get("modifyTime")),
                    download_id=str(item.get("fid") or item["id"]),
                ))
            if len(page_items) < 100:
                return entries
            page += 1

    def download_url(self, remote_id: str) -> str:
        # GetDownloadUrl is intended for provider clients that fetch the file
        # themselves with WoPan-specific headers.  Its hydownload URL returns
        # Bad Request when exposed as a normal browser navigation.  AList uses
        # GetDownloadUrlV2 for browser-direct links, whose response wraps the
        # usable URLs in ``list``.
        data = require_mapping(
            self._wohome(
                "GetDownloadUrlV2",
                {
                    "type": "1",
                    "fidList": [remote_id],
                    "clientId": CLIENT_ID,
                },
            ),
            "联通云盘直连下载地址",
        )
        items = data.get("list")
        if not isinstance(items, list):
            raise RemoteProtocolError("联通云盘未返回直连下载地址列表")
        for item in items:
            if isinstance(item, dict) and str(item.get("fid") or "") == remote_id and item.get("downloadUrl"):
                return str(item["downloadUrl"])
        raise RemoteProtocolError("联通云盘未返回直连下载地址")

    @staticmethod
    def _file_type(name: str) -> str:
        suffix = Path(name).suffix.lower().lstrip(".")
        if suffix in {"jpg", "jpeg", "png", "gif", "bmp", "webp"}:
            return "1"
        if suffix in {"mp4", "mov", "avi", "mkv", "flv", "wmv"}:
            return "2"
        if suffix in {"mp3", "wav", "flac", "aac", "m4a"}:
            return "3"
        if suffix in {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt", "md"}:
            return "4"
        return "0"

    def create_folder(self, parent_id: str, name: str) -> RemoteEntry:
        try:
            data = require_mapping(
                self._wohome(
                    "CreateDirectory",
                    {
                        "spaceType": "0",
                        "familyId": "0",
                        "parentDirectoryId": parent_id,
                        "directoryName": name,
                        "clientId": CLIENT_ID,
                    },
                ),
                "联通云盘创建文件夹",
            )
        except RemoteProtocolError as exc:
            if exc.code != self.FOLDER_EXISTS_CODE:
                raise
            # Another upload or client may have created the directory between
            # the list and create calls. WoPan can expose it with a short delay.
            for attempt in range(5):
                try:
                    existing = next(
                        (
                            entry
                            for entry in self.list_children(parent_id)
                            if entry.is_dir and entry.name == name
                        ),
                        None,
                    )
                except RemoteProtocolError:
                    existing = None
                if existing is not None:
                    return existing
                if attempt < 4:
                    time.sleep(0.1 * (attempt + 1))
            raise
        remote_id = str(data.get("id") or "")
        if not remote_id:
            raise RemoteProtocolError("联通云盘创建文件夹未返回文件 ID")
        return RemoteEntry(remote_id, parent_id, name, True)

    def _upload_zone(self) -> str:
        cache_key = hashlib.sha256(self.token.encode()).hexdigest()
        with self._zone_lock:
            cached = self._zone_cache.get(cache_key)
            now = time.monotonic()
            if cached and now - cached[0] < self._ZONE_CACHE_TTL_SECONDS:
                return cached[1]
            data = require_mapping(
                self._wohome("GetZoneInfo", {"appId": "10000001"}, body_extra={"key": True}),
                "联通云盘上传区域",
            )
            zone = str(data.get("url") or "https://tjupload.pan.wo.cn").rstrip("/")
            self._zone_cache[cache_key] = (now, zone)
            return zone

    def begin_upload(
        self,
        *,
        parent_id: str,
        file_name: str,
        total_size: int,
        part_size: int,
        provider_checksum: str | None,
        mime_type: str | None,
    ) -> str:
        _ = provider_checksum
        return encode_upload_state({
            "i": f"{int(time.time() * 1000)}{randbelow(1000):03d}",
            "b": time.strftime("%Y%m%d%H%M%S"),
            "p": parent_id,
            "n": file_name,
            "s": total_size,
            "z": part_size,
            "t": max(1, (total_size + part_size - 1) // part_size),
            "m": mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream",
            "f": self._file_type(file_name),
            "u": self._upload_zone(),
        })

    def part_plan(self, upload: MultipartUploadState, part_number: int) -> PresignedURL:
        state = decode_upload_state(upload.provider_upload_id or upload.upload_id)
        offset = (part_number - 1) * int(state["z"])
        actual_size = min(int(state["z"]), max(0, int(state["s"]) - offset))
        file_info = {
            "spaceType": "0",
            "directoryId": state["p"],
            "batchNo": state["b"],
            "fileName": state["n"],
            "fileSize": int(state["s"]),
            "fileType": state["f"],
        }
        return PresignedURL(
            method="POST",
            url=f"{str(state['u']).rstrip('/')}/openapi/client/upload2C",
            body_mode="multipart",
            form_fields={
                "uniqueId": str(state["i"]),
                "accessToken": self.token,
                "fileName": str(state["n"]),
                "psToken": "undefined",
                "fileSize": str(state["s"]),
                "totalPart": str(state["t"]),
                "channel": "wocloud",
                "directoryId": str(state["p"]),
                "fileInfo": self._crypt(file_info, self.token[:16]),
                "partSize": str(actual_size),
                "partIndex": str(part_number),
            },
            file_field="file",
            # WoPan accepts a browser multipart POST but does not expose the
            # response through CORS. The browser records an opaque successful
            # transfer and completion verifies the resulting remote object.
            record_etag=f"part:{part_number}",
            response_mode="opaque",
        )

    def upload_part(
        self,
        upload: MultipartUploadState,
        part_number: int,
        body: bytes | BinaryIO,
    ) -> MultipartPart:
        state = decode_upload_state(upload.provider_upload_id or upload.upload_id)
        plan = self.part_plan(upload, part_number)
        expected_size = int(plan.form_fields["partSize"])
        content = body if isinstance(body, bytes) else body.read(expected_size + 1)
        if len(content) != expected_size:
            raise RemoteProtocolError(
                f"联通云盘上传分片大小无效：预期 {expected_size} 字节，实际 {len(content)} 字节"
            )
        response = self._http.post(
            plan.url,
            data=plan.form_fields,
            files={
                plan.file_field: (
                    str(state["n"]),
                    content,
                    str(state["m"]),
                )
            },
        )
        response.raise_for_status()
        raw = require_mapping(response.json(), "联通云盘上传分片")
        code = str(raw.get("code") or "")
        if code != "0000":
            raise RemoteProtocolError(
                str(raw.get("msg") or "联通云盘上传失败"),
                code=code or None,
            )
        data = require_mapping(raw.get("data"), "联通云盘上传分片")
        remote_id = str(data.get("fid") or "")
        return MultipartPart(
            part_number,
            f"remote:{remote_id}" if remote_id else f"part:{part_number}",
            expected_size,
        )

    def complete_upload(
        self,
        upload: MultipartUploadState,
        parts: list[MultipartPart],
        *,
        mime_type: str | None = None,
    ) -> RemoteEntry:
        _ = mime_type
        state = decode_upload_state(upload.provider_upload_id or upload.upload_id)
        remote_id = ""
        for part in parts:
            if part.etag.startswith("remote:"):
                remote_id = part.etag.split(":", 1)[1]
        if not remote_id:
            # Direct browser uploads have an opaque response because WoPan's
            # upload host does not publish CORS response headers. The completed
            # object can appear in the file list slightly after the last part.
            for attempt in range(10):
                match = next(
                    (
                        entry
                        for entry in self.list_children(str(state["p"]))
                        if not entry.is_dir
                        and entry.name == str(state["n"])
                        and entry.size == int(state["s"])
                    ),
                    None,
                )
                if match is not None:
                    return match
                if attempt < 9:
                    time.sleep(min(0.2 * (attempt + 1), 1.0))
            raise RemoteProtocolError("联通云盘直传完成后未找到文件")
        return RemoteEntry(remote_id, str(state["p"]), str(state["n"]), False, int(state["s"]), download_id=remote_id)

    def abort_upload(self, upload: MultipartUploadState) -> bool:
        _ = upload
        return True

    def delete_entry(self, entry: RemoteEntry) -> None:
        self._wohome(
            "DeleteFile",
            {
                "spaceType": "0",
                "vipLevel": "0",
                "dirList": [entry.remote_id] if entry.is_dir else [],
                "fileList": [entry.remote_id] if not entry.is_dir else [],
                "clientId": CLIENT_ID,
            },
        )

    def rename_entry(self, entry: RemoteEntry, new_name: str) -> None:
        self._wohome(
            "RenameFileOrDirectory",
            {
                "spaceType": "0",
                "type": 0 if entry.is_dir else 1,
                "fileType": "0",
                "id": entry.remote_id,
                "name": new_name,
                "clientId": CLIENT_ID,
            },
        )

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        file_name: str,
        mime_type: str | None = None,
    ) -> RemoteEntry:
        size = local_path.stat().st_size
        part_size = self.MIN_PART_SIZE
        state = self.begin_upload(
            parent_id=parent_id,
            file_name=file_name,
            total_size=size,
            part_size=part_size,
            provider_checksum=None,
            mime_type=mime_type,
        )
        upload = MultipartUploadState(
            upload_id=state, provider_upload_id=state, provider_type="drive_wo", mount_id="",
            object_key=file_name, upload_root=local_path.parent, upload_dir=local_path.parent,
            part_size_bytes=part_size, total_size=size,
        )
        parts: list[MultipartPart] = []
        with local_path.open("rb") as stream:
            for number in range(1, max(1, (size + part_size - 1) // part_size) + 1):
                content = stream.read(part_size)
                parts.append(self.upload_part(upload, number, content))
        return self.complete_upload(upload, parts, mime_type=mime_type)
