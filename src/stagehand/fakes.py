"""A simulated stage for demo mode and the tests. It moves on the clock, not on hardware.

``conex=True`` adds the CONEX-CC rules that matter to the app (CONEX-CC manual):
the speed (VA) can be read or set only when the stage is READY; the speed stored in
the controller is also the highest it accepts; and a reset (also the one before a
home search of a homed stage) restores the stored speed.
"""

import time
from collections.abc import Callable

from stagehand.stage import DeviceError, Reading, StageInfo, State


class FakeStage:
    def __init__(
        self,
        address: str,
        *,
        model: str = "Simulated stage",
        units: str = "mm",
        lower: float = 0.0,
        upper: float = 25.0,
        start: float | None = None,
        velocity: float = 5.0,
        max_velocity: float | None = None,
        conex: bool = False,
        homed: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.address = address
        self.model = model
        self.units = units
        self.lower, self.upper = lower, upper
        self.velocity = velocity
        self.max_velocity = max_velocity  # reported to the app (Kinesis does this)
        self.conex = conex
        self.stored_velocity = velocity  # the CONEX configuration speed: the maximum too
        self.clock = clock
        self.commands: list[str] = []  # what was sent, for the tests
        self.fail_open: str | None = None
        self.fail_read: str | None = None
        self.connected = False
        self._homed = homed
        self._enabled = True
        self._origin = (lower + upper) / 2 if start is None else start
        self._target = self._origin
        self._started = clock()
        self._homing = False

    def open(self, *, read_only: bool = False) -> StageInfo:
        if self.fail_open:
            raise DeviceError(self.fail_open)
        self.connected = True
        self.commands.append("open-read-only" if read_only else "open")
        reported = None if self.conex else self.max_velocity
        return StageInfo(self.model, self.address, self.units, self.lower, self.upper, reported)

    def get_velocity(self) -> float:
        self._speed_allowed()
        return self.velocity

    def set_velocity(self, velocity: float) -> None:
        self.commands.append(f"set_velocity {velocity:g}")
        self._speed_allowed()
        if self.conex and velocity > self.stored_velocity:
            raise DeviceError("Simulated CONEX-CC refused VA: parameter out of range.")
        if self.max_velocity is not None and velocity > self.max_velocity:
            raise DeviceError("Simulated stage refused the speed: above its maximum.")
        here = self._position()  # a move in progress keeps its place
        self._origin, self._started = here, self.clock()
        self.velocity = velocity

    def close(self) -> None:
        self.connected = False
        self.commands.append("close")

    def read(self) -> Reading:
        if self.fail_read:
            raise DeviceError(self.fail_read)
        return Reading(position=round(self._position(), 6), state=self._state())

    def move_to(self, position: float) -> None:
        self.commands.append(f"move_to {position:g}")
        if not self._homed:
            raise DeviceError("Simulated stage refused the move: not homed.")
        if not self.lower <= position <= self.upper:
            raise DeviceError("Simulated stage refused the move: outside the travel limits.")
        self._start(position)

    def home(self) -> None:
        self.commands.append("home")
        if self.conex and self._homed:
            self.velocity = self.stored_velocity  # the reset before the home search
        self._homing, self._homed = True, False
        self._start(self.lower)

    def stop(self) -> None:
        self.commands.append("stop")
        here = self._position()
        self._origin = self._target = here
        self._started = self.clock()
        if self._homing:
            self._homing = False

    def reset(self) -> None:
        self.commands.append("reset")
        self.stop()
        self._homed = False
        self._enabled = True
        if self.conex:
            self.velocity = self.stored_velocity

    def _state(self) -> State:
        moving = self._position() != self._target
        if self._homing and not moving:
            self._homing, self._homed = False, True
        if self._homing:
            return State.HOMING
        if moving:
            return State.MOVING
        if not self._enabled:
            return State.DISABLED
        if not self._homed:
            return State.NOT_HOMED
        return State.READY

    def _speed_allowed(self) -> None:
        if self.conex and self._state() is not State.READY:
            raise DeviceError("Simulated CONEX-CC refused VA: not allowed before homing.")

    def _start(self, target: float) -> None:
        self._origin = self._position()
        self._target = target
        self._started = self.clock()

    def _position(self) -> float:
        distance = self._target - self._origin
        travelled = self.velocity * (self.clock() - self._started)
        if travelled >= abs(distance):
            return self._target
        return self._origin + (travelled if distance > 0 else -travelled)
