"""Plugin registry and factory for inverter type dispatch."""
from __future__ import annotations

from pv_inverter_master.config import GatewayConfig, InverterEntry


def plugin_factory(
    entry: InverterEntry,
    gateway_config: GatewayConfig | None = None,
    data_dir: str | None = None,
):
    """Create the appropriate InverterPlugin for an InverterEntry.

    Args:
        entry: InverterEntry with type field ("solaredge", "opendtu",
            "shelly", "sungrow", "hoymiles", or "senec").
        gateway_config: Optional GatewayConfig for opendtu entries.
            If None for opendtu, a default is created from entry.gateway_host.
        data_dir: Optional writable directory for plugins that persist
            runtime state (SENEC integrates its own lifetime energy).

    Returns:
        An InverterPlugin instance configured for the entry.

    Raises:
        ValueError: For unknown inverter types.
    """
    if entry.type == "solaredge":
        from pv_inverter_master.plugins.solaredge import SolarEdgePlugin
        if (
            entry.dynamic_power_control
            and entry.throttle_dead_time_s > entry.dpc_command_timeout_s / 2
        ):
            import logging
            logging.getLogger(__name__).warning(
                "SolarEdge %s: throttle_dead_time_s=%.0fs exceeds half the "
                "DPC command timeout (%ds) — refresh writes may arrive too "
                "late and the inverter will flap to the fallback limit",
                entry.host, entry.throttle_dead_time_s,
                entry.dpc_command_timeout_s,
            )
        return SolarEdgePlugin(
            host=entry.host, port=entry.port, unit_id=entry.unit_id,
            dynamic_power_control=entry.dynamic_power_control,
            dpc_command_timeout_s=entry.dpc_command_timeout_s,
            dpc_fallback_pct=entry.dpc_fallback_pct,
        )
    elif entry.type == "opendtu":
        from pv_inverter_master.plugins.opendtu import OpenDTUPlugin
        if gateway_config is None:
            gateway_config = GatewayConfig(
                host=entry.gateway_host,
                user=entry.gateway_user or "admin",
                password=entry.gateway_password or "openDTU42",
            )
        return OpenDTUPlugin(
            gateway_config=gateway_config,
            serial=entry.serial,
            name=entry.name,
        )
    elif entry.type == "shelly":
        from pv_inverter_master.plugins.shelly import ShellyPlugin
        return ShellyPlugin(
            host=entry.host,
            generation=entry.shelly_gen,
            name=entry.name,
            rated_power=entry.rated_power,
            channel=entry.shelly_channel,
        )
    elif entry.type == "sungrow":
        from pv_inverter_master.plugins.sungrow import SungrowPlugin
        return SungrowPlugin(
            host=entry.host,
            port=entry.port,
            unit_id=entry.unit_id,
            rated_power=entry.rated_power,
        )
    elif entry.type == "hoymiles":
        from pv_inverter_master.plugins.hoymiles import HoymilesPlugin
        return HoymilesPlugin(
            host=entry.host,
            name=entry.name,
            serial=entry.serial,
            rated_power=entry.rated_power,
            firmware_version=entry.firmware_version,
        )
    elif entry.type == "senec":
        from pv_inverter_master.plugins.senec import SenecPlugin
        return SenecPlugin(
            host=entry.host,
            name=entry.name,
            rated_power=entry.rated_power,
            data_dir=data_dir,
            device_id=entry.id,
        )
    else:
        raise ValueError(
            f"Unknown inverter type: {entry.type} "
            "(valid: solaredge, opendtu, shelly, sungrow, hoymiles, senec)"
        )
