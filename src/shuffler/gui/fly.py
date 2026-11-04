"""The fruit fly in the top bar: identity while the stages are still, motion while they move.

The fly is drawn with QPainter (no image files), seen from above with the head to the
right, in the app palette: a cream body with navy stripes, glassy wings, and red-pink
eyes. What it does follows the stages:

    every stage still       it sits; after a move ends it grooms once (front legs rub)
    a stage moves or homes  it shuffles from side to side and its wings buzz; eyes go pink
    Stop all                the escape jump: it springs up, wings out, and lands
    no stage connected      it sleeps ("z z")
    a click on the fly      it grooms

Like sniffler's squirrel, the animation runs only on the GUI thread, from the window's
status snapshots; it never waits for a stage and never delays a command. The frame timer
runs only while something animates. Under reduced motion nothing moves: the eyes and
wings turn pink while a stage moves (the static equivalent of the shuffle).
"""

import math
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QWidget

from shuffler.gui import theme
from shuffler.stage import State

FRAME_MS = 33
GROOM_S = 1.6
JUMP_S = 0.5
FLASH_S = 0.6  # reduced motion: how long the eyes light after Stop all
SWAY_S = 1.0  # one side-to-side shuffle
STEP_S = 0.3  # one tripod step
BUZZ_S = 0.12  # one wing beat (drawn as a blur, not frame by frame)
RUB_S = 0.25  # one leg rub
SWAY_PX = 5.0

# The drawing is in body units: the thorax is at the origin, the head points to +x.
# HEIGHT is the vertical extent the widget must show (legs and spread wings).
HEIGHT = 1.0
FOLDED_DEG = 12.0
BUZZ_DEG = 10.0
JUMP_DEG = 42.0

# (root, knee, foot) of the legs on the +y side; the -y side is the mirror image.
LEGS = (
    ((0.08, 0.07), (0.20, 0.22), (0.34, 0.30)),  # front
    ((0.00, 0.10), (0.02, 0.30), (-0.08, 0.42)),  # middle
    ((-0.07, 0.08), (-0.20, 0.26), (-0.40, 0.36)),  # hind
)
# Tripod gait: front and hind of one side step with the middle leg of the other side.
TRIPOD = {(0, 1): 1, (1, 1): -1, (2, 1): 1, (0, -1): -1, (1, -1): 1, (2, -1): -1}


def reduce_motion() -> bool:
    """Read the platform reduce-motion setting. SHUFFLER_REDUCE_MOTION overrides it."""
    setting = os.environ.get("SHUFFLER_REDUCE_MOTION")
    if setting is not None:
        return setting.strip().lower() not in {"", "0", "false", "no"}
    if sys.platform == "win32":
        try:
            import ctypes

            animation = ctypes.c_int(1)
            spi_getclientareaanimation = 0x1042
            if ctypes.windll.user32.SystemParametersInfoW(
                spi_getclientareaanimation, 0, ctypes.byref(animation), 0
            ):
                return animation.value == 0
        except (OSError, AttributeError):
            pass
    return False


@dataclass(frozen=True)
class Pose:
    """One frame of the fly. Times are phases in turns (0..1 is one cycle)."""

    sway: float = 0.0  # -1..1, side to side
    tilt: float = 0.0  # degrees; the heading swings with the sway
    step: float | None = None  # walking phase, or None when standing
    rub: float | None = None  # grooming phase, or None
    wings: float = FOLDED_DEG  # degrees out from the body
    buzz: bool = False  # draw the wing blur
    lift: float = 0.0  # 0 on the ground, 1 at the top of a jump
    live: bool = False  # pink eyes and wings: a stage moves


def paint_fly(painter: QPainter, pose: Pose, *, ink: str = theme.NAVY) -> None:
    """Draw the fly at the origin, one body unit = one painter unit, head to +x."""
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    line = QPen(QColor(ink), 0.03, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    eye_color = QColor(theme.PINK if pose.live else theme.PINK_TEXT)

    # Shadow: further away and fainter while the fly is in the air.
    shadow = QColor(theme.NAVY)
    shadow.setAlphaF(0.10 * (1.0 - 0.5 * pose.lift))
    painter.setPen(Qt.NoPen)
    painter.setBrush(shadow)
    offset = 0.05 + 0.18 * pose.lift
    painter.drawEllipse(QPointF(-0.12 + offset, offset), 0.46, 0.2)

    scale = 1.0 + 0.12 * pose.lift
    painter.scale(scale, scale)

    # Legs, under the body.
    painter.setPen(line)
    painter.setBrush(Qt.NoBrush)
    for index, (root, knee, foot) in enumerate(LEGS):
        for side in (1, -1):
            knee_x, knee_y, foot_x, foot_y = knee[0], knee[1] * side, foot[0], foot[1] * side
            if pose.step is not None:
                shift = 0.05 * TRIPOD[(index, side)] * math.sin(2 * math.pi * pose.step)
                knee_x += shift / 2
                foot_x += shift
            if index == 0 and pose.rub is not None:
                # Grooming: the front feet meet in front of the head and rub.
                rub = 0.03 * side * math.sin(2 * math.pi * pose.rub)
                knee_x, knee_y = 0.31, 0.24 * side
                foot_x, foot_y = 0.43 + rub, 0.03 * side
            path = QPainterPath(QPointF(root[0], root[1] * side))
            path.lineTo(knee_x, knee_y)
            path.lineTo(foot_x, foot_y)
            painter.drawPath(path)

    # Body: abdomen with stripes, thorax, head.
    body = QColor(theme.CREAM)
    painter.setBrush(body)
    abdomen = QPainterPath()
    abdomen.addEllipse(QPointF(-0.30, 0.0), 0.24, 0.15)
    painter.drawPath(abdomen)
    painter.save()
    painter.setClipPath(abdomen)
    stripe = QPen(QColor(ink), 0.045)
    painter.setPen(stripe)
    for x in (-0.25, -0.36, -0.47):
        painter.drawLine(QPointF(x, -0.2), QPointF(x - 0.02, 0.2))
    painter.restore()
    painter.setPen(line)
    painter.drawEllipse(QPointF(0.0, 0.0), 0.15, 0.12)
    painter.drawEllipse(QPointF(0.21, 0.0), 0.08, 0.10)
    for side in (1, -1):
        painter.drawLine(QPointF(0.28, 0.03 * side), QPointF(0.33, 0.065 * side))  # antenna
    painter.setBrush(eye_color)
    for side in (1, -1):
        painter.drawEllipse(QPointF(0.22, 0.085 * side), 0.07, 0.055)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.PANEL))
    for side in (1, -1):
        painter.drawEllipse(QPointF(0.245, 0.07 * side), 0.016, 0.016)  # the shine

    # Wings, last: glassy, so the stripes show through.
    if pose.buzz:
        _paint_wings(painter, pose.wings + BUZZ_DEG, ink, pose.live, opacity=0.35)
    _paint_wings(painter, pose.wings, ink, pose.live, opacity=1.0)
    painter.restore()


def _paint_wings(
    painter: QPainter, degrees: float, ink: str, live: bool, *, opacity: float
) -> None:
    edge = QColor(theme.PINK if live else ink)
    edge.setAlphaF(0.85 * opacity)
    fill = QColor(theme.PINK_WASH if live else theme.PANEL)
    fill.setAlphaF(0.6 * opacity)
    painter.setPen(QPen(edge, 0.022))
    painter.setBrush(fill)
    for side in (1, -1):
        painter.save()
        painter.translate(-0.02, 0.05 * side)
        # The wing points back (-x); a positive angle swings it out to its own side.
        painter.rotate(-degrees * side)
        painter.drawEllipse(QPointF(-0.30, 0.0), 0.32, 0.095)
        painter.drawLine(QPointF(-0.04, 0.0), QPointF(-0.52, 0.0))  # the main vein
        painter.restore()


def window_icon() -> QIcon:
    """The fly on a navy tile, drawn at run time."""
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.NAVY))
        painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        painter.translate(size * 0.53, size * 0.5)
        painter.scale(size * 0.95, size * 0.95)
        paint_fly(painter, Pose(wings=22.0), ink=theme.CREAM)
        painter.end()
        result.addPixmap(pixmap)
    return result


class FlyWidget(QWidget):
    """The fly, fed with the stage states at each window refresh."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        reduced: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self.reduced_motion = reduce_motion() if reduced is None else reduced
        self._clock = clock
        self._busy = False
        self._asleep = True
        self._groom_from: float | None = None
        self._jump_from: float | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._frame)
        self.setAccessibleName("shuffler fly")
        self.setToolTip("shuffler")
        self._describe()

    # State ---------------------------------------------------------------

    @property
    def mode(self) -> str:
        """What the fly does now: asleep, rest, shuffle, or groom."""
        if self._busy:
            return "shuffle"
        if self._grooming():
            return "groom"
        return "asleep" if self._asleep else "rest"

    @property
    def jumping(self) -> bool:
        return self._jump_from is not None and self._clock() - self._jump_from < self._jump_s()

    @property
    def animating(self) -> bool:
        return self._timer.isActive()

    def show_states(self, states: Iterable[State]) -> None:
        """Follow the stages. Called at every window refresh; cheap when nothing changes."""
        states = list(states)
        busy = any(s.busy for s in states)
        asleep = not any(s.connected for s in states)
        if busy == self._busy and asleep == self._asleep:
            return
        move_ended = self._busy and not busy and not asleep
        self._busy, self._asleep = busy, asleep
        if busy:
            self._groom_from = None
        if move_ended:
            self.groom()  # the fly tidies up
        self._describe()
        self._run()
        self.update()

    def groom(self) -> None:
        if self._busy or self.reduced_motion:
            return
        self._groom_from = self._clock()
        self._run()

    def startle(self) -> None:
        """Stop all: the escape jump (under reduced motion, a short flash of the eyes)."""
        self._jump_from = self._clock()
        if self.reduced_motion:
            QTimer.singleShot(int(FLASH_S * 1000) + 10, self.update)
        self._run()
        self.update()

    def pose(self) -> Pose:
        now = self._clock()
        live = self._busy or (self.reduced_motion and self.jumping)
        if self.reduced_motion:
            return Pose(live=live)
        lift = 0.0
        if self.jumping:
            lift = math.sin(math.pi * (now - self._jump_from) / JUMP_S)
        wings = FOLDED_DEG + (JUMP_DEG - FOLDED_DEG) * lift
        if self._busy:
            return Pose(
                sway=math.sin(2 * math.pi * now / SWAY_S),
                tilt=6.0 * math.cos(2 * math.pi * now / SWAY_S),
                step=now / STEP_S,
                wings=wings + BUZZ_DEG * abs(math.sin(2 * math.pi * now / BUZZ_S)),
                buzz=True,
                lift=lift,
                live=True,
            )
        if self._grooming():
            return Pose(rub=(now - self._groom_from) / RUB_S, wings=wings, lift=lift)
        return Pose(wings=wings, lift=lift)

    def _grooming(self) -> bool:
        return self._groom_from is not None and self._clock() - self._groom_from < GROOM_S

    def _jump_s(self) -> float:
        return FLASH_S if self.reduced_motion else JUMP_S

    def _run(self) -> None:
        if self.reduced_motion:
            return
        if (self._busy or self._grooming() or self.jumping) and not self._timer.isActive():
            self._timer.start()

    def _frame(self) -> None:
        if not (self._busy or self._grooming() or self.jumping):
            self._timer.stop()
            self._groom_from = None
            self._jump_from = None
        self.update()

    def _describe(self) -> None:
        self.setAccessibleDescription(
            {
                "shuffle": "A stage is moving.",
                "groom": "Every stage is still.",
                "rest": "Every stage is still.",
                "asleep": "No stage is connected.",
            }[self.mode]
        )

    # Qt ------------------------------------------------------------------

    def sizeHint(self) -> QSize:
        return QSize(80, 48)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.groom()
        super().mousePressEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pose = self.pose()
        unit = self.height() / HEIGHT
        centre_x = 0.66 * unit + SWAY_PX + SWAY_PX * pose.sway
        painter.save()
        painter.translate(centre_x, self.height() / 2)
        painter.rotate(pose.tilt)
        painter.scale(unit, unit)
        paint_fly(painter, pose)
        painter.restore()
        if self.mode == "asleep":
            # Above and right of the head.
            painter.setPen(QColor(theme.INK_SOFT))
            painter.setFont(theme.font(theme.SMALL_PX, bold=True))
            left = centre_x + 0.36 * unit
            painter.drawText(QPointF(left, self.height() * 0.34), "z")
            painter.setFont(theme.font(theme.SMALL_PX - 3, bold=True))
            painter.drawText(QPointF(left + 9, self.height() * 0.18), "z")
        painter.end()
