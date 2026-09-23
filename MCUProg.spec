# -*- mode: python ; coding: utf-8 -*-
import os
import shutil


HERE = os.path.abspath(os.getcwd())

a = Analysis(
    ['MCUProg.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['FlashAlgo.MT7687_32M_MXIC'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MCUProg',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MCUProg',
)


''' 数据文件铺到 exe 旁边，而不是 PyInstaller 的 _internal 里。

    不是为了好看：devices.txt 和 setting.ini 是程序运行时要写回去的，
    识别或搜索到新芯片会往 devices.txt 追加词条，下载来的 .FLM 会落进
    FlashAlgo/，这些都得是用户看得见、改得动的地方。datas 只会进
    contents 目录，所以构建完再铺一遍。 '''

OUT = os.path.join(DISTPATH, 'MCUProg')

FILES   = ['MCUProg.ui', 'devices.txt', 'setting.ini']
FOLDERS = ['FlashAlgo', 'libusb-1.0.24']

for name in FILES:
    source = os.path.join(HERE, name)

    if os.path.exists(source):
        shutil.copy2(source, OUT)
        print(f'  随 exe 一起发布：{name}')

for name in FOLDERS:
    source = os.path.join(HERE, name)

    if not os.path.isdir(source):
        continue

    shutil.copytree(source, os.path.join(OUT, name), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.packcache'))

    print(f'  随 exe 一起发布：{name}/')
