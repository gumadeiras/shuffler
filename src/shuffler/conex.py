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

# TE command error codes.
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
            raise DeviceError(f"Cannot open {self.port}: {text}") from None
        # Nothing is written here: the speed is set by the worker once the stage is READY
        # (VA is not allowed while NOT REFERENCED, which is the state after power-up).
        del read_only
        try:
            self._serial.reset_input_buffer()
            version = self._query("VE")
            if "CONEX" not in version.upper():
                raise DeviceError(f"The device on {self.port} is not a CONEX controller.")
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
        state, detail = parse_status(self._query("TS"))
        return Reading(position=position, state=state, detail=detail)

    # -- motion -----------------------------------------------------------------------------

    def move_to(self, position: float) -> None:
        self._command(f"PA{format_number(position)}")

    def home(self) -> None:
        """Home search. A CONEX-CC accepts it only when not referenced, so reset first."""
        state, _ = parse_status(self._query("TS"))
        if state is not State.NOT_HOMED:
            self.reset()
        if self.abort.is_set():
            raise DeviceError("Home cancelled by Stop. The controller was reset; home it again.")
        self._command("OR")

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
        self._sleep(1.0)
        deadline = self._clock() + RESET_TIMEOUT_S
        while self._clock() < deadline:
            try:
                state, _ = parse_status(self._query("TS"))
            except DeviceError:
                state = None
            if state is State.NOT_HOMED:
                self._clear_error()
                return
            if self.abort.is_set():
                break  # Stop pressed: the stage cannot move while it reboots, so stop waiting
            self._sleep(0.25)
        else:
            raise DeviceError(f"The controller on {self.port} did not come back after the reset.")
        raise DeviceError(f"Reset of {self.port} interrupted by Stop. Check the stage state.")

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
            raise DeviceError(f"{self.port} is not connected.")
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
            raise DeviceError(f"No answer from the CONEX-CC on {self.port}.")
        reply = raw.decode("ascii", errors="replace").strip()
        echo = self._prefix + code.rstrip("?")
        if not reply.startswith(echo):
            self._resync()
            raise DeviceError(f"Unexpected answer from {self.port}: {reply!r}.")
        return reply[len(echo) :].strip()

    def _flush(self) -> None:
        if self._serial is None:
            raise DeviceError(f"{self.port} is not connected.")
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
        self._clear_error()
        self._write(code)
        error = self._query("TE")
        if error and error != "@":
            message = COMMAND_ERRORS.get(error, f"error {error}")
            raise DeviceError(f"The CONEX-CC on {self.port} refused {code[:2]}: {message}.")

    def _clear_error(self) -> None:
        self._query("TE")
