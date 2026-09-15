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

from PySide6.QtCore import QObject, QThread, Signal

from kasseimail.run import RunProblem, SendRun


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
        """Ask the GUI thread to show the code, then let MSAL block waiting for it."""
        self._code_shown.clear()
        self.device_code.emit(flow)
        # -- a ceiling rather than a wait forever: if the dialog never appears, the run should end
        #    with a sign-in error instead of a thread nobody can reach.
        self._code_shown.wait(timeout=10)

    def code_is_showing(self) -> None:
        self._code_shown.set()


class SendController(QObject):
    """The thread, the worker and their wiring, so the window holds one object instead of three.

    It keeps references to both: a `QThread` whose Python wrapper is garbage-collected takes the
    running thread with it, and the crash that follows points at nothing useful.
    """

    progress = Signal(object)
    device_code = Signal(object)
    finished = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, run: SendRun) -> None:
        if self.busy:
            raise RuntimeError("a run is already going")

        self._thread = QThread()
        self._worker = SendWorker(run)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.start)
        self._worker.progress.connect(self.progress)
        self._worker.device_code.connect(self.device_code)
        self._worker.finished.connect(self._done)
        self._worker.failed.connect(self._done_with_error)

        self._thread.start()

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def code_is_showing(self) -> None:
        if self._worker is not None:
            self._worker.code_is_showing()

    def wait(self, milliseconds: int = 30_000) -> None:
        """Let a run in flight finish. Called when the window closes."""
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(milliseconds)

    def _done(self, summary) -> None:
        self.finished.emit(summary)
        self._teardown()

    def _done_with_error(self, message: str) -> None:
        self.failed.emit(message)
        self._teardown()

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread.deleteLater()
        if self._worker is not None:
            self._worker.deleteLater()
        self._thread = self._worker = None
        self.stopped.emit()
