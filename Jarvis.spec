# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files


datas = collect_data_files("openwakeword")
datas += [("omar_ai_core/persona/system_prompt.txt", "omar_ai_core/persona")]
datas += [("assets/jarvis_hud.qml", "assets")]
datas += [
    ("assets/shaders/ultron_core.vert", "assets/shaders"),
    ("assets/shaders/ultron_core.frag", "assets/shaders"),
]

a = Analysis(
    ["desktop_main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "onnxruntime", "sounddevice", "cffi",
        "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtQuickWidgets",
        "PyQt6.QtOpenGL", "PyQt6.QtOpenGLWidgets", "PyQt6.QtMultimedia",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["cv2", "torch", "tensorflow", "sklearn", "scipy", "google.generativeai"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Jarvis",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="assets/jarvis.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Jarvis",
)
