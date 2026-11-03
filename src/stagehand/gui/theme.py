"""The stagehand look: the sniffler palette, spacing, and type, so the rig's apps match.

Pink has one meaning: live attention (a stage in motion, the Stop control while
something moves, an error). Navy carries text and the primary controls; the cream
family carries surfaces. The window stays light and uses the Fusion style so it is
drawn the same way on every platform.
"""

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from stagehand.stage import State

ASSETS = Path(__file__).with_name("assets")

# The sniffler palette.
NAVY = "#122d58"
CREAM = "#f3d39b"
PINK = "#e83879"
PINK_TEXT = "#9e204e"
PINK_WASH = "#fbe1ea"
SURFACE = "#fbf5e8"
PANEL = "#ffffff"
INK_SOFT = "#4f5f7a"
DISABLED = "#5f6b85"  # darkened from sniffler's #76819a so disabled labels stay readable
LINE = "#d9d0bf"
GOLD = "#8a5f10"
# Additions for stagehand, derived from the same colors.
NAVY_WASH = "#e3e8f1"  # navy at low opacity over white: the Ready pill
GOLD_WASH = "#fbeccc"  # cream, a little stronger: the Not homed pill
TRACK = "#ece4d3"  # the travel bar track
PINK_FILL = "#c81e64"  # pink deepened until white text on it passes 4.5:1 (live pills, Stop)

UNIT = 8
GAP = UNIT
SECTION_GAP = 2 * UNIT
MARGIN = 2 * UNIT

BODY_PX = 13
SMALL_PX = 12
TITLE_PX = 18
DISPLAY_PX = 34

ICON_PX = 18
CARD_WIDTH = 300
CHECK_MARK = (ASSETS / "icons" / "check-white.svg").as_posix()
CHEVRON = (ASSETS / "icons" / "chevron-down-navy.svg").as_posix()
CHEVRON_DISABLED = (ASSETS / "icons" / "chevron-down-disabled.svg").as_posix()

# (text, background, border) for the state pill.
PILL: dict[State, tuple[str, str, str]] = {
    State.OFFLINE: (INK_SOFT, SURFACE, LINE),
    State.CONNECTING: (INK_SOFT, SURFACE, LINE),
    State.NOT_HOMED: (GOLD, GOLD_WASH, GOLD_WASH),
    State.HOMING: (PANEL, PINK_FILL, PINK_FILL),
    State.READY: (NAVY, NAVY_WASH, NAVY_WASH),
    State.MOVING: (PANEL, PINK_FILL, PINK_FILL),
    State.DISABLED: (INK_SOFT, SURFACE, LINE),
    State.ERROR: (PINK_TEXT, PINK_WASH, PINK_WASH),
}

# Every (foreground, background) pair that carries text. The contrast test reads this.
TEXT_PAIRS = {
    "body on window": (NAVY, SURFACE),
    "body on panel": (NAVY, PANEL),
    "secondary on panel": (INK_SOFT, PANEL),
    "attention text on panel": (PINK_TEXT, PANEL),
    "primary button text": (PANEL, NAVY),
    "live Stop button text": (PANEL, PINK_FILL),
    "demo badge": (PANEL, GOLD),
    "error text on panel": (PINK_TEXT, PANEL),
    "secondary on window": (INK_SOFT, SURFACE),
    "disabled text on window": (DISABLED, SURFACE),
    "disabled text on panel": (DISABLED, PANEL),
    "table text on striped row": (NAVY, SURFACE),
    "table secondary on striped row": (INK_SOFT, SURFACE),
    "direction word on panel": (GOLD, PANEL),
    **{f"pill {state.value}": (fg, bg) for state, (fg, bg, _) in PILL.items()},
}


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2 contrast ratio between two sRGB colors."""

    def luminance(color: str) -> float:
        channels = []
        for value in QColor(color).getRgb()[:3]:
            scaled = value / 255
            channels.append(
                scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4
            )
        red, green, blue = channels
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def font(size_px: int = BODY_PX, *, bold: bool = False) -> QFont:
    result = QFont(QApplication.font())
    result.setPixelSize(size_px)
    result.setBold(bold)
    return result


MONOSPACE = ("Cascadia Mono", "Consolas")  # the first one installed wins


def number_font(size_px: int = DISPLAY_PX) -> QFont:
    """A monospace font for numbers, so a readout does not jitter while the stage moves.

    The generic system fixed font on Windows is Courier New, which is thin and hard to
    read, so a modern monospace is chosen by name first.
    """
    family = next((f for f in MONOSPACE if QFontDatabase.hasFamily(f)), None)
    if family is None:
        result = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    else:
        result = QFont(family)
        result.setStyleHint(QFont.Monospace)
    result.setPixelSize(size_px)
    return result


def palette() -> QPalette:
    result = QPalette()
    result.setColor(QPalette.Window, QColor(SURFACE))
    result.setColor(QPalette.WindowText, QColor(NAVY))
    result.setColor(QPalette.Base, QColor(PANEL))
    result.setColor(QPalette.AlternateBase, QColor(SURFACE))
    result.setColor(QPalette.Text, QColor(NAVY))
    result.setColor(QPalette.PlaceholderText, QColor(INK_SOFT))
    result.setColor(QPalette.Button, QColor(SURFACE))
    result.setColor(QPalette.ButtonText, QColor(NAVY))
    result.setColor(QPalette.Highlight, QColor(NAVY))
    result.setColor(QPalette.HighlightedText, QColor(PANEL))
    result.setColor(QPalette.Link, QColor(PINK_TEXT))
    result.setColor(QPalette.ToolTipBase, QColor(NAVY))
    result.setColor(QPalette.ToolTipText, QColor(PANEL))
    result.setColor(QPalette.Light, QColor(PANEL))
    result.setColor(QPalette.Midlight, QColor(SURFACE))
    result.setColor(QPalette.Mid, QColor(LINE))
    result.setColor(QPalette.Dark, QColor(INK_SOFT))
    result.setColor(QPalette.Shadow, QColor(NAVY))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        result.setColor(QPalette.Disabled, role, QColor(DISABLED))
    return result


STYLE_SHEET = f"""
QToolTip {{ color: {PANEL}; background: {NAVY}; border: 1px solid {NAVY}; padding: 4px; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 10px; }}
QFrame#card QLabel {{ background: transparent; }}
QFrame#rule {{ background: {LINE}; border: 0; max-height: 1px; min-height: 1px; }}
QLabel#secondary {{ color: {INK_SOFT}; }}
QLabel#error {{ color: {PINK_TEXT}; font-weight: bold; }}
QLabel#demo {{ color: {PANEL}; background: {GOLD}; border-radius: 9px; padding: 2px 10px;
    font-weight: bold; }}
QPushButton, QToolButton {{ border: 1px solid {LINE}; border-radius: 6px; padding: 5px 12px;
    background: {PANEL}; color: {NAVY}; }}
QPushButton:hover, QToolButton:hover {{ border-color: {NAVY}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {NAVY_WASH}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {DISABLED}; background: {SURFACE};
    border-color: {LINE}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QPushButton#primary {{ background: {NAVY}; color: {PANEL}; border-color: {NAVY};
    font-weight: bold; }}
QPushButton#primary:hover {{ background: #1d3d70; }}
QPushButton#primary:disabled {{ background: {SURFACE}; color: {DISABLED}; border-color: {LINE}; }}
QPushButton#stop {{ font-weight: bold; }}
QPushButton#stop[live="true"] {{ background: {PINK_FILL}; color: {PANEL};
    border-color: {PINK_FILL}; }}
QPushButton#stopAll {{ font-weight: bold; padding: 8px 18px; background: {NAVY}; color: {PANEL};
    border-color: {NAVY}; }}
QPushButton#stopAll[live="true"] {{ background: {PINK_FILL}; border-color: {PINK_FILL}; }}
QComboBox, QLineEdit {{ border: 1px solid {LINE}; border-radius: 6px;
    padding: 4px 8px; background: {PANEL}; color: {NAVY}; }}
QComboBox:focus, QLineEdit:focus {{ border-color: {NAVY}; }}
QComboBox:disabled, QLineEdit:disabled {{ background: {SURFACE}; color: {DISABLED}; }}
QComboBox::drop-down {{ border: 0; width: 24px; }}
QComboBox::down-arrow {{ image: url({CHEVRON}); width: 14px; height: 14px; }}
QComboBox::down-arrow:disabled {{ image: url({CHEVRON_DISABLED}); }}
QComboBox QAbstractItemView {{ border: 1px solid {LINE}; background: {PANEL};
    selection-background-color: {NAVY}; selection-color: {PANEL}; outline: 0; }}
QHeaderView::section {{ background: {SURFACE}; color: {INK_SOFT}; border: 0;
    border-bottom: 1px solid {LINE}; padding: 6px 10px; font-weight: bold; }}
QTableWidget {{ border: 0; gridline-color: {LINE}; background: {PANEL};
    alternate-background-color: {SURFACE};
    selection-background-color: {NAVY}; selection-color: {PANEL}; }}
QTableWidget::item {{ padding: 0 10px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {NAVY}; border-radius: 3px;
    background: {PANEL}; }}
QCheckBox::indicator:checked {{ background: {NAVY}; image: url({CHECK_MARK}); }}
QCheckBox::indicator:disabled {{ border-color: {DISABLED}; }}
QStatusBar {{ color: {INK_SOFT}; padding-left: {MARGIN}px; }}
QStatusBar QLabel {{ color: {INK_SOFT}; }}
QMenu {{ background: {PANEL}; border: 1px solid {LINE}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px 6px 10px; color: {NAVY}; border-radius: 4px; }}
QMenu::item:selected {{ background: {NAVY_WASH}; }}
QMenu::item:disabled {{ color: {DISABLED}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 6px; }}
"""


def pill_style(state: State) -> str:
    text, background, border = PILL[state]
    return (
        f"color: {text}; background: {background}; border: 1px solid {border};"
        "border-radius: 10px; padding: 2px 10px; font-weight: bold;"
    )


def window_icon() -> QIcon:
    """A navy tile with a cream crosshair and a pink target dot, drawn at run time."""
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(NAVY))
        painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        pen = QPen(QColor(CREAM), max(1.0, size / 14), Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen)
        middle, reach = size / 2, size * 0.3
        painter.drawLine(QPointF(middle - reach, middle), QPointF(middle + reach, middle))
        painter.drawLine(QPointF(middle, middle - reach), QPointF(middle, middle + reach))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(PINK))
        painter.drawEllipse(QPointF(middle, middle), size * 0.11, size * 0.11)
        painter.end()
        result.addPixmap(pixmap)
    return result


def apply(application: QApplication) -> None:
    """Style, palette, font, and window icon. Call once before the window is built."""
    application.setStyle("Fusion")
    application.setPalette(palette())
    body = QFont(application.font())
    body.setPixelSize(BODY_PX)
    application.setFont(body)
    application.setStyleSheet(STYLE_SHEET)
    application.setWindowIcon(window_icon())
