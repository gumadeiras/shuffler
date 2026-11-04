# shuffler

Simple control for the rig's motorized stages. It replaces the Newport CONEX-CC
application (two controllers) and the Thorlabs Kinesis application (one K-Cube)
with one window.

- A card for each stage: the live position, a travel bar, Go to, jog − and +,
  Home, Stop, and Reset.
- **Stop all** in the top-right corner, or press **Esc** from anywhere in the window,
  including an open dialog. Esc is reserved for Stop; use Cancel to close a dialog.
- Presets: save the current positions under a name, and go back to them later.
- Safe ranges: optional `min` and `max` for each stage. Moves outside them are refused.

The look follows [sniffler](https://github.com/gumadeiras/sniffler): the same
palette, Fusion style, and icon set.

### The fly

A fruit fly sits at the top left and follows the stages:

| The stages | The fly |
|---|---|
| none connected | sleeps (z z) |
| still | sits; after a move or a home ends, it grooms (rubs its front legs) |
| one moves or homes | shuffles from side to side, wings buzzing, eyes and wings pink |
| Stop all | the escape jump: it springs up with its wings out, then lands |

Click it and it grooms. The fly is drawn on the GUI thread from the same status that
the cards show; it never waits for a stage or delays a command, and it uses no CPU
while it sits still. With Windows animation effects off (Settings > Accessibility >
Visual effects), or `SHUFFLER_REDUCE_MOTION=1`, nothing moves: the eyes and wings
turn pink while a stage moves.

## Hardware on this rig

| Name in `stages.toml` | Controller | Connection | Stage |
|---|---|---|---|
| CONEX A | Newport CONEX-CC | COM3 (FTDI, 921600 baud) | read on connect (`ID?`) |
| CONEX B | Newport CONEX-CC | COM4 | read on connect |
| DDS100 | Thorlabs KBD101 K-Cube | USB, serial 28253849 | DDS100 (100 mm direct drive) |

### Name each axis and make + mean forward, up, or right

Double-click a stage title (or use **⋯ → Name and direction…**):

- **Name**: "X", "Odor Z", and so on.
- **Direction**: left / right, down / up, back / forward, or out / in. The words
  appear on the − and + buttons and at the ends of the travel bar. + is always the
  second word: right, up, forward, or in.
- **Reverse this stage**: press + and watch the stage. If it moves the wrong way
  (back instead of forward), check Reverse. Then + moves it forward, and the position
  counts from the other end of the travel, so a larger number is always further
  forward. Only the display changes; the controller and the presets keep the
  controller's own coordinates, so reversing never changes what a preset does.

Use **⋯ → Move card left / right** to put the cards in order. These settings are kept
in `axes.json` and come back the next time the window opens. The card subtitle still
shows the port, the `stages.toml` name, and "reversed", so the mapping stays visible.

## Set up (once)

You need [uv](https://docs.astral.sh/uv/) and, for the K-Cube, Thorlabs Kinesis
(64-bit) in `C:\Program Files\Thorlabs\Kinesis`. Nothing from Newport needs to be
installed: shuffler talks to the CONEX-CC through its COM port.

```powershell
cd shuffler
uv sync
copy stages.toml.example stages.toml   # then edit names, ports, and limits
```

## Use

**Close the Newport CONEX-CC and Thorlabs Kinesis applications first.** A COM port
or a K-Cube can be open in only one program at a time. shuffler tells you which
program to close if it cannot connect.

### A taskbar icon

```powershell
uv run shuffler shortcut
```

This makes a **shuffler** shortcut in the Start menu and on the desktop, with the fly
icon (`shuffler.ico`). Open Start, right-click **shuffler**, and choose **Pin to
taskbar** (or right-click the open window's taskbar button). The shortcut starts
`.venv\Scripts\shufflerw.exe`, which opens the window without a console. Windows does
not pin a shortcut to a `.bat` file, and the shortcut and the window share an app ID
(`ClarkLab.shuffler`), so the pinned icon and the open window are one taskbar button.
`uv run shuffler icon` writes only the `.ico`.

Or double-click `shuffler.bat`, or:

```powershell
uv run shuffler-gui            # uses stages.toml in this folder
uv run shuffler-gui --demo     # simulated stages; no hardware is touched
```

1. Press **Connect all** (or Connect on one card). Nothing connects by itself; set
   `connect_on_start = true` in `stages.toml` to connect when the window opens. When
   every stage is connected, the button turns red and reads **Disconnect all** (it
   asks first if a stage is moving, and stops it before it disconnects).
2. A stage that shows **Not homed** must be homed before it can move. Home moves the
   stage to its home position at one end of its travel, so check that the path is
   clear. A CONEX-CC loses its home after power-off or a reset.
3. Type a target and press **Enter** (or **Go**), or use **−** and **+** with a step
   size. Repeated jogs add up exactly (they continue from the last target). The bar
   under the position shows where the stage is in its travel; it is a readout only.
4. **Speed** sets how fast moves and jogs go (mm/s), when the stage is homed and
   still. Type a value and press **Enter** or **Set**. After the controller accepts
   the speed, shuffler saves it and applies it again each time the stage is ready
   after a connect, home, or reset (the
   controllers forget it). A speed more than twice the current one asks first.
   Homing uses the controller's own homing speed. See "Speed limits" below.
5. **Presets → Save current…** stores the positions of the stages you pick. A pin
   marks the preset that the stages are at now. Double-click a preset to go to it:
   a dialog shows each move (now, target, change) and you can leave stages out.

### Safety rules

- A move needs a connected, homed, idle stage, and a target inside the allowed range:
  the controller's travel, narrowed by `min` and `max` in `stages.toml`.
- A new move is refused while the stage moves. Press Stop first. After a command,
  the stage counts as moving until the controller confirms the motion (Kinesis
  reports its status up to 100 ms late), so a double-click cannot send two moves.
- Home on a CONEX-CC that is already homed resets it first (it accepts a home search
  only when not referenced). It never resets one that is homing or moving. After a
  reset it waits until the controller has settled, and after the home search it
  checks that homing started: on the rig, a home search sent right after a reset was
  accepted but never started. If it does not start, it is sent once more, and then
  the card says so. Every state change a controller reports is written to
  `shuffler.log`.
- Stop clears queued moves, then stops the stage. It also cancels a CONEX home
  while the controller reboots.
- Reset is refused while the stage moves. Disabling the DDS100 in motion would let
  the carriage coast.
- If a controller stops answering, shuffler sends a stop before it disconnects.
  New motion commands are blocked after a failed poll until a reading succeeds.
  The card controls and preset preview show the same block. Stale positions are
  not offered for saving or used to mark the current preset.
- A Kinesis controller error blocks moves until you reset it.
- Going to a preset sends moves to the checked stages without waiting for one move
  to finish before sending the next. Each stage is checked again after confirmation
  and by its worker before it moves. A blocked stage is skipped; a later failure can
  stop one stage from moving while others move. Presets do not guarantee that every
  checked stage will move.
- Closing the window while a stage moves asks first, then stops the stage.
  Stops are queued for every stage before the window waits for any worker to close.
- Positions are checked twice: shuffler refuses a target outside the allowed range
  (it never clamps one silently), and the controller refuses a target outside its own
  limits (CONEX: SL/SR, error G; Kinesis: the stage travel). Presets are checked again
  when you go to them.
- shuffler never writes controller configuration (no CONEX configuration mode, no
  home-type, stored-limit, acceleration, or homing-speed changes). On a K-Cube it loads
  the stage settings that Kinesis uses for it (DDS100) and enables the motor, as the
  Kinesis application does. `shuffler probe` writes nothing.

### Speed limits

- Every speed (typed, saved, or `velocity` in `stages.toml`) must be more than 0 and
  at most the ceiling: the controller's reported maximum (K-Cube) and `max_velocity`
  in `stages.toml`, whichever is lower.
- `max_velocity` is also enforced on the controller's own speed: when a stage is ready
  and the controller runs faster (a speed left by another program, or a CONEX's
  stored speed after a reset), shuffler lowers it before any move. Set it for the
  DDS100; its controller allows 500 mm/s.
- When a speed ceiling is set, moves are blocked until the controller reports a
  positive speed within that ceiling. If the speed cannot be read or lowered, set a
  valid speed before moving. The speed field remains available for this recovery.
- If a speed reading fails, shuffler tries to read it again once per second while
  the stage is ready. These retries do not change the speed. When a valid reading
  returns, the move controls and preset preview use it at the next window refresh.
  A failed read after an accepted startup speed does not replace that speed with
  `max_velocity`; moves stay blocked until the speed is verified.
- A saved speed in `axes.json` takes precedence over `velocity` in `stages.toml`.
  A configured startup speed is saved only after the controller accepts it. A refused
  startup value does not become a saved override of later configuration changes.
- A CONEX-CC also refuses a speed above the maximum stored in its configuration
  (manual, VA: error C). The CONEX does not report that maximum, so the first time you
  ask for too much, the controller refuses it and the speed does not change.
- A CONEX-CC accepts a speed only when homed and still (manual, VA: errors H and M),
  so shuffler sends it only then. Every CONEX command reads the controller's error
  register before and after it, so a refused command is reported as refused, and
  only that one.

## Diagnostics (nothing moves)

```powershell
uv run shuffler ports    # COM ports and Thorlabs serial numbers
uv run shuffler probe    # connect to each stage, read model, travel, position, state
```

A log of every command is written to `shuffler.log` next to `stages.toml`.

### When something goes wrong

Every error says what happened and what to do next. The card shows it in pink. The
most common ones:

| The card says | Cause | Do this |
|---|---|---|
| DC voltage too low (Home is grayed out) | CONEX-CC without its 24 V supply. USB powers only the communication | Plug in and switch on the 24 V supply. Home comes back by itself |
| … is in use by another program | The Newport or Kinesis application has the port | Close it, then press Connect |
| No Thorlabs controller with serial … | K-Cube off, unplugged, or open in Kinesis | Switch it on, close Kinesis, press Connect |
| Following error, current limit, short circuit | Something blocks the stage, or a cable is loose | Clear the path, check the cable, then ⋯ > Reset controller |
| Lost contact | USB cable or power lost | A stop was already sent. Fix the cable, press Connect |

A CONEX-CC that refuses a command is asked for its status at once, so a hardware
error is named instead of the controller's "command not allowed".

## Files

| Path | Purpose |
|---|---|
| `stages.toml` | this rig's stages (git-ignored; start from `stages.toml.example`) |
| `presets.json` | saved positions (git-ignored; rewritten atomically) |
| `axes.json` | stage names, directions, Reverse, speed, and card order, set in the window (git-ignored) |
| `src/shuffler/conex.py` | Newport CONEX-CC ASCII protocol (pyserial) |
| `src/shuffler/kinesis.py` | Thorlabs K-Cubes (KBD101, KDC101, KST101) via Kinesis .NET |
| `src/shuffler/worker.py` | one thread per stage; all motion rules |
| `src/shuffler/presets.py` | preset storage |
| `src/shuffler/axes.py` | names, directions, and the Reverse mirror |
| `src/shuffler/gui/` | the window |
| `src/shuffler/gui/fly.py` | the fly and the window icon (drawn with QPainter, no image files) |
| `src/shuffler/fakes.py` | simulated stage for `--demo` and the tests |

Invalid settings and preset files are kept unchanged and cannot be written until
they are repaired or moved away. Values must use their JSON types: for example,
`"reversed": false`, with no quotes around `false`. Preset positions must be finite
numbers, preset names must be unique, and the card order cannot repeat a stage.

## Development

The GUI uses `PySide6-Essentials`, which supplies all Qt modules used by the app
and tests, including SVG rendering. This keeps unused Qt add-ons, PDF, and browser
engine packages out of the environment.

```powershell
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests
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
