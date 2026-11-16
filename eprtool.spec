# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_all

import os
import glob
import re
import argparse

argp = argparse.ArgumentParser()
argp.add_argument("--guionly", action="store_true")
args = argp.parse_args()

def path_re_norm(s: str) -> str:
    return s.replace("/", "\\\\") if os.name == "nt" else s

exclude_datas = [re.compile(path_re_norm(p)) for p in [
    # Filter out all simplemma language files that aren't German or English (saves ~20MiB)
    r".*/simplemma/strategies/dictionaries/data/(?!(en|de)\.)\w+\.plzma$",
    # We don't use the default imgui_bundle resources
    r".*/imgui_bundle/(assets|demos_assets|demos_cpp|demos_python)/.*$",
]]

datas = [('src/eprtool/res', 'eprtool/res')]
binaries = []
hiddenimports = ["eprtool"]

datas += collect_data_files('imgui_bundle')

isort_datas, isort_binaries, isort_hiddenimports = collect_all("isort")
datas += isort_datas
binaries += isort_binaries
hiddenimports += isort_hiddenimports

def collect_isort_mypyc_bins() -> list[tuple[str, str]]:
    res = []
    import isort
    pkg_dir = os.path.dirname(os.path.dirname(isort.__file__))
    for b in glob.glob(os.path.join(pkg_dir, "*__mypyc*")):
        if b.endswith((".pyd", ".so")):
            res.append((b, "."))
    return res
binaries += collect_isort_mypyc_bins()

a = Analysis(
    ['src/eprtool/__init__.py'],
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

a.datas = [(dst, src, typ) for dst, src, typ in a.datas
    if not any(p.match(src) is not None for p in exclude_datas)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='eprtool',
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
    icon=["src/eprtool/res/app_settings/icon.ico"],
)
