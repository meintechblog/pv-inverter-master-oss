"""Fake single-session Modbus-TCP server (127.0.0.1 only) that models the SE30K.

It does not enforce the limits, it MEASURES violations so tests can assert on
them: simultaneous connections, requests in flight (received, answer not yet
sent) and the gap between one answer and the next request.
"""
from __future__ import annotations

import asyncio
import struct
import time


class FakeSingleSessionServer:
    def __init__(self, response_delay_s: float = 0.01):
        self.response_delay_s = response_delay_s
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self.connections_now = 0
        self.max_connections = 0
        self.total_connections = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.requests: list[tuple[int, int]] = []  # (function code, address)
        self.gaps: list[float] = []  # answer sent -> next request received
        self._last_answer = None
        self.store: dict[int, int] = {}
        self.units: list[tuple[int, int, int]] = []  # (function code, address, unit id)
        self.mute = False  # accept TCP, read requests, never answer (wedge signature)
        self.aborted_in_flight = 0  # requests cut off by the client closing mid-transaction

    async def start(self) -> "FakeSingleSessionServer":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections_now += 1
        self.total_connections += 1
        self.max_connections = max(self.max_connections, self.connections_now)
        tasks: list[asyncio.Task] = []
        try:
            while True:
                header = await reader.readexactly(7)
                tid, _pid, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                now = time.monotonic()
                if self._last_answer is not None:
                    self.gaps.append(now - self._last_answer)
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                tasks.append(asyncio.create_task(self._answer(writer, tid, unit, pdu)))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for t in tasks:
                if not t.done():
                    self.aborted_in_flight += 1
                t.cancel()
            self.connections_now -= 1
            writer.close()

    async def _answer(self, writer, tid: int, unit: int, pdu: bytes) -> None:
        fc = pdu[0]
        address = struct.unpack(">H", pdu[1:3])[0]
        self.requests.append((fc, address))
        self.units.append((fc, address, unit))
        if self.mute:
            await asyncio.sleep(3600)
            return
        await asyncio.sleep(self.response_delay_s)
        if fc in (3, 4):
            count = struct.unpack(">H", pdu[3:5])[0]
            regs = [self.store.get(address + i, 0) for i in range(count)]
            body = bytes([fc, count * 2]) + b"".join(struct.pack(">H", r) for r in regs)
        elif fc == 16:
            count = struct.unpack(">H", pdu[3:5])[0]
            for i in range(count):
                self.store[address + i] = struct.unpack(">H", pdu[6 + 2 * i:8 + 2 * i])[0]
            body = bytes([fc]) + pdu[1:5]
        elif fc == 6:
            self.store[address] = struct.unpack(">H", pdu[3:5])[0]
            body = pdu
        else:
            body = bytes([fc | 0x80, 1])
        writer.write(struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body)
        await writer.drain()
        self._last_answer = time.monotonic()
        self.in_flight -= 1
