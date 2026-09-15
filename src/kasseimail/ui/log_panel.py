"""The log pane -- the same loguru lines the CLI prints, live, while a run goes on.

The engine logs and does not know where it lands. Here a loguru sink emits a Qt signal instead of
writing to a stream, so the window shows exactly what the terminal would have, including everything
the engine logs from the worker thread.

**The signal is what makes that safe.** loguru calls the sink on whichever thread logged, and a
widget may only be touched from the GUI thread. A queued signal -- which is what Qt uses by default
across threads -- hands the line over to the GUI thread to append. Writing to the widget straight
from the sink would work for a while and then crash somewhere unrelated.
"""

from loguru import logger
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

LEVELS = ["DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]

#: readable on both a light and a dark Fusion palette, which is the only constraint that matters --
#: the window does not know which one it will be shown in.
LEVEL_COLOURS = {
    "DEBUG": "#8a8a8a",
    "INFO": None,
    "SUCCESS": "#2e7d32",
    "WARNING": "#b26a00",
    "ERROR": "#c62828",
    "CRITICAL": "#c62828",
}

#: how many lines the pane keeps. A run of several thousand messages would otherwise grow the
#: document until scrolling stutters; the full record is in the run log on disk regardless.
MAX_LINES = 5000


class LogBridge(QObject):
    """A loguru sink that emits a Qt signal. Lives on the GUI thread; called from any thread."""

    message = Signal(str, str)

    def write(self, record) -> None:
        """loguru hands a Message whose `.record` is the structured form."""
        try:
            fields = record.record
            line = f"{fields['time']:%H:%M:%S}  {fields['message']}"
            self.message.emit(fields["level"].name, line)
        except Exception:  # pragma: no cover -- a sink that raises would take the logger with it
            pass


class LogPanel(QWidget):
    """The pane, with a level filter and a copy button."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.bridge = LogBridge()
        self.bridge.message.connect(self._append)
        self._handler_id = None
        self._minimum = "INFO"

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = self.view.font()
        font.setFamilies(["JetBrains Mono", "DejaVu Sans Mono", "Consolas", "monospace"])
        self.view.setFont(font)

        self.level_box = QComboBox()
        self.level_box.addItems(LEVELS)
        self.level_box.setCurrentText("INFO")
        self.level_box.currentTextChanged.connect(self._set_minimum)

        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)

        copy_button = QPushButton("Copy")
        copy_button.clicked.connect(self._copy)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.view.clear)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Log"))
        controls.addStretch(1)
        controls.addWidget(QLabel("Level"))
        controls.addWidget(self.level_box)
        controls.addWidget(self.follow)
        controls.addWidget(copy_button)
        controls.addWidget(clear_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addWidget(self.view, 1)

    # -- the sink -----------------------------------------------------------------------------

    def attach(self) -> None:
        """Register with loguru. DEBUG, so the filter below is the only thing hiding anything.

        `enqueue=False` on purpose: loguru's own queue would add a second thread hop on top of the
        signal, and the signal is already the handover the GUI thread needs.
        """
        if self._handler_id is not None:
            return
        self._handler_id = logger.add(self.bridge.write, level="DEBUG", format="{message}",
                                      enqueue=False)

    def detach(self) -> None:
        if self._handler_id is None:
            return
        try:
            logger.remove(self._handler_id)
        except ValueError:
            pass
        self._handler_id = None

    # -- showing ------------------------------------------------------------------------------

    def _set_minimum(self, level: str) -> None:
        self._minimum = level

    def _append(self, level: str, line: str) -> None:
        if LEVELS.index(level) < LEVELS.index(self._minimum):
            return

        fmt = QTextCharFormat()
        colour = LEVEL_COLOURS.get(level)
        if colour:
            fmt.setForeground(QColor(colour))
        if level in ("ERROR", "CRITICAL"):
            fmt.setFontWeight(700)

        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(line + "\n", fmt)

        if self.follow.isChecked():
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.view.toPlainText())

    def closeEvent(self, event):  # pragma: no cover -- Qt lifecycle
        self.detach()
        super().closeEvent(event)


__all__ = ["LogPanel", "LogBridge", "Qt"]
