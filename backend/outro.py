"""Auto Outro: dari isian nama channel + akun sosmed sampai video outro.

Tiga langkah, semuanya lewat API - tidak ada peramban, tidak ada login:

1. AI (DeepSeek) menulis prompt VISUAL untuk pembuat video: suasana, gerakan,
   pencahayaan. Sengaja tanpa username - lihat alasannya di bawah.
2. LTX-Video di Hugging Face ZeroGPU membuat latar bergeraknya dari prompt
   itu. Gratis, dipanggil lewat gradio_client, hasilnya berkas mp4.
3. Renderer teks aplikasi ini menempelkan nama channel dan enam handle di
   atasnya - PERSIS, huruf demi huruf.

Kenapa teksnya tidak diserahkan ke AI: diuji langsung, dan semua model video
(termasuk Google Flow) tidak bisa menulis teks dengan tepat - "@tvrakyat"
keluar jadi "Basahetwi". Untuk outro, yang paling penting justru username
yang benar. Jadi AI hanya membuat suasananya, dan teksnya ditempel sendiri
oleh alat yang memang sudah menulis teks pixel-perfect di Auto Edit Video.

Berjalan di utas biasa (bukan Celery): alat developer, satu pekerjaan pada
satu waktu, keadaannya di memori proses.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from video_edit import FFMPEG_BIN, FONT_BUNDLED, MEDIA_DIR, VideoError, probe

logger = logging.getLogger(__name__)

# Kata-kata perintah untuk AI. Yang diminta hanya SUASANA visualnya; username
# sengaja tidak disebut, sebab AI video tidak bisa mengejanya dan teksnya
# ditempel sendiri setelah video jadi. Center dimintai lapang supaya ada
# ruang untuk logo dan teks yang ditempel di atasnya.
PERMINTAAN = """Write a single English prompt for an AI video generator to create a 4-second premium brand outro background.
Media brand theme: {channel}
Requirements for the scene:
- Portrait 9:16, dark elegant background (deep charcoal to black gradient)
- Premium, modern, minimalist motion graphics: soft light sweep, subtle floating particles, gentle glow, smooth camera drift
- Keep the CENTER of the frame calm and mostly empty (a logo and text will be placed there afterward)
- No humans, no real events, no news, no politics, no real-world footage
- No on-screen text, no letters, no words, no logos, no watermark
Output ONLY the prompt text, one paragraph, no explanation."""

# Kalau AI tak terpakai/gagal, prompt bawaan ini sudah cukup bagus sendiri.
PROMPT_BAWAAN = (
    "Premium minimalist brand outro background, portrait 9:16, deep charcoal to "
    "black gradient, soft horizontal light sweep, subtle floating particles, gentle "
    "glow, smooth slow camera drift, elegant cinematic motion graphics, calm empty "
    "center, no text, no letters, no logos, no people"
)

# Urutan akun mengikuti teks perintah.
KUNCI_AKUN = ("instagram", "tiktok", "x", "threads", "facebook", "youtube")
NAMA_TAMPIL = {
    "instagram": "Instagram", "tiktok": "TikTok", "x": "X",
    "threads": "Threads", "facebook": "Facebook", "youtube": "YouTube",
}

# --- penyedia AI penulis prompt (sama seperti bot komentar) ---
AI_KUNCI = os.getenv("OUTRO_AI_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
AI_ALAMAT = (os.getenv("OUTRO_AI_URL", "") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")
AI_MODEL = os.getenv("OUTRO_AI_MODEL", "") or "deepseek-flash"
TUNGGU_AI = float(os.getenv("OUTRO_TUNGGU_AI", "60"))

# --- pembuat video: Hugging Face ZeroGPU Space ---
# Gratis dan tanpa token untuk pemakaian sekali-sekali; token menaikkan jatah
# harian kalau nanti sering dipakai. Space bisa diganti lewat setelan.
HF_SPACE = os.getenv("OUTRO_HF_SPACE", "Lightricks/ltx-video-distilled")
HF_TOKEN = os.getenv("OUTRO_HF_TOKEN", "").strip() or None
VIDEO_W = int(os.getenv("OUTRO_VIDEO_W", "416"))   # kelipatan 32
VIDEO_H = int(os.getenv("OUTRO_VIDEO_H", "704"))
VIDEO_DETIK = float(os.getenv("OUTRO_VIDEO_DETIK", "4"))
TUNGGU_VIDEO = float(os.getenv("OUTRO_TUNGGU_VIDEO", "300"))


class OutroError(Exception):
    """Kegagalan yang pesannya layak dibaca orang."""


class Dibatalkan(Exception):
    """Pekerjaan dihentikan atas permintaan."""


def outro_dir() -> Path:
    return MEDIA_DIR / "outro"


# ============================================================
#  DAFTAR PEKERJAAN (di memori proses)
# ============================================================

_jobs: dict[str, dict[str, Any]] = {}
_batal: dict[str, threading.Event] = {}
_kunci = threading.Lock()
_sedang_jalan = threading.Lock()


def _catat(job_id: str, pesan: str, **ubahan: Any) -> None:
    with _kunci:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.setdefault("logs", []).append(pesan)
        job["logs"] = job["logs"][-100:]
        job["message"] = pesan
        job.update(ubahan)
        job["updated"] = time.time()
    logger.info("outro %s: %s", job_id, pesan)


def baca_job(job_id: str) -> dict[str, Any] | None:
    with _kunci:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def daftar_job(batas: int = 20) -> list[dict[str, Any]]:
    with _kunci:
        semua = sorted(_jobs.values(), key=lambda j: j.get("created", 0), reverse=True)
        return [dict(j) for j in semua[:batas]]


def minta_batal(job_id: str) -> bool:
    ev = _batal.get(job_id)
    if ev is None:
        return False
    ev.set()
    _catat(job_id, "Diminta berhenti; menunggu langkah yang sedang berjalan selesai.")
    return True


def mulai_job(channel: str, akun: dict[str, str]) -> str:
    channel = " ".join(str(channel or "").split())
    if not channel:
        raise OutroError("Nama channel wajib diisi.")
    job_id = uuid.uuid4().hex[:12]
    with _kunci:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "langkah": "antre",
            "progress": 0,
            "channel": channel,
            "akun": {k: str(akun.get(k) or "").strip() for k in KUNCI_AKUN},
            "prompt": "",
            "video": "",
            "message": "Menunggu giliran.",
            "logs": ["Pekerjaan masuk antrean."],
            "created": time.time(),
            "updated": time.time(),
        }
    _batal[job_id] = threading.Event()
    threading.Thread(target=_jalankan, args=(job_id,), name=f"outro-{job_id}", daemon=True).start()
    return job_id


def _jalankan(job_id: str) -> None:
    ev = _batal[job_id]
    folder = outro_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    with _sedang_jalan:
        try:
            job = baca_job(job_id) or {}
            channel = job["channel"]
            akun = job["akun"]

            if ev.is_set():
                raise Dibatalkan("Dihentikan sebelum mulai.")
            _catat(job_id, "Menulis gaya visual lewat AI ...",
                   status="running", langkah="ai", progress=8)
            prompt = tulis_prompt_visual(channel, lambda m: _catat(job_id, m))
            _catat(job_id, "Gaya visual siap.", prompt=prompt, langkah="video", progress=25)

            if ev.is_set():
                raise Dibatalkan("Dihentikan setelah prompt jadi.")
            _catat(job_id, "Membuat latar video (Hugging Face ZeroGPU) ...")
            latar = buat_video_ai(prompt, folder, ev, lambda m: _catat(job_id, m))
            _catat(job_id, "Latar video jadi; menempelkan nama channel dan akun ...",
                   langkah="teks", progress=80)

            if ev.is_set():
                raise Dibatalkan("Dihentikan sebelum menempel teks.")
            hasil = tempel_teks(latar, channel, akun, folder)
            _catat(job_id, f"Video outro siap ({hasil.stat().st_size / 1_048_576:.1f} MB).",
                   status="done", langkah="selesai", progress=100, video=str(hasil))
        except Dibatalkan as error:
            _catat(job_id, str(error), status="dibatalkan", langkah="berhenti")
        except OutroError as error:
            _catat(job_id, f"Gagal: {error}", status="error", langkah="gagal")
        except Exception as error:  # noqa: BLE001
            logger.exception("outro %s gagal", job_id)
            _catat(job_id, f"Gagal: {type(error).__name__}: {error}", status="error", langkah="gagal")
        finally:
            _batal.pop(job_id, None)


# ============================================================
#  LANGKAH 1: PROMPT VISUAL DARI AI
# ============================================================

_PAGAR = re.compile(r"^```[a-zA-Z]*\n?|\n?```$", re.S)


def tulis_prompt_visual(channel: str, log: Callable[[str], None]) -> str:
    """Minta AI menulis prompt visual. Kalau gagal, pakai prompt bawaan."""
    if not AI_KUNCI.strip():
        log("Kunci AI kosong; memakai gaya bawaan.")
        return PROMPT_BAWAAN
    import httpx

    try:
        r = httpx.post(
            f"{AI_ALAMAT}/chat/completions",
            timeout=TUNGGU_AI,
            headers={"Authorization": f"Bearer {AI_KUNCI.strip()}", "Content-Type": "application/json"},
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": "Reply with only the prompt text, no preamble, no quotes, no code block."},
                    {"role": "user", "content": PERMINTAAN.format(channel=channel)},
                ],
                "temperature": 0.8,
            },
        )
        if r.status_code != 200:
            log(f"AI menolak ({r.status_code}); memakai gaya bawaan.")
            return PROMPT_BAWAAN
        isi = _PAGAR.sub("", str(r.json()["choices"][0]["message"]["content"]).strip()).strip()
        return isi if len(isi) >= 40 else PROMPT_BAWAAN
    except Exception as error:  # noqa: BLE001 - AI hanya penyempurna, bukan penentu
        log(f"AI tak terjangkau ({type(error).__name__}); memakai gaya bawaan.")
        return PROMPT_BAWAAN


# ============================================================
#  LANGKAH 2: VIDEO DARI HUGGING FACE ZEROGPU
# ============================================================

_NEG = ("text, letters, words, captions, subtitles, watermark, logo, blurry, "
        "low quality, distorted, people, human, face, jittery")


def buat_video_ai(
    prompt: str, folder: Path, ev: threading.Event, log: Callable[[str], None]
) -> Path:
    """Buat latar video dari prompt lewat Space text-to-video di ZeroGPU.

    gradio_client memanggil Space seperti orang menekan tombolnya, dan
    hasilnya berkas mp4 sementara yang lalu disalin ke folder pekerjaan.
    ZeroGPU gratis tanpa token untuk pemakaian sekali-sekali; kalau jatah
    hariannya habis, pesannya diteruskan apa adanya supaya jelas sebabnya.
    """
    try:
        from gradio_client import Client
    except Exception as error:  # noqa: BLE001
        raise OutroError("Paket gradio_client belum terpasang di backend.") from error

    if ev.is_set():
        raise Dibatalkan("Dihentikan sebelum membuat video.")
    log(f"Menghubungi {HF_SPACE} ...")
    try:
        klien = Client(HF_SPACE, token=HF_TOKEN, verbose=False)
    except Exception as error:  # noqa: BLE001
        raise OutroError(
            f"Tidak bisa membuka Space {HF_SPACE} ({type(error).__name__}). "
            "Space-nya mungkin sedang tidur atau diperbaiki; coba lagi nanti."
        ) from error

    log("Antre di GPU gratis lalu membuat video (biasanya 15-60 detik) ...")
    try:
        hasil = klien.predict(
            prompt=prompt,
            negative_prompt=_NEG,
            input_image_filepath=None,
            input_video_filepath=None,
            height_ui=VIDEO_H,
            width_ui=VIDEO_W,
            mode="text-to-video",
            duration_ui=VIDEO_DETIK,
            ui_frames_to_use=9,
            seed_ui=42,
            randomize_seed=True,
            ui_guidance_scale=1,
            improve_texture_flag=True,
            api_name="/text_to_video",
        )
    except Exception as error:  # noqa: BLE001
        pesan = str(error)
        if "quota" in pesan.lower() or "gpu" in pesan.lower():
            raise OutroError(
                "Jatah GPU gratis Hugging Face sedang habis. Tunggu beberapa menit, "
                "atau isi OUTRO_HF_TOKEN dengan token HF untuk jatah lebih besar."
            ) from error
        raise OutroError(f"Space gagal membuat video: {pesan[:200]}") from error

    # Kembaliannya (video, seed); video berupa dict {"video": path} atau path.
    berkas = hasil[0] if isinstance(hasil, (list, tuple)) else hasil
    if isinstance(berkas, dict):
        berkas = berkas.get("video") or berkas.get("path") or ""
    if not berkas or not Path(berkas).is_file():
        raise OutroError("Space selesai tetapi berkas videonya tidak ditemukan.")
    tujuan = folder / "latar.mp4"
    shutil.copy2(berkas, tujuan)
    return tujuan


# ============================================================
#  LANGKAH 3: TEMPEL NAMA CHANNEL + HANDLE (PERSIS)
# ============================================================

def _lebar_teks(draw: Any, teks: str, font: Any) -> int:
    kotak = draw.textbbox((0, 0), teks, font=font)
    return kotak[2] - kotak[0]


def _muat_font(ukuran: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.truetype(FONT_BUNDLED, ukuran)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def buat_lapisan_teks(w: int, h: int, channel: str, akun: dict[str, str], tujuan: Path) -> Path:
    """Gambar PNG transparan seukuran video: nama channel + handle, di tengah.

    Semua teks digambar sendiri di sini, jadi username muncul PERSIS - tidak
    ada yang diserahkan ke model video. Bayangan lembut ditaruh di belakang
    supaya terbaca di atas latar yang bergerak dan berubah terang-gelap.
    """
    from PIL import Image, ImageDraw

    kanvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(kanvas)

    def tulis_tengah(teks: str, font: Any, y: int, warna: tuple) -> int:
        lebar = _lebar_teks(d, teks, font)
        x = (w - lebar) // 2
        # bayangan: beberapa lapis offset supaya lembut dan tetap terbaca
        for dx, dy in ((0, 2), (2, 0), (0, -2), (-2, 0), (2, 2), (-2, 2)):
            d.text((x + dx, y + dy), teks, font=font, fill=(0, 0, 0, 180))
        d.text((x, y), teks, font=font, fill=warna)
        kotak = d.textbbox((x, y), teks, font=font)
        return kotak[3] - kotak[1]

    # Nama channel: besar, sekitar 34% tinggi.
    f_channel = _muat_font(max(28, int(w * 0.11)))
    y = int(h * 0.34)
    tinggi = tulis_tengah(channel.upper(), f_channel, y, (255, 255, 255, 255))
    y += tinggi + int(h * 0.05)

    # Garis pemisah tipis.
    d.line([(int(w * 0.30), y), (int(w * 0.70), y)], fill=(255, 255, 255, 120), width=2)
    y += int(h * 0.035)

    # Handle: hanya yang terisi, satu baris masing-masing.
    f_akun = _muat_font(max(16, int(w * 0.052)))
    for k in KUNCI_AKUN:
        nilai = str(akun.get(k) or "").strip()
        if not nilai:
            continue
        baris = f"{NAMA_TAMPIL[k]}   {nilai}"
        tinggi = tulis_tengah(baris, f_akun, y, (235, 235, 235, 255))
        y += tinggi + int(h * 0.028)

    kanvas.save(tujuan)
    return tujuan


def tempel_teks(latar: Path, channel: str, akun: dict[str, str], folder: Path) -> Path:
    """Tempel lapisan teks di atas latar video. Muncul perlahan (fade in)."""
    info = probe(latar)
    w, h = int(info.get("width") or VIDEO_W), int(info.get("height") or VIDEO_H)
    png = buat_lapisan_teks(w, h, channel, akun, folder / "teks.png")
    keluaran = folder / "outro.mp4"

    # PNG di-LOOP jadi aliran sepanjang video. Tanpa "-loop 1" ia hanya satu
    # frame di detik 0, dan fade yang mulai 0,6 detik membuatnya tidak pernah
    # terlihat sama sekali - teksnya hilang walau lapisannya benar. overlay
    # dengan shortest=1 memotong keluaran sepanjang latar video.
    # Teks muncul dari 0,6 detik, penuh pada 1,4 detik, lalu tetap.
    perintah = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(latar), "-loop", "1", "-i", str(png),
        "-filter_complex",
        "[1:v]fade=t=in:st=0.6:d=0.8:alpha=1,format=rgba[t];"
        "[0:v][t]overlay=0:0:shortest=1,format=yuv420p[v]",
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(keluaran),
    ]
    hasil = subprocess.run(perintah, capture_output=True, text=True, timeout=180)
    if hasil.returncode != 0 or not keluaran.is_file():
        detail = (hasil.stderr or "").strip().splitlines()[-1:] or ["tanpa pesan"]
        raise OutroError(f"Gagal menempel teks: {detail[0][:200]}")
    return keluaran


def bersihkan_lama(jam: float = 24.0) -> int:
    akar = outro_dir()
    if not akar.is_dir():
        return 0
    batas = time.time() - jam * 3600
    dibuang = 0
    for folder in akar.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < batas:
                shutil.rmtree(folder, ignore_errors=True)
                dibuang += 1
        except OSError:
            continue
    return dibuang
