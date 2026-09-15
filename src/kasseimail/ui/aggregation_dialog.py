"""Choosing how each column is combined when rows are grouped.

Only reachable once a group column is chosen, because until then there is nothing to combine. The
default is `auto` everywhere, and `auto` is right often enough that most people never open this: a
column that reads the same on every row of the group collapses to that value, one that differs
becomes the list. This is for the rest -- a price that should be added up rather than listed.
"""

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from kasseimail.recipients import AGGREGATOR_HELP, AGGREGATORS, DEFAULT_AGGREGATOR


class AggregationDialog(QDialog):
    """One dropdown per column."""

    def __init__(self, headers, original_headers, chosen, group_by, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Aggregation")
        self.setMinimumWidth(460)

        self.boxes: dict[str, QComboBox] = {}

        intro = QLabel(
            f"Rows are grouped by <b>{group_by}</b>. These say how the other columns are "
            "combined across the rows of a group."
        )
        intro.setWordWrap(True)

        legend = QLabel("<br>".join(
            f"<b>{name}</b> — {AGGREGATOR_HELP[name]}" for name in AGGREGATORS
        ))
        legend.setWordWrap(True)
        legend.setStyleSheet("color: palette(mid); font-size: 11px;")

        form = QFormLayout()
        for name, original in zip(headers, original_headers):
            if name == group_by:
                # -- the column being grouped on has one value by definition.
                continue

            box = QComboBox()
            box.addItems(AGGREGATORS)
            box.setCurrentText(chosen.get(name, DEFAULT_AGGREGATOR))
            box.setToolTip(AGGREGATOR_HELP[box.currentText()])
            box.currentTextChanged.connect(
                lambda how, box=box: box.setToolTip(AGGREGATOR_HELP[how]))

            self.boxes[name] = box
            form.addRow(original or name, box)

        inner = QWidget()
        inner.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(scroll, 1)
        layout.addWidget(legend)
        layout.addWidget(buttons)

    def aggregators(self) -> dict:
        """Only what differs from the default, so the spec stays readable and short."""
        return {
            name: box.currentText()
            for name, box in self.boxes.items()
            if box.currentText() != DEFAULT_AGGREGATOR
        }
