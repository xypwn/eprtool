# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_data_files

from misc.installer_utils import *

args = parse_args()

datas = [('src/common/res', 'common/res')]
binaries = []
hiddenimports = []

datas += collect_data_files('imgui_bundle')

isort_datas, isort_binaries, isort_hiddenimports = collect_all("isort")
datas += isort_datas
binaries += isort_binaries
hiddenimports += isort_hiddenimports

binaries += collect_isort_mypyc_bins()

a = Analysis(
    ['src/eprcheck/__init__.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

a.datas = exclude_datas(a.datas, DATAS_SIMPLEMMA_NON_EN_DE)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='eprcheck',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=not args.guionly,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["src/common/res/app_settings/icon.ico"],
)
