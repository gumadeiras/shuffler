"""Thorlabs K-Cube motor controllers through the Kinesis .NET libraries (pythonnet).

Kinesis must be installed (64-bit). The libraries load from ``kinesis_dir`` the
first time a stage connects, so the GUI starts and runs demo mode without them.

The first two digits of the serial number name the controller family. The stage
settings (for example DDS100) are the ones the Kinesis application uses for that
serial number, so positions and limits are in the same real units as in Kinesis.
"""

import contextlib
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shuffler.stage import DeviceError, Reading, StageInfo, State


@dataclass(frozen=True)
class Family:
    product: str
    dll: str
    namespace: str
    cls: str
    factory: str


FAMILIES = {
    "28": Family(
        "KBD101",
        "Thorlabs.MotionControl.KCube.BrushlessMotorCLI",
        "Thorlabs.MotionControl.KCube.BrushlessMotorCLI",
        "KCubeBrushlessMotor",
        "CreateKCubeBrushlessMotor",
    ),
    "27": Family(
        "KDC101",
        "ThorLabs.MotionControl.KCube.DCServoCLI",
        "Thorlabs.MotionControl.KCube.DCServoCLI",
        "KCubeDCServo",
        "CreateKCubeDCServo",
    ),
    "26": Family(
        "KST101",
        "Thorlabs.MotionControl.KCube.StepperMotorCLI",
        "Thorlabs.MotionControl.KCube.StepperMotorCLI",
        "KCubeStepper",
        "CreateKCubeStepper",
    ),
}

POLL_MS = 100
SETTINGS_TIMEOUT_MS = 10000

_load_lock = threading.Lock()
_loaded: set[str] = set()


def _load(kinesis_dir: Path, *dlls: str) -> None:
    """Add Kinesis assemblies to the .NET runtime once per process."""
    with _load_lock:
        try:
            import clr
        except ImportError as error:
            raise DeviceError(f"pythonnet is not available: {error}") from None
        if str(kinesis_dir) not in sys.path:
            sys.path.append(str(kinesis_dir))
        for dll in dlls:
            if dll in _loaded:
                continue
            path = kinesis_dir / f"{dll}.dll"
            if not path.exists():
                raise DeviceError(
                    f"Thorlabs Kinesis is not installed in {kinesis_dir} ({path.name} is missing)."
                )
            clr.AddReference(str(path))
            _loaded.add(dll)


def _message(error: BaseException) -> str:
    """The text of a .NET exception, without the type name."""
    return str(getattr(error, "Message", None) or error).strip()


def to_float(value: Any) -> float:
    """A System.Decimal (or a number) to float, independent of the Windows locale."""
    if isinstance(value, int | float):
        return float(value)
    from System import Decimal

    return float(Decimal.ToDouble(value))


def to_decimal(value: float) -> Any:
    from System import Decimal

    return Decimal(float(value))


class KinesisStage:
    def __init__(
        self,
        serial: str,
        *,
        kinesis_dir: Path,
        units: str = "mm",
    ) -> None:
        self.serial = serial
        self._dir = kinesis_dir
        self._units = units
        self._device: Any = None
        family = FAMILIES.get(serial[:2])
        if family is None:
            known = ", ".join(f"{p}xxxxxx ({f.product})" for p, f in FAMILIES.items())
            raise DeviceError(f"Serial {serial} is not a supported K-Cube. Supported: {known}.")
        self._family = family

    def open(self, *, read_only: bool = False) -> StageInfo:
        family = self._family
        try:
            _load(
                self._dir,
                "Thorlabs.MotionControl.DeviceManagerCLI",
                "Thorlabs.MotionControl.GenericMotorCLI",
                family.dll,
            )
            import importlib

            from Thorlabs.MotionControl.DeviceManagerCLI import (
                DeviceConfiguration,
                DeviceManagerCLI,
            )

            controller = getattr(importlib.import_module(family.namespace), family.cls)
            with _load_lock:  # the device list is process-wide
                DeviceManagerCLI.BuildDeviceList()
                present = {str(s) for s in DeviceManagerCLI.GetDeviceList()}
        except DeviceError:
            raise
        except Exception as error:
            raise DeviceError(
                f"Cannot load Thorlabs Kinesis from {self._dir}: {_message(error)}"
            ) from None
        if self.serial not in present:
            raise DeviceError(
                f"No Thorlabs controller with serial {self.serial} was found. "
                "Close the Kinesis application if it is open (a controller in use by another "
                "program is not listed), and check that it is powered and connected by USB."
            )
        device = None
        try:
            device = getattr(controller, family.factory)(self.serial)
            device.Connect(self.serial)
        except Exception as error:
            if device is not None:
                with contextlib.suppress(Exception):
                    device.Disconnect(True)
            raise DeviceError(
                f"Cannot connect to Thorlabs {self.serial}: {_message(error)} "
                "If the Kinesis application is open, close it and try again."
            ) from None
        self._device = device
        try:
            # The order follows the Thorlabs pythonnet examples: poll, then wait for settings.
            time.sleep(0.25)
            device.StartPolling(POLL_MS)
            time.sleep(0.25)
            if not device.IsSettingsInitialized():
                device.WaitForSettingsInitialized(SETTINGS_TIMEOUT_MS)
            if read_only:
                # Read the settings stored in the controller; push nothing to it.
                use = DeviceConfiguration.DeviceSettingsUseOptionType.UseDeviceSettings
                configuration = device.LoadMotorConfiguration(self.serial, use)
            else:
                # The settings that the Kinesis application uses for this serial (DDS100).
                configuration = device.LoadMotorConfiguration(self.serial)
            if not read_only:
                device.EnableDevice()
                time.sleep(0.25)
            # No speed is set here: the worker sets it, after its checks, once READY.
            info = device.GetDeviceInfo()
            limits = device.AdvancedMotorLimits
            lower = to_float(limits.LengthMinimum)
            upper = to_float(limits.LengthMaximum)
            try:
                max_velocity: float | None = to_float(limits.VelocityMaximum) or None
            except Exception:
                max_velocity = None
            stage_name = str(configuration.DeviceSettingsName or "").strip()
        except DeviceError:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise DeviceError(f"Thorlabs {self.serial}: {_message(error)}") from None
        description = str(info.Description or family.product).strip()
        model = " · ".join(p for p in (description, stage_name) if p)
        return StageInfo(
            model=model,
            address=f"SN {self.serial}",
            units=self._units,
            lower=lower,
            upper=upper,
            max_velocity=max_velocity,
        )

    def close(self) -> None:
        device, self._device = self._device, None
        if device is None:
            return
        for step in (device.StopPolling, lambda: device.Disconnect(True)):
            with contextlib.suppress(Exception):
                step()

    def read(self) -> Reading:
        device = self._require()
        try:
            status = device.Status
            position = to_float(device.Position)
            homed, homing = bool(status.IsHomed), bool(status.IsHoming)
            moving, enabled = bool(status.IsInMotion), bool(status.IsEnabled)
            error = bool(status.IsError)
            details = []
            if error:
                details.append("Controller reports an error; reset it")
            if status.IsAtForwardHWLimit:
                details.append("At forward limit switch")
            if status.IsAtBackwardHWLimit:
                details.append("At backward limit switch")
        except Exception as error:
            raise DeviceError(
                f"Lost contact with Thorlabs {self.serial}: {_message(error)}"
            ) from None
        if homing:
            state = State.HOMING
        elif moving:
            state = State.MOVING
        elif error:
            state = State.ERROR  # blocks moves until a reset clears it
        elif not enabled:
            state = State.DISABLED
        elif not homed:
            state = State.NOT_HOMED
        else:
            state = State.READY
        return Reading(position=position, state=state, detail="; ".join(details))

    def move_to(self, position: float) -> None:
        self._call("move", lambda d: d.MoveTo(to_decimal(position), 0))

    def home(self) -> None:
        self._call("home", lambda d: d.Home(0))

    def stop(self) -> None:
        try:
            self._call("stop", lambda d: d.Stop(0))
        except DeviceError:
            self._call("stop", lambda d: d.StopImmediate())

    def get_velocity(self) -> float:
        device = self._require()
        try:
            return to_float(device.GetVelocityParams().MaxVelocity)
        except Exception as error:
            raise DeviceError(
                f"Thorlabs {self.serial}: cannot read the speed: {_message(error)}"
            ) from None

    def set_velocity(self, velocity: float) -> None:
        """The move speed; the acceleration stays as it is."""

        def apply(device: Any) -> None:
            acceleration = device.GetVelocityParams().Acceleration
            device.SetVelocityParams(to_decimal(velocity), acceleration)

        self._call("the speed", apply)

    def reset(self) -> None:
        def reset(device: Any) -> None:
            status = device.Status
            if status.IsInMotion or status.IsHoming:
                # Disabling a direct-drive stage in motion lets the carriage coast.
                raise DeviceError("the stage is moving. Press Stop and wait for it to stop.")
            device.ClearDeviceExceptions()
            device.DisableDevice()
            time.sleep(0.5)
            device.EnableDevice()
            time.sleep(0.25)

        self._call("reset", reset)

    def _require(self) -> Any:
        if self._device is None:
            raise DeviceError(f"Thorlabs {self.serial} is not connected.")
        return self._device

    def _call(self, what: str, action: Any) -> None:
        device = self._require()
        try:
            action(device)
        except Exception as error:
            raise DeviceError(f"Thorlabs {self.serial} refused {what}: {_message(error)}") from None
