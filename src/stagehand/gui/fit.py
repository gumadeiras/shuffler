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
    """Every widget whose text does not fit, that reaches outside its parent, or that
    overlaps a sibling (one drawn over another, as when a panel covers a row of buttons).
    """
    problems = []
    widgets = [w for w in [root, *root.findChildren(QWidget)] if _shown(w, root)]
    for widget in widgets:
        if type(widget).__name__ == "ElidedLabel":
            continue
        problem = _check(widget) or _overflow(widget, root)
        if problem:
            problems.append(f"{type(widget).__name__} {problem}")
    for parent in widgets:
        if _in_scroll_area(parent, root):
            continue
        children = [c for c in parent.findChildren(QWidget, options=_direct()) if _shown(c, root)]
        for index, first in enumerate(children):
            for second in children[index + 1 :]:
                overlap = first.geometry().intersected(second.geometry())
                if overlap.width() > SLACK and overlap.height() > SLACK:
                    problems.append(
                        f"{_name(first)} and {_name(second)} overlap by "
                        f"{overlap.width()}x{overlap.height()} px"
                    )
    return problems


def _shown(widget: QWidget, root: QWidget) -> bool:
    return (
        widget.isVisibleTo(root)
        and widget.width() > 0
        and (widget is root or not widget.isWindow())
    )


def _direct():
    from PySide6.QtCore import Qt

    return Qt.FindDirectChildrenOnly


def _in_scroll_area(widget: QWidget, root: QWidget) -> bool:
    ancestor: QWidget | None = widget
    while ancestor is not None and ancestor is not root:
        if isinstance(ancestor, QAbstractScrollArea):
            return True
        ancestor = ancestor.parentWidget()
    return False


def _name(widget: QWidget) -> str:
    text = getattr(widget, "text", None)
    label = text() if callable(text) else widget.objectName()
    return f"{type(widget).__name__} {label!r}" if label else type(widget).__name__


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
