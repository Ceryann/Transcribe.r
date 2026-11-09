"""
Transcriber
-----------
Dependensi:
    pip install -U openai-whisper customtkinter pillow
    pip install demucs          # opsional, SANGAT disarankan untuk transkrip lagu
    pip install -U yt-dlp       # opsional, untuk transkrip dari link video
    pip install deep-translator # opsional, untuk terjemahan & ekspor transkrip
    pip install tkinterdnd2     # opsional, untuk seret & lepas (drag and drop) file/link
    pip install argostranslate  # opsional, untuk terjemahan OFFLINE (paket bahasa diunduh sekali)
"""

import os
import re
import gc
import sys
import json
import time
import shutil
import queue
import threading
import subprocess
import importlib.util
import ctypes
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from tkinter import filedialog, messagebox

# ----------------------------------------------------------------------------
# Bootstrap untuk versi .exe (PyInstaller, tanpa jendela konsol)
# ----------------------------------------------------------------------------
FROZEN = getattr(sys, "frozen", False)

# Tanpa konsol, sys.stdout/sys.stderr bernilai None -> tqdm, yt-dlp, Demucs bisa error saat menulis.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

# Cegah jendela hitam (konsol) berkedip setiap kali ffmpeg dipanggil oleh Whisper/Demucs/yt-dlp.
if os.name == "nt":
    _OrigPopen = subprocess.Popen

    class _NoWindowPopen(_OrigPopen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen

import customtkinter as ctk

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES, DND_TEXT
    _DnDBase = TkinterDnD.DnDWrapper
    HAS_DND_LIB = True
except Exception:
    TkinterDnD = None
    DND_FILES = DND_TEXT = None
    _DnDBase = object
    HAS_DND_LIB = False
import torch
import whisper
import whisper.tokenizer
from whisper.utils import get_writer
from PIL import Image, ImageDraw

# ----------------------------------------------------------------------------
# Pendaftaran ID Aplikasi ke Windows (Agar Ikon Taskbar Berubah)
# ----------------------------------------------------------------------------
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("transcriber.gui.v1")
except Exception:
    pass

# ----------------------------------------------------------------------------
# Konfigurasi Jalur & File
# ----------------------------------------------------------------------------
def _is_writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except Exception:
        return False


if FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))   # folder tempat Transcriber.exe berada
    RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)                  # sumber daya bawaan (ffmpeg, model, ikon)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))         # saat dijalankan sebagai skrip: folder skrip
    RES_DIR = APP_DIR

# Data pengguna (hasil, settings, model unduhan) ditulis di sebelah aplikasi; jika folder itu tidak bisa
# ditulisi (mis. di Program Files), pindah otomatis ke %LOCALAPPDATA%\Transcriber.
DATA_DIR = APP_DIR if _is_writable(APP_DIR) else os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Transcriber")

ffmpeg_path = os.path.join(RES_DIR, "ffmpeg")
os.environ["PATH"] = ffmpeg_path + os.pathsep + os.environ.get("PATH", "")

# Bobot Demucs yang sudah dikemas (jika ada) dipakai langsung, tanpa unduhan.
_BUNDLED_TORCH_HOME = os.path.join(RES_DIR, "torch_home")
if os.path.isdir(_BUNDLED_TORCH_HOME):
    os.environ.setdefault("TORCH_HOME", _BUNDLED_TORCH_HOME)

OUTPUT_DIR = os.path.join(DATA_DIR, "Hasil Transkrip")
MODEL_DIR = os.path.join(DATA_DIR, "models")
BUNDLED_MODEL_DIR = os.path.join(RES_DIR, "models")             # model Whisper yang sudah dikemas
TMP_DIR = os.path.join(OUTPUT_DIR, "_tmp_demucs")
DL_DIR = os.path.join(OUTPUT_DIR, "_tmp_download")
ICON_PATH = os.path.join(RES_DIR, "app_icon.ico")
if FROZEN and not os.path.exists(ICON_PATH):
    ICON_PATH = os.path.join(DATA_DIR, "app_icon.ico")
MODEL_NAME = "small"

# ----------------------------------------------------------------------------
# Pembuat Ikon Otomatis (Logo Minimalis Kotak Biru)
# ----------------------------------------------------------------------------
def ensure_icon_exists():
    if not os.path.exists(ICON_PATH):
        try:
            size = 256
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # Kotak luar biru terang (#0091FF)
            pad = 12
            draw.rounded_rectangle([pad, pad, size - pad, size - pad], radius=52, fill=(0, 145, 255, 255))

            # Kotak dalam warna gelap (#14141C)
            margin = 48
            draw.rounded_rectangle(
                [pad + margin, pad + margin, size - pad - margin, size - pad - margin], 
                radius=28, 
                fill=(20, 20, 28, 255)
            )

            img.save(
                ICON_PATH,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
            )
        except Exception:
            pass
    return ICON_PATH

# ----------------------------------------------------------------------------
# Pembuat Shortcut Desktop Otomatis ("Transcriber")
# ----------------------------------------------------------------------------
def create_desktop_shortcut():
    if os.name != "nt":
        return
    try:
        desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
        shortcut_path = os.path.join(desktop, "Transcriber.lnk")
        old_shortcut = os.path.join(desktop, "Mp3 Transcriber.lnk")

        # Hapus shortcut nama lama jika ada
        if os.path.exists(old_shortcut):
            try:
                os.remove(old_shortcut)
            except Exception:
                pass

        if os.path.exists(shortcut_path):
            try:
                os.remove(shortcut_path)
            except Exception:
                pass

        if FROZEN:
            target, arguments = sys.executable, ""
        else:
            target = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(target):
                target = "pythonw.exe"
            arguments = f'"{os.path.abspath(__file__)}"'
        arguments_vbs = arguments.replace('"', '""')

        vbs = (
            'Set WshShell = CreateObject("WScript.Shell")\n'
            f'Set shortcut = WshShell.CreateShortcut("{shortcut_path}")\n'
            f'shortcut.TargetPath = "{target}"\n'
            f'shortcut.Arguments = "{arguments_vbs}"\n'
            f'shortcut.WorkingDirectory = "{APP_DIR}"\n'
            f'shortcut.IconLocation = "{ICON_PATH},0"\n'
            'shortcut.Description = "Transcriber"\n'
            'shortcut.Save\n'
        )
        vbs_path = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "make_shortcut.vbs")
        with open(vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs)

        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.run(["cscript", "//Nologo", vbs_path], capture_output=True, creationflags=flags)
        if os.path.exists(vbs_path):
            os.remove(vbs_path)
    except Exception:
        pass

# ----------------------------------------------------------------------------
# Bahasa
# ----------------------------------------------------------------------------
PRIORITY = ["id", "en", "jw", "su", "ms", "ja", "ko", "zh", "ar"]
LANGUAGE_MAP = {"Otomatis (Auto-detect)": None}
for _code in PRIORITY:
    LANGUAGE_MAP[whisper.tokenizer.LANGUAGES[_code].title()] = _code
for _code, _name in sorted(whisper.tokenizer.LANGUAGES.items(), key=lambda x: x[1]):
    if _code not in PRIORITY:
        LANGUAGE_MAP[_name.title()] = _code

# Bahasa tujuan terjemahan (kode Google Translate)
TRANSLATE_LANGS = {
    "Inggris": "en",
    "Indonesia": "id",
    "Melayu": "ms",
    "Jepang": "ja",
    "Korea": "ko",
    "Mandarin (Sederhana)": "zh-CN",
    "Arab": "ar",
    "Spanyol": "es",
    "Prancis": "fr",
    "Jerman": "de",
    "Portugis": "pt",
    "Rusia": "ru",
    "Hindi": "hi",
    "Thailand": "th",
    "Vietnam": "vi",
    "Belanda": "nl",
    "Turki": "tr",
    "Italia": "it",
}

# ----------------------------------------------------------------------------
# Preset Parameter Transkripsi
# ----------------------------------------------------------------------------
MODES = {
    "🎤 Lagu": {
        "hint": "Dioptimalkan untuk lirik: tahan halusinasi di bagian instrumental, "
                "toleran terhadap lirik berulang (chorus), timestamp per kata.",
        "params": dict(
            condition_on_previous_text=False,
            no_speech_threshold=0.8,
            compression_ratio_threshold=2.8,
            logprob_threshold=-1.0,
            beam_size=5,
            best_of=5,
            word_timestamps=True,
            hallucination_silence_threshold=2.0,
        ),
    },
    "💬 Percakapan": {
        "hint": "Dioptimalkan untuk pidato, wawancara, rapat, dan podcast.",
        "params": dict(
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            beam_size=5,
            best_of=5,
        ),
    },
}

# ----------------------------------------------------------------------------
# Tema & Warna UI
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# Tema warna: Tema Gelap (palet Warm Dark) dan Tema Terang (keluarga Modern Blue versi terang).
# Warna dasar tertulis di sini; warna turunan (panel dalam, hover, label status, dll.) dihitung otomatis
# dan dijaga kontrasnya.
# ----------------------------------------------------------------------------
THEMES = {
    "gelap": dict(label="🌙 Tema Gelap", mode="dark", bg="#0F0E0E", card="#191717", text="#F3F1EE",
                  muted="#C9C2BA", primary="#F97316", accent="#EAB308", border="#2A2626",
                  success="#22C55E", warn="#EAB308"),
    # Modern Blue yang aslinya gelap, dibalik menjadi terang: latar biru sangat muda, kartu putih,
    # teks memakai navy bawaan Modern Blue (#0B1220). Biru utama diturunkan satu tingkat (#3B82F6 ->
    # #2563EB) supaya teks putih di atas tombol terbaca (kontras 5,2 dibanding 3,7).
    "terang": dict(label="☀️ Tema Terang", mode="light", bg="#EEF3FB", card="#FFFFFF", text="#0B1220",
                   muted="#51607A", primary="#2563EB", accent="#22C55E", border="#D3DEEE",
                   success="#22C55E", warn="#B45309"),
}
THEME_LABELS = {t["label"]: k for k, t in THEMES.items()}
DEFAULT_THEME = "gelap"


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(t):
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(v))) for v in t)


def _mix(a, b, t):
    A, B = _rgb(a), _rgb(b)
    return _hex(tuple(A[i] * (1 - t) + B[i] * t for i in range(3)))


def _lum(h):
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = [v / 255 for v in _rgb(h)]
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _readable(fg, bgs, target=4.5):
    """Pertahankan warna jika sudah cukup kontras di semua latar; jika tidak, geser ke hitam/putih."""
    if all(_contrast(fg, b) >= target for b in bgs):
        return fg
    toward = "#000000" if sum(_lum(b) for b in bgs) / len(bgs) > 0.4 else "#FFFFFF"
    for step in range(1, 21):
        c = _mix(fg, toward, step / 20)
        if all(_contrast(c, b) >= target for b in bgs):
            return c
    return toward


def _fill_for(text, fill, target=4.5):
    """Geser isi (fill) menjauhi warna teks sampai teks terbaca di atasnya."""
    if _contrast(fill, text) >= target:
        return fill
    away = "#000000" if _lum(text) > 0.4 else "#FFFFFF"
    for step in range(1, 21):
        c = _mix(fill, away, step / 20)
        if _contrast(c, text) >= target:
            return c
    return away


def _on(fills):
    """Teks putih atau gelap, mana yang paling terbaca di atas semua isi tersebut."""
    return max(("#FFFFFF", "#10131A"), key=lambda c: min(_contrast(c, f) for f in fills))


def _hover(fill, on):
    """Hover: gelap bila teksnya putih, terang bila teksnya gelap (kontras teks tetap terjaga)."""
    toward = "#000000" if on == "#FFFFFF" else "#FFFFFF"
    for amount in (0.16, 0.08, 0.04):
        c = _mix(fill, toward, amount)
        if _contrast(c, on) >= 4.5:
            return c
    return fill


def build_palette(t):
    dark = t["mode"] == "dark"
    bg, card, text, muted = t["bg"], t["card"], t["text"], t["muted"]
    primary, accent, border = t["primary"], t["accent"], t["border"]
    success, warn = t["success"], t["warn"]
    danger = "#EF4444" if dark else "#DC2626"
    card2 = _mix(card, border, 0.30)     # panel di dalam kartu
    inp = _mix(card, border, 0.50)       # kolom input, jalur, dll.

    on_accent, on_convert, on_danger = _on([primary]), _on([accent]), _on([danger])
    seg_sel = _fill_for(text, primary)
    stop_bg = _mix(card, danger, 0.18)
    badge_cpu = _mix(inp, muted, 0.55)
    badge_cpu = _fill_for(_on([badge_cpu]), badge_cpu)   # geser isi agar teksnya cukup kontras
    chip_bg = {"wait": _mix(card2, text, 0.10), "run": _mix(card2, primary, 0.22),
               "done": _mix(card2, success, 0.22), "fail": _mix(card2, danger, 0.22)}
    chip_fg = {"wait": muted, "run": primary, "done": success, "fail": danger}
    chip_txt = {"wait": "Menunggu", "run": "Memproses…", "done": "Selesai ✓", "fail": "Gagal ✕"}

    return dict(
        BG=bg, CARD=card, CARD2=card2, INPUT=inp, BORDER=border, TEXT=text, MUTED=muted,
        ACCENT=primary, ACCENT_HOVER=_hover(primary, on_accent), ON_ACCENT=on_accent,
        CONVERT_BTN=accent, CONVERT_HOVER=_hover(accent, on_convert), ON_CONVERT=on_convert,
        DANGER=danger, DANGER_HOVER=_hover(danger, on_danger), ON_DANGER=on_danger,
        OK=_readable(success, [bg, card]), WARN=_readable(warn, [bg, card]),
        PLACEHOLDER=_readable(muted, [inp, card2]),   # teks petunjuk di dalam kolom isian
        TITLE=_readable(primary, [card2]),
        SEG_BG=inp, SEG_HOVER=_mix(inp, text, 0.10), SEG_SEL=seg_sel,
        SEG_SEL_HOVER=_fill_for(text, _mix(seg_sel, "#000000" if dark else "#FFFFFF", 0.15)),
        SWITCH_TRACK=_mix(inp, muted, 0.45), SWITCH_KNOB=text if dark else "#FFFFFF",
        GHOST_TEXT=text, REMOVE_HOVER=_mix(card2, danger, 0.25),
        DISABLED_BG=inp, DISABLED_TEXT=_readable(muted, [inp]),
        STOP_IDLE_BG=stop_bg, STOP_IDLE_TEXT=_readable(danger, [stop_bg]),
        STOP_IDLE_BORDER=_readable(_mix(card, danger, 0.55), [card], 3.0),
        BADGE_OK=success, ON_BADGE_OK=_on([success]), BADGE_CPU=badge_cpu, ON_BADGE_CPU=_on([badge_cpu]),
        CHIPS={k: (chip_txt[k], chip_bg[k], _readable(chip_fg[k], [chip_bg[k]])) for k in chip_txt},
    )


def apply_palette(key):
    """Pasang palet ke konstanta modul (dipakai saat widget dibuat) dan sesuaikan mode customtkinter."""
    t = THEMES.get(key) or THEMES[DEFAULT_THEME]
    globals().update(build_palette(t))
    ctk.set_appearance_mode(t["mode"])


apply_palette(DEFAULT_THEME)


# Widget dengan warna teks bawaan yang mengikuti tema (customtkinter hanya punya default terang/gelap).
class TLabel(ctk.CTkLabel):
    def __init__(self, *a, **k):
        k.setdefault("text_color", TEXT)
        super().__init__(*a, **k)


class TSwitch(ctk.CTkSwitch):
    def __init__(self, *a, **k):
        k.setdefault("text_color", TEXT)
        k.setdefault("text_color_disabled", MUTED)
        super().__init__(*a, **k)


class TEntry(ctk.CTkEntry):
    def __init__(self, *a, **k):
        k.setdefault("text_color", TEXT)
        k.setdefault("placeholder_text_color", PLACEHOLDER)
        super().__init__(*a, **k)


class TCombo(ctk.CTkComboBox):
    def __init__(self, *a, **k):
        k.setdefault("text_color", TEXT)
        k.setdefault("dropdown_fg_color", CARD2)
        k.setdefault("dropdown_text_color", TEXT)
        k.setdefault("dropdown_hover_color", BORDER)
        super().__init__(*a, **k)


class TTextbox(ctk.CTkTextbox):
    def __init__(self, *a, **k):
        k.setdefault("text_color", TEXT)
        k.setdefault("scrollbar_button_color", BORDER)
        k.setdefault("scrollbar_button_hover_color", MUTED)
        super().__init__(*a, **k)


class TSegmented(ctk.CTkSegmentedButton):
    def __init__(self, *a, **k):
        k.setdefault("text_color", TEXT)
        super().__init__(*a, **k)


TAB_RESULT = "📝 Hasil"
TAB_LOG = "📋 Log"
MAX_ROWS = 6   # jumlah item yang ditampilkan di daftar sumber
ENGINE_GOOGLE = "🌐 Google (online)"
ENGINE_ARGOS = "💾 Offline (Argos)"

# Bahasa yang tersedia di mode offline. Nilai = kode bahasa Argos (sama dengan kode Whisper).
# Indonesia ikut disertakan karena bahasa sumber/tujuan yang paling sering dipakai.
ARGOS_LANGS = {
    "Inggris": "en",
    "Indonesia": "id",
    "Korea": "ko",
    "Jepang": "ja",
    "Mandarin (Sederhana)": "zh",
    "Rusia": "ru",
}
ARGOS_SOURCES = set(ARGOS_LANGS.values())

MEDIA_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma",
              ".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp")

RESULT_PLACEHOLDER = ("Hasil transkrip akan muncul di sini setelah proses selesai.\n\n"
                      "Tips: untuk lagu, aktifkan “Pisahkan vokal” agar lirik lebih akurat.")

# Pilihan model Whisper (nilai = nama model, label menyertakan perkiraan VRAM GPU)
MODEL_CHOICES = {
    "base — sangat cepat, akurasi dasar (~1 GB)": "base",
    "small — cepat & seimbang (~2 GB)": "small",
    "medium — lebih akurat (~5 GB)": "medium",
    "turbo — akurat & cepat (~6 GB)": "turbo",
}

# Kecepatan vs akurasi dekode. Dipadukan DI ATAS parameter mode (kunci yang sama ditimpa).
# "Cepat" memakai greedy decoding dan fallback suhu seminimal mungkin, jadi jauh lebih ringan
# daripada bawaan mode (beam 5 + best_of 5 + fallback), tetapi lebih rentan salah dengar pada musik/noise.
SPEED_CHOICES = {
    "⚡ Cepat — tanpa pengulangan dekode": dict(beam_size=None, best_of=None, temperature=(0.0, 0.4)),
    "⚖️ Seimbang": dict(beam_size=3, best_of=3, temperature=(0.0, 0.2, 0.4)),
    "🎯 Akurat — bawaan mode": {},
}
DEFAULT_SPEED = "🎯 Akurat — bawaan mode"


SETTINGS_PATH = os.path.join(os.path.dirname(OUTPUT_DIR), "settings.json")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def format_lrc_time(t: float) -> str:
    m = int(t // 60)
    s = t - m * 60
    return f"[{m:02d}:{s:05.2f}]"


def write_lrc(result: dict, audio_path: str, out_dir: str) -> str:
    base = os.path.splitext(os.path.basename(audio_path))[0]
    out_path = os.path.join(out_dir, base + ".lrc")
    with open(out_path, "w", encoding="utf-8") as f:
        for seg in result.get("segments", []):
            text = seg["text"].strip()
            if text:
                f.write(f"{format_lrc_time(seg['start'])}{text}\n")
    return out_path


# Filter penguat dialog pelan: buang dengung rendah, lalu naikkan volume bagian yang pelan secara dinamis
# (maksimum ±28 dB) tanpa membuat bagian yang sudah keras menjadi pecah.
BOOST_FILTER = "highpass=f=60,dynaudnorm=f=150:g=15:m=25"


def get_ffmpeg() -> str:
    exe = os.path.join(ffmpeg_path, "ffmpeg.exe")
    return exe if os.path.exists(exe) else "ffmpeg"


def has_module(name: str) -> bool:
    """True jika modul bisa diimpor (di .exe, modul yang dikemas dicoba diimpor langsung)."""
    try:
        if importlib.util.find_spec(name) is not None:
            return True
    except Exception:
        pass
    if FROZEN:
        try:
            importlib.import_module(name)
            return True
        except Exception:
            return False
    return False


def fmt_dur(sec: float) -> str:
    sec = int(round(max(sec, 0)))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}j {m}m {s}d" if h else (f"{m}m {s}d" if m else f"{s}d")


def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://\S+$", str(text).strip(), re.IGNORECASE))


def format_sub_time(t: float, sep: str) -> str:
    """Format HH:MM:SS<sep>mmm. sep=',' untuk SRT, '.' untuk VTT."""
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def translate_texts(texts, target, on_progress=None):
    """Terjemahkan daftar teks (urutan dipertahankan) via Google Translate.
    Segmen yang gagal setelah 3x percobaan dibiarkan dalam bahasa asli.
    Mengembalikan (hasil, jumlah_gagal)."""
    from deep_translator import GoogleTranslator

    total = len(texts)
    lock = threading.Lock()
    state = {"done": 0, "failed": 0}

    def one(text):
        out = text
        if text.strip():
            for attempt in range(3):
                try:
                    out = GoogleTranslator(source="auto", target=target).translate(text) or text
                    break
                except Exception:
                    if attempt == 2:
                        with lock:
                            state["failed"] += 1
                    else:
                        time.sleep(0.8 * (attempt + 1))
        with lock:
            state["done"] += 1
            if on_progress:
                on_progress(state["done"] / total)
        return out

    with ThreadPoolExecutor(max_workers=4) as pool:
        translated = list(pool.map(one, texts))
    return translated, state["failed"]


def argos_needed_packages(src: str, dst: str):
    """Paket (dari, ke) yang dibutuhkan. Argos menerjemahkan antar bahasa non-Inggris lewat Inggris."""
    if src == dst:
        return []
    if src == "en" or dst == "en":
        return [(src, dst)]
    return [(src, "en"), ("en", dst)]


def all_argos_pairs():
    """Semua paket untuk enam bahasa di ARGOS_LANGS (masing-masing dua arah dengan Inggris)."""
    pairs = []
    for code in sorted(ARGOS_SOURCES - {"en"}):
        pairs += [(code, "en"), ("en", code)]
    return pairs


def argos_installed_pairs():
    import argostranslate.package as ap
    return {(p.from_code, p.to_code) for p in ap.get_installed_packages()}


def install_argos_pairs(pairs, log=None, on_progress=None, should_stop=None):
    """Unduh & pasang paket yang belum ada. Butuh internet hanya jika ada yang kurang.
    Mengembalikan jumlah paket yang baru dipasang."""
    import argostranslate.package as ap

    missing = [p for p in pairs if p not in argos_installed_pairs()]
    if not missing:
        return 0
    if log:
        log("📦 Memperbarui daftar paket Argos (butuh internet)...")
    try:
        ap.update_package_index()
        available = ap.get_available_packages()
    except Exception as e:
        raise RuntimeError(f"Tidak dapat mengambil daftar paket Argos: {e}")

    done = 0
    for i, (a, b) in enumerate(missing):
        if should_stop and should_stop():
            raise DownloadCancelled()
        pkg = next((x for x in available if x.from_code == a and x.to_code == b), None)
        if pkg is None:
            raise RuntimeError(f"Paket {a}→{b} tidak tersedia di daftar Argos.")
        if log:
            log(f"⬇️ Mengunduh paket {a}→{b} ({i + 1}/{len(missing)})...")
        ap.install_from_path(pkg.download())
        done += 1
        if on_progress:
            on_progress((i + 1) / len(missing))
    return done


def translate_texts_argos(texts, source, target, on_progress=None, should_stop=None):
    """Terjemahkan offline dengan Argos. Mengembalikan (hasil, jumlah_gagal)."""
    if source == target:
        return list(texts), 0
    import argostranslate.translate as at

    try:
        at.get_installed_languages.cache_clear()   # pastikan paket yang baru dipasang terbaca
    except Exception:
        pass
    langs = at.get_installed_languages()
    src = next((l for l in langs if l.code == source), None)
    dst = next((l for l in langs if l.code == target), None)
    translation = src.get_translation(dst) if (src and dst) else None
    if translation is None:
        raise RuntimeError(f"Paket bahasa offline {source}→{target} belum terpasang.")

    out, failed, total = [], 0, len(texts)
    for i, text in enumerate(texts):
        if should_stop and should_stop():
            raise DownloadCancelled()
        res = text
        if text.strip():
            try:
                res = translation.translate(text) or text
            except Exception:
                failed += 1
        out.append(res)
        if on_progress:
            on_progress((i + 1) / total)
    return out, failed


def export_translated(result: dict, base: str, code: str, with_lrc: bool, on_progress=None,
                      engine: str = "google", argos_target: str = None, should_stop=None):
    """Terjemahkan seluruh segmen lalu ekspor ke .txt, .srt, .vtt (+ .lrc).
    File diberi akhiran kode bahasa, mis. 'lagu.en.srt'.
    Mengembalikan (teks_terjemahan, daftar_nama_file, jumlah_gagal)."""
    segs = [s for s in result.get("segments", []) if s["text"].strip()]
    texts = [s["text"].strip() for s in segs]
    if engine == "argos":
        translated, failed = translate_texts_argos(
            texts, result.get("language"), argos_target, on_progress, should_stop)
    else:
        translated, failed = translate_texts(texts, code, on_progress)
    stem = os.path.join(OUTPUT_DIR, f"{base}.{code}")

    files = []
    with open(stem + ".txt", "w", encoding="utf-8") as f:
        f.write("\n".join(t.strip() for t in translated) + "\n")
    files.append(os.path.basename(stem + ".txt"))

    with open(stem + ".srt", "w", encoding="utf-8") as f:
        for n, (seg, text) in enumerate(zip(segs, translated), 1):
            f.write(f"{n}\n{format_sub_time(seg['start'], ',')} --> "
                    f"{format_sub_time(seg['end'], ',')}\n{text.strip()}\n\n")
    files.append(os.path.basename(stem + ".srt"))

    with open(stem + ".vtt", "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for seg, text in zip(segs, translated):
            f.write(f"{format_sub_time(seg['start'], '.')} --> "
                    f"{format_sub_time(seg['end'], '.')}\n{text.strip()}\n\n")
    files.append(os.path.basename(stem + ".vtt"))

    if with_lrc:
        with open(stem + ".lrc", "w", encoding="utf-8") as f:
            for seg, text in zip(segs, translated):
                f.write(f"{format_lrc_time(seg['start'])}{text.strip()}\n")
        files.append(os.path.basename(stem + ".lrc"))

    return "\n".join(t.strip() for t in translated), files, failed


class DownloadCancelled(Exception):
    pass


# Penghubung kemajuan transkripsi Whisper -> GUI.
_PROGRESS = {"cb": None}


class _WhisperProgress:
    """Pengganti tqdm milik whisper.transcribe: dipanggil setiap satu jendela audio
    (~30 detik) selesai diproses, lalu meneruskan persentasenya ke GUI."""

    def __init__(self, *args, total=None, **kwargs):
        self.total = total or 0
        self.n = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, n=1):
        self.n += n
        cb = _PROGRESS["cb"]
        if cb and self.total:
            cb(min(1.0, self.n / self.total))

    def close(self):
        pass

    def set_postfix(self, *args, **kwargs):
        pass


def install_whisper_progress_hook():
    try:
        import types
        # whisper.transcribe sebagai atribut adalah FUNGSI, jadi ambil modulnya lewat import_module
        mod = importlib.import_module("whisper.transcribe")
        mod.tqdm = types.SimpleNamespace(tqdm=_WhisperProgress)
    except Exception:
        pass


install_whisper_progress_hook()


# ----------------------------------------------------------------------------
# Aplikasi Utama
# ----------------------------------------------------------------------------
class App(ctk.CTk, _DnDBase):
    def __init__(self):
        super().__init__()

        # Seret & lepas: butuh paket tkinterdnd2 + pustaka native tkdnd yang berhasil dimuat
        self.has_dnd = False
        if HAS_DND_LIB:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.has_dnd = True
            except Exception:
                self.has_dnd = False
        self.title("Transcriber")
        self.geometry("1120x760")
        self.minsize(1100, 640)
        self._early_theme()
        self.configure(fg_color=BG)

        # Buat Ikon & Shortcut Desktop
        icon_p = ensure_icon_exists()
        create_desktop_shortcut()
        if os.path.exists(icon_p):
            try:
                self.iconbitmap(icon_p)
            except Exception:
                pass

        self.files = []          # path file lokal ATAU link http(s)
        self.results = []        # hasil transkrip sesi ini (untuk diterjemahkan)
        self.item_state = {}     # path/link -> "wait" | "run" | "done" | "fail"
        self.item_chips = {}     # path/link -> widget label status
        self._result_empty = True
        self._result_chunks = []   # [(teks, tag)] isi tab Hasil, untuk dipulihkan saat tema diganti
        self._log_lines = []       # isi tab Log
        self._rebuilding = False
        self.model = None
        self.model_name = None
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False

        self.has_demucs = has_module("demucs")
        self.has_ytdlp = has_module("yt_dlp")
        self.has_translator = has_module("deep_translator")
        self.has_argos = has_module("argostranslate")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self._build_ui()
        self._setup_dnd()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-o>", lambda _e: None if self.running else self._browse())
        self.bind("<Control-Return>", lambda _e: None if self.running else self._start())
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------ Helper widget
    def _card(self, parent):
        return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)

    def _step_header(self, parent, num, title):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 10))
        TLabel(head, text=str(num), width=24, height=24, corner_radius=12, fg_color=ACCENT,
                     text_color=ON_ACCENT, font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold")
                     ).pack(side="left")
        TLabel(head, text=title, font=self.f_h).pack(side="left", padx=10)
        return head

    def _ghost_btn(self, parent, text, command, width=100, height=30):
        return ctk.CTkButton(parent, text=text, width=width, height=height, fg_color="transparent",
                             border_width=1, border_color=BORDER, hover_color=CARD2, text_color=GHOST_TEXT, text_color_disabled=MUTED,
                             font=self.f_body, command=command)

    def _style_btn(self, btn, enabled, kind):
        """Terapkan tampilan aktif/nonaktif yang tetap terbaca pada tombol berwarna."""
        fg, hover, txt = {
            "accent": (ACCENT, ACCENT_HOVER, ON_ACCENT),
            "convert": (CONVERT_BTN, CONVERT_HOVER, ON_CONVERT),
            "danger": (DANGER, DANGER_HOVER, ON_DANGER),
        }[kind]
        if enabled:
            btn.configure(state="normal", fg_color=fg, hover_color=hover, text_color=txt, border_width=0)
        elif kind == "danger":
            btn.configure(state="disabled", fg_color=STOP_IDLE_BG, text_color_disabled=STOP_IDLE_TEXT,
                          border_width=1, border_color=STOP_IDLE_BORDER)
        else:
            btn.configure(state="disabled", fg_color=DISABLED_BG, text_color_disabled=DISABLED_TEXT,
                          border_width=0)

    def _refresh_button_styles(self):
        r = self.running
        self._style_btn(self.btn_browse, not r, "accent")
        self._style_btn(self.btn_add_link, (not r) and self.has_ytdlp, "convert")
        self._style_btn(self.btn_translate, not r, "convert")
        self._style_btn(self.btn_stop, r, "danger")

    def _switch(self, parent, text, sub=None):
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(fill="x", pady=4)
        sw = TSwitch(box, text=text, progress_color=ACCENT, button_color=SWITCH_KNOB,
                           fg_color=SWITCH_TRACK, font=self.f_body)
        sw.pack(anchor="w")
        if sub:
            TLabel(box, text=sub, font=self.f_small, text_color=MUTED, justify="left",
                         wraplength=340).pack(anchor="w", padx=(46, 0))
        return sw

    # ------------------------------------------------------------------ Susunan UI
    def _build_ui(self):
        self.f_title = ctk.CTkFont(family="Segoe UI", size=24, weight="bold")
        self.f_h = ctk.CTkFont(family="Segoe UI", size=14, weight="bold")
        self.f_body = ctk.CTkFont(family="Segoe UI", size=13)
        self.f_bold = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
        self.f_small = ctk.CTkFont(family="Segoe UI", size=12)
        self.font_mono = ctk.CTkFont(family="Consolas", size=12)

        wrap = ctk.CTkFrame(self, fg_color="transparent")
        self._wrap = wrap
        wrap.pack(fill="both", expand=True, padx=20, pady=16)
        wrap.grid_columnconfigure(1, weight=1)
        wrap.grid_rowconfigure(1, weight=1)

        self._build_header(wrap)

        # Kolom kiri: input (sumber + pengaturan). Bisa di-scroll di layar kecil.
        left = ctk.CTkScrollableFrame(wrap, fg_color="transparent", width=410, corner_radius=0,
                                      scrollbar_button_color=BORDER, scrollbar_button_hover_color=MUTED)
        left.grid(row=1, column=0, sticky="ns", padx=(0, 14))
        self._build_source_card(left)
        self._build_settings_card(left)

        # Kolom kanan: tombol mulai, progres, dan hasil.
        right = ctk.CTkFrame(wrap, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew")
        self._build_action_card(right)
        self._build_result_card(right)

        if not self._rebuilding:   # log awal hanya sekali (bukan saat UI dibangun ulang karena ganti tema)
            if self.device != "cuda":
                if torch.version.cuda is None:
                    self._log("⚠️ PyTorch yang terpasang versi CPU-only, jadi GPU tidak terpakai. "
                              "Pasang PyTorch versi CUDA (lihat https://pytorch.org/get-started/locally/).")
                else:
                    self._log("⚠️ PyTorch versi CUDA terpasang, tapi GPU tidak terdeteksi. "
                              "Perbarui driver NVIDIA lalu restart komputer.")
            else:
                self._log(f"GPU: {torch.cuda.get_device_name(0)} | VRAM "
                          f"{torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB")
            self._log(f"Perangkat: {self.device.upper()} | Demucs: {'ada' if self.has_demucs else 'tidak ada'} | "
                      f"yt-dlp: {'ada' if self.has_ytdlp else 'TIDAK ADA'} | "
                      f"Google: {'siap' if self.has_translator else 'TIDAK ADA (pip install deep-translator)'} | "
                      f"Argos offline: {'siap' if self.has_argos else 'TIDAK ADA (pip install argostranslate)'}")


        self._apply_engine()
        self._refresh_button_styles()
        self._refresh_files()
        self._reset_results()
        self._load_settings()

    def _build_header(self, wrap):
        header = ctk.CTkFrame(wrap, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))

        left = ctk.CTkFrame(header, fg_color="transparent")
        left.pack(side="left")
        TLabel(left, text="🎵 Transcriber", font=self.f_title).pack(anchor="w")
        TLabel(left, text="Audio, video & link → teks, lirik, dan terjemahan.",
                     font=self.f_small, text_color=MUTED).pack(anchor="w")

        # Status komponen opsional (hijau = siap, kuning = belum terpasang)
        chips = ctk.CTkFrame(left, fg_color="transparent")
        chips.pack(anchor="w", pady=(6, 0))
        for ok, label in ((self.has_demucs, "Demucs"),
                          (self.has_ytdlp, "yt-dlp"),
                          (self.has_translator, "Google"),
                          (self.has_argos, "Argos"),
                          (self.has_dnd, "Drag & drop")):
            TLabel(chips, text=f"{'●' if ok else '○'} {label}",
                         font=self.f_small, text_color=OK if ok else WARN).pack(side="left", padx=(0, 14))

        # Pilihan tema (paling kanan): Tema Gelap / Tema Terang
        self.theme_btn = TSegmented(
            header, values=list(THEME_LABELS), command=self._on_theme, selected_color=SEG_SEL,
            selected_hover_color=SEG_SEL_HOVER, unselected_color=SEG_BG, unselected_hover_color=SEG_HOVER,
            fg_color=SEG_BG, font=self.f_small, height=30)
        self.theme_btn.set(THEMES[self.theme_key]["label"])
        self.theme_btn.pack(side="right", padx=(10, 0))

        if self.device == "cuda":
            short = re.sub(r"\b(NVIDIA|GeForce|GPU)\b\s*", "", torch.cuda.get_device_name(0)).strip()
            badge_text = f"⚡ GPU • {short}"
            badge_color, badge_fg = BADGE_OK, ON_BADGE_OK
        else:
            badge_text = "🖥️ CPU (lebih lambat)"
            badge_color, badge_fg = BADGE_CPU, ON_BADGE_CPU
        TLabel(header, text=badge_text, font=self.f_body, fg_color=badge_color, text_color=badge_fg,
               corner_radius=14, padx=14, pady=6).pack(side="right")
        self.btn_convert = self._ghost_btn(header, "🎥 MP4 → MP3", self._convert_mp4_to_mp3, width=120, height=34)
        self.btn_convert.pack(side="right", padx=(0, 10))
        self.btn_open = self._ghost_btn(header, "📂 Folder Hasil", self._open_output, width=130, height=34)
        self.btn_open.pack(side="right", padx=(0, 10))

    def _build_source_card(self, parent):
        card = self._card(parent)
        card.pack(fill="x", pady=(0, 12))
        self.src_card = card
        head = self._step_header(card, 1, "Sumber audio / video")
        self.count_label = TLabel(head, text="", font=self.f_small, text_color=MUTED)
        self.count_label.pack(side="right")

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=(0, 8))
        self.btn_browse = ctk.CTkButton(btns, text="📁  Pilih File", height=34, fg_color=ACCENT,
                                        hover_color=ACCENT_HOVER, text_color=ON_ACCENT, font=self.f_bold,
                                        command=self._browse)
        self.btn_browse.pack(side="left", fill="x", expand=True)
        self.btn_clear = self._ghost_btn(btns, "🗑️ Kosongkan", self._clear_files, width=110, height=34)
        self.btn_clear.pack(side="left", padx=(8, 0))

        self.file_list = ctk.CTkFrame(card, fg_color=CARD2, corner_radius=12)
        self.file_list.pack(fill="x", padx=16, pady=(0, 8))

        link_row = ctk.CTkFrame(card, fg_color="transparent")
        link_row.pack(fill="x", padx=16, pady=(0, 16))
        self.link_entry = TEntry(link_row, height=34, font=self.f_body, fg_color=INPUT, border_width=0,
                                       placeholder_text="Atau tempel link video, lalu tekan Enter")
        self.link_entry.pack(side="left", fill="x", expand=True)
        self.link_entry.bind("<Return>", lambda _e: self._add_link())
        self.btn_add_link = ctk.CTkButton(link_row, text="＋ Tambah", width=90, height=34, fg_color=CONVERT_BTN,
                                          hover_color=CONVERT_HOVER, text_color=ON_CONVERT, font=self.f_bold,
                                          command=self._add_link)
        self.btn_add_link.pack(side="left", padx=(8, 0))
        if not self.has_ytdlp:
            self.link_entry.configure(state="disabled",
                                      placeholder_text="yt-dlp belum terpasang: pip install -U yt-dlp")
            self.btn_add_link.configure(state="disabled")

    def _build_settings_card(self, parent):
        card = self._card(parent)
        card.pack(fill="x", pady=(0, 12))
        self._step_header(card, 2, "Pengaturan")
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=16)

        TLabel(body, text="Jenis audio", font=self.f_bold).pack(anchor="w")
        self.mode_var = ctk.StringVar(value="🎤 Lagu")
        self.mode_btn = TSegmented(
            body, values=list(MODES.keys()), variable=self.mode_var, command=self._on_mode,
            selected_color=SEG_SEL, selected_hover_color=SEG_SEL_HOVER, unselected_color=SEG_BG,
            unselected_hover_color=SEG_HOVER, fg_color=SEG_BG, font=self.f_body, height=34)
        self.mode_btn.pack(fill="x", pady=(4, 4))
        self.mode_hint = TLabel(body, text=MODES["🎤 Lagu"]["hint"], font=self.f_small,
                                      text_color=MUTED, wraplength=360, justify="left")
        self.mode_hint.pack(anchor="w", pady=(0, 12))

        TLabel(body, text="Bahasa audio", font=self.f_bold).pack(anchor="w")
        self.lang_box = TCombo(body, values=list(LANGUAGE_MAP.keys()), state="readonly",
                                        fg_color=INPUT, border_width=0, button_color=ACCENT,
                                        button_hover_color=ACCENT_HOVER, font=self.f_body, height=34)
        self.lang_box.set("Indonesian")
        self.lang_box.pack(fill="x", pady=(4, 2))
        TLabel(body, text="💡 Untuk lagu, pilih bahasa secara manual (jangan auto-detect) agar lebih akurat.",
                     font=self.f_small, text_color=MUTED, wraplength=360, justify="left"
                     ).pack(anchor="w", pady=(0, 12))

        ctk.CTkFrame(body, height=1, fg_color=BORDER).pack(fill="x", pady=(0, 8))

        self.sep_switch = self._switch(body, "Pisahkan vokal dulu (Demucs)",
                                       "Akurasi lirik jauh lebih baik, tapi proses lebih lama.")
        if self.has_demucs:
            self.sep_switch.select()
        else:
            self.sep_switch.configure(text="Pisahkan vokal (Demucs belum terpasang)", state="disabled")
        self.lrc_switch = self._switch(body, "Simpan lirik bersinkron (.lrc)",
                                       "Bisa dipakai di pemutar musik atau karaoke.")
        self.lrc_switch.select()
        self.boost_switch = self._switch(
            body, "Perkuat suara pelan",
            "Naikkan volume dialog yang pelan dan kurangi pelewatan bagian sunyi. "
            "Aktifkan jika ada kalimat yang tidak terbaca.")
        self.boost_switch.select()

        trow = ctk.CTkFrame(body, fg_color="transparent")
        trow.pack(fill="x", pady=(4, 0))
        self.tr_switch = TSwitch(trow, text="Terjemahkan otomatis ke", progress_color=ACCENT,
                                       button_color=SWITCH_KNOB, fg_color=SWITCH_TRACK, font=self.f_body)
        self.tr_switch.pack(side="left")
        self.tr_box = TCombo(trow, values=list(TRANSLATE_LANGS.keys()), state="readonly", width=150,
                                      fg_color=INPUT, border_width=0, button_color=ACCENT,
                                      button_hover_color=ACCENT_HOVER, font=self.f_body, height=32,
                                      command=self._on_tr_change)
        self.tr_box.set("Inggris")
        self.tr_box.pack(side="right")
        self.engine_var = ctk.StringVar(value=ENGINE_GOOGLE)
        self.engine_btn = TSegmented(
            body, values=[ENGINE_GOOGLE, ENGINE_ARGOS], variable=self.engine_var, command=self._on_engine,
            selected_color=SEG_SEL, selected_hover_color=SEG_SEL_HOVER, unselected_color=SEG_BG,
            unselected_hover_color=SEG_HOVER, fg_color=SEG_BG, font=self.f_small, height=30)
        self.engine_btn.pack(fill="x", pady=(8, 4))
        self.tr_sub = TLabel(body, text="", font=self.f_small, text_color=MUTED, justify="left",
                                   wraplength=360)
        self.tr_sub.pack(anchor="w", pady=(0, 6))
        self.btn_argos_pkg = self._ghost_btn(body, "📦  Pasang paket bahasa offline",
                                             self._install_argos_packages, width=200, height=32)
        self.btn_argos_pkg.pack(fill="x", pady=(0, 4))

        # Opsi lanjutan (dilipat secara default supaya tampilan tetap ringkas)
        self.adv_open = False
        self.adv_btn = ctk.CTkButton(card, text="⚙️  Opsi lanjutan  ▸", anchor="w", fg_color="transparent",
                                     hover_color=CARD2, text_color=MUTED, font=self.f_body, height=30,
                                     command=self._toggle_adv)
        self.adv_btn.pack(fill="x", padx=12, pady=(8, 12))
        self.adv_frame = ctk.CTkFrame(card, fg_color="transparent")

        TLabel(self.adv_frame, text="Model Whisper", font=self.f_bold).pack(anchor="w")
        self.model_box = TCombo(self.adv_frame, values=list(MODEL_CHOICES.keys()), state="readonly",
                                         fg_color=INPUT, border_width=0, button_color=ACCENT,
                                         button_hover_color=ACCENT_HOVER, font=self.f_body, height=34)
        self.model_box.set(next(k for k, v in MODEL_CHOICES.items() if v == MODEL_NAME))
        self.model_box.pack(fill="x", pady=(4, 2))
        TLabel(self.adv_frame, text="Model lebih besar = lebih akurat, tapi lebih lambat dan butuh VRAM "
                                          "GPU lebih banyak. Jika muncul error VRAM, pilih model lebih kecil.",
                     font=self.f_small, text_color=MUTED, wraplength=360, justify="left"
                     ).pack(anchor="w", pady=(0, 10))
        TLabel(self.adv_frame, text="Kecepatan vs akurasi", font=self.f_bold).pack(anchor="w")
        self.speed_box = TCombo(self.adv_frame, values=list(SPEED_CHOICES.keys()), state="readonly",
                                         fg_color=INPUT, border_width=0, button_color=ACCENT,
                                         button_hover_color=ACCENT_HOVER, font=self.f_body, height=34)
        self.speed_box.set(DEFAULT_SPEED)
        self.speed_box.pack(fill="x", pady=(4, 2))
        TLabel(self.adv_frame, text="Cepat: bisa beberapa kali lebih cepat, tetapi lebih rentan salah "
                                          "dengar di musik/noise. Akurat = pengaturan bawaan mode.",
                     font=self.f_small, text_color=MUTED, wraplength=360, justify="left"
                     ).pack(anchor="w", pady=(0, 10))
        TLabel(self.adv_frame, text="Petunjuk tambahan (opsional)", font=self.f_bold).pack(anchor="w")
        self.prompt_entry = TEntry(
            self.adv_frame, height=34, font=self.f_body, fg_color=INPUT, border_width=0,
            placeholder_text="Nama artis, judul, atau kata sulit yang sering muncul")
        self.prompt_entry.pack(fill="x", pady=(4, 0))

    def _build_action_card(self, parent):
        card = self._card(parent)
        card.pack(fill="x", pady=(0, 12))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(16, 12))
        self.btn_start = ctk.CTkButton(row, text="▶  Mulai Transkripsi", height=52, corner_radius=12,
                                       font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
                                       fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT,
                                       text_color_disabled=ON_ACCENT, command=self._start)
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_stop = ctk.CTkButton(row, text="⏹  Hentikan", height=52, width=120, corner_radius=12,
                                      fg_color=DANGER, hover_color=DANGER_HOVER, text_color=ON_DANGER, font=self.f_bold,
                                      state="disabled", command=self._stop)
        self.btn_stop.pack(side="left", padx=(10, 0))

        prog_row = ctk.CTkFrame(card, fg_color="transparent")
        prog_row.pack(fill="x", padx=16)
        self.progress = ctk.CTkProgressBar(prog_row, height=14, progress_color=ACCENT, fg_color=INPUT)
        self.progress.set(0)
        self.progress.pack(side="left", fill="x", expand=True)
        self.pct_label = TLabel(prog_row, text="0%", width=52, font=self.f_bold, text_color=MUTED)
        self.pct_label.pack(side="left", padx=(10, 0))

        self.status = TLabel(card, text="Siap. Pilih file atau tempel link, lalu klik Mulai.",
                                   font=self.f_small, text_color=MUTED, anchor="w", justify="left",
                                   wraplength=640)
        self.status.pack(fill="x", padx=16, pady=(6, 14))

    def _build_result_card(self, parent):
        card = self._card(parent)
        card.pack(fill="both", expand=True)

        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(12, 8))
        self.tab_var = ctk.StringVar(value=TAB_RESULT)
        self.tab_btn = TSegmented(
            bar, values=[TAB_RESULT, TAB_LOG], variable=self.tab_var, command=self._show_tab,
            selected_color=SEG_SEL, selected_hover_color=SEG_SEL_HOVER, unselected_color=SEG_BG,
            unselected_hover_color=SEG_HOVER, fg_color=SEG_BG, font=self.f_body, height=30)
        self.tab_btn.pack(side="left")

        self.btn_translate = ctk.CTkButton(bar, text="🌐 Terjemahkan ke Inggris", height=30, width=200,
                                           fg_color=CONVERT_BTN, hover_color=CONVERT_HOVER, text_color=ON_CONVERT,
                                           font=self.f_bold, command=self._translate_last)
        self.btn_translate.pack(side="right")
        self.btn_copy = self._ghost_btn(bar, "📋 Salin", self._copy_results, width=80)
        self.btn_copy.pack(side="right", padx=(0, 8))
        self.btn_clear_res = self._ghost_btn(bar, "🧹 Bersihkan", self._clear_current, width=100)
        self.btn_clear_res.pack(side="right", padx=(0, 8))

        holder = ctk.CTkFrame(card, fg_color="transparent")
        holder.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.result_box = TTextbox(holder, font=ctk.CTkFont(family="Segoe UI", size=14), fg_color=CARD2,
                                         wrap="word", corner_radius=12)
        self.log_box = TTextbox(holder, font=self.font_mono, fg_color=CARD2, wrap="word",
                                      corner_radius=12)
        self._style_text_tags()
        self.result_box.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ Interaksi UI
    def _show_tab(self, name):
        self.tab_var.set(name)
        if name == TAB_LOG:
            self.result_box.pack_forget()
            self.log_box.pack(fill="both", expand=True)
        else:
            self.log_box.pack_forget()
            self.result_box.pack(fill="both", expand=True)

    def _toggle_adv(self):
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.adv_frame.pack(fill="x", padx=16, pady=(0, 14))
            self.adv_btn.configure(text="⚙️  Opsi lanjutan  ▾")
        else:
            self.adv_frame.pack_forget()
            self.adv_btn.configure(text="⚙️  Opsi lanjutan  ▸")

    def _engine_key(self):
        return "argos" if self.engine_var.get() == ENGINE_ARGOS else "google"

    def _engine_ready(self):
        return self.has_argos if self._engine_key() == "argos" else self.has_translator

    def _update_argos_btn(self):
        usable = self._engine_key() == "argos" and self.has_argos and not self.running
        self.btn_argos_pkg.configure(state="normal" if usable else "disabled")

    def _apply_engine(self):
        """Sesuaikan daftar bahasa, keterangan, dan tombol dengan mesin terjemahan terpilih."""
        key = self._engine_key()
        values = list(ARGOS_LANGS.keys()) if key == "argos" else list(TRANSLATE_LANGS.keys())
        current = self.tr_box.get()
        self.tr_box.configure(values=values)
        if current not in values:
            current = "Inggris"
        self.tr_box.set(current)
        self._on_tr_change(current)

        if not self._engine_ready():
            text = ("Argos Translate belum terpasang: pip install argostranslate" if key == "argos"
                    else "deep-translator belum terpasang: pip install deep-translator")
        elif key == "argos":
            text = ("Offline: berjalan di komputer Anda. Internet hanya dipakai sekali untuk mengunduh paket "
                    "bahasa (Inggris, Indonesia, Korea, Jepang, Mandarin, Rusia). Hasil: .txt, .srt, .vtt.")
        else:
            text = "Memakai Google Translate (butuh internet). Hasil: .txt, .srt, .vtt."
        self.tr_sub.configure(text=text)
        self.tr_switch.configure(state="normal" if self._engine_ready() else "disabled")
        self._update_argos_btn()

    def _on_engine(self, value):
        if value == ENGINE_ARGOS and not self.has_argos:
            self.engine_var.set(ENGINE_GOOGLE)
            msg = "Argos Translate belum terpasang. Jalankan: pip install argostranslate lalu buka ulang aplikasi."
            self.status.configure(text="⚠️ " + msg)
            self._log("⚠️ " + msg)
            self._show_tab(TAB_LOG)
        self._apply_engine()

    # ------------------------------------------------------------------ Tema
    def _early_theme(self):
        """Baca tema tersimpan dan terapkan sebelum widget dibuat (mencegah kedipan)."""
        key = DEFAULT_THEME
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                saved = json.load(f).get("theme")
            # Nama tema dari versi sebelumnya dipetakan ke dua tema yang ada sekarang.
            saved = {"dark": "gelap", "warm_dark": "gelap", "light": "terang", "modern_blue": "terang",
                     "clean_minimal": "terang", "soft_pastel": "terang"}.get(saved, saved)
            if saved in THEMES:
                key = saved
        except Exception:
            pass
        self.theme_key = key
        apply_palette(key)

    def _style_text_tags(self):
        """Warna tag di kotak teks harus berupa satu warna hex (diambil dari palet aktif)."""
        self.result_box.tag_config("title", foreground=TITLE)
        self.result_box.tag_config("muted", foreground=MUTED)

    def _on_theme(self, value):
        key = THEME_LABELS.get(value)
        if not key or key == self.theme_key:
            return
        self.theme_key = key
        # Dijadwalkan agar kontrol yang memanggil callback ini tidak dihancurkan saat masih berjalan.
        self.after(40, self._switch_theme)

    def _switch_theme(self):
        apply_palette(self.theme_key)
        self._rebuild_ui()

    def _rebuild_ui(self):
        """Bangun ulang seluruh tampilan dengan palet baru, lalu pulihkan semua isi dan keadaan."""
        snap = dict(
            status=self.status.cget("text"), progress=self.progress.get(), tab=self.tab_var.get(),
            adv=self.adv_open, prompt=self.prompt_entry.get(), link=self.link_entry.get(),
            chunks=list(self._result_chunks), empty=self._result_empty)
        self._save_settings()
        self._wrap.destroy()
        self.configure(fg_color=BG)
        self._rebuilding = True
        try:
            self._build_ui()
        finally:
            self._rebuilding = False

        if not snap["empty"]:
            self.result_box.delete("1.0", "end")
            self._result_empty = False
            for text, tag in snap["chunks"]:
                self._result_add(text, tag)
            self.result_box.see("end")
        for line in self._log_lines:
            self.log_box.insert("end", line)
        self.log_box.see("end")
        if snap["adv"]:
            self._toggle_adv()
        if snap["prompt"]:
            self.prompt_entry.insert(0, snap["prompt"])
        if snap["link"] and self.has_ytdlp:
            self.link_entry.insert(0, snap["link"])
        self._set_running(self.running)
        self._set_progress(snap["progress"])
        self.status.configure(text=snap["status"])
        self._show_tab(snap["tab"])

    def _result_add(self, text, tag=None):
        self._result_chunks.append((text, tag))
        if tag:
            self.result_box.insert("end", text, tag)
        else:
            self.result_box.insert("end", text)

    def _on_tr_change(self, value):
        short = value.split(" (")[0]
        self.btn_translate.configure(text=f"🌐 Terjemahkan ke {short}")

    def _reset_results(self):
        self.result_box.delete("1.0", "end")
        self.result_box.insert("1.0", RESULT_PLACEHOLDER, "muted")
        self._result_empty = True
        self._result_chunks = []

    def _copy_results(self):
        if self.tab_var.get() == TAB_LOG:
            text = self.log_box.get("1.0", "end").strip()
            if not text:
                self.status.configure(text="⚠️ Log masih kosong.")
                return
            self.clipboard_clear()
            self.clipboard_append(text)
            self.status.configure(text="✅ Log disalin ke clipboard.")
            return
        if self._result_empty:
            self.status.configure(text="⚠️ Belum ada hasil untuk disalin.")
            return
        text = self.result_box.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text="✅ Hasil disalin ke clipboard.")

    def _clear_current(self):
        if self.tab_var.get() == TAB_LOG:
            self.log_box.delete("1.0", "end")
        else:
            self._reset_results()

    # ------------------------------------------------------------------ Pengaturan tersimpan
    def _load_settings(self):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        try:
            if d.get("mode") in MODES:
                self.mode_var.set(d["mode"])
                self.mode_hint.configure(text=MODES[d["mode"]]["hint"])
            if d.get("lang") in LANGUAGE_MAP:
                self.lang_box.set(d["lang"])
            if d.get("engine") in (ENGINE_GOOGLE, ENGINE_ARGOS) and (d["engine"] != ENGINE_ARGOS or self.has_argos):
                self.engine_var.set(d["engine"])
                self._apply_engine()
            allowed = ARGOS_LANGS if self._engine_key() == "argos" else TRANSLATE_LANGS
            if d.get("target") in allowed:
                self.tr_box.set(d["target"])
                self._on_tr_change(d["target"])
            if d.get("model") in MODEL_CHOICES:
                self.model_box.set(d["model"])
            if d.get("speed") in SPEED_CHOICES:
                self.speed_box.set(d["speed"])
            for key, sw, available in (("sep", self.sep_switch, self.has_demucs),
                                       ("lrc", self.lrc_switch, True),
                                       ("boost", self.boost_switch, True),
                                       ("tr", self.tr_switch, self._engine_ready())):
                if key in d and available:
                    sw.select() if d[key] else sw.deselect()
        except Exception:
            pass

    def _save_settings(self):
        d = dict(
            mode=self.mode_var.get(), lang=self.lang_box.get(), target=self.tr_box.get(),
            model=self.model_box.get(), sep=bool(self.sep_switch.get()), lrc=bool(self.lrc_switch.get()), boost=bool(self.boost_switch.get()),
            tr=bool(self.tr_switch.get()), engine=self.engine_var.get(), speed=self.speed_box.get(),
            theme=self.theme_key,
        )
        try:
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _on_close(self):
        self._save_settings()
        self.destroy()

    def _on_mode(self, value):
        self.mode_hint.configure(text=MODES[value]["hint"])
        if self.has_demucs:
            if value.startswith("🎤"):
                self.sep_switch.select()
            else:
                self.sep_switch.deselect()

    @staticmethod
    def _short(text, n=40):
        return text if len(text) <= n else text[:n - 1] + "…"

    def _display_name(self, p):
        if is_url(p):
            return self._short(re.sub(r"^https?://(www\.)?", "", p), 38)
        return self._short(os.path.basename(p), 38)

    def _set_item_state(self, key, state):
        self.item_state[key] = state
        chip = self.item_chips.get(key)
        if chip is not None:
            text, bg, fg = CHIPS[state]
            chip.configure(text=text, fg_color=bg, text_color=fg)

    def _remove_file(self, path):
        if self.running:
            return
        if path in self.files:
            self.files.remove(path)
            self.item_state.pop(path, None)
        self._refresh_files()

    def _refresh_files(self):
        for w in self.file_list.winfo_children():
            w.destroy()
        self.item_chips = {}
        n = len(self.files)
        self.count_label.configure(text=f"{n} item" if n else "")

        if not self.files:
            empty = ("📂  Seret file ke sini, klik “Pilih File”,\natau tempel link di bawah." if self.has_dnd
                     else "📂  Belum ada file.\nKlik “Pilih File” atau tempel link di bawah.")
            TLabel(self.file_list, text=empty, font=self.f_body, text_color=MUTED,
                         justify="center").pack(pady=22)
            return

        for p in self.files[:MAX_ROWS]:
            row = ctk.CTkFrame(self.file_list, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=3)
            row.grid_columnconfigure(1, weight=1)
            TLabel(row, text="🔗" if is_url(p) else "🎵", width=24, font=self.f_body
                         ).grid(row=0, column=0, padx=(2, 4))
            TLabel(row, text=self._display_name(p), font=self.f_body, anchor="w"
                         ).grid(row=0, column=1, sticky="w")
            text, bg, fg = CHIPS[self.item_state.get(p, "wait")]
            chip = TLabel(row, text=text, width=86, height=22, corner_radius=8, fg_color=bg,
                                text_color=fg, font=self.f_small)
            chip.grid(row=0, column=2, padx=6)
            self.item_chips[p] = chip
            ctk.CTkButton(row, text="✕", width=26, height=26, fg_color="transparent", hover_color=REMOVE_HOVER,
                          text_color=MUTED, font=self.f_body,
                          state="disabled" if self.running else "normal",
                          command=lambda path=p: self._remove_file(path)).grid(row=0, column=3)
        if n > MAX_ROWS:
            TLabel(self.file_list, text=f"+ {n - MAX_ROWS} item lainnya", font=self.f_small,
                         text_color=MUTED).pack(pady=(0, 8))
        else:
            ctk.CTkFrame(self.file_list, height=4, fg_color="transparent").pack()

    def _add_link(self):
        if not self.has_ytdlp:
            return
        parts = self.link_entry.get().split()
        if not parts:
            return
        invalid = [p for p in parts if not is_url(p)]
        for p in parts:
            if is_url(p) and p not in self.files:
                self.files.append(p)
        if invalid:
            self.status.configure(text="⚠️ Link harus diawali http:// atau https://")
        else:
            self.link_entry.delete(0, "end")
            self.status.configure(text="Link ditambahkan.")
        self._refresh_files()

    def _set_progress(self, value):
        value = max(0.0, min(1.0, float(value)))
        self.progress.set(value)
        self.pct_label.configure(text=f"{value * 100:.0f}%")

    def _log(self, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {text}\n"
        self._log_lines.append(line)
        if len(self._log_lines) > 3000:
            del self._log_lines[:500]
        self.log_box.insert("end", line)
        self.log_box.see("end")

    def _set_running(self, running):
        self.running = running
        self.btn_start.configure(state="disabled" if running else "normal",
                                 text="⏳  Sedang memproses…" if running else "▶  Mulai Transkripsi")
        self.btn_convert.configure(state="disabled" if running else "normal")
        self.btn_clear.configure(state="disabled" if running else "normal")
        link_state = "disabled" if (running or not self.has_ytdlp) else "normal"
        self.link_entry.configure(state=link_state)
        self._refresh_button_styles()
        self.engine_btn.configure(state="disabled" if running else "normal")
        self._update_argos_btn()
        self._refresh_files()

    # ------------------------------------------------------------------ Seret & lepas
    def _setup_dnd(self):
        if not self.has_dnd:
            return
        try:
            self.drop_target_register(DND_FILES, DND_TEXT)
            self.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            self.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as e:
            self.has_dnd = False
            self._log(f"⚠️ Seret & lepas tidak dapat diaktifkan: {e}")

    def _drop_highlight(self, on):
        self.src_card.configure(border_color=ACCENT if on else BORDER, border_width=2 if on else 1)

    def _on_drop_enter(self, event):
        if not self.running:
            self._drop_highlight(True)
            self.status.configure(text="📥 Lepaskan di sini untuk menambahkan ke daftar sumber.")
        return event.action

    def _on_drop_leave(self, event):
        self._drop_highlight(False)
        return event.action

    def _collect_media(self, folder):
        found = []
        for root, _dirs, names in os.walk(folder):
            for n in sorted(names):
                if n.lower().endswith(MEDIA_EXTS):
                    found.append(os.path.join(root, n))
        return found

    def _on_drop(self, event):
        self._drop_highlight(False)
        if self.running:
            self.status.configure(text="⚠️ Tunggu proses selesai dulu sebelum menambah file.")
            return event.action

        try:
            tokens = self.tk.splitlist(event.data)   # menangani path ber-spasi: {C:/Musik Saya/a.mp3}
        except Exception:
            tokens = str(event.data).split()

        added = unsupported = links_blocked = 0
        for raw in tokens:
            raw = raw.strip().strip('"')
            if not raw:
                continue
            candidates = []
            if os.path.isdir(raw):
                candidates = self._collect_media(raw)
            elif os.path.isfile(raw):
                if raw.lower().endswith(MEDIA_EXTS):
                    candidates = [raw]
                else:
                    unsupported += 1
            elif is_url(raw):
                if self.has_ytdlp:
                    candidates = [raw]
                else:
                    links_blocked += 1
            for c in candidates:
                if c not in self.files:
                    self.files.append(c)
                    added += 1

        self._refresh_files()
        msg = f"✅ {added} item ditambahkan." if added else "Tidak ada item baru yang ditambahkan."
        if unsupported:
            msg += f" {unsupported} file dilewati (format tidak didukung)."
        if links_blocked:
            msg += " Link dilewati: yt-dlp belum terpasang."
        self.status.configure(text=msg)
        return event.action

    def _browse(self):
        paths = filedialog.askopenfilenames(
            title="Pilih File Audio / Video",
            filetypes=[("Audio & Video", " ".join(f"*{e}" for e in MEDIA_EXTS)),
                       ("Semua file", "*.*")])
        if paths:
            for p in paths:
                if p not in self.files:
                    self.files.append(p)
            self._refresh_files()

    def _clear_files(self):
        if self.running:
            return
        self.files = []
        self.item_state = {}
        self._refresh_files()

    def _open_output(self):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        try:
            os.startfile(OUTPUT_DIR)
        except AttributeError:
            subprocess.Popen(["xdg-open", OUTPUT_DIR])

    # ------------------------------------------------------------ Konverter MP4 -> MP3
    def _convert_mp4_to_mp3(self):
        paths = filedialog.askopenfilenames(
            title="Pilih File Video (MP4/MKV) untuk Dikonversi ke MP3",
            filetypes=[("Video File", "*.mp4 *.mkv *.mov *.avi *.webm"), ("Semua file", "*.*")]
        )
        if not paths:
            return

        self.stop_event.clear()
        self._set_running(True)
        self._set_progress(0)
        self._show_tab(TAB_LOG)
        threading.Thread(target=self._worker_convert_mp3, args=(paths,), daemon=True).start()

    def _worker_convert_mp3(self, paths):
        total = len(paths)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        self.q.put(("log", f"🎬 Memulai konversi {total} file video ke MP3..."))

        for i, path in enumerate(paths, 1):
            if self.stop_event.is_set():
                self.q.put(("log", "⏹ Konversi dihentikan oleh pengguna."))
                break

            base_name = os.path.splitext(os.path.basename(path))[0]
            out_mp3 = os.path.join(OUTPUT_DIR, f"{base_name}.mp3")
            self.q.put(("status", f"[{i}/{total}] Mengonversi ke MP3: {os.path.basename(path)}"))
            self.q.put(("log", f"🎵 [{i}/{total}] Mengonversi: {os.path.basename(path)} -> {base_name}.mp3"))

            ffmpeg_bin = os.path.join(ffmpeg_path, "ffmpeg.exe") if os.path.exists(os.path.join(ffmpeg_path, "ffmpeg.exe")) else "ffmpeg"
            cmd = [
                ffmpeg_bin, "-y", "-i", path, "-vn",
                "-acodec", "libmp3lame", "-q:a", "2", out_mp3
            ]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
                if proc.returncode == 0:
                    self.q.put(("log", f"✅ Berhasil disimpan: {base_name}.mp3"))
                else:
                    self.q.put(("log", f"❌ Gagal mengonversi {os.path.basename(path)}"))
            except Exception as e:
                self.q.put(("log", f"❌ Error konversi: {e}"))

            self.q.put(("progress", i / total))

        self.q.put(("log", f"🎉 Konversi selesai! Hasil MP3 tersimpan di: {OUTPUT_DIR}"))
        self.q.put(("done",))

    def _stop(self):
        self.stop_event.set()
        self._style_btn(self.btn_stop, False, "danger")
        self.status.configure(text="Menghentikan setelah proses saat ini selesai...")

    def _start(self, force_translate=False):
        if not self.files:
            self.status.configure(text="⚠️ Pilih minimal satu file audio/video atau tambahkan link dulu.")
            return
        cfg = dict(
            model=MODEL_CHOICES.get(self.model_box.get(), MODEL_NAME),
            lang=LANGUAGE_MAP.get(self.lang_box.get()),
            lang_name=self.lang_box.get(),
            mode=self.mode_var.get(),
            prompt=self.prompt_entry.get().strip() or None,
            separate=bool(self.sep_switch.get()) and self.has_demucs,
            lrc=bool(self.lrc_switch.get()),
            translate=(bool(self.tr_switch.get()) or force_translate) and self._engine_ready(),
            engine=self._engine_key(),
            speed=self.speed_box.get(),
            boost=bool(self.boost_switch.get()),
            target_name=self.tr_box.get(),
        )
        self._save_settings()
        self.item_state = {}
        self.stop_event.clear()
        self.results = []
        self._set_progress(0)
        self._reset_results()
        self._set_running(True)
        threading.Thread(target=self._worker, args=(list(self.files), cfg), daemon=True).start()

    # ------------------------------------------------------------- Worker Transkrip
    def _boost_audio(self, path):
        """Normalisasi volume dinamis ke WAV 16 kHz mono. Mengembalikan path hasil, atau None jika gagal."""
        os.makedirs(TMP_DIR, exist_ok=True)
        out = os.path.join(TMP_DIR, "boosted.wav")
        cmd = [get_ffmpeg(), "-y", "-i", path, "-vn", "-af", BOOST_FILTER, "-ar", "16000", "-ac", "1", out]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", creationflags=flags)
        if proc.returncode != 0 or not os.path.exists(out):
            tail = (proc.stderr or "").strip().splitlines()[-2:]
            self.q.put(("log", "⚠️ Penguatan suara gagal, memakai audio asli. " + " | ".join(tail)))
            return None
        return out

    def _separate_vocals(self, path):
        os.makedirs(TMP_DIR, exist_ok=True)
        args = ["-n", "htdemucs", "--two-stems=vocals", "-d", self.device, "-o", TMP_DIR, path]
        try:
            # Dipanggil langsung di dalam proses ini. `sys.executable -m demucs.separate` TIDAK bisa
            # dipakai di .exe karena sys.executable adalah aplikasi ini sendiri.
            import demucs.separate as demucs_separate
            demucs_separate.main(args)
        except SystemExit as e:
            if e.code not in (0, None):
                self.q.put(("log", f"⚠️ Demucs gagal (kode {e.code}), memakai audio asli."))
                return None
        except Exception as e:
            self.q.put(("log", f"⚠️ Demucs gagal, memakai audio asli. {type(e).__name__}: {e}"))
            return None
        finally:
            gc.collect()
            if self.device == "cuda":
                torch.cuda.empty_cache()
        stem = os.path.splitext(os.path.basename(path))[0]
        vocals = os.path.join(TMP_DIR, "htdemucs", stem, "vocals.wav")
        return vocals if os.path.exists(vocals) else None

    def _worker(self, files, cfg):
        q = self.q
        total = len(files)
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)

            if self.model is None or self.model_name != cfg["model"]:
                self.model = None
                gc.collect()
                if self.device == "cuda":
                    torch.cuda.empty_cache()
                q.put(("status", f"Memuat model '{cfg['model']}'..."))
                q.put(("log", f"⏳ Memuat model '{cfg['model']}' di {self.device.upper()} "
                              "(unduhan pertama bisa memakan waktu)..."))
                root = MODEL_DIR
                try:
                    bundled = os.path.join(BUNDLED_MODEL_DIR, os.path.basename(whisper._MODELS[cfg["model"]]))
                    if os.path.exists(bundled):
                        root = BUNDLED_MODEL_DIR
                except Exception:
                    pass
                self.model = whisper.load_model(cfg["model"], device=self.device, download_root=root)
                self.model_name = cfg["model"]

            try:
                model_dev = str(next(self.model.parameters()).device)
            except Exception:
                model_dev = self.device
            q.put(("log", f"🧠 Model '{self.model_name}' berjalan di perangkat: {model_dev}"))

            q.put(("log", f"🚀 {total} item | Mode: {cfg['mode']} | Bahasa: {cfg['lang_name']} | "
                          f"Vokal terpisah: {'ya' if cfg['separate'] else 'tidak'} | "
                          f"Perkuat suara: {'ya' if cfg['boost'] else 'tidak'} | "
                          f"Terjemahan: {cfg['target_name'] if cfg['translate'] else 'tidak'}"))

            params = dict(MODES[cfg["mode"]]["params"])
            params.update(SPEED_CHOICES.get(cfg["speed"], {}))
            if cfg["boost"]:
                # Whisper melewati segmen jika peluang "bukan ucapan" melebihi ambang ini. Dinaikkan agar
                # ucapan pelan tidak ikut terbuang (hanya yang hampir pasti sunyi yang dilewati).
                params["no_speech_threshold"] = 0.95
            q.put(("log", f"⚙️ Kecepatan/akurasi: {cfg['speed']}"))

            for i, path in enumerate(files):
                if self.stop_event.is_set():
                    q.put(("log", "⏹ Dihentikan oleh pengguna."))
                    break

                item = path
                name = item
                q.put(("item", item, "run"))
                base = i / total
                span = 1 / total
                q.put(("progress", base))
                audio = path
                tstart = 0.0   # porsi progress yang sudah terpakai tahap sebelumnya

                try:
                    # Link video -> unduh audio dulu
                    if is_url(item):
                        q.put(("status", f"[{i + 1}/{total}] Mengunduh audio dari link..."))
                        q.put(("log", f"⬇️ [{i + 1}/{total}] Mengunduh: {item}"))
                        path = self._download_audio(
                            item, on_progress=lambda f, b=base, sp=span: q.put(("progress", b + sp * 0.15 * f)))
                        audio = path
                        tstart = 0.15
                        q.put(("log", f"✅ Terunduh: {os.path.basename(path)}"))
                        q.put(("progress", base + span * 0.15))
                    name = os.path.basename(path)

                    if cfg["separate"]:
                        q.put(("status", f"[{i + 1}/{total}] Memisahkan vokal: {name}"))
                        q.put(("log", f"🎙️ [{i + 1}/{total}] Memisahkan vokal: {name} "
                                      f"(Demucs di {self.device.upper()})"))
                        t_sep = time.time()
                        vocals = self._separate_vocals(path)
                        q.put(("log", f"⏱️ Pemisahan vokal selesai dalam {fmt_dur(time.time() - t_sep)}"))
                        if vocals:
                            audio = vocals
                        q.put(("progress", base + span * 0.35))
                        tstart = 0.35

                    if cfg["boost"]:
                        q.put(("status", f"[{i + 1}/{total}] Memperkuat suara pelan: {name}"))
                        t_boost = time.time()
                        boosted = self._boost_audio(audio)
                        if boosted:
                            audio = boosted
                            q.put(("log", f"🔊 Volume dinormalisasi dalam {fmt_dur(time.time() - t_boost)}"))

                    q.put(("status", f"[{i + 1}/{total}] Mentranskrip: {name} — 0%"))
                    q.put(("log", f"📝 [{i + 1}/{total}] Mentranskrip: {name}"))
                    tend = 0.9 if cfg["translate"] else 1.0

                    def on_tick(f, b=base, sp=span, a=tstart, z=tend, nm=name, idx=i):
                        q.put(("progress", b + sp * (a + (z - a) * f)))
                        q.put(("status", f"[{idx + 1}/{total}] Mentranskrip: {nm} — {f * 100:.0f}%"))

                    _PROGRESS["cb"] = on_tick
                    t_tr = time.time()
                    try:
                        result = self.model.transcribe(
                            audio,
                            language=cfg["lang"],
                            fp16=(self.device == "cuda"),
                            initial_prompt=cfg["prompt"],
                            verbose=None,
                            **params,
                        )
                    finally:
                        _PROGRESS["cb"] = None
                    q.put(("progress", base + span * tend))
                    el = time.time() - t_tr
                    segs_ = result.get("segments") or []
                    dur_ = segs_[-1]["end"] if segs_ else 0
                    speed_txt = f" (±{dur_ / el:.1f}× kecepatan normal)" if el > 0 and dur_ > 0 else ""
                    q.put(("log", f"⏱️ Transkripsi selesai dalam {fmt_dur(el)} untuk ±{fmt_dur(dur_)} audio"
                                  f"{speed_txt}"))

                    writer = get_writer("all", OUTPUT_DIR)
                    writer(result, path, {"max_line_width": None, "max_line_count": None,
                                          "highlight_words": False})
                    if cfg["lrc"]:
                        write_lrc(result, path, OUTPUT_DIR)

                    lines = [s["text"].strip() for s in result["segments"] if s["text"].strip()]
                    q.put(("result", name, "\n".join(lines)))
                    q.put(("log", f"✅ Selesai: {name}"))

                    entry = {"name": name, "base": os.path.splitext(name)[0], "result": result}
                    self.results.append(entry)

                    if cfg["translate"]:
                        self._translate_entry(
                            entry, cfg["target_name"], cfg["lrc"], prefix=f"[{i + 1}/{total}] ",
                            engine=cfg["engine"],
                            on_progress=lambda f, b=base, sp=span: q.put(("progress", b + sp * (0.9 + 0.1 * f))))
                    q.put(("item", item, "done"))
                except DownloadCancelled:
                    q.put(("item", item, "fail"))
                    q.put(("log", "⏹ Dibatalkan oleh pengguna."))
                    break
                except Exception as e:
                    q.put(("item", item, "fail"))
                    q.put(("log", f"❌ Error pada {name}: {e}"))
                    if "out of memory" in str(e).lower():
                        q.put(("log", "💡 VRAM GPU habis. Pilih model lebih kecil di “Opsi lanjutan”, "
                                      "atau matikan “Pisahkan vokal”, lalu coba lagi."))
                finally:
                    shutil.rmtree(TMP_DIR, ignore_errors=True)
                    shutil.rmtree(DL_DIR, ignore_errors=True)

                q.put(("progress", base + span))

            q.put(("log", f"🎉 Selesai! Hasil tersimpan di: {OUTPUT_DIR}"))
            if self.results and not cfg["translate"]:
                q.put(("log", "💡 Ingin versi terjemahan? Pilih bahasa lalu klik “🌐 Terjemahkan & Ekspor”."))
        except Exception as e:
            q.put(("log", f"❌ Error: {e}"))
            if "large-v3-turbo" in str(e) or "not found" in str(e).lower():
                q.put(("log", "Coba perbarui Whisper: pip install -U openai-whisper"))
        finally:
            q.put(("done",))

    # ------------------------------------------------------------- Unduh audio dari link
    def _download_audio(self, url, on_progress=None):
        import yt_dlp

        os.makedirs(DL_DIR, exist_ok=True)
        q = self.q

        def hook(d):
            if self.stop_event.is_set():
                raise DownloadCancelled()
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    frac = min(1.0, d.get("downloaded_bytes", 0) / total)
                    q.put(("status", f"Mengunduh audio... {frac * 100:.0f}%"))
                    if on_progress:
                        on_progress(frac)

        opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(DL_DIR, "%(title).120s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "windowsfilenames": True,
            "ffmpeg_location": ffmpeg_path,
            "progress_hooks": [hook],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and info.get("entries"):
                info = next(e for e in info["entries"] if e)
            path = ydl.prepare_filename(info)

        if not os.path.exists(path):
            raise FileNotFoundError("File hasil unduhan tidak ditemukan.")
        return path

    # ------------------------------------------------------------- Terjemahan & Ekspor
    def _translate_entry(self, entry, target_name, with_lrc, prefix="", on_progress=None, engine="google"):
        q = self.q
        code = TRANSLATE_LANGS[target_name]
        argos_target = None

        if engine == "argos":
            argos_target = ARGOS_LANGS[target_name]
            src = entry["result"].get("language")
            if src not in ARGOS_SOURCES:
                raise RuntimeError(
                    f"Bahasa sumber “{src}” belum didukung mode offline "
                    f"(didukung: {', '.join(sorted(ARGOS_SOURCES))}). Gunakan mesin Google.")
            if src == argos_target:
                q.put(("log", "ℹ️ Bahasa sumber sama dengan bahasa tujuan; teks disalin tanpa diterjemahkan."))
            pairs = argos_needed_packages(src, argos_target)
            if pairs:
                q.put(("status", f"{prefix}Memeriksa paket bahasa offline..."))
                install_argos_pairs(pairs, log=lambda t: q.put(("log", t)), should_stop=self.stop_event.is_set)

        label = "offline" if engine == "argos" else "Google"
        q.put(("status", f"{prefix}Menerjemahkan ke {target_name} ({label}): {entry['name']}"))
        q.put(("log", f"🌐 {prefix}Menerjemahkan ke {target_name} ({label}): {entry['name']}"))
        text, files, failed = export_translated(
            entry["result"], entry["base"], code, with_lrc, on_progress=on_progress,
            engine=engine, argos_target=argos_target, should_stop=self.stop_event.is_set)
        if failed:
            q.put(("log", f"⚠️ {failed} segmen gagal diterjemahkan (dibiarkan dalam bahasa asli)."))
        q.put(("result", f"{entry['name']} → {target_name}", text))
        q.put(("log", f"✅ Terjemahan diekspor: {', '.join(files)}"))

    def _install_argos_packages(self):
        if not self.has_argos:
            return
        try:
            installed = argos_installed_pairs()
        except Exception as e:
            self.status.configure(text=f"⚠️ Argos tidak dapat dibaca: {e}")
            return
        missing = [p for p in all_argos_pairs() if p not in installed]
        if not missing:
            self.status.configure(text="✅ Semua paket bahasa offline sudah terpasang.")
            return
        n = len(missing)
        ok = messagebox.askyesno(
            "Pasang paket bahasa offline",
            f"Akan mengunduh {n} paket (Inggris ↔ Indonesia, Korea, Jepang, Mandarin, Rusia).\n\n"
            f"Perkiraan ukuran: ±{n * 50}–{n * 100} MB. Butuh internet, hanya sekali.\n\nLanjutkan?")
        if not ok:
            return
        self.stop_event.clear()
        self._set_progress(0)
        self._show_tab(TAB_LOG)
        self._set_running(True)
        threading.Thread(target=self._worker_install_argos, args=(all_argos_pairs(),), daemon=True).start()

    def _worker_install_argos(self, pairs):
        q = self.q
        try:
            n = install_argos_pairs(pairs, log=lambda t: q.put(("log", t)),
                                    on_progress=lambda f: q.put(("progress", f)),
                                    should_stop=self.stop_event.is_set)
            q.put(("log", f"🎉 Selesai! {n} paket bahasa offline terpasang."))
            q.put(("done", "✅ Paket bahasa offline siap dipakai."))
        except DownloadCancelled:
            q.put(("log", "⏹ Dibatalkan oleh pengguna."))
            q.put(("done",))
        except Exception as e:
            q.put(("log", f"❌ Gagal memasang paket: {e}"))
            q.put(("log", "💡 Pastikan terhubung ke internet, lalu coba lagi."))
            q.put(("done", "⚠️ Pemasangan paket gagal. Lihat tab Log."))

    def _translate_last(self):
        if not self._engine_ready():
            msg = ("Argos Translate belum terpasang. Jalankan: pip install argostranslate lalu buka ulang aplikasi."
                   if self._engine_key() == "argos" else
                   "deep-translator belum terpasang. Jalankan: pip install deep-translator lalu buka ulang aplikasi.")
            self.status.configure(text="⚠️ " + msg)
            self._log("⚠️ " + msg)
            self._show_tab(TAB_LOG)
            return
        if not self.results:
            if self.files:
                # Belum ada hasil, tapi ada file/link di daftar: transkrip dulu, lalu terjemahkan.
                self._log("ℹ️ Belum ada hasil transkrip, jadi file di daftar akan ditranskrip "
                          "lalu otomatis diterjemahkan.")
                self._show_tab(TAB_LOG)
                self._start(force_translate=True)
            else:
                msg = "Pilih file atau tempel link dulu, lalu klik Terjemahkan & Ekspor."
                self.status.configure(text="⚠️ " + msg)
                self._log("⚠️ " + msg)
                self._show_tab(TAB_LOG)
            return
        target = self.tr_box.get()
        self.stop_event.clear()
        self._set_progress(0)
        self._show_tab(TAB_LOG)
        self._set_running(True)
        threading.Thread(target=self._worker_translate,
                         args=(list(self.results), target, bool(self.lrc_switch.get()), self._engine_key()),
                         daemon=True).start()

    def _worker_translate(self, entries, target, with_lrc, engine="google"):
        q = self.q
        total = len(entries)
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            for i, entry in enumerate(entries):
                if self.stop_event.is_set():
                    q.put(("log", "⏹ Terjemahan dihentikan oleh pengguna."))
                    break
                self._translate_entry(
                    entry, target, with_lrc, prefix=f"[{i + 1}/{total}] ", engine=engine,
                    on_progress=lambda f, i=i: q.put(("progress", (i + f) / total)))
                q.put(("progress", (i + 1) / total))
            q.put(("log", f"🎉 Selesai! Terjemahan tersimpan di: {OUTPUT_DIR}"))
        except DownloadCancelled:
            q.put(("log", "⏹ Dibatalkan oleh pengguna."))
        except Exception as e:
            q.put(("log", f"❌ Error terjemahan: {e}"))
        finally:
            q.put(("done",))

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "status":
                    self.status.configure(text=msg[1])
                elif kind == "progress":
                    self._set_progress(min(1.0, msg[1]))
                elif kind == "item":
                    self._set_item_state(msg[1], msg[2])
                elif kind == "result":
                    _, name, text = msg
                    if self._result_empty:
                        self.result_box.delete("1.0", "end")
                        self._result_empty = False
                    self._result_add(f"━━ {name} ━━\n", "title")
                    self._result_add(f"{text}\n\n")
                    self.result_box.see("end")
                    self._show_tab(TAB_RESULT)
                elif kind == "done":
                    custom = msg[1] if len(msg) > 1 else None
                    if self.stop_event.is_set():
                        self.status.configure(text="⏹ Dihentikan oleh pengguna.")
                    else:
                        if custom is None or custom.startswith("✅"):
                            self._set_progress(1.0)
                        self.status.configure(
                            text=custom or "✅ Selesai! Klik “Folder Hasil” untuk membuka file.")
                    self._set_running(False)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


def run_selftest(report_path):
    """Dipakai di CI: memastikan semua komponen yang dikemas benar-benar bisa dipakai. Mengembalikan kode keluar."""
    lines, failed = [], []

    def check(name, fn):
        try:
            detail = fn()
            lines.append(f"OK    {name}" + (f" -> {detail}" if detail else ""))
        except BaseException as e:   # noqa: BLE001
            failed.append(name)
            lines.append(f"FAIL  {name}: {type(e).__name__}: {e}")

    def imp(mod):
        return lambda: (importlib.import_module(mod), None)[1]

    def t_ffmpeg():
        out = subprocess.run([get_ffmpeg(), "-version"], capture_output=True, text=True)
        assert out.returncode == 0, "ffmpeg tidak bisa dijalankan"
        return out.stdout.splitlines()[0]

    def t_ffprobe():
        out = subprocess.run([os.path.join(ffmpeg_path, "ffprobe.exe"), "-version"], capture_output=True, text=True)
        assert out.returncode == 0, "ffprobe tidak bisa dijalankan"

    def t_whisper_assets():
        import whisper.audio
        whisper.audio.mel_filters("cpu", 80)
        whisper.tokenizer.get_tokenizer(True)

    def t_numba():
        from whisper.timing import dtw
        dtw(torch.randn(8, 12))

    def t_whisper_model():
        fn = os.path.basename(whisper._MODELS[MODEL_NAME])
        if os.path.isdir(BUNDLED_MODEL_DIR) and os.listdir(BUNDLED_MODEL_DIR):
            assert os.path.exists(os.path.join(BUNDLED_MODEL_DIR, fn)), f"{fn} tidak ada di paket"
            return fn
        return "tidak dikemas (akan diunduh saat pertama dipakai)"

    def t_demucs_weights():
        if not os.path.isdir(_BUNDLED_TORCH_HOME):
            return "tidak dikemas (akan diunduh saat pertama dipakai)"
        from demucs.pretrained import get_model
        get_model("htdemucs")
        return "htdemucs dimuat dari paket"

    def t_demucs_run():
        import tempfile
        import demucs.separate as dsep
        tmp = tempfile.mkdtemp(prefix="selftest_")
        wav = os.path.join(tmp, "tone.wav")
        gen = subprocess.run([get_ffmpeg(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", wav],
                             capture_output=True, text=True)
        assert gen.returncode == 0 and os.path.exists(wav), "gagal membuat audio uji"
        try:
            dsep.main(["-n", "htdemucs", "--two-stems=vocals", "-d", "cpu", "-o", tmp, wav])
        except SystemExit as e:
            assert e.code in (0, None), f"demucs keluar dengan kode {e.code}"
        assert os.path.exists(os.path.join(tmp, "htdemucs", "tone", "vocals.wav")), "vocals.wav tidak terbentuk"
        shutil.rmtree(tmp, ignore_errors=True)
        return "pemisahan vokal berjalan"

    def t_whisper_run():
        import numpy as np
        fn = os.path.basename(whisper._MODELS[MODEL_NAME])
        if not os.path.exists(os.path.join(BUNDLED_MODEL_DIR, fn)):
            return "dilewati (model tidak dikemas)"
        model = whisper.load_model(MODEL_NAME, device="cpu", download_root=BUNDLED_MODEL_DIR)
        model.transcribe(np.zeros(16000 * 3, dtype=np.float32), word_timestamps=True, fp16=False)
        return f"model {MODEL_NAME} memuat & mentranskrip"

    check("torch", lambda: f"{torch.__version__}, cuda={torch.cuda.is_available()}")
    check("ffmpeg", t_ffmpeg)
    check("ffprobe", t_ffprobe)
    check("whisper aset (mel+tokenizer)", t_whisper_assets)
    check("whisper timing (numba)", t_numba)
    check("model whisper bawaan", t_whisper_model)
    for mod in ("demucs.separate", "demucs.htdemucs", "yt_dlp", "deep_translator",
                "argostranslate.package", "argostranslate.translate", "tkinterdnd2", "customtkinter"):
        check("impor " + mod, imp(mod))
    check("bobot demucs", t_demucs_weights)
    check("demucs jalan nyata", t_demucs_run)
    check("whisper jalan nyata", t_whisper_run)

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    if "--selftest" in sys.argv:
        _idx = sys.argv.index("--selftest")
        _report = sys.argv[_idx + 1] if len(sys.argv) > _idx + 1 else os.path.join(DATA_DIR, "selftest.txt")
        sys.exit(run_selftest(_report))
    App().mainloop()