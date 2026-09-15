"""The installed version, asked of the package metadata rather than kept here twice.

`pyproject.toml` is the single place the number is written. Reading it back through
`importlib.metadata` means a `--version` can never drift from what was actually installed, which a
second literal in this file would eventually do.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kasseimail")
except PackageNotFoundError:
    # -- running from a checkout that was never installed, e.g. `python -m kasseimail.cli`.
    __version__ = "0.0.0+unknown"
