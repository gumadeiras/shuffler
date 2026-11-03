"""A simulated stage for demo mode and the tests. It moves on the clock, not on hardware."""

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
        homed: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.address = address
        self.model = model
        self.units = units
        self.lower, self.upper = lower, upper
        self.velocity = velocity
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
        return StageInfo(self.model, self.address, self.units, self.lower, self.upper)

    def close(self) -> None:
        self.connected = False
        self.commands.append("close")

    def read(self) -> Reading:
        if self.fail_read:
            raise DeviceError(self.fail_read)
        position = self._position()
        moving = position != self._target
        if self._homing and not moving:
            self._homing, self._homed = False, True
        if self._homing:
            state = State.HOMING
        elif moving:
            state = State.MOVING
        elif not self._enabled:
            state = State.DISABLED
        elif not self._homed:
            state = State.NOT_HOMED
        else:
            state = State.READY
        return Reading(position=round(position, 6), state=state)

    def move_to(self, position: float) -> None:
        self.commands.append(f"move_to {position:g}")
        if not self._homed:
            raise DeviceError("Simulated stage refused the move: not homed.")
        if not self.lower <= position <= self.upper:
            raise DeviceError("Simulated stage refused the move: outside the travel limits.")
        self._start(position)

    def home(self) -> None:
        self.commands.append("home")
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
