"""Kinesis error handling without loading .NET or opening a controller."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shuffler.kinesis import KinesisStage
from shuffler.stage import DeviceError, State


class KinesisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stage = KinesisStage("28253849", kinesis_dir=Path("unused"))
        self.status = SimpleNamespace(
            IsHomed=True,
            IsHoming=False,
            IsInMotion=False,
            IsEnabled=True,
            IsError=False,
            IsAtForwardHWLimit=False,
            IsAtBackwardHWLimit=False,
        )
        self.device = mock.Mock(Status=self.status, Position=12.5)
        self.stage._device = self.device

    def test_fault_blocks_moves_but_motion_stays_visible_until_stopped(self) -> None:
        self.status.IsError = True
        reading = self.stage.read()
        self.assertIs(reading.state, State.ERROR)
        self.assertIn("reset", reading.detail)
        for flag, state in (("IsInMotion", State.MOVING), ("IsHoming", State.HOMING)):
            with self.subTest(flag=flag):
                setattr(self.status, flag, True)
                self.assertIs(self.stage.read().state, state)
                setattr(self.status, flag, False)

    def test_stop_falls_back_to_immediate_stop_when_normal_stop_fails(self) -> None:
        self.device.Stop.side_effect = RuntimeError("normal stop failed")
        self.stage.stop()
        self.assertEqual(self.device.method_calls, [mock.call.Stop(0), mock.call.StopImmediate()])
        self.device.StopImmediate.side_effect = RuntimeError("USB disconnected")
        with self.assertRaisesRegex(DeviceError, "USB disconnected"):
            self.stage.stop()

    def test_reset_never_disables_a_moving_or_homing_stage(self) -> None:
        for flag in ("IsInMotion", "IsHoming"):
            with self.subTest(flag=flag):
                setattr(self.status, flag, True)
                with self.assertRaisesRegex(DeviceError, "Press Stop"):
                    self.stage.reset()
                self.device.DisableDevice.assert_not_called()
                self.device.ClearDeviceExceptions.assert_not_called()
                setattr(self.status, flag, False)

    def test_speed_change_keeps_the_existing_acceleration(self) -> None:
        self.device.GetVelocityParams.return_value = SimpleNamespace(Acceleration=7.0)
        with mock.patch("shuffler.kinesis.to_decimal", side_effect=lambda value: value):
            self.stage.set_velocity(2.0)
        self.device.SetVelocityParams.assert_called_once_with(2.0, 7.0)
        self.device.SetVelocityParams.side_effect = RuntimeError("speed refused")
        with (
            mock.patch("shuffler.kinesis.to_decimal", return_value=3.0),
            self.assertRaisesRegex(DeviceError, "speed refused"),
        ):
            self.stage.set_velocity(3.0)


if __name__ == "__main__":
    unittest.main()
