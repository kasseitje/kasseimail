"""The device code, on screen, while the worker thread waits for it to be used.

Modeless on purpose. A modal dialog would block the GUI thread's event loop, and the log pane has to
keep showing what the worker is doing -- including the sign-in failing, which is precisely when
somebody is staring at this window.
"""

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)


class DeviceCodeDialog(QDialog):
    """Shows the code and opens the verification page."""

    #: the sign-in should be called off -- the button says so, so it has to mean it. Hiding the
    #: dialog alone would leave a thread polling for a quarter of an hour with nothing on screen.
    cancelled = Signal()

    def __init__(self, flow: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sign in to Microsoft 365")
        self.setModal(False)
        self.setMinimumWidth(460)

        self.url = flow.get("verification_uri", "https://microsoft.com/devicelogin")
        code = flow.get("user_code", "")

        intro = QLabel(
            "Open the sign-in page, enter this code, and sign in with the account the "
            "mail should come from."
        )
        intro.setWordWrap(True)

        self.code_field = QLineEdit(code)
        self.code_field.setReadOnly(True)
        self.code_field.setAlignment(Qt.AlignCenter)
        font = QFont(self.code_field.font())
        font.setPointSize(font.pointSize() + 8)
        font.setBold(True)
        font.setFamilies(["JetBrains Mono", "DejaVu Sans Mono", "Consolas", "monospace"])
        self.code_field.setFont(font)
        self.code_field.selectAll()

        copy_button = QPushButton("Copy code")
        copy_button.clicked.connect(self._copy)

        open_button = QPushButton("Open sign-in page")
        open_button.setDefault(True)
        open_button.clicked.connect(self._open)

        close_button = QPushButton("Cancel sign-in")
        close_button.clicked.connect(self._cancel)

        where = QLabel(f'<a href="{self.url}">{self.url}</a>')
        where.setOpenExternalLinks(True)
        where.setTextInteractionFlags(Qt.TextBrowserInteraction)

        self.status = QLabel("Waiting for you to finish signing in...")
        self.status.setStyleSheet("color: palette(mid);")

        buttons = QHBoxLayout()
        buttons.addWidget(copy_button)
        buttons.addWidget(open_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.code_field)
        layout.addWidget(where)
        layout.addLayout(buttons)
        layout.addWidget(self.status)

    def _cancel(self) -> None:
        self.status.setText("Cancelling...")
        self.cancelled.emit()

    def closeEvent(self, event):
        """The window-manager X means the same thing as the button."""
        self.cancelled.emit()
        super().closeEvent(event)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.code_field.text())
        self.status.setText("Code copied. Paste it into the sign-in page.")

    def _open(self) -> None:
        self._copy()
        QDesktopServices.openUrl(QUrl(self.url))

    def signed_in(self) -> None:
        self.status.setText("Signed in.")
        self.hide()
