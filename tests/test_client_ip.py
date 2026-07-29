from starlette.requests import Request

from web.client_ip import get_client_ip


def _request(*, headers: dict[str, str] | None = None, client: tuple[str, int] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in (headers or {}).items()
            ],
            "client": client,
        }
    )


def test_prefers_proxy_client_header_over_forwarded_chain() -> None:
    request = _request(
        headers={
            "CF-Connecting-IP": "203.0.113.8",
            "X-Real-IP": "198.51.100.7",
            "X-Forwarded-For": "192.0.2.6, 10.0.0.2",
        },
        client=("10.0.0.3", 443),
    )

    assert get_client_ip(request) == "203.0.113.8"


def test_uses_original_address_from_forwarded_chain() -> None:
    request = _request(
        headers={"X-Forwarded-For": "198.51.100.24, 10.0.0.2"},
        client=("10.0.0.3", 443),
    )

    assert get_client_ip(request) == "198.51.100.24"


def test_ignores_invalid_proxy_values_and_falls_back_to_peer() -> None:
    request = _request(
        headers={"X-Real-IP": "unknown", "X-Forwarded-For": "invalid"},
        client=("192.0.2.12", 443),
    )

    assert get_client_ip(request) == "192.0.2.12"


def test_normalizes_bracketed_ipv6_with_port() -> None:
    request = _request(
        headers={"X-Real-IP": "[2001:db8::1]:8443"},
        client=("10.0.0.3", 443),
    )

    assert get_client_ip(request) == "2001:db8::1"


def test_returns_none_without_proxy_or_peer_address() -> None:
    assert get_client_ip(_request()) is None
