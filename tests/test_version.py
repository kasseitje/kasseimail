"""The version, and the build that bakes it in.

The version comes from the git tag through hatch-vcs, so there is no literal anywhere to assert
against. What is pinned instead is that it resolves at all, that a frozen build can still find it,
and that the four-integer form Windows demands does not throw away the wrong half.
"""

import tomllib

from pathlib import Path

import pytest

from kasseimail import version as version_module
from kasseimail.version import __version__, version_tuple

ROOT = Path(__file__).resolve().parent.parent


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# -- where the number comes from -------------------------------------------------------------------

def test_the_version_is_declared_dynamic_and_taken_from_the_tag():
    """A literal in pyproject.toml is a second place the number lives, and the two drift the first
    time somebody tags a release and forgets to edit it."""
    data = pyproject()

    assert "version" in data["project"]["dynamic"]
    assert "version" not in data["project"]
    assert data["tool"]["hatch"]["version"]["source"] == "vcs"


def test_the_scheme_does_not_invent_a_release_that_does_not_exist():
    """setuptools_scm's default guesses the *next* version, so a build one commit past 0.1.0 would
    call itself 0.2.0.dev1 -- a number nobody tagged, in a file somebody may be sent."""
    raw = pyproject()["tool"]["hatch"]["version"]["raw-options"]

    assert raw["version_scheme"] == "no-guess-dev"


def test_a_version_file_is_generated_into_the_package():
    """The only way a frozen executable knows its version: PyInstaller bundles no package
    metadata, so `importlib.metadata` finds nothing at all inside one."""
    hook = pyproject()["tool"]["hatch"]["build"]["hooks"]["vcs"]

    assert hook["version-file"] == "src/kasseimail/_version.py"


def test_the_generated_file_is_not_tracked():
    """It is written at install time from whatever the tag says. Committed, it would be one more
    stale copy of the number, and every build would show it as a local change."""
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "src/kasseimail/_version.py" in ignored


def test_the_version_resolves_to_something():
    assert __version__
    assert __version__ != "0.0.0+unknown", "no version source worked; run `uv sync`"


def test_a_frozen_build_never_asks_git_for_the_version(monkeypatch):
    """A frozen executable has no repository around it, but the directory it was started from may
    well be one -- somebody else's. Asking git there reports another project's version."""
    monkeypatch.setattr(version_module.sys, "frozen", True, raising=False)

    assert version_module._from_git() is None


# -- the four integers Windows wants ---------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("0.1.0", (0, 1, 0, 0)),
    ("1.2.3.4", (1, 2, 3, 4)),
    ("2.0", (2, 0, 0, 0)),
    # -- everything after the first non-numeric piece is dropped, because Windows takes integers
    #    and nothing else. The full string still goes in the exe's text fields.
    ("0.1.0.post1.dev0+g78eb3790b.d20260915", (0, 1, 0, 0)),
    ("0.1.0+dirty", (0, 1, 0, 0)),
    ("0.0.0+unknown", (0, 0, 0, 0)),
])
def test_a_version_becomes_four_integers(text, expected):
    assert version_tuple(text) == expected


def test_a_version_with_no_numbers_at_all_is_still_four_integers():
    """`git describe` on a repo with no tags gives a bare commit hash. A build from one must not
    fail on the version resource, it must just say zero."""
    assert version_tuple("g78eb3790b") == (0, 0, 0, 0)
    assert version_tuple("untagged") == (0, 0, 0, 0)


def test_the_four_integers_are_always_integers():
    """The Windows resource compiler takes a tuple of ints. A string in there fails the build with
    an error naming neither the field nor the version."""
    for number in version_tuple("0.1.0.post1.dev0"):
        assert isinstance(number, int)


# -- the build ---------------------------------------------------------------------------------------

def test_the_spec_and_the_build_script_are_both_there():
    assert (ROOT / "kasseimail.spec").is_file()
    assert (ROOT / "scripts" / "build_exe.py").is_file()
    assert (ROOT / "pyinstaller_entry.py").is_file()


def test_pyinstaller_is_a_development_dependency_and_not_a_runtime_one():
    """Nobody installing kasseimail to send mail needs the thing that freezes it."""
    data = pyproject()

    assert "pyinstaller" not in " ".join(data["project"]["dependencies"]).lower()
    assert "pyinstaller" in " ".join(data["dependency-groups"]["dev"]).lower()


def test_the_frozen_entry_point_chooses_by_the_name_it_was_started_under():
    """One analysis builds both programs, so the only thing telling them apart is argv[0]. If this
    stopped matching the names in the spec, kasseimail-gui.exe would open a command line."""
    # -- by path: it sits at the repo root, which is deliberately not on sys.path. Nothing in the
    #    package imports it and nothing should -- PyInstaller is its only caller.
    import importlib.util

    location = importlib.util.spec_from_file_location(
        "pyinstaller_entry", ROOT / "pyinstaller_entry.py")
    entry = importlib.util.module_from_spec(location)
    location.loader.exec_module(entry)

    spec = (ROOT / "kasseimail.spec").read_text(encoding="utf-8")

    assert 'name="kasseimail-gui"' in spec
    assert 'name="kasseimail"' in spec
    assert any("kasseimail-gui".endswith(suffix) for suffix in entry.GUI_SUFFIXES)
    assert not any("kasseimail".endswith(suffix) for suffix in entry.GUI_SUFFIXES)


def test_the_starter_template_is_collected_into_the_bundle():
    """It is data, so nothing in the import graph points at it and PyInstaller would leave it
    behind -- and a frozen build would then open on an empty template list."""
    spec = (ROOT / "kasseimail.spec").read_text(encoding="utf-8")

    assert "collect_data_files" in spec
    assert "templates_builtin" in spec
