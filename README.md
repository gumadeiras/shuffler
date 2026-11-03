# stagehand

Simple control for the rig's motorized stages. It replaces the Newport CONEX-CC
application (two controllers) and the Thorlabs Kinesis application (one K-Cube)
with one window.

- A card for each stage: the live position, a travel bar, Go to, jog − and +,
  Home, Stop, and Reset.
- **Stop all** in the top-right corner, or press **Esc** from anywhere in the window.
- Presets: save the current positions under a name, and go back to them later.
- Safe ranges: optional `min` and `max` for each stage. Moves outside them are refused.

The look follows [sniffler](https://github.com/gumadeiras/sniffler): the same
palette, Fusion style, and icon set.

## Hardware on this rig

| Name in `stages.toml` | Controller | Connection | Stage |
|---|---|---|---|
| CONEX A | Newport CONEX-CC | COM3 (FTDI, 921600 baud) | read on connect (`ID?`) |
| CONEX B | Newport CONEX-CC | COM4 | read on connect |
| DDS100 | Thorlabs KBD101 K-Cube | USB, serial 28253849 | DDS100 (100 mm direct drive) |

Rename the stages in `stages.toml` to say what each one moves (for example
"Odor X"). Do this before you save presets: a preset stores positions by stage name.

## Set up (once)

You need [uv](https://docs.astral.sh/uv/) and, for the K-Cube, Thorlabs Kinesis
(64-bit) in `C:\Program Files\Thorlabs\Kinesis`. Nothing from Newport needs to be
installed: stagehand talks to the CONEX-CC through its COM port.

```powershell
cd stagehand
uv sync
copy stages.toml.example stages.toml   # then edit names, ports, and limits
```

## Use

**Close the Newport CONEX-CC and Thorlabs Kinesis applications first.** A COM port
or a K-Cube can be open in only one program at a time. stagehand tells you which
program to close if it cannot connect.

Double-click `stagehand.bat`, or:

```powershell
uv run stagehand-gui            # uses stages.toml in this folder
uv run stagehand-gui --demo     # simulated stages; no hardware is touched
```

1. The stages connect when the window opens.
2. A stage that shows **Not homed** must be homed before it can move. Home moves the
   stage to its home position at one end of its travel, so check that the path is
   clear. A CONEX-CC loses its home after power-off or a reset.
3. Type a target and press **Enter** (or **Go**), or use **−** and **+** with a step
   size. Repeated jogs add up exactly (they continue from the last target).
4. **Presets → Save current…** stores the positions of the stages you pick. A pin
   marks the preset that the stages are at now. Double-click a preset to go to it:
   a dialog shows each move (now, target, change) and you can leave stages out.

### Safety rules

- A move needs a connected, homed, idle stage, and a target inside the allowed range:
  the controller's travel, narrowed by `min` and `max` in `stages.toml`.
- A new move is refused while the stage moves. Press Stop first. After a command,
  the stage counts as moving until the controller confirms the motion (Kinesis
  reports its status up to 100 ms late), so a double-click cannot send two moves.
- Stop clears queued moves, then stops the stage. It also cancels a CONEX home
  while the controller reboots.
- Reset is refused while the stage moves. Disabling the DDS100 in motion would let
  the carriage coast.
- If a controller stops answering, stagehand sends a stop before it disconnects.
- A Kinesis controller error blocks moves until you reset it.
- Going to a preset moves the checked stages at the same time.
- Closing the window while a stage moves asks first, then stops the stage.
- stagehand never writes controller configuration (no CONEX configuration mode, no
  home-type or stored-limit changes). An optional `velocity` in `stages.toml` sets the
  working speed when the stage connects.

## Diagnostics (nothing moves)

```powershell
uv run stagehand ports    # COM ports and Thorlabs serial numbers
uv run stagehand probe    # connect to each stage, read model, travel, position, state
```

A log of every command is written to `stagehand.log` next to `stages.toml`.

## Files

| Path | Purpose |
|---|---|
| `stages.toml` | this rig's stages (git-ignored; start from `stages.toml.example`) |
| `presets.json` | saved positions (git-ignored; rewritten atomically) |
| `src/stagehand/conex.py` | Newport CONEX-CC ASCII protocol (pyserial) |
| `src/stagehand/kinesis.py` | Thorlabs K-Cubes (KBD101, KDC101, KST101) via Kinesis .NET |
| `src/stagehand/worker.py` | one thread per stage; all motion rules |
| `src/stagehand/presets.py` | preset storage |
| `src/stagehand/gui/` | the window |
| `src/stagehand/fakes.py` | simulated stage for `--demo` and the tests |

## Development

```powershell
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests
```

The tests use simulated stages and the offscreen Qt platform. They never open a port.

### Adding the mass flow controllers later

Add a driver module next to `conex.py`, a `[[mfc]]` table in `config.py`, and a
worker like `worker.py`, which owns one device on its own thread. sniffler's Alicat
code (`hardware.py`, `mfc_worker.py`) is the model.
