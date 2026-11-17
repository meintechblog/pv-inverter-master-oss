"""mDNS discovery for Shelly devices on the LAN and HTTP probe.

Scans for _shelly._tcp.local. services using zeroconf AsyncZeroconf.
Also provides probe_shelly_device() to query a single Shelly's /shelly endpoint.
Manual-only -- triggered via REST endpoint, not at startup.
"""
from __future__ import annotations

import asyncio
import socket

import aiohttp
import structlog
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

log = structlog.get_logger(component="shelly_discovery")

SHELLY_SERVICE_TYPE = "_shelly._tcp.local."


async def discover_shelly_devices(
    timeout: float = 3.0,
    skip: set[tuple[str, int]] | None = None,
    subnet_override: str | None = None,
) -> list[dict]:
    """Scan for Shelly devices.

    Without ``subnet_override`` this browses ``_shelly._tcp.local.`` over
    mDNS — fast, but link-local only. With a ``subnet_override`` it falls
    back to an IP sweep instead, because mDNS multicast does not cross a
    subnet / VPN boundary; see :func:`_discover_shelly_subnet`.

    Args:
        timeout: mDNS scan duration in seconds.
        skip: ``(host, channel)`` pairs of already-configured device
            entries — the matching channel is kept in the results but
            flagged ``already_added`` so the UI greys it out.
        subnet_override: CIDR / "x"-shorthand / single IP. When set, the
            subnet is IP-swept instead of browsed via mDNS.

    Returns:
        List of device dicts. Each carries a non-empty ``channels`` list
        (one entry per switch/relay, fields: id, name, power_w, output,
        already_added) plus host, name, generation, model, switch_name.
    """
    if subnet_override:
        return await _discover_shelly_subnet(subnet_override, skip)

    found_names: set[str] = set()

    def on_state_change(zeroconf, service_type, name, state_change):
        # mDNS re-announces a service repeatedly during a browse window —
        # a set keeps each physical device to a single discovery entry.
        if state_change == ServiceStateChange.Added:
            found_names.add(name)

    aiozc = AsyncZeroconf()
    try:
        browser = AsyncServiceBrowser(
            aiozc.zeroconf, SHELLY_SERVICE_TYPE, handlers=[on_state_change]
        )
        await asyncio.sleep(timeout)
        await browser.async_cancel()

        results = []
        seen_ips: set[str] = set()
        for name in found_names:
            info = AsyncServiceInfo(SHELLY_SERVICE_TYPE, name)
            await info.async_request(aiozc.zeroconf, timeout=1000)

            # Extract IP from addresses or fallback to server
            if info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
            elif info.server:
                ip = info.server.rstrip(".")
            else:
                continue

            # A device may advertise under more than one service name —
            # collapse to one entry per IP.
            if ip in seen_ips:
                continue
            seen_ips.add(ip)

            # Parse TXT record properties
            props = info.properties or {}
            gen = _decode_txt(props.get(b"gen", b"1"))
            app = _decode_txt(props.get(b"app", b""))
            ver = _decode_txt(props.get(b"ver", b""))

            results.append({
                "host": ip,
                "name": name.replace(f".{SHELLY_SERVICE_TYPE}", ""),
                "generation": f"gen{gen}",
                "model": app,
                "firmware": ver,
                "switch_name": None,
                "channels": [],
                "already_added": False,
            })

        # Enrich with device name + per-channel breakdown (Gen2+ only)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3.0)
        ) as session:
            for dev in results:
                gen_num = int(dev["generation"].replace("gen", ""))
                if gen_num >= 2:
                    try:
                        # Device name first (/shelly → "name" field)
                        async with session.get(f"http://{dev['host']}/shelly") as resp:
                            shelly_data = await resp.json()
                            device_name = shelly_data.get("name") or None

                        channels = await _probe_gen2_channels(session, dev["host"])
                        dev["channels"] = channels
                        # Device-level label: single-channel keeps its switch
                        # name; multi-channel falls back to the device name.
                        if len(channels) == 1 and channels[0].get("name"):
                            dev["switch_name"] = channels[0]["name"]
                        else:
                            dev["switch_name"] = device_name
                    except Exception:
                        pass

        _finalize_channels(results, skip)
        log.info("shelly_mdns_scan_complete", devices_found=len(results), timeout=timeout)
        return results
    finally:
        await aiozc.async_close()


def _finalize_channels(
    results: list[dict], skip: set[tuple[str, int]] | None
) -> None:
    """Normalise every device to a non-empty per-channel list, in place.

    Gen1 / single-relay devices arrive with no enumerated channels — a
    synthetic channel 0 is added so the UI can treat every device the
    same. Each channel is flagged ``already_added`` for its own
    ``(host, channel)`` pair, and the device level reflects "all added".
    """
    skip = skip or set()
    for dev in results:
        channels = dev.get("channels") or []
        if not channels:
            channels = [{"id": 0, "name": None, "power_w": 0.0, "output": False}]
        for ch in channels:
            ch["already_added"] = (dev["host"], ch.get("id", 0)) in skip
        dev["channels"] = channels
        dev["already_added"] = all(ch["already_added"] for ch in channels)


async def _discover_shelly_subnet(
    subnet_override: str,
    skip: set[tuple[str, int]] | None = None,
    tcp_timeout: float = 0.6,
    probe_timeout: float = 4.0,
    concurrency: int = 50,
) -> list[dict]:
    """IP-sweep a (possibly VPN-reachable) subnet for Shelly devices.

    mDNS multicast does not cross a subnet / VPN boundary, so a remote
    network is scanned by TCP-probing port 80 and confirming each open
    host via its ``/shelly`` endpoint. ``subnet_override`` accepts CIDR /
    "x"-shorthand / single-IP just like the SunSpec scanner.
    """
    from pv_inverter_master.scanner import _probe_port, parse_subnet_override

    subnet = parse_subnet_override(subnet_override)
    hosts = [str(ip) for ip in subnet.hosts()]
    sem = asyncio.Semaphore(concurrency)

    async def _sweep(ip: str) -> tuple[str, bool]:
        async with sem:
            return ip, await _probe_port(ip, 80, tcp_timeout)

    swept = await asyncio.gather(*(_sweep(ip) for ip in hosts))
    open_hosts = [ip for ip, is_open in swept if is_open]
    log.info(
        "shelly_subnet_sweep",
        subnet=str(subnet),
        scanned=len(hosts),
        open_80=len(open_hosts),
    )

    # Confirm each open-80 host is actually a Shelly via /shelly. Probes run
    # concurrently (bounded) so a subnet full of web devices stays quick.
    psem = asyncio.Semaphore(20)

    async def _probe(ip: str) -> tuple[str, dict]:
        async with psem:
            return ip, await probe_shelly_device(ip, timeout=probe_timeout)

    probed = await asyncio.gather(*(_probe(ip) for ip in open_hosts))

    results: list[dict] = []
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=probe_timeout)
    ) as session:
        for ip, probe in probed:
            if not probe.get("success"):
                continue
            gen = probe.get("generation", "gen1")
            dev = {
                "host": ip,
                "name": probe.get("switch_name") or probe.get("model") or "Shelly",
                "generation": gen,
                "model": probe.get("model", ""),
                "firmware": "",
                "switch_name": probe.get("switch_name"),
                "channels": [],
                "already_added": False,
            }
            if gen in ("gen2", "gen3"):
                try:
                    channels = await _probe_gen2_channels(session, ip)
                    dev["channels"] = channels
                    if len(channels) == 1 and channels[0].get("name"):
                        dev["switch_name"] = channels[0]["name"]
                except Exception:
                    pass
            results.append(dev)

    _finalize_channels(results, skip)
    log.info("shelly_subnet_scan_complete", devices_found=len(results))
    return results


async def _probe_gen2_channels(
    session: aiohttp.ClientSession, host: str
) -> list[dict]:
    """Enumerate the switch channels of a Gen2+ Shelly.

    A multi-channel Shelly (e.g. Pro 2PM/4PM) exposes one ``switch:N``
    component per relay. Returns one dict per channel with its configured
    name, live power and on/off state, so discovery can show the device's
    channels transparently instead of a single ambiguous entry.
    """
    async with session.get(f"http://{host}/rpc/Shelly.GetStatus") as resp:
        status = await resp.json()

    config: dict = {}
    try:
        async with session.get(f"http://{host}/rpc/Shelly.GetConfig") as resp:
            config = await resp.json()
    except Exception:
        pass

    channels: list[dict] = []
    for key in sorted(status):
        if not key.startswith("switch:"):
            continue
        try:
            idx = int(key.split(":", 1)[1])
        except ValueError:
            continue
        sw = status.get(key) or {}
        cfg = config.get(key) or {}
        channels.append({
            "id": idx,
            "name": cfg.get("name") or None,
            "power_w": round(float(sw.get("apower") or 0.0), 1),
            "output": bool(sw.get("output", False)),
        })
    return channels


async def probe_shelly_device(host: str, timeout: float = 5.0) -> dict:
    """Probe a single Shelly device by querying its /shelly HTTP endpoint.

    Args:
        host: IP address or hostname of the Shelly device.
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict with success, generation, model, mac, gen_display on success.
        Dict with success=False and error message on failure.
    """
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(f"http://{host}/shelly") as resp:
                data = await resp.json()

        gen_value = data.get("gen", 0)
        if gen_value >= 3:
            generation = "gen3"
            gen_display = "Gen3"
        elif gen_value >= 2:
            generation = "gen2"
            gen_display = "Gen2"
        else:
            generation = "gen1"
            gen_display = "Gen1"

        # Gen2+ uses "app", Gen1 uses "type"
        model = data.get("app", data.get("type", "Unknown"))
        mac = data.get("mac", "")

        # Try to fetch user-configured name (Gen2+): device name, then switch name
        device_name = data.get("name") or None  # /shelly already has device name
        switch_name = None
        if gen_value >= 2:
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as session:
                    async with session.get(f"http://{host}/rpc/Switch.GetConfig?id=0") as sw_resp:
                        sw_data = await sw_resp.json()
                        switch_name = sw_data.get("name") or None
            except Exception:
                pass
        # Prefer switch name, fall back to device name
        switch_name = switch_name or device_name

        return {
            "success": True,
            "generation": generation,
            "model": model,
            "mac": mac,
            "gen_display": gen_display,
            "switch_name": switch_name,
        }
    except Exception as e:
        return {"success": False, "error": f"Could not reach Shelly at {host}: {e}"}


def _decode_txt(value: bytes | str) -> str:
    """Decode a TXT record value from bytes to str."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
