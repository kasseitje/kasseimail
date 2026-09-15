"""The window -- enough of it that a broken import or a disconnected signal fails here.

Skipped whole when PySide6 is not installed, which is the normal state on a machine that only runs
the CLI. Everything runs on Qt's offscreen platform, so it needs no display.

This does not try to test the engine through the GUI -- that is what the other modules are for.
What it pins is the wiring: the panels talk to each other, the worker thread reports back, loguru
reaches the log pane from that thread, and Cancel stops a run between messages rather than during
one.
"""

import os

import pytest

pytest.importorskip("PySide6", reason="the gui group is not installed")

# -- must be set before the first QApplication, so before any other Qt import does it for us.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from kasseimail import config  # noqa: E402
from kasseimail.ledger import MODE_SEND  # noqa: E402
from kasseimail.ui.main_window import MainWindow  # noqa: E402
from kasseimail.ui.recipients_panel import RecipientModel  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    """One QApplication for the session -- Qt refuses a second one in the same process."""
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    yield app


@pytest.fixture
def dialogs(monkeypatch):
    """Record what a message box would have said instead of showing it.

    Two reasons. A modal dialog blocks the thread the test is pumping events on, so a run that
    ends in one would hang the suite forever. And a dialog that was *raised* is often the thing
    worth asserting -- a confirmation before sending is a feature, not a side effect.
    """
    from PySide6.QtWidgets import QMessageBox

    shown = []

    def record(kind, answer=QMessageBox.Yes):
        def fake(_parent, title, text, *args, **kwargs):
            shown.append((kind, title, text))
            return answer

        return fake

    for name in ("information", "warning", "critical", "question", "about"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(record(name)))

    # -- the send confirmation builds a QMessageBox rather than calling a static method.
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(
        ("confirm", self.windowTitle(), self.text())) or QMessageBox.Yes)

    return shown


@pytest.fixture
def window(qt_app, dialogs, template_dir, make_template, people, tmp_path):
    from PySide6.QtCore import QSettings

    make_template(
        "invoice",
        subject="Invoice {{ invoice_no }} for {{ company }}\n",
        html="<p>Dear {{ first_name }}, {{ amount | money }}</p>\n",
        text="Dear {{ first_name }}\n",
    )

    settings = config.load_settings(template_dir=template_dir)
    # -- its own store, so nothing typed in a real session reaches a test.
    store = QSettings(str(tmp_path / "state.ini"), QSettings.IniFormat)
    window = MainWindow(settings, store=store)
    window.templates.select("invoice")
    window.recipients.load(people)
    window.send.out_field.setText(str(tmp_path / "out"))
    window.send.pause.setValue(0)

    yield window

    window.log.detach()
    window.controller.cancel()
    window.controller.wait(5000)
    window.close()


# -- the panels talk to each other ---------------------------------------------------------------

def test_selecting_a_row_previews_the_template_against_it(window):
    """The preview is the whole reason the two panels are in one window. Rendered against
    placeholder text it would show none of the things that actually go wrong."""
    window.recipients.view.selectRow(0)

    preview = window.templates.preview.toPlainText()

    assert "Invoice 1001 for Peeters & Zn" in preview
    assert "Dear Jan" in preview


def test_the_preview_never_shows_a_number_as_a_float(window):
    """The bug this whole tool is most likely to ship: 1001.0 in front of a customer. If it ever
    comes back, it is visible here first."""
    window.recipients.view.selectRow(0)

    assert "1001.0" not in window.templates.preview.toPlainText()


def test_editing_the_body_updates_the_preview_and_the_file(window):
    """The editor writes to disk and the engine reads from disk, so a preview that disagreed with
    the file would be showing something that is not what gets sent."""
    window.recipients.view.selectRow(0)
    window.templates.tabs.setCurrentIndex(1)

    window.templates._editors["body.html.j2"].setPlainText("<p>Rewritten for {{ first_name }}</p>")
    window.templates._flush()

    assert "Rewritten for Jan" in window.templates.preview.toPlainText()
    assert "Rewritten" in (window.templates.template.directory / "body.html.j2").read_text()


def test_a_template_error_is_shown_in_place_rather_than_as_a_dialog(window):
    """While you are typing, a half-finished `{{ ` is an error on nearly every keystroke. A modal
    dialog each time would make the editor unusable."""
    window.recipients.view.selectRow(0)

    window.templates._editors["body.html.j2"].setPlainText("<p>{{ not_a_column }}</p>")
    window.templates._flush()

    assert "not_a_column" in window.templates.preview.toPlainText()


def test_changing_the_template_clears_a_stale_preflight(window, make_template):
    """The red rows described the previous template. Leaving them up would say a row is fine when
    the template it was checked against is gone."""
    make_template("reminder", subject="Reminder\n", html="<p>Reminder</p>\n")
    window.templates.reload()
    window._validate()
    assert window._last_preflight is not None

    window.templates.select("reminder")

    assert window._last_preflight is None


# -- the table ------------------------------------------------------------------------------------

def test_the_table_shows_the_headers_as_the_file_wrote_them(window):
    """The template uses `first_name`; this table is for recognising your own spreadsheet."""
    model = window.recipients.model
    headers = [model.headerData(c, Qt.Horizontal, Qt.DisplayRole)
               for c in range(model.columnCount())]

    assert headers[:3] == ["Row", "Status", "Attachments"]
    assert "First Name" in headers


def test_a_date_is_shown_as_a_date_and_not_as_a_timestamp(window):
    """`str()` on a datetime gives `2026-03-01 00:00:00`, which is wide enough to push every other
    column off screen."""
    model = window.recipients.model
    due = model.columnCount() - 2

    assert model.data(model.index(0, due), Qt.DisplayRole) == "2026-03-01"


def test_preflight_marks_each_row_and_tints_the_broken_ones(window, tmp_path):
    """Three hundred rows with four wrong is unreadable as a list and obvious as four red lines."""
    window.send.pattern_field.setText("nowhere/{{ invoice_no }}.pdf")
    window._validate()

    model = window.recipients.model
    statuses = [model.data(model.index(r, 1), Qt.DisplayRole) for r in range(model.rowCount())]

    assert "error" in statuses
    assert "no address" in statuses
    assert model.data(model.index(0, 0), Qt.BackgroundRole) is not None


def test_the_row_number_shown_is_the_spreadsheet_s_own(window):
    model = window.recipients.model

    assert [model.data(model.index(r, 0), Qt.DisplayRole) for r in range(model.rowCount())] == \
        [2, 3, 4, 5]


def test_an_empty_model_does_not_fall_over():
    """The window opens before any file is chosen."""
    model = RecipientModel()

    assert model.rowCount() == 0 and model.columnCount() == 0
    assert model.data(model.index(0, 0), Qt.DisplayRole) is None


# -- the worker thread ------------------------------------------------------------------------------

def run_in_window(window, qt_app, fake_mailer, *, cancel_after=None, mode=MODE_SEND):
    """Drive one run through the controller and pump the event loop until it reports back."""
    from PySide6.QtCore import QTimer

    run = window._build_run(mode)
    mailer = fake_mailer()
    run._mailer = mailer

    outcome = {}
    window.controller.finished.connect(lambda summary: outcome.setdefault("summary", summary))
    window.controller.failed.connect(lambda message: outcome.setdefault("failed", message))

    if cancel_after is not None:
        window.controller.progress.connect(
            lambda event: window.controller.cancel() if event.index >= cancel_after else None
        )

    window.send.set_busy(True, total=3)
    window.controller.start(run)

    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.start(15_000)
    while not outcome and timeout.isActive():
        qt_app.processEvents()

    return outcome, mailer


def test_a_run_happens_off_the_gui_thread_and_reports_back(window, qt_app, fake_mailer):
    """Seventy messages with a pause between them is minutes of a window the desktop would offer
    to kill."""
    window.send.test_to.setText("me@ours.be")

    outcome, mailer = run_in_window(window, qt_app, fake_mailer)

    assert "failed" not in outcome, outcome.get("failed")
    assert outcome["summary"].delivered == 3
    assert len(mailer.sent) == 3


def test_the_log_pane_shows_lines_logged_from_the_worker_thread(window, qt_app, fake_mailer):
    """loguru calls its sink on whichever thread logged, and a widget may only be touched from the
    GUI thread. The queued signal in LogPanel is what makes this land safely instead of crashing
    somewhere unrelated later."""
    window.log.attach()
    window.send.test_to.setText("me@ours.be")

    run_in_window(window, qt_app, fake_mailer)

    assert "sent to me@ours.be" in window.log.view.toPlainText()


def test_progress_reaches_the_bar(window, qt_app, fake_mailer):
    run_in_window(window, qt_app, fake_mailer)

    assert window.send.progress.value() == 3


def test_cancelling_stops_between_messages(window, qt_app, fake_mailer):
    """Never during one: stopping mid-message could leave a half-uploaded draft with nothing in
    the report about it, and the report is what a resumed run reads.

    The pause is real here. Against a fake mailer with no pause the worker finishes all three
    before the GUI thread has processed the first progress event, and the test would be asserting
    that a cancel arriving after the end does nothing -- which is true and not the point.
    """
    window.send.pause.setValue(1)

    outcome, mailer = run_in_window(window, qt_app, fake_mailer, cancel_after=1)

    assert outcome["summary"].cancelled
    assert len(mailer.sent) < 3


def test_the_buttons_come_back_when_a_run_ends(window, qt_app, fake_mailer):
    """A Send button left disabled after a run makes the window look hung."""
    run_in_window(window, qt_app, fake_mailer)
    qt_app.processEvents()

    assert window.send.send_button.isEnabled()
    assert not window.send.cancel_button.isEnabled()


# -- what the panel collected -----------------------------------------------------------------------

def test_the_attachment_rules_typed_in_the_window_reach_the_run(window, tmp_path, make_files):
    make_files("handbook.pdf")
    window.send.attach_field.setText("handbook.pdf")
    window.send.column_field.setText("attachment")
    window.send.root_field.setText(str(tmp_path))

    run = window._build_run(MODE_SEND)

    assert run.spec.common == ["handbook.pdf"]
    assert run.spec.columns == ["attachment"]
    assert run.spec.root == tmp_path


def test_a_column_named_with_a_capital_still_matches(window, tmp_path):
    """Somebody types the header as they see it in Excel; the engine works in normalised names."""
    window.send.column_field.setText("Attachment")

    assert window._build_run(MODE_SEND).spec.columns == ["attachment"]


def test_the_templates_own_patterns_are_used_alongside_the_typed_one(window, make_template,
                                                                    tmp_path):
    make_template("withmeta", meta='attachments = ["handbook.pdf"]\n')
    window.templates.reload()
    window.templates.select("withmeta")
    window.send.pattern_field.setText("invoices/{{ invoice_no }}.pdf")

    run = window._build_run(MODE_SEND)

    assert run.spec.patterns == ["handbook.pdf", "invoices/{{ invoice_no }}.pdf"]


def test_pending_edits_are_written_before_a_run_is_built(window):
    """Otherwise the run uses the file as it was before the last keystroke, and what goes out is
    not what the preview showed."""
    window.templates._editors["subject.j2"].setPlainText("Edited subject\n")

    run = window._build_run(MODE_SEND)

    context = window.recipients.table.rows[0].context()
    assert run.template.render(context).subject == "Edited subject"


# -- signing in ------------------------------------------------------------------------------------

class PollingMailer:
    """A mailer that blocks the way MSAL's device flow does, and stops the same way.

    The real thing polls until the code is entered or expires -- about fifteen minutes -- and
    checks `flow["expires_at"]` between polls. Both halves matter here: the blocking is what the
    worker thread exists for, and the check is the only way out of it.
    """

    def __init__(self, seconds=900):
        self._flow = None
        self.thread = None
        self.seconds = seconds

    def sign_in(self, on_device_code=None, silent_only=False):
        import threading
        import time

        self.thread = threading.current_thread()
        self._flow = {"user_code": "ABCD-1234", "expires_at": time.time() + self.seconds,
                      "verification_uri": "https://microsoft.com/devicelogin", "message": "go"}
        on_device_code(self._flow)

        while self._flow.get("expires_at", 0) > time.time():
            time.sleep(0.02)

        from kasseimail.graph import SignInProblem

        raise SignInProblem("Sign-in was cancelled.")

    def cancel_sign_in(self):
        if self._flow is None:
            return False
        self._flow["expires_at"] = 0
        return True

    def cached_account(self):
        return None


def pump(qt_app, until, seconds=10):
    import time

    end = time.time() + seconds
    while not until() and time.time() < end:
        qt_app.processEvents()
        time.sleep(0.005)
    qt_app.processEvents()


def test_signing_in_does_not_block_the_window(window, qt_app, monkeypatch):
    """`acquire_token_by_device_flow` polls for up to fifteen minutes. Run on the GUI thread that
    is fifteen minutes without a repaint, which the desktop reports as "not responding" and offers
    to kill -- and pumping the event loop by hand to get the dialog painted lets a close event
    through, tearing the window down while the sign-in is still on the stack holding it.
    """
    import threading

    mailer = PollingMailer()
    monkeypatch.setattr(window, "_mailer", lambda: mailer)

    window._sign_in()
    pump(qt_app, lambda: window.device_dialog is not None)

    assert window.device_dialog is not None, "the code has to be on screen while it polls"
    assert window.device_dialog.code_field.text() == "ABCD-1234"
    assert mailer.thread is not threading.main_thread()

    window.signin.cancel()
    pump(qt_app, lambda: not window.signin.busy)


def test_the_window_stays_live_while_a_sign_in_polls(window, qt_app, monkeypatch):
    """The point of the thread, stated as the thing a person would notice."""
    from PySide6.QtCore import QTimer

    monkeypatch.setattr(window, "_mailer", lambda: PollingMailer())

    ticks = []
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: ticks.append(1))
    heartbeat.start(20)

    window._sign_in()
    pump(qt_app, lambda: len(ticks) > 10, seconds=5)

    assert len(ticks) > 10, "the event loop stopped turning"

    heartbeat.stop()
    window.signin.cancel()
    pump(qt_app, lambda: not window.signin.busy)


def test_cancelling_from_the_dialog_stops_the_sign_in(window, qt_app, monkeypatch):
    """The button says "Cancel sign-in", so it has to mean it. Only hiding the dialog would leave
    a thread polling for a quarter of an hour with nothing on screen to explain it."""
    monkeypatch.setattr(window, "_mailer", lambda: PollingMailer())

    window._sign_in()
    pump(qt_app, lambda: window.device_dialog is not None)

    window.device_dialog._cancel()
    pump(qt_app, lambda: not window.signin.busy)

    assert not window.signin.busy
    assert window.device_dialog is None


def test_a_second_sign_in_raises_the_dialog_instead_of_starting_another(window, qt_app,
                                                                       monkeypatch):
    """Two device codes for one sign-in is two codes that both look right and one that works."""
    monkeypatch.setattr(window, "_mailer", lambda: PollingMailer())

    window._sign_in()
    pump(qt_app, lambda: window.device_dialog is not None)
    first = window.device_dialog

    window._sign_in()

    assert window.device_dialog is first

    window.signin.cancel()
    pump(qt_app, lambda: not window.signin.busy)


def test_credentials_microsoft_rejects_do_not_take_the_window_down(qt_app, template_dir,
                                                                   make_template, tmp_path,
                                                                   dialogs, monkeypatch):
    """The window asks who is signed in from its own constructor. A tenant MSAL will not resolve
    raises there, and the application used to die before it was ever on screen."""
    import msal
    from PySide6.QtCore import QSettings

    make_template("invoice")

    def refuse(*args, **kwargs):
        raise ValueError("Unable to get authority configuration for ...")

    monkeypatch.setattr(msal, "PublicClientApplication", refuse)

    settings = config.load_settings(template_dir=template_dir)
    settings.tenant_id, settings.client_id = "not-a-tenant", "client"

    built = MainWindow(settings, store=QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    try:
        assert "credentials rejected" in built.windowTitle()
    finally:
        built.log.detach()
        built.close()
