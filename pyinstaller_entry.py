"""Where a frozen build starts.

**One script, two executables.** PyInstaller analyses this once and `kasseimail.spec` builds two
programs from the result, sharing a single bundled runtime:

    kasseimail-gui.exe   opens the window
    kasseimail.exe       is the command line

Which one you started decides what happens, by the name it was given. Two separate builds would
double a ~200 MB bundle to say the same thing twice, and the halves would then be able to drift.

Not imported by anything in the package -- PyInstaller is the only caller, and `uv run kasseimail`
goes through the console scripts in pyproject.toml instead.
"""

import sys

from pathlib import Path

#: a build whose executable ends in this runs the window rather than the CLI.
GUI_SUFFIXES = ("-gui", "_gui")


def main() -> None:
    name = Path(sys.argv[0]).stem.lower()

    if name.endswith(GUI_SUFFIXES):
        from kasseimail import config, logs
        from kasseimail.ui.app import run_gui

        # -- `logs.setup` copes with the missing stderr of a windowed build; the log pane is where
        #    these lines are meant to be read anyway.
        logs.setup()
        sys.exit(run_gui(config.load_settings()))

    from kasseimail.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
