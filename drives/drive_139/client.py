"""China Mobile Cloud (139) personal-drive protocol client.

This is an independent Python implementation based on the public web protocol.
It intentionally supports token-based personal-drive access only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from drives.remote_drive.common import (
    RemoteAuthenticationError,
    RemoteProtocolError,
    decode_upload_state,
    encode_upload_state,
    find_number,
    parse_datetime,
    require_mapping,
)
from drives.remote_drive.models import RemoteAccount, RemoteEntry
from store.types import MultipartPart, MultipartUploadState, PresignedURL


class Drive139Client:
    ROOT_ID = "/"
    MIN_PART_SIZE = 5 * 1024 * 1024
    PART_SIZE_ALIGNMENT = 64
    MAX_PART_SIZE = 64 * 1024 * 1024
    MAX_PARTS = 10_000
    MAX_OBJECT_SIZE = 50 * 1024 * 1024 * 1024

    def __init__(self, credentials: dict[str, str]) -> None:
        self.credentials = credentials
        self.allow_token_refresh = True
        self._refresh_attempted = False
        authorization = credentials.get("authorization", "").strip()
        self.authorization = authorization.removeprefix("Basic ").strip()
        self.user_domain_id = credentials.get("user_domain_id", "").strip()
        self.root_id = credentials.get("root_id", "").strip() or self.ROOT_ID
        if not self.authorization:
            raise RemoteAuthenticationError("移动云盘 Authorization 不能为空")
        try:
            decoded = base64.b64decode(self.authorization).decode("utf-8")
            parts = decoded.split(":", 2)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RemoteAuthenticationError("移动云盘 Authorization 格式无效") from exc
        if len(parts) < 3 or not parts[1]:
            raise RemoteAuthenticationError("移动云盘 Authorization 缺少账号信息")
        self.account_id = parts[1]
        self._http = httpx.Client(timeout=30.0, follow_redirects=True)
        self._personal_host: str | None = None

    def _refresh_if_needed(self) -> None:
        if not self.allow_token_refresh or self._refresh_attempted:
            return
        self._refresh_attempted = True
        prefix, account, token = base64.b64decode(self.authorization).decode().split(":", 2)
        fields = token.split("|")
        try:
            remaining = int(fields[3]) - int(time.time() * 1000)
        except (IndexError, ValueError):
            return  # Unrecognized token format: let the provider validate it.
        if remaining <= 0:
            raise RemoteAuthenticationError("移动云盘 Authorization 已过期")
        if remaining > 15 * 24 * 60 * 60 * 1000:
            return
        root = ElementTree.Element("root")
        for key, value in (("token", token), ("account", account), ("clienttype", "656")):
            ElementTree.SubElement(root, key).text = value
        response = self._http.post(
            "https://aas.caiyun.feixin.10086.cn/tellin/authTokenRefresh.do",
            content=ElementTree.tostring(root, encoding="utf-8"),
            headers={"Content-Type": "application/xml"},
        )
        if response.status_code == 401:
            raise RemoteAuthenticationError("移动云盘登录状态已失效")
        response.raise_for_status()
        try:
            result = ElementTree.fromstring(response.text)
        except ElementTree.ParseError as exc:
            raise RemoteProtocolError("移动云盘续期响应无法解析") from exc
        new_token = result.findtext("token")
        if result.findtext("return") != "0" or not new_token:
            # A rejected renewal does not prove that the still-valid token has
            # expired. Continue with it; authentication failure is handled below.
            return
        self.authorization = base64.b64encode(f"{prefix}:{account}:{new_token}".encode()).decode()
        self.credentials["authorization"] = self.authorization

    @staticmethod
    def _signature(body: str, timestamp: str, nonce: str) -> str:
        escaped = quote(body, safe="~()*!.'-")
        sorted_body = "".join(sorted(escaped))
        encoded = base64.b64encode(sorted_body.encode()).decode()
        first = hashlib.md5(encoded.encode()).hexdigest()
        second = hashlib.md5(f"{timestamp}:{nonce}".encode()).hexdigest()
        return hashlib.md5(f"{first}{second}".encode()).hexdigest().upper()

    def _headers(self, body: str) -> dict[str, str]:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nonce = "".join(random.SystemRandom().choices(string.ascii_letters + string.digits, k=16))
        return {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Basic {self.authorization}",
            "Caller": "web",
            "Cms-Device": "default",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Route": "001",
            "Mcloud-Sign": f"{timestamp},{nonce},{self._signature(body, timestamp, nonce)}",
            "Mcloud-Version": "7.14.0",
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Client-Info": "||13|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||",
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": "1",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/w/",
        }

    def _post_url(self, url: str, payload: dict) -> dict:
        self._refresh_if_needed()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        response = self._http.post(url, content=body.encode(), headers={**self._headers(body), "Content-Type": "application/json"})
        if response.status_code == 401:
            raise RemoteAuthenticationError("移动云盘登录状态已失效")
        response.raise_for_status()
        raw = require_mapping(response.json(), "移动云盘")
        success = raw.get("success")
        if str(raw.get("code") or "") in {"401", "403"}:
            raise RemoteAuthenticationError(str(raw.get("message") or "移动云盘登录状态已失效"))
        if success is False:
            raise RemoteProtocolError(str(raw.get("message") or "移动云盘请求失败"))
        return dict(raw)

    def _personal_url(self) -> str:
        if self._personal_host:
            return self._personal_host
        raw = self._post_url(
            "https://user-njs.yun.139.com/user/route/qryRoutePolicy",
            {"userInfo": {"userType": 1, "accountType": 1, "accountName": self.account_id}, "modAddrType": 1},
        )
        policies = ((raw.get("data") or {}).get("routePolicyList") or [])
        for item in policies:
            if isinstance(item, dict) and item.get("modName") == "personal" and item.get("httpsUrl"):
                self._personal_host = str(item["httpsUrl"]).rstrip("/")
                return self._personal_host
        raise RemoteProtocolError("移动云盘未返回个人云路由")

    def _personal_post(self, path: str, payload: dict) -> dict:
        return self._post_url(f"{self._personal_url()}{path}", payload)

    def inspect(self) -> RemoteAccount:
        self.list_children(self.root_id)
        if not self.user_domain_id:
            raise RemoteProtocolError("请同时填写移动云盘 Cookie 中的 ud_id，以便读取空间容量")
        raw = self._post_url(
            "https://user-njs.yun.139.com/user/disk/quota/detail",
            {"userDomainId": self.user_domain_id},
        )
        total = find_number(raw, ("diskSize", "totalSize", "totalSpace"))
        free = find_number(raw, ("freeDiskSize", "freeSize", "freeSpace"))
        used = find_number(raw, ("usedDiskSize", "usedSize", "usedSpace"))
        if total is None or total <= 0:
            raise RemoteProtocolError("移动云盘未返回有效容量信息，请检查 ud_id")
        # This endpoint reports capacity in MiB.
        total_bytes = total * 1024 * 1024
        used_bytes = (used if used is not None else max(0, total - int(free or 0))) * 1024 * 1024
        return RemoteAccount(self.account_id, self.account_id, self.root_id, used_bytes, total_bytes)

    def list_children(self, parent_id: str) -> list[RemoteEntry]:
        cursor = ""
        entries: list[RemoteEntry] = []
        while True:
            raw = self._personal_post(
                "/file/list",
                {
                    "imageThumbnailStyleList": ["Small", "Large"],
                    "orderBy": "updated_at",
                    "orderDirection": "DESC",
                    "pageInfo": {"pageCursor": cursor, "pageSize": 100},
                    "parentFileId": parent_id,
                },
            )
            data = require_mapping(raw.get("data"), "移动云盘文件列表")
            for item in data.get("items") or []:
                if not isinstance(item, dict) or not item.get("fileId") or not item.get("name"):
                    continue
                is_dir = item.get("type") == "folder"
                entries.append(RemoteEntry(
                    remote_id=str(item["fileId"]), parent_id=parent_id, name=str(item["name"]),
                    is_dir=is_dir, size=0 if is_dir else int(item.get("size") or 0),
                    modified_at=parse_datetime(item.get("updatedAt")),
                ))
            cursor = str(data.get("nextPageCursor") or "")
            if not cursor:
                return entries

    def download_url(self, remote_id: str) -> str:
        raw = self._personal_post("/file/getDownloadUrl", {"fileId": remote_id})
        data = require_mapping(raw.get("data"), "移动云盘下载地址")
        url = ""
        if data.get("cdnSwitch") is True:
            url = str(data.get("cdnUrl") or "").strip()
        url = url or str(data.get("url") or "").strip()
        if not url:
            raise RemoteProtocolError("移动云盘未返回下载地址")
        return url

    def create_folder(self, parent_id: str, name: str) -> RemoteEntry:
        raw = self._personal_post(
            "/file/create",
            {
                "parentFileId": parent_id,
                "name": name,
                "description": "",
                "type": "folder",
                "fileRenameMode": "force_rename",
            },
        )
        data = require_mapping(raw.get("data") or {}, "移动云盘创建文件夹")
        remote_id = str(data.get("fileId") or data.get("id") or "")
        if not remote_id:
            match = next((entry for entry in self.list_children(parent_id) if entry.is_dir and entry.name == name), None)
            if match is None:
                raise RemoteProtocolError("移动云盘创建文件夹后未返回文件 ID")
            return match
        return RemoteEntry(remote_id, parent_id, str(data.get("fileName") or name), True)

    @staticmethod
    def _part_info(part_number: int, part_size: int, total_size: int) -> dict[str, object]:
        offset = (part_number - 1) * part_size
        actual = min(part_size, max(0, total_size - offset))
        return {
            "partNumber": part_number,
            "partSize": actual,
            "parallelHashCtx": {"partOffset": offset},
        }

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
        checksum = str(provider_checksum or "").lower()
        if len(checksum) != 64:
            raise RemoteProtocolError("移动云盘直传需要完整文件的 SHA-256")
        initial = [self._part_info(1, part_size, total_size)]
        raw = self._personal_post(
            "/file/create",
            {
                "contentHash": checksum,
                "contentHashAlgorithm": "SHA256",
                "contentType": mime_type or "application/octet-stream",
                "parallelUpload": False,
                "partInfos": initial,
                "size": total_size,
                "parentFileId": parent_id,
                "name": file_name,
                "type": "file",
                "fileRenameMode": "auto_rename",
            },
        )
        data = require_mapping(raw.get("data"), "移动云盘初始化上传")
        file_id = str(data.get("fileId") or "")
        if not file_id:
            raise RemoteProtocolError("移动云盘初始化上传未返回 fileId")
        state = {
            "f": file_id,
            "u": str(data.get("uploadId") or ""),
            "p": parent_id,
            "n": str(data.get("fileName") or file_name),
            "s": total_size,
            "z": part_size,
            "h": checksum,
            "r": not bool(data.get("partInfos")),
        }
        return encode_upload_state(state)

    def part_plan(self, upload: MultipartUploadState, part_number: int) -> PresignedURL:
        state = decode_upload_state(upload.provider_upload_id or upload.upload_id)
        if state.get("r"):
            return PresignedURL(
                method="SKIP",
                url="",
                record_etag=f"rapid:{part_number}",
                response_mode="skip",
            )
        part_info = self._part_info(part_number, int(state["z"]), int(state["s"]))
        raw = self._personal_post(
            "/file/getUploadUrl",
            {
                "fileId": state["f"],
                "uploadId": state["u"],
                "partInfos": [part_info],
                "commonAccountInfo": {"account": self.account_id, "accountType": 1},
            },
        )
        data = require_mapping(raw.get("data"), "移动云盘分片地址")
        values = data.get("partInfos") or []
        part = next((item for item in values if int(item.get("partNumber") or 0) == part_number), None)
        url = str((part or {}).get("uploadUrl") or "").replace("http://", "https://", 1)
        if not url:
            raise RemoteProtocolError("移动云盘未返回分片上传地址")
        return PresignedURL(
            method="PUT",
            url=url,
            headers={"Content-Type": "application/octet-stream"},
            record_etag=f"part:{part_number}",
        )

    def upload_part(self, upload: MultipartUploadState, part_number: int, body) -> MultipartPart:
        state = decode_upload_state(upload.provider_upload_id or upload.upload_id)
        part_size = int(state["z"])
        expected_size = min(part_size, max(0, int(state["s"]) - (part_number - 1) * part_size))
        content = body if isinstance(body, bytes) else body.read(expected_size + 1)
        if len(content) != expected_size:
            raise RemoteProtocolError(f"移动云盘上传分片大小无效：预期 {expected_size} 字节，实际 {len(content)} 字节")
        plan = self.part_plan(upload, part_number)
        response = self._http.put(plan.url, content=content, headers=plan.headers)
        response.raise_for_status()
        return MultipartPart(part_number, plan.record_etag or f"part:{part_number}", expected_size)

    def complete_upload(
        self,
        upload: MultipartUploadState,
        parts: list[MultipartPart],
        *,
        mime_type: str | None = None,
    ) -> RemoteEntry:
        _ = (parts, mime_type)
        state = decode_upload_state(upload.provider_upload_id or upload.upload_id)
        if not state.get("r"):
            self._personal_post(
                "/file/complete",
                {
                    "contentHash": state["h"],
                    "contentHashAlgorithm": "SHA256",
                    "fileId": state["f"],
                    "uploadId": state["u"],
                },
            )
        return RemoteEntry(str(state["f"]), str(state["p"]), str(state["n"]), False, int(state["s"]))

    def abort_upload(self, upload: MultipartUploadState) -> bool:
        _ = upload
        return True

    def delete_entry(self, entry: RemoteEntry) -> None:
        self._personal_post("/recyclebin/batchTrash", {"fileIds": [entry.remote_id]})

    def rename_entry(self, entry: RemoteEntry, new_name: str) -> None:
        self._personal_post(
            "/file/update",
            {"fileId": entry.remote_id, "name": new_name, "description": ""},
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
        hasher = hashlib.sha256()
        with local_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        state = self.begin_upload(
            parent_id=parent_id,
            file_name=file_name,
            total_size=size,
            part_size=part_size,
            provider_checksum=hasher.hexdigest(),
            mime_type=mime_type,
        )
        upload = MultipartUploadState(
            upload_id=state, provider_upload_id=state, provider_type="drive_139", mount_id="",
            object_key=file_name, upload_root=local_path.parent, upload_dir=local_path.parent,
            part_size_bytes=part_size, total_size=size,
        )
        parts: list[MultipartPart] = []
        with local_path.open("rb") as stream:
            for number in range(1, max(1, (size + part_size - 1) // part_size) + 1):
                chunk = stream.read(part_size)
                plan = self.part_plan(upload, number)
                if plan.response_mode != "skip":
                    response = self._http.put(plan.url, content=chunk, headers=plan.headers)
                    response.raise_for_status()
                parts.append(MultipartPart(number, plan.record_etag or f"part:{number}", len(chunk)))
        return self.complete_upload(upload, parts, mime_type=mime_type)
