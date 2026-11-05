# Development

[Back to the README](../README.md)

The look follows [sniffler](https://github.com/gumadeiras/sniffler): the same
palette, Fusion style, and icon set.

## Files

| Path | Purpose |
|---|---|
| `stages.toml` | this rig's controller connections and settings (tracked) |
| `presets.json` | this rig's saved positions (tracked; rewritten atomically) |
| `axes.json` | stage names, directions, Reverse, speed, and card order, set in the window (tracked) |
| `src/shuffler/conex.py` | Newport CONEX-CC ASCII protocol (pyserial) |
| `src/shuffler/kinesis.py` | Thorlabs K-Cubes (KBD101, KDC101, KST101) via Kinesis .NET |
| `src/shuffler/worker.py` | one thread per stage; all motion rules |
| `src/shuffler/presets.py` | preset storage |
| `src/shuffler/axes.py` | names, directions, and the Reverse mirror |
| `src/shuffler/gui/` | the window |
| `src/shuffler/gui/fly.py` | the fly and the window icon (drawn with QPainter, no image files) |
| `src/shuffler/fakes.py` | simulated stage for `--demo` and the tests |

The app updates `axes.json` and `presets.json` when you save changes in the window.
Review those changes before you commit them.

Invalid settings and preset files are kept unchanged and cannot be written until
they are repaired or moved away. Values must use their JSON types: for example,
`"reversed": false`, with no quotes around `false`. Preset positions must be finite
numbers, preset names must be unique, and the card order cannot repeat a stage.

## Checks

The GUI uses `PySide6-Essentials`, which supplies all Qt modules used by the app
and tests, including SVG rendering. This keeps unused Qt add-ons, PDF, and browser
engine packages out of the environment.

```powershell
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests
uv build
```

The tests use simulated stages and the offscreen Qt platform. They never open a port.
`gui/fit.py` checks that no text is cut and no control reaches outside its card; a
test runs it at the smallest window size.

### Command and status flow

`config.py` reads `stages.toml`. `rig.py` selects a driver, and one `StageWorker`
owns each driver on its own thread. Only the drivers communicate with hardware.

The card converts displayed targets and jog directions to controller coordinates
once, then queues the command. Presets already contain controller coordinates.
The worker checks each command immediately before sending it to the driver. Stop
sets the abort flag, removes queued motion commands, and queues a stop.

After each command, or about every 100 ms when idle, the worker reads the controller
and publishes an immutable `StageStatus`. It includes contact validity, the effective
speed ceiling, the last accepted speed request, and the shared motion-blocking
reason. The window reads these snapshots every 100 ms to update the cards, presets,
and fly. It saves accepted speed requests, not pending startup values or speed
readbacks. Unknown speeds retry on a separate one-second deadline in the worker.

### Adding the mass flow controllers later

Add a driver module next to `conex.py`, a `[[mfc]]` table in `config.py`, and a
worker like `worker.py`, which owns one device on its own thread. sniffler's Alicat
code (`hardware.py`, `mfc_worker.py`) is the model.

## README images

`assets/shuffler-ui.png` shows the real window with simulated stages and a temporary
copy of the rig files. `assets/fly.gif` uses the app's `FlyWidget` with a controlled
clock. Neither capture opens a hardware connection.
