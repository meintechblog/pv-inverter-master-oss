"""Single-shot Modbus write probe — Plan 45-05 Task 5 step 8.

Attempts a single write to register 40154 (Model 123 WMaxLimPct) and
prints the result: OK, DEVICE_BUSY (0x06), or an error. Used to verify
the maintenance-mode gate on the live LXC.

NOTE (single-session inverters): this script talks to the PROXY endpoint
(register cache / control path), never to the inverter itself. Do NOT point it
at an SE30K host: the SE30K accepts one Modbus session and a second client
wedges it. Direct inverter access belongs to scripts/validate_se30k.py, which
refuses to run next to the service.

USAGE:

    # During a normal (non-maintenance) window — expected: OK
    .venv/bin/python scripts/modbus_write_probe.py --host 192.0.2.191

    # During maintenance mode (run in a tight loop while POST /api/update/start
    # is being handled) — expected: DEVICE_BUSY
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from pymodbus.client import AsyncModbusTcpClient


async def main(host: str, port: int, device_id: int, value: int) -> int:
    client = AsyncModbusTcpClient(host=host, port=port, timeout=15.0, retries=1)
    ok = await client.connect()
    if not ok:
        print("FAIL: could not connect", flush=True)
        return 2
    try:
        response = await client.write_register(
            address=40154, value=value, device_id=device_id,
        )
        if response.isError():
            exc_code = getattr(response, "exception_code", None)
            print(f"EXCEPTION exception_code={exc_code}", flush=True)
            if exc_code == 6:
                print("  -> DEVICE_BUSY (maintenance mode active)", flush=True)
            return 1
        print("OK write accepted", flush=True)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.0.2.191")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--device-id", type=int, default=126)
    parser.add_argument("--value", type=int, default=85)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.host, args.port, args.device_id, args.value)))
