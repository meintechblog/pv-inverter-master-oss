"""Connection management with exponential backoff and night mode.

Handles SE30K connection lifecycle: reconnection on failure with exponential
backoff (5s to 60s), and night mode transition after 5 minutes of continuous
failure. Night mode serves synthetic zero-power registers with SLEEPING status
instead of Modbus errors, since the inverter is expected to be offline at night.
"""
from __future__ import annotations

import enum
import time

from pv_inverter_master.sunspec_models import INVERTER_DID, INVERTER_LENGTH


class ConnectionState(enum.Enum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    NIGHT_MODE = "night_mode"
    QUIESCE = "quiesce"


class ConnectionManager:
    """Manages reconnection backoff and night mode transitions.

    State machine:
      CONNECTED -> RECONNECTING (first poll failure)
      RECONNECTING -> NIGHT_MODE (>5 min continuous failure)
      RECONNECTING -> CONNECTED (poll success)
      NIGHT_MODE -> CONNECTED (poll success)

    Backoff: starts at INITIAL_BACKOFF (5s), doubles each failure, caps at MAX_BACKOFF (60s).
    Resets to INITIAL_BACKOFF on any successful poll.
    """

    INITIAL_BACKOFF = 5.0
    MAX_BACKOFF = 60.0
    NIGHT_MODE_THRESHOLD = 300.0  # 5 minutes

    CONSECUTIVE_FAILURES_BEFORE_RECONNECTING = 3

    # Wedge quiesce (Modbus-TCP inverters whose SunSpec server can wedge:
    # TCP accepts, every Modbus request times out — observed twice on the
    # SE30K 2026-06-10/11). The wedge releases after ~5-7 min of TRUE
    # zero-client silence (live-verified 2026-06-11), so after enough
    # consecutive failures we stop ALL traffic (polls, writes, reconnects)
    # for QUIESCE_DURATION instead of hammering reconnects for hours.
    QUIESCE_AFTER_CONSECUTIVE_FAILURES = 8
    QUIESCE_DURATION = 480.0  # 8 minutes
    # After a window expires the device gets a short probe burst (quick
    # retries ~1s apart) before the next full window is armed — a single
    # transient first-request timeout must not cost another 8 minutes.
    QUIESCE_PROBE_ATTEMPTS = 3

    def __init__(self, poll_interval: float = 1.0, wedge_quiesce: bool = False):
        self._poll_interval = poll_interval
        self._backoff = self.INITIAL_BACKOFF
        self._first_failure_time: float | None = None
        self._state = ConnectionState.CONNECTED
        self._reconnected_from_night = False
        self._consecutive_failures: int = 0
        self._wedge_quiesce = wedge_quiesce
        self._quiesce_until: float | None = None
        self._quiesce_entered = False
        self._post_window_fails = 0

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def reconnected_from_night(self) -> bool:
        """True if last state change was NIGHT_MODE -> CONNECTED. Reset after read."""
        val = self._reconnected_from_night
        self._reconnected_from_night = False
        return val

    @property
    def quiesce_entered(self) -> bool:
        """True if the last failure newly entered/re-armed QUIESCE. Reset after read.

        One-shot signal for the poll loop to close the plugin connection so
        the device sees TRUE zero-client silence during the quiesce window.
        """
        val = self._quiesce_entered
        self._quiesce_entered = False
        return val

    @property
    def sleep_duration(self) -> float:
        if self._state == ConnectionState.QUIESCE and self._quiesce_until is not None:
            return max(self._quiesce_until - time.monotonic(), 1.0)
        if self._state == ConnectionState.CONNECTED:
            return self._poll_interval
        return self._backoff

    @property
    def quiesce_remaining(self) -> float:
        """Seconds left in the current QUIESCE window (0 when none / expired)."""
        if self._state != ConnectionState.QUIESCE or self._quiesce_until is None:
            return 0.0
        return max(self._quiesce_until - time.monotonic(), 0.0)

    def on_poll_success(self) -> ConnectionState:
        """Call after a successful poll. Returns the new state."""
        prev = self._state
        self._backoff = self.INITIAL_BACKOFF
        self._first_failure_time = None
        self._consecutive_failures = 0
        self._quiesce_until = None
        self._post_window_fails = 0
        self._state = ConnectionState.CONNECTED
        if prev == ConnectionState.NIGHT_MODE:
            self._reconnected_from_night = True
        return self._state

    def on_poll_failure(self, now: float | None = None) -> ConnectionState:
        """Call after a failed poll. Returns the new state.

        Args:
            now: Current monotonic time (injectable for testing). Defaults to time.monotonic().
        """
        if now is None:
            now = time.monotonic()

        if self._first_failure_time is None:
            self._first_failure_time = now

        elapsed = now - self._first_failure_time

        self._consecutive_failures += 1

        # Wedge quiesce takes priority over night mode for opted-in device
        # types: enter on threshold, re-arm after an expired window's probe
        # poll fails again. While a window is still running (poll loop woke
        # early / spurious extra failure) keep the existing window.
        if (
            self._wedge_quiesce
            and self._consecutive_failures >= self.QUIESCE_AFTER_CONSECUTIVE_FAILURES
        ):
            if self._state != ConnectionState.QUIESCE:
                # Initial entry.
                self._quiesce_until = now + self.QUIESCE_DURATION
                self._state = ConnectionState.QUIESCE
                self._post_window_fails = 0
            elif self._quiesce_until is not None and now >= self._quiesce_until:
                # Probe phase after an expired window: a few quick retries
                # (sleep_duration bottoms out at ~1s past the window) before
                # the next full window is armed.
                self._post_window_fails += 1
                if self._post_window_fails >= self.QUIESCE_PROBE_ATTEMPTS:
                    self._quiesce_until = now + self.QUIESCE_DURATION
                    self._post_window_fails = 0
            # else: failure during a still-active window — keep the window.
            # Always (re-)signal the close — each probe gets a fresh session,
            # and a mid-window failure means something re-opened the
            # connection (e.g. a polling_paused resume polled early); the
            # window only works with the session truly closed. Idempotent.
            self._quiesce_entered = True
            return self._state

        # Night mode is suppressed for wedge-quiesce devices: a healthy
        # SE30K stays Modbus-reachable (SLEEPING) at night, so "night_mode"
        # is always a misclassification there (2026-06-10 incident).
        if (
            elapsed > self.NIGHT_MODE_THRESHOLD
            and self._state != ConnectionState.NIGHT_MODE
            and not self._wedge_quiesce
        ):
            self._state = ConnectionState.NIGHT_MODE
        elif self._state == ConnectionState.CONNECTED and self._consecutive_failures >= self.CONSECUTIVE_FAILURES_BEFORE_RECONNECTING:
            self._state = ConnectionState.RECONNECTING

        if self._state != ConnectionState.CONNECTED:
            self._backoff = min(self._backoff * 2, self.MAX_BACKOFF)
        return self._state


def build_night_mode_inverter_registers(last_energy_wh: int = 0) -> list[int]:
    """Build synthetic Model 103 inverter registers for night mode.

    Returns 52 uint16 values (DID + Length + 50 data) with:
    - All power, current, voltage = 0
    - Energy = last_energy_wh (preserved from last known value)
    - Status = 4 (SLEEPING per CONTEXT.md decision)
    - Vendor status = 0
    """
    regs = [0] * 52
    regs[0] = INVERTER_DID   # 103
    regs[1] = INVERTER_LENGTH  # 50

    # SunSpec Model 103 layout, offsets relative to the DID register:
    #   24-25 WH (acc32, big-endian)   26 WH_SF   38 St   39 StVnd   40-41 Evt1
    #
    # These were previously off by two (energy written to 26-27, status to 40),
    # which put the energy HIGH WORD into the scale factor and the status into
    # the event field. Nothing caught it because this helper has no caller in
    # src/ and its tests asserted the same wrong offsets — the tests agreed
    # with the bug rather than with the decoder. Verified against
    # aggregation.decode_model_103_to_physical (energy 24/25, WH_SF 26, St 38)
    # and the Model 103 field order.
    regs[24] = (last_energy_wh >> 16) & 0xFFFF  # WH high word
    regs[25] = last_energy_wh & 0xFFFF           # WH low word
    regs[26] = 0                                 # WH_SF: energy is plain Wh

    regs[38] = 4  # St = SLEEPING

    return regs
