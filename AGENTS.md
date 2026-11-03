# AGENTS.md

`stagehand` controls the rig's motorized stages: two Newport CONEX-CC controllers
(COM3, COM4) and one Thorlabs KBD101 K-Cube with a DDS100 stage (serial 28253849).
Mass flow controllers will be added later, following sniffler's Alicat code.

## Rules

- **Never move a stage while you develop or test.** No move, home, jog, reset, or enable
  on real hardware. Read-only checks (`uv run stagehand ports`, `uv run stagehand probe`)
  are allowed. Verify motion logic with `fakes.FakeStage` and `--demo`.
- Only the driver modules (`conex.py`, `kinesis.py`) talk to devices. Every motion rule
  (homed, idle, in range) lives in `worker.py`, so the GUI and the tests share it.
- Never send CONEX configuration commands (PW, HT, SL/SR with a value, ...). The test
  `test_conex.FORBIDDEN` checks this.
- Expected device failures raise `DeviceError` with text for the user; no tracebacks
  in the window.
- Keep the sniffler look (`gui/theme.py`). The contrast test checks every text pair.
- Use ASD-STE100 Simplified Technical English in user text.

## Validation

```text
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests
```
