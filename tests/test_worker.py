"""The worker rules that protect the hardware, with a simulated stage and clock."""

import unittest

from stagehand.config import StageConfig
from stagehand.fakes import FakeStage
from stagehand.stage import Reading, State
from stagehand.worker import LATCH_S, READ_FAILURE_LIMIT, StageWorker


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

    def test_reset_requires_homing_again(self) -> None:
        worker, _ = self.connected_and_homed()
        worker.reset()
        self.run_all(worker)
        self.assertIs(worker.status.state, State.NOT_HOMED)


if __name__ == "__main__":
    unittest.main()
