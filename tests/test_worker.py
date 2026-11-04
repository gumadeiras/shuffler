"""The worker rules that protect the hardware, with a simulated stage and clock."""

import unittest
from unittest import mock

from shuffler import worker as worker_module
from shuffler.config import StageConfig
from shuffler.fakes import FakeStage
from shuffler.stage import DeviceError, Reading, State
from shuffler.worker import LATCH_S, READ_FAILURE_LIMIT, StageWorker


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class LaggingStage(FakeStage):
    """Reports the status from before the last command for a while, like Kinesis polling."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.stale: Reading | None = None
        self.stale_reads = 0

    def _freeze(self) -> None:
        self.stale = super().read()
        self.stale_reads = 3

    def move_to(self, position: float) -> None:
        self._freeze()
        super().move_to(position)

    def home(self) -> None:
        self._freeze()
        super().home()

    def read(self) -> Reading:
        if self.stale_reads:
            self.stale_reads -= 1
            return self.stale
        return super().read()


class WorkerTest(unittest.TestCase):
    def make(
        self, cls: type[FakeStage] = FakeStage, **config: object
    ) -> tuple[StageWorker, FakeStage]:
        self.clock = Clock()
        self.stage = cls("COM9", upper=25.0, start=10.0, velocity=10.0, clock=self.clock)
        settings = {"name": "X", "driver": "conex", "port": "COM9", **config}
        worker = StageWorker(StageConfig(**settings), lambda _config: self.stage, clock=self.clock)
        return worker, self.stage

    def run_all(self, worker: StageWorker) -> None:
        """Run every queued command (no thread), then poll once more."""
        while not worker._commands.empty():
            worker.tick(0)
        worker.tick(0)

    def settle(self, worker: StageWorker, seconds: float = 10.0) -> None:
        self.clock.now += seconds
        worker.tick(0)

    def connected_and_homed(self, **config: object) -> tuple[StageWorker, FakeStage]:
        worker, stage = self.make(**config)
        worker.connect()
        worker.home()
        self.run_all(worker)
        self.settle(worker)
        self.assertIs(worker.status.state, State.READY)
        return worker, stage

    def test_connect_reports_info_and_state(self) -> None:
        worker, _ = self.make()
        worker.connect()
        self.run_all(worker)
        status = worker.status
        self.assertIs(status.state, State.NOT_HOMED)
        self.assertEqual(status.position, 10.0)
        self.assertEqual(status.allowed, (0.0, 25.0))

    def test_connect_failure_is_shown(self) -> None:
        worker, stage = self.make()
        stage.fail_open = "COM9 is in use by another program."
        worker.connect()
        self.run_all(worker)
        self.assertIs(worker.status.state, State.OFFLINE)
        self.assertIn("in use", worker.status.error)

    def test_no_move_before_homing(self) -> None:
        worker, stage = self.make()
        worker.connect()
        worker.move_to(5.0)
        self.run_all(worker)
        self.assertIn("not homed", worker.status.error)
        self.assertFalse(any(c.startswith("move_to") for c in stage.commands))

    def test_a_fault_blocks_home_and_moves_and_clears_when_fixed(self) -> None:
        worker, stage = self.make()
        stage.fault = "DC voltage too low. Plug in the 24 V supply"
        worker.connect()
        self.run_all(worker)
        self.assertEqual(worker.status.fault, stage.fault)
        worker.home()
        self.run_all(worker)
        self.assertEqual(
            worker.status.error, "X: cannot home. DC voltage too low. Plug in the 24 V supply."
        )
        self.assertNotIn("home", stage.commands, "the controller is never asked")
        stage.fault = ""  # power restored
        worker.tick(0)
        self.assertEqual(worker.status.fault, "")
        worker.home()
        self.run_all(worker)
        self.assertIn("home", stage.commands)

    def test_a_fault_while_ready_blocks_moves(self) -> None:
        worker, stage = self.connected_and_homed()
        stage.fault = "Following error. Check the stage cable"
        worker.tick(0)
        worker.move_to(5.0)
        self.run_all(worker)
        self.assertIn("Following error", worker.status.error)
        self.assertFalse(any(c.startswith("move_to") for c in stage.commands))

    def test_home_then_move(self) -> None:
        worker, stage = self.connected_and_homed()
        self.assertEqual(worker.status.position, 0.0)
        worker.move_to(5.0)
        self.run_all(worker)
        self.assertIs(worker.status.state, State.MOVING)
        self.assertEqual(worker.status.target, 5.0)
        self.settle(worker)
        self.assertIs(worker.status.state, State.READY)
        self.assertEqual(worker.status.position, 5.0)
        self.assertIn("move_to 5", stage.commands)

    def test_target_outside_controller_travel_is_refused(self) -> None:
        worker, stage = self.connected_and_homed()
        worker.move_to(30.0)
        self.run_all(worker)
        self.assertIn("outside the allowed range", worker.status.error)
        self.assertNotIn("move_to 30", stage.commands)

    def test_configured_range_narrows_travel(self) -> None:
        worker, stage = self.connected_and_homed(min=2.0, max=20.0)
        self.assertEqual(worker.status.allowed, (2.0, 20.0))
        worker.move_to(21.0)
        self.run_all(worker)
        self.assertIn("2.0000 to 20.0000", worker.status.error)
        self.assertNotIn("move_to 21", stage.commands)

    def test_configured_range_outside_travel_blocks_moves(self) -> None:
        worker, _stage = self.make(min=30.0, max=40.0)
        worker.connect()
        self.run_all(worker)
        self.assertIsNone(worker.status.allowed)
        self.assertIn("outside the controller travel", worker.status.error)

    def test_no_new_move_while_moving(self) -> None:
        worker, stage = self.connected_and_homed()
        worker.move_to(20.0)
        self.run_all(worker)
        worker.move_to(1.0)
        self.run_all(worker)
        self.assertIn("moving", worker.status.error)
        self.assertNotIn("move_to 1", stage.commands)

    def test_repeated_jogs_add_up_exactly(self) -> None:
        worker, _ = self.connected_and_homed()
        worker.move_to(10.0)
        self.run_all(worker)
        self.settle(worker)
        for _ in range(3):
            worker.move_by(0.1)
            self.run_all(worker)
            self.settle(worker)
        self.assertEqual(worker.status.target, 10.3)
        self.assertEqual(worker.status.position, 10.3)

    def test_stop_drops_queued_moves(self) -> None:
        worker, stage = self.connected_and_homed()
        worker.move_to(20.0)
        worker.move_to(5.0)
        worker.stop()
        self.run_all(worker)
        self.assertNotIn("move_to 20", stage.commands)
        self.assertNotIn("move_to 5", stage.commands)
        self.assertEqual(stage.commands[-1], "stop")

    def test_stop_cancels_a_command_already_taken_from_the_queue(self) -> None:
        worker, stage = self.connected_and_homed()
        get_command = worker._get_nowait

        def stop_after_dequeue():
            command = get_command()
            worker.stop()
            return command

        for command in (worker.move_to, worker.move_by):
            with self.subTest(command=command.__name__):
                command(5.0)
                with mock.patch.object(worker, "_get_nowait", side_effect=stop_after_dequeue):
                    worker.tick(0)
                self.run_all(worker)
        self.assertFalse(any(c.startswith("move_to") for c in stage.commands))
        worker.move_to(5.0)  # a new command after Stop must still work
        self.run_all(worker)
        self.assertIn("move_to 5", stage.commands)

    def test_no_move_uses_a_reading_after_a_poll_failure(self) -> None:
        worker, stage = self.connected_and_homed()
        stage.fail_read = "No answer."
        worker.tick(0)
        worker.move_to(5.0)
        worker.tick(0)
        self.assertNotIn("move_to 5", stage.commands)
        self.assertIn("contact", worker.status.error)
        stage.fail_read = None
        worker.tick(0)
        worker.move_to(5.0)
        self.run_all(worker)
        self.assertIn("move_to 5", stage.commands)

    def test_snapshot_reports_the_same_motion_block_as_the_worker(self) -> None:
        worker, stage = self.connected_and_homed()
        self.assertEqual(worker.status.move_block_reason, "")
        stage.fail_read = "No answer."
        worker.tick(0)
        reason = worker.status.move_block_reason
        self.assertIn("contact", reason)
        worker.move_by(1.0)
        worker.tick(0)
        self.assertIn(reason, worker.status.error)
        stage.fail_read = None
        worker.tick(0)
        self.assertEqual(worker.status.move_block_reason, "")

    def test_stop_during_a_move(self) -> None:
        worker, _stage = self.connected_and_homed()
        worker.move_to(20.0)
        self.run_all(worker)
        self.clock.now += 0.5  # 5 mm into the move
        worker.stop()
        self.run_all(worker)
        self.assertIs(worker.status.state, State.READY)
        self.assertAlmostEqual(worker.status.position, 5.0)

    def test_lost_contact(self) -> None:
        worker, stage = self.connected_and_homed()
        stage.fail_read = "No answer from the CONEX-CC on COM9."
        for _ in range(READ_FAILURE_LIMIT):
            worker.tick(0)
        self.assertIs(worker.status.state, State.OFFLINE)
        self.assertIn("Lost contact", worker.status.error)
        self.assertFalse(stage.connected)

    def test_shutdown_stops_a_moving_stage(self) -> None:
        worker, stage = self.connected_and_homed()
        worker.move_to(20.0)
        self.run_all(worker)
        worker.shutdown()
        self.assertEqual(stage.commands[-2:], ["stop", "close"])
        self.assertIs(worker.status.state, State.OFFLINE)

    def test_stale_ready_after_a_move_does_not_allow_a_second_move(self) -> None:
        self.make(LaggingStage)
        worker = StageWorker(
            StageConfig(name="X", driver="kinesis", serial="28253849"),
            lambda _c: self.stage,
            clock=self.clock,
        )
        worker.connect()
        worker.home()
        self.run_all(worker)
        self.settle(worker)
        self.settle(worker)
        self.assertIs(worker.status.state, State.READY)
        worker.move_to(20.0)
        worker.move_to(1.0)  # double-click: the controller still reports READY
        self.run_all(worker)
        self.assertIs(worker.status.state, State.MOVING)
        self.assertIn("moving", worker.status.error)
        self.assertNotIn("move_to 1", self.stage.commands)

    def test_stale_ready_after_home_blocks_a_move(self) -> None:
        worker, stage = self.make(LaggingStage)
        worker.connect()
        self.run_all(worker)
        stage._homed = True  # homed before, as after an earlier session
        self.settle(worker)
        worker.home()
        worker.move_to(5.0)
        self.run_all(worker)
        self.assertIs(worker.status.state, State.HOMING)
        self.assertNotIn("move_to 5", stage.commands)

    def test_stale_position_does_not_complete_a_small_jog(self) -> None:
        worker, stage = self.make(LaggingStage)
        worker.connect()
        worker.home()
        self.run_all(worker)
        self.settle(worker)
        self.settle(worker)
        worker.move_by(0.001)
        worker.move_by(0.001)
        self.run_all(worker)
        self.assertEqual([c for c in stage.commands if c.startswith("move_to")], ["move_to 0.001"])
        self.assertIs(worker.status.state, State.MOVING)
        self.settle(worker, 0.1)
        self.assertIs(worker.status.state, State.READY)
        self.assertEqual(worker.status.position, 0.001)
        worker.move_by(0.001)
        self.run_all(worker)
        self.settle(worker, 0.1)
        worker.tick(0)  # the last stale report has now been consumed
        self.assertEqual(worker.status.position, 0.002)

    def test_latch_expires_when_the_controller_never_reports_motion(self) -> None:
        worker, stage = self.connected_and_homed()
        stage.velocity = 1e9  # the move ends before the first poll
        worker._execute("move_to", (20.0,))
        self.assertIs(worker.status.state, State.MOVING)
        self.clock.now += 0.001
        worker.tick(0)
        self.assertIs(worker.status.state, State.READY, "at the target: no latch needed")
        self.clock.now += LATCH_S
        worker.tick(0)
        self.assertIs(worker.status.state, State.READY)

    def test_reset_is_refused_while_moving(self) -> None:
        worker, stage = self.connected_and_homed()
        worker.move_to(20.0)
        self.run_all(worker)
        worker.reset()
        self.run_all(worker)
        self.assertIn("Press Stop", worker.status.error)
        self.assertNotIn("reset", stage.commands)

    def test_lost_contact_sends_a_stop(self) -> None:
        worker, stage = self.connected_and_homed()
        worker.move_to(20.0)
        self.run_all(worker)
        stage.fail_read = "No answer."
        for _ in range(READ_FAILURE_LIMIT):
            worker.tick(0)
        self.assertEqual(stage.commands[-2:], ["stop", "close"])
        self.assertIn("A stop was sent", worker.status.error)

    def test_stop_keeps_a_queued_disconnect(self) -> None:
        worker, stage = self.connected_and_homed()
        worker.move_to(20.0)
        worker.disconnect()
        worker.stop()
        self.run_all(worker)
        self.assertNotIn("move_to 20", stage.commands)
        self.assertIs(worker.status.state, State.OFFLINE)

    def test_unexpected_connect_error_returns_to_offline(self) -> None:
        def broken(_config: StageConfig) -> FakeStage:
            raise OSError("the DLL is blocked")

        worker = StageWorker(StageConfig(name="X", driver="conex", port="COM9"), broken)
        worker.connect()
        worker.tick(0)
        self.assertIs(worker.status.state, State.OFFLINE)
        self.assertIn("DLL is blocked", worker.status.error)

    def test_target_on_the_limit_is_clamped(self) -> None:
        worker, stage = self.connected_and_homed(decimals=6)
        worker.move_to(25.0000005)
        self.run_all(worker)
        self.assertIn("move_to 25", stage.commands)

    def test_set_speed(self) -> None:
        worker, stage = self.connected_and_homed()
        self.assertEqual(worker.status.velocity, 10.0)
        worker.set_velocity(2.5)
        self.run_all(worker)
        self.assertEqual(worker.status.velocity, 2.5)
        self.assertEqual(worker.speed, 2.5)
        self.assertIn("set_velocity 2.5", stage.commands)

    def test_speed_rules(self) -> None:
        worker, stage = self.connected_and_homed()
        stage.max_velocity = 50.0
        worker._set(info=stage.open())  # the controller reports its maximum
        for bad, message in ((0.0, "more than 0"), (-1.0, "more than 0"), (80.0, "maximum")):
            worker.set_velocity(bad)
            self.run_all(worker)
            self.assertIn(message, worker.status.error)
        worker.move_to(20.0)
        worker.set_velocity(3.0)
        self.run_all(worker)
        self.assertIn("moving", worker.status.error, "no speed change during a move")
        self.assertFalse(any(c.startswith("set_velocity") for c in stage.commands))

    def test_configured_max_velocity_is_a_ceiling(self) -> None:
        worker, stage = self.connected_and_homed(max_velocity=4.0)
        worker.set_velocity(6.0)
        self.run_all(worker)
        self.assertIn("above the maximum of 4", worker.status.error)
        self.assertNotIn("set_velocity 6", stage.commands)

    def test_a_faster_controller_speed_is_lowered_to_the_ceiling(self) -> None:
        """The controller starts at 10 (left by another program); max_velocity is 4. Moves
        must not use 10: the worker lowers it as soon as the stage is READY."""
        worker, stage = self.make(max_velocity=4.0)
        worker.connect()
        worker.home()
        self.run_all(worker)
        self.settle(worker)
        self.assertEqual(stage.velocity, 4.0)
        self.assertEqual(worker.status.velocity, 4.0)

    def test_moves_are_blocked_until_the_speed_ceiling_is_verified(self) -> None:
        for reported in (10.0, None, float("nan")):
            with self.subTest(reported=reported):
                worker, stage = self.make(max_velocity=4.0)
                stage._homed = True
                read_speed = (
                    {"side_effect": DeviceError("No speed reply.")}
                    if reported is None
                    else {"return_value": reported}
                )
                with (
                    mock.patch.object(stage, "set_velocity", side_effect=DeviceError("Refused.")),
                    mock.patch.object(stage, "get_velocity", **read_speed),
                ):
                    worker.connect()
                    self.run_all(worker)
                    worker.move_to(20.0)
                    self.run_all(worker)
                    self.assertNotIn("move_to 20", stage.commands)
                    self.assertIn("speed", worker.status.error)
                worker.set_velocity(2.0)
                self.run_all(worker)
                worker.move_to(20.0)
                self.run_all(worker)
                self.assertIn("move_to 20", stage.commands)
                self.assertEqual(stage.velocity, 2.0)

    def test_configured_velocity_obeys_the_ceiling_too(self) -> None:
        worker, stage = self.make(velocity=3.0)
        self.assertEqual(worker.speed, 3.0)
        self.assertIsNone(worker.status.accepted_speed)
        worker.connect()
        worker.home()
        self.run_all(worker)
        self.settle(worker)
        self.assertIn("set_velocity 3", stage.commands)
        self.assertEqual(worker.status.velocity, 3.0)
        self.assertEqual(worker.status.accepted_speed, 3.0)

    def test_unknown_speed_retries_at_a_bounded_interval(self) -> None:
        worker, stage = self.connected_and_homed(max_velocity=4.0)
        with mock.patch.object(
            stage, "get_velocity", side_effect=DeviceError("No speed reply.")
        ) as read:
            worker.set_velocity(2.0)
            worker.tick(0)
            self.assertIsNone(worker.status.velocity)
            self.assertIn("speed", worker.status.move_block_reason)
            for _ in range(3):
                worker.tick(0)
            self.clock.now += worker_module.VELOCITY_RETRY_S / 2
            worker.tick(0)
            self.assertEqual(read.call_count, 1)
            self.clock.now += worker_module.VELOCITY_RETRY_S / 2
            worker.tick(0)
            self.assertEqual(read.call_count, 2)
        with mock.patch.object(stage, "get_velocity", wraps=stage.get_velocity) as read:
            self.clock.now += worker_module.VELOCITY_RETRY_S
            worker.tick(0)
            self.assertEqual(worker.status.velocity, 2.0)
            self.assertEqual(worker.status.move_block_reason, "")
            self.clock.now += worker_module.VELOCITY_RETRY_S
            worker.tick(0)
            read.assert_called_once()
        self.assertEqual([c for c in stage.commands if c == "set_velocity 2"], ["set_velocity 2"])

    def test_failed_startup_read_does_not_replace_an_accepted_speed_with_the_ceiling(self) -> None:
        worker, stage = self.make(velocity=2.0, max_velocity=4.0)
        stage._homed = True
        with mock.patch.object(stage, "get_velocity", side_effect=DeviceError("No speed reply.")):
            worker.connect()
            self.run_all(worker)
        self.assertEqual(stage.commands, ["open", "set_velocity 2"])
        self.assertIsNone(worker.status.velocity)
        self.assertEqual(worker.status.accepted_speed, 2.0)
        self.assertIn("speed", worker.status.move_block_reason)
        self.clock.now += worker_module.VELOCITY_RETRY_S
        worker.tick(0)
        self.assertEqual(worker.status.velocity, 2.0)
        self.assertEqual(worker.status.move_block_reason, "")

    def test_speed_retry_waits_until_the_stage_is_ready(self) -> None:
        worker, stage = self.connected_and_homed()
        with mock.patch.object(stage, "get_velocity", side_effect=DeviceError("No speed reply.")):
            worker.set_velocity(2.0)
            worker.tick(0)
        worker.move_to(20.0)
        worker.tick(0)
        with mock.patch.object(stage, "get_velocity", wraps=stage.get_velocity) as read:
            self.clock.now += worker_module.VELOCITY_RETRY_S
            worker.tick(0)
            self.assertIs(worker.status.state, State.MOVING)
            read.assert_not_called()
            self.settle(worker)
            read.assert_called_once()
        self.assertEqual(worker.status.velocity, 2.0)

    def test_speed_needs_a_homed_still_stage(self) -> None:
        worker, stage = self.make()
        worker.connect()
        worker.set_velocity(2.0)
        self.run_all(worker)
        self.assertIn("not homed", worker.status.error)
        self.assertNotIn("set_velocity 2", stage.commands)

    def test_saved_speed_waits_for_ready(self) -> None:
        worker, stage = self.make()
        worker.speed = 1.5
        worker.connect()
        self.run_all(worker)
        self.assertNotIn("set_velocity 1.5", stage.commands, "not before homing")
        worker.home()
        self.run_all(worker)
        self.settle(worker)
        self.assertIn("set_velocity 1.5", stage.commands)
        self.assertEqual(worker.status.velocity, 1.5)

    def test_conex_rules_end_to_end(self) -> None:
        """The CONEX-CC sequence from the manual: VA only in READY, a reset before the home
        search restores the stored speed, and VA above the stored speed is refused."""
        self.clock = Clock()
        stage = FakeStage(
            "COM9", upper=25.0, start=10.0, velocity=2.0, conex=True, clock=self.clock
        )
        worker = StageWorker(
            StageConfig(name="X", driver="conex", port="COM9"), lambda _c: stage, clock=self.clock
        )
        worker.speed = 0.5  # saved in the window
        worker.connect()
        self.run_all(worker)
        self.assertIsNone(worker.status.velocity, "VA? is not asked before homing")
        self.assertEqual(worker.status.error, "", "nothing refused: no VA was sent")
        worker.home()
        self.run_all(worker)
        self.settle(worker, 30)
        self.assertEqual(stage.velocity, 0.5, "applied once READY")
        worker.home()  # homed: the controller resets first, back to its stored 2.0
        self.run_all(worker)
        self.settle(worker, 30)
        self.assertEqual(stage.velocity, 0.5, "applied again after the reset")
        worker.set_velocity(3.0)  # above the stored maximum
        self.run_all(worker)
        self.assertIn("refused VA", worker.status.error)
        self.assertEqual(stage.velocity, 0.5)
        self.assertEqual(worker.status.accepted_speed, 0.5)

    def test_reset_requires_homing_again(self) -> None:
        worker, _ = self.connected_and_homed()
        worker.reset()
        self.run_all(worker)
        self.assertIs(worker.status.state, State.NOT_HOMED)


if __name__ == "__main__":
    unittest.main()
