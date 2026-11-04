"""The CONEX-CC driver against a simulated controller on a fake serial port."""

import re
import unittest

import serial

from shuffler.conex import ConexStage, parse_status
from shuffler.stage import DeviceError, State

# Commands that would change stored configuration. The driver must never send them.
FORBIDDEN = re.compile(r"^1(PW|HT|SL-?\d|SR-?\d|OH|ZT|SA)")


class FakeConexPort:
    """Answers like a CONEX-CC at address 1. Moves finish at once."""

    def __init__(self, **_settings: object) -> None:
        self.written: list[str] = []
        self.buffer = b""
        self.version = "CONEX-CC 2.0.1"
        self.state = "32"
        self.flags = 0
        self.position = 1.5
        self.stored_velocity = 2.0  # the configured maximum, and the speed after a reboot
        self.velocity = 2.0
        self.error = "@"
        self.silent = False
        self.late = False  # the next reply arrives after the read times out
        self.closed = False

    def write(self, data: bytes) -> int:
        assert data.endswith(b"\r\n")
        command = data.decode("ascii").strip()
        self.written.append(command)
        if not self.silent:
            reply = self._answer(command[1:])
            if reply is not None:
                line = f"1{reply}\r\n".encode("ascii")
                if self.late:
                    self.late, self.held = False, line
                else:
                    self.buffer += line
        return len(data)

    def read_until(self, terminator: bytes = b"\n") -> bytes:
        held = getattr(self, "held", None)
        if held is not None:  # time out now; the reply lands in the buffer afterwards
            self.held = None
            data, self.buffer = self.buffer, held
            return data
        index = self.buffer.find(terminator)
        if index < 0:
            data, self.buffer = self.buffer, b""
            return data
        data, self.buffer = self.buffer[: index + 2], self.buffer[index + 2 :]
        return data

    def reset_input_buffer(self) -> None:
        self.buffer = b""

    def close(self) -> None:
        self.closed = True

    def _answer(self, code: str) -> str | None:
        if code == "VE":
            return f"VE {self.version}"
        if code == "ID?":
            return "ID TRB25CC_SN12345"
        if code == "SL?":
            return "SL0"
        if code == "SR?":
            return "SR25"
        if code == "TP":
            return f"TP{self.position}"
        if code == "TS":
            return f"TS{self.flags:04X}{self.state}"
        if code == "TE":
            error, self.error = self.error, "@"
            return f"TE{error}"
        if code.startswith("PA"):
            if self.state.startswith("0"):
                self.error = "H"
            elif not 0 <= float(code[2:]) <= 25:
                self.error = "G"
            else:
                self.position, self.state = float(code[2:]), "33"
            return None
        if code == "OR":
            if self.state.startswith("0"):
                self.position, self.state = 0.0, "32"
            else:
                self.error = "K"
            return None
        if code == "RS":
            self.state = "0A"
            self.velocity = self.stored_velocity  # a reboot restores the stored speed
            return None
        if code == "ST":
            if self.state != "28":
                self.error = "D"
            return None
        if code == "VA?":
            return f"VA{self.velocity}"
        if code.startswith("VA"):  # allowed in READY (and DISABLE/CONFIGURATION) only
            if self.state.startswith("0"):
                self.error = "H"
            elif self.state == "28":
                self.error = "M"
            elif not 1e-6 < float(code[2:]) <= self.stored_velocity:
                self.error = "C"  # up to the value stored in CONFIGURATION
            else:
                self.velocity = float(code[2:])
            return None
        self.error = "A"
        return None


class ConexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.port = FakeConexPort()
        self.now = 0.0

        def clock() -> float:
            return self.now

        def sleep(seconds: float) -> None:
            self.now += seconds

        self.stage = ConexStage(
            "COM3", serial_factory=lambda **_: self.port, sleep=sleep, clock=clock
        )

    def tearDown(self) -> None:
        bad = [c for c in self.port.written if FORBIDDEN.match(c)]
        self.assertEqual(bad, [], "the driver sent a configuration command")

    def test_open_reads_identity_and_limits(self) -> None:
        self.port.error = "C"  # left over from another program
        info = self.stage.open()
        self.assertIn("CONEX-CC 2.0.1", info.model)
        self.assertIn("TRB25CC", info.model)
        self.assertEqual((info.lower, info.upper), (0.0, 25.0))
        self.assertEqual(info.address, "COM3")
        self.assertEqual(self.port.error, "@", "open clears a stale command error")

    def test_open_uses_conex_serial_settings(self) -> None:
        captured: dict[str, object] = {}

        def factory(**settings: object) -> FakeConexPort:
            captured.update(settings)
            return self.port

        ConexStage("COM3", serial_factory=factory).open()
        self.assertEqual(captured["baudrate"], 921600)
        self.assertTrue(captured["xonxoff"])

    def test_open_refuses_another_device(self) -> None:
        self.port.version = "SMC100"
        with self.assertRaisesRegex(DeviceError, "not a CONEX"):
            self.stage.open()
        self.assertTrue(self.port.closed)

    def test_port_in_use_names_the_newport_app(self) -> None:
        def busy(**_: object) -> None:
            raise serial.SerialException(
                "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"
            )

        with self.assertRaisesRegex(DeviceError, "in use by another program"):
            ConexStage("COM3", serial_factory=busy).open()

    def test_silent_controller(self) -> None:
        self.port.silent = True
        with self.assertRaisesRegex(DeviceError, "No answer"):
            self.stage.open()

    def test_read_position_and_state(self) -> None:
        self.stage.open()
        self.port.position, self.port.state, self.port.flags = 12.3456, "28", 0x0020
        reading = self.stage.read()
        self.assertEqual(reading.position, 12.3456)
        self.assertIs(reading.state, State.MOVING)
        self.assertIn("Following error", reading.detail)

    def test_move_absolute(self) -> None:
        self.stage.open()
        self.stage.move_to(12.5)
        self.assertIn("1PA12.500000", self.port.written)
        self.assertEqual(self.port.written[-1], "1TE")

    def test_refused_move_is_reported(self) -> None:
        self.stage.open()
        self.port.state = "0A"
        with self.assertRaisesRegex(
            DeviceError, "refused PA: not allowed before the stage is homed"
        ):
            self.stage.move_to(1.0)

    def test_home_when_not_referenced_does_not_reset(self) -> None:
        self.port.state = "0A"
        self.stage.open()
        self.stage.home()
        self.assertNotIn("1RS", self.port.written)
        self.assertIn("1OR", self.port.written)
        self.assertEqual(self.port.state, "32")

    def test_home_when_ready_resets_first(self) -> None:
        self.stage.open()
        self.stage.home()
        sent = self.port.written
        self.assertLess(sent.index("1RS"), sent.index("1OR"))
        self.assertEqual(self.port.state, "32")

    def test_stop_when_idle_is_not_an_error(self) -> None:
        self.stage.open()
        self.stage.stop()
        self.assertIn("1ST", self.port.written)

    def test_late_reply_does_not_shift_later_replies(self) -> None:
        self.stage.open()
        self.port.position, self.port.state = 7.5, "33"
        self.port.late = True
        with self.assertRaisesRegex(DeviceError, "No answer"):
            self.stage.read()
        reading = self.stage.read()
        self.assertEqual(reading.position, 7.5)
        self.assertIs(reading.state, State.READY)
        self.port.state = "0A"
        with self.assertRaisesRegex(DeviceError, "not allowed before the stage is homed"):
            self.stage.move_to(1.0)  # the TE read belongs to this PA, not to an old query

    def test_no_velocity_is_sent_while_not_referenced(self) -> None:
        """VA is refused while NOT REFERENCED (manual: error H). Connect and the reset
        before a home search must not send it."""
        self.port.state = "0A"  # just powered on
        self.stage.open()
        self.port.state = "32"
        self.stage.home()  # homed stage: RS, then OR
        self.assertFalse([c for c in self.port.written if c.startswith("1VA")])
        self.assertLess(self.port.written.index("1RS"), self.port.written.index("1OR"))

    def test_speed_up_to_the_stored_maximum(self) -> None:
        self.stage.open()
        self.stage.set_velocity(1.0)
        self.assertEqual(self.stage.get_velocity(), 1.0)
        with self.assertRaisesRegex(DeviceError, "refused VA: parameter missing or out of range"):
            self.stage.set_velocity(5.0)  # above the configured maximum of 2
        self.assertEqual(self.stage.get_velocity(), 1.0, "a refused value changes nothing")

    def test_speed_is_refused_before_homing(self) -> None:
        self.port.state = "0A"
        self.stage.open()
        with self.assertRaisesRegex(DeviceError, "not allowed before the stage is homed"):
            self.stage.set_velocity(1.0)

    def test_a_stale_error_is_not_blamed_on_the_next_command(self) -> None:
        """TE keeps an error until read; a command that works does not clear it."""
        self.stage.open()
        self.port.error = "H"  # left by a refused query
        self.stage.move_to(3.0)  # accepted: no error may be reported
        self.assertEqual(self.port.position, 3.0)

    def test_stop_cancels_a_home_before_the_search_starts(self) -> None:
        self.stage.open()
        self.stage.abort.set()  # Stop pressed while the controller reboots
        with self.assertRaisesRegex(DeviceError, "cancelled by Stop"):
            self.stage.home()
        self.assertNotIn("1OR", self.port.written)

    def test_connect_writes_nothing(self) -> None:
        """Connecting only reads: VE, ID?, SL?, SR? and TE."""
        self.stage.open()
        codes = {c[1:3] for c in self.port.written}
        self.assertLessEqual(codes, {"VE", "ID", "SL", "SR", "TE"})


class ParseStatusTest(unittest.TestCase):
    def test_states(self) -> None:
        self.assertIs(parse_status("00000A")[0], State.NOT_HOMED)
        self.assertIs(parse_status("00001E")[0], State.HOMING)
        self.assertIs(parse_status("000033")[0], State.READY)
        self.assertIs(parse_status("00003C")[0], State.DISABLED)
        self.assertIs(parse_status("000014")[0], State.ERROR)

    def test_unknown_state_is_an_error(self) -> None:
        state, detail = parse_status("000099")
        self.assertIs(state, State.ERROR)
        self.assertIn("99", detail)

    def test_flags(self) -> None:
        _, detail = parse_status("000332")
        self.assertIn("At positive end of run", detail)
        self.assertIn("At negative end of run", detail)

    def test_bad_reply(self) -> None:
        with self.assertRaises(DeviceError):
            parse_status("XYZ")


if __name__ == "__main__":
    unittest.main()
