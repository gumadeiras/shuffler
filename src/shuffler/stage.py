"""What every stage driver shares: the states, a reading, and the driver contract.

A driver talks to one controller. It is used from one thread only (its worker), so
it needs no locks. Expected device failures raise ``DeviceError`` with text that a
user can act on; the GUI shows that text without a traceback.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class DeviceError(Exception):
    """An expected device failure. The message is for the user."""


class State(Enum):
    OFFLINE = "Offline"
    CONNECTING = "Connecting"
    NOT_HOMED = "Not homed"
    HOMING = "Homing"
    READY = "Ready"
    MOVING = "Moving"
    DISABLED = "Disabled"
    ERROR = "Error"

    @property
    def busy(self) -> bool:
        """The stage is in motion (a move or a home search)."""
        return self in (State.HOMING, State.MOVING)

    @property
    def connected(self) -> bool:
        return self not in (State.OFFLINE, State.CONNECTING)


@dataclass(frozen=True)
class StageInfo:
    """What a controller says about itself when it connects."""

    model: str  # for example "CONEX-CC 2.0.1 · TRB25CC" or "KBD101 · DDS100"
    address: str  # "COM3" or "SN 28253849"
    units: str
    lower: float  # travel limits that the controller enforces
    upper: float
    max_velocity: float | None = None  # units/s, when the controller reports one


@dataclass(frozen=True)
class Reading:
    position: float | None
    state: State
    detail: str = ""  # controller detail, for example "Following error"


class StageDriver(Protocol):
    def open(self, *, read_only: bool = False) -> StageInfo:
        """Connect. ``read_only`` connects without enabling the motor or writing settings."""
        ...

    def close(self) -> None: ...

    def read(self) -> Reading: ...

    def move_to(self, position: float) -> None:
        """Start an absolute move and return at once."""
        ...

    def home(self) -> None:
        """Start a home search and return at once."""
        ...

    def stop(self) -> None:
        """Stop motion. Never raises because the stage was already stopped."""
        ...

    def reset(self) -> None:
        """Clear a fault. A CONEX-CC reboots and must be homed again."""
        ...

    def get_velocity(self) -> float:
        """The speed that moves use now, in units/s."""
        ...

    def set_velocity(self, velocity: float) -> None:
        """Set the speed for the next moves. The worker applies it again after a reset."""
        ...
