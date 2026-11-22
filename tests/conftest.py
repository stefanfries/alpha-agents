import socket
import threading

import httpx
import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    attempts: list[str] = []
    socketpair_state = threading.local()
    original_socketpair = socket.socketpair
    original_connect = socket.socket.connect

    def reject(operation: str):
        attempts.append(operation)
        raise RuntimeError(f"Network access is disabled during tests: {operation}")

    def socketpair(*args, **kwargs):
        # Windows implements socketpair with a temporary loopback connection.
        previous = getattr(socketpair_state, "creating", False)
        socketpair_state.creating = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            socketpair_state.creating = previous

    def connect(sock, address):
        if getattr(socketpair_state, "creating", False):
            return original_connect(sock, address)
        return reject("socket connect")

    def reject_socket(*_args, **_kwargs):
        return reject("socket operation")

    def reject_dns(*_args, **_kwargs):
        return reject("DNS lookup")

    async def reject_async_http(*_args, **_kwargs):
        return reject("httpx async request")

    def reject_http(*_args, **_kwargs):
        return reject("httpx request")

    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_socket)
    monkeypatch.setattr(socket.socket, "sendto", reject_socket)
    if hasattr(socket.socket, "sendmsg"):
        monkeypatch.setattr(socket.socket, "sendmsg", reject_socket)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        monkeypatch.setattr(socket, name, reject_dns)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_async_http)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_http)

    yield

    if attempts:
        pytest.fail(f"Unexpected network access attempted: {', '.join(attempts)}")
