"""Starting the window.

Two ways in, and they end up in the same place: `kasseimail gui` and the `kasseimail-gui` console
script, the latter for a desktop launcher that cannot pass an argument.
"""

import locale
import sys

from kasseimail import config, logs


def run_gui(settings=None) -> int:
    """Open the window on a resolved `Settings`. Returns the exit code Qt gives back."""
    from PySide6.QtWidgets import QApplication

    from kasseimail.ui.main_window import MainWindow

    if settings is None:
        settings = config.load_settings()

    # -- **Qt changes the C locale, and templates render through it.** Constructing a
    #    QApplication calls setlocale(LC_ALL, ""), which adopts the desktop's locale -- and
    #    `{{ due | date("%d %B %Y") }}` goes through strftime, so the very same template comes out
    #    "01 March 2026" from the command line and "01 maart 2026" from this window. A mail that
    #    reads differently depending on which front end sent it is the kind of difference nobody
    #    finds until a recipient points at it. So: whatever locale Python had, it still has.
    before = locale.setlocale(locale.LC_ALL)

    app = QApplication.instance() or QApplication(sys.argv)

    try:
        locale.setlocale(locale.LC_ALL, before)
    except locale.Error:  # pragma: no cover -- a locale Python cannot set back is not fatal
        pass

    # -- Fusion rather than the platform theme: it is the one style that looks the same and stays
    #    legible on every desktop this runs on, light or dark.
    app.setStyle("Fusion")
    app.setApplicationName("kasseimail")
    app.setOrganizationName("kasseimail")

    window = MainWindow(settings)
    window.show()

    return app.exec()


def main() -> None:
    """The `kasseimail-gui` console script."""
    config.require("PySide6")
    logs.setup()
    sys.exit(run_gui())


if __name__ == "__main__":
    main()
