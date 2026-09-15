# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build. Works on any platform; a Windows .exe needs Windows.

    uv run pyinstaller kasseimail.spec          # or: uv run python scripts/build_exe.py

**One analysis, two programs.** `pyinstaller_entry.py` looks at the name it was started under, so
`kasseimail.exe` is the command line and `kasseimail-gui.exe` opens the window -- both out of one
`COLLECT`, sharing a single copy of Qt. Building them separately would ship that ~150 MB twice.

**onedir, not onefile.** A onefile build of a Qt application unpacks the whole bundle into a temp
directory on every launch, which costs seconds each time and leaves copies behind when it is killed.
The build script zips the directory instead, which is what you hand somebody.
"""

import sys

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

# -- SPECPATH is where the .spec sits; `__file__` is not defined while a spec is being evaluated.
ROOT = Path(SPECPATH).resolve()  # noqa: F821

sys.path.insert(0, str(ROOT / "src"))
from kasseimail.version import __version__, version_tuple  # noqa: E402

ICON = ROOT / "static" / "kasseimail.ico"

# -- the starter template is data, not code, so nothing in the import graph points at it and
#    PyInstaller would leave it behind. `templates.BUILTIN_DIR` resolves relative to the package,
#    and collect_data_files puts it exactly there.
DATAS = collect_data_files("kasseimail", includes=["templates_builtin/**"])

# -- Qt ships far more than this uses, and every module of it is tens of megabytes. Excluded by
#    name rather than trimmed afterwards, so a build that starts needing one fails loudly here.
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtBluetooth",
    "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtNfc",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    # -- pulled in by nothing here, and large.
    "tkinter", "matplotlib", "numpy", "pandas", "scipy", "IPython", "pytest",
]

analysis = Analysis(  # noqa: F821
    [str(ROOT / "pyinstaller_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=DATAS,
    # -- imported inside functions, or not at all until Qt asks. PyInstaller does follow a
    #    function-level import, but naming them costs nothing and a missing one only shows up as a
    #    traceback on somebody else's machine.
    hiddenimports=[
        "kasseimail.ui.app", "kasseimail.ui.main_window", "kasseimail.ui.worker",
        "msal", "requests", "openpyxl", "platformdirs",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)  # noqa: F821


def _windows_version_resource():
    """The version in the exe's Properties → Details, on Windows only.

    Windows insists on four plain integers, so `0.1.0.post1.dev2+g78eb379` has to be cut down to
    (0, 1, 0, 0) for the numeric fields. The full string goes in the text fields next to it, which
    is where anybody chasing a bug report will actually read it.
    """
    if sys.platform != "win32":
        return None

    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct,
        VSVersionInfo,
    )

    numbers = version_tuple()
    strings = StringTable("040904B0", [
        StringStruct("CompanyName", "Bino Maiheu"),
        StringStruct("FileDescription", "kasseimail - bulk mail over the Microsoft Graph API"),
        StringStruct("FileVersion", __version__),
        StringStruct("InternalName", "kasseimail"),
        StringStruct("OriginalFilename", "kasseimail.exe"),
        StringStruct("ProductName", "kasseimail"),
        StringStruct("ProductVersion", __version__),
    ])

    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3F, flags=0x0,
                          OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[StringFileInfo([strings]), VarFileInfo([VarStruct("Translation", [1033, 1200])])],
    )


VERSION_RESOURCE = _windows_version_resource()
ICON_ARGUMENT = str(ICON) if ICON.is_file() else None

common = dict(
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # -- UPX is off. It roughly halves the download and is a well-known way to have a fresh build
    #    quarantined by Windows Defender, which costs far more than the megabytes save.
    upx=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_ARGUMENT,
    version=VERSION_RESOURCE,
)

# -- the command line: a console program, because its whole output is text and a windowed build on
#    Windows has no stdout to write it to.
cli = EXE(pyz, analysis.scripts, [], name="kasseimail", console=True, **common)  # noqa: F821

# -- the window: no console, so starting it does not flash a black box that then sits there.
gui = EXE(pyz, analysis.scripts, [], name="kasseimail-gui", console=False, **common)  # noqa: F821

COLLECT(  # noqa: F821
    cli, gui,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="kasseimail",
)
