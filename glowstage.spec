# PyInstaller build of Glowstage: a folder app (dist\Glowstage\Glowstage.exe). Run tools\build.ps1.
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["Glowstage.pyw"],
    pathex=["."],
    datas=[
        ("glowstage/ui", "glowstage/ui"),
        ("tools/yamnet", "tools/yamnet"),                     # the AI genre model and its labels
        ("tools/install-shortcut.ps1", "tools"),               # adds the app to the Start menu
        ("LICENSE", "."),
    ],
    hiddenimports=collect_submodules("glowstage") + ["pystray._win32"],
    excludes=["tkinter", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Glowstage",
    icon="build/icon.ico",
    console=False,
    version="build/version.txt",
)
coll = COLLECT(exe, a.binaries, a.datas, name="Glowstage")
