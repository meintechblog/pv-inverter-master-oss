"""Register-viewer routes.

Serves side-by-side SE30K source and Fronius target register values
based on the static SunSpec model layout in ``REGISTER_MODELS``.
"""
from __future__ import annotations

from aiohttp import web


# SunSpec register layout for the register viewer.
# Each model defines fields with addresses, names, and SE source mapping.
REGISTER_MODELS = [
    {
        "name": "Common (Model 1)",
        "start": 40002,
        "source": "se",
        "se_offset_key": "common_registers",
        "se_base_addr": 40002,
        "fields": [
            {"addr": 40002, "name": "DID", "size": 1},
            {"addr": 40003, "name": "Length", "size": 1},
            {"addr": 40004, "name": "Manufacturer", "size": 16, "type": "string"},
            {"addr": 40020, "name": "Model", "size": 16, "type": "string"},
            {"addr": 40036, "name": "Options", "size": 8, "type": "string"},
            {"addr": 40044, "name": "Version", "size": 8, "type": "string"},
            {"addr": 40052, "name": "Serial Number", "size": 16, "type": "string"},
            {"addr": 40068, "name": "Device Address", "size": 1},
        ],
    },
    {
        "name": "Inverter (Model 103)",
        "start": 40069,
        "source": "se",
        "se_offset_key": "inverter_registers",
        "se_base_addr": 40069,
        "fields": [
            {"addr": 40069, "name": "DID", "size": 1},
            {"addr": 40070, "name": "Length", "size": 1},
            {"addr": 40071, "name": "AC Current", "size": 1},
            {"addr": 40072, "name": "AC Current A", "size": 1},
            {"addr": 40073, "name": "AC Current B", "size": 1},
            {"addr": 40074, "name": "AC Current C", "size": 1},
            {"addr": 40075, "name": "AC Current SF", "size": 1},
            {"addr": 40076, "name": "AC Voltage AB", "size": 1},
            {"addr": 40077, "name": "AC Voltage BC", "size": 1},
            {"addr": 40078, "name": "AC Voltage CA", "size": 1},
            {"addr": 40079, "name": "AC Voltage AN", "size": 1},
            {"addr": 40080, "name": "AC Voltage BN", "size": 1},
            {"addr": 40081, "name": "AC Voltage CN", "size": 1},
            {"addr": 40082, "name": "AC Voltage SF", "size": 1},
            {"addr": 40083, "name": "AC Power", "size": 1},
            {"addr": 40084, "name": "AC Power SF", "size": 1},
            {"addr": 40085, "name": "AC Frequency", "size": 1},
            {"addr": 40086, "name": "AC Frequency SF", "size": 1},
            {"addr": 40087, "name": "AC VA", "size": 1},
            {"addr": 40088, "name": "AC VA SF", "size": 1},
            {"addr": 40089, "name": "AC VAR", "size": 1},
            {"addr": 40090, "name": "AC VAR SF", "size": 1},
            {"addr": 40091, "name": "AC PF", "size": 1},
            {"addr": 40092, "name": "AC PF SF", "size": 1},
            {"addr": 40093, "name": "AC Energy", "size": 2},
            {"addr": 40095, "name": "AC Energy SF", "size": 1},
            {"addr": 40096, "name": "DC Current", "size": 1},
            {"addr": 40097, "name": "DC Current SF", "size": 1},
            {"addr": 40098, "name": "DC Voltage", "size": 1},
            {"addr": 40099, "name": "DC Voltage SF", "size": 1},
            {"addr": 40100, "name": "DC Power", "size": 1},
            {"addr": 40101, "name": "DC Power SF", "size": 1},
            {"addr": 40102, "name": "Cab Temp", "size": 1},
            {"addr": 40103, "name": "Sink Temp", "size": 1},
            {"addr": 40104, "name": "Trans Temp", "size": 1},
            {"addr": 40105, "name": "Other Temp", "size": 1},
            {"addr": 40106, "name": "Temp SF", "size": 1},
            {"addr": 40107, "name": "Status", "size": 1},
            {"addr": 40108, "name": "Status Vendor", "size": 1},
            {"addr": 40109, "name": "Reserved", "size": 12},
        ],
    },
    {
        "name": "Nameplate (Model 120)",
        "start": 40121,
        "source": None,
        "se_offset_key": None,
        "se_base_addr": None,
        "fields": [
            {"addr": 40121, "name": "DID", "size": 1},
            {"addr": 40122, "name": "Length", "size": 1},
            {"addr": 40123, "name": "DER Type", "size": 1},
            {"addr": 40124, "name": "W Rating", "size": 1},
            {"addr": 40125, "name": "W Rating SF", "size": 1},
            {"addr": 40126, "name": "VA Rating", "size": 1},
            {"addr": 40127, "name": "VA Rating SF", "size": 1},
            {"addr": 40128, "name": "VArRtgQ1", "size": 1},
            {"addr": 40129, "name": "VArRtgQ2", "size": 1},
            {"addr": 40130, "name": "VArRtgQ3", "size": 1},
            {"addr": 40131, "name": "VArRtgQ4", "size": 1},
            {"addr": 40132, "name": "VArRtg SF", "size": 1},
            {"addr": 40133, "name": "ARtg", "size": 1},
            {"addr": 40134, "name": "ARtg SF", "size": 1},
            {"addr": 40135, "name": "PFRtgQ1", "size": 1},
            {"addr": 40136, "name": "PFRtgQ2", "size": 1},
            {"addr": 40137, "name": "PFRtgQ3", "size": 1},
            {"addr": 40138, "name": "PFRtgQ4", "size": 1},
            {"addr": 40139, "name": "PFRtg SF", "size": 1},
            {"addr": 40140, "name": "WHRtg", "size": 1},
            {"addr": 40141, "name": "WHRtg SF", "size": 1},
            {"addr": 40142, "name": "AhrRtg", "size": 1},
            {"addr": 40143, "name": "AhrRtg SF", "size": 1},
            {"addr": 40144, "name": "MaxChaRte", "size": 1},
            {"addr": 40145, "name": "MaxChaRte SF", "size": 1},
            {"addr": 40146, "name": "MaxDisChaRte", "size": 1},
            {"addr": 40147, "name": "MaxDisChaRte SF", "size": 1},
            {"addr": 40148, "name": "Pad", "size": 1},
        ],
    },
    {
        "name": "Controls (Model 123)",
        "start": 40149,
        "source": None,
        "se_offset_key": None,
        "se_base_addr": None,
        "fields": [
            {"addr": 40149, "name": "DID", "size": 1},
            {"addr": 40150, "name": "Length", "size": 1},
            {"addr": 40151, "name": "Conn_WinTms", "size": 1},
            {"addr": 40152, "name": "Conn_RvrtTms", "size": 1},
            {"addr": 40153, "name": "Conn", "size": 1},
            {"addr": 40154, "name": "WMaxLimPct", "size": 1},
            {"addr": 40155, "name": "WMaxLimPct_WinTms", "size": 1},
            {"addr": 40156, "name": "WMaxLimPct_RvrtTms", "size": 1},
            {"addr": 40157, "name": "WMaxLimPct_RmpTms", "size": 1},
            {"addr": 40158, "name": "WMaxLim_Ena", "size": 1},
            {"addr": 40159, "name": "OutPFSet", "size": 1},
            {"addr": 40160, "name": "Remaining", "size": 15},
        ],
    },
]


def _decode_register_value(regs: list[int], field: dict) -> object:
    """Decode register value(s) based on field type."""
    if field.get("type") == "string":
        # Decode uint16 list to ASCII string
        chars = []
        for val in regs:
            chars.append(chr((val >> 8) & 0xFF))
            chars.append(chr(val & 0xFF))
        return "".join(chars).rstrip("\x00")
    elif field["size"] == 1:
        return regs[0] if regs else 0
    else:
        return list(regs)

async def device_registers_handler(request: web.Request) -> web.Response:
    """Return side-by-side register data for a specific device."""
    device_id = request.match_info["id"]
    app_ctx = request.app["app_ctx"]
    cache = app_ctx.cache
    ds = app_ctx.devices.get(device_id)
    if ds is None:
        return web.json_response({"error": "Device not found"}, status=404)
    last_se_poll = ds.last_poll_data
    return _build_register_response(cache, last_se_poll)


async def registers_handler(request: web.Request) -> web.Response:
    """Return side-by-side register data: SE source + Fronius target per field."""
    app_ctx = request.app["app_ctx"]
    cache = app_ctx.cache
    # Legacy endpoint: use first device's data for backward compatibility
    first_dev = next(iter(app_ctx.devices.values()), None)
    last_se_poll = first_dev.last_poll_data if first_dev else None
    return _build_register_response(cache, last_se_poll)


def _build_register_response(cache, last_se_poll) -> web.Response:
    """Build register JSON response from cache and poll data."""

    models_out = []
    for model in REGISTER_MODELS:
        fields_out = []
        for field in model["fields"]:
            # Fronius target value from cache datablock
            # pymodbus datablock uses address+1 internally
            try:
                fronius_regs = cache.datablock.getValues(
                    field["addr"] + 1, field["size"],
                )
                fronius_value = _decode_register_value(fronius_regs, field)
            except Exception:
                fronius_value = None

            # SE30K source value
            se_value = None
            if (
                model["source"] == "se"
                and last_se_poll is not None
                and model["se_offset_key"] in last_se_poll
            ):
                se_data = last_se_poll[model["se_offset_key"]]
                offset = field["addr"] - model["se_base_addr"]
                end = offset + field["size"]
                if end <= len(se_data):
                    se_regs = se_data[offset:end]
                    se_value = _decode_register_value(se_regs, field)

            fields_out.append({
                "addr": field["addr"],
                "name": field["name"],
                "se_value": se_value,
                "fronius_value": fronius_value,
            })

        models_out.append({
            "name": model["name"],
            "fields": fields_out,
        })

    return web.json_response(models_out)

