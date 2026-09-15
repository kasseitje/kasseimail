"""Build the standalone executable, with the version taken from the git tag.

    uv run python scripts/build_exe.py

**A Windows .exe has to be built on Windows.** PyInstaller freezes the interpreter and libraries of
the machine it runs on -- there is no cross-compilation, and no flag that changes that. Run this on
Linux and you get a working Linux build of the same thing, which is useful for checking the spec but
is not what you hand to somebody on Windows.

What it does, in order:

1. refuses if the working tree is dirty, unless you say otherwise -- an executable built from
   uncommitted changes has a version that names a commit not containing them;
2. re-stamps the version from `git describe`, because `_version.py` is written at install time and
   goes stale the moment you tag;
3. clears `build/` and `dist/`, since PyInstaller reuses both and a stale one is how a fixed bug
   comes back;
4. runs PyInstaller over `kasseimail.spec`;
5. zips the result into `dist/`, named for the version and the platform.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "kasseimail.spec"
BUILD, DIST = ROOT / "build", ROOT / "dist"
BUNDLE = DIST / "kasseimail"


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"    $ {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def git(*arguments: str) -> str:
    try:
        return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def describe() -> str:
    return git("describe", "--tags", "--dirty", "--always") or "untagged"


def stamp_version() -> str:
    """Rewrite `_version.py` from the current tag, and report what it says.

    `hatch-vcs` writes that file when the package is installed, and `uv` will not reinstall just
    because a tag appeared -- so the version baked into a build is whatever was true at the last
    `uv sync`. Without this step, tagging a release and building it produces an executable that
    still calls itself the previous version.
    """
    print("==> stamping the version from git")
    run([*uv(), "sync", "--group", "dev", "--group", "gui", "--reinstall-package", "kasseimail"],
        stdout=subprocess.DEVNULL)

    sys.path.insert(0, str(ROOT / "src"))
    for module in ("kasseimail.version", "kasseimail._version", "kasseimail"):
        sys.modules.pop(module, None)

    from kasseimail.version import __version__

    print(f"    version: {__version__}   (git describe: {describe()})")
    return __version__


def uv() -> list[str]:
    found = shutil.which("uv")
    if found:
        return [found]
    # -- already inside the environment, e.g. `python scripts/build_exe.py` after activating it.
    return [sys.executable, "-m", "uv"]


def clean() -> None:
    print("==> clearing build/ and dist/")
    for directory in (BUILD, DIST):
        if directory.exists():
            shutil.rmtree(directory)


def build() -> None:
    print("==> running PyInstaller")
    run([*uv(), "run", "pyinstaller", "--noconfirm", "--clean", str(SPEC)])


def package(version: str) -> Path:
    """Zip the bundle, named for the version and the platform it will run on."""
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(
        platform.system(), platform.system().lower())
    machine = platform.machine().lower().replace("amd64", "x86_64")

    safe = version.replace("+", "-")
    stem = DIST / f"kasseimail-{safe}-{system}-{machine}"

    print(f"==> zipping {stem.name}.zip")
    # -- `make_archive` returns the name it actually used, and that is the only safe way to learn
    #    it: a version like 0.1.0.post1.dev0 is full of dots, so `with_suffix` would eat the last
    #    piece of it rather than append.
    return Path(shutil.make_archive(str(stem), "zip", root_dir=DIST, base_dir=BUNDLE.name))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_exe",
        description="Build the standalone kasseimail executable.",
        epilog="A Windows .exe must be built on Windows: PyInstaller freezes the interpreter of "
               "the machine it runs on and cannot cross-compile.",
    )
    parser.add_argument("--dirty", action="store_true",
                        help="build even though the working tree has uncommitted changes")
    parser.add_argument("--no-zip", action="store_true", help="leave the bundle unpacked")
    parser.add_argument("--no-stamp", action="store_true",
                        help="skip the reinstall that refreshes the version (faster; may be stale)")
    args = parser.parse_args(argv)

    if not SPEC.is_file():
        print(f"error: {SPEC} is missing", file=sys.stderr)
        return 1

    if platform.system() != "Windows":
        print(f"note: this is {platform.system()}, so the result runs on {platform.system()}, "
              "not on Windows.\n      A Windows .exe has to be built on Windows.\n")

    dirty = git("status", "--porcelain")
    if dirty and not args.dirty:
        print("error: the working tree has uncommitted changes, so the version would name a "
              "commit\n       that does not contain them. Commit them, or pass --dirty.\n",
              file=sys.stderr)
        print(dirty, file=sys.stderr)
        return 1

    version = describe() if args.no_stamp else stamp_version()

    clean()
    build()

    if not BUNDLE.is_dir():
        print(f"error: PyInstaller produced no {BUNDLE}", file=sys.stderr)
        return 1

    programs = sorted(
        entry.name for entry in BUNDLE.iterdir()
        if entry.is_file() and (os.access(entry, os.X_OK) or entry.suffix == ".exe")
    )

    print()
    print(f"==> built {BUNDLE}")
    for name in programs:
        print(f"    {name}")
    print(f"    {_size(BUNDLE) / (1024 * 1024):.0f} MB")

    if not args.no_zip:
        archive = package(version)
        print(f"    {archive}  ({archive.stat().st_size / (1024 * 1024):.0f} MB)")

    return 0


def _size(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


if __name__ == "__main__":
    sys.exit(main())
