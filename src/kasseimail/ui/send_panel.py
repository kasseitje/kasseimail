"""The attachment rules, the four buttons, and the progress bar.

The buttons are in the order of how much they commit to, left to right, and Send is the only one
that is coloured. Nothing here decides anything -- it collects what was typed and asks the window
to act on it -- so the rules about what may run live in one place, in `main_window.py`.
"""

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from kasseimail.attachments import AttachmentSpec


class SendPanel(QWidget):
    """What to attach, where it goes, and the controls to start it."""

    validate_requested = Signal()
    run_requested = Signal(str)     # a mode from ledger.MODE_*
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        # -- attachments ------------------------------------------------------------------------
        self.attach_field = QLineEdit()
        self.attach_field.setPlaceholderText("handbook.pdf; terms.pdf")
        self.attach_field.setToolTip("The same file on every message. Separate several with ;")

        browse = QPushButton("...")
        browse.setMaximumWidth(32)
        browse.clicked.connect(self._browse_attachments)

        attach_row = QHBoxLayout()
        attach_row.addWidget(self.attach_field, 1)
        attach_row.addWidget(browse)

        self.pattern_field = QLineEdit()
        self.pattern_field.setPlaceholderText("invoices/{{ invoice_no }}.pdf")
        self.pattern_field.setToolTip(
            "Rendered per row, like the body. Globs work: invoices/A-{{ invoice_no }}-*.pdf"
        )

        self.column_field = QLineEdit()
        self.column_field.setPlaceholderText("attachment")
        self.column_field.setToolTip("A column holding paths, separated by ; or |")

        self.root_field = QLineEdit()
        self.root_field.setPlaceholderText("the directory the spreadsheet is in")
        self.root_field.setToolTip("What relative attachment paths are relative to")

        self.allow_missing = QCheckBox("Send anyway when an attachment is not on disk")

        attachments = QFormLayout()
        attachments.addRow("Every message", attach_row)
        attachments.addRow("Pattern", self.pattern_field)
        attachments.addRow("From column", self.column_field)
        attachments.addRow("Relative to", self.root_field)
        attachments.addRow("", self.allow_missing)

        attachment_box = QGroupBox("Attachments")
        attachment_box.setLayout(attachments)

        # -- delivery ----------------------------------------------------------------------------
        self.test_to = QLineEdit()
        self.test_to.setPlaceholderText("you@yourtenant.be")
        self.test_to.setToolTip(
            "Send everything to this address instead, to rehearse. The real name stays in the "
            "subject, and a row without an address is still skipped."
        )

        self.mailbox_field = QLineEdit()
        self.mailbox_field.setPlaceholderText("your own mailbox")
        self.mailbox_field.setToolTip(
            "Send from a shared mailbox instead of your own, e.g. info@example.be.\n"
            "You need 'Send As' or 'Send on behalf' on it, granted in Exchange -- signing in is "
            "not enough.\nDrafts then land in that mailbox's Drafts, and sent mail in its Sent "
            "Items.\nEmpty means the account you are signed in with."
        )

        self.cc_field = QLineEdit()
        self.cc_field.setPlaceholderText("books@example.be; boss@example.be")

        self.bcc_field = QLineEdit()

        self.out_field = QLineEdit("out")
        self.out_field.setToolTip("Where the rendered bodies, the report and the run log go")

        out_browse = QPushButton("...")
        out_browse.setMaximumWidth(32)
        out_browse.clicked.connect(self._browse_out)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_field, 1)
        out_row.addWidget(out_browse)

        self.resume = QCheckBox("Resume: skip the rows already sent in that directory")

        self.limit = QSpinBox()
        self.limit.setRange(0, 1_000_000)
        self.limit.setSpecialValueText("all")
        self.limit.setToolTip("Only the first N rows that would go out. 0 means all of them.")

        self.pause = QSpinBox()
        self.pause.setRange(0, 60)
        self.pause.setSuffix(" s")
        self.pause.setToolTip(
            "Between two messages. Exchange Online passes 30 a minute on client submission."
        )

        limits = QHBoxLayout()
        limits.addWidget(QLabel("Limit"))
        limits.addWidget(self.limit)
        limits.addSpacing(16)
        limits.addWidget(QLabel("Pause"))
        limits.addWidget(self.pause)
        limits.addStretch(1)

        delivery = QFormLayout()
        delivery.addRow("Send from", self.mailbox_field)
        delivery.addRow("Send all to", self.test_to)
        delivery.addRow("Cc", self.cc_field)
        delivery.addRow("Bcc", self.bcc_field)
        delivery.addRow("Output", out_row)
        delivery.addRow("", self.resume)
        delivery.addRow("", limits)

        delivery_box = QGroupBox("Delivery")
        delivery_box.setLayout(delivery)

        # -- the buttons ---------------------------------------------------------------------------
        self.validate_button = QPushButton("Validate")
        self.validate_button.setToolTip("Render every row and check every attachment. No network.")
        self.validate_button.clicked.connect(self.validate_requested)

        self.dry_run_button = QPushButton("Dry run")
        self.dry_run_button.setToolTip("Write the bodies to the output directory. Nothing leaves.")
        self.dry_run_button.clicked.connect(lambda: self.run_requested.emit("dry-run"))

        self.drafts_button = QPushButton("Create drafts")
        self.drafts_button.setToolTip("Leave the messages in your Drafts; you press send")
        self.drafts_button.clicked.connect(lambda: self.run_requested.emit("drafts"))

        self.send_button = QPushButton("Send")
        self.send_button.setToolTip("Actually send them")
        self.send_button.setStyleSheet(
            "QPushButton { background: #c62828; color: white; font-weight: bold; padding: 5px 18px; }"
            "QPushButton:disabled { background: palette(mid); color: palette(window); }"
        )
        self.send_button.clicked.connect(lambda: self.run_requested.emit("send"))

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_requested)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v of %m")
        self.progress.setVisible(False)

        self.status = QLabel("")

        buttons = QHBoxLayout()
        buttons.addWidget(self.validate_button)
        buttons.addWidget(self.dry_run_button)
        buttons.addWidget(self.drafts_button)
        buttons.addWidget(self.send_button)
        buttons.addSpacing(12)
        buttons.addWidget(self.progress, 1)
        buttons.addWidget(self.cancel_button)

        boxes = QHBoxLayout()
        boxes.addWidget(attachment_box, 1)
        boxes.addWidget(delivery_box, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(boxes)
        layout.addLayout(buttons)
        layout.addWidget(self.status)

    # -- what was typed -------------------------------------------------------------------------

    def attachment_spec(self, default_root, template_patterns=()) -> AttachmentSpec:
        """The rules, with the template's own patterns in front of the typed one."""
        from kasseimail.recipients import normalise_header

        root = self.root_field.text().strip()
        patterns = list(template_patterns)
        typed = self.pattern_field.text().strip()
        if typed:
            patterns.append(typed)

        return AttachmentSpec(
            columns=[normalise_header(name, 0) for name in _split(self.column_field.text())],
            common=_split(self.attach_field.text()),
            patterns=patterns,
            # -- a Path and not the text of the field: `resolve` joins it with `/`, and a str on
            #    the left of that raises a TypeError the moment somebody fills this box in.
            root=Path(root).expanduser() if root else Path(default_root),
            allow_missing=self.allow_missing.isChecked(),
        )

    def run_options(self) -> dict:
        return {
            "mailbox": self.mailbox_field.text().strip(),
            "test_to": self.test_to.text().strip() or None,
            "cc": _split(self.cc_field.text()),
            "bcc": _split(self.bcc_field.text()),
            "out_dir": self.out_field.text().strip() or "out",
            "resume": self.resume.isChecked(),
            "limit": self.limit.value() or None,
            "pause": float(self.pause.value()),
        }

    # -- state ------------------------------------------------------------------------------------

    def set_busy(self, busy: bool, total: int = 0) -> None:
        for button in (self.validate_button, self.dry_run_button, self.drafts_button,
                       self.send_button):
            button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, max(1, total))
            self.progress.setValue(0)

    def set_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    # -- browsing ------------------------------------------------------------------------------

    def _browse_attachments(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach to every message")
        if paths:
            self.attach_field.setText("; ".join(paths))

    def _browse_out(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Output directory")
        if path:
            self.out_field.setText(path)


def _split(text: str) -> list[str]:
    parts = [text]
    for separator in (";", "|"):
        parts = [piece for part in parts for piece in part.split(separator)]
    return [part.strip() for part in parts if part.strip()]
