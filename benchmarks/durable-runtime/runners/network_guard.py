"""Fail-closed external-network guard for offline benchmark workers."""

from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import socket
from typing import Iterator

from psycopg.conninfo import conninfo_to_dict


class ExternalNetworkAccessDenied(RuntimeError):
    """An offline benchmark attempted a non-loopback network operation."""


def validate_local_postgres_dsn(dsn: str) -> None:
    parameters = conninfo_to_dict(dsn)
    host = parameters.get("host")
    hostaddr = parameters.get("hostaddr")
    for raw_value in (host, hostaddr):
        if raw_value is None or not raw_value.strip():
            continue
        for value in raw_value.split(","):
            candidate = value.strip()
            if candidate.startswith("/"):
                continue
            if candidate == "localhost" or _is_loopback(candidate):
                continue
            raise ExternalNetworkAccessDenied(
                "offline benchmark PostgreSQL must use a Unix socket or loopback"
            )


@contextmanager
def external_network_guard() -> Iterator[None]:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo

    def guarded_connect(sock: socket.socket, address: object) -> object:
        _require_allowed_address(sock.family, address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: object) -> int:
        _require_allowed_address(sock.family, address)
        return original_connect_ex(sock, address)

    def guarded_create_connection(
        address: tuple[str, int],
        *args: object,
        **kwargs: object,
    ) -> socket.socket:
        _require_allowed_host(address[0])
        return original_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(
        host: str | bytes | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[object, ...]]:
        if host is not None:
            decoded = host.decode() if isinstance(host, bytes) else host
            _require_allowed_host(decoded)
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create_connection
    socket.getaddrinfo = guarded_getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo


def _require_allowed_address(family: int, address: object) -> None:
    if family == socket.AF_UNIX:
        return
    if not isinstance(address, tuple) or not address:
        raise ExternalNetworkAccessDenied("unsupported offline network address")
    _require_allowed_host(str(address[0]))


def _require_allowed_host(host: str) -> None:
    if host == "localhost" or _is_loopback(host):
        return
    raise ExternalNetworkAccessDenied(
        f"offline benchmark blocked external network host: {host}"
    )


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
