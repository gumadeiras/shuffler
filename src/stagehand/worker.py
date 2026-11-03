"""One thread per stage. It owns the driver, runs commands in order, and polls the stage.

The GUI never calls a driver. It puts commands on the worker's queue and reads
``worker.status``, an immutable snapshot that the worker replaces after each change.
Stop removes queued motion commands first, so it never waits behind them, and it
sets an abort flag that a long driver sequence (the CONEX reset before a home) checks.

Every rule that protects the hardware is here, so the GUI and the tests share them:
a move needs a connected, homed, idle stage and a target inside the allowed range
(the controller's travel limits, narrowed by ``min`` and ``max`` in stages.toml).

A controller can report the old status for a short time after it accepts a move
(Kinesis refreshes its status every 100 ms). So after a move or home command the
worker keeps the stage busy until the controller reports the motion, the stage is
at the target, or ``LATCH_S`` passes. Without this, a second command could pass
the idle check while the first one starts.
"""

import contextlib
import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from stagehand.config import StageConfig
from stagehand.stage import DeviceError, StageDriver, StageInfo, State

log = logging.getLogger("stagehand")

POLL_S = 0.1
READ_FAILURE_LIMIT = 5
TOLERANCE = 1e-6  # floating point slack on range checks
SETTLED = 0.002  # a jog continues from the last target when the stage is this close to it
LATCH_S = 0.75  # how long a command keeps the stage busy until the controller confirms it
MOTION = frozenset({"move_to", "move_by", "home", "reset"})


@dataclass(frozen=True)
class StageStatus:
    name: str
    state: State = State.OFFLINE
    position: float | None = None
    target: float | None = None
    info: StageInfo | None = None
    allowed: tuple[float, float] | None = None
    velocity: float | None = None  # the speed moves use now, units/s
    detail: str = ""
    error: str = ""


def allowed_range(config: StageConfig, info: StageInfo) -> tuple[float, float] | None:
    """The controller's travel limits narrowed by the configured min and max."""
    low = info.lower if config.min is None else max(info.lower, config.min)
    high = info.upper if config.max is None else min(info.upper, config.max)
    return (low, high) if low < high else None


class StageWorker:
    def __init__(
        self,
        config: StageConfig,
        make_driver: Callable[[StageConfig], StageDriver],
        *,
        poll_s: float = POLL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        # What messages call the stage. The window sets it; one assignment, so no lock.
        self.label = config.name
        # Controller coordinate -> the number the window shows (a reversed axis mirrors it).
        # Used only in messages; the window sets it.
        self.display: Callable[[float], float] = lambda value: value
        # The speed this stage should use (units/s): the value saved in the window, else
        # `velocity` from stages.toml, else None (keep the controller's speed). A
        # successful set_velocity() updates it, and the window saves it. It is applied,
        # after the same checks as a typed speed, each time the stage becomes READY after
        # a connect, home, or reset: both controllers forget a working speed, and a
        # CONEX-CC accepts VA only in READY.
        self.speed: float | None = config.velocity
        self._speed_due = False
        self._make_driver = make_driver
        self._poll_s = poll_s
        self._clock = clock
        self._driver: StageDriver | None = None
        self._commands: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        self._failures = 0
        self._latch: tuple[State, float | None, float] | None = None  # state, target, deadline
        self._abort = threading.Event()
        self._quit = threading.Event()
        self._status = StageStatus(config.name)
        self._thread = threading.Thread(target=self._run, name=f"stage-{config.name}", daemon=True)

    # -- any thread -------------------------------------------------------------------------

    @property
    def status(self) -> StageStatus:
        return self._status

    @property
    def name(self) -> str:
        """The fixed key from stages.toml. Presets and settings use it."""
        return self.config.name

    @property
    def _who(self) -> str:
        """For the log: the label, and the key when they differ."""
        return self.label if self.label == self.name else f"{self.label} [{self.name}]"

    def start(self) -> None:
        self._thread.start()

    def connect(self) -> None:
        self._put("connect")

    def disconnect(self) -> None:
        self._put("disconnect")

    def move_to(self, position: float) -> None:
        self._put("move_to", position)

    def move_by(self, delta: float) -> None:
        self._put("move_by", delta)

    def home(self) -> None:
        self._put("home")

    def reset(self) -> None:
        self._put("reset")

    def set_velocity(self, velocity: float) -> None:
        self._put("set_velocity", velocity)

    def stop(self) -> None:
        """Drop queued motion, interrupt a long sequence, then stop."""
        self._abort.set()
        self._drain(only=MOTION)
        self._put("stop")

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the stage, disconnect, and end the thread."""
        self._abort.set()
        self._drain()
        self._put("disconnect")
        self._put("quit")
        if self._thread.is_alive():
            self._thread.join(timeout)
        else:
            while not self._quit.is_set() and not self._commands.empty():
                self.tick(0)

    # -- worker thread ----------------------------------------------------------------------

    def _run(self) -> None:
        while not self._quit.is_set():
            try:
                self.tick(self._poll_s)
            except Exception as error:  # keep the thread alive, so Stop still works
                log.exception("%s: unexpected error in the worker", self._who)
                self._set(error=f"Unexpected error: {error}")
                time.sleep(self._poll_s)

    def tick(self, timeout: float) -> None:
        """Run at most one command, then poll. The tests call this without a thread."""
        try:
            name, args = self._commands.get(timeout=timeout) if timeout else self._get_nowait()
        except queue.Empty:
            pass
        else:
            self._execute(name, args)
        if self._driver is not None:
            self._poll()

    def _execute(self, name: str, args: tuple[Any, ...]) -> None:
        if name == "quit":
            self._quit.set()
            return
        if name in MOTION or name == "connect":
            self._abort.clear()  # a Stop pressed before this command does not cancel it
        if name not in ("stop", "disconnect"):
            self._set(error="")
        try:
            getattr(self, f"_do_{name}")(*args)
        except DeviceError as error:
            log.warning("%s: %s", self._who, error)
            self._set(error=str(error))
        except Exception as error:
            log.exception("%s: unexpected error in %s", self._who, name)
            self._set(error=f"Unexpected error: {error}")

    def _do_connect(self) -> None:
        if self._driver is not None:
            return
        self._set(state=State.CONNECTING, detail="", error="")
        try:
            driver = self._make_driver(self.config)
            if hasattr(driver, "abort"):
                driver.abort = self._abort  # lets Stop interrupt a long driver sequence
            info = driver.open()
        except DeviceError as error:
            self._set(state=State.OFFLINE, error=str(error))
            log.warning("%s: %s", self._who, error)
            return
        except Exception as error:
            log.exception("%s: cannot connect", self._who)
            self._set(state=State.OFFLINE, error=f"Cannot connect: {error}")
            return
        allowed = allowed_range(self.config, info)
        self._driver, self._failures, self._latch = driver, 0, None
        self._set(info=info, allowed=allowed, target=None)
        log.info(
            "%s connected: %s at %s, travel %g to %g %s",
            self._who,
            info.model,
            info.address,
            info.lower,
            info.upper,
            info.units,
        )
        self._speed_due = True
        self._poll()  # applies the speed now if the stage is already READY
        if allowed is None:
            self._set(
                error=f"min and max in stages.toml are outside the controller travel "
                f"({info.lower:g} to {info.upper:g} {info.units}). Moves are blocked."
            )

    def _do_set_velocity(self, velocity: float) -> None:
        driver = self._ready_driver()  # homed and still: a CONEX-CC accepts VA only in READY
        self._check_speed(velocity)
        driver.set_velocity(velocity)
        self.speed = velocity
        self._speed_due = False
        self._read_velocity()
        log.info("%s: speed set to %g %s/s", self._who, velocity, self.config.units)

    def speed_limit(self) -> float | None:
        """The highest speed the app sends: the lower of the controller's reported maximum
        and `max_velocity` in stages.toml. A CONEX-CC reports none, but refuses a speed above
        the maximum stored in its configuration (error C)."""
        info = self._status.info
        limits = [v for v in (info.max_velocity if info else None, self.config.max_velocity) if v]
        return min(limits) if limits else None

    def _check_speed(self, velocity: float) -> None:
        units = self.config.units
        if not math.isfinite(velocity) or velocity <= 0:
            raise DeviceError(f"{self.label}: the speed must be more than 0 {units}/s.")
        limit = self.speed_limit()
        if limit is not None and velocity > limit:
            raise DeviceError(
                f"{self.label}: {velocity:g} {units}/s is above the maximum of {limit:g} {units}/s."
            )

    def _apply_speed(self) -> None:
        """Set the wanted speed again (the stage just became READY after connect/home/reset).

        Then enforce `max_velocity` from stages.toml on the controller's own speed too: a
        speed left by another program, or a CONEX-CC's stored speed after a reset, can be
        higher than the ceiling, and moves use it unless the app sets one.
        """
        assert self._driver is not None
        self._speed_due = False
        units = self.config.units
        if self.speed is not None:
            try:
                self._check_speed(self.speed)
                self._driver.set_velocity(self.speed)
                log.info("%s: speed %g %s/s applied", self._who, self.speed, units)
            except DeviceError as error:
                log.warning("%s: saved speed not applied: %s", self._who, error)
                self._set(error=f"The saved speed of {self.speed:g} was not applied: {error}")
        self._read_velocity()
        ceiling = self.config.max_velocity
        current = self._status.velocity
        if ceiling is not None and (current is None or current > ceiling):
            try:
                self._driver.set_velocity(ceiling)
                log.info("%s: speed lowered to max_velocity %g %s/s", self._who, ceiling, units)
            except DeviceError as error:
                log.error("%s: cannot lower the speed to max_velocity: %s", self._who, error)
                self._set(error=f"Cannot lower the speed to max_velocity {ceiling:g}: {error}")
            self._read_velocity()

    def _read_velocity(self) -> None:
        """The speed the controller reports, read only in READY (a CONEX-CC refuses VA?
        in other states). A read failure does not stop the stage."""
        assert self._driver is not None
        if self._status.state is not State.READY:
            return
        try:
            self._set(velocity=self._driver.get_velocity())
        except DeviceError as error:
            log.warning("%s: %s", self._who, error)
            self._set(velocity=None)

    def _do_disconnect(self) -> None:
        driver, self._driver = self._driver, None
        if driver is None:
            return
        busy = self._status.state.busy
        try:
            with contextlib.suppress(Exception):  # harmless when idle; the cache may be stale
                driver.stop()
            if busy:
                log.info("%s: stopped before disconnect", self._who)
        finally:
            driver.close()
            self._latch = None
            self._set(state=State.OFFLINE, position=None, target=None, velocity=None, detail="")
            log.info("%s disconnected", self._who)

    def _do_move_to(self, target: float) -> None:
        driver = self._ready_driver()
        target = round(target, self.config.decimals)
        allowed = self._status.allowed
        if allowed is None:
            raise DeviceError(f"{self.label}: no allowed range. Check min and max in stages.toml.")
        low, high = allowed
        if not low - TOLERANCE <= target <= high + TOLERANCE:
            units = self.config.units
            shown_low, shown_high = sorted((self.display(low), self.display(high)))
            raise DeviceError(
                f"{self.label}: {self._fmt(self.display(target))} {units} is outside the "
                f"allowed range {self._fmt(shown_low)} to {self._fmt(shown_high)} {units}."
            )
        target = min(max(target, low), high)
        driver.move_to(target)
        self._hold(State.MOVING, target)
        log.info(
            "%s: move to %s (controller %s)",
            self._who,
            self._fmt(self.display(target)),
            self._fmt(target),
        )

    def _do_move_by(self, delta: float) -> None:
        self._ready_driver()
        status = self._status
        base = status.position
        if base is None:
            raise DeviceError(f"{self.label}: the position is not known yet.")
        if status.target is not None and abs(base - status.target) <= SETTLED:
            base = status.target  # repeated jogs add up exactly
        self._do_move_to(base + delta)

    def _do_home(self) -> None:
        driver = self._connected_driver()
        self._refuse_if_busy()
        driver.home()
        self._hold(State.HOMING, None)
        self._speed_due = True  # a CONEX-CC resets before homing, which restores its speed
        log.info("%s: home search", self._who)

    def _do_stop(self) -> None:
        if self._driver is None:
            return
        self._latch = None
        self._driver.stop()
        self._set(target=None)
        log.info("%s: stop", self._who)

    def _do_reset(self) -> None:
        driver = self._connected_driver()
        self._refuse_if_busy("Press Stop and wait for it to stop first.")
        driver.reset()
        self._latch = None
        self._set(target=None)
        self._speed_due = True  # applied again once the stage is homed and READY
        log.info("%s: reset", self._who)

    def _poll(self) -> None:
        assert self._driver is not None
        try:
            reading = self._driver.read()
        except DeviceError as error:
            self._failures += 1
            if self._failures >= READ_FAILURE_LIMIT:
                self._lost_contact(error)
            return
        self._failures = 0
        state = self._latched(reading.state, reading.position)
        changes: dict[str, Any] = {
            "position": reading.position,
            "state": state,
            "detail": reading.detail,
        }
        if state is State.NOT_HOMED:
            changes["target"] = None
        if state in (State.NOT_HOMED, State.OFFLINE):
            changes["velocity"] = None  # unknown until READY (a reset restores the default)
        was_ready = self._status.state is State.READY
        self._set(**changes)
        if state is State.READY and (self._speed_due or not was_ready):
            if self._speed_due:
                self._apply_speed()
            elif self._status.velocity is None:
                self._read_velocity()

    def _lost_contact(self, error: DeviceError) -> None:
        log.error("%s: lost contact: %s", self._who, error)
        driver, self._driver, self._latch = self._driver, None, None
        assert driver is not None
        # Try to stop: a write can still get through when replies do not.
        with contextlib.suppress(Exception):
            driver.stop()
        with contextlib.suppress(Exception):
            driver.close()
        self._set(
            state=State.OFFLINE,
            position=None,
            target=None,
            error=f"Lost contact. {error} A stop was sent. Press Connect to try again.",
        )

    # -- helpers ----------------------------------------------------------------------------

    def _hold(self, state: State, target: float | None) -> None:
        """Show the commanded motion until the controller confirms it (see module doc)."""
        self._latch = (state, target, self._clock() + LATCH_S)
        self._set(state=state, target=target)

    def _latched(self, reported: State, position: float | None) -> State:
        if self._latch is None:
            return reported
        held, target, deadline = self._latch
        arrived = target is not None and position is not None and abs(position - target) <= SETTLED
        if reported.busy or reported is State.ERROR or arrived or self._clock() >= deadline:
            self._latch = None  # the controller reports for itself now
            return reported
        return held

    def _connected_driver(self) -> StageDriver:
        if self._driver is None:
            raise DeviceError(f"{self.label} is not connected.")
        return self._driver

    def _refuse_if_busy(self, advice: str = "Wait or press Stop.") -> None:
        state = self._status.state
        if state.busy:
            raise DeviceError(f"{self.label} is {state.value.lower()}. {advice}")

    def _ready_driver(self) -> StageDriver:
        driver = self._connected_driver()
        state = self._status.state
        if state is State.NOT_HOMED:
            raise DeviceError(f"{self.label} is not homed. Home it first.")
        self._refuse_if_busy()
        if state is not State.READY:
            raise DeviceError(f"{self.label} is {state.value.lower()}. Reset it first.")
        return driver

    def _set(self, **changes: Any) -> None:
        self._status = replace(self._status, **changes)

    def _put(self, name: str, *args: Any) -> None:
        self._commands.put((name, args))

    def _get_nowait(self) -> tuple[str, tuple[Any, ...]]:
        return self._commands.get_nowait()

    def _drain(self, only: frozenset[str] | None = None) -> None:
        """Remove queued commands (all, or only those in ``only``), keeping the others in order."""
        kept = []
        while True:
            try:
                item = self._commands.get_nowait()
            except queue.Empty:
                break
            if only is not None and item[0] not in only:
                kept.append(item)
        for item in kept:
            self._commands.put(item)

    def _fmt(self, value: float) -> str:
        return f"{value:.{self.config.decimals}f}"
