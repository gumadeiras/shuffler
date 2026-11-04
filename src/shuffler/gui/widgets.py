"""Small widgets with sizes that box layouts can trust.

Qt's word-wrapped QLabel reports its height through height-for-width. One such label
anywhere in the window puts the whole window layout into height-for-width mode, and
in that mode Qt gave the presets panel its preferred height and squeezed the cards
below their minimum: the presets panel was drawn over the cards' Home and Stop
buttons. ``WrapLabel`` wraps at a width that its owner sets and reports an ordinary
size hint, so the layout keeps every minimum.
"""

from PySide6.QtCore import QLocale, QSize, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QMouseEvent, QResizeEvent
from PySide6.QtWidgets import QLabel, QLineEdit, QSizePolicy, QWidget

from shuffler.gui import theme


class WrapLabel(QLabel):
    """Word-wrapped text, wrapped at ``wrap_width``; its size hint is the wrapped size.

    The horizontal size policy is Ignored, so the text never makes its container wider.
    An empty label keeps one line of height unless ``collapse`` is set, in which case
    it hides itself.
    """

    def __init__(self, text: str = "", *, collapse: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._wrap_width = 200
        self._collapse = collapse
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def set_wrap_width(self, width: int) -> None:
        width = max(width, 40)
        if width != self._wrap_width:
            self._wrap_width = width
            self.updateGeometry()

    def setText(self, text: str) -> None:
        if self._collapse:
            self.setVisible(bool(text.strip()))
        super().setText(text if text.strip() else " ")
        self.updateGeometry()

    def hasHeightForWidth(self) -> bool:
        return False  # the whole point: the size hint already holds the wrapped height

    def sizeHint(self) -> QSize:
        line = self.fontMetrics().height()
        height = super().heightForWidth(self._wrap_width) if self.text().strip() else line
        margins = self.contentsMargins()
        return QSize(self._wrap_width, max(height, line) + margins.top() + margins.bottom())

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


class TitleLabel(WrapLabel):
    """A stage name. Double-click it to change the name and direction."""

    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.double_clicked.emit()
        event.accept()


class ElidedLabel(QLabel):
    """One line that ends in "…" when there is no room; the tooltip holds the full text.

    Used for the status line, so a long message cannot make the window wider.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str) -> None:
        self._full = text
        self._elide()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(self.width(), 0))
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


class NumberField(QLineEdit):
    """A typed number. No spin arrows: a stray click must not change a value.

    The validator accepts numbers only, with a period as the decimal point on every PC.
    The range goes in the tooltip but is not enforced here: a value outside it reaches
    the worker, which refuses it with a clear message instead of changing it silently.
    The minimum width fits a four-digit number with its decimals and a sign.
    """

    def __init__(self, decimals: int, unit: str, placeholder: str) -> None:
        super().__init__()
        self._decimals = decimals
        self._unit = unit
        self._range = (-1e9, 1e9)
        validator = QDoubleValidator(-1e9, 1e9, decimals, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        validator.setLocale(QLocale.c())
        self.setValidator(validator)
        self.setFont(theme.number_font(theme.BODY_PX + 1))
        self.setPlaceholderText(placeholder)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        sample = "-0000." + "0" * decimals
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance(sample) + 22)

    def setRange(self, low: float, high: float, what: str = "a target") -> None:
        self._range = (low, high)
        self.setToolTip(f"Type {what} from {low:g} to {high:g} {self._unit} and press Enter.")

    def minimum(self) -> float:
        return self._range[0]

    def maximum(self) -> float:
        return self._range[1]

    def setValue(self, value: float) -> None:
        self.setText(f"{value:.{self._decimals}f}")

    def value(self) -> float | None:
        try:
            return float(self.text())
        except ValueError:
            return None
