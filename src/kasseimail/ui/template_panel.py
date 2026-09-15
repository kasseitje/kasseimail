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
    QComboBox, QHBoxLayout, QInputDialog, QLabel, QListWidget, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
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

    def __init__(self, template_dir, parent=None):
        super().__init__(parent)

        self.templates = TemplateSet(template_dir)
        self.template = None
        self.preview_row = None

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

        preview_head = QHBoxLayout()
        preview_head.addWidget(QLabel("Preview"))
        preview_head.addWidget(self.preview_for, 1)
        preview_head.addWidget(self.preview_mode)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)

        preview_box = QVBoxLayout()
        preview_box.setContentsMargins(0, 0, 0, 0)
        preview_box.addLayout(preview_head)
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

    def set_preview_row(self, recipient) -> None:
        self.preview_row = recipient
        self.preview_for.setText(
            f"row {recipient.row} — {recipient.email or 'no address'}" if recipient
            else "no row selected"
        )
        self.refresh_preview()

    def refresh_preview(self) -> None:
        if self.template is None:
            return

        if self.preview_row is None:
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
            self.preview.setHtml(
                "<div style='font-family:sans-serif'>"
                "<p style='color:#c62828'><b>Cannot render this row yet</b></p>"
                f"<pre style='white-space:pre-wrap'>{_escape(str(exc))}</pre></div>"
            )
            return

        body = rendered.html or rendered.text or ""

        if self.preview_mode.currentText() == "Source" or not rendered.html:
            self.preview.setPlainText(f"Subject: {rendered.subject}\n\n{body}")
        else:
            self.preview.setHtml(
                f"<div style='font-family:sans-serif;color:palette(mid);"
                f"border-bottom:1px solid palette(mid);padding-bottom:4px;margin-bottom:10px'>"
                f"<b>Subject:</b> {_escape(rendered.subject)}</div>{rendered.html}"
            )

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
