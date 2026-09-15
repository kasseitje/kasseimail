"""Starting the window.

Two ways in, and they end up in the same place: `kasseimail gui` and the `kasseimail-gui` console
script, the latter for a desktop launcher that cannot pass an argument.
"""

import locale
import os
import sys

from loguru import logger

from kasseimail import config, logs


def build_application(argv=None):
    """The QApplication, with the locale Qt would have changed put back.

    **Every QApplication in this project has to be built here.** Constructing one calls
    `setlocale(LC_ALL, "")`, which adopts the desktop's locale -- and `{{ due | date("%d %B %Y") }}`
    goes through `strftime`, so the very same template comes out "01 March 2026" from the command
    line and "01 maart 2026" from the window. A mail that reads differently depending on which front
    end sent it is the kind of difference nobody finds until a recipient points at it.

    It is a shared helper rather than a few lines inside `run_gui` because the trap caught this
    project twice: the second time was a test fixture building its own QApplication, which then
    changed how every later test in the session rendered a date.
    """
    from PySide6.QtWidgets import QApplication

    before = locale.setlocale(locale.LC_ALL)

    app = QApplication.instance() or QApplication(argv if argv is not None else [])

    try:
        locale.setlocale(locale.LC_ALL, before)
    except locale.Error:  # pragma: no cover -- a locale Python cannot set back is not fatal
        pass

    # -- Fusion rather than the platform theme: it is the one style that looks the same and stays
    #    legible on every desktop this runs on, light or dark.
    app.setStyle("Fusion")
    app.setApplicationName("kasseimail")
    app.setOrganizationName("kasseimail")
    return app


def run_gui(settings=None) -> int:
    """Open the window on a resolved `Settings`. Returns the exit code Qt gives back."""
    from kasseimail.ui.main_window import MainWindow

    if settings is None:
        settings = config.load_settings()

    app = build_application(sys.argv)

    window = MainWindow(settings)
    window.show()

    code = app.exec()

    # -- If a worker never stopped -- a socket read with minutes still to run on it -- its thread
    #    was orphaned rather than destroyed mid-flight, because destroying a running QThread aborts
    #    the process. Returning normally here would destroy it anyway during interpreter shutdown
    #    and abort exactly the same, as the last thing the user sees.
    #
    #    So: leave now. The window is closed, the report is written and flushed, and there is
    #    nothing left for this process to do but wait on a socket nobody is listening to.
    from kasseimail.ui.worker import _ORPHANED

    if _ORPHANED:
        logger.debug("leaving {} unfinished worker(s) behind at exit", len(_ORPHANED))
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, ValueError):
                pass
        os._exit(code)

    return code


def main() -> None:
    """The `kasseimail-gui` console script."""
    config.require("PySide6")
    logs.setup()
    sys.exit(run_gui())


if __name__ == "__main__":
    main()
