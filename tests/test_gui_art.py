"""Fly animation, icon files, and theme checks on the shared Qt application."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gui_support import Clock
from PySide6.QtCore import QSize

from shuffler.gui import fly as fly_module
from shuffler.gui import theme
from shuffler.gui.fly import FlyWidget
from shuffler.stage import State


class FlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def fly(self, *, reduced: bool) -> FlyWidget:
        widget = FlyWidget(reduced=reduced, clock=self.clock)
        widget.resize(widget.sizeHint())
        return widget

    def test_shuffle_then_groom_then_rest(self) -> None:
        fly = self.fly(reduced=False)
        fly.show_states([State.READY, State.MOVING])
        self.assertEqual(fly.mode, "shuffle")
        self.assertTrue(fly.animating)
        self.assertTrue(fly.pose().live)
        fly.show_states([State.READY, State.READY])
        self.assertEqual(fly.mode, "groom", "the move ended: the fly tidies up")
        self.clock.now += fly_module.GROOM_S + 0.01
        fly._frame()
        self.assertEqual(fly.mode, "rest")
        self.assertFalse(fly.animating, "no frames while nothing animates")

    def test_no_groom_when_the_stages_disconnect(self) -> None:
        fly = self.fly(reduced=False)
        fly.show_states([State.MOVING])
        fly.show_states([State.OFFLINE])
        self.assertEqual(fly.mode, "asleep")

    def test_jump_ends(self) -> None:
        fly = self.fly(reduced=False)
        fly.show_states([State.READY])
        fly.startle()
        self.assertTrue(fly.jumping)
        self.assertTrue(fly.animating)
        self.clock.now += fly_module.JUMP_S / 2
        self.assertGreater(fly.pose().lift, 0.99, "the top of the jump")
        self.assertGreater(fly.pose().wings, fly_module.FOLDED_DEG + 20, "wings out")
        self.clock.now += fly_module.JUMP_S / 2 + 0.01
        fly._frame()
        self.assertFalse(fly.jumping)
        self.assertFalse(fly.animating)

    def test_reduced_motion_never_moves(self) -> None:
        fly = self.fly(reduced=True)
        fly.show_states([State.HOMING])
        self.assertFalse(fly.animating)
        self.assertEqual(fly.pose(), fly_module.Pose(live=True), "pink, and still")
        fly.show_states([State.READY])
        self.assertEqual(fly.mode, "rest", "no groom")
        fly.startle()
        self.assertFalse(fly.animating)
        self.assertTrue(fly.pose().live, "Stop all lights the eyes")

    def test_reduce_motion_setting(self) -> None:
        for value, expected in (("1", True), ("yes", True), ("0", False), ("", False)):
            with mock.patch.dict(os.environ, {"SHUFFLER_REDUCE_MOTION": value}):
                self.assertEqual(fly_module.reduce_motion(), expected)

    def test_every_mode_paints(self) -> None:
        fly = self.fly(reduced=False)
        for states in ([State.OFFLINE], [State.READY], [State.MOVING], [State.READY]):
            fly.show_states(states)
            self.clock.now += 0.37
            self.assertFalse(fly.grab().isNull())
        fly.startle()
        self.clock.now += fly_module.JUMP_S / 2
        self.assertFalse(fly.grab().isNull())

    def test_every_pose_stays_inside_the_widget(self) -> None:
        """The widget clips; the jump's spread wings were cut off at the top and bottom.

        Draw the widest poses without clipping, on a canvas with a margin around the
        widget, and check that nothing lands in the margin.
        """
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPainter

        widget = self.fly(reduced=False)
        margin = 20
        poses = [fly_module.Pose(wings=fly_module.JUMP_DEG, lift=1.0), fly_module.Pose(rub=0.25)]
        for sway in (-1.0, 1.0):
            for tilt in (-6.0, 6.0):
                poses.append(
                    fly_module.Pose(sway=sway, tilt=tilt, wings=fly_module.JUMP_DEG, lift=1.0)
                )
                wings = fly_module.FOLDED_DEG + fly_module.BUZZ_DEG
                poses.append(fly_module.Pose(sway=sway, tilt=tilt, wings=wings, buzz=True))
        for pose in poses:
            image = QImage(
                widget.width() + 2 * margin, widget.height() + 2 * margin, QImage.Format_ARGB32
            )
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.translate(margin, margin)
            widget.draw_fly(painter, pose)
            painter.end()
            outside = [
                (x - margin, y - margin)
                for y in range(image.height())
                for x in range(image.width())
                if not (
                    margin <= x < margin + widget.width() and margin <= y < margin + widget.height()
                )
                and image.pixelColor(x, y).alpha() > 8
            ]
            with self.subTest(pose=pose):
                self.assertEqual(outside[:3], [], "the fly reaches outside its widget")

    def test_ico_file(self) -> None:
        """A valid .ico: every size, 256 px as PNG, the rest as 32-bit bitmaps whose
        corners are transparent (the rounded tile) and whose centre is opaque."""
        import struct

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "shuffler.ico"
            fly_module.write_ico(path)
            data = path.read_bytes()
        reserved, kind, count = struct.unpack_from("<HHH", data)
        self.assertEqual((reserved, kind, count), (0, 1, len(fly_module.ICON_SIZES)))
        for index, size in enumerate(fly_module.ICON_SIZES):
            width, height, _, _, planes, bits, length, offset = struct.unpack_from(
                "<BBBBHHII", data, 6 + 16 * index
            )
            with self.subTest(size=size):
                self.assertEqual((width or 256, height or 256, planes, bits), (size, size, 1, 32))
                self.assertLessEqual(offset + length, len(data))
                blob = data[offset : offset + length]
                if size == 256:
                    self.assertTrue(blob.startswith(b"\x89PNG"))
                    continue
                self.assertEqual(struct.unpack_from("<Iii", blob), (40, size, size * 2))

                def alpha(x: int, y: int, blob: bytes = blob, size: int = size) -> int:
                    row = size - 1 - y  # bottom-up
                    return blob[40 + (row * size + x) * 4 + 3]

                self.assertEqual(alpha(0, 0), 0, "rounded corner")
                self.assertEqual(alpha(size // 2, size // 2), 255)

    def test_window_icon(self) -> None:
        sizes = fly_module.window_icon().availableSizes()
        self.assertIn(QSize(16, 16), sizes)
        self.assertIn(QSize(256, 256), sizes)


class ThemeTest(unittest.TestCase):
    def test_text_contrast(self) -> None:
        for label, (foreground, background) in theme.TEXT_PAIRS.items():
            with self.subTest(label):
                self.assertGreaterEqual(theme.contrast_ratio(foreground, background), 4.5)

    def test_every_state_has_a_pill(self) -> None:
        self.assertEqual(set(theme.PILL), set(State))

    def test_numbers_use_a_monospace_font(self) -> None:
        """A named monospace font where one is installed; the system fixed font otherwise.

        The offscreen test platform has no Windows fonts, so check what is asked for.
        """
        from PySide6.QtGui import QFont, QFontDatabase

        number = theme.number_font()
        if any(QFontDatabase.hasFamily(f) for f in theme.MONOSPACE):
            self.assertIn(number.family(), theme.MONOSPACE)
            self.assertEqual(number.styleHint(), QFont.Monospace)
        else:
            fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            self.assertEqual(number.family(), fixed.family())


if __name__ == "__main__":
    unittest.main()
