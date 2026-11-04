"""Newport CONEX-CC over its USB virtual COM port, with the ASCII command set.

Serial settings: 921600 baud, 8 data bits, no parity, 1 stop bit, XON/XOFF, CR LF.
Each command is ``<address><code>[value]``; a query reply echoes the address and
code before the value (``1TP12.345``). A command that sets something returns no
reply, so each one is followed by ``TE`` (last command error).

This module sends only these commands:
  queries  VE, ID?, SL?, SR?, TP, TS, TE, VA?
  motion   PA (move absolute), OR (home search), ST (stop)
  other    RS (reset: reboot the controller), VA (velocity, set from the window)
It never enters configuration mode (PW) and never changes the home type (HT) or
stored limits.
"""

import contextlib
import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

import serial

from shuffler.stage import DeviceError, Reading, StageInfo, State

BAUD_RATE = 921600
TIMEOUT_S = 0.3
RESET_TIMEOUT_S = 15.0
RESET_SETTLE_S = 0.5  # after a reset, "not referenced" must hold this long (see reset)
HOME_START_S = 2.0  # after OR, the controller must leave its state within this time

log = logging.getLogger("shuffler")

# TS controller state (the last two characters of the reply).
STATES: dict[str, tuple[State, str]] = {
    "0A": (State.NOT_HOMED, "Not referenced after reset"),
    "0B": (State.NOT_HOMED, "Not referenced after homing"),
    "0C": (State.NOT_HOMED, "Not referenced after configuration"),
    "0D": (State.NOT_HOMED, "Not referenced after disable"),
    "0E": (State.NOT_HOMED, "Not referenced after ready"),
    "0F": (State.NOT_HOMED, "Not referenced after moving"),
    "10": (State.ERROR, "Not referenced: stage error"),
    "14": (State.ERROR, "Configuration mode: reset the controller"),
    "1E": (State.HOMING, "Homing"),
    "28": (State.MOVING, "Moving"),
    "32": (State.READY, ""),
    "33": (State.READY, ""),
    "34": (State.READY, ""),
    "35": (State.READY, ""),
    "36": (State.READY, "Tracking ready"),
    "37": (State.READY, "Tracking ready"),
    "38": (State.READY, "Tracking ready"),
    "3C": (State.DISABLED, "Disabled"),
    "3D": (State.DISABLED, "Disabled after moving"),
    "3E": (State.DISABLED, "Disabled after tracking"),
    "3F": (State.DISABLED, "Disabled"),
    "46": (State.MOVING, "Tracking"),
    "47": (State.MOVING, "Tracking"),
}

# TS positioner error flags (the first four hex characters of the reply).
POSITIONER_ERRORS: dict[int, str] = {
    0x0200: "80 W output power exceeded",
    0x0100: "DC voltage too low",
    0x0080: "Wrong stage",
    0x0040: "Homing time-out",
    0x0020: "Following error",
    0x0010: "Short circuit",
    0x0008: "RMS current limit",
    0x0004: "Peak current limit",
    0x0002: "At positive end of run",
    0x0001: "At negative end of run",
}
# The end-of-run flags are positions, not faults: OR is still accepted at end of run.
END_OF_RUN = 0x0002 | 0x0001
# What to do about each fault. With several faults, the first one listed here decides
# the advice: a missing power supply explains the others, so it comes first.
RESET = "then use ⋯ > Reset controller"
FAULT_ADVICE: dict[int, str] = {
    0x0100: "Check that the controller's 24 V power supply is plugged in and switched on "
    "(USB alone powers only the communication)",
    0x0010: f"Check the stage cable and its connector for damage, {RESET}",
    0x0080: "The stage plugged into this controller is not the one it is configured for. "
    "Check which stage cable goes to which controller",
    0x0200: f"The motor is overloaded. Check that nothing blocks the stage, {RESET}",
    0x0008: f"The motor drew too much current. Check that nothing blocks the stage, {RESET}",
    0x0004: f"The motor drew too much current. Check that nothing blocks the stage, {RESET}",
    0x0020: "The stage did not follow the command. Check that nothing blocks it and that "
    f"its cable is plugged in, {RESET}",
    0x0040: "The home search took too long. Check that the stage can move freely and its "
    f"cable is plugged in, {RESET} and home again",
}

# TE command error codes: what the controller said, and what to do.
COMMAND_ERRORS: dict[str, str] = {
    "A": "unknown command",
    "B": "wrong controller address",
    "C": "parameter missing or out of range",
    "D": "command not allowed",
    "E": "home search already started",
    "G": "target is outside the travel limits",
    "H": "not allowed before the stage is homed",
    "I": "not allowed in configuration mode",
    "J": "not allowed while disabled",
    "K": "not allowed while ready",
    "L": "not allowed while homing",
    "M": "not allowed while moving",
    "N": "the position is outside the software limits",
    "S": "communication time-out",
    "U": "EEPROM error",
    "V": "error during command execution",
    "W": "not allowed for this controller version",
    "X": "not allowed for this controller version",
}
COMMAND_HINTS: dict[str, str] = {
    "A": "This is a shuffler bug. Please report it with shuffler.log",
    "B": "Check that the port in stages.toml is this controller's port "
    "(uv run shuffler ports lists them)",
    "C": "Use a smaller value. For a speed, the CONEX-CC allows at most the speed stored "
    "in its configuration",
    "D": f"Check the controller's 24 V power supply and the stage cable, {RESET}",
    "E": "Wait for the home search to finish",
    "G": "Choose a target inside the travel shown on the card",
    "H": "Press Home first",
    "I": "Use ⋯ > Reset controller",
    "J": "Use ⋯ > Reset controller",
    "K": "Use ⋯ > Reset controller, then Home",
    "L": "Wait for the home search to finish, or press Stop",
    "M": "Wait for the move to finish, or press Stop",
    "N": "Choose a target inside the travel shown on the card",
    "S": "Check the USB cable, then press Connect",
    "U": "Unplug the controller's power and USB for a few seconds and plug them back. "
    "If it happens again, contact Newport",
    "V": "Use ⋯ > Reset controller and try again",
    "W": "This controller's firmware does not support it",
    "X": "This controller's firmware does not support it",
}


def parse_status(text: str) -> tuple[State, str]:
    """``TS`` value (``EEEESS``) to a state and a detail line."""
    if len(text) != 6:
        raise DeviceError(f"Unexpected controller status {text!r}.")
    try:
        flags = int(text[:4], 16)
    except ValueError:
        raise DeviceError(f"Unexpected controller status {text!r}.") from None
    state, detail = STATES.get(text[4:].upper(), (State.ERROR, f"Unknown state {text[4:]}"))
    problems = [message for bit, message in POSITIONER_ERRORS.items() if flags & bit]
    return state, "; ".join(p for p in (detail, *problems) if p)


def parse_fault(text: str) -> str:
    """The hardware errors in a ``TS`` value, with what to do; empty when there are none.

    The controller refuses a home search while any of these is present (CONEX-CC
    manual, OR: error D), so the window shows them instead of offering Home.
    """
    try:
        flags = int(text[:4], 16) & ~END_OF_RUN
    except ValueError:
        return ""
    problems = [message for bit, message in POSITIONER_ERRORS.items() if flags & bit]
    if not problems:
        return ""
    advice = next(
        (FAULT_ADVICE[bit] for bit in FAULT_ADVICE if flags & bit),
        f"Check the stage cable and the 24 V power supply, {RESET}",
    )
    return f"{'; '.join(problems)}. {advice}"


def format_number(value: float) -> str:
    return f"{value:.6f}"


class ConexStage:
    def __init__(
        self,
        port: str,
        *,
        address: int = 1,
        units: str = "mm",
        serial_factory: Callable[..., Any] = serial.Serial,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.port = port
        self._prefix = str(address)
        self._units = units
        self._serial_factory = serial_factory
        self._sleep = sleep
        self._clock = clock
        self._serial: Any = None
        # The worker replaces this with its own event, so Stop can interrupt reset().
        self.abort = threading.Event()

    # -- connection -------------------------------------------------------------------------

    def open(self, *, read_only: bool = False) -> StageInfo:
        try:
            self._serial = self._serial_factory(
                port=self.port,
                baudrate=BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                xonxoff=True,
                timeout=TIMEOUT_S,
                write_timeout=TIMEOUT_S,
            )
        except serial.SerialException as error:
            text = str(error)
            if "PermissionError" in text or "Access is denied" in text:
                raise DeviceError(
                    f"{self.port} is in use by another program. "
                    "Close the Newport CONEX-CC application and try again."
                ) from None
            if "FileNotFoundError" in text:
                raise DeviceError(
                    f"{self.port} does not exist. Is the controller plugged in?"
                ) from None
            raise DeviceError(
                f"Cannot open {self.port}: {text} Unplug the controller's USB cable, plug it "
                "back in, and press Connect."
            ) from None
        # Nothing is written here: the speed is set by the worker once the stage is READY
        # (VA is not allowed while NOT REFERENCED, which is the state after power-up).
        del read_only
        try:
            self._serial.reset_input_buffer()
            version = self._query("VE")
            if "CONEX" not in version.upper():
                raise DeviceError(
                    f"The device on {self.port} is not a CONEX controller. Check the port in "
                    "stages.toml (uv run shuffler ports lists the ports)."
                )
            self._clear_error()  # an error left by another program
            try:
                stage = self._query("ID?")  # descriptive only; may be refused before homing
            except DeviceError:
                stage = ""
            self._clear_error()
            # The software limits are needed. If the controller refuses them, the
            # connection fails: no moves are possible without known limits.
            lower = self._query_number("SL?")
            upper = self._query_number("SR?")
            self._clear_error()
        except Exception:
            self.close()
            raise
        model = " · ".join(p for p in (version, stage) if p)
        return StageInfo(
            model=model, address=self.port, units=self._units, lower=lower, upper=upper
        )

    def close(self) -> None:
        port, self._serial = self._serial, None
        if port is not None:
            with contextlib.suppress(Exception):
                port.close()

    # -- status -----------------------------------------------------------------------------

    def read(self) -> Reading:
        position = self._query_number("TP")
        status = self._query("TS")
        state, detail = parse_status(status)
        return Reading(position=position, state=state, detail=detail, fault=parse_fault(status))

    # -- motion -----------------------------------------------------------------------------

    def move_to(self, position: float) -> None:
        self._command(f"PA{format_number(position)}")

    def home(self) -> None:
        """Home search. A CONEX-CC accepts it only when not referenced, so a homed
        controller (READY or DISABLED) is reset first.

        The controller refuses OR while a hardware error is present (manual, OR: error D,
        which TE reports only as "execution not allowed"), so that is checked first,
        before any reset. A controller that is homing or moving is never reset: that
        would end its home search or move (this happened when Home was pressed twice).

        On the rig, a CONEX-CC reset this way accepted the OR that followed (no error)
        but never started homing; it stayed "not referenced after reset". So the
        start is confirmed: the controller must leave its state within HOME_START_S.
        If it does not, OR is sent once more (a second OR during homing only gives
        error E, "already started"), and then the home fails with a clear message.
        """
        status = self._query("TS")
        if fault := parse_fault(status):
            raise DeviceError(f"Cannot home: the CONEX-CC on {self.port} reports {fault}.")
        state, _ = parse_status(status)
        if state.busy:
            raise DeviceError(
                f"The CONEX-CC on {self.port} is {state.value.lower()} already. Wait for it "
                "to finish, or press Stop."
            )
        if state is not State.NOT_HOMED:
            self.reset()
        for attempt in (1, 2):
            if self.abort.is_set():
                raise DeviceError(
                    "Home cancelled by Stop. The controller was reset; home it again."
                )
            before = self._query("TS")[4:]
            error = self._send("OR")
            if error == "E":  # a home search is running already
                return
            if error:
                raise DeviceError(self._refusal("OR", error))
            started = self._wait_for_change(before, HOME_START_S)
            log.info(
                "%s: OR (attempt %d): state %s -> %s", self.port, attempt, before, started or before
            )
            if started:
                return
        raise DeviceError(
            f"The CONEX-CC on {self.port} accepted the home search twice but did not start "
            "it. Press Home again. If it happens again, unplug the controller's 24 V power "
            "for a few seconds, plug it back, and press Connect."
        )

    def _wait_for_change(self, before: str, seconds: float) -> str:
        """The new TS state code once it differs from ``before``, or "" after ``seconds``.

        Stop ends the wait at once and counts as a change: nothing is retried after Stop.
        """
        deadline = self._clock() + seconds
        while True:
            try:
                code = self._query("TS")[4:]
            except DeviceError:
                code = before
            if code.upper() != before.upper():
                return code
            if self.abort.is_set():
                return "stopped"
            if self._clock() >= deadline:
                return ""
            self._sleep(0.1)

    def stop(self) -> None:
        self._write("ST")
        # Read TE to clear it. "Not allowed in this state" is not a failure for stop.
        with contextlib.suppress(DeviceError):
            self._query("TE")

    def reset(self) -> None:
        """Reboot the controller. It comes back not referenced, at its stored velocity.

        The speed set in the window is lost; the worker sets it again once the stage is
        READY (VA is not allowed while NOT REFERENCED).
        """
        self._write("RS")
        started = self._clock()
        self._sleep(1.0)
        deadline = started + RESET_TIMEOUT_S
        # Back means "not referenced" in two reads at least RESET_SETTLE_S apart: the
        # first answer can come before the controller has finished starting up.
        first_seen: float | None = None
        while self._clock() < deadline:
            try:
                state, _ = parse_status(self._query("TS"))
            except DeviceError:
                state = None
            if state is not State.NOT_HOMED:
                first_seen = None
            elif self.abort.is_set():  # back, and Stop pressed: nothing follows, no settling
                self._clear_error()
                return
            elif first_seen is None:
                first_seen = self._clock()
            elif self._clock() - first_seen >= RESET_SETTLE_S:
                self._clear_error()
                log.info("%s: reset done in %.1f s", self.port, self._clock() - started)
                return
            if self.abort.is_set():
                break  # Stop pressed: the stage cannot move while it reboots, so stop waiting
            self._sleep(0.25)
        else:
            raise DeviceError(
                f"The controller on {self.port} did not come back after the reset. Unplug its "
                "24 V power and USB for a few seconds, plug them back, and press Connect."
            )
        raise DeviceError(
            f"Reset of {self.port} interrupted by Stop. Use ⋯ > Reset controller again, or "
            "press Connect if the card shows Offline."
        )

    def get_velocity(self) -> float:
        """The speed for moves (VA?). The worker asks only in READY."""
        try:
            return self._query_number("VA?")
        finally:
            with contextlib.suppress(DeviceError):  # keep the query's own error, if any
                self._clear_error()  # a refused query must not be blamed on the next command

    def set_velocity(self, velocity: float) -> None:
        """Set the speed for the next moves (VA), in READY only.

        In READY the controller accepts a speed up to the maximum stored in its
        configuration and refuses a higher one with error C (CONEX-CC manual, VA). The
        value is a working value: a reset or power-off restores the stored speed.
        """
        self._command(f"VA{format_number(velocity)}")

    # -- protocol ---------------------------------------------------------------------------

    def _write(self, code: str) -> None:
        if self._serial is None:
            raise DeviceError(f"{self.port} is not connected. Press Connect.")
        try:
            self._serial.write(f"{self._prefix}{code}\r\n".encode("ascii"))
        except serial.SerialException as error:
            raise DeviceError(f"Lost contact with {self.port}: {error}") from None

    def _query(self, code: str) -> str:
        """Send a query and return the value after the echo.

        The input buffer is flushed before each query, and again after a failed one, so a
        reply that arrives late can never be read as the answer to a later query.
        """
        self._flush()
        self._write(code)
        try:
            raw = self._serial.read_until(b"\r\n")
        except serial.SerialException as error:
            raise DeviceError(f"Lost contact with {self.port}: {error}") from None
        if not raw.endswith(b"\r\n"):
            self._resync()
            raise DeviceError(
                f"No answer from the CONEX-CC on {self.port}. Check that its USB cable is "
                "plugged in."
            )
        reply = raw.decode("ascii", errors="replace").strip()
        echo = self._prefix + code.rstrip("?")
        if not reply.startswith(echo):
            self._resync()
            raise DeviceError(
                f"Unexpected answer from {self.port}: {reply!r}. Close any other program that "
                "uses this port (the Newport application), then press Connect."
            )
        return reply[len(echo) :].strip()

    def _flush(self) -> None:
        if self._serial is None:
            raise DeviceError(f"{self.port} is not connected. Press Connect.")
        try:
            self._serial.reset_input_buffer()
        except serial.SerialException as error:
            raise DeviceError(f"Lost contact with {self.port}: {error}") from None

    def _resync(self) -> None:
        """Wait for a late reply to arrive, then discard it."""
        self._sleep(0.05)
        with contextlib.suppress(DeviceError):
            self._flush()

    def _query_number(self, code: str) -> float:
        value = self._query(code)
        try:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError
        except ValueError:
            raise DeviceError(f"Unexpected answer from {self.port} to {code}: {value!r}.") from None
        return number

    def _command(self, code: str) -> None:
        """Send a command and raise if the controller refused it.

        The controller keeps a command error until TE reads it; a successful command does
        not clear it (CONEX-CC manual, TE). So TE is read before the command too, and the
        error read after it belongs to this command only.
        """
        if error := self._send(code):
            raise DeviceError(self._refusal(code[:2], error))

    def _send(self, code: str) -> str:
        """Send a command; return its TE error letter, or "" if it was accepted."""
        self._clear_error()
        self._write(code)
        error = self._query("TE")
        return "" if error in ("", "@") else error

    def _refusal(self, command: str, error: str) -> str:
        """What the controller refused, why, and what to do.

        A hardware error explains most refusals (error D says only "not allowed"), so
        the status is read again and a fault, when there is one, is named instead.
        """
        message = COMMAND_ERRORS.get(error, f"error {error}")
        text = f"The CONEX-CC on {self.port} refused {command}: {message}."
        fault = ""
        with contextlib.suppress(DeviceError):
            fault = parse_fault(self._query("TS"))
        if fault:
            return f"{text} It reports {fault}."
        hint = COMMAND_HINTS.get(error, "Use ⋯ > Reset controller and try again")
        return f"{text} {hint}."

    def _clear_error(self) -> None:
        self._query("TE")
