# PyInstaller spec: single-folder app dist/CoreKeeperMapViewer/CoreKeeperMapViewer.exe
# Build: build.bat   (or: py -m PyInstaller --noconfirm CoreKeeperMapViewer.spec)
a = Analysis(
    ["app.py"],
    pathex=[],
    datas=[
        ("ckmapviewer/web", "ckmapviewer/web"),
        ("ckmapviewer/mods", "ckmapviewer/mods"),
    ],
    hiddenimports=[],
    excludes=["tkinter", "unittest", "pydoc", "test", "lib2to3"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CoreKeeperMapViewer",
    console=True,          # the console window shows the log; closing it stops the viewer
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="CoreKeeperMapViewer")
