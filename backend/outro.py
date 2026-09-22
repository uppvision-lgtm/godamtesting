"""Auto Outro: dari isian nama channel + akun sosmed sampai video outro.

Dua langkah, semuanya dikerjakan sendiri di mesin ini - tanpa AI, tanpa API,
tanpa jatah, tanpa antre, tanpa biaya:

1. Latar bergerak dirender prosedural dengan numpy + ffmpeg: gradien gelap,
   sapuan cahaya diagonal yang melintas, partikel melayang, vignette. Tiap
   video memakai seed berbeda, jadi 50 video sehari tidak ada yang kembar.
2. Renderer teks menempelkan nama channel dan enam handle di atasnya -
   PERSIS, huruf demi huruf, dengan fade-in lembut.

Kenapa tidak pakai AI video: dicoba dua-duanya. Google Flow tidak bisa
diotomasi (Google + Chrome menutup semua jalannya). Hugging Face ZeroGPU
bisa, tapi jatah gratisnya hanya ~1 video/hari tanpa token dan ~10 dengan
token - sedangkan kebutuhannya 50/hari. Dan yang dibuat AI ternyata hanya
"latar gelap + sapuan cahaya + partikel": grafis gerak sederhana yang bisa
dirender sendiri dalam hitungan detik, dengan resolusi lebih tinggi pula.
Teksnya memang sejak awal digambar sendiri, sebab semua model video tidak
bisa mengeja username dengan benar.

Berjalan di utas biasa (bukan Celery): alat developer, satu pekerjaan pada
satu waktu, keadaannya di memori proses. Ringan, jadi jalan di server pun bisa.
"""

from __future__ import annotations

import logging
import math
import os
import random
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from video_edit import FFMPEG_BIN, FONT_BUNDLED, MEDIA_DIR

logger = logging.getLogger(__name__)

# Urutan akun mengikuti teks perintah.
KUNCI_AKUN = ("instagram", "tiktok", "x", "threads", "facebook", "youtube")
NAMA_TAMPIL = {
    "instagram": "Instagram", "tiktok": "TikTok", "x": "X",
    "threads": "Threads", "facebook": "Facebook", "youtube": "YouTube",
}

# 720x1280 potret 9:16 - lebih tajam dari 416x704 milik ZeroGPU, dan masih
# dirender dalam beberapa detik. Semua bisa diubah lewat setelan.
VIDEO_W = int(os.getenv("OUTRO_VIDEO_W", "720"))
VIDEO_H = int(os.getenv("OUTRO_VIDEO_H", "1280"))
VIDEO_DETIK = float(os.getenv("OUTRO_VIDEO_DETIK", "4"))
FPS = int(os.getenv("OUTRO_FPS", "30"))


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
            seed = int(uuid.UUID(job_id.ljust(32, "0")).int % (2**31))

            if ev.is_set():
                raise Dibatalkan("Dihentikan sebelum mulai.")
            _catat(job_id, "Merender latar bergerak ...",
                   status="running", langkah="latar", progress=10)
            latar = render_latar(seed, folder, ev, lambda p: _catat(
                job_id, f"Merender latar bergerak ... {p}%", progress=10 + int(p * 0.6)))

            if ev.is_set():
                raise Dibatalkan("Dihentikan sebelum menempel teks.")
            _catat(job_id, "Menempelkan nama channel dan akun ...",
                   langkah="teks", progress=80)
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
#  LANGKAH 1: LATAR BERGERAK (prosedural, numpy + ffmpeg)
# ============================================================

# Semburat warna dasar yang dipilih per seed, supaya video tidak selalu abu.
_TINT = [(18, 22, 34), (26, 18, 30), (16, 26, 26), (22, 20, 20), (20, 24, 38)]


def _siapkan_latar(seed: int, w: int, h: int) -> dict[str, Any]:
    """Tetapkan warna dasar, partikel, dan sudut sapuan dari satu seed."""
    import numpy as np

    rnd = random.Random(seed)
    tint = rnd.choice(_TINT)
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    base = np.zeros((h, w, 3), np.float32)
    for i, c in enumerate(tint):
        base[:, :, i] = c * (1.0 - 0.55 * yy)

    jml = 90
    px = np.array([rnd.uniform(0, w) for _ in range(jml)], np.float32)
    py = np.array([rnd.uniform(0, h) for _ in range(jml)], np.float32)
    pr = np.array([rnd.uniform(1.0, 3.2) for _ in range(jml)], np.float32)
    pv = np.array([rnd.uniform(6, 22) for _ in range(jml)], np.float32)
    pph = np.array([rnd.uniform(0, math.tau) for _ in range(jml)], np.float32)
    sudut = rnd.uniform(math.radians(20), math.radians(52))
    return {"base": base, "px": px, "py": py, "pr": pr, "pv": pv, "pph": pph,
            "sudut": sudut, "w": w, "h": h}


def _frame_latar(t: float, s: dict[str, Any]) -> Any:
    """Satu frame latar pada detik ke-t."""
    import numpy as np

    w, h, sudut = s["w"], s["h"], s["sudut"]
    img = s["base"].copy()

    # Sapuan cahaya diagonal yang melintas dari satu sudut ke sudut lain.
    xs = np.arange(w, dtype=np.float32)[None, :]
    ys = np.arange(h, dtype=np.float32)[:, None]
    proj = xs * math.cos(sudut) + ys * math.sin(sudut)
    pmax = w * math.cos(sudut) + h * math.sin(sudut)
    pos = pmax * (0.15 + 0.7 * (t / VIDEO_DETIK))
    sweep = np.exp(-((proj - pos) ** 2) / (2 * 130.0 ** 2)).astype(np.float32)
    img += sweep[:, :, None] * np.array([200, 210, 235], np.float32) * 0.9

    # Partikel: titik lembut melayang naik, berkedip halus.
    yy = (s["py"] - s["pv"] * t) % h
    kedip = 0.5 + 0.5 * np.sin(s["pph"] + t * 2.2)
    px, pr = s["px"], s["pr"]
    for i in range(len(px)):
        cx, cy, r = float(px[i]), float(yy[i]), float(pr[i])
        x0, x1 = max(0, int(cx - r * 3)), min(w, int(cx + r * 3))
        y0, y1 = max(0, int(cy - r * 3)), min(h, int(cy + r * 3))
        if x1 <= x0 or y1 <= y0:
            continue
        gx = np.arange(x0, x1, dtype=np.float32)[None, :] - cx
        gy = np.arange(y0, y1, dtype=np.float32)[:, None] - cy
        g = np.exp(-(gx ** 2 + gy ** 2) / (2 * r ** 2)).astype(np.float32)
        img[y0:y1, x0:x1, :] += g[:, :, None] * (180.0 * float(kedip[i]))

    # Vignette lembut supaya tepi lebih gelap dan pusat menonjol.
    vx = (np.arange(w, dtype=np.float32) - w / 2) / (w / 2)
    vy = (np.arange(h, dtype=np.float32) - h / 2) / (h / 2)
    vig = 1.0 - 0.35 * np.clip(vx[None, :] ** 2 + vy[:, None] ** 2, 0, 1)
    img *= vig[:, :, None]
    return np.clip(img, 0, 255).astype(np.uint8)


def render_latar(
    seed: int, folder: Path, ev: threading.Event, lapor: Callable[[int], None]
) -> Path:
    """Render latar bergerak ke berkas mp4. Frame dikirim mentah ke ffmpeg."""
    try:
        import numpy  # noqa: F401 - dipastikan ada sebelum mulai
    except Exception as error:  # noqa: BLE001
        raise OutroError("Paket numpy belum terpasang di backend.") from error

    w = VIDEO_W - VIDEO_W % 2
    h = VIDEO_H - VIDEO_H % 2
    s = _siapkan_latar(seed, w, h)
    total = max(1, int(FPS * VIDEO_DETIK))
    keluaran = folder / "latar.mp4"
    proses = subprocess.Popen(
        [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(keluaran)],
        stdin=subprocess.PIPE,
    )
    try:
        lapor_tiap = max(1, total // 10)
        for n in range(total):
            if ev.is_set():
                proses.kill()
                raise Dibatalkan("Render latar dihentikan.")
            proses.stdin.write(_frame_latar(n / FPS, s).tobytes())
            if n % lapor_tiap == 0:
                lapor(int(n / total * 100))
    finally:
        if proses.stdin:
            proses.stdin.close()
        proses.wait()
    if proses.returncode != 0 or not keluaran.is_file():
        raise OutroError("Gagal merender latar video.")
    lapor(100)
    return keluaran


# ============================================================
#  LANGKAH 2: TEMPEL NAMA CHANNEL + HANDLE (PERSIS)
# ============================================================

def _muat_font(ukuran: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.truetype(FONT_BUNDLED, ukuran)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def buat_lapisan_teks(w: int, h: int, channel: str, akun: dict[str, str], tujuan: Path) -> Path:
    """Gambar PNG transparan seukuran video: nama channel + handle, di tengah.

    Semua teks digambar sendiri di sini, jadi username muncul PERSIS. Bayangan
    lembut di belakang supaya terbaca di atas latar yang bergerak.
    """
    from PIL import Image, ImageDraw

    kanvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(kanvas)

    def tulis_tengah(teks: str, font: Any, y: int, warna: tuple) -> int:
        kotak = d.textbbox((0, 0), teks, font=font)
        lebar = kotak[2] - kotak[0]
        x = (w - lebar) // 2
        for dx, dy in ((0, 2), (2, 0), (0, -2), (-2, 0), (2, 2), (-2, 2)):
            d.text((x + dx, y + dy), teks, font=font, fill=(0, 0, 0, 180))
        d.text((x, y), teks, font=font, fill=warna)
        kk = d.textbbox((x, y), teks, font=font)
        return kk[3] - kk[1]

    # Handle yang terisi menentukan seberapa tinggi blok teks; blok dipusatkan.
    isi = [(NAMA_TAMPIL[k], str(akun.get(k) or "").strip()) for k in KUNCI_AKUN]
    isi = [(nama, nilai) for nama, nilai in isi if nilai]

    f_channel = _muat_font(max(28, int(w * 0.095)))
    f_akun = _muat_font(max(16, int(w * 0.044)))
    tinggi_akun = int(h * 0.052)
    tinggi_blok = int(h * 0.10) + int(h * 0.05) + (len(isi) * tinggi_akun if isi else 0)
    y = max(int(h * 0.22), (h - tinggi_blok) // 2)

    t = tulis_tengah(channel.upper(), f_channel, y, (255, 255, 255, 255))
    y += t + int(h * 0.045)
    d.line([(int(w * 0.30), y), (int(w * 0.70), y)], fill=(255, 255, 255, 120), width=2)
    y += int(h * 0.03)
    for nama, nilai in isi:
        tulis_tengah(f"{nama}   {nilai}", f_akun, y, (235, 235, 235, 255))
        y += tinggi_akun

    kanvas.save(tujuan)
    return tujuan


def tempel_teks(latar: Path, channel: str, akun: dict[str, str], folder: Path) -> Path:
    """Tempel lapisan teks di atas latar video, muncul perlahan (fade in)."""
    png = buat_lapisan_teks(VIDEO_W - VIDEO_W % 2, VIDEO_H - VIDEO_H % 2,
                            channel, akun, folder / "teks.png")
    keluaran = folder / "outro.mp4"
    # PNG di-LOOP jadi aliran sepanjang video. Tanpa "-loop 1" ia hanya satu
    # frame di detik 0, dan fade yang mulai 0,6 detik membuatnya tak pernah
    # terlihat. overlay shortest=1 memotong keluaran sepanjang latar.
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
