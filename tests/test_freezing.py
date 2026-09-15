"""The ways the window could stop responding, and the reasons it no longer does.

A freeze is the worst kind of bug to report: there is nothing on screen, nothing in the log, and
the only thing the person can tell you is "it hung". So each of these pins a specific way the GUI
thread used to end up blocked, with the measurement that shows it no longer is.

The rule these all come back to: **the GUI thread never waits on the worker.** It asks the worker
to stop and carries on turning its event loop until the worker says it has.
"""

import os
import threading
import time

import pytest

pytest.importorskip("PySide6", reason="the gui group is not installed")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from kasseimail import config  # noqa: E402
from kasseimail.graph import SignInProblem  # noqa: E402
from kasseimail.run import interruptible_sleep  # noqa: E402
from kasseimail.ui.main_window import MainWindow  # noqa: E402
from kasseimail.ui.worker import _ORPHANED  # noqa: E402

#: how long a stall has to be before it reads as a freeze rather than a hitch.
RESPONSIVE_MS = 0.5


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class Heartbeat:
    """Measures the longest stretch in which the event loop did not turn.

    The honest test of "does the window freeze": not whether a call returned quickly, but whether
    the loop kept beating throughout. A `QThread.wait()` on the GUI thread returns eventually and
    the window is dead for the whole of it.
    """

    def __init__(self, app, interval_ms=25):
        self.app = app
        self.ticks = []
        self.timer = QTimer()
        self.timer.timeout.connect(lambda: self.ticks.append(time.monotonic()))
        self.timer.start(interval_ms)

    def spin(self, until, seconds=20):
        end = time.monotonic() + seconds
        while not until() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    @property
    def worst_stall(self) -> float:
        gaps = [b - a for a, b in zip(self.ticks, self.ticks[1:])]
        return max(gaps) if gaps else 0.0

    def stop(self):
        self.timer.stop()


class SlowMailer:
    """A Graph that takes a while, the way a real one does.

    The wait is on an Event rather than a plain sleep so a test can let a "wedged" worker go at
    the end. Nothing in the product can do that -- a socket read is a socket read -- but leaving a
    ten-minute thread running past the end of the suite means pytest exits by destroying a live
    QThread, which aborts and buries the result it just printed.
    """

    def __init__(self, seconds=1.0):
        self.seconds = seconds
        self.delivered = 0
        self.released = threading.Event()

    def deliver(self, delivery, *, send, on_chunk=None):
        self.released.wait(timeout=self.seconds)
        self.delivered += 1
        return "draft-x"

    def release(self):
        self.released.set()

    def cached_account(self):
        return None

    def cancel_sign_in(self):
        return False


class PollingMailer:
    """A device-code sign-in that polls, and stops the way MSAL does."""

    def __init__(self):
        self._flow = None

    def sign_in(self, on_device_code=None, silent_only=False):
        self._flow = {"user_code": "ABCD-1234", "expires_at": time.time() + 900,
                      "verification_uri": "https://x", "message": "go"}
        on_device_code(self._flow)
        while self._flow.get("expires_at", 0) > time.time():
            time.sleep(0.02)
        raise SignInProblem("cancelled")

    def cancel_sign_in(self):
        if self._flow is None:
            return False
        self._flow["expires_at"] = 0
        return True

    def cached_account(self):
        return None


#: every mailer a test started, so the fixture can let them go however the test ended.
_STARTED: list = []


@pytest.fixture
def window(qt_app, dialogs, template_dir, make_template, people, tmp_path):
    make_template("invoice", subject="Invoice {{ invoice_no }}\n", html="<p>{{ first_name }}</p>\n")

    store = QSettings(str(tmp_path / "state.ini"), QSettings.IniFormat)
    store.clear()
    built = MainWindow(config.load_settings(template_dir=template_dir), store=store)
    built.templates.select("invoice")
    built.recipients.load(people)
    built.send.out_field.setText(str(tmp_path / "out"))
    built.show()

    yield built

    built.log.detach()
    # -- let any "wedged" worker finish before the thread is dropped, so the suite does not exit
    #    by destroying a live QThread.
    for mailer in _STARTED:
        mailer.release()
    _STARTED.clear()

    built.controller.wait(5000)
    built.signin.wait(5000)
    built.controller.detach()
    built.signin.detach()
    if built.isVisible():
        built._stopping = True
        built.close()


def start_run(window, mailer, mode="drafts", pause=0):
    _STARTED.append(mailer)
    window.send.pause.setValue(pause)
    run = window._build_run(mode)
    assert run is not None
    run._mailer = mailer
    window.send.set_busy(True, total=3)
    window.controller.start(run)


# -- the interruptible waits -------------------------------------------------------------------

def test_a_pause_between_messages_can_be_cut_short():
    """The pause is where a run spends most of its time -- 2.5s by default and far more when
    somebody is being careful about throttling. A plain sleep cannot be interrupted, so Cancel and
    the close box both appear to do nothing until it happens to end."""
    started = time.monotonic()

    finished = interruptible_sleep(5.0, lambda: time.monotonic() - started > 0.2)

    assert finished is False
    assert time.monotonic() - started < 1.0


def test_a_pause_nobody_cancels_still_waits_the_whole_time():
    """The other half: throttling only works if the pause is actually observed."""
    started = time.monotonic()

    assert interruptible_sleep(0.3) is True
    assert time.monotonic() - started >= 0.28


def test_waiting_out_graph_s_throttling_can_be_cancelled(monkeypatch):
    """Graph is entitled to ask for minutes with a Retry-After, and that used to be minutes in
    which the Cancel button and the close box both did nothing."""
    from kasseimail.graph import GraphMailer, GraphProblem

    mailer = GraphMailer("00000000-0000-0000-0000-000000000000", "client", "token.json")
    mailer._token, mailer._expires_at = "t", time.time() + 3600
    mailer.should_cancel = lambda: True

    class Throttled:
        status_code = 429
        headers = {"Retry-After": "600"}
        text = ""

        def json(self):
            return {}

    mailer._session = type("S", (), {"request": lambda *a, **k: Throttled()})()

    started = time.monotonic()
    with pytest.raises(GraphProblem) as refusal:
        mailer._request("POST", "https://example/x")

    assert "cancelled" in str(refusal.value)
    assert time.monotonic() - started < 1.0


# -- closing ---------------------------------------------------------------------------------

def test_closing_during_a_run_does_not_block_the_gui_thread(window, qt_app):
    """The freeze this whole module is about. `QThread.wait()` in closeEvent is a plain block:
    no repaints, no events, for as long as the worker takes -- a message in flight plus a pause
    is easily ten seconds of a window the desktop offers to kill."""
    mailer = SlowMailer(seconds=1.0)
    start_run(window, mailer, pause=5)

    beat = Heartbeat(qt_app)
    beat.spin(lambda: mailer.delivered >= 1, seconds=5)

    started = time.monotonic()
    window.close()
    returned_in = time.monotonic() - started

    beat.spin(lambda: not window.isVisible(), seconds=20)
    beat.stop()

    assert returned_in < RESPONSIVE_MS, "closeEvent blocked the GUI thread"
    assert beat.worst_stall < RESPONSIVE_MS, f"the window stalled for {beat.worst_stall:.2f}s"
    assert not window.isVisible()


def test_the_window_stays_up_and_says_so_while_it_stops(window, qt_app):
    """Closing is refused and deferred rather than blocking, so there has to be something on
    screen explaining why the window is still there."""
    start_run(window, SlowMailer(seconds=1.0), pause=0)

    beat = Heartbeat(qt_app)
    beat.spin(lambda: window.controller.busy, seconds=3)

    window.close()

    assert window.isVisible()
    assert "stopping" in window.send.status.text().lower()

    beat.spin(lambda: not window.isVisible(), seconds=20)
    beat.stop()


def test_a_five_second_pause_does_not_add_five_seconds_to_the_close(window, qt_app):
    """What made the real one so noticeable: the cancel was only seen after the pause, so closing
    took the whole of it with the window dead."""
    mailer = SlowMailer(seconds=0.2)
    start_run(window, mailer, pause=5)

    beat = Heartbeat(qt_app)
    beat.spin(lambda: mailer.delivered >= 1, seconds=5)

    started = time.monotonic()
    window.close()
    beat.spin(lambda: not window.isVisible(), seconds=20)
    beat.stop()

    assert time.monotonic() - started < 3.0, "the pause was waited out before stopping"
    assert beat.worst_stall < RESPONSIVE_MS


def test_closing_during_a_sign_in_does_not_block_either(window, qt_app, monkeypatch):
    """The device flow polls for a quarter of an hour. Closing has to call it off, not wait."""
    monkeypatch.setattr(window, "_mailer", lambda m=PollingMailer(): m)
    window._sign_in()

    beat = Heartbeat(qt_app)
    beat.spin(lambda: window.signin.busy, seconds=3)
    assert window.signin.busy

    started = time.monotonic()
    window.close()
    returned_in = time.monotonic() - started

    beat.spin(lambda: not window.isVisible(), seconds=20)
    beat.stop()

    assert returned_in < RESPONSIVE_MS
    assert beat.worst_stall < RESPONSIVE_MS
    assert not window.isVisible()


def test_a_worker_that_never_stops_is_let_go_of_rather_than_waited_on(window, qt_app,
                                                                     monkeypatch):
    """A socket read with two minutes left on it cannot be interrupted. The window must still
    close: a window that will not close is worse than a leaked thread, and destroying a running
    QThread aborts the process outright."""
    monkeypatch.setattr(MainWindow, "STOP_TIMEOUT_MS", 1000)
    before = len(_ORPHANED)

    start_run(window, SlowMailer(seconds=600), pause=0)

    beat = Heartbeat(qt_app)
    beat.spin(lambda: window.controller.busy, seconds=3)

    window.close()
    beat.spin(lambda: not window.isVisible(), seconds=15)
    beat.stop()

    assert not window.isVisible(), "the window never closed"
    assert beat.worst_stall < RESPONSIVE_MS
    assert len(_ORPHANED) > before, "the thread was dropped rather than kept alive"


def test_saying_no_to_the_question_leaves_the_window_open(window, qt_app, monkeypatch):
    """The question is worth asking, so the answer has to be worth something."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    start_run(window, SlowMailer(seconds=0.2), pause=0)

    beat = Heartbeat(qt_app)
    beat.spin(lambda: window.controller.busy, seconds=3)

    window.close()

    assert window.isVisible()
    assert not window._stopping

    window.controller.cancel()
    beat.spin(lambda: not window.controller.busy, seconds=10)
    beat.stop()


def test_closing_with_nothing_running_is_immediate(window, qt_app):
    """The ordinary case must not have grown a deferral it does not need."""
    started = time.monotonic()
    window.close()

    assert not window.isVisible()
    assert time.monotonic() - started < RESPONSIVE_MS


def test_no_dialog_is_raised_at_the_end_of_a_run_that_was_closed(window, qt_app, dialogs):
    """A modal box on a window that is already going away holds the close open until somebody
    dismisses it -- and it is reporting a cancellation the person just asked for."""
    start_run(window, SlowMailer(seconds=0.2), pause=0)

    beat = Heartbeat(qt_app)
    beat.spin(lambda: window.controller.busy, seconds=3)
    dialogs.clear()

    window.close()
    beat.spin(lambda: not window.isVisible(), seconds=20)
    beat.stop()

    assert not [kind for kind, _title, _text in dialogs if kind != "question"], \
        f"a dialog was raised while closing: {dialogs}"
