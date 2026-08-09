# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Waikiki.app (macOS).

Bundles the FastAPI server + pywebview shell. Uses fastembed (ONNX) for
embeddings; PyTorch / sentence-transformers are excluded to keep the app small.
"""
from PyInstaller.utils.hooks import collect_all
import glob
import sqlite_vec

# Stamp the real release into the bundle. The updater compares a downloaded
# bundle's CFBundleShortVersionString against the release tag before installing
# it, so a hardcoded version here would make every genuine update look like a
# mismatch (and Finder's Get Info would lie too).
from waikiki import __version__ as WAIKIKI_VERSION

datas = [
    ("waikiki/templates", "waikiki/templates"),
    ("waikiki/static", "waikiki/static"),
]
binaries = []
hiddenimports = [
    "apsw",
    "webview.platforms.cocoa",
    "objc", "Foundation", "WebKit", "AppKit", "Quartz", "Cocoa", "PyObjCTools",
]

# Packages with native libs / data files that PyInstaller won't fully auto-detect.
for pkg in [
    "fastembed", "onnxruntime", "tokenizers", "huggingface_hub", "sqlite_vec",
    "pycrdt", "fastmcp", "anthropic", "markdown_it", "mdit_py_plugins",
    "linkify_it", "uvicorn", "anyio", "pydantic", "webview",
    "xhtml2pdf", "reportlab",
]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# The sqlite-vec loadable extension must sit next to its package so
# sqlite_vec.loadable_path() resolves inside the bundle. loadable_path() omits
# the platform suffix, so glob for the real file (vec0.dylib / vec0.so).
for _ext in glob.glob(sqlite_vec.loadable_path() + ".*"):
    binaries += [(_ext, "sqlite_vec")]

a = Analysis(
    ["waikiki_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "sentence_transformers", "transformers", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Waikiki",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Waikiki")

app = BUNDLE(
    coll,
    name="Waikiki.app",
    icon="assets/Waikiki.icns",
    bundle_identifier="com.verinfast.waikiki",
    info_plist={
        "NSHighResolutionCapable": True,
        "LSBackgroundOnly": False,
        "CFBundleShortVersionString": WAIKIKI_VERSION,
        "CFBundleVersion": WAIKIKI_VERSION,
    },
)
