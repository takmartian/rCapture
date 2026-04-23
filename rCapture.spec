# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

block_cipher = None

import shutil

rapidocr_datas = collect_data_files('rapidocr_onnxruntime', includes=['**/*'])

_ffmpeg = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=collect_dynamic_libs('onnxruntime') + [(_ffmpeg, '.')],
    datas=rapidocr_datas,
    hiddenimports=[
        'PySide6.QtNetwork',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'pynput.keyboard._darwin',
        'pynput.mouse._darwin',
        'pynput.keyboard._base',
        'pynput.mouse._base',
        'AppKit',
        'objc',
        'Foundation',
        'Quartz',
        'mss',
        'mss.darwin',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFilter',
        'onnxruntime',
        'onnxruntime.capi',
        'onnxruntime.capi._pybind_state',
    ] + collect_submodules('rapidocr_onnxruntime'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='rCapture',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='rCapture',
)

app = BUNDLE(
    coll,
    name='rCapture.app',
    icon=None,
    bundle_identifier='com.rexng.rcapture',
    info_plist={
        'NSPrincipalClass': 'NSApplication',
        'NSAppleScriptEnabled': False,
        'NSScreenCaptureUsageDescription': 'rCapture 需要屏幕录制权限以进行截图和录屏。',
        'NSMicrophoneUsageDescription': 'rCapture 需要麦克风权限以录制音频。',
        'NSAppleEventsUsageDescription': 'rCapture 需要此权限以控制其他应用程序。',
        'NSInputMonitoringUsageDescription': 'rCapture 需要输入监控权限以响应全局快捷键。',
        'LSUIElement': True,
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1',
        'CFBundleDisplayName': 'rCapture',
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'NSSupportsAutomaticGraphicsSwitching': True,
    },
)
