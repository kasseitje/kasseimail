"""The version, which comes from the git tag and nowhere else.

`hatch-vcs` derives it from `git describe` at build time and writes `_version.py` next to this file,
so a build can never claim a number that was never tagged. There is no literal here to drift from it.

Three places are tried, in this order, because each covers a case the next one does not:

1. **`_version.py`** -- generated, gitignored, and included in the wheel. The only one that works in
   a frozen executable: PyInstaller bundles no package metadata, so `importlib.metadata` finds
   nothing at all there.
2. **The installed metadata** -- for a checkout whose `_version.py` has not been written yet, which
   is every checkout before its first `uv sync`.
3. **`git describe`** -- for running straight out of a checkout that was never installed, e.g.
   `python -m kasseimail.cli`. Last, because it shells out.
"""

import subprocess
import sys

from pathlib import Path

__all__ = ["__version__", "version_tuple"]


def _from_generated_file() -> str | None:
    try:
        from kasseimail._version import __version__ as generated
    except ImportError:
        return None
    return generated or None


def _from_metadata() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("kasseimail")
    except PackageNotFoundError:
        return None


def _from_git() -> str | None:
    """`git describe` in the checkout this file lives in. Never on a frozen build.

    A frozen executable has no repository around it, and the directory it was started from may well
    be one -- somebody else's. Asking git there would report a version belonging to another project.
    """
    if getattr(sys, "frozen", False):
        return None

    root = Path(__file__).resolve().parent.parent.parent
    if not (root / ".git").exists():
        return None

    try:
        described = subprocess.check_output(
            ["git", "describe", "--tags", "--dirty", "--always"],
            cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    return described.strip() or None


def _resolve() -> str:
    for source in (_from_generated_file, _from_metadata, _from_git):
        found = source()
        if found:
            return found
    return "0.0.0+unknown"


__version__ = _resolve()


def version_tuple(text: str | None = None) -> tuple[int, int, int, int]:
    """The version as four integers, for a Windows executable's file-version resource.

    Windows wants exactly four numbers and nothing else, so everything a real version carries --
    `0.1.0.post1.dev2+g78eb379` -- has to be thrown away down to the leading numbers. Losing the
    suffix here is why the exe's Properties dialog also shows the full string separately.
    """
    numbers = []
    for piece in (text or __version__).split("+")[0].split("."):
        digits = "".join(character for character in piece if character.isdigit())
        if not digits or not piece[0].isdigit():
            break
        numbers.append(int(digits))

    numbers = (numbers + [0, 0, 0, 0])[:4]
    return tuple(numbers)
