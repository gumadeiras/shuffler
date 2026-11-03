"""Name a stage and say which way + moves it."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from stagehand.axes import DIRECTIONS, MAX_LABEL, Axis
from stagehand.gui import theme

DIRECTION_TEXT = {
    "none": "No direction words (− and +)",
    "left-right": "Left and right",
    "down-up": "Down and up",
    "back-forward": "Back and forward",
    "out-in": "Out and in",
}


class AxisDialog(QDialog):
    def __init__(self, key: str, address: str, axis: Axis, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Stage name and direction")
        self.setMinimumWidth(420)

        where = QLabel(f"{address}  ·  “{key}” in stages.toml")
        where.setObjectName("secondary")

        self.name_edit = QLineEdit(axis.label)
        self.name_edit.setMaxLength(MAX_LABEL)
        self.name_edit.setPlaceholderText("For example X, Y, Z, or Odor port")

        self.direction_box = QComboBox()
        for direction in DIRECTIONS:
            self.direction_box.addItem(DIRECTION_TEXT[direction], direction)
        self.direction_box.setCurrentIndex(max(0, self.direction_box.findData(axis.direction)))

        self.reverse_box = QCheckBox("Reverse this stage")
        self.reverse_box.setChecked(axis.reversed)
        self.reverse_box.setEnabled(axis.travel is not None)

        self.help = QLabel()
        self.help.setObjectName("secondary")
        self.help.setWordWrap(True)
        self.direction_box.currentIndexChanged.connect(self._explain)
        self._travel_known = axis.travel is not None

        form = QFormLayout()
        form.setHorizontalSpacing(theme.SECTION_GAP)
        form.setVerticalSpacing(theme.GAP)
        form.addRow("Name", self.name_edit)
        form.addRow("Direction", self.direction_box)
        form.addRow("", self.reverse_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.SECTION_GAP)
        layout.addWidget(where)
        layout.addLayout(form)
        layout.addWidget(self.help)
        layout.addWidget(buttons)
        self._explain()

    def _explain(self) -> None:
        negative, positive = DIRECTIONS[self.direction()]
        if positive:
            text = (
                f"The + button should move the stage {positive}. Jog it with + and watch: "
                f"if it moves {negative}, check Reverse."
            )
        else:
            text = "Pick a direction to label the − and + buttons and the ends of the travel bar."
        text += (
            " A reversed stage counts its position from the other end of the travel, so a "
            "larger number is always further "
            + (positive or "along the axis")
            + ". Presets are not affected."
        )
        if not self._travel_known:
            text += " Connect the stage once to enable Reverse."
        self.help.setText(text)

    def name(self) -> str:
        return self.name_edit.text()

    def direction(self) -> str:
        return str(self.direction_box.currentData())

    def reversed(self) -> bool:
        return self.reverse_box.isChecked()
