"""Auto Outro: dari nama channel + akun sosmed sampai video outro - dan tiap
kali dibuat, tampilannya berbeda jauh.

Kebutuhannya 100 outro sehari yang tidak boleh terasa sama, dengan tiga hal
yang selalu ada: logo (badge), nama TV, dan enam sosmed. Yang dibangun bukan
tumpukan ratusan berkas aset, melainkan SISTEM GAYA BERSUMBU: tiap outro
memilih satu nilai di tiap sumbu, seedd dari job-nya -

  palet warna (14) x gaya latar (6) x dekorasi (7) x bentuk badge (5)
  x tata letak (5) x gaya ikon (2) x animasi badge (4) x animasi handle (4)
  x tipografi x durasi

- puluhan ribu kombinasi, dan tiap sumbu mengubah tampilan secara nyata
(bentuk, susunan, gerakan), bukan sekadar menggeser warna.

Semuanya dirender sendiri dengan numpy + Pillow + ffmpeg: tanpa AI, tanpa
API, tanpa jatah, ~3-5 detik per video di 1080x1920. Teks digambar sendiri,
jadi username muncul PERSIS. Satu dentingan (assets/outro/denting.m4a)
berbunyi saat handle-handle muncul.

Berjalan di utas biasa: alat developer, satu pekerjaan pada satu waktu.
Setiap perintah ffmpeg membawa -threads (kontainer server 1 GB).
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

KUNCI_AKUN = ("instagram", "youtube", "facebook", "tiktok", "x", "threads")
NAMA_TAMPIL = {
    "instagram": "Instagram", "tiktok": "TikTok", "x": "X",
    "threads": "Threads", "facebook": "Facebook", "youtube": "YouTube",
}

VIDEO_W = int(os.getenv("OUTRO_VIDEO_W", "1080"))
VIDEO_H = int(os.getenv("OUTRO_VIDEO_H", "1920"))
FPS = int(os.getenv("OUTRO_FPS", "30"))
DENTING = Path(__file__).resolve().parent / "assets" / "outro" / "denting.m4a"


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
    channel: str, akun: dict[str, str], seed: int | None = None, logo_path: str | None = None
) -> str:
    channel = " ".join(str(channel or "").split())
    if not channel:
        raise OutroError("Nama channel wajib diisi.")
    job_id = uuid.uuid4().hex[:12]
    if seed is None:
        # Dari 12 digit heksa job_id langsung. Dulu di-pad kanan dengan nol
        # menjadi UUID lalu diambil 31 bit terendahnya - yang semuanya nol,
        # sehingga setiap outro "acak" memakai seed 0 dan gaya yang sama.
        seed = int(job_id, 16) % (2**31)
    gaya = pilih_gaya(int(seed))
    with _kunci:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "langkah": "antre",
            "progress": 0,
            "channel": channel,
            "akun": {k: str(akun.get(k) or "").strip() for k in KUNCI_AKUN},
            "seed": int(seed),
            "gaya": ringkas_gaya(gaya),
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
            gaya = pilih_gaya(int(job["seed"]))
            _catat(job_id, f"Merender outro ({job['gaya']}) ...",
                   status="running", langkah="render", progress=5)
            mentah = render_video(
                job, gaya, folder, ev,
                lambda p: _catat(job_id, f"Merender outro ... {p}%", progress=5 + int(p * 0.85)),
            )
            if ev.is_set():
                raise Dibatalkan("Dihentikan sebelum menempel audio.")
            _catat(job_id, "Menempel audio ...", langkah="audio", progress=92)
            hasil = tempel_audio(mentah, folder, gaya["denting_detik"])
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
#  SUMBU GAYA
# ============================================================

# Palet: (nama, atas, bawah, aksen, aksen2/bingkai, dekor1, dekor2, glow)
PALET = [
    ("merah",   (14, 8, 8),    (36, 10, 10),  (200, 26, 26),  (236, 236, 240), (150, 12, 20), (214, 22, 30),  (56, 13, 6)),
    ("biru",    (6, 10, 22),   (10, 20, 46),  (30, 90, 220),  (230, 236, 250), (16, 50, 140), (40, 110, 230), (8, 30, 70)),
    ("teal",    (6, 18, 18),   (8, 34, 34),   (0, 150, 140),  (230, 250, 246), (0, 90, 84),   (0, 180, 165),  (0, 50, 46)),
    ("emas",    (16, 12, 6),   (34, 26, 10),  (214, 160, 40), (250, 240, 210), (140, 100, 20),(230, 180, 60), (60, 42, 8)),
    ("ungu",    (14, 6, 22),   (30, 10, 46),  (140, 40, 220), (240, 230, 250), (80, 20, 140), (160, 60, 230), (44, 10, 70)),
    ("hijau",   (6, 16, 8),    (10, 34, 14),  (30, 160, 70),  (230, 250, 236), (14, 90, 40),  (40, 180, 90),  (8, 50, 18)),
    ("jingga",  (18, 8, 4),    (40, 18, 6),   (240, 110, 20), (255, 240, 225), (160, 60, 10), (250, 140, 40), (70, 30, 6)),
    ("mono",    (8, 8, 8),     (24, 24, 24),  (240, 240, 240),(30, 30, 30),    (60, 60, 60),  (110, 110, 110),(30, 30, 30)),
    ("magenta", (18, 4, 14),   (44, 8, 34),   (230, 40, 150), (255, 230, 245), (140, 20, 90), (240, 80, 170),(70, 10, 46)),
    ("laut",    (4, 12, 24),   (6, 26, 52),   (0, 170, 230),  (225, 245, 255), (0, 90, 140),  (0, 200, 255), (0, 40, 80)),
    ("zaitun",  (12, 14, 6),   (28, 32, 12),  (150, 170, 40), (245, 250, 225), (80, 95, 20),  (170, 195, 60),(40, 48, 10)),
    ("marun",   (16, 4, 8),    (40, 8, 18),   (160, 20, 60),  (250, 225, 235), (100, 10, 40), (190, 40, 80), (60, 6, 24)),
    ("es",      (10, 14, 20),  (26, 34, 46),  (160, 200, 240),(20, 30, 44),    (60, 80, 110), (120, 160, 210),(30, 44, 64)),
    ("api",     (16, 6, 4),    (46, 14, 6),   (250, 60, 30),  (255, 235, 220), (170, 30, 10), (255, 120, 40),(80, 20, 8)),
]
LATAR = ("gradien", "sapuan", "partikel", "titik", "sinar", "gumpal")
DEKOR = ("gelombang-kanan", "gelombang-kiri", "pita", "cincin", "sudut", "garis", "tanpa")
BENTUK = ("oktagon", "lingkaran", "kotak", "heksagon", "perisai")
TATA = ("acuan", "tengah", "grid", "dua-kolom", "baris-ikon")
IKON = ("warna", "mono")
MASUK_BADGE = ("lenting", "turun", "putar", "zoom")
MASUK_HANDLE = ("kiri", "atas", "pop", "fade")


def pilih_gaya(seed: int) -> dict[str, Any]:
    """Satu nilai di tiap sumbu, ditentukan seed - sama seed, sama tampilan."""
    rnd = random.Random(seed)
    p = rnd.choice(PALET)
    tata = rnd.choice(TATA)
    return {
        "seed": seed,
        "palet": p[0], "atas": p[1], "bawah": p[2], "aksen": p[3], "aksen2": p[4],
        "dekor1": p[5], "dekor2": p[6], "glow": p[7],
        "latar": rnd.choice(LATAR),
        "dekor": rnd.choice(DEKOR),
        "bentuk": rnd.choice(BENTUK),
        "tata": tata,
        "ikon": rnd.choice(IKON),
        "masuk_badge": rnd.choice(MASUK_BADGE),
        "masuk_handle": rnd.choice(MASUK_HANDLE),
        "huruf_besar": rnd.random() < 0.55,
        "miring": rnd.random() < 0.35,
        "durasi": rnd.choice((4.5, 5.0, 5.5, 6.0)),
        "sudut_sapuan": rnd.uniform(math.radians(18), math.radians(60)),
        "denting_detik": 1.9,
        "acak": rnd.random(),
    }


def ringkas_gaya(g: dict[str, Any]) -> str:
    return (f"palet {g['palet']} · latar {g['latar']} · dekor {g['dekor']} · badge {g['bentuk']} · "
            f"tata {g['tata']} · ikon {g['ikon']} · masuk {g['masuk_badge']}/{g['masuk_handle']}")


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
    t = _clamp(t)
    return t * t * (3 - 2 * t)


def _lenting(t: float) -> float:
    t = _clamp(t)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _teks_gambar(teks: str, ukuran: int, warna: tuple, miring: bool = False) -> Any:
    from PIL import Image, ImageDraw

    font = _font(ukuran)
    d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    x0, y0, x1, y1 = d0.textbbox((0, 0), teks, font=font)
    tepi = max(6, ukuran // 6)
    geser = int((y1 - y0) * 0.22) if miring else 0
    im = Image.new("RGBA", (x1 - x0 + 2 * tepi + geser, y1 - y0 + 2 * tepi), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    ox, oy = tepi - x0, tepi - y0
    for dx, dy in ((2, 3), (3, 2), (0, 3)):
        d.text((ox + dx, oy + dy), teks, font=font, fill=(0, 0, 0, 150))
    d.text((ox, oy), teks, font=font, fill=warna)
    if miring:
        w, h = im.size
        im = im.transform((w, h), Image.AFFINE, (1, 0.22, -geser, 0, 1, 0), resample=Image.BICUBIC)
    return im


def _tempel(dasar: Any, lapisan: Any, x: int, y: int, alpha: float = 1.0) -> None:
    if alpha <= 0:
        return
    if alpha < 1:
        a = lapisan.getchannel("A").point(lambda v: int(v * alpha))
        lapisan = lapisan.copy()
        lapisan.putalpha(a)
    w, h = lapisan.size
    W, H = dasar.size
    sx0, sy0 = max(0, -x), max(0, -y)
    sx1, sy1 = min(w, W - x), min(h, H - y)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    if (sx0, sy0, sx1, sy1) != (0, 0, w, h):
        lapisan = lapisan.crop((sx0, sy0, sx1, sy1))
        x, y = x + sx0, y + sy0
    dasar.alpha_composite(lapisan, dest=(x, y))


def _skala(im: Any, faktor: float) -> Any:
    if abs(faktor - 1.0) < 1e-3:
        return im
    w, h = im.size
    return im.resize((max(2, int(w * faktor)), max(2, int(h * faktor))))


# ============================================================
#  LATAR (statis per gaya) + GERAKAN LATAR (per frame)
# ============================================================

def _latar(w: int, h: int, g: dict[str, Any]) -> Any:
    """Latar dasar: gradien dua warna + glow sudut + vignette. Pola tambahan
    (titik/sinar/gumpal) ikut dipasang di sini kalau gayanya begitu."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    ys = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    xs = np.linspace(0, 1, w, dtype=np.float32)[None, :]
    atas = np.array(g["atas"], np.float32)
    bawah = np.array(g["bawah"], np.float32)
    # arah gradien ikut seed: tegak, atau miring
    u = ys if g["acak"] < 0.5 else np.clip(0.7 * ys + 0.3 * xs, 0, 1)
    # ys berbentuk (h,1): disebar ke (h,w) dulu, lalu ke kanal warna.
    u = np.broadcast_to(u, (h, w))[:, :, None]
    dasar = atas + (bawah - atas) * u
    # glow di satu sudut
    sx, sy = (0.88, 0.94) if g["acak"] < 0.5 else (0.12, 0.08)
    d2 = (xs - sx) ** 2 * 1.1 + (ys - sy) ** 2 * 1.6
    dasar = dasar + np.exp(-d2 / 0.22)[:, :, None] * np.array(g["glow"], np.float32)
    vx, vy = (xs - 0.5) * 2, (ys - 0.5) * 2
    dasar *= (1.0 - 0.18 * np.clip(vx ** 2 + vy ** 2, 0, 1))[:, :, None]
    im = Image.fromarray(np.clip(dasar, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

    if g["latar"] == "titik":
        lap = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(lap)
        c = (*g["dekor2"], 70)
        langkah = 56
        for yy in range(28, h, langkah):
            for xx in range(28, w, langkah):
                d.ellipse((xx - 3, yy - 3, xx + 3, yy + 3), fill=c)
        im.alpha_composite(lap)
    elif g["latar"] == "gumpal":
        lap = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(lap)
        rnd = random.Random(g["seed"] + 7)
        for i, warna in enumerate((g["dekor1"], g["dekor2"], g["aksen"])):
            cx, cy = rnd.uniform(0.1, 0.9) * w, rnd.uniform(0.1, 0.9) * h
            r = rnd.uniform(0.25, 0.45) * w
            d.ellipse((cx - r, cy - r * 0.8, cx + r, cy + r * 0.8), fill=(*warna, 60))
        lap = lap.filter(ImageFilter.GaussianBlur(w * 0.12))
        im.alpha_composite(lap)
    return im


def _sinar(w: int, h: int, g: dict[str, Any]) -> Any:
    """Kipas sinar tipis berpusat di badge, diputar pelan per frame."""
    from PIL import Image, ImageDraw, ImageFilter

    n = int(max(w, h) * 1.6)
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = n / 2
    for i in range(14):
        a = i * math.tau / 14
        d.polygon([(c, c), (c + n * math.cos(a - 0.06), c + n * math.sin(a - 0.06)),
                   (c + n * math.cos(a + 0.06), c + n * math.sin(a + 0.06))], fill=(*g["dekor2"], 38))
    return im.filter(ImageFilter.GaussianBlur(6))


def _gerak_latar(frame: Any, w: int, h: int, t: float, g: dict[str, Any], cache: dict[str, Any]) -> None:
    """Gerakan latar per frame sesuai gaya."""
    import numpy as np
    from PIL import Image, ImageDraw

    if g["latar"] == "sapuan":
        sudut = g["sudut_sapuan"]
        xs = np.arange(w, dtype=np.float32)[None, :]
        ys = np.arange(h, dtype=np.float32)[:, None]
        proj = xs * math.cos(sudut) + ys * math.sin(sudut)
        pmax = w * math.cos(sudut) + h * math.sin(sudut)
        pos = pmax * (0.1 + 0.8 * ((t / g["durasi"]) % 1.0))
        sweep = np.exp(-((proj - pos) ** 2) / (2 * 150.0 ** 2)).astype(np.float32)
        lap = np.zeros((h, w, 4), np.uint8)
        lap[:, :, :3] = g["aksen2"] if g["palet"] != "mono" else (255, 255, 255)
        lap[:, :, 3] = (sweep * 110).astype(np.uint8)
        frame.alpha_composite(Image.fromarray(lap, "RGBA"))
    elif g["latar"] == "partikel":
        if "partikel" not in cache:
            rnd = random.Random(g["seed"] + 3)
            cache["partikel"] = [(rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(2, 5),
                                  rnd.uniform(8, 30), rnd.uniform(0, math.tau)) for _ in range(70)]
        lap = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(lap)
        for px, py, r, v, ph in cache["partikel"]:
            y = (py - v * t) % h
            a = int(120 + 100 * (0.5 + 0.5 * math.sin(ph + t * 2.0)))
            d.ellipse((px - r, y - r, px + r, y + r), fill=(*g["aksen2"], a))
        frame.alpha_composite(lap)
    elif g["latar"] == "sinar":
        # Kipas sinarnya buram, jadi diputar di 1/4 skala lalu diperbesar:
        # memutar gambar 3000-an piksel tiap frame memakan ~30 detik per
        # video, versi kecil ini di bawah 5 detik tanpa perbedaan yang tampak.
        if "sinar" not in cache:
            penuh = _sinar(w, h, g)
            cache["sinar_n"] = penuh.width
            cache["sinar"] = penuh.resize((penuh.width // 4, penuh.height // 4), Image.BILINEAR)
        n = cache["sinar_n"]
        s = cache["sinar"].rotate(t * 6.0, resample=Image.BILINEAR).resize((n, n), Image.BILINEAR)
        _tempel(frame, s, w // 2 - n // 2, int(h * 0.33) - n // 2, 1.0)


# ============================================================
#  DEKORASI
# ============================================================

def _gelombang(draw: Any, w: int, h: int, t: float, naik: float, g: dict[str, Any], kanan: bool) -> None:
    dasar_y = h + 60 - int(300 * naik)
    lapis = ((g["dekor1"], 0.0, 0, 300, 0.7), (g["dekor2"], 1.7, 70, 225, 0.9), (g["aksen"], 3.3, 150, 150, 1.15))
    for isi, fase, turun, naik_kanan, kec in lapis:
        titik = []
        for x in range(0, w + 40, 40):
            u = x / w if kanan else 1 - x / w
            y = (dasar_y + turun + 140 * (1 - u) ** 1.2 - naik_kanan * u ** 1.7
                 - 34 * math.sin(u * 3.6 + fase + t * kec) - 18 * math.sin(u * 7.9 - fase * 0.6 + t * kec * 0.7))
            titik.append((x, int(y)))
        draw.polygon(titik + [(w, h), (0, h)], fill=(*isi, 255))
        tepi = tuple(min(255, c + 40) for c in isi)
        draw.line(titik, fill=(*tepi, 255), width=3)


def _dekor(frame: Any, w: int, h: int, t: float, g: dict[str, Any], pusat_badge: tuple[int, int], D: int) -> None:
    from PIL import Image, ImageDraw

    jenis = g["dekor"]
    naik = _halus(t / 0.9)
    if jenis in ("gelombang-kanan", "gelombang-kiri"):
        _gelombang(ImageDraw.Draw(frame), w, h, t, naik, g, kanan=(jenis == "gelombang-kanan"))
    elif jenis == "pita":
        lap = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(lap)
        geser = int((1 - naik) * 400)
        for i, warna in enumerate((g["dekor1"], g["dekor2"], g["aksen"])):
            y0 = h - 420 + i * 120 + geser + int(8 * math.sin(t * 1.5 + i))
            d.polygon([(0, y0 + 160), (w, y0 - 60), (w, y0 + 40), (0, y0 + 260)], fill=(*warna, 230))
        frame.alpha_composite(lap)
    elif jenis == "cincin":
        lap = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(lap)
        cx, cy = pusat_badge
        for i in range(3):
            r = D * (0.62 + i * 0.14) * (0.9 + 0.1 * naik) + 6 * math.sin(t * 1.2 + i)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(*g["aksen"], 120 - i * 30), width=4)
        frame.alpha_composite(lap)
    elif jenis == "sudut":
        lap = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(lap)
        s = int(w * 0.55 * naik)
        d.polygon([(0, 0), (s, 0), (0, s)], fill=(*g["dekor1"], 200))
        d.polygon([(w, h), (w - s, h), (w, h - s)], fill=(*g["dekor2"], 200))
        d.polygon([(0, 0), (int(s * 0.6), 0), (0, int(s * 0.6))], fill=(*g["aksen"], 160))
        frame.alpha_composite(lap)
    elif jenis == "garis":
        lap = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(lap)
        panjang = int(w * naik)
        for i, yy in enumerate((int(h * 0.08), int(h * 0.11), int(h * 0.90), int(h * 0.93))):
            x0 = 0 if i % 2 == 0 else w - panjang
            d.line([(x0, yy), (x0 + panjang, yy)], fill=(*g["aksen"], 200), width=4 if i % 2 == 0 else 2)
        frame.alpha_composite(lap)


# ============================================================
#  BADGE (logo) - lima bentuk, satu gaya bevel
# ============================================================

def _mask_bentuk(bentuk: str, n: int, r: float, bulat: float) -> Any:
    from PIL import Image, ImageDraw, ImageFilter

    c = n / 2
    m = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(m)
    if bentuk == "lingkaran":
        d.ellipse((c - r, c - r, c + r, c + r), fill=255)
        return m
    if bentuk == "kotak":
        d.rounded_rectangle((c - r, c - r, c + r, c + r), r * 0.28, fill=255)
        return m
    if bentuk == "heksagon":
        pts = [(c + r * math.cos(math.radians(60 * i - 30)), c + r * math.sin(math.radians(60 * i - 30))) for i in range(6)]
    elif bentuk == "perisai":
        pts = [(c - r, c - r * 0.85), (c + r, c - r * 0.85), (c + r, c + r * 0.15), (c, c + r), (c - r, c + r * 0.15)]
    else:  # oktagon
        pts = [(c + r * math.cos(math.radians(22.5 + 45 * i)), c + r * math.sin(math.radians(22.5 + 45 * i))) for i in range(8)]
    d.polygon(pts, fill=255)
    if bulat > 0:
        m = m.filter(ImageFilter.GaussianBlur(bulat)).point(lambda v: 255 if v >= 128 else 0)
    return m


def _badge(nama: str, diameter: int, g: dict[str, Any]) -> Any:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    D, S = diameter, 4
    n = D * S
    c = n / 2
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    def lapis(m: Any, warna: tuple) -> None:
        lap = Image.new("RGBA", (n, n), (*warna, 255))
        lap.putalpha(m)
        im.alpha_composite(lap)

    bentuk = g["bentuk"]
    bulat = c * 0.05
    bayang = _mask_bentuk(bentuk, n, c * 0.93, bulat)
    bayang = bayang.transform((n, n), Image.AFFINE, (1, 0, 0, 0, 1, -c * 0.07)).filter(ImageFilter.GaussianBlur(c * 0.09))
    lapis(bayang, (0, 0, 0))
    aksen, bingkai = g["aksen"], g["aksen2"]
    lapis(_mask_bentuk(bentuk, n, c * 0.94, c * 0.06), bingkai)
    lapis(_mask_bentuk(bentuk, n, c * 0.855, bulat), tuple(max(0, v - 25) for v in aksen))
    lapis(_mask_bentuk(bentuk, n, c * 0.815, bulat), bingkai)
    mask = _mask_bentuk(bentuk, n, c * 0.775, c * 0.045)
    lapis(mask, aksen)
    # gradien radial: terang di tengah-atas
    ys = np.arange(n, dtype=np.float32)[:, None]
    xs = np.arange(n, dtype=np.float32)[None, :]
    r2 = ((xs - c) ** 2 + (ys - c * 0.85) ** 2) / (c * 0.8) ** 2
    terang = (np.clip(1 - r2, 0, 1) ** 1.2) * (np.asarray(mask, dtype=np.float32) / 255)
    arr = np.asarray(im)
    rgb = arr[:, :, :3].astype(np.float32)
    for i, k in enumerate((44, 30, 30)):
        rgb[:, :, i] += k * terang
    keluar = np.empty_like(arr)
    keluar[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    keluar[:, :, 3] = arr[:, :, 3]
    del rgb, terang, r2
    im = Image.fromarray(keluar, "RGBA")
    kilau = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(kilau).ellipse((c * 0.25, c * 0.28, c * 1.25, c * 0.95), fill=(255, 255, 255, 70))
    kilau = kilau.filter(ImageFilter.GaussianBlur(c * 0.12))
    kilau.putalpha(Image.composite(kilau.getchannel("A"), Image.new("L", (n, n), 0), mask))
    im.alpha_composite(kilau)

    # nama channel dua baris, warna teks kontras dengan aksen
    gelap = sum(aksen) / 3 < 140
    warna_teks = (255, 255, 255, 255) if gelap or g["palet"] != "mono" else (20, 20, 20, 255)
    if g["palet"] == "mono":
        warna_teks = (20, 20, 20, 255)
    kata = nama.upper().split()
    if len(kata) >= 2:
        belah = max(1, len(kata) // 2) if len(kata) > 2 else 1
        atas, bawah = " ".join(kata[:belah]), " ".join(kata[belah:])
    else:
        s = nama.upper()
        atas, bawah = (s[: len(s) // 2], s[len(s) // 2:]) if len(s) > 6 else (s, "")
    d = ImageDraw.Draw(im)
    lebar_maks = c * (1.05 if bentuk in ("lingkaran", "heksagon") else 1.15)
    for baris, y_pusat in ((atas, c * 0.80), (bawah, c * 1.22)):
        if not baris:
            continue
        uk = int(c * 0.30)
        f = _font(uk)
        while uk > 20 and d.textbbox((0, 0), baris, font=f)[2] > lebar_maks:
            uk -= 4
            f = _font(uk)
        x0, y0, x1, y1 = d.textbbox((0, 0), baris, font=f)
        px, py = c - (x1 - x0) / 2 - x0, y_pusat - (y1 - y0) / 2 - y0
        d.text((px + 4, py + 5), baris, font=f, fill=(0, 0, 0, 150))
        d.text((px, py), baris, font=f, fill=warna_teks)
    if atas and bawah:
        d.line([(c * 0.45, c), (c * 1.55, c)], fill=(*warna_teks[:3], 230), width=int(c * 0.02))
    return im.resize((D, D), Image.LANCZOS)


def _logo_unggahan(path: str, diameter: int) -> Any:
    from PIL import Image

    im = Image.open(path).convert("RGBA")
    im.thumbnail((diameter, diameter), Image.LANCZOS)
    kanvas = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    kanvas.alpha_composite(im, ((diameter - im.width) // 2, (diameter - im.height) // 2))
    return kanvas


# ============================================================
#  IKON PLATFORM - warna merek, atau mono (putih di aksen)
# ============================================================

def _ikon(platform: str, s: int, g: dict[str, Any]) -> Any:
    import numpy as np
    from PIL import Image, ImageDraw

    S = 4
    n = s * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    mono = g["ikon"] == "mono"
    bulat_penuh = mono and g["acak"] < 0.5
    rad = n * 0.22
    mask = Image.new("L", (n, n), 0)
    if bulat_penuh:
        ImageDraw.Draw(mask).ellipse((0, 0, n - 1, n - 1), fill=255)
    else:
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, n - 1, n - 1), rad, fill=255)
    putih = (255, 255, 255, 255)

    def latar_ikon(warna: tuple) -> None:
        if bulat_penuh:
            d.ellipse((0, 0, n - 1, n - 1), fill=(*warna, 255))
        else:
            d.rounded_rectangle((0, 0, n - 1, n - 1), rad, fill=(*warna, 255))

    if platform == "instagram" and not mono:
        ys = np.linspace(0, 1, n, dtype=np.float32)[:, None]
        xs = np.linspace(0, 1, n, dtype=np.float32)[None, :]
        u = np.clip(0.6 * ys + 0.4 * (1 - xs), 0, 1)[:, :, None]
        a, m, b = (np.array(v, np.float32) for v in ((255, 190, 60), (225, 45, 110), (110, 45, 200)))
        arr = np.where(u < 0.5, a + (m - a) * (u / 0.5), m + (b - m) * ((u - 0.5) / 0.5))
        bg = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
        bg.putalpha(mask)
        im = bg
        d = ImageDraw.Draw(im)
    else:
        warna_bg = g["aksen"] if mono else {
            "instagram": (225, 45, 110), "youtube": (255, 0, 0), "facebook": (24, 119, 242),
            "tiktok": (0, 0, 0), "x": (0, 0, 0), "threads": (0, 0, 0)}[platform]
        if mono and g["palet"] == "mono":
            warna_bg = (240, 240, 240)
            putih = (20, 20, 20, 255)
        latar_ikon(warna_bg)

    if platform == "instagram":
        p = n * 0.2
        d.rounded_rectangle((p, p, n - p, n - p), n * 0.14, outline=putih, width=int(n * 0.075))
        r = n * 0.16
        d.ellipse((n / 2 - r, n / 2 - r, n / 2 + r, n / 2 + r), outline=putih, width=int(n * 0.075))
        q = n * 0.045
        d.ellipse((n * 0.70 - q, n * 0.30 - q, n * 0.70 + q, n * 0.30 + q), fill=putih)
    elif platform == "youtube":
        p = n * 0.2
        d.rounded_rectangle((p, n * 0.28, n - p, n * 0.72), n * 0.12, fill=putih)
        isi = (*g["aksen"], 255) if mono else (255, 0, 0, 255)
        if mono and g["palet"] == "mono":
            isi = (240, 240, 240, 255)
        d.polygon([(n * 0.43, n * 0.38), (n * 0.43, n * 0.62), (n * 0.63, n * 0.5)], fill=isi)
    elif platform == "facebook":
        f = _font(int(n * 0.86))
        x0, y0, x1, y1 = d.textbbox((0, 0), "f", font=f)
        d.text((n * 0.56 - (x1 - x0) / 2 - x0, n * 0.62 - (y1 - y0) / 2 - y0), "f", font=f, fill=putih)
    elif platform == "tiktok":
        lapisan = (((37, 244, 238, 255), -n * 0.02, -n * 0.02), ((254, 44, 85, 255), n * 0.02, n * 0.02), (putih, 0, 0)) \
            if not mono else ((putih, 0, 0),)
        for warna, dx, dy in lapisan:
            bx, by = n * 0.40 + dx, n * 0.66 + dy
            r = n * 0.12
            d.ellipse((bx - r, by - r, bx + r, by + r), fill=warna)
            d.rectangle((bx + r * 0.55, n * 0.20 + dy, bx + r * 0.55 + n * 0.075, by), fill=warna)
            d.arc((bx + r * 0.55 - n * 0.02, n * 0.18 + dy, bx + n * 0.42, n * 0.48 + dy), start=270, end=30, fill=warna, width=int(n * 0.075))
    elif platform == "x":
        lw = int(n * 0.085)
        d.line([(n * 0.25, n * 0.25), (n * 0.75, n * 0.75)], fill=putih, width=lw)
        d.line([(n * 0.75, n * 0.25), (n * 0.25, n * 0.75)], fill=putih, width=lw)
    else:
        f = _font(int(n * 0.78))
        x0, y0, x1, y1 = d.textbbox((0, 0), "@", font=f)
        d.text((n / 2 - (x1 - x0) / 2 - x0, n * 0.52 - (y1 - y0) / 2 - y0), "@", font=f, fill=putih)

    if not (platform == "instagram" and not mono):
        im.putalpha(Image.composite(im.getchannel("A"), Image.new("L", (n, n), 0), mask))
    return im.resize((s, s), Image.LANCZOS)


# ============================================================
#  TATA LETAK
# ============================================================

def _susun(w: int, h: int, g: dict[str, Any], isi: list[tuple[str, str]], pakai_nama_teks: bool) -> dict[str, Any]:
    """Posisi badge, nama (teks), dan tiap baris handle untuk tata letak gaya ini."""
    tata = g["tata"]
    n = len(isi)
    L: dict[str, Any] = {"baris": []}
    if tata == "dua-kolom":
        D = int(w * 0.40)
        L["badge"] = (int(w * 0.06), int(h * 0.30), D)
        ikon = int(w * 0.075)
        pitch = int(h * 0.062)
        x0 = int(w * 0.52)
        y0 = int(h * 0.30) + (D - n * pitch) // 2
        for i in range(n):
            L["baris"].append({"ikon": (x0, y0 + i * pitch), "teks": (x0 + ikon + int(w * 0.02), y0 + i * pitch), "ikon_s": ikon, "font": int(w * 0.040)})
        L["nama"] = (w // 2, int(h * 0.30) + D + int(h * 0.05))
    elif tata == "grid":
        D = int(w * 0.44)
        L["badge"] = ((w - D) // 2, int(h * 0.14), D)
        ikon = int(w * 0.07)
        kol_w = int(w * 0.44)
        x_kol = [int(w * 0.05), int(w * 0.51)]
        y0 = int(h * 0.14) + D + int(h * 0.09)
        pitch = int(h * 0.075)
        for i in range(n):
            xk, yk = x_kol[i % 2], y0 + (i // 2) * pitch
            L["baris"].append({"pil": (xk, yk, kol_w, int(h * 0.058)), "ikon": (xk + 12, yk + 6), "teks": (xk + 12 + ikon + 14, yk + 6), "ikon_s": ikon, "font": int(w * 0.034)})
        L["nama"] = (w // 2, int(h * 0.14) + D + int(h * 0.03))
    elif tata == "baris-ikon":
        D = int(w * 0.48)
        L["badge"] = ((w - D) // 2, int(h * 0.16), D)
        ikon = int(w * 0.11)
        per_baris = 3
        gap = int(w * 0.30)
        x0 = w // 2 - gap
        y0 = int(h * 0.16) + D + int(h * 0.10)
        for i in range(n):
            xk = x0 + (i % per_baris) * gap - ikon // 2
            yk = y0 + (i // per_baris) * int(h * 0.15)
            L["baris"].append({"ikon": (xk, yk), "teks_tengah": (xk + ikon // 2, yk + ikon + 10), "ikon_s": ikon, "font": int(w * 0.030)})
        L["nama"] = (w // 2, int(h * 0.16) + D + int(h * 0.035))
    elif tata == "tengah":
        D = int(w * 0.50)
        L["badge"] = ((w - D) // 2, int(h * 0.15), D)
        ikon = int(w * 0.075)
        pitch = int(h * 0.062)
        y0 = int(h * 0.15) + D + int(h * 0.09)
        for i in range(n):
            L["baris"].append({"tengah": True, "y": y0 + i * pitch, "ikon_s": ikon, "font": int(w * 0.044)})
        L["nama"] = (w // 2, int(h * 0.15) + D + int(h * 0.03))
    else:  # acuan
        D = int(w * 0.56)
        L["badge"] = ((w - D) // 2, int(h * 0.17), D)
        ikon = int(w * 0.082)
        pitch = int(h * 0.062) if n <= 4 else int(h * 0.055)
        y0 = int(h * 0.17) + D + int(h * 0.11)
        for i in range(n):
            L["baris"].append({"ikon": (int(w * 0.22), y0 + i * pitch), "teks": (int(w * 0.22) + ikon + int(w * 0.03), y0 + i * pitch), "ikon_s": ikon, "font": int(w * 0.050)})
        L["nama"] = (w // 2, int(h * 0.17) + D + int(h * 0.035))
    L["pakai_nama_teks"] = pakai_nama_teks
    return L


# ============================================================
#  RENDER
# ============================================================

def render_video(job: dict[str, Any], g: dict[str, Any], folder: Path, ev: threading.Event, lapor: Callable[[int], None]) -> Path:
    from PIL import Image, ImageDraw

    w = VIDEO_W - VIDEO_W % 2
    h = VIDEO_H - VIDEO_H % 2
    channel = job["channel"]
    isi = [(k, job["akun"][k]) for k in KUNCI_AKUN if job["akun"].get(k)]
    durasi = float(g["durasi"])
    huruf = (lambda s: s.upper()) if g["huruf_besar"] else (lambda s: s)

    latar = _latar(w, h, g)
    pakai_logo = bool(job.get("logo")) and Path(job["logo"]).is_file()
    L = _susun(w, h, g, isi, pakai_nama_teks=pakai_logo or g["tata"] in ("grid", "baris-ikon", "dua-kolom"))
    bx, by, D = L["badge"]
    badge = _logo_unggahan(job["logo"], D) if pakai_logo else _badge(channel, D, g)
    pusat_badge = (bx + D // 2, by + D // 2)
    nama_im = _teks_gambar(huruf(channel), int(w * 0.058), (*g["aksen2"][:3], 255) if g["palet"] != "mono" else (240, 240, 240, 255), miring=g["miring"]) if L["pakai_nama_teks"] else None

    warna_teks = (245, 245, 245, 255) if g["palet"] != "mono" else (235, 235, 235, 255)
    baris_ims = []
    for (k, nilai), b in zip(isi, L["baris"]):
        ik = _ikon(k, b["ikon_s"], g)
        tx = _teks_gambar(huruf(nilai) if g["huruf_besar"] and g["acak"] < 0.3 else nilai, b["font"], warna_teks)
        baris_ims.append((ik, tx, b))

    t_badge, t_handle = 0.35, 1.6
    total = max(1, int(FPS * durasi))
    keluaran = folder / "video-tanpa-audio.mp4"
    berkas_log = folder / "ffmpeg.log"
    utas = os.getenv("VIDEO_THREADS", "1") or "1"
    if utas == "0":
        utas = "1"
    galat = berkas_log.open("w", encoding="utf-8")
    proses = subprocess.Popen(
        [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", utas,
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
         "-threads", utas, "-movflags", "+faststart", str(keluaran)],
        stdin=subprocess.PIPE, stderr=galat,
    )

    def _sebab_mati() -> str:
        try:
            if not galat.closed:
                galat.flush()
            baris = [b for b in berkas_log.read_text(encoding="utf-8", errors="replace").splitlines() if b.strip()]
        except (OSError, ValueError):
            baris = []
        if proses.returncode == -9:
            return "ffmpeg dihentikan paksa oleh sistem (SIGKILL) - hampir pasti kehabisan memori di kontainer."
        return f"ffmpeg keluar dengan kode {proses.returncode}: {baris[-1][:200]}" if baris else f"ffmpeg keluar dengan kode {proses.returncode} tanpa pesan."

    cache: dict[str, Any] = {}
    try:
        lapor_tiap = max(1, total // 12)
        for n in range(total):
            if ev.is_set():
                proses.kill()
                raise Dibatalkan("Render dihentikan.")
            t = n / FPS
            frame = latar.copy()
            _gerak_latar(frame, w, h, t, g, cache)
            _dekor(frame, w, h, t, g, pusat_badge, D)

            # ----- badge -----
            if t >= t_badge:
                u = (t - t_badge) / 0.8
                a = _clamp(u / 0.35)
                apung = int(5 * math.sin(t * 1.8))
                jenis = g["masuk_badge"]
                if jenis == "lenting":
                    sk = max(0.02, _lenting(u)); bimg = _skala(badge, sk); dy = 0
                elif jenis == "turun":
                    sk = 1.0; bimg = badge; dy = int(-(1 - _halus(u)) * h * 0.25)
                elif jenis == "putar":
                    sk = max(0.05, _halus(u)); bimg = _skala(badge, sk).rotate((1 - _halus(u)) * 180, resample=Image.BICUBIC, expand=False); dy = 0
                else:  # zoom
                    sk = 1.35 - 0.35 * _halus(u); bimg = _skala(badge, sk); dy = 0
                _tempel(frame, bimg, bx + (D - bimg.width) // 2, by + (D - bimg.height) // 2 + apung + dy, a)

            # ----- nama (teks) -----
            if nama_im is not None and t >= t_badge + 0.6:
                a = _halus((t - t_badge - 0.6) / 0.5)
                nx, ny = L["nama"]
                _tempel(frame, nama_im, nx - nama_im.width // 2, ny + int(24 * (1 - a)), a)

            # ----- handle -----
            for i, (ik, tx, b) in enumerate(baris_ims):
                mulai = t_handle + i * 0.13
                if t < mulai:
                    continue
                u = (t - mulai) / 0.45
                a = _halus(u)
                jenis = g["masuk_handle"]
                dx = int(-70 * (1 - a)) if jenis == "kiri" else 0
                dy = int(40 * (1 - a)) if jenis == "atas" else 0
                sk = max(0.05, _lenting(u)) if jenis == "pop" else 1.0
                ikx = _skala(ik, sk) if sk != 1.0 else ik
                if "pil" in b:
                    px, py, pw, ph = b["pil"]
                    lap = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
                    ImageDraw.Draw(lap).rounded_rectangle((0, 0, pw - 1, ph - 1), ph // 2, fill=(*g["aksen"][:3], 60), outline=(*g["aksen2"][:3], 140), width=2)
                    _tempel(frame, lap, px + dx, py + dy, a)
                if b.get("tengah"):
                    # Satu tepi kiri untuk SEMUA baris, dari baris terlebar -
                    # kalau tiap baris dipusatkan sendiri, ikon baris pendek
                    # (mis. "@tvrakyat") melompat ke kanan dan kolomnya bergerigi.
                    if "lebar_tengah" not in cache:
                        cache["lebar_tengah"] = max(i2.width + 18 + t2.width for i2, t2, _ in baris_ims)
                    x0 = w // 2 - cache["lebar_tengah"] // 2
                    _tempel(frame, ikx, x0 + dx + (ik.width - ikx.width) // 2, b["y"] + dy + (ik.height - ikx.height) // 2, a)
                    _tempel(frame, tx, x0 + ik.width + 18 + dx, b["y"] + dy + (ik.height - tx.height) // 2, a)
                elif "teks_tengah" in b:
                    ix, iy = b["ikon"]
                    _tempel(frame, ikx, ix + dx + (ik.width - ikx.width) // 2, iy + dy + (ik.height - ikx.height) // 2, a)
                    cx, cy = b["teks_tengah"]
                    _tempel(frame, tx, cx - tx.width // 2, cy + dy, a)
                else:
                    ix, iy = b["ikon"]
                    txx, txy = b["teks"]
                    _tempel(frame, ikx, ix + dx + (ik.width - ikx.width) // 2, iy + dy + (ik.height - ikx.height) // 2, a)
                    _tempel(frame, tx, txx + dx, txy + dy + (ik.height - tx.height) // 2, a)

            try:
                proses.stdin.write(frame.convert("RGB").tobytes())
            except BrokenPipeError:
                break
            if n % lapor_tiap == 0:
                lapor(int(n / total * 100))
    finally:
        if proses.stdin:
            try:
                proses.stdin.close()
            except OSError:
                pass
        proses.wait()
        galat.close()
    if proses.returncode != 0 or not keluaran.is_file():
        raise OutroError(f"Gagal merender video outro: {_sebab_mati()}")
    lapor(100)
    return keluaran


def tempel_audio(video: Path, folder: Path, detik: float) -> Path:
    keluaran = folder / "outro.mp4"
    if not DENTING.is_file():
        shutil.copy2(video, keluaran)
        return keluaran
    tunda = int(detik * 1000)
    utas = os.getenv("VIDEO_THREADS", "1") or "1"
    perintah = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", utas,
        "-i", str(video), "-i", str(DENTING),
        "-filter_complex", f"[1:a]adelay={tunda}|{tunda},apad[a]",
        "-map", "0:v", "-map", "[a]", "-shortest",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(keluaran),
    ]
    hasil = subprocess.run(perintah, capture_output=True, text=True, timeout=120)
    if hasil.returncode != 0 or not keluaran.is_file():
        logger.warning("audio gagal ditempel, video tanpa audio dipakai: %s", (hasil.stderr or "").strip()[-200:])
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
