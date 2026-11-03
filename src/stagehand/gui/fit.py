"""Find text that does not fit its widget. The tests run this at the smallest window size.

A widget is cut when Qt gave it less room than its text needs: less than the size
hint for a label or button, less than the wrapped height for a word-wrapped label,
or a field narrower than its text. Labels that shorten their text with "…" on
purpose (``ElidedLabel``) are skipped.
"""

from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractScrollArea,
    QComboBox,
    QLabel,
    QLineEdit,
    QWidget,
)

SLACK = 1  # pixels of rounding


def find_cropped(root: QWidget) -> list[str]:
    problems = []
    for widget in [root, *root.findChildren(QWidget)]:
        if not widget.isVisibleTo(root) or widget.width() <= 0:
            continue
        if type(widget).__name__ == "ElidedLabel":
            continue
        problem = _check(widget) or _overflow(widget, root)
        if problem:
            problems.append(f"{type(widget).__name__} {problem}")
    return problems


def _overflow(widget: QWidget, root: QWidget) -> str:
    """A widget that reaches outside its parent is cut by the parent's edge.

    Children of scroll areas are skipped: they are larger than their viewport by design.
    """
    parent = widget.parentWidget()
    if widget is root or parent is None or widget.isWindow():
        return ""
    ancestor: QWidget | None = parent
    while ancestor is not None and ancestor is not root:
        if isinstance(ancestor, QAbstractScrollArea):
            return ""
        ancestor = ancestor.parentWidget()
    if isinstance(parent, QAbstractScrollArea):
        return ""
    if not parent.rect().adjusted(-SLACK, -SLACK, SLACK, SLACK).contains(widget.geometry()):
        name = getattr(widget, "text", lambda: "")()
        return f"{name!r} reaches outside its {type(parent).__name__}"
    return ""


def _check(widget: QWidget) -> str:
    if isinstance(widget, QLabel):
        text = widget.text()
        if not text.strip() or (widget.pixmap() is not None and not widget.pixmap().isNull()):
            return ""
        if widget.wordWrap():
            needed = widget.heightForWidth(widget.width())
            if needed > widget.height() + SLACK:
                return f"{text!r}: needs {needed} px high, has {widget.height()}"
            return ""
        hint = widget.sizeHint()
        if hint.width() > widget.width() + SLACK or hint.height() > widget.height() + SLACK:
            return f"{text!r}: needs {hint.width()}x{hint.height()}, has {_size(widget)}"
        return ""
    if isinstance(widget, QAbstractButton):
        hint = widget.sizeHint()
        if hint.width() > widget.width() + SLACK or hint.height() > widget.height() + SLACK:
            return f"{widget.text()!r}: needs {hint.width()}x{hint.height()}, has {_size(widget)}"
        return ""
    if isinstance(widget, QLineEdit):
        if widget.parent() is not None and isinstance(widget.parent(), QComboBox):
            return ""
        text = widget.text() or widget.placeholderText()
        margins = widget.textMargins()
        needed = (
            widget.fontMetrics().horizontalAdvance(text) + margins.left() + margins.right() + 16
        )
        if needed > widget.width() + SLACK:
            return f"{text!r}: needs {needed} px wide, has {widget.width()}"
        return ""
    if isinstance(widget, QComboBox):
        hint = widget.sizeHint()
        if hint.width() > widget.width() + SLACK:
            return f"{widget.currentText()!r}: needs {hint.width()} px wide, has {widget.width()}"
    return ""


def _size(widget: QWidget) -> str:
    return f"{widget.width()}x{widget.height()}"
