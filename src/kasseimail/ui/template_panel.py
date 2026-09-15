"""The template list, the editor, and the preview.

This is "manage templates from a local directory", end to end: the list is the directory, editing a
tab writes the file, and the preview renders what that file will actually produce against a real row
of the loaded spreadsheet.

**The preview renders against a row, not against placeholder text.** A preview of `{{ first_name }}`
that shows `{{ first_name }}` tells you nothing about the thing that goes wrong -- a column that is
not there, a date that renders as `2026-03-01 00:00:00`, a number that renders as `1001.0`. Rendered
against row two, all three are visible before anybody presses send.
"""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QTextBrowser, QToolButton, QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from kasseimail.config import HTML_FILE, META_FILE, SUBJECT_FILE, TEXT_FILE
from kasseimail.templates import PARTS, TemplateProblem, TemplateSet
from kasseimail.ui.highlighter import JinjaHighlighter

#: how long the typing has to stop before the file is written and the preview re-rendered. Long
#: enough not to write on every keystroke, short enough that it feels like it follows along.
SAVE_DELAY_MS = 400

PART_LABELS = {
    SUBJECT_FILE: "Subject",
    HTML_FILE: "HTML body",
    TEXT_FILE: "Plain text",
    META_FILE: "Settings",
}


class TemplatePanel(QWidget):
    """The left-hand list and the editor beside it."""

    template_changed = Signal(str)
    #: step the preview to another recipient: -1 for the previous, +1 for the next. The window
    #: turns it into a selection in the recipients table, so the table and the preview can never
    #: disagree about which one is being looked at.
    navigate = Signal(int)

    def __init__(self, template_dir, parent=None):
        super().__init__(parent)

        self.templates = TemplateSet(template_dir)
        self.template = None
        self.preview_row = None
        self.position = (0, 0)

        self._editors: dict[str, QPlainTextEdit] = {}
        self._dirty: set[str] = set()
        self._loading = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._save_dirty)

        self._build()
        self.reload()

    # -- building -----------------------------------------------------------------------------

    def _build(self) -> None:
        self.list = QListWidget()
        self.list.currentTextChanged.connect(self._select)

        new_button = QPushButton("New")
        new_button.clicked.connect(self._new)
        copy_button = QPushButton("Duplicate")
        copy_button.clicked.connect(self._duplicate)
        delete_button = QPushButton("Delete")
        delete_button.clicked.connect(self._delete)
        reload_button = QPushButton("Reload")
        reload_button.setToolTip("Re-read the directory, for templates changed outside this window")
        reload_button.clicked.connect(self.reload)

        buttons = QHBoxLayout()
        for button in (new_button, copy_button, delete_button, reload_button):
            buttons.addWidget(button)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(QLabel("Templates"))
        left.addWidget(self.list, 1)
        left.addLayout(buttons)

        left_widget = QWidget()
        left_widget.setLayout(left)

        self.tabs = QTabWidget()
        for part in PARTS:
            editor = QPlainTextEdit()
            editor.setLineWrapMode(QPlainTextEdit.NoWrap)
            font = editor.font()
            font.setFamilies(["JetBrains Mono", "DejaVu Sans Mono", "Consolas", "monospace"])
            editor.setFont(font)
            editor.setTabStopDistance(28)
            JinjaHighlighter(editor.document(), html=(part == HTML_FILE))
            editor.textChanged.connect(lambda part=part: self._touched(part))

            self._editors[part] = editor
            self.tabs.addTab(editor, PART_LABELS[part])

        self.preview_mode = QComboBox()
        self.preview_mode.addItems(["Rendered", "Source"])
        self.preview_mode.currentTextChanged.connect(self.refresh_preview)

        self.preview_for = QLabel("no row selected")
        self.preview_for.setStyleSheet("color: palette(mid);")

        # -- stepping through the list is how you check a run: the first, the last, and the one
        #    you know is awkward. Clicking rows in the table below does it too, but that table is
        #    three panes away from the text you are reading.
        self.previous_button = QToolButton()
        self.previous_button.setText("◀")
        self.previous_button.setToolTip("Preview the previous recipient")
        self.previous_button.clicked.connect(lambda: self.navigate.emit(-1))

        self.next_button = QToolButton()
        self.next_button.setText("▶")
        self.next_button.setToolTip("Preview the next recipient")
        self.next_button.clicked.connect(lambda: self.navigate.emit(1))

        self.position_label = QLabel("—")
        self.position_label.setStyleSheet("color: palette(mid);")
        self.position_label.setMinimumWidth(96)
        self.position_label.setAlignment(Qt.AlignCenter)

        preview_head = QHBoxLayout()
        preview_head.addWidget(QLabel("Preview"))
        preview_head.addWidget(self.preview_for, 1)
        preview_head.addWidget(self.previous_button)
        preview_head.addWidget(self.position_label)
        preview_head.addWidget(self.next_button)
        preview_head.addWidget(self.preview_mode)

        # -- the subject on its own line, not folded into the body. It is a separate field of the
        #    message, it is the one line every recipient certainly reads, and buried above the body
        #    it was the easiest thing on this screen to skim past.
        self.subject_field = QLineEdit()
        self.subject_field.setReadOnly(True)
        self.subject_field.setPlaceholderText("the rendered subject appears here")
        subject_font = self.subject_field.font()
        subject_font.setBold(True)
        self.subject_field.setFont(subject_font)

        subject_form = QFormLayout()
        subject_form.setContentsMargins(0, 4, 0, 4)
        subject_form.addRow("Subject", self.subject_field)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)

        preview_box = QVBoxLayout()
        preview_box.setContentsMargins(0, 0, 0, 0)
        preview_box.addLayout(preview_head)
        preview_box.addLayout(subject_form)
        preview_box.addWidget(self.preview, 1)

        preview_widget = QWidget()
        preview_widget.setLayout(preview_box)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(self.tabs)
        splitter.addWidget(preview_widget)
        splitter.setSizes([180, 520, 420])
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

    # -- the list -----------------------------------------------------------------------------

    def reload(self) -> None:
        """Re-read the directory. Keeps the selection if that template is still there."""
        self.templates.ensure()
        wanted = self.list.currentItem().text() if self.list.currentItem() else None

        self.list.blockSignals(True)
        self.list.clear()
        names = self.templates.names()
        self.list.addItems(names)
        self.list.blockSignals(False)

        if names:
            target = wanted if wanted in names else names[0]
            self.list.setCurrentRow(names.index(target))
            self._select(target)

    def select(self, name: str) -> None:
        names = self.templates.names()
        if name in names:
            self.list.setCurrentRow(names.index(name))

    def _select(self, name: str) -> None:
        if not name:
            return

        self._flush()

        try:
            self.template = self.templates.get(name)
        except TemplateProblem as exc:
            QMessageBox.warning(self, "Template", str(exc))
            return

        self._loading = True
        try:
            for part, editor in self._editors.items():
                editor.setPlainText(self.template.read_part(part))
            self._dirty.clear()
            self._update_tab_labels()
        finally:
            self._loading = False

        self.template_changed.emit(name)
        self.refresh_preview()

    # -- editing ------------------------------------------------------------------------------

    def _touched(self, part: str) -> None:
        if self._loading or self.template is None:
            return
        self._dirty.add(part)
        self._update_tab_labels()
        self._save_timer.start()

    def _update_tab_labels(self) -> None:
        for index, part in enumerate(PARTS):
            marker = " •" if part in self._dirty else ""
            self.tabs.setTabText(index, PART_LABELS[part] + marker)

    def _save_dirty(self) -> None:
        """Write what changed, then re-render. A save that fails must not clear the dirty flag."""
        if self.template is None or not self._dirty:
            return

        saved = set()
        for part in list(self._dirty):
            try:
                self.template.write_part(part, self._editors[part].toPlainText())
                saved.add(part)
            except (TemplateProblem, OSError) as exc:
                QMessageBox.warning(self, "Could not save", f"{part}: {exc}")

        self._dirty -= saved
        self._update_tab_labels()
        self.refresh_preview()

    def _flush(self) -> None:
        """Write pending edits now -- before switching template, and before a run starts."""
        self._save_timer.stop()
        self._save_dirty()

    def commit(self) -> None:
        """What the window calls before building a run, so the engine reads what is on screen."""
        self._flush()

    # -- the preview --------------------------------------------------------------------------

    def set_preview_row(self, recipient, position: int = 0, total: int = 0) -> None:
        """Preview this recipient, and say where it sits in the list.

        A grouped recipient is several spreadsheet rows, so it is named by all of them -- "rows
        2, 3, 4" -- and not by the first. Seeing "row 2" on a message that covers three of them is
        how you end up believing the grouping did not happen.
        """
        self.preview_row = recipient
        self.position = (position, total)

        if recipient is None:
            self.preview_for.setText("no row selected")
        else:
            rows = getattr(recipient, "source_rows", None) or [recipient.row]
            where = f"row {rows[0]}" if len(rows) == 1 else \
                f"rows {', '.join(str(number) for number in rows)}"
            self.preview_for.setText(f"{where} — {recipient.email or 'no address'}")

        self._show_position()
        self.refresh_preview()

    def _show_position(self) -> None:
        position, total = self.position
        self.position_label.setText(f"{position} of {total}" if total else "—")
        self.previous_button.setEnabled(total > 1 and position > 1)
        self.next_button.setEnabled(total > 1 and position < total)

    def refresh_preview(self) -> None:
        if self.template is None:
            return

        if self.preview_row is None:
            self.subject_field.clear()
            self.preview.setPlainText(
                "Load a spreadsheet below to preview this template against a real row.\n\n"
                "Rendering against actual values is the point: a column that is not there, a date "
                "that comes out as 2026-03-01 00:00:00, a number that comes out as 1001.0 — none "
                "of those show up in a preview of the raw text."
            )
            return

        try:
            rendered = self.template.render(self.preview_row.context(attachments=[]))
        except TemplateProblem as exc:
            # -- shown in place rather than as a dialog: while you are typing, a half-finished
            #    `{{ ` is an error on nearly every keystroke and a dialog each time is unusable.
            self.subject_field.clear()
            self.subject_field.setPlaceholderText("this row does not render yet")
            self.preview.setHtml(
                "<div style='font-family:sans-serif'>"
                "<p style='color:#c62828'><b>Cannot render this row yet</b></p>"
                f"<pre style='white-space:pre-wrap'>{_escape(str(exc))}</pre></div>"
            )
            return

        self.subject_field.setText(rendered.subject)
        self.subject_field.setCursorPosition(0)
        self.subject_field.setToolTip(rendered.subject)

        body = rendered.html or rendered.text or ""

        # -- the subject is in its own field above, so the body shown here is only the body.
        if self.preview_mode.currentText() == "Source" or not rendered.html:
            self.preview.setPlainText(body)
        else:
            self.preview.setHtml(rendered.html)

    # -- the buttons --------------------------------------------------------------------------

    def _new(self) -> None:
        name, accepted = QInputDialog.getText(self, "New template", "Name:")
        if not accepted or not name.strip():
            return
        self._create(name.strip())

    def _duplicate(self) -> None:
        if self.template is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Duplicate template", "Name for the copy:", text=f"{self.template.name}-copy"
        )
        if not accepted or not name.strip():
            return
        self._create(name.strip(), copy_from=self.template.name)

    def _create(self, name: str, copy_from: str | None = None) -> None:
        try:
            self.templates.create(name, copy_from=copy_from)
        except TemplateProblem as exc:
            QMessageBox.warning(self, "Could not create it", str(exc))
            return
        self.reload()
        self.select(name)

    def _delete(self) -> None:
        if self.template is None:
            return

        answer = QMessageBox.question(
            self,
            "Delete template",
            f"Delete '{self.template.name}' and every file in it?\n\n"
            f"{self.template.directory}\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            self.templates.delete(self.template.name)
        except TemplateProblem as exc:
            QMessageBox.warning(self, "Could not delete it", str(exc))
            return

        self.template = None
        self.reload()


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
