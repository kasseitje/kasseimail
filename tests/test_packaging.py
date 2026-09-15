"""The install itself, and the guard around the optional Qt dependency.

None of this is exercised by the other tests: they import modules by path, never through a console
script, and they never ask whether Qt is installed. So an entry point that stopped resolving, or a
guard that stopped refusing, would go unnoticed until somebody typed the command.
"""

import importlib
import importlib.util
import tomllib

from pathlib import Path

import pytest

from kasseimail import cli, config

ROOT = Path(__file__).resolve().parent.parent


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# -- the console scripts ------------------------------------------------------------------------

@pytest.mark.parametrize("script", ["kasseimail", "kasseimail-gui"])
def test_every_console_script_points_at_something_that_exists(script):
    """An entry point naming a module that moved installs fine and then fails to import, and
    nothing else here would catch it -- every other test imports by path."""
    target = pyproject()["project"]["scripts"][script]
    module_name, _, function = target.partition(":")

    module = importlib.import_module(module_name)

    assert callable(getattr(module, function))


def test_the_main_entry_point_is_the_cli_module_s_own():
    assert pyproject()["project"]["scripts"]["kasseimail"] == "kasseimail.cli:main"
    assert cli.main is not None


# -- the optional group -------------------------------------------------------------------------

def test_qt_is_not_a_dependency_of_the_command_line_tool():
    """The whole reason the group exists: a machine that sends mail from cron has no display."""
    installed_everywhere = " ".join(pyproject()["project"]["dependencies"]).lower()

    assert "pyside" not in installed_everywhere
    assert "pyside6" in " ".join(pyproject()["dependency-groups"]["gui"]).lower()


def test_everything_the_engine_needs_is_a_plain_dependency():
    """The opposite mistake: `kasseimail send` on a fresh install must not need a group."""
    declared = " ".join(pyproject()["project"]["dependencies"]).lower()

    for package in ("jinja2", "loguru", "msal", "requests", "openpyxl", "platformdirs"):
        assert package in declared


# -- the guard ----------------------------------------------------------------------------------

def test_a_missing_package_refuses_and_says_which_command_installs_it(monkeypatch):
    """`kasseimail gui` ships with every install and Qt does not, so on most machines the command
    exists and cannot work. Without this the first sign is an ImportError from somewhere in the
    widget tree, which names a module rather than a thing to do."""
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)

    with pytest.raises(SystemExit) as refusal:
        config.require("PySide6")

    message = str(refusal.value)
    assert "PySide6" in message
    assert "uv sync --group gui" in message


def test_a_package_that_is_present_is_silent(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())

    assert config.require("PySide6") is None


def test_the_guard_asks_whether_the_package_exists_and_does_not_import_it(monkeypatch):
    """Importing PySide6 costs the better part of a second, and this runs before any real work."""
    asked = []
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: asked.append(name) or object())

    config.require("PySide6")

    assert asked == ["PySide6"]


def test_the_gui_command_refuses_before_it_tries_to_open_a_window(monkeypatch):
    """Order matters: a refusal that first fails inside Qt reports the wrong problem."""
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)

    with pytest.raises(SystemExit) as refusal:
        cli.cli(["gui"])

    assert "uv sync --group gui" in str(refusal.value)


# -- the built-in template ships ------------------------------------------------------------------

def test_the_starter_template_is_inside_the_package():
    """It lives next to the code rather than in the repo root, because only the package directory
    goes into the wheel -- a starter template outside it installs as nothing at all."""
    from kasseimail.templates import BUILTIN_DIR

    assert BUILTIN_DIR.is_relative_to(Path(importlib.import_module("kasseimail").__file__).parent)
    assert (BUILTIN_DIR / "basic" / "subject.j2").is_file()


def test_the_wheel_is_told_where_the_package_is():
    """A src layout that hatchling is not told about builds an empty wheel."""
    assert pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == \
        ["src/kasseimail"]
