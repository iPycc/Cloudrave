"""Safe OSS upload diagnostics and retries for read-only provider operations."""

from __future__ import annotations

import asyncio
import re

from alibabacloud_oss_v2.exceptions import BaseError as OssSdkError, CredentialsBaseError
from fastapi import HTTPException
from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout

from drives.ids import ALIYUN_OSS
from store.call import call_adapter
from store.errors import storage_error_code, unwrap_storage_error


_PHASES = {
    "presign_part": "签发上传地址",
    "presign_put": "签发上传地址",
    "create_multipart_upload": "创建分片会话",
    "list_parts": "查询分片",
    "upload_part": "上传分片",
    "complete_multipart_upload": "合并分片",
    "head_object": "核对已上传对象",
    "validate_completed_object": "校验已上传对象",
    "abort_multipart_upload": "清理分片",
    "delete_object": "清理上传对象",
}
_RETRY_READS = {"presign_part", "presign_put", "list_parts", "head_object", "validate_completed_object"}
_RECOVER_MISSING = {"list_parts", "complete_multipart_upload", "abort_multipart_upload"}
_TIMEOUT_ERRORS = (TimeoutError, RequestsTimeout)
_CONNECTION_ERRORS = (ConnectionError, RequestsConnectionError)
_CONFIGURATION_CODES = {"AccessDenied", "InvalidAccessKeyId", "NoSuchBucket", "InvalidBucketName", "RequestTimeTooSkewed"}


async def call_upload_storage(adapter, method: str, /, *args, **kwargs):
    if adapter.provider_type != ALIYUN_OSS:
        return await call_adapter(adapter, method, *args, **kwargs)

    for attempt in range(2):
        try:
            return await call_adapter(adapter, method, *args, **kwargs)
        except Exception as exc:
            cause = unwrap_storage_error(exc)
            code = storage_error_code(cause)
            status = getattr(cause, "status_code", 0)
            if code == "NoSuchUpload" and method in _RECOVER_MISSING:
                raise
            if method == "head_object" and (
                isinstance(cause, FileNotFoundError) or (status == 404 and code in {"", "NoSuchKey", "NotFound"})
                or code == "NoSuchKey"
            ):
                raise
            if not (code or status or isinstance(cause, (OssSdkError, *_TIMEOUT_ERRORS, *_CONNECTION_ERRORS))):
                raise
            response_status, message, retryable = _diagnostic(cause, code, status)
            if retryable and method in _RETRY_READS and attempt == 0:
                await asyncio.sleep(0.25)
                continue
            detail = f"阿里云 OSS {_PHASES[method]}失败。{message}"
            provider_code = _identifier(code)
            provider_request_id = _identifier(getattr(cause, "request_id", ""))
            if provider_code:
                detail += f" 上游错误码 {provider_code}。"
            if provider_request_id:
                detail += f" OSS 请求编号 {provider_request_id}。"
            headers = None if retryable else {"X-Upload-Retryable": "false"}
            raise HTTPException(status_code=response_status, detail=detail, headers=headers) from None


def _diagnostic(cause, code: str, status: int) -> tuple[int, str, bool]:
    if code == "SignatureDoesNotMatch":
        return 502, "请检查 Bucket 地域、Endpoint 和 AccessKey。", False
    if code in {"SecurityTokenExpired", "InvalidSecurityToken", "ExpiredToken"}:
        return 422, "请更新 STS 临时凭据后重试。", False
    if code in _CONFIGURATION_CODES or isinstance(cause, CredentialsBaseError):
        return 422, "请核对存储桶、凭据权限和服务器时间。", False
    if code in {"InvalidPart", "InvalidPartOrder", "EntityTooSmall"}:
        return 409, "请继续上传任务以重新查询和补传分片。", False
    if code == "NoSuchUpload":
        return 410, "分片会话已失效，请重新上传文件。", False
    if code in {"FileAlreadyExists", "PreconditionFailed"}:
        return 409, "目标对象已存在，请更换文件名。", False
    if code in {"SlowDown", "TooManyRequests"} or status == 429:
        return 429, "OSS 请求过多，请稍后重试。", True
    if code == "RequestTimeout" or status in {408, 504} or isinstance(cause, _TIMEOUT_ERRORS):
        return 504, "OSS 响应超时，请稍后继续上传任务。", True
    retryable = (
        status in {500, 502, 503} or code in {"InternalError", "ServiceUnavailable"}
        or isinstance(cause, _CONNECTION_ERRORS)
    )
    if retryable:
        return 503, "OSS 暂不可用，请稍后重试。", True
    return 502, "请检查存储配置或根据请求编号查看日志。", False


def _identifier(value: object) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", text) else ""
