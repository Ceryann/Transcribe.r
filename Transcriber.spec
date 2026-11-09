# -*- mode: python ; coding: utf-8 -*-
# Build: pyinstaller --noconfirm Transcriber.spec
import importlib.util
import os
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []


def grab(pkg):
    """Kemas seluruh paket (kode + data + DLL). Paket yang tidak terpasang dilewati."""
    if importlib.util.find_spec(pkg) is None:
        print(f"[spec] dilewati (tidak terpasang): {pkg}")
        return
    d, b, h = collect_all(pkg)
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)
    print(f"[spec] dikemas: {pkg}")


for pkg in (
    "customtkinter",      # tema & font
    "tkinterdnd2",        # pustaka native tkdnd untuk seret & lepas
    "whisper",            # mel_filters.npz, file tokenizer
    "tiktoken", "tiktoken_ext",
    "numba", "llvmlite",  # dipakai Whisper untuk timestamp per kata (mode Lagu)
    "demucs",             # daftar model (yaml) + semua kelas model
    "torchaudio",
    "yt_dlp", "yt_dlp_ejs",
    "deep_translator",
    "argostranslate", "ctranslate2", "sentencepiece", "stanza", "minisbd",
):
    grab(pkg)

# Sumber daya yang disiapkan workflow sebelum build (hanya dikemas jika foldernya ada)
for src, dst in (("app_icon.ico", "."), ("ffmpeg", "ffmpeg"),
                 ("models", "models"), ("torch_home", "torch_home")):
    if os.path.exists(src):
        datas.append((src, dst))
        print(f"[spec] sumber daya: {src}")

a = Analysis(
    ["app_gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "IPython", "jupyter", "notebook", "pytest",
              "PyQt5", "PyQt6", "PySide2", "PySide6", "sphinx"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Transcriber",
    debug=False,
    strip=False,
    upx=False,
    console=False,                 # tanpa jendela hitam
    icon="app_icon.ico" if os.path.exists("app_icon.ico") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Transcriber",
)
