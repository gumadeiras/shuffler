"""Diagnostics only. Nothing here moves a stage.

shuffler ports            list serial ports and Thorlabs controllers
shuffler probe            connect to each configured stage, read it, disconnect
shuffler icon [FILE]      write the app icon as a Windows .ico (default shuffler.ico)
shuffler shortcut         Start menu and desktop shortcuts that can be pinned to the taskbar
"""

import argparse
import sys
from pathlib import Path

from serial.tools import list_ports

from shuffler import config as config_module
from shuffler.config import ConfigError
from shuffler.rig import driver_factory
from shuffler.stage import DeviceError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shuffler", description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("stages.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ports", help="list serial ports and Thorlabs controllers")
    sub.add_parser("probe", help="read each configured stage without moving it")
    icon_parser = sub.add_parser("icon", help="write the app icon as a Windows .ico file")
    icon_parser.add_argument("file", type=Path, nargs="?", default=Path("shuffler.ico"))
    sub.add_parser("shortcut", help="make Start menu and desktop shortcuts you can pin")
    args = parser.parse_args(argv)
    if args.command == "ports":
        return ports(args.config)
    if args.command == "icon":
        return icon(args.file)
    if args.command == "shortcut":
        return shortcut()
    return probe(args.config)


def shortcut() -> int:
    """Start menu and desktop shortcuts that Windows can pin (see tools/install-shortcut.ps1)."""
    import subprocess

    if sys.platform != "win32":
        print("Shortcuts are made on Windows only.")
        return 1
    repo = Path(__file__).resolve().parents[2]
    from shuffler.gui.fly import write_ico

    write_ico(repo / "shuffler.ico")
    script = repo / "tools" / "install-shortcut.ps1"
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    return subprocess.call([*command, "-Repo", str(repo)])


def icon(path: Path) -> int:
    from shuffler.gui.fly import write_ico

    write_ico(path)
    print(f"Wrote {path.resolve()}")
    print("Shortcut icon: right-click the shortcut > Properties > Change Icon > Browse.")
    return 0


def ports(config_path: Path) -> int:
    print("Serial ports:")
    for port in sorted(list_ports.comports(), key=lambda p: p.device):
        print(f"  {port.device:6}  {port.description}  [{port.hwid}]")
    kinesis_dir = config_module.DEFAULT_KINESIS_DIR
    if config_path.exists():
        try:
            kinesis_dir = config_module.load(config_path).kinesis_dir
        except ConfigError as error:
            print(f"({error})")
    print("Thorlabs controllers:")
    try:
        from shuffler.kinesis import _load

        _load(kinesis_dir, "Thorlabs.MotionControl.DeviceManagerCLI")
        from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI

        DeviceManagerCLI.BuildDeviceList()
        serials = [str(s) for s in DeviceManagerCLI.GetDeviceList()]
    except (DeviceError, ImportError) as error:
        print(f"  not available: {error}")
        return 0
    for serial in serials or ["(none: a controller that Kinesis has open is not listed)"]:
        print(f"  {serial}")
    return 0


def probe(config_path: Path) -> int:
    try:
        app = config_module.load(config_path)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    make = driver_factory(app)
    failures = 0
    for stage in app.stages:
        print(f"{stage.name} ({stage.driver}, {stage.address})")
        try:
            driver = make(stage)
            info = driver.open(read_only=True)
        except DeviceError as error:
            print(f"  cannot connect: {error}")
            failures += 1
            continue
        try:
            reading = driver.read()
            print(f"  model     {info.model}")
            print(f"  travel    {info.lower:g} to {info.upper:g} {info.units}")
            position = "unknown" if reading.position is None else f"{reading.position:.6f}"
            print(f"  position  {position} {info.units}")
            print(
                f"  state     {reading.state.value}"
                + (f" ({reading.detail})" if reading.detail else "")
            )
            if reading.fault:
                print(f"  PROBLEM   {reading.fault}. Home and moves are blocked until then.")
        except DeviceError as error:
            print(f"  read failed: {error}")
            failures += 1
        finally:
            driver.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
