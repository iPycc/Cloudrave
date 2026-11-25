"""HTTP probes that connect only to public addresses, retaining TLS host validation."""

import asyncio
import socket
import ssl
from ipaddress import ip_address

import httpcore
import httpx


class _PublicNetworkBackend(httpcore.AnyIOBackend):
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        async with asyncio.timeout(timeout):
            try:
                addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise httpcore.ConnectError("Storage endpoint DNS lookup failed") from exc
            if not addresses or any(not ip_address(item[4][0]).is_global for item in addresses):
                raise httpcore.ConnectError("Storage probe requires a public endpoint")
            # Connect to the checked IP so a second DNS lookup cannot change the destination.
            attempt_timeout = timeout / len(addresses) if timeout is not None else None
            for index, address in enumerate(addresses):
                try:
                    return await super().connect_tcp(address[4][0], port, attempt_timeout, local_address, socket_options)
                except (httpcore.ConnectError, httpcore.ConnectTimeout):
                    if index == len(addresses) - 1:
                        raise


class PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=_PublicNetworkBackend(),
        )
