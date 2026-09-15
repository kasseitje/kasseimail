"""The window: templates on top, recipients in the middle, the run controls and the log below.

It owns the pieces and the rules about what may run when. The panels collect what was typed and the
engine does the work; the decisions in between -- may this send, does it need confirming, what does
Cancel do -- are all here, because spreading them across four widgets is how a bulk mailer ends up
sending something nobody confirmed.
"""

from pathlib import Path

from loguru import logger
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QVBoxLayout, QWidget,
)

from kasseimail import config
from kasseimail.graph import GraphMailer, SignInProblem
from kasseimail.ledger import MODE_DRAFTS, MODE_DRY_RUN, MODE_SEND
from kasseimail.run import RunProblem, SendRun
from kasseimail.templates import TemplateProblem
from kasseimail.ui.device_code_dialog import DeviceCodeDialog
from kasseimail.ui.log_panel import LogPanel
from kasseimail.ui.recipients_panel import RecipientsPanel
from kasseimail.ui.send_panel import SendPanel
from kasseimail.ui.template_panel import TemplatePanel
from kasseimail.ui.worker import SendController


class MainWindow(QMainWindow):
    def __init__(self, settings, parent=None, store=None):
        super().__init__(parent)

        self.settings = settings
        # -- injectable so a test gets its own, empty. Sharing the real one would mean a test
        #    restoring whatever was last typed in a real session -- an attachment pattern from
        #    last week deciding whether today's assertion passes.
        self.store = store if store is not None else QSettings("kasseimail", "kasseimail")
        self.controller = SendController(self)
        self.device_dialog = None
        self._last_preflight = None

        self.setWindowTitle("kasseimail")
        self._build()
        self._connect()
        self._restore()

        self.log.attach()
        logger.info("templates in {}", self.settings.template_dir)
        self._show_account()

    # -- building -----------------------------------------------------------------------------

    def _build(self) -> None:
        self.templates = TemplatePanel(self.settings.template_dir)
        self.recipients = RecipientsPanel()
        self.send = SendPanel()
        self.log = LogPanel()

        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addWidget(self.send)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.templates)
        self.splitter.addWidget(self.recipients)
        self.splitter.addWidget(lower)
        self.splitter.addWidget(self.log)
        self.splitter.setSizes([340, 240, 210, 170])
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.splitter)
        self.setCentralWidget(container)

        self._build_menu()
        self.statusBar().showMessage("Ready")

    def _build_menu(self) -> None:
        account = self.menuBar().addMenu("&Account")
        account.addAction("Sign in...", self._sign_in)
        account.addAction("Sign out", self._sign_out)
        account.addSeparator()
        account.addAction("Credentials...", self._edit_credentials)

        view = self.menuBar().addMenu("&Templates")
        view.addAction("Reload from disk", self.templates.reload)
        view.addAction("Open the template folder", self._open_template_dir)

        helped = self.menuBar().addMenu("&Help")
        helped.addAction("About", self._about)

    def _connect(self) -> None:
        self.recipients.row_selected.connect(self.templates.set_preview_row)
        self.recipients.table_loaded.connect(lambda _table: self._clear_preflight())
        self.templates.template_changed.connect(lambda _name: self._clear_preflight())

        self.send.validate_requested.connect(self._validate)
        self.send.run_requested.connect(self._run)
        self.send.cancel_requested.connect(self._cancel)

        self.controller.progress.connect(self._on_progress)
        self.controller.device_code.connect(self._on_device_code)
        self.controller.finished.connect(self._on_finished)
        self.controller.failed.connect(self._on_failed)
        self.controller.stopped.connect(lambda: self.send.set_busy(False))

    # -- building a run -------------------------------------------------------------------------

    def _build_run(self, mode: str) -> SendRun | None:
        """The one place a `SendRun` is made, so every button gets the same object.

        Returns None and says why, rather than raising: every caller here is a button press and
        the answer belongs in a dialog.
        """
        # -- write pending edits first. Otherwise a run uses the file as it was before the last
        #    keystroke, and the preview on screen is not what goes out.
        self.templates.commit()

        if self.templates.template is None:
            QMessageBox.information(self, "No template", "Choose a template first.")
            return None

        if self.recipients.table is None:
            QMessageBox.information(self, "No recipients", "Open a spreadsheet first.")
            return None

        options = self.send.run_options()
        table = self.recipients.table

        try:
            return SendRun(
                template=self.templates.template,
                table=table,
                spec=self.send.attachment_spec(
                    default_root=table.path.parent,
                    template_patterns=self.templates.template.meta.attachments,
                ),
                mode=mode,
                settings=self.settings,
                **options,
            )
        except (RunProblem, TemplateProblem) as exc:
            QMessageBox.warning(self, "Cannot start", str(exc))
            return None

    def _validate(self) -> None:
        run = self._build_run(MODE_DRY_RUN)
        if run is None:
            return

        found = run.preflight()
        self._last_preflight = found
        self.recipients.show_preflight(found)

        for level, text in found.summary_lines():
            logger.log(level.upper(), text)

        self.send.set_status(
            f"{len(found.sendable)} ready, {len(found.failing)} with errors"
            if found.failing else f"{len(found.sendable)} ready to send"
        )

    def _run(self, mode: str) -> None:
        if self.controller.busy:
            return

        # -- before the confirmation, not after it. Asking "send seventy messages?", getting a
        #    yes, and only then saying there are no credentials wastes the one moment the person
        #    was paying full attention.
        if mode != MODE_DRY_RUN and not (self.settings.tenant_id and self.settings.client_id):
            QMessageBox.information(
                self, "No credentials yet",
                "kasseimail needs the tenant and client id of an Entra ID app registration "
                "before it can sign in.\n\nAccount → Credentials, or see the README under "
                "'Setting up the app registration'.",
            )
            self._edit_credentials()
            return

        run = self._build_run(mode)
        if run is None:
            return

        found = run.preflight()
        self._last_preflight = found
        self.recipients.show_preflight(found)
        for level, text in found.summary_lines():
            logger.log(level.upper(), text)

        if not found.sendable:
            QMessageBox.information(self, "Nothing to do", "No row would get a message.")
            return

        if not found.ok and mode != MODE_DRY_RUN:
            QMessageBox.warning(
                self, "Not sending",
                "Preflight found problems, so nothing was sent.\n\n"
                + "\n".join(text for level, text in found.summary_lines() if level == "error")[:2000]
                + "\n\nThe rows in red say what is wrong. A dry run works regardless.",
            )
            return

        if mode != MODE_DRY_RUN and not self._confirm(run, found, mode):
            return

        self.send.set_busy(True, total=len(found.sendable))
        self.send.set_status("running...")
        self.statusBar().showMessage(f"{mode}: 0 of {len(found.sendable)}")
        self.controller.start(run)

    def _confirm(self, run: SendRun, found, mode: str) -> bool:
        """Ask once, naming the count, the template and where it goes.

        Deliberately not skippable: this is the step that stands between a typo and fifty mails
        that cannot be recalled.
        """
        where = (f"all to {run.test_to} (a rehearsal)" if run.test_to
                 else "to the addresses in the spreadsheet")
        verb = "Send" if mode == MODE_SEND else "Create drafts for"

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if mode == MODE_SEND else QMessageBox.Question)
        box.setWindowTitle("Confirm")
        box.setText(f"{verb} {len(found.sendable)} message(s)?")
        box.setInformativeText(
            f"Template:    {run.template.name}\n"
            f"Recipients:  {where}\n"
            f"From:        {self._account_name()}\n"
            + (f"Cc/Bcc:      {', '.join(run.cc + run.bcc)}\n" if (run.cc or run.bcc) else "")
            + ("\nSent mail cannot be recalled." if mode == MODE_SEND else "")
        )
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        box.setDefaultButton(QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText(verb)

        return box.exec() == QMessageBox.Yes

    def _cancel(self) -> None:
        self.controller.cancel()
        self.send.set_status("cancelling after this message...")

    # -- what the worker reports ----------------------------------------------------------------

    def _on_progress(self, event) -> None:
        self.send.set_progress(event.index, event.total)
        self.statusBar().showMessage(
            f"{event.index} of {event.total} — row {event.row} {event.address} ({event.status})"
        )

    def _on_device_code(self, flow: dict) -> None:
        """Show the code. The worker thread is waiting on `code_is_showing` for this to happen."""
        self.device_dialog = DeviceCodeDialog(flow, self)
        self.device_dialog.show()
        self.device_dialog.raise_()
        self.controller.code_is_showing()

    def _on_finished(self, summary) -> None:
        if self.device_dialog is not None:
            self.device_dialog.signed_in()
            self.device_dialog = None

        self.send.set_busy(False)
        self.send.set_progress(summary.delivered, max(1, summary.total))

        headline = summary.summary_lines()[0][1]
        self.send.set_status(headline)
        self.statusBar().showMessage(headline)

        if summary.failed:
            QMessageBox.warning(
                self, "Finished with failures",
                f"{summary.failed} of {summary.total} did not go out.\n\n"
                + "\n".join(f"row {row}: {reason}" for row, reason in summary.failures[:10])
                + f"\n\nThe report is at {summary.report}.\n"
                "Ticking Resume and running again retries only those rows.",
            )
        elif summary.cancelled:
            QMessageBox.information(
                self, "Cancelled",
                f"Stopped after {summary.delivered} of {summary.total}.\n\n"
                "Tick Resume and run again to continue where it stopped.",
            )

        self._show_account()

    def _on_failed(self, message: str) -> None:
        if self.device_dialog is not None:
            self.device_dialog.hide()
            self.device_dialog = None

        self.send.set_busy(False)
        self.send.set_status("failed")
        logger.error(message)
        QMessageBox.critical(self, "The run stopped", message)

    def _clear_preflight(self) -> None:
        """A changed template or a reloaded file makes the previous result wrong, not stale."""
        self._last_preflight = None
        self.recipients.show_preflight(None)
        self.send.set_status("")

    # -- the account ----------------------------------------------------------------------------

    def _mailer(self) -> GraphMailer | None:
        if not (self.settings.tenant_id and self.settings.client_id):
            return None
        return GraphMailer(self.settings.tenant_id, self.settings.client_id,
                           self.settings.token_cache)

    def _account_name(self) -> str:
        mailer = self._mailer()
        if mailer is None:
            return "(no credentials configured)"
        try:
            account = mailer.cached_account()
        except SignInProblem:
            return "(not signed in)"
        return account.username if account else "(not signed in — you will be asked)"

    def _show_account(self) -> None:
        self.setWindowTitle(f"kasseimail — {self._account_name()}")

    def _sign_in(self) -> None:
        """Sign in from the menu, so it can be done before a run rather than during one.

        The device flow blocks, so this is the one place the GUI thread does wait -- the dialog is
        shown first, and the wait is bounded by MSAL's own expiry on the code.
        """
        mailer = self._mailer()
        if mailer is None:
            self._edit_credentials()
            return

        def show_code(flow):
            self.device_dialog = DeviceCodeDialog(flow, self)
            self.device_dialog.show()
            self.device_dialog.raise_()
            # -- paint it before MSAL starts polling and the event loop stops turning.
            from PySide6.QtWidgets import QApplication

            QApplication.processEvents()

        try:
            mailer.sign_in(on_device_code=show_code)
        except SignInProblem as exc:
            QMessageBox.warning(self, "Could not sign in", str(exc))
            return
        finally:
            if self.device_dialog is not None:
                self.device_dialog.hide()
                self.device_dialog = None

        self._show_account()
        QMessageBox.information(self, "Signed in", f"Signed in as {self._account_name()}.")

    def _sign_out(self) -> None:
        mailer = self._mailer()
        if mailer is not None and mailer.forget():
            logger.info("token cache removed")
        self._show_account()

    def _edit_credentials(self) -> None:
        """The tenant and client id, written to the config file the CLI reads too."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Credentials")
        dialog.setMinimumWidth(460)

        tenant = QLineEdit(self.settings.tenant_id or "")
        client = QLineEdit(self.settings.client_id or "")

        explanation = QLabel(
            "The Entra ID app registration kasseimail signs in through. There is no secret: it is "
            "a public client and you sign in with a device code.\n\n"
            "See the README, 'Setting up the app registration'. The setting people forget is "
            "Allow public client flows → Yes."
        )
        explanation.setWordWrap(True)

        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(dialog.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(dialog.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)

        form = QFormLayout()
        form.addRow("Tenant ID", tenant)
        form.addRow("Client ID", client)

        layout = QVBoxLayout(dialog)
        layout.addWidget(explanation)
        layout.addLayout(form)
        layout.addWidget(QLabel(f"Saved to {config.config_path()}"))
        layout.addLayout(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        config.write_config_file({
            "tenant_id": tenant.text().strip() or None,
            "client_id": client.text().strip() or None,
        })
        self.settings = config.load_settings(template_dir=self.settings.template_dir)
        self._show_account()
        logger.info("credentials saved to {}", config.config_path())

    # -- odds and ends ----------------------------------------------------------------------------

    def _open_template_dir(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.template_dir)))

    def _about(self) -> None:
        from kasseimail.version import __version__

        QMessageBox.about(
            self, "kasseimail",
            f"<b>kasseimail {__version__}</b><br><br>"
            "Bulk mail over the Microsoft Graph API.<br><br>"
            f"Templates: {self.settings.template_dir}<br>"
            f"Config: {config.config_path()}<br>"
            f"Token: {self.settings.token_cache}",
        )

    # -- remembering ------------------------------------------------------------------------------

    def _restore(self) -> None:
        geometry = self.store.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(1280, 900)

        state = self.store.value("splitter")
        if state:
            self.splitter.restoreState(state)

        template = self.store.value("template")
        if template:
            self.templates.select(template)

        data_file = self.store.value("data_file")
        if data_file and Path(data_file).is_file():
            self.recipients.load(data_file)

        for key, field in (("out", self.send.out_field), ("test_to", self.send.test_to),
                           ("attach", self.send.attach_field),
                           ("pattern", self.send.pattern_field),
                           ("column", self.send.column_field)):
            value = self.store.value(key)
            if value:
                field.setText(str(value))

        self.send.pause.setValue(int(float(self.store.value("pause", self.settings.pause))))

    def _remember(self) -> None:
        self.store.setValue("geometry", self.saveGeometry())
        self.store.setValue("splitter", self.splitter.saveState())
        if self.templates.template is not None:
            self.store.setValue("template", self.templates.template.name)
        if self.recipients.table is not None:
            self.store.setValue("data_file", str(self.recipients.table.path))

        self.store.setValue("out", self.send.out_field.text())
        self.store.setValue("test_to", self.send.test_to.text())
        self.store.setValue("attach", self.send.attach_field.text())
        self.store.setValue("pattern", self.send.pattern_field.text())
        self.store.setValue("column", self.send.column_field.text())
        self.store.setValue("pause", self.send.pause.value())

    def closeEvent(self, event) -> None:
        """A run in flight is a reason to ask, not to stop.

        Closing the window mid-run would cut the thread off between two messages, leaving the
        report short of what actually went out -- and that report is what a resumed run reads.
        """
        if self.controller.busy:
            answer = QMessageBox.question(
                self, "A run is going",
                "Messages are still being sent. Stop after the current one and close?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.controller.cancel()
            self.controller.wait()

        self.templates.commit()
        self._remember()
        self.log.detach()
        super().closeEvent(event)
