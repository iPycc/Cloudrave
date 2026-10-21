"""China Telecom Cloud (189) session-token client."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
import base64
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse
from xml.etree import ElementTree

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from drives.remote_drive.common import (
    RemoteAuthenticationError,
    RemoteProtocolError,
    clean_url,
    decode_upload_state,
    encode_upload_state,
    find_number,
    parse_datetime,
    require_mapping,
)
from drives.remote_drive.models import RemoteAccount, RemoteEntry
from store.types import MultipartPart, MultipartUploadState, PresignedURL


API_URL = "https://api.cloud.189.cn"
UPLOAD_URL = "https://upload.cloud.189.cn"
CLIENT_SUFFIX = {"clientType": "TELEPC", "version": "6.2", "channelId": "web_cloud.189.cn"}


class Drive189Client:
    ROOT_ID = "-11"
    PART_CHECKSUM_ALGORITHM = "md5"
    MIN_PART_SIZE = 10 * 1024 * 1024
    MAX_PART_SIZE = 5 * 1024 * 1024 * 1024
    MAX_PARTS = 1_999
    MAX_OBJECT_SIZE = 50 * 1024 * 1024 * 1024

    def __init__(self, credentials: dict[str, str]) -> None:
        self.credentials = credentials
        value = credentials.get("session", "").strip()
        if not value:
            raise RemoteAuthenticationError("天翼云盘会话 Token 不能为空")
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RemoteAuthenticationError("天翼云盘 Token 需要是 JSON 对象") from exc
        if not isinstance(raw, dict):
            raise RemoteAuthenticationError("天翼云盘 Token 需要是 JSON 对象")
        nested = raw.get("tokenInfo") if isinstance(raw.get("tokenInfo"), dict) else {}
        self.data = {**nested, **raw}
        self.root_id = credentials.get("root_id", "").strip() or str(self.data.get("rootId") or self.ROOT_ID)
        self._http = httpx.Client(timeout=30.0, follow_redirects=True, headers={"Accept": "application/json;charset=UTF-8", "Referer": "https://cloud.189.cn"})
        self._ensure_session()

    def _value(self, *names: str) -> str:
        lowered = {str(key).lower(): value for key, value in self.data.items()}
        for name in names:
            value = self.data.get(name, lowered.get(name.lower()))
            if value not in (None, ""):
                return str(value)
        return ""

    def _ensure_session(self, *, force: bool = False) -> None:
        if not force and self._value("sessionKey") and self._value("sessionSecret"):
            return
        access_token = self._value("accessToken", "access_token")
        if not access_token:
            raise RemoteAuthenticationError("天翼云盘 Token 缺少 accessToken 或 sessionKey/sessionSecret")
        response = self._http.get(
            f"{API_URL}/getSessionForPC.action",
            params={**CLIENT_SUFFIX, "appId": "8025431004", "accessToken": access_token, "rand": uuid.uuid4().hex},
            headers={"X-Request-ID": str(uuid.uuid4())},
        )
        if self._session_expired(response):
            raise RemoteAuthenticationError("天翼云盘 accessToken 已失效")
        response.raise_for_status()
        payload = self._decode(response)
        if not isinstance(payload, dict) or not payload.get("sessionKey") or not payload.get("sessionSecret"):
            raise RemoteProtocolError("天翼云盘未返回有效会话，请稍后重试")
        self.data.update(payload)
        self.credentials["session"] = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))

    def _headers(self, method: str, url: str, encrypted_params: str = "") -> dict[str, str]:
        date = format_datetime(datetime.now(timezone.utc), usegmt=True)
        session_key = self._value("sessionKey")
        session_secret = self._value("sessionSecret")
        path = urlparse(url).path
        source = f"SessionKey={session_key}&Operate={method}&RequestURI={path}&Date={date}"
        if encrypted_params:
            source += f"&params={encrypted_params}"
        signature = hmac.new(session_secret.encode(), source.encode(), hashlib.sha1).hexdigest().upper()
        return {"Date": date, "SessionKey": session_key, "X-Request-ID": str(uuid.uuid4()), "Signature": signature}

    def _encrypt_params(self, params: dict[str, object]) -> str:
        source = "&".join(f"{key}={params[key]}" for key in sorted(params))
        raw = source.encode()
        pad = 16 - len(raw) % 16
        cipher = Cipher(algorithms.AES(self._value("sessionSecret")[:16].encode()), modes.ECB())
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(raw + bytes([pad]) * pad) + encryptor.finalize()
        return encrypted.hex().upper()

    @staticmethod
    def _decode(response: httpx.Response):
        try:
            return response.json()
        except json.JSONDecodeError:
            try:
                root = ElementTree.fromstring(response.text)
            except ElementTree.ParseError as exc:
                raise RemoteProtocolError("天翼云盘响应无法解析") from exc
            return {child.tag.split("}")[-1]: child.text for child in root.iter() if child is not root and len(child) == 0}

    def _get(self, path: str, params: dict[str, object], *, retry: bool = True) -> dict:
        url = f"{API_URL}{path}"
        response = self._http.get(
            url,
            params={**CLIENT_SUFFIX, "rand": f"{uuid.uuid4().int % 100000}_{uuid.uuid4().int % 10000000000}", **params},
            headers=self._headers("GET", url),
        )
        if self._session_expired(response):
            if retry:
                self._ensure_session(force=True)
                return self._get(path, params, retry=False)
            raise RemoteAuthenticationError("天翼云盘登录状态已失效")
        response.raise_for_status()
        raw = self._decode(response)
        mapping = require_mapping(raw, "天翼云盘")
        error_code = str(mapping.get("errorCode") or mapping.get("code") or "")
        if error_code and error_code not in {"0", "SUCCESS"}:
            raise RemoteProtocolError(str(mapping.get("errorMsg") or mapping.get("message") or error_code))
        return dict(mapping)

    @staticmethod
    def _session_expired(response: httpx.Response) -> bool:
        if response.status_code == 401:
            return True
        if response.status_code >= 500:
            return False
        try:
            raw = Drive189Client._decode(response)
        except RemoteProtocolError:
            return False
        if not isinstance(raw, dict):
            return False
        code = str(raw.get("errorCode") or raw.get("code") or "")
        return code in {"InvalidSessionKey", "UserInvalidOpenToken", "InvalidAccessToken"}

    def _post(self, path: str, params: dict[str, object], *, data=None, retry: bool = True) -> dict:
        url = f"{API_URL}{path}"
        response = self._http.post(
            url, params={**CLIENT_SUFFIX, **params}, data=data,
            headers=self._headers("POST", url),
        )
        if self._session_expired(response):
            if retry:
                self._ensure_session(force=True)
                return self._post(path, params, data=data, retry=False)
            raise RemoteAuthenticationError("天翼云盘登录状态已失效")
        response.raise_for_status()
        raw = require_mapping(self._decode(response), "天翼云盘")
        error_code = str(raw.get("errorCode") or raw.get("code") or "")
        if error_code and error_code not in {"0", "SUCCESS"}:
            raise RemoteProtocolError(str(raw.get("errorMsg") or raw.get("message") or error_code))
        return dict(raw)

    def _encrypted_get(self, path: str, params: dict[str, object], *, retry: bool = True) -> dict:
        url = f"{UPLOAD_URL}{path}"
        encrypted = self._encrypt_params(params)
        response = self._http.get(
            url,
            params={
                **CLIENT_SUFFIX,
                "rand": f"{uuid.uuid4().int % 100000}_{uuid.uuid4().int % 10000000000}",
                "params": encrypted,
            },
            headers=self._headers("GET", url, encrypted),
        )
        if self._session_expired(response):
            if retry:
                self._ensure_session(force=True)
                return self._encrypted_get(path, params, retry=False)
            raise RemoteAuthenticationError("天翼云盘登录状态已失效")
        response.raise_for_status()
        raw = require_mapping(self._decode(response), "天翼云盘上传")
        error_code = str(raw.get("errorCode") or raw.get("code") or "")
        if error_code and error_code not in {"0", "SUCCESS"}:
            raise RemoteProtocolError(str(raw.get("errorMsg") or raw.get("message") or error_code))
        return dict(raw)

    def inspect(self) -> RemoteAccount:
        self.list_children(self.root_id)
        raw = self._get("/getUserSizeInfo.action", {})
        total = find_number(raw, ("capacity", "totalSize", "totalSpace", "total"))
        available = find_number(raw, ("available", "freeSize", "freeSpace"))
        used = find_number(raw, ("usedSize", "usedSpace", "used"))
        if total is None or total <= 0:
            raise RemoteProtocolError("天翼云盘未返回有效容量信息")
        used_bytes = used if used is not None else max(0, total - int(available or 0))
        login_name = self._value("loginName", "userName", "account")
        account_id = login_name or self._value("userId") or "189-cloud"
        return RemoteAccount(account_id, login_name or None, self.root_id, used_bytes, total)

    def list_children(self, parent_id: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        page = 1
        while True:
            raw = self._get(
                "/listFiles.action",
                {"folderId": parent_id, "fileType": 0, "mediaAttr": 0, "iconOption": 5, "pageNum": page, "pageSize": 100, "recursive": 0, "orderBy": "filename", "descending": "false"},
            )
            listing = raw.get("fileListAO") if isinstance(raw.get("fileListAO"), dict) else raw
            folders = listing.get("folderList") or []
            files = listing.get("fileList") or []
            for item in folders:
                if isinstance(item, dict) and item.get("id") not in (None, ""):
                    entries.append(RemoteEntry(str(item["id"]), parent_id, str(item.get("name") or "未命名文件夹"), True, modified_at=parse_datetime(item.get("lastOpTime"))))
            for item in files:
                if isinstance(item, dict) and item.get("id") not in (None, ""):
                    entries.append(RemoteEntry(str(item["id"]), parent_id, str(item.get("name") or "未命名文件"), False, int(item.get("size") or 0), parse_datetime(item.get("lastOpTime")), str(item["id"])))
            if len(folders) + len(files) < 100:
                return entries
            page += 1

    def download_url(self, remote_id: str) -> str:
        raw = self._get("/getFileDownloadUrl.action", {"fileId": remote_id, "dt": 3, "flag": 1})
        url = clean_url(raw.get("fileDownloadUrl"))
        if not url:
            raise RemoteProtocolError("天翼云盘未返回下载地址")
        response = self._http.get(url, follow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            return clean_url(response.headers.get("location"))
        return url

    def create_folder(self, parent_id: str, name: str) -> RemoteEntry:
        raw = self._post(
            "/createFolder.action",
            {
                "folderName": name,
                "relativePath": "",
                "parentFolderId": parent_id,
            },
        )
        remote_id = str(raw.get("id") or raw.get("folderId") or "")
        if not remote_id:
            match = next((entry for entry in self.list_children(parent_id) if entry.is_dir and entry.name == name), None)
            if match is None:
                raise RemoteProtocolError("天翼云盘创建文件夹后未返回文件 ID")
            return match
        return RemoteEntry(remote_id, parent_id, str(raw.get("name") or name), True)

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
        _ = mime_type
        checksum = str(provider_checksum or "").upper()
        if len(checksum) != 32:
            raise RemoteProtocolError("天翼云盘直传需要完整文件的 MD5")
        raw = self._encrypted_get(
            "/person/initMultiUpload",
            {
                "parentFolderId": parent_id,
                "fileName": quote_plus(file_name),
                "fileSize": total_size,
                "sliceSize": part_size,
                "lazyCheck": 1,
            },
        )
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        upload_id = str(data.get("uploadFileId") or "")
        if not upload_id:
            raise RemoteProtocolError("天翼云盘初始化上传未返回 uploadFileId")
        return encode_upload_state({
            "u": upload_id,
            "p": parent_id,
            "n": file_name,
            "s": total_size,
            "z": part_size,
            "h": checksum,
        })

    @staticmethod
    def _with_client_suffix(url: str) -> str:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.update(CLIENT_SUFFIX)
        query["rand"] = f"{uuid.uuid4().int % 100000}_{uuid.uuid4().int % 10000000000}"
        return urlunparse(parsed._replace(query=urlencode(query)))

    def part_plan(self, upload: MultipartUploadState, part_number: int) -> PresignedURL:
        state = decode_upload_state(upload.provider_upload_id or upload.upload_id)
        checksum = str(upload.metadata.get("part_checksum") or "").lower()
        if len(checksum) != 32:
            raise RemoteProtocolError("天翼云盘分片直传需要 MD5")
        try:
            encoded = base64.b64encode(bytes.fromhex(checksum)).decode()
        except ValueError as exc:
            raise RemoteProtocolError("天翼云盘分片 MD5 格式无效") from exc
        raw = self._encrypted_get(
            "/person/getMultiUploadUrls",
            {"uploadFileId": state["u"], "partInfo": f"{part_number}-{encoded}"},
        )
        values = raw.get("uploadUrls")
        if not isinstance(values, dict) and isinstance(raw.get("data"), dict):
            values = raw["data"].get("uploadUrls") or raw["data"]
        values = require_mapping(values, "天翼云盘分片地址")
        item = values.get(f"partNumber_{part_number}")
        item = require_mapping(item, "天翼云盘分片地址")
        url = str(item.get("requestURL") or "")
        if not url:
            raise RemoteProtocolError("天翼云盘未返回分片上传地址")
        headers = {
            key: value
            for field in str(item.get("requestHeader") or "").split("&")
            if "=" in field
            for key, value in [field.split("=", 1)]
        }
        return PresignedURL(
            method="PUT",
            url=self._with_client_suffix(url),
            headers=headers,
            record_etag=checksum.upper(),
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
        ordered = [part.etag.upper() for part in sorted(parts, key=lambda value: value.part_number)]
        slice_md5 = state["h"]
        if len(ordered) > 1:
            slice_md5 = hashlib.md5("\n".join(ordered).encode()).hexdigest().upper()
        raw = self._encrypted_get(
            "/person/commitMultiUploadFile",
            {
                "uploadFileId": state["u"],
                "fileMd5": state["h"],
                "sliceMd5": slice_md5,
                "lazyCheck": 1,
                "isLog": 0,
                "opertype": 1,
            },
        )
        file_data = raw.get("file") if isinstance(raw.get("file"), dict) else raw.get("data")
        file_data = require_mapping(file_data, "天翼云盘完成上传")
        remote_id = str(file_data.get("userFileId") or file_data.get("id") or "")
        if not remote_id:
            raise RemoteProtocolError("天翼云盘完成上传未返回文件 ID")
        return RemoteEntry(
            remote_id,
            str(state["p"]),
            str(file_data.get("fileName") or state["n"]),
            False,
            int(file_data.get("fileSize") or state["s"]),
            parse_datetime(file_data.get("createDate")),
            remote_id,
        )

    def abort_upload(self, upload: MultipartUploadState) -> bool:
        _ = upload
        return True

    def delete_entry(self, entry: RemoteEntry) -> None:
        task = json.dumps([{"fileId": entry.remote_id, "fileName": entry.name, "isFolder": 1 if entry.is_dir else 0}], separators=(",", ":"))
        raw = self._post(
            "/batch/createBatchTask.action", {},
            data={"type": "DELETE", "taskInfos": task},
        )
        if not raw.get("taskId"):
            raise RemoteProtocolError(str(raw.get("errorMsg") or "天翼云盘未创建删除任务"))

    def rename_entry(self, entry: RemoteEntry, new_name: str) -> None:
        path = "/renameFolder.action" if entry.is_dir else "/renameFile.action"
        id_key = "folderId" if entry.is_dir else "fileId"
        name_key = "destFolderName" if entry.is_dir else "destFileName"
        self._post(path, {id_key: entry.remote_id, name_key: new_name})

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
        full = hashlib.md5()
        with local_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                full.update(chunk)
        state = self.begin_upload(
            parent_id=parent_id,
            file_name=file_name,
            total_size=size,
            part_size=part_size,
            provider_checksum=full.hexdigest(),
            mime_type=mime_type,
        )
        upload = MultipartUploadState(
            upload_id=state, provider_upload_id=state, provider_type="drive_189", mount_id="",
            object_key=file_name, upload_root=local_path.parent, upload_dir=local_path.parent,
            part_size_bytes=part_size, total_size=size,
        )
        parts: list[MultipartPart] = []
        with local_path.open("rb") as stream:
            for number in range(1, max(1, (size + part_size - 1) // part_size) + 1):
                chunk = stream.read(part_size)
                checksum = hashlib.md5(chunk).hexdigest()
                upload.metadata["part_checksum"] = checksum
                plan = self.part_plan(upload, number)
                response = self._http.put(plan.url, content=chunk, headers=plan.headers)
                response.raise_for_status()
                parts.append(MultipartPart(number, plan.record_etag or checksum.upper(), len(chunk)))
        return self.complete_upload(upload, parts, mime_type=mime_type)
