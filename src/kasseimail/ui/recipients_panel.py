"""The spreadsheet, as a table, with what preflight made of each row.

Two columns are added in front of the file's own: the row number as the spreadsheet shows it, and
the status preflight gave it. The colouring is the point -- a run of three hundred rows where four
are wrong is unreadable as a list of messages and obvious as four red lines.
"""

from datetime import date, datetime, time
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableView,
    QVBoxLayout, QWidget,
)

from kasseimail.recipients import RecipientProblem
from kasseimail.recipients import load as load_recipients
from kasseimail.recipients import sheet_names

#: kept pale so the text stays readable on either palette; the status column carries the words.
ROW_COLOURS = {
    "error": QColor(198, 40, 40, 40),
    "warning": QColor(178, 106, 0, 40),
    "skip": QColor(128, 128, 128, 30),
}


class RecipientModel(QAbstractTableModel):
    """The loaded table plus the preflight result, which arrives later and separately."""

    def __init__(self, table=None, parent=None):
        super().__init__(parent)
        self.table = table
        self.plans_by_row: dict[int, object] = {}

    # -- shape --------------------------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() or self.table is None else len(self.table.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() or self.table is None else len(self.table.headers) + 3

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole or orientation != Qt.Horizontal or self.table is None:
            return None
        if section == 0:
            return "Row"
        if section == 1:
            return "Status"
        if section == 2:
            return "Attachments"
        # -- the header as it was written in the file, not the normalised one. The template uses
        #    the normalised name, but this table is for recognising your own spreadsheet.
        return self.table.original_headers[section - 3] or self.table.headers[section - 3]

    # -- content ------------------------------------------------------------------------------

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or self.table is None:
            return None

        recipient = self.table.rows[index.row()]
        plan = self.plans_by_row.get(recipient.row)
        column = index.column()

        if role == Qt.DisplayRole:
            if column == 0:
                return recipient.row
            if column == 1:
                return _status_of(plan)
            if column == 2:
                return ", ".join(plan.resolution.names) if plan else ""
            return _display(recipient.fields.get(self.table.headers[column - 3], ""))

        if role == Qt.ToolTipRole and plan is not None:
            problems = plan.errors + plan.warnings
            return "\n".join(problems) if problems else None

        if role == Qt.BackgroundRole and plan is not None:
            if plan.errors:
                return QBrush(ROW_COLOURS["error"])
            if plan.skip:
                return QBrush(ROW_COLOURS["skip"])
            if plan.warnings:
                return QBrush(ROW_COLOURS["warning"])

        if role == Qt.TextAlignmentRole and column == 0:
            return int(Qt.AlignRight | Qt.AlignVCenter)

        return None

    # -- updating -----------------------------------------------------------------------------

    def set_table(self, table) -> None:
        self.beginResetModel()
        self.table = table
        self.plans_by_row = {}
        self.endResetModel()

    def set_preflight(self, found) -> None:
        self.beginResetModel()
        self.plans_by_row = {plan.row: plan for plan in found.rows} if found else {}
        self.endResetModel()

    def recipient_at(self, row: int):
        if self.table is None or not (0 <= row < len(self.table.rows)):
            return None
        return self.table.rows[row]


def _display(value) -> str:
    """A cell as the table shows it.

    A date is the one value worth reformatting here: openpyxl hands back a `datetime`, and
    `str()` on one gives `2026-03-01 00:00:00`, which is both wrong-looking and wide enough to
    push every other column off screen. It is only the *display* -- a template still gets the real
    datetime, which is what the `date` filter needs.
    """
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d" if value.time() == time(0, 0) else "%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return "" if value == "" else str(value)


def _status_of(plan) -> str:
    if plan is None:
        return ""
    if plan.errors:
        return "error"
    if plan.skip:
        return plan.skip
    if plan.warnings:
        return "warning"
    return "ready"


class RecipientsPanel(QWidget):
    """Opening a file, choosing the columns, and the table itself."""

    table_loaded = Signal(object)
    row_selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.table = None
        self.model = RecipientModel()
        self._build()

    def _build(self) -> None:
        self.path_field = QLineEdit()
        self.path_field.setPlaceholderText("no spreadsheet loaded")
        self.path_field.setReadOnly(True)

        open_button = QPushButton("Open...")
        open_button.clicked.connect(self.choose_file)

        self.reload_button = QPushButton("Reload")
        self.reload_button.setToolTip("Re-read the file, after editing it elsewhere")
        self.reload_button.clicked.connect(self.reload)
        self.reload_button.setEnabled(False)

        self.sheet_box = QComboBox()
        self.sheet_box.setMinimumWidth(120)
        self.sheet_box.currentTextChanged.connect(self._sheet_changed)
        self.sheet_box.setEnabled(False)

        self.email_box = QComboBox()
        self.email_box.setMinimumWidth(140)
        self.email_box.currentTextChanged.connect(self._column_changed)

        self.key_box = QComboBox()
        self.key_box.setMinimumWidth(140)
        self.key_box.setToolTip(
            "What identifies a row in the report, and what --resume matches on together with the "
            "row number. Defaults to the address."
        )
        self.key_box.currentTextChanged.connect(self._column_changed)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Recipients"))
        controls.addWidget(self.path_field, 1)
        controls.addWidget(open_button)
        controls.addWidget(self.reload_button)
        controls.addWidget(QLabel("Sheet"))
        controls.addWidget(self.sheet_box)
        controls.addWidget(QLabel("Address"))
        controls.addWidget(self.email_box)
        controls.addWidget(QLabel("Key"))
        controls.addWidget(self.key_box)

        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QTableView.SelectRows)
        self.view.setSelectionMode(QTableView.SingleSelection)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.selectionModel().selectionChanged.connect(self._selection_changed)

        self.summary = QLabel("")
        self.summary.setStyleSheet("color: palette(mid);")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.summary)

    # -- loading ------------------------------------------------------------------------------

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open a recipient list", "",
            "Spreadsheets (*.xlsx *.xlsm *.csv *.tsv);;All files (*)",
        )
        if path:
            self.load(path)

    def load(self, path, *, sheet=None, email_column=None, key_column=None) -> bool:
        """Read the file and fill the table. Returns whether it worked."""
        from PySide6.QtWidgets import QMessageBox

        path = Path(path)
        try:
            sheets = sheet_names(path)
            chosen_sheet = sheet if sheet in sheets else (sheets[0] if sheets else None)

            table = load_recipients(
                path,
                sheet=chosen_sheet,
                email_column=email_column or self._preferred_email_column(path, chosen_sheet),
                key_column=key_column,
            )
        except RecipientProblem as exc:
            QMessageBox.warning(self, "Could not read that file", str(exc))
            return False

        self.table = table
        self.path_field.setText(str(path))
        self.reload_button.setEnabled(True)
        self._fill_boxes(sheets, chosen_sheet, table)
        self.model.set_table(table)
        self.view.resizeColumnsToContents()
        self._show_summary()

        self.table_loaded.emit(table)
        if table.rows:
            self.view.selectRow(0)
        return True

    def _preferred_email_column(self, path: Path, sheet) -> str:
        """Guess the address column, so opening a file usually just works.

        Tries the obvious names before giving up and letting `load` raise with the column list --
        which is still the right error, just one most people will not have to read.
        """
        from kasseimail.recipients import normalise_header

        try:
            probe = load_recipients(path, sheet=sheet, email_column="email")
            return probe.email_column
        except RecipientProblem:
            pass

        for guess in ("e_mail", "e-mail", "mail", "email_address", "adres", "emailadres"):
            try:
                load_recipients(path, sheet=sheet, email_column=guess)
                return normalise_header(guess, 0)
            except RecipientProblem:
                continue

        return "email"

    def reload(self) -> None:
        if self.table is not None:
            self.load(
                self.table.path,
                sheet=self.sheet_box.currentText() or None,
                email_column=self.email_box.currentText() or None,
                key_column=self.key_box.currentText() or None,
            )

    def _fill_boxes(self, sheets, chosen_sheet, table) -> None:
        for box in (self.sheet_box, self.email_box, self.key_box):
            box.blockSignals(True)

        self.sheet_box.clear()
        self.sheet_box.addItems(sheets)
        self.sheet_box.setEnabled(bool(sheets))
        if chosen_sheet:
            self.sheet_box.setCurrentText(chosen_sheet)

        self.email_box.clear()
        self.email_box.addItems(table.headers)
        self.email_box.setCurrentText(table.email_column)

        self.key_box.clear()
        self.key_box.addItems(table.headers)
        self.key_box.setCurrentText(table.key_column)

        for box in (self.sheet_box, self.email_box, self.key_box):
            box.blockSignals(False)

    def _sheet_changed(self, name: str) -> None:
        if name and self.table is not None:
            self.load(self.table.path, sheet=name)

    def _column_changed(self, _name: str) -> None:
        if self.table is None:
            return
        self.load(
            self.table.path,
            sheet=self.sheet_box.currentText() or None,
            email_column=self.email_box.currentText(),
            key_column=self.key_box.currentText(),
        )

    # -- preflight ----------------------------------------------------------------------------

    def show_preflight(self, found) -> None:
        self.model.set_preflight(found)
        # -- the Status and Attachments columns are empty until now, so the sizing done when the
        #    file was opened made them too narrow to read. Re-measure once they have content.
        self.view.resizeColumnToContents(1)
        self.view.resizeColumnToContents(2)
        self._show_summary(found)

    def _show_summary(self, found=None) -> None:
        if self.table is None:
            self.summary.setText("")
            return

        parts = [f"{len(self.table)} rows", f"{len(self.table.headers)} columns"]
        if self.table.without_email:
            parts.append(f"{len(self.table.without_email)} without an address")
        if found is not None:
            parts.append(f"{len(found.sendable)} ready")
            if found.failing:
                parts.append(f"{len(found.failing)} with errors")
        self.summary.setText("   ·   ".join(parts))

    # -- selection ----------------------------------------------------------------------------

    def _selection_changed(self, *_args) -> None:
        self.row_selected.emit(self.current_recipient())

    def current_recipient(self):
        rows = self.view.selectionModel().selectedRows()
        if not rows:
            return None
        return self.model.recipient_at(rows[0].row())
