"""Monitoring subpackage for pv-inverter-master.

Contains the deterministic hourly anomaly detector (``anomaly_detector``)
that gates on solar elevation, classifies dead/stale/hung inverters, and
auto-fixes the known Sungrow Modbus-hang case via a reboot POST. Escalation
webhooks and push notifications are gated behind feature flags and sent to
a configurable webhook base URL (``PV_ANOMALY_WEBHOOK_BASE_URL``), using
``/api/escalate`` and ``/api/push``. Enable them with
``PV_ANOMALY_ESCALATION`` and ``PV_ANOMALY_PUSH`` respectively.
"""
