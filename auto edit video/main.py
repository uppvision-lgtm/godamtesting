#!/usr/bin/env python3
"""
Auto-edit video vertikal (gaya CapCut / lower-third berita TV).

Alur kerja:
  1. Transkripsi audio `sumber.mp4` memakai mlx-whisper  -> hasil_video/transcript.json
  2. Ringkas isi video jadi 3 baris hook memakai LLM lokal
     (Ollama, model vision) yang membaca beberapa frame + transkrip
                                                          -> hasil_video/visual_summary.json
  3. Render 3 baris hook itu jadi PNG transparan           -> assets/hook_text.png
  4. Gabungkan semua layer + outro dengan satu perintah FFmpeg
                                                          -> hasil_video/output_final.mp4

Semua path dihitung relatif terhadap direktori kerja saat script dijalankan.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


# ============================================================================
# KONFIGURASI  (ubah bagian ini kalau mau menyesuaikan tampilan)
# ============================================================================

BASE_DIR = Path(os.getcwd())
ASSETS_DIR = BASE_DIR / "assets"
OUTPUT_DIR = BASE_DIR / "hasil_video"

# --- Berkas sumber. Urutan dict = urutan input "-i" ke FFmpeg (index 0..5) ---
FILES = {
    "sumber": BASE_DIR / "sumber.mp4",           # [0] video utama + audio
    "layer4": BASE_DIR / "layer4.mov",           # [1] overlay kecil (mis. logo animasi)
    "layer3": BASE_DIR / "layer3.png",           # [2] panel bawah (banner besar)
    "layer2": BASE_DIR / "layer2.png",           # [3] kotak lower-third
    "layer1": BASE_DIR / "layer1-teratas.png",   # [4] bingkai paling atas
    "outro":  BASE_DIR / "outro.mp4",            # [5] video penutup
}
HOOK_PNG = ASSETS_DIR / "hook_text.png"          # [6] teks hook (dibuat script ini)
FONT_PATH = ASSETS_DIR / "Poppins-Bold.ttf"

OUTPUT_VIDEO = OUTPUT_DIR / "output_final.mp4"
TRANSCRIPT_JSON = OUTPUT_DIR / "transcript.json"
SUMMARY_JSON = OUTPUT_DIR / "visual_summary.json"

# --- Ukuran kanvas video (vertikal 9:16) ---
VIDEO_WIDTH = 720
VIDEO_HEIGHT = 1280

# --- Penempatan tiap layer di atas video ---
HOOK_DURATION = 3                 # detik: lama layer3/layer2/hook tampil di awal
LAYER4_SIZE = (280, 158)          # layer4.mov di-scale ke ukuran ini
LAYER4_POS = (45, 55)             # posisi x, y layer4
LAYER3_CROP = "2880:1197:40:0"    # crop w:h:x:y sebelum di-scale
LAYER3_SIZE = (720, 429)          # layer3 setelah crop
LAYER2_SIZE = (700, 283)          # layer2 + kanvas teks hook
LAYER2_POS = (10, 710)            # posisi x, y layer2 dan teks hook

# --- Gaya teks hook (dirender dengan Pillow ke hook_text.png) ---
HOOK_CANVAS = LAYER2_SIZE         # kanvas PNG transparan 700x283
FONT_SIZE = 27
TEXT_X = 28                       # titik kiri teks di dalam kanvas
TEXT_Y = 82                       # titik atas baris pertama
LINE_GAP = 53                     # jarak vertikal antar baris
COLOR_LINE_1 = (211, 45, 39, 255)     # merah  -> baris 1 (headline)
COLOR_LINE_REST = (0, 0, 0, 255)      # hitam  -> baris 2 & 3
MAX_CHARS = 36                    # batas karakter per baris (dipotong keras)
TOTAL_LINES = 3

# --- Model AI ---
WHISPER_MODEL = "mlx-community/whisper-small-mlx"
WHISPER_LANGUAGE = "id"
VISION_MODEL = "qwen2.5vl:3b"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_TIMEOUT = 300

# --- Pengambilan frame untuk dianalisis LLM ---
FRAME_FPS = "1/10"                # 1 frame tiap 10 detik
FRAME_WIDTH = 384                 # scale=384:-2
MAX_FRAMES = 8
FRAME_QUALITY = 5                 # -q:v (1 = terbaik)

# Naikkan angka ini kalau PROMPT_TEMPLATE / gaya hook diubah,
# supaya visual_summary.json lama otomatis dianggap kedaluwarsa.
SUMMARY_STYLE_VERSION = 5

# --- Encoder ---
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "fast"
VIDEO_CRF = "22"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"

PROMPT_TEMPLATE = (
    "Anda adalah redaktur grafis berita televisi Indonesia. "
    "Analisis semua frame dan transkrip dubbing berikut, lalu tulis lower-third berita "
    "seperti kanal berita televisi: singkat, tegas, informatif, dan mudah dipindai. "
    "Jangan menyalin ucapan mentah dan jangan membuat opini atau fakta baru. "
    "Jawab tepat 3 baris tanpa nomor, tanda kutip, bullet, atau label. "
    "Baris 1 adalah headline utama yang menarik (4-8 kata). "
    "Baris 2 adalah keterangan faktual tentang isi video. "
    "Baris 3 adalah dampak, tips, atau pesan penting. "
    "Semua huruf kapital, maksimal 36 karakter per baris. "
    "Jangan menulis label seperti HEADLINE, KETERANGAN, DAMPAK, atau PESAN "
    "dan jangan memakai markdown. "
    "Balas hanya JSON valid dengan tiga field string: headline, detail, message."
    "\n\nTRANSKRIP:\n{transkrip}"
)

# Kata-kata label yang sering ikut terbawa di awal jawaban LLM; dibuang.
LABEL_PREFIX_RE = re.compile(
    r"^(HEADLINE|KETERANGAN|DAMPAK|PESAN|UTAMA|FAKTUAL|RINGKASAN|TIPS)\s*[:\-*]*\s*",
    re.IGNORECASE,
)
LEADING_JUNK_RE = re.compile(r"^[\s\-*\d.]+")    # spasi, bullet, "1.", "2. 5 " dst.
TRAILING_PUNCT_RE = re.compile(r"[\s.!?]+$")


# ============================================================================
# UTILITAS KECIL
# ============================================================================

def baca_json(path: Path):
    """Baca file JSON; kembalikan None kalau tidak ada atau rusak."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def tulis_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def kunci_sumber(video: Path) -> str:
    """
    Sidik jari file sumber = "<ukuran>:<mtime_ns>".
    Dipakai agar transkrip/ringkasan otomatis dibuat ulang saat sumber.mp4 berubah.
    """
    stat = video.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def cek_file(daftar) -> bool:
    """Pastikan semua file ada. Berhenti di file pertama yang hilang."""
    for path in daftar:
        if not Path(path).exists():
            print(f"❌ File tidak ditemukan: {path}")
            return False
    return True


def get_duration(video: Path) -> float:
    """Ambil durasi video (detik) lewat ffprobe."""
    hasil = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(video),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(hasil.stdout)["format"]["duration"])


# ============================================================================
# LANGKAH 1 — TRANSKRIPSI
# ============================================================================

def transkripsi_video(video: Path) -> list[str]:
    """
    Kembalikan daftar kalimat hasil Whisper.
    Hasil disimpan di transcript.json dan dipakai ulang selama sumber.mp4 tidak berubah.
    """
    key = kunci_sumber(video)
    cache = baca_json(TRANSCRIPT_JSON)
    if cache and cache.get("source_key") == key:
        print("📝 Memakai transkrip yang tersimpan...")
        return cache.get("segments", [])

    try:
        import mlx_whisper
    except ImportError:
        print("❌ Engine mlx-whisper tidak tersedia pada Python yang aktif.")
        print(f"   Pasang dulu: {sys.executable} -m pip install mlx-whisper")
        return []

    print("🎙️ Membaca isi video dengan Whisper...")
    hasil = mlx_whisper.transcribe(
        str(video),
        path_or_hf_repo=WHISPER_MODEL,
        language=WHISPER_LANGUAGE,
        verbose=False,
    )

    segments = []
    for seg in hasil.get("segments", []):
        teks = (seg.get("text") or "").strip()
        if teks:
            segments.append(teks)

    tulis_json(TRANSCRIPT_JSON, {"source_key": key, "segments": segments})
    return segments


# ============================================================================
# LANGKAH 2 — RINGKASAN VISUAL (LLM)
# ============================================================================

def ambil_frame_video(video: Path) -> list[str]:
    """
    Ambil sampel frame (1 frame / 10 detik, maks MAX_FRAMES) sebagai JPEG,
    lalu kembalikan dalam bentuk base64 untuk dikirim ke model vision.
    """
    with tempfile.TemporaryDirectory(prefix="capcut-frames-") as frame_dir:
        pola = os.path.join(frame_dir, "frame-%02d.jpg")
        subprocess.run(
            [
                "ffmpeg", "-loglevel", "error",
                "-i", str(video),
                "-vf", f"fps={FRAME_FPS},scale={FRAME_WIDTH}:-2",
                "-frames:v", str(MAX_FRAMES),
                "-q:v", str(FRAME_QUALITY),
                pola, "-y",
            ],
            check=True,
        )
        berkas = sorted(Path(frame_dir).glob("frame-*.jpg"))
        return [base64.b64encode(p.read_bytes()).decode("ascii") for p in berkas]


def tanya_llm(prompt: str, gambar_b64: list[str]) -> str:
    """Kirim prompt + frame ke Ollama. Kembalikan string balasan ('' bila gagal)."""
    payload = json.dumps({
        "model": VISION_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "images": gambar_b64,
    }).encode("utf-8")

    request = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("response", "")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        print(f"⚠️  Gagal menghubungi Ollama ({e}); memakai ringkasan cadangan.")
        return ""


def ambil_json_pertama(teks: str):
    """Ambil objek JSON pertama di dalam teks balasan LLM."""
    if not teks:
        return None
    try:
        return json.loads(teks)
    except json.JSONDecodeError:
        pass
    cocok = re.search(r"\{.*\}", teks, re.DOTALL)
    if not cocok:
        return None
    try:
        return json.loads(cocok.group(0))
    except json.JSONDecodeError:
        return None


def bersihkan_baris(teks: str) -> str:
    """Rapikan satu baris hook: buang markdown/bullet/label, kapitalkan, potong 36 karakter."""
    teks = (teks or "").replace("**", "")
    teks = LEADING_JUNK_RE.sub("", teks)
    teks = LABEL_PREFIX_RE.sub("", teks)
    teks = TRAILING_PUNCT_RE.sub("", teks)
    return teks.upper()[:MAX_CHARS]


def normalisasi_summary(data) -> list[str]:
    """Ubah balasan JSON LLM jadi tepat 3 baris bersih. Kembalikan [] kalau tidak layak."""
    if not isinstance(data, dict):
        return []
    mentah = [data.get("headline", ""), data.get("detail", ""), data.get("message", "")]
    baris = [bersihkan_baris(t) for t in mentah]
    if not all(baris):
        return []
    return baris[:TOTAL_LINES]


def ringkasan_dari_transkrip(segments: list[str]) -> list[str]:
    """
    Rencana cadangan bila LLM tidak tersedia / balasannya tidak valid:
    ambil 3 kalimat pertama transkrip, buang tanda baca, kapitalkan, potong 36 karakter.
    """
    teks = " ".join(segments)
    kalimat = [k.strip() for k in re.split(r"[.!?]", teks) if k.strip()]
    hasil = []
    for k in kalimat[:TOTAL_LINES]:
        bersih = re.sub(r"[^A-Za-z0-9 ]", " ", k)
        bersih = re.sub(r"\s+", " ", bersih).strip().upper()
        if bersih:
            hasil.append(bersih[:MAX_CHARS])
    return hasil


def simpulkan_video(video: Path, segments: list[str]) -> list[str]:
    """
    Hasilkan 3 baris hook. Urutan usaha:
      1. cache visual_summary.json (kalau sumber & style version masih sama)
      2. model vision lewat Ollama
      3. cadangan dari transkrip
    """
    key = kunci_sumber(video)
    cache = baca_json(SUMMARY_JSON)
    if (
        cache
        and cache.get("source_key") == key
        and cache.get("style_version") == SUMMARY_STYLE_VERSION
        and cache.get("summary")
    ):
        print("📝 Memakai kesimpulan visual yang tersimpan...")
        return cache["summary"]

    baris: list[str] = []
    if segments:
        prompt = PROMPT_TEMPLATE.format(transkrip=" ".join(segments))
        try:
            frames = ambil_frame_video(video)
        except subprocess.CalledProcessError:
            frames = []
        baris = normalisasi_summary(ambil_json_pertama(tanya_llm(prompt, frames)))

    if not baris:
        baris = ringkasan_dari_transkrip(segments)
    if not baris:
        return []

    tulis_json(SUMMARY_JSON, {
        "source_key": key,
        "style_version": SUMMARY_STYLE_VERSION,
        "summary": baris,
    })
    return baris


def buat_hook(video: Path, segments: list[str]) -> list[str]:
    """Ambil 3 baris hook lalu tampilkan ke layar."""
    baris = simpulkan_video(video, segments)
    if baris:
        print("📰 Hook otomatis:")
        for b in baris:
            print(f"   {b}")
    return baris


# ============================================================================
# LANGKAH 3 — RENDER TEKS HOOK JADI PNG
# ============================================================================

def buat_layer_teks(baris: list[str]) -> bool:
    """Gambar baris hook ke assets/hook_text.png (PNG transparan 700x283)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("❌ Pillow belum terpasang.")
        print(f"   Pasang dulu: {sys.executable} -m pip install Pillow")
        return False

    if not FONT_PATH.exists():
        print(f"⚠️  Font {FONT_PATH.name} tidak ditemukan, memakai font bawaan.")
        font = ImageFont.load_default()
    else:
        font = ImageFont.truetype(str(FONT_PATH), FONT_SIZE)

    kanvas = Image.new("RGBA", HOOK_CANVAS, (0, 0, 0, 0))
    draw = ImageDraw.Draw(kanvas)
    for i, teks in enumerate(baris[:TOTAL_LINES]):
        warna = COLOR_LINE_1 if i == 0 else COLOR_LINE_REST
        draw.text((TEXT_X, TEXT_Y + i * LINE_GAP), teks, font=font, fill=warna)

    HOOK_PNG.parent.mkdir(parents=True, exist_ok=True)
    kanvas.save(HOOK_PNG)
    return True


# ============================================================================
# LANGKAH 4 — SUSUN & JALANKAN FFMPEG
# ============================================================================

def bangun_filter_complex() -> str:
    """
    Susun filter_complex FFmpeg.

    Input: [0]=sumber [1]=layer4 [2]=layer3 [3]=layer2 [4]=layer1 [5]=outro [6]=hook

    Urutan menumpuk (bawah -> atas):
        sumber -> layer3 -> layer2 -> teks hook -> layer4 -> layer1
    layer3, layer2 dan teks hook hanya muncul pada detik 0..HOOK_DURATION.
    Terakhir video utama disambung dengan outro memakai filter concat.
    """
    w, h = VIDEO_WIDTH, VIDEO_HEIGHT
    l4w, l4h = LAYER4_SIZE
    l3w, l3h = LAYER3_SIZE
    l2w, l2h = LAYER2_SIZE
    l4x, l4y = LAYER4_POS
    l2x, l2y = LAYER2_POS
    tampil = f"enable='between(t,0,{HOOK_DURATION})'"

    bagian = [
        # --- siapkan tiap sumber ---
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setpts=PTS-STARTPTS[src]",

        f"[1:v]scale={l4w}:{l4h}:force_original_aspect_ratio=decrease,"
        f"format=rgba,setpts=PTS-STARTPTS[ov4]",

        f"[2:v]crop={LAYER3_CROP},scale={l3w}:{l3h},"
        f"format=rgba,setpts=PTS-STARTPTS[img3]",

        f"[3:v]scale={l2w}:{l2h}:force_original_aspect_ratio=decrease,"
        f"format=rgba,setpts=PTS-STARTPTS[img2]",

        "[6:v]format=rgba,setpts=PTS-STARTPTS[hook]",

        f"[4:v]scale={w}:-1:force_original_aspect_ratio=decrease,"
        f"format=rgba,setpts=PTS-STARTPTS[img1]",

        # --- tumpuk layer di atas video utama ---
        f"[src][img3]overlay=format=auto:x=0:y=main_h-h:{tampil}[tmp1]",
        f"[tmp1][img2]overlay=format=auto:x={l2x}:y={l2y}:{tampil}[tmp2]",
        f"[tmp2][hook]overlay=format=auto:x={l2x}:y={l2y}:{tampil}[tmp3]",
        f"[tmp3][ov4]overlay=format=auto:x={l4x}:y={l4y}[tmp4]",
        "[tmp4][img1]overlay=format=auto:x=0:y=main_h-h[main_v]",
        "[0:a]asetpts=PTS-STARTPTS[main_a]",

        # --- siapkan outro lalu sambung ---
        f"[5:v]setpts=PTS-STARTPTS,scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h}[out_v]",
        "[5:a]asetpts=PTS-STARTPTS[out_a]",
        "[main_v][main_a][out_v][out_a]concat=n=2:v=1:a=1[final_v][final_a]",
    ]
    return "; ".join(bagian)


def bangun_perintah_ffmpeg() -> list[str]:
    cmd = ["ffmpeg"]
    for path in FILES.values():          # input [0] .. [5]
        cmd += ["-i", str(path)]
    cmd += ["-i", str(HOOK_PNG)]         # input [6]
    cmd += [
        "-filter_complex", bangun_filter_complex(),
        "-map", "[final_v]",
        "-map", "[final_a]",
        "-c:v", VIDEO_CODEC,
        "-preset", VIDEO_PRESET,
        "-crf", VIDEO_CRF,
        "-c:a", AUDIO_CODEC,
        "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(OUTPUT_VIDEO),
        "-y",
    ]
    return cmd


# ============================================================================
# ALUR UTAMA
# ============================================================================

def main() -> int:
    berkas_sumber = list(FILES.values())

    # Cek dulu semua bahan sebelum kerja berat.
    if not cek_file(berkas_sumber):
        return 1

    segments = transkripsi_video(FILES["sumber"])
    baris_hook = buat_hook(FILES["sumber"], segments)

    if not baris_hook:
        print("❌ Tidak ada teks hook yang bisa dibuat.")
        return 1
    if not buat_layer_teks(baris_hook):
        return 1

    if not cek_file(berkas_sumber + [HOOK_PNG]):
        return 1
    print("✅ Semua file ditemukan. Memulai proses rendering...")

    durasi_sumber = get_duration(FILES["sumber"])
    durasi_outro = get_duration(FILES["outro"])
    print(f"📹 Durasi sumber: {durasi_sumber} detik")
    print(f"📹 Durasi outro: {durasi_outro} detik")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = bangun_perintah_ffmpeg()
    print("\n🚀 Menjalankan FFmpeg...")
    print(" ".join(cmd))

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ FFmpeg gagal (exit code {e.returncode}).")
        return 1

    print(f"✅ Berhasil! Video final tersimpan di: {OUTPUT_VIDEO}")
    print(f"📁 Durasi total: {durasi_sumber + durasi_outro} detik")
    return 0


if __name__ == "__main__":
    sys.exit(main())
