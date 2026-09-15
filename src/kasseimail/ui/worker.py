"""Running a send off the GUI thread.

A run of seventy messages is seventy HTTP calls with a pause between each -- three minutes in which
a window that did the work on its own thread would be a grey rectangle the desktop offers to kill.
So `SendRun.execute` happens on a `QThread` and reports back through signals.

Two things travel the other way, and both go through a `threading.Event` or a signal rather than a
shared variable:

- **Cancel**, which `execute` polls *between* messages. Never in the middle of one: stopping there
  could leave a half-uploaded draft with nothing in the report about it.
- **The device code**, because `acquire_token_by_device_flow` blocks until somebody types the code
  into a browser. It blocks *here*, on the worker thread, while the dialog it asked for is shown on
  the GUI thread.
"""

import threading
import time

from PySide6.QtCore import QObject, QThread, Signal

from kasseimail.graph import SignInProblem
from kasseimail.run import RunProblem, SendRun

#: how long a worker waits for the GUI thread to put the device code on screen before carrying on
#: without it. A ceiling rather than a wait forever: if the dialog never appears the run should end
#: with a sign-in error rather than a thread nobody can reach.
CODE_DIALOG_TIMEOUT = 10.0


class SendWorker(QObject):
    """Owns a `SendRun` and runs it. Moved onto a `QThread` by `SendController`."""

    progress = Signal(object)          # run.Progress
    device_code = Signal(object)       # the MSAL flow dict
    finished = Signal(object)          # run.RunSummary
    failed = Signal(str)

    def __init__(self, run: SendRun):
        super().__init__()
        self.run = run
        self._cancel = threading.Event()
        # -- set by the GUI thread once the dialog is up, so the worker does not race ahead and
        #    start the blocking wait before the code is on screen.
        self._code_shown = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()
        # -- a run that is still waiting on a device code has nothing to interrupt between
        #    messages, because it has not reached the first one. Stop the polling too.
        self.run.cancel_sign_in()

    def start(self) -> None:
        """The slot the thread's `started` signal is wired to."""
        try:
            summary = self.run.execute(
                progress=self.progress.emit,
                should_cancel=self._cancel.is_set,
                on_device_code=self._on_device_code,
            )
            self.finished.emit(summary)
        except RunProblem as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # pragma: no cover -- anything unforeseen still has to surface
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _on_device_code(self, flow: dict) -> None:
        """Ask the GUI thread to show the code, then let MSAL block waiting for it.

        Waited in slices, watching for a cancel. The GUI thread is what sets `_code_shown`, and a
        window that is closing may never get round to it -- a flat ten-second wait then holds the
        close open for ten seconds over a dialog nobody is going to look at.
        """
        self._code_shown.clear()
        self.device_code.emit(flow)

        deadline = time.monotonic() + CODE_DIALOG_TIMEOUT
        while not self._code_shown.is_set() and not self._cancel.is_set():
            if time.monotonic() >= deadline:
                break
            self._code_shown.wait(timeout=0.05)

    def code_is_showing(self) -> None:
        self._code_shown.set()


class SignInWorker(QObject):
    """Signing in, off the GUI thread.

    Its own worker rather than a corner of `SendWorker`, because signing in from the menu is a
    thing you do *before* a run -- and it has to be off the GUI thread for the same reason the run
    does. `acquire_token_by_device_flow` polls until somebody enters the code, up to about fifteen
    minutes. On the GUI thread that is fifteen minutes of a window that does not repaint, which the
    desktop reports as "not responding" and offers to kill.
    """

    device_code = Signal(object)
    finished = Signal(object)      # graph.Account, or None
    failed = Signal(str)

    def __init__(self, mailer):
        super().__init__()
        self.mailer = mailer

    def cancel(self) -> None:
        """Call off the polling. Takes effect on MSAL's next poll, so within a few seconds."""
        self.mailer.cancel_sign_in()

    def start(self) -> None:
        try:
            self.mailer.sign_in(on_device_code=self.device_code.emit)
            self.finished.emit(self.mailer.cached_account())
        except SignInProblem as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # pragma: no cover -- a network that is simply not there
            self.failed.emit(f"{type(exc).__name__}: {exc}")


#: threads that would not stop in time, kept alive on purpose.
#:
#: A `QThread` destroyed while it is still running aborts the process -- Qt says so and means it.
#: When a worker is wedged in a socket read there is nothing to interrupt, so the choice is between
#: aborting on the way out and leaking a thread that the process exit will take with it anyway.
#: Leaking is the one that does not look like a crash to the person closing the window.
_ORPHANED: list[tuple] = []


class ThreadedTask(QObject):
    """One worker on one `QThread`, with the lifetime handled in a single place.

    Both controllers below need the same five lines and the same two mistakes avoided: a `QThread`
    whose Python wrapper is garbage-collected takes the running thread with it, and a worker deleted
    while its thread still runs crashes somewhere that names neither. Having written that twice, it
    lives here once.
    """

    failed = Signal(str)
    stopped = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _launch(self, worker: QObject) -> None:
        if self.busy:
            raise RuntimeError("a task is already running")

        self._thread = QThread()
        self._worker = worker
        worker.moveToThread(self._thread)

        self._thread.started.connect(worker.start)
        worker.failed.connect(self._done_with_error)
        self._thread.start()

    def wait(self, milliseconds: int = 10_000) -> bool:
        """Block until the worker stops. Returns whether it did.

        **Not for the GUI thread.** `quit()` only ends the thread's event loop and does nothing to
        a slot that is still running, so this is a plain block with no repaints and no events for
        however long the worker takes -- which is exactly what a frozen window is. The window
        closes through `detach` and a signal instead; this is for tests and for the CLI.
        """
        if self._thread is None or not self._thread.isRunning():
            return True

        self._thread.quit()
        return bool(self._thread.wait(milliseconds))

    def detach(self) -> None:
        """Let go of a worker that has not stopped, without destroying a running QThread.

        The window is closing and something -- a socket that will not time out for another two
        minutes -- is still going. Dropping the last reference here would destroy a running
        QThread and abort; keeping it in `_ORPHANED` costs a thread until the process exits, which
        is about to happen anyway.
        """
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            _ORPHANED.append((self._thread, self._worker))
            self._thread = self._worker = None
            self.stopped.emit()
            return

        self._teardown()

    def _done_with_error(self, message: str) -> None:
        self.failed.emit(message)
        self._teardown()

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            # -- short: this runs on the GUI thread, and by the time the worker has emitted the
            #    signal that got us here its slot has returned and only the event loop is left to
            #    unwind. A worker that somehow has not finished is orphaned rather than waited on.
            if not self._thread.wait(2000):
                _ORPHANED.append((self._thread, self._worker))
                self._thread = self._worker = None
                self.stopped.emit()
                return
            self._thread.deleteLater()
        if self._worker is not None:
            self._worker.deleteLater()
        self._thread = self._worker = None
        self.stopped.emit()


class SendController(ThreadedTask):
    """A `SendRun` on its own thread."""

    progress = Signal(object)
    device_code = Signal(object)
    finished = Signal(object)

    def start(self, run: SendRun) -> None:
        worker = SendWorker(run)
        worker.progress.connect(self.progress)
        worker.device_code.connect(self.device_code)
        worker.finished.connect(self._done)
        self._launch(worker)

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def code_is_showing(self) -> None:
        if self._worker is not None:
            self._worker.code_is_showing()

    def _done(self, summary) -> None:
        self.finished.emit(summary)
        self._teardown()


class SignInController(ThreadedTask):
    """A device-code sign-in on its own thread."""

    device_code = Signal(object)
    finished = Signal(object)

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def start(self, mailer) -> None:
        worker = SignInWorker(mailer)
        worker.device_code.connect(self.device_code)
        worker.finished.connect(self._done)
        self._launch(worker)

    def _done(self, account) -> None:
        self.finished.emit(account)
        self._teardown()
