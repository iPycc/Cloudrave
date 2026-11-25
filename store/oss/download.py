"""OSS download routing; management and upload endpoints stay independent."""

import ipaddress
import re
from urllib.parse import urlsplit


def domain_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    parsed = urlsplit(raw if "://" in raw else "https://" + raw)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query
            or parsed.fragment or parsed.port is not None
            or any(c.isspace() for c in raw) or any(c in raw for c in "\\?#")):
        raise ValueError("下载域名仅支持 HTTPS 域名，不能包含路径、端口或参数")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if len(host) > 253 or "." not in host or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in host.split(".")
    ):
        raise ValueError("下载域名格式无效")
    if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", host.rsplit(".", 1)[-1]):
        raise ValueError("下载域名不能使用 IP 地址")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f"{parsed.scheme}://{host}"
    raise ValueError("下载域名不能使用 IP 地址")


def download_settings(extra: dict) -> tuple[str, str]:
    mode = extra.get("download_domain_type", "default")
    if mode not in {"default", "origin"}:
        raise ValueError("OSS 下载域名类型必须是 default 或 origin")
    return mode, domain_url(str(extra.get("download_domain") or "")) if mode == "origin" else ""
