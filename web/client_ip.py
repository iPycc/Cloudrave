"""Resolve the original client address for requests received through proxies."""

from __future__ import annotations

from ipaddress import ip_address

from fastapi import Request


_DIRECT_CLIENT_HEADERS = (
    "cf-connecting-ip",
    "true-client-ip",
    "x-real-ip",
)


def get_client_ip(request: Request) -> str | None:
    """Return the first valid visitor IP supplied by a proxy, then the peer IP."""
    for header_name in _DIRECT_CLIENT_HEADERS:
        candidate = _normalize_ip(request.headers.get(header_name))
        if candidate is not None:
            return candidate

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        for value in forwarded_for.split(","):
            candidate = _normalize_ip(value)
            if candidate is not None:
                return candidate

    if request.client is None:
        return None
    return _normalize_ip(request.client.host) or request.client.host


def _normalize_ip(value: str | None) -> str | None:
    if value is None:
        return None

    candidate = value.strip().strip('"')
    if not candidate:
        return None

    # Some proxies include an optional port even though the de-facto headers
    # normally contain only an address. Bracketed IPv6 is unambiguous here.
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    else:
        try:
            return str(ip_address(candidate))
        except ValueError:
            host, separator, port = candidate.rpartition(":")
            if not separator or not port.isdigit():
                return None
            candidate = host

    try:
        return str(ip_address(candidate))
    except ValueError:
        return None


__all__ = ["get_client_ip"]
