"""loguru, set up once, for whichever front end is driving.

Everything in this package logs through `loguru.logger` and nothing configures it -- that happens
here, and only from an entry point. It is what lets the same engine print to a terminal under the
CLI and appear in a text pane under the GUI without knowing which is which: the GUI adds its own
sink and the lines arrive there too.

A run also gets a file sink in its output directory. A bulk send is the kind of thing somebody asks
about a week later ("did Jan get it?"), and the answer should not depend on whether the terminal
scrollback survived.
"""

import sys

from pathlib import Path

from loguru import logger

#: short and readable; the CLI is a foreground tool and a full timestamp per line is noise.
CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> <level>{message}</level>"
)

#: the file keeps the date and the module -- it is read after the fact, when neither is obvious.
FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} {level: <7} {name}:{function}:{line} {message}"


def setup(verbose: bool = False, quiet: bool = False) -> None:
    """Point the logger at stderr. Call once, from an entry point.

    stderr and not stdout, so a run whose output is piped somewhere keeps the progress lines on the
    terminal and the piped stream clean.

    **There may be no stderr at all.** A windowed PyInstaller build on Windows has no console, and
    Python sets `sys.stderr` to `None` there -- handing that to loguru raises, so the window would
    die on its very first line of setup. The GUI adds its own sink and shows the same lines in the
    log pane, so there is nothing to lose by skipping this one.
    """
    logger.remove()
    if quiet:
        level = "WARNING"
    else:
        level = "DEBUG" if verbose else "INFO"

    if sys.stderr is None:
        return

    logger.add(sys.stderr, format=CONSOLE_FORMAT, level=level, colorize=True, enqueue=False)


def add_run_log(directory: str | Path, name: str = "run.log") -> int:
    """A file sink for one run, in its output directory. Returns the handler id, to remove after.

    Always DEBUG: the point of the file is to hold what the console did not show.
    """
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)

    return logger.add(path / name, format=FILE_FORMAT, level="DEBUG", encoding="utf-8",
                      enqueue=False)


def remove(handler_id: int | None) -> None:
    """Drop a sink added above, tolerating one that is already gone."""
    if handler_id is None:
        return
    try:
        logger.remove(handler_id)
    except ValueError:
        pass
