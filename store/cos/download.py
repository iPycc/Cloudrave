"""Download-only routing, independent of the COS management endpoint."""

from urllib.parse import urlsplit


def download_settings(extra: dict) -> tuple[str, str, str]:
    mode = extra.get("download_domain_type", "default")
    auth = extra.get("download_cdn_auth", "cos")
    if not isinstance(mode, str) or mode not in {"default", "origin", "cdn"}:
        raise ValueError("下载域名类型必须是 default、origin 或 cdn")
    if not isinstance(auth, str) or auth not in {"cos", "public"}:
        raise ValueError("CDN 访问方式必须是 cos 或 public")
    if mode == "default":
        return mode, "", auth
    raw = str(extra.get("download_domain") or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw if "://" in raw else "https://" + raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("下载域名格式无效") from exc
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment or port is not None
            or any(char.isspace() for char in raw)
            or any(char in raw for char in "\\?#")):
        raise ValueError("下载域名仅支持 HTTP/HTTPS 域名，不能包含路径、端口或参数")
    try:
        host = parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("下载域名格式无效") from exc
    if not all(part and all(c.isalnum() or c == "-" for c in part) for part in host.split(".")):
        raise ValueError("下载域名格式无效")
    encrypted = str(extra.get("server_side_encryption", "none")).strip().lower() not in {"", "none"}
    if parsed.scheme != "https" and (encrypted or not (mode == "cdn" and auth == "public")):
        raise ValueError("签名下载域名必须使用 HTTPS")
    return mode, f"{parsed.scheme}://{host}", auth
