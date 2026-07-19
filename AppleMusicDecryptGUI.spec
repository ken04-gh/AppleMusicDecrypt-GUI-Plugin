# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


root = Path(SPECPATH).resolve()

a = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "assets" / "Touch_ID_Logo.ico"), "assets"),
        (str(root / "assets" / "Touch_ID_Logo.png"), "assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)

# Keep GUI and third-party runtime dependencies in the executable, but force
# every AppleMusicDecrypt core module to be imported from the external project.
a.pure = [item for item in a.pure if item[0] != "src" and not item[0].startswith("src.")]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AppleMusicDecryptGUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(root / "assets" / "Touch_ID_Logo.ico")],
    version=str(root / "version_info.txt"),
)
