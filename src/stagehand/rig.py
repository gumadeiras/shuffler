"""Build the driver for each configured stage: real hardware or, in demo mode, a simulation."""

from collections.abc import Callable

from stagehand.conex import ConexStage
from stagehand.config import AppConfig, StageConfig
from stagehand.fakes import FakeStage
from stagehand.kinesis import KinesisStage
from stagehand.stage import StageDriver


def driver_factory(app: AppConfig, *, demo: bool = False) -> Callable[[StageConfig], StageDriver]:
    def make(stage: StageConfig) -> StageDriver:
        if demo:
            return demo_stage(stage)
        if stage.driver == "conex":
            assert stage.port is not None
            return ConexStage(stage.port, units=stage.units, velocity=stage.velocity)
        assert stage.serial is not None
        return KinesisStage(
            stage.serial, kinesis_dir=app.kinesis_dir, units=stage.units, velocity=stage.velocity
        )

    return make


def demo_stage(stage: StageConfig) -> FakeStage:
    """Travel and speed like the real controllers: CONEX 25 mm, DDS100 100 mm."""
    if stage.driver == "kinesis":
        return FakeStage(
            stage.address,
            model="Simulated KBD101 · DDS100",
            units=stage.units,
            upper=100.0,
            start=50.0,
            velocity=20.0,
        )
    return FakeStage(
        stage.address,
        model="Simulated CONEX-CC · 25 mm",
        units=stage.units,
        upper=25.0,
        start=12.5,
        velocity=2.0,
    )
