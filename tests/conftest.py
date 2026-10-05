"""Fixtures for the Home Assistant tests. No plugin imports here: test_engine runs with plain pytest."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

NODE_A = "02:57:49:53:50:01"
NODE_B = "02:57:49:53:50:02"
AP = "a8:29:48:db:b6:70"
IP_A = "192.168.1.50"
IP_B = "192.168.1.51"


class FakeTransport(asyncio.DatagramTransport):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[bytes, Any]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: Any = None) -> None:
        self.sent.append((data, addr))

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return ("0.0.0.0", 40000) if name == "sockname" else default


class FakeUdp:
    """Stands in for the hub's socket: records what it sends; tests deliver datagrams by hand."""

    def __init__(self) -> None:
        self.transport: FakeTransport | None = None
        self.protocol: asyncio.DatagramProtocol | None = None

    async def create_datagram_endpoint(self, factory, local_addr=None, **kwargs):
        self.transport, self.protocol = FakeTransport(), factory()
        self.protocol.connection_made(self.transport)
        return self.transport, self.protocol

    def receive(self, data: bytes, ip: str = IP_A) -> None:
        self.protocol.datagram_received(data, (ip, 47010))

    def sent_to(self) -> list[Any]:
        return [addr for _, addr in self.transport.sent]

    def clear(self) -> None:
        self.transport.sent.clear()


class FakeClock:
    """The hub's clock (seconds). Tests move it by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def udp(hass) -> FakeUdp:
    fake = FakeUdp()
    with patch.object(hass.loop, "create_datagram_endpoint", fake.create_datagram_endpoint):
        yield fake
