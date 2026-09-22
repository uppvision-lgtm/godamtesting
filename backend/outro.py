"""Auto Outro: dari isian nama channel + akun sosmed sampai video outro.

Meniru outro acuan milik TV Rakyat (templates/utama/assets/outro.mp4) yang
dipelajari frame demi frame: latar gelap bersemburat merah-cokelat, badge
logo oktagon merah berbingkai perak yang muncul membesar, tagline miring,
pil emas "Follow untuk Update Terkini" dengan cahaya berputar di tepinya,
daftar handle dengan ikon platform berwarna yang masuk satu per satu, dan
gelombang merah berlapis di dasar. Satu dentingan berbunyi tepat saat pil
muncul - itu satu-satunya yang dipinjam dari acuan (assets/outro/denting.m4a);
selebihnya digambar sendiri supaya tajam di 1080x1920 dan berlaku untuk
channel mana pun.

Semuanya dikerjakan di mesin ini dengan numpy + Pillow + ffmpeg - tanpa AI,
tanpa API, tanpa jatah, tanpa antre. Frame dikirim mentah ke ffmpeg lewat
stdin, lalu audionya ditempel. Teks digambar sendiri, jadi username muncul
PERSIS - sesuatu yang tidak pernah bisa dijamin model video mana pun.

Berjalan di utas biasa (bukan Celery): alat developer, satu pekerjaan pada
satu waktu, keadaannya di memori proses.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from video_edit import FFMPEG_BIN, FONT_BUNDLED, MEDIA_DIR

logger = logging.getLogger(__name__)

KUNCI_AKUN = ("instagram", "youtube", "facebook", "tiktok", "x", "threads")
NAMA_TAMPIL = {
    "instagram": "Instagram", "tiktok": "TikTok", "x": "X",
    "threads": "Threads", "facebook": "Facebook", "youtube": "YouTube",
}

# Sama dengan acuan: 1080x1920, 30 fps, 5,5 detik.
VIDEO_W = int(os.getenv("OUTRO_VIDEO_W", "1080"))
VIDEO_H = int(os.getenv("OUTRO_VIDEO_H", "1920"))
VIDEO_DETIK = float(os.getenv("OUTRO_VIDEO_DETIK", "5.5"))
FPS = int(os.getenv("OUTRO_FPS", "30"))
AJAKAN_BAWAAN = "Follow untuk Update Terkini"
DENTING = Path(__file__).resolve().parent / "assets" / "outro" / "denting.m4a"
# Detik pil muncul; dentingnya berbunyi di sini juga (di acuan: 3,22 detik).
DETIK_PIL = 3.2


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


def mulai_job(
    channel: str,
    akun: dict[str, str],
    tagline: str = "",
    ajakan: str = AJAKAN_BAWAAN,
    logo_path: str | None = None,
) -> str:
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
            "tagline": " ".join(str(tagline or "").split()),
            "ajakan": " ".join(str(ajakan or "").split()) or AJAKAN_BAWAAN,
            "akun": {k: str(akun.get(k) or "").strip() for k in KUNCI_AKUN},
            "logo": logo_path or "",
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
            if ev.is_set():
                raise Dibatalkan("Dihentikan sebelum mulai.")
            _catat(job_id, "Merender outro ...", status="running", langkah="render", progress=5)
            mentah = render_video(
                job, folder, ev,
                lambda p: _catat(job_id, f"Merender outro ... {p}%", progress=5 + int(p * 0.85)),
            )
            if ev.is_set():
                raise Dibatalkan("Dihentikan sebelum menempel audio.")
            _catat(job_id, "Menempel audio ...", langkah="audio", progress=92)
            hasil = tempel_audio(mentah, folder)
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
#  ALAT GAMBAR
# ============================================================

def _font(ukuran: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.truetype(FONT_BUNDLED, ukuran)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _halus(t: float) -> float:
    """Smoothstep: masuk & keluar lembut."""
    t = _clamp(t)
    return t * t * (3 - 2 * t)


def _lenting(t: float) -> float:
    """Ease-out dengan lentingan kecil (melampaui 1 lalu kembali)."""
    t = _clamp(t)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _teks_gambar(teks: str, ukuran: int, warna: tuple, miring: bool = False,
                 bayang: bool = True) -> Any:
    """Gambar teks ke RGBA seukuran teks (plus tepian), opsional miring."""
    from PIL import Image, ImageDraw

    font = _font(ukuran)
    d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    x0, y0, x1, y1 = d0.textbbox((0, 0), teks, font=font)
    tepi = max(6, ukuran // 6)
    geser = int((y1 - y0) * 0.22) if miring else 0
    im = Image.new("RGBA", (x1 - x0 + 2 * tepi + geser, y1 - y0 + 2 * tepi), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    ox, oy = tepi - x0, tepi - y0
    if bayang:
        for dx, dy in ((2, 3), (3, 2), (0, 3)):
            d.text((ox + dx, oy + dy), teks, font=font, fill=(0, 0, 0, 150))
    d.text((ox, oy), teks, font=font, fill=warna)
    if miring:
        w, h = im.size
        im = im.transform((w, h), Image.AFFINE, (1, 0.22, -geser, 0, 1, 0), resample=Image.BICUBIC)
    return im


def _tempel(dasar: Any, lapisan: Any, x: int, y: int, alpha: float = 1.0) -> None:
    """Komposit RGBA di posisi (x, y) dengan alpha tambahan, aman di tepi."""
    from PIL import Image

    if alpha <= 0:
        return
    if alpha < 1:
        a = lapisan.getchannel("A").point(lambda v: int(v * alpha))
        lapisan = lapisan.copy()
        lapisan.putalpha(a)
    w, h = lapisan.size
    W, H = dasar.size
    # potong bagian yang keluar kanvas
    sx0, sy0 = max(0, -x), max(0, -y)
    sx1, sy1 = min(w, W - x), min(h, H - y)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    if (sx0, sy0, sx1, sy1) != (0, 0, w, h):
        lapisan = lapisan.crop((sx0, sy0, sx1, sy1))
        x, y = x + sx0, y + sy0
    dasar.alpha_composite(lapisan, dest=(x, y))


# ============================================================
#  ELEMEN: LATAR, GELOMBANG, BADGE, IKON, PIL
# ============================================================

def _latar(w: int, h: int) -> Any:
    """Latar gelap dengan semburat merah-cokelat hangat dari kanan-bawah."""
    import numpy as np
    from PIL import Image

    ys = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    xs = np.linspace(0, 1, w, dtype=np.float32)[None, :]
    # Nyaris hitam di atas; ke bawah sedikit menghangat. Acuannya gelap
    # pekat, semburat merahnya hanya terasa di pojok kanan-bawah.
    dasar = np.zeros((h, w, 3), np.float32)
    dasar[:, :, 0] = 7 + 9 * ys + 12 * ys ** 2
    dasar[:, :, 1] = 6 + 3 * ys
    dasar[:, :, 2] = 6 + 2 * ys
    d2 = (xs - 0.88) ** 2 * 1.1 + (ys - 0.94) ** 2 * 1.6
    semburat = np.exp(-d2 / 0.20)[:, :, None] * np.array([56, 13, 6], np.float32)
    dasar += semburat
    vx = (xs - 0.5) * 2
    vy = (ys - 0.5) * 2
    vig = 1.0 - 0.18 * np.clip(vx ** 2 + vy ** 2, 0, 1)
    dasar *= vig[:, :, None]
    return Image.fromarray(np.clip(dasar, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _gelombang(draw: Any, w: int, h: int, t: float, naik: float) -> None:
    """Tiga lapis gelombang merah di dasar, bergerak pelan. naik: 0..1."""
    # Seperti acuan: gelombang menjulang di pojok KANAN-bawah dan nyaris tak
    # ada di kiri, supaya tidak menabrak daftar handle. Digambar dari lapis
    # paling belakang (gelap, paling tinggi) ke depan (terang, paling rendah),
    # tiap lapis diberi garis tepi lebih terang - kesan kertas berlapis.
    dasar_y = h + 60 - int(300 * naik)
    lapis = (  # isi, tepi, fase, turun, naik_kanan, kecepatan
        ((108, 8, 16, 255), (150, 26, 34, 255), 0.0, 0, 300, 0.7),
        ((158, 12, 22, 255), (206, 42, 50, 255), 1.7, 70, 225, 0.9),
        ((214, 22, 30, 255), (240, 84, 88, 255), 3.3, 150, 150, 1.15),
    )
    for isi, tepi, fase, turun, naik_kanan, kec in lapis:
        titik = []
        for x in range(0, w + 40, 40):
            u = x / w
            y = (dasar_y + turun
                 + 140 * (1 - u) ** 1.2          # kiri diturunkan
                 - naik_kanan * u ** 1.7          # kanan dinaikkan
                 - 34 * math.sin(u * 3.6 + fase + t * kec)
                 - 18 * math.sin(u * 7.9 - fase * 0.6 + t * kec * 0.7))
            titik.append((x, int(y)))
        draw.polygon(titik + [(w, h), (0, h)], fill=isi)
        draw.line(titik, fill=tepi, width=3)


def _badge(nama: str, diameter: int) -> Any:
    """Badge oktagon merah berbingkai perak dengan nama channel dua baris."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    D = diameter
    S = 4  # digambar 4x lalu diperkecil supaya tepinya halus
    im = Image.new("RGBA", (D * S, D * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = D * S / 2

    def okta(r: float) -> list[tuple[float, float]]:
        return [(c + r * math.cos(math.radians(22.5 + 45 * i)),
                 c + r * math.sin(math.radians(22.5 + 45 * i))) for i in range(8)]

    n = D * S

    def mask_okta(r: float, bulat: float, geser_y: float = 0.0) -> Any:
        """Mask oktagon; sudutnya dibulatkan lewat blur + ambang (acuannya
        bukan rambu stop bersudut tajam, melainkan lempeng bersudut lembut)."""
        m = Image.new("L", (n, n), 0)
        ImageDraw.Draw(m).polygon([(x, y + geser_y) for x, y in okta(r)], fill=255)
        if bulat > 0:
            m = m.filter(ImageFilter.GaussianBlur(bulat)).point(lambda v: 255 if v >= 128 else 0)
        return m

    def lapis(m: Any, warna: tuple) -> None:
        lap = Image.new("RGBA", (n, n), warna)
        lap.putalpha(m)
        im.alpha_composite(lap)

    # bayangan lembut di bawah badge, supaya terkesan mengambang
    bayang = mask_okta(c * 0.93, c * 0.05, geser_y=c * 0.07).filter(ImageFilter.GaussianBlur(c * 0.09))
    lapis(bayang, (0, 0, 0, 160))
    lapis(mask_okta(c * 0.94, c * 0.06), (236, 236, 240, 255))   # bevel perak luar, tebal
    lapis(mask_okta(c * 0.855, c * 0.05), (186, 20, 22, 255))    # merah tepi
    lapis(mask_okta(c * 0.815, c * 0.05), (220, 220, 226, 255))  # cincin perak dalam
    mask = mask_okta(c * 0.775, c * 0.045)
    lapis(mask, (200, 26, 26, 255))                               # bidang merah
    # gradien radial di bidang merah: terang di tengah-atas
    ys, xs = np.mgrid[0:n, 0:n].astype(np.float32)
    r2 = ((xs - c) ** 2 + (ys - c * 0.85) ** 2) / (c * 0.8) ** 2
    terang = np.clip(1 - r2, 0, 1) ** 1.2
    arr = np.array(im).astype(np.float32)
    m = np.array(mask).astype(np.float32) / 255
    arr[:, :, 0] += 42 * terang * m
    arr[:, :, 1] += 20 * terang * m
    arr[:, :, 2] += 20 * terang * m
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")
    # kilau lembut kiri-atas
    kilau = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(kilau).ellipse(
        (c * 0.25, c * 0.28, c * 1.25, c * 0.95), fill=(255, 255, 255, 70))
    kilau = kilau.filter(ImageFilter.GaussianBlur(c * 0.12))
    kilau.putalpha(Image.composite(kilau.getchannel("A"), Image.new("L", (n, n), 0), mask))
    im.alpha_composite(kilau)
    # nama channel: dua baris, dibagi di spasi tengah
    kata = nama.upper().split()
    if len(kata) >= 2:
        belah = max(1, len(kata) // 2) if len(kata) > 2 else 1
        atas, bawah = " ".join(kata[:belah]), " ".join(kata[belah:])
    else:
        s = nama.upper()
        atas, bawah = (s[: len(s) // 2], s[len(s) // 2:]) if len(s) > 6 else (s, "")
    lebar_maks = c * 1.15
    for baris, y_pusat, skala in ((atas, c * 0.80, 0.30), (bawah, c * 1.22, 0.30)):
        if not baris:
            continue
        uk = int(c * skala)
        f = _font(uk)
        while uk > 20 and d.textbbox((0, 0), baris, font=f)[2] > lebar_maks:
            uk -= 4
            f = _font(uk)
        d = ImageDraw.Draw(im)
        x0, y0, x1, y1 = d.textbbox((0, 0), baris, font=f)
        px, py = c - (x1 - x0) / 2 - x0, y_pusat - (y1 - y0) / 2 - y0
        d.text((px + 4, py + 5), baris, font=f, fill=(90, 0, 0, 170))
        d.text((px, py), baris, font=f, fill=(255, 255, 255, 255))
    if atas and bawah:
        d.line([(c * 0.45, c * 1.0), (c * 1.55, c * 1.0)], fill=(255, 255, 255, 230), width=int(c * 0.02))
    return im.resize((D, D), Image.LANCZOS)


def _logo_unggahan(path: str, diameter: int) -> Any:
    from PIL import Image

    im = Image.open(path).convert("RGBA")
    im.thumbnail((diameter, diameter), Image.LANCZOS)
    kanvas = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    kanvas.alpha_composite(im, ((diameter - im.width) // 2, (diameter - im.height) // 2))
    return kanvas


def _ikon(platform: str, s: int) -> Any:
    """Ikon platform berwarna, digambar sendiri (tajam di ukuran berapa pun)."""
    import numpy as np
    from PIL import Image, ImageDraw

    S = 4
    n = s * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    rad = n * 0.22
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, n - 1, n - 1), rad, fill=255)

    if platform == "instagram":
        ys = np.linspace(0, 1, n, dtype=np.float32)[:, None]
        xs = np.linspace(0, 1, n, dtype=np.float32)[None, :]
        u = np.clip(0.6 * ys + 0.4 * (1 - xs), 0, 1)
        atas = np.array([255, 190, 60], np.float32)
        tengah = np.array([225, 45, 110], np.float32)
        bawah = np.array([110, 45, 200], np.float32)
        arr = np.where(u[:, :, None] < 0.5,
                       atas + (tengah - atas) * (u[:, :, None] / 0.5),
                       tengah + (bawah - tengah) * ((u[:, :, None] - 0.5) / 0.5))
        bg = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
        bg.putalpha(mask)
        im = bg
        d = ImageDraw.Draw(im)
        p = n * 0.2
        d.rounded_rectangle((p, p, n - p, n - p), n * 0.14, outline=(255, 255, 255, 255), width=int(n * 0.075))
        r = n * 0.16
        d.ellipse((n / 2 - r, n / 2 - r, n / 2 + r, n / 2 + r), outline=(255, 255, 255, 255), width=int(n * 0.075))
        q = n * 0.045
        d.ellipse((n * 0.70 - q, n * 0.30 - q, n * 0.70 + q, n * 0.30 + q), fill=(255, 255, 255, 255))
    elif platform == "youtube":
        d.rounded_rectangle((0, 0, n - 1, n - 1), rad, fill=(255, 0, 0, 255))
        p = n * 0.2
        d.rounded_rectangle((p, n * 0.28, n - p, n * 0.72), n * 0.12, fill=(255, 255, 255, 255))
        d.polygon([(n * 0.43, n * 0.38), (n * 0.43, n * 0.62), (n * 0.63, n * 0.5)], fill=(255, 0, 0, 255))
    elif platform == "facebook":
        d.rounded_rectangle((0, 0, n - 1, n - 1), rad, fill=(24, 119, 242, 255))
        f = _font(int(n * 0.86))
        x0, y0, x1, y1 = d.textbbox((0, 0), "f", font=f)
        d.text((n * 0.56 - (x1 - x0) / 2 - x0, n * 0.62 - (y1 - y0) / 2 - y0), "f", font=f, fill=(255, 255, 255, 255))
    elif platform == "tiktok":
        d.rounded_rectangle((0, 0, n - 1, n - 1), rad, fill=(0, 0, 0, 255))
        # not musik: bulatan + batang + bendera, versi cyan & pink digeser lalu putih
        for warna, dx, dy in (((37, 244, 238, 255), -n * 0.02, -n * 0.02),
                              ((254, 44, 85, 255), n * 0.02, n * 0.02),
                              ((255, 255, 255, 255), 0, 0)):
            bx, by = n * 0.40 + dx, n * 0.66 + dy
            r = n * 0.12
            d.ellipse((bx - r, by - r, bx + r, by + r), fill=warna)
            d.rectangle((bx + r * 0.55, n * 0.20 + dy, bx + r * 0.55 + n * 0.075, by), fill=warna)
            d.arc((bx + r * 0.55 - n * 0.02, n * 0.18 + dy, bx + n * 0.42, n * 0.48 + dy),
                  start=270, end=30, fill=warna, width=int(n * 0.075))
    elif platform == "x":
        d.rounded_rectangle((0, 0, n - 1, n - 1), rad, fill=(0, 0, 0, 255))
        lw = int(n * 0.085)
        d.line([(n * 0.25, n * 0.25), (n * 0.75, n * 0.75)], fill=(255, 255, 255, 255), width=lw)
        d.line([(n * 0.75, n * 0.25), (n * 0.25, n * 0.75)], fill=(255, 255, 255, 255), width=lw)
    else:  # threads
        d.rounded_rectangle((0, 0, n - 1, n - 1), rad, fill=(0, 0, 0, 255))
        f = _font(int(n * 0.78))
        x0, y0, x1, y1 = d.textbbox((0, 0), "@", font=f)
        d.text((n / 2 - (x1 - x0) / 2 - x0, n * 0.52 - (y1 - y0) / 2 - y0), "@", font=f, fill=(255, 255, 255, 255))

    if platform != "instagram":
        im.putalpha(Image.composite(im.getchannel("A"), Image.new("L", (n, n), 0), mask))
    return im.resize((s, s), Image.LANCZOS)


def _pil(teks: str, lebar: int, tinggi: int) -> tuple[Any, list[tuple[float, float]]]:
    """Pil emas berteks + daftar titik keliling tepinya (untuk cahaya berputar)."""
    from PIL import Image, ImageDraw

    S = 3
    W, H = lebar * S, tinggi * S
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    tepi = int(H * 0.06)
    r = H / 2 - tepi
    d.rounded_rectangle((tepi, tepi, W - tepi, H - tepi), r, outline=(212, 168, 60, 190), width=int(H * 0.045))
    f = _font(int(H * 0.36))
    x0, y0, x1, y1 = d.textbbox((0, 0), teks, font=f)
    while (x1 - x0) > W * 0.82 and f.size > 12:
        f = _font(f.size - 2)
        x0, y0, x1, y1 = d.textbbox((0, 0), teks, font=f)
    d.text((W / 2 - (x1 - x0) / 2 - x0 + 2, H / 2 - (y1 - y0) / 2 - y0 + 3), teks, font=f, fill=(0, 0, 0, 160))
    d.text((W / 2 - (x1 - x0) / 2 - x0, H / 2 - (y1 - y0) / 2 - y0), teks, font=f, fill=(255, 255, 255, 255))
    im = im.resize((lebar, tinggi), Image.LANCZOS)

    # keliling pil (dalam koordinat akhir), urut searah jarum jam dari kiri-atas
    t = tepi / S
    rr = r / S
    titik: list[tuple[float, float]] = []
    seg = 40
    L, R, T, B = t + rr, lebar - t - rr, t, tinggi - t
    for i in range(seg + 1):
        titik.append((L + (R - L) * i / seg, T))
    for i in range(1, seg):
        a = -math.pi / 2 + math.pi * i / seg
        titik.append((R + rr * math.cos(a), t + rr + rr * math.sin(a)))
    for i in range(seg + 1):
        titik.append((R - (R - L) * i / seg, B))
    for i in range(1, seg):
        a = math.pi / 2 + math.pi * i / seg
        titik.append((L + rr * math.cos(a), t + rr + rr * math.sin(a)))
    return im, titik


def _cahaya_pil(lebar: int, tinggi: int, titik: list[tuple[float, float]], p: float) -> Any:
    """Segmen cahaya emas terang yang berjalan di keliling pil (p: 0..1)."""
    from PIL import Image, ImageDraw, ImageFilter

    im = Image.new("RGBA", (lebar, tinggi), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    n = len(titik)
    panjang = int(n * 0.26)
    awal = int(p * n) % n
    seg = [titik[(awal + i) % n] for i in range(panjang)]
    # Tiga lapis: bloom lebar yang diburamkan kuat, pendar sedang, lalu inti
    # terang tipis - seperti pijar emas di acuan.
    d.line(seg, fill=(255, 205, 80, 235), width=max(4, tinggi // 5), joint="curve")
    im = im.filter(ImageFilter.GaussianBlur(tinggi * 0.11))
    d = ImageDraw.Draw(im)
    d.line(seg, fill=(255, 225, 120, 255), width=max(3, tinggi // 12), joint="curve")
    im = im.filter(ImageFilter.GaussianBlur(tinggi * 0.03))
    d = ImageDraw.Draw(im)
    d.line(seg, fill=(255, 246, 200, 255), width=max(2, tinggi // 26), joint="curve")
    return im


# ============================================================
#  RENDER
# ============================================================

def render_video(
    job: dict[str, Any], folder: Path, ev: threading.Event, lapor: Callable[[int], None]
) -> Path:
    from PIL import ImageDraw

    w = VIDEO_W - VIDEO_W % 2
    h = VIDEO_H - VIDEO_H % 2
    channel = job["channel"]
    akun = job["akun"]
    tagline = job.get("tagline") or ""
    ajakan = job.get("ajakan") or AJAKAN_BAWAAN
    isi = [(k, akun[k]) for k in KUNCI_AKUN if akun.get(k)]

    # --- pra-render elemen yang tidak berubah ---
    latar = _latar(w, h)
    D = int(w * 0.56)
    if job.get("logo") and Path(job["logo"]).is_file():
        badge = _logo_unggahan(job["logo"], D)
    else:
        badge = _badge(channel, D)
    bx, by = (w - D) // 2, int(h * 0.19)

    tag_im = _teks_gambar(tagline, int(w * 0.062), (255, 255, 255, 255), miring=True) if tagline else None
    tag_y = by + D + int(h * 0.03)

    pil_w, pil_h = int(w * 0.60), int(h * 0.062)
    pil_im, pil_titik = _pil(ajakan, pil_w, pil_h)
    pil_x = (w - pil_w) // 2
    pil_y = (tag_y + (tag_im.height if tag_im else 0) + int(h * 0.02)) if tagline else tag_y

    ikon_s = int(w * 0.082)
    pitch = int(h * 0.046) if len(isi) > 4 else int(h * 0.05)
    baris_y0 = pil_y + pil_h + int(h * 0.035)
    baris_ims = []
    for k, nilai in isi:
        ik = _ikon(k, ikon_s)
        tx = _teks_gambar(nilai, int(w * 0.052), (255, 255, 255, 255))
        baris_ims.append((ik, tx))

    total = max(1, int(FPS * VIDEO_DETIK))
    keluaran = folder / "video-tanpa-audio.mp4"
    proses = subprocess.Popen(
        [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(keluaran)],
        stdin=subprocess.PIPE,
    )
    try:
        lapor_tiap = max(1, total // 12)
        for n in range(total):
            if ev.is_set():
                proses.kill()
                raise Dibatalkan("Render dihentikan.")
            t = n / FPS
            frame = latar.copy()

            # gelombang: naik 0-0.9s, lalu bergerak pelan
            d = ImageDraw.Draw(frame)
            _gelombang(d, w, h, t, _halus(t / 0.9))

            # badge: muncul 0.4s dengan lentingan, lalu mengambang halus
            if t >= 0.4:
                sk = _lenting((t - 0.4) / 0.8)
                sk = max(0.02, sk)
                ukur = max(2, int(D * sk))
                bimg = badge.resize((ukur, ukur)) if ukur != D else badge
                apung = int(5 * math.sin(t * 1.8))
                _tempel(frame, bimg, bx + (D - ukur) // 2, by + (D - ukur) // 2 + apung,
                        _clamp((t - 0.4) / 0.25))

            # tagline: fade + naik, 1.2-1.7s
            if tag_im is not None and t >= 1.2:
                a = _halus((t - 1.2) / 0.5)
                _tempel(frame, tag_im, (w - tag_im.width) // 2, tag_y + int(30 * (1 - a)), a)

            # pil + cahaya berputar, mulai 3.2s
            if t >= DETIK_PIL:
                a = _halus((t - DETIK_PIL) / 0.35)
                sk = 0.9 + 0.1 * _lenting((t - DETIK_PIL) / 0.45)
                pw, ph = max(2, int(pil_w * sk)), max(2, int(pil_h * sk))
                pim = pil_im.resize((pw, ph)) if (pw, ph) != (pil_w, pil_h) else pil_im
                _tempel(frame, pim, pil_x + (pil_w - pw) // 2, pil_y + (pil_h - ph) // 2, a)
                cahaya = _cahaya_pil(pil_w, pil_h, pil_titik, ((t - DETIK_PIL) / 1.6) % 1.0)
                _tempel(frame, cahaya, pil_x, pil_y, a)

            # handle: masuk dari kiri satu per satu, mulai 1.8s
            for i, (ik, tx) in enumerate(baris_ims):
                mulai = 1.8 + i * 0.14
                if t < mulai:
                    continue
                a = _halus((t - mulai) / 0.4)
                geser = int(-60 * (1 - a))
                y = baris_y0 + i * pitch
                _tempel(frame, ik, int(w * 0.22) + geser, y, a)
                _tempel(frame, tx, int(w * 0.22) + ikon_s + int(w * 0.03) + geser,
                        y + (ikon_s - tx.height) // 2, a)

            proses.stdin.write(frame.convert("RGB").tobytes())
            if n % lapor_tiap == 0:
                lapor(int(n / total * 100))
    finally:
        if proses.stdin:
            proses.stdin.close()
        proses.wait()
    if proses.returncode != 0 or not keluaran.is_file():
        raise OutroError("Gagal merender video outro.")
    lapor(100)
    return keluaran


def tempel_audio(video: Path, folder: Path) -> Path:
    """Tempel dentingan tepat saat pil muncul. Tanpa aset audio, video tetap jadi."""
    keluaran = folder / "outro.mp4"
    if not DENTING.is_file():
        shutil.copy2(video, keluaran)
        return keluaran
    tunda = int(DETIK_PIL * 1000)
    perintah = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(video), "-i", str(DENTING),
        "-filter_complex", f"[1:a]adelay={tunda}|{tunda},apad[a]",
        "-map", "0:v", "-map", "[a]", "-shortest",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(keluaran),
    ]
    hasil = subprocess.run(perintah, capture_output=True, text=True, timeout=120)
    if hasil.returncode != 0 or not keluaran.is_file():
        logger.warning("audio gagal ditempel, video tanpa audio dipakai: %s",
                       (hasil.stderr or "").strip()[-200:])
        shutil.copy2(video, keluaran)
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
