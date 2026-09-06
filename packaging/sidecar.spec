# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the VoiceClone desktop sidecar (onedir).

Build:
    .\.venv\Scripts\python.exe -m PyInstaller packaging\sidecar.spec --noconfirm

Output:
    dist\sidecar\sidecar.exe  (+ _internal\ with app code, static/, models/)

Design notes:
    - console=True: the sidecar prints `READY port=... token=...` on stdout,
      which the Tauri shell parses to discover the endpoint.
    - onedir (not onefile): models (~600MB) must NOT be extracted to a temp
      dir on every launch; onedir keeps them on disk next to the exe.
    - ROOT_DIR in app/config.py resolves to <exe_dir>/_internal when frozen
      (Path(__file__).resolve().parent.parent), so the datas below land
      exactly where the app expects them: _internal/static, _internal/models,
      and runtime data is created at _internal/data. Zero core code changes.
    - Lazy imports in app code (faster_whisper, pyaudiowpatch, sherpa_onnx,
      soundfile, uvicorn auto modules) are invisible to static analysis and
      must be listed explicitly below.
"""

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# Lazy / dynamic imports that PyInstaller's static analysis cannot see.
hiddenimports = [
    # app/asr.py: from faster_whisper import WhisperModel (lazy)
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "av",
    # app/capture.py: import pyaudiowpatch as pyaudio (lazy)
    "pyaudiowpatch",
    "pyaudio",
    # app/providers/local.py: import sherpa_onnx (lazy)
    "sherpa_onnx",
    # app/providers/local.py + volcengine.py: import soundfile (lazy)
    "soundfile",
    # python-multipart (FastAPI form parsing)
    "multipart",
    # uvicorn auto-detected loop/protocol/lifespan modules
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

# Native packages with lazy imports: pull in every submodule + DLL.
_native_pkgs = [
    "sherpa_onnx",
    "ctranslate2",
    "onnxruntime",
    "soundfile",
    "pyaudiowpatch",
    "pyaudio",
    "av",
    "faster_whisper",
]
for _pkg in _native_pkgs:
    hiddenimports += collect_submodules(_pkg)

binaries = []
for _pkg in _native_pkgs:
    binaries += collect_dynamic_libs(_pkg)

datas = [
    ("../static", "static"),
    ("../models", "models"),
]

a = Analysis(
    ["../sidecar.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PIL", "pytest", "IPython", "jupyter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    upx=False,
    upx_exclude=[],
    name="sidecar",
)