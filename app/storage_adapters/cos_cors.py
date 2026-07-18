"""Non-destructive COS CORS rule management."""

from __future__ import annotations

from typing import Any


RULE_ID = "cloudrave-direct-transfer"


def configured_origins(policy: Any) -> list[str]:
    extra = dict(getattr(policy, "extra", None) or {})
    configured = extra.get("cors_origins")
    if isinstance(configured, str):
        configured = [configured]
    origins = [str(value).strip().rstrip("/") for value in (configured or []) if str(value).strip()]
    if not origins:
        raise ValueError("Configure at least one trusted cors_origins value before applying COS CORS")
    return origins


def merge_rule(client: Any, bucket: str, rule: dict[str, Any]) -> int:
    existing: list[dict[str, Any]] = []
    try:
        response = client.get_bucket_cors(Bucket=bucket)
        raw = response.get("CORSRule", [])
        existing = [raw] if isinstance(raw, dict) else list(raw or [])
    except Exception as exc:
        code_getter = getattr(exc, "get_error_code", None)
        code = code_getter() if callable(code_getter) else None
        if code not in {"NoSuchCORSConfiguration", "NoSuchCORS"}:
            raise
    rules = [item for item in existing if str(item.get("ID", "")) != RULE_ID]
    rules.append(rule)
    client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRule": rules})
    return len(rules)
