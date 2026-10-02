"""Auto Outro: dari nama channel + akun sosmed sampai video outro - dan tiap
kali dibuat, tampilannya berbeda jauh.

Kebutuhannya 100 outro sehari yang tidak boleh terasa sama, dengan tiga hal
yang selalu ada: logo (badge), nama TV, dan enam sosmed dengan logo merek
yang asli. Yang dibangun bukan tumpukan berkas aset, melainkan SISTEM GAYA
BERSUMBU - tiap outro memilih satu nilai di tiap sumbu, seedd dari job:

  palet (44, dari roda warna) x latar (14) x dekorasi (14) x bentuk badge (9)
  x tata letak (10) x gaya ikon (4) x font badge (7) x font teks (7)
  x animasi badge (6) x animasi handle (6) x glow badge x gerak latar
  x huruf besar x miring x durasi

Tiap sumbu mengubah bentuk, susunan, gerakan, atau tipografi - bukan sekadar
menggeser warna - jadi dua outro yang berbeda seed nyaris mustahil terasa
sama.

Ikon platform memakai glyph merek resmi (Simple Icons, CC0) yang sudah
dirasterisasi ke PNG di assets/outro/ikon, jadi Instagram, YouTube,
Facebook, TikTok, X, dan Threads tampak seperti logo aslinya - bukan
perkiraan gambar tangan. Font dari Google Fonts (OFL) di assets/outro/font.

Semuanya dirender sendiri dengan numpy + Pillow + ffmpeg: tanpa AI, tanpa
API, tanpa jatah. Teks digambar sendiri, jadi username muncul PERSIS. Setiap
perintah ffmpeg membawa -threads (kontainer server 1 GB).
"""

from __future__ import annotations

import colorsys
import json
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

import storage
import resource_queue
from video_edit import FFMPEG_BIN, FONT_BUNDLED, MEDIA_DIR, ruang_media

logger = logging.getLogger(__name__)

KUNCI_AKUN = ("instagram", "youtube", "facebook", "tiktok", "x", "threads")
NAMA_TAMPIL = {
    "instagram": "Instagram", "tiktok": "TikTok", "x": "X",
    "threads": "Threads", "facebook": "Facebook", "youtube": "YouTube",
}
WARNA_MEREK = {
    "instagram": (225, 48, 108), "youtube": (255, 0, 0), "facebook": (24, 119, 242),
    "tiktok": (0, 0, 0), "x": (0, 0, 0), "threads": (0, 0, 0),
}

VIDEO_W = int(os.getenv("OUTRO_VIDEO_W", "1080"))
VIDEO_H = int(os.getenv("OUTRO_VIDEO_H", "1920"))
FPS = int(os.getenv("OUTRO_FPS", "30"))
ASET = Path(__file__).resolve().parent / "assets" / "outro"
DENTING = ASET / "denting.m4a"
FONT = {
    "poppins": Path(FONT_BUNDLED),
    "montserrat": ASET / "font" / "Montserrat.ttf",
    "oswald": ASET / "font" / "Oswald.ttf",
    "bebas": ASET / "font" / "BebasNeue.ttf",
    "anton": ASET / "font" / "Anton.ttf",
    "righteous": ASET / "font" / "Righteous.ttf",
    "archivo": ASET / "font" / "ArchivoBlack.ttf",
}


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

# Hasil outro disimpan di Supabase, bukan di volume backend (433 MB, dulu
# penuh oleh hasil outro). Daftar pekerjaan yang selesai ikut disimpan
# sebagai satu indeks di sana supaya riwayatnya bertahan melewati restart.
# Backend di laptop (DEBUG) memakai bucket yang SAMA dengan server; tanpa
# awalan sendiri, indeksnya akan menimpa riwayat outro milik server.
_LOKAL = os.getenv("DEBUG", "").strip().lower() in ("1", "true", "yes")
AWALAN_STORAGE = os.getenv("OUTRO_STORAGE_AWALAN", "outro-lokal" if _LOKAL else "outro").strip("/")
INDEKS_STORAGE = f"{AWALAN_STORAGE}/indeks.json"
INDEKS_MAKS = 200  # cukup untuk satu kiriman banyak channel sekaligus
_indeks_dimuat = False
_kunci_indeks = threading.Lock()


def _ringkasan_simpan(job: dict[str, Any]) -> dict[str, Any]:
    """Salinan job untuk indeks: tanpa jalur disk yang tak berlaku di proses lain."""
    salinan = {k: v for k, v in job.items() if k != "video"}
    salinan["logs"] = list(job.get("logs", []))[-20:]
    return salinan


def _muat_indeks() -> None:
    """Sekali per proses: kembalikan riwayat outro dari Supabase ke memori."""
    global _indeks_dimuat
    if _indeks_dimuat:
        return
    with _kunci_indeks:
        if _indeks_dimuat:
            return
        _indeks_dimuat = True
        if not storage.enabled():
            return
        tujuan = outro_dir() / "indeks.json"
        try:
            tujuan.parent.mkdir(parents=True, exist_ok=True)
            storage.download(INDEKS_STORAGE, tujuan)
            daftar = json.loads(tujuan.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - belum ada indeks / Supabase mati
            logger.info("Indeks outro belum bisa dimuat: %s", error)
            return
        finally:
            tujuan.unlink(missing_ok=True)
        with _kunci:
            for job in daftar if isinstance(daftar, list) else []:
                if isinstance(job, dict) and job.get("job_id") and job["job_id"] not in _jobs:
                    _jobs[job["job_id"]] = job


def _simpan_indeks() -> None:
    """Tulis ulang indeks: job yang sudah tersimpan di Supabase, terbaru dulu."""
    with _kunci:
        tersimpan = [
            _ringkasan_simpan(j) for j in _jobs.values() if j.get("storage_path")
        ]
    tersimpan.sort(key=lambda j: j.get("created", 0), reverse=True)
    storage.upload(
        INDEKS_STORAGE,
        json.dumps(tersimpan[:INDEKS_MAKS], ensure_ascii=False).encode("utf-8"),
        "application/json",
    )


def _kirim_ke_storage(job_id: str, hasil: Path) -> str:
    """Unggah hasil ke Supabase; kembalikan jalurnya, atau "" kalau gagal."""
    if not storage.enabled():
        return ""
    jalur = f"{AWALAN_STORAGE}/{job_id}/outro.mp4"
    try:
        storage.upload(jalur, hasil, "video/mp4")
    except Exception as error:  # noqa: BLE001 - hasil tetap ada di disk
        logger.warning("Outro %s gagal disimpan ke Supabase: %s", job_id, error)
        return ""
    return jalur


def _catat_indeks() -> None:
    try:
        with _kunci_indeks:
            _simpan_indeks()
    except Exception as error:  # noqa: BLE001 - videonya sudah aman; indeks menyusul
        logger.warning("Indeks outro gagal diperbarui: %s", error)


def ambil_video(job_id: str) -> tuple[bytes | Path, str] | None:
    """Berkas hasil (Path di disk) atau isinya dari Supabase, plus nama channel."""
    job = baca_job(job_id)
    if job is None:
        return None
    channel = str(job.get("channel") or "outro")
    lokal = Path(job["video"]) if job.get("video") else None
    if lokal is not None and lokal.is_file():
        return lokal, channel
    jalur = job.get("storage_path")
    if not jalur or not storage.enabled():
        return None
    tujuan = outro_dir() / f"unduh-{job_id}.mp4"
    try:
        tujuan.parent.mkdir(parents=True, exist_ok=True)
        storage.download(jalur, tujuan)
        return tujuan.read_bytes(), channel
    except Exception as error:  # noqa: BLE001
        logger.warning("Outro %s gagal diambil dari Supabase: %s", job_id, error)
        return None
    finally:
        tujuan.unlink(missing_ok=True)


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
    _muat_indeks()
    with _kunci:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def daftar_job(batas: int = 20) -> list[dict[str, Any]]:
    _muat_indeks()
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


MODE_SAH = ("biasa", "dpp")


def mulai_job(
    channel: str,
    akun: dict[str, str],
    seed: int | None = None,
    logo_path: str | None = None,
    mode: str = "biasa",
    ticket_id: str | None = None,
) -> str:
    channel = " ".join(str(channel or "").split())
    if not channel:
        raise OutroError("Nama channel wajib diisi.")
    mode = str(mode or "biasa").strip().lower()
    if mode not in MODE_SAH:
        raise OutroError(f"Mode '{mode}' tidak dikenal.")
    job_id = uuid.uuid4().hex[:12]
    ticket = ticket_id or resource_queue.buat_tiket("developer", "Auto Outro")
    if seed is None:
        # Dari 12 digit heksa job_id langsung. Dulu di-pad kanan menjadi UUID
        # lalu diambil 31 bit terendahnya - yang semuanya nol - sehingga tiap
        # outro "acak" memakai seed 0 dan gaya yang sama.
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
            "mode": mode,
            # Mode DPP tidak memakai sumbu gaya: tampilannya tetap, meniru
            # outro TV Rakyat, yang berganti hanya nama channel di logo dan
            # daftar sosmednya.
            "gaya": ringkas_gaya(gaya) if mode == "biasa" else "DPP - meniru outro TV Rakyat",
            "logo": logo_path or "",
            "video": "",
            "message": "Menunggu giliran.",
            "logs": ["Pekerjaan masuk antrean."],
            "created": time.time(),
            "updated": time.time(),
        }
    _batal[job_id] = threading.Event()
    threading.Thread(target=_jalankan, args=(job_id, ticket), name=f"outro-{job_id}", daemon=True).start()
    return job_id


# Satu outro butuh ~2x ukuran hasilnya selama render (mentah + hasil).
RUANG_OUTRO_MB = float(os.getenv("OUTRO_RUANG_MIN_MB", "40"))


def _jalankan(job_id: str, ticket: str) -> None:
    ev = _batal[job_id]
    folder = outro_dir() / job_id
    with resource_queue.giliran(ticket), _sedang_jalan:
        try:
            job = baca_job(job_id) or {}
            if ev.is_set():
                raise Dibatalkan("Dihentikan sebelum mulai.")
            # Dulu mkdir ada DI LUAR try: saat disk penuh ia melempar OSError,
            # thread mati diam-diam, dan job selamanya "Menunggu giliran".
            try:
                sisa = float(ruang_media().get("sisa_mb", RUANG_OUTRO_MB))
            except Exception:  # noqa: BLE001 - pengukuran gagal bukan alasan menolak
                sisa = RUANG_OUTRO_MB
            if sisa < RUANG_OUTRO_MB:
                raise OutroError(
                    f"Penyimpanan server penuh (sisa {sisa:.0f} MB). "
                    "Hubungi developer untuk membersihkan volume, lalu coba lagi."
                )
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise OutroError(f"Tidak bisa menyiapkan folder outro: {error}") from error
            lapor = lambda p: _catat(job_id, f"Merender outro ... {p}%", progress=5 + int(p * 0.85))
            _catat(job_id, f"Merender outro ({job['gaya']}) ...",
                   status="running", langkah="render", progress=5)
            if job.get("mode") == "dpp":
                mentah = render_dpp(job, folder, ev, lapor)
            else:
                gaya = pilih_gaya(int(job["seed"]))
                mentah = render_video(job, gaya, folder, ev, lapor)
            if ev.is_set():
                raise Dibatalkan("Dihentikan sebelum menempel audio.")
            _catat(job_id, "Menempel audio ...", langkah="audio", progress=92)
            if job.get("mode") == "dpp":
                hasil = tempel_audio_dpp(mentah, folder)
            else:
                hasil = tempel_audio(mentah, folder, gaya["denting_detik"])
            if mentah != hasil:
                # Bahan mentah tanpa audio tak dipakai lagi; dulu menumpuk
                # di volume dan menggandakan pemakaian disk tiap outro.
                mentah.unlink(missing_ok=True)
            ukuran = hasil.stat().st_size / 1_048_576
            _catat(job_id, "Menyimpan hasil ke Supabase ...", langkah="simpan", progress=96)
            jalur = _kirim_ke_storage(job_id, hasil)
            if jalur:
                _catat(job_id, f"Video outro siap ({ukuran:.1f} MB).",
                       status="done", langkah="selesai", progress=100, storage_path=jalur)
                _catat_indeks()
                shutil.rmtree(folder, ignore_errors=True)
            else:
                _catat(job_id, f"Video outro siap ({ukuran:.1f} MB). Supabase tidak bisa "
                       "dipakai, jadi hasilnya hanya disimpan sementara di server (24 jam).",
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

def _rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    # Dipanggil saat modul dimuat (PALET dibangun di atas), sebelum _clamp
    # didefinisikan di bawah - jadi pembatasannya ditulis langsung di sini.
    s = max(0.0, min(1.0, s))
    v = max(0.0, min(1.0, v))
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _buat_palet() -> list[dict[str, Any]]:
    """44 palet: 15 rona x dua watak (pekat & lembut), plus mono & aksen ganda."""
    palet: list[dict[str, Any]] = []
    nama_rona = ["merah", "jingga", "kuning", "lemon", "hijau", "zamrud", "teal", "sian",
                 "biru", "laut", "nila", "ungu", "magenta", "merah-muda", "marun"]
    for i, nama in enumerate(nama_rona):
        h = i / len(nama_rona)
        # pekat: latar gelap berona, aksen jenuh, bingkai terang
        palet.append({
            "nama": f"{nama} pekat", "atas": _rgb(h, 0.55, 0.07), "bawah": _rgb(h, 0.6, 0.2),
            "aksen": _rgb(h, 0.85, 0.85), "aksen2": _rgb(h, 0.08, 0.96), "dekor1": _rgb(h, 0.7, 0.45),
            "dekor2": _rgb(h, 0.75, 0.65), "glow": _rgb(h, 0.7, 0.3), "teks": (245, 245, 245),
        })
        # lembut: latar hampir hitam netral, aksen pastel, bingkai gelap
        h2 = (h + 0.5) % 1.0
        palet.append({
            "nama": f"{nama} lembut", "atas": (9, 9, 12), "bawah": _rgb(h, 0.35, 0.16),
            "aksen": _rgb(h, 0.45, 0.95), "aksen2": _rgb(h, 0.6, 0.35), "dekor1": _rgb(h2, 0.5, 0.35),
            "dekor2": _rgb(h, 0.4, 0.5), "glow": _rgb(h, 0.5, 0.25), "teks": (240, 240, 240),
        })
    palet += [
        {"nama": "mono putih", "atas": (8, 8, 8), "bawah": (26, 26, 26), "aksen": (242, 242, 242), "aksen2": (30, 30, 30),
         "dekor1": (60, 60, 60), "dekor2": (120, 120, 120), "glow": (36, 36, 36), "teks": (236, 236, 236)},
        {"nama": "mono arang", "atas": (16, 16, 18), "bawah": (40, 40, 44), "aksen": (70, 70, 76), "aksen2": (220, 220, 225),
         "dekor1": (90, 90, 96), "dekor2": (140, 140, 150), "glow": (50, 50, 56), "teks": (240, 240, 240)},
        {"nama": "emas hitam", "atas": (10, 8, 4), "bawah": (30, 24, 8), "aksen": (214, 170, 50), "aksen2": (250, 240, 210),
         "dekor1": (120, 90, 20), "dekor2": (200, 160, 60), "glow": (60, 44, 10), "teks": (250, 244, 225)},
        {"nama": "perak biru", "atas": (8, 12, 20), "bawah": (24, 34, 52), "aksen": (190, 210, 235), "aksen2": (20, 30, 48),
         "dekor1": (60, 84, 120), "dekor2": (130, 160, 200), "glow": (30, 44, 70), "teks": (236, 242, 250)},
    ]
    return palet


PALET = _buat_palet()
LATAR = ("gradien", "sapuan", "partikel", "titik", "sinar", "gumpal", "garis-miring", "heks",
         "butir", "radial", "pita-halus", "sorot", "kotak", "bokeh")
DEKOR = ("gelombang-kanan", "gelombang-kiri", "pita", "cincin", "sudut", "garis", "tanpa", "bingkai",
         "segitiga", "bar", "lingkaran", "diagonal-ganda", "busur", "titik-sudut")
BENTUK = ("oktagon", "lingkaran", "kotak", "heksagon", "perisai", "berlian", "pil", "lencana", "bintang")
TATA = ("acuan", "tengah", "grid", "dua-kolom", "baris-ikon", "kiri-atas", "bawah", "kolom-kanan", "melingkar", "dua-baris")
IKON = ("warna", "mono", "garis", "lingkaran")
MASUK_BADGE = ("lenting", "turun", "putar", "zoom", "kiri", "kedip")
MASUK_HANDLE = ("kiri", "atas", "pop", "fade", "kanan", "ketik")
GERAK_LATAR = ("diam", "zoom", "geser")
NAMA_FONT = tuple(FONT)
# Font di luar undian gaya acak (dipakai mode DPP).
FONT_LAIN = {"poppins-regular": ASET / "font" / "Poppins-Regular.ttf"}


def pilih_gaya(seed: int) -> dict[str, Any]:
    """Satu nilai di tiap sumbu, ditentukan seed - sama seed, sama tampilan."""
    rnd = random.Random(seed)
    p = rnd.choice(PALET)
    return {
        "seed": seed, **p, "palet": p["nama"],
        "latar": rnd.choice(LATAR), "dekor": rnd.choice(DEKOR), "bentuk": rnd.choice(BENTUK),
        "tata": rnd.choice(TATA), "ikon": rnd.choice(IKON),
        "font_badge": rnd.choice(NAMA_FONT), "font_teks": rnd.choice(NAMA_FONT),
        "masuk_badge": rnd.choice(MASUK_BADGE), "masuk_handle": rnd.choice(MASUK_HANDLE),
        "gerak_latar": rnd.choice(GERAK_LATAR), "glow_badge": rnd.random() < 0.5,
        "huruf_besar": rnd.random() < 0.55, "miring": rnd.random() < 0.3,
        "durasi": rnd.choice((4.5, 5.0, 5.5, 6.0, 6.5)),
        "sudut_sapuan": rnd.uniform(math.radians(18), math.radians(62)),
        "denting_detik": 1.9, "acak": rnd.random(), "acak2": rnd.random(),
    }


def ringkas_gaya(g: dict[str, Any]) -> str:
    return (f"palet {g['palet']} · latar {g['latar']} · dekor {g['dekor']} · badge {g['bentuk']} · "
            f"tata {g['tata']} · ikon {g['ikon']} · font {g['font_badge']}/{g['font_teks']} · "
            f"masuk {g['masuk_badge']}/{g['masuk_handle']}")


# ============================================================
#  ALAT GAMBAR
# ============================================================

_FONT_CACHE: dict[tuple[str, int], Any] = {}


def _font(ukuran: int, nama: str = "poppins") -> Any:
    from PIL import ImageFont

    kunci = (nama, ukuran)
    if kunci in _FONT_CACHE:
        return _FONT_CACHE[kunci]
    jalur = FONT.get(nama) or FONT_LAIN.get(nama) or FONT["poppins"]
    try:
        f = ImageFont.truetype(str(jalur), ukuran)
        try:
            nama_var = [n.decode() if isinstance(n, bytes) else n for n in f.get_variation_names()]
            if "Bold" in nama_var:
                f.set_variation_by_name("Bold")
        except Exception:  # noqa: BLE001 - font statis tidak punya variasi
            pass
    except Exception:  # noqa: BLE001
        f = ImageFont.truetype(FONT_BUNDLED, ukuran)
    _FONT_CACHE[kunci] = f
    return f


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _halus(t: float) -> float:
    t = _clamp(t)
    return t * t * (3 - 2 * t)


def _lenting(t: float) -> float:
    t = _clamp(t)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _teks_gambar(teks: str, ukuran: int, warna: tuple, miring: bool = False, nama_font: str = "poppins") -> Any:
    from PIL import Image, ImageDraw

    font = _font(ukuran, nama_font)
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


def _lebih_terang(c: tuple, k: int) -> tuple:
    return tuple(min(255, v + k) for v in c[:3])


# ============================================================
#  LATAR (statis per gaya) + GERAKAN LATAR (per frame)
# ============================================================

def _latar(w: int, h: int, g: dict[str, Any]) -> Any:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    ys = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    xs = np.linspace(0, 1, w, dtype=np.float32)[None, :]
    atas = np.array(g["atas"], np.float32)
    bawah = np.array(g["bawah"], np.float32)
    if g["latar"] == "radial":
        u = np.clip(np.sqrt((xs - 0.5) ** 2 * 0.6 + (ys - 0.42) ** 2) * 1.6, 0, 1)
        dasar = bawah + (atas - bawah) * u[:, :, None]
    else:
        u = ys if g["acak"] < 0.5 else np.clip(0.7 * ys + 0.3 * xs, 0, 1)
        u = np.broadcast_to(u, (h, w))[:, :, None]
        dasar = atas + (bawah - atas) * u
    sx, sy = (0.88, 0.94) if g["acak"] < 0.5 else (0.12, 0.08)
    d2 = (xs - sx) ** 2 * 1.1 + (ys - sy) ** 2 * 1.6
    dasar = dasar + np.exp(-d2 / 0.22)[:, :, None] * np.array(g["glow"], np.float32)
    vx, vy = (xs - 0.5) * 2, (ys - 0.5) * 2
    dasar *= (1.0 - 0.18 * np.clip(vx ** 2 + vy ** 2, 0, 1))[:, :, None]
    if g["latar"] == "butir":
        rng = np.random.default_rng(g["seed"])
        dasar += rng.normal(0, 9, size=(h, w, 1)).astype(np.float32)
    im = Image.fromarray(np.clip(dasar, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

    lap = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(lap)
    jenis = g["latar"]
    if jenis == "titik":
        c = (*g["dekor2"], 70)
        for yy in range(28, h, 56):
            for xx in range(28, w, 56):
                d.ellipse((xx - 3, yy - 3, xx + 3, yy + 3), fill=c)
    elif jenis == "garis-miring":
        for k in range(-h, w + h, 90):
            d.line([(k, 0), (k + h, h)], fill=(*g["dekor1"], 55), width=3)
    elif jenis == "heks":
        r = 54
        for j, yy in enumerate(range(0, h + r, int(r * 1.5))):
            for xx in range(0, w + r, int(r * 1.732)):
                cx = xx + (r * 0.866 if j % 2 else 0)
                pts = [(cx + r * 0.9 * math.cos(math.radians(60 * i + 30)), yy + r * 0.9 * math.sin(math.radians(60 * i + 30))) for i in range(6)]
                d.polygon(pts, outline=(*g["dekor2"], 50))
    elif jenis == "kotak":
        s = 80
        for j, yy in enumerate(range(0, h, s)):
            for i, xx in enumerate(range(0, w, s)):
                if (i + j) % 2 == 0:
                    d.rectangle((xx, yy, xx + s, yy + s), fill=(255, 255, 255, 8))
    elif jenis == "gumpal":
        rnd = random.Random(g["seed"] + 7)
        for warna in (g["dekor1"], g["dekor2"], g["aksen"]):
            cx, cy = rnd.uniform(0.1, 0.9) * w, rnd.uniform(0.1, 0.9) * h
            r = rnd.uniform(0.25, 0.45) * w
            d.ellipse((cx - r, cy - r * 0.8, cx + r, cy + r * 0.8), fill=(*warna, 60))
        lap = lap.filter(ImageFilter.GaussianBlur(w * 0.12))
    im.alpha_composite(lap)
    return im


def _sinar(w: int, h: int, g: dict[str, Any]) -> Any:
    from PIL import Image, ImageDraw, ImageFilter

    # Kipasnya berpusat di badge (sepertiga atas layar) dan hanya perlu
    # menjangkau bagian atas; 1,25x LEBAR layar cukup, dan 3x lebih murah
    # daripada 1,25x tinggi - biayanya ada di pembesaran tiap frame.
    n = int(w * 1.25)
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = n / 2
    for i in range(14):
        a = i * math.tau / 14
        d.polygon([(c, c), (c + n * math.cos(a - 0.06), c + n * math.sin(a - 0.06)),
                   (c + n * math.cos(a + 0.06), c + n * math.sin(a + 0.06))], fill=(*g["dekor2"], 38))
    return im.filter(ImageFilter.GaussianBlur(6))


def _gerak_latar(frame: Any, w: int, h: int, t: float, g: dict[str, Any], cache: dict[str, Any]) -> None:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    jenis = g["latar"]
    if jenis == "sapuan":
        sudut = g["sudut_sapuan"]
        xs = np.arange(w, dtype=np.float32)[None, :]
        ys = np.arange(h, dtype=np.float32)[:, None]
        proj = xs * math.cos(sudut) + ys * math.sin(sudut)
        pmax = w * math.cos(sudut) + h * math.sin(sudut)
        pos = pmax * (0.1 + 0.8 * ((t / g["durasi"]) % 1.0))
        sweep = np.exp(-((proj - pos) ** 2) / (2 * 150.0 ** 2)).astype(np.float32)
        lap = np.zeros((h, w, 4), np.uint8)
        lap[:, :, :3] = g["aksen2"]
        lap[:, :, 3] = (sweep * 110).astype(np.uint8)
        frame.alpha_composite(Image.fromarray(lap, "RGBA"))
    elif jenis == "partikel":
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
    elif jenis == "bokeh":
        # Lapisan yang memang buram digambar dan di-blur di 1/4 resolusi lalu
        # diperbesar: blur satu frame penuh tiap frame adalah operasi termahal
        # di Pillow, dan hasilnya tak berbeda untuk isi yang memang kabur.
        if "bokeh" not in cache:
            rnd = random.Random(g["seed"] + 5)
            cache["bokeh"] = [(rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(40, 130),
                               rnd.uniform(4, 14), rnd.uniform(0, math.tau)) for _ in range(12)]
        q = 4
        lap = Image.new("RGBA", (w // q, h // q), (0, 0, 0, 0))
        d = ImageDraw.Draw(lap)
        for px, py, r, v, ph in cache["bokeh"]:
            y = (py - v * t) % h
            x = px + 30 * math.sin(ph + t * 0.6)
            d.ellipse(((x - r) / q, (y - r) / q, (x + r) / q, (y + r) / q), fill=(*g["dekor2"], 28))
        frame.alpha_composite(lap.filter(ImageFilter.GaussianBlur(18 / q)).resize((w, h), Image.BILINEAR))
    elif jenis == "sinar":
        if "sinar" not in cache:
            penuh = _sinar(w, h, g)
            cache["sinar_n"] = penuh.width
            cache["sinar"] = penuh.resize((penuh.width // 4, penuh.height // 4), Image.BILINEAR)
        n = cache["sinar_n"]
        s = cache["sinar"].rotate(t * 6.0, resample=Image.BILINEAR).resize((n, n), Image.BILINEAR)
        _tempel(frame, s, w // 2 - n // 2, int(h * 0.33) - n // 2, 1.0)
    elif jenis == "pita-halus":
        q = 4
        lap = Image.new("RGBA", (w // q, h // q), (0, 0, 0, 0))
        d = ImageDraw.Draw(lap)
        for k in range(5):
            titik = [(x / q, (h * (0.25 + 0.14 * k) + 40 * math.sin(x / 180 + t * 1.2 + k)) / q) for x in range(0, w + 40, 40)]
            d.line(titik, fill=(*g["dekor2"], 40), width=max(2, 26 // q))
        frame.alpha_composite(lap.filter(ImageFilter.GaussianBlur(8 / q)).resize((w, h), Image.BILINEAR))
    elif jenis == "sorot":
        q = 4
        lap = Image.new("RGBA", (w // q, h // q), (0, 0, 0, 0))
        d = ImageDraw.Draw(lap)
        cx = w * (0.5 + 0.35 * math.sin(t * 0.7))
        r = w * 0.55
        d.ellipse(((cx - r) / q, (h * 0.3 - r) / q, (cx + r) / q, (h * 0.3 + r) / q), fill=(*g["dekor2"], 34))
        frame.alpha_composite(lap.filter(ImageFilter.GaussianBlur(70 / q)).resize((w, h), Image.BILINEAR))


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
        draw.line(titik, fill=(*_lebih_terang(isi, 40), 255), width=3)


def _dekor(frame: Any, w: int, h: int, t: float, g: dict[str, Any], pusat: tuple[int, int], D: int) -> None:
    from PIL import Image, ImageDraw

    jenis = g["dekor"]
    naik = _halus(t / 0.9)
    if jenis == "tanpa":
        return
    if jenis in ("gelombang-kanan", "gelombang-kiri"):
        _gelombang(ImageDraw.Draw(frame), w, h, t, naik, g, kanan=(jenis == "gelombang-kanan"))
        return
    lap = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(lap)
    cx, cy = pusat
    if jenis == "pita":
        geser = int((1 - naik) * 400)
        for i, warna in enumerate((g["dekor1"], g["dekor2"], g["aksen"])):
            y0 = h - 420 + i * 120 + geser + int(8 * math.sin(t * 1.5 + i))
            d.polygon([(0, y0 + 160), (w, y0 - 60), (w, y0 + 40), (0, y0 + 260)], fill=(*warna, 230))
    elif jenis == "cincin":
        for i in range(3):
            r = D * (0.62 + i * 0.14) * (0.9 + 0.1 * naik) + 6 * math.sin(t * 1.2 + i)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(*g["aksen"], 120 - i * 30), width=4)
    elif jenis == "sudut":
        s = int(w * 0.55 * naik)
        d.polygon([(0, 0), (s, 0), (0, s)], fill=(*g["dekor1"], 200))
        d.polygon([(w, h), (w - s, h), (w, h - s)], fill=(*g["dekor2"], 200))
        d.polygon([(0, 0), (int(s * 0.6), 0), (0, int(s * 0.6))], fill=(*g["aksen"], 160))
    elif jenis == "garis":
        panjang = int(w * naik)
        for i, yy in enumerate((int(h * 0.08), int(h * 0.11), int(h * 0.90), int(h * 0.93))):
            x0 = 0 if i % 2 == 0 else w - panjang
            d.line([(x0, yy), (x0 + panjang, yy)], fill=(*g["aksen"], 200), width=4 if i % 2 == 0 else 2)
    elif jenis == "bingkai":
        m = int(w * 0.045)
        a = int(255 * naik)
        d.rounded_rectangle((m, m, w - m, h - m), int(w * 0.04), outline=(*g["aksen"], a), width=6)
        d.rounded_rectangle((m + 14, m + 14, w - m - 14, h - m - 14), int(w * 0.035), outline=(*g["aksen2"], a // 2), width=2)
    elif jenis == "segitiga":
        s = int(w * 0.7 * naik)
        d.polygon([(w, 0), (w - s, 0), (w, int(s * 0.8))], fill=(*g["dekor1"], 210))
        d.polygon([(0, h), (s, h), (0, h - int(s * 0.8))], fill=(*g["dekor2"], 210))
    elif jenis == "bar":
        tb = int(h * 0.06 * naik)
        d.rectangle((0, 0, w, tb), fill=(*g["aksen"], 255))
        d.rectangle((0, h - tb, w, h), fill=(*g["aksen"], 255))
        d.rectangle((0, tb, w, tb + 6), fill=(*g["aksen2"], 200))
        d.rectangle((0, h - tb - 6, w, h - tb), fill=(*g["aksen2"], 200))
    elif jenis == "lingkaran":
        rnd = random.Random(g["seed"] + 11)
        for i in range(7):
            r = rnd.uniform(40, 160) * (0.6 + 0.4 * naik)
            x, y = rnd.uniform(0, w), rnd.uniform(0, h)
            y += 10 * math.sin(t * 0.8 + i)
            d.ellipse((x - r, y - r, x + r, y + r), outline=(*g["dekor2"], 110), width=3)
    elif jenis == "diagonal-ganda":
        geser = int((1 - naik) * w)
        d.polygon([(w - geser, 0), (w + 220 - geser, 0), (0 + 220 - geser, h), (0 - geser, h)], fill=(*g["dekor1"], 150))
        d.polygon([(w + 260 - geser, 0), (w + 340 - geser, 0), (340 - geser, h), (260 - geser, h)], fill=(*g["aksen"], 120))
    elif jenis == "busur":
        r = int(w * 1.3)
        yb = h + int(r * 0.55) - int(120 * naik)
        d.ellipse((w // 2 - r, yb - r, w // 2 + r, yb + r), fill=(*g["dekor1"], 255))
        d.ellipse((w // 2 - r, yb - r + 60, w // 2 + r, yb + r + 60), fill=(*g["dekor2"], 255))
    elif jenis == "titik-sudut":
        for j in range(12):
            for i in range(12):
                r = max(0.0, (10 - i - j) * 1.1) * naik
                if r > 0.5:
                    d.ellipse((w - 40 - i * 44 - r, h - 40 - j * 44 - r, w - 40 - i * 44 + r, h - 40 - j * 44 + r), fill=(*g["aksen"], 200))
                    d.ellipse((40 + i * 44 - r, 40 + j * 44 - r, 40 + i * 44 + r, 40 + j * 44 + r), fill=(*g["aksen"], 200))
    frame.alpha_composite(lap)


# ============================================================
#  BADGE (logo) - sembilan bentuk, satu gaya bevel
# ============================================================

def _mask_bentuk(bentuk: str, n: int, r: float, bulat: float) -> Any:
    from PIL import Image, ImageDraw, ImageFilter

    c = n / 2
    m = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(m)
    if bentuk in ("lingkaran", "lencana"):
        d.ellipse((c - r, c - r, c + r, c + r), fill=255)
        return m
    if bentuk == "kotak":
        d.rounded_rectangle((c - r, c - r, c + r, c + r), r * 0.28, fill=255)
        return m
    if bentuk == "pil":
        d.rounded_rectangle((c - r, c - r * 0.62, c + r, c + r * 0.62), r * 0.62, fill=255)
        return m
    if bentuk == "berlian":
        pts = [(c, c - r), (c + r, c), (c, c + r), (c - r, c)]
    elif bentuk == "bintang":
        pts = []
        for i in range(16):
            rr = r if i % 2 == 0 else r * 0.82
            a = math.radians(22.5 * i - 90)
            pts.append((c + rr * math.cos(a), c + rr * math.sin(a)))
    elif bentuk == "heksagon":
        pts = [(c + r * math.cos(math.radians(60 * i - 30)), c + r * math.sin(math.radians(60 * i - 30))) for i in range(6)]
    elif bentuk == "perisai":
        pts = [(c - r, c - r * 0.85), (c + r, c - r * 0.85), (c + r, c + r * 0.15), (c, c + r), (c - r, c + r * 0.15)]
    else:
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
        lap = Image.new("RGBA", (n, n), (*warna[:3], 255))
        lap.putalpha(m)
        im.alpha_composite(lap)

    bentuk = g["bentuk"]
    bulat = c * 0.05
    aksen, bingkai = g["aksen"], g["aksen2"]
    bayang = _mask_bentuk(bentuk, n, c * 0.93, bulat)
    bayang = bayang.transform((n, n), Image.AFFINE, (1, 0, 0, 0, 1, -c * 0.07)).filter(ImageFilter.GaussianBlur(c * 0.09))
    lapis(bayang, (0, 0, 0))
    if bentuk == "lencana":
        # cincin luar terpisah dengan celah, lalu badan lingkaran
        cincin = Image.new("L", (n, n), 0)
        ImageDraw.Draw(cincin).ellipse((c * 0.03, c * 0.03, n - c * 0.03, n - c * 0.03), outline=255, width=int(c * 0.07))
        lapis(cincin, bingkai)
        lapis(_mask_bentuk(bentuk, n, c * 0.84, 0), bingkai)
        lapis(_mask_bentuk(bentuk, n, c * 0.78, 0), tuple(max(0, v - 25) for v in aksen))
        mask = _mask_bentuk(bentuk, n, c * 0.72, 0)
    else:
        lapis(_mask_bentuk(bentuk, n, c * 0.94, c * 0.06), bingkai)
        lapis(_mask_bentuk(bentuk, n, c * 0.855, bulat), tuple(max(0, v - 25) for v in aksen))
        lapis(_mask_bentuk(bentuk, n, c * 0.815, bulat), bingkai)
        mask = _mask_bentuk(bentuk, n, c * 0.775, c * 0.045)
    lapis(mask, aksen)
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

    # nama channel: kontras terhadap warna aksen
    terang_aksen = 0.299 * aksen[0] + 0.587 * aksen[1] + 0.114 * aksen[2]
    warna_teks = (20, 20, 24, 255) if terang_aksen > 150 else (255, 255, 255, 255)
    kata = nama.upper().split()
    lebar_maks = c * (1.05 if bentuk in ("lingkaran", "lencana", "heksagon", "berlian", "bintang") else 1.15)
    if bentuk == "pil":
        baris_baris = [(" ".join(kata), c)]
        lebar_maks = c * 1.6
    elif len(kata) >= 2:
        belah = max(1, len(kata) // 2) if len(kata) > 2 else 1
        baris_baris = [(" ".join(kata[:belah]), c * 0.80), (" ".join(kata[belah:]), c * 1.22)]
    else:
        s = nama.upper()
        baris_baris = [(s[: len(s) // 2], c * 0.80), (s[len(s) // 2:], c * 1.22)] if len(s) > 6 else [(s, c)]
    d = ImageDraw.Draw(im)
    for baris, y_pusat in baris_baris:
        if not baris:
            continue
        uk = int(c * 0.30)
        f = _font(uk, g["font_badge"])
        while uk > 20 and d.textbbox((0, 0), baris, font=f)[2] > lebar_maks:
            uk -= 4
            f = _font(uk, g["font_badge"])
        x0, y0, x1, y1 = d.textbbox((0, 0), baris, font=f)
        px, py = c - (x1 - x0) / 2 - x0, y_pusat - (y1 - y0) / 2 - y0
        d.text((px + 4, py + 5), baris, font=f, fill=(0, 0, 0, 150))
        d.text((px, py), baris, font=f, fill=warna_teks)
    if len(baris_baris) == 2 and all(b for b, _ in baris_baris):
        d.line([(c * 0.45, c), (c * 1.55, c)], fill=(*warna_teks[:3], 230), width=int(c * 0.02))
    return im.resize((D, D), Image.LANCZOS)


def _logo_unggahan(path: str, diameter: int) -> Any:
    from PIL import Image

    im = Image.open(path).convert("RGBA")
    im.thumbnail((diameter, diameter), Image.LANCZOS)
    kanvas = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    kanvas.alpha_composite(im, ((diameter - im.width) // 2, (diameter - im.height) // 2))
    return kanvas


def _glow_badge(D: int, g: dict[str, Any]) -> Any:
    from PIL import Image, ImageDraw, ImageFilter

    n = int(D * 1.6)
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    r = D * 0.55
    ImageDraw.Draw(im).ellipse((n / 2 - r, n / 2 - r, n / 2 + r, n / 2 + r), fill=(*g["aksen"], 150))
    return im.filter(ImageFilter.GaussianBlur(D * 0.14))


# ============================================================
#  IKON PLATFORM - glyph merek resmi
# ============================================================

_GLYPH_CACHE: dict[str, Any] = {}


def _glyph(platform: str) -> Any:
    from PIL import Image

    if platform not in _GLYPH_CACHE:
        _GLYPH_CACHE[platform] = Image.open(ASET / "ikon" / f"{platform}.png").convert("RGBA")
    return _GLYPH_CACHE[platform]


def _ikon(platform: str, s: int, g: dict[str, Any]) -> Any:
    """Ikon platform: glyph resmi di atas latar sesuai gaya.

    warna     : latar warna merek (Instagram gradien), glyph putih - seperti app icon.
    mono      : latar warna aksen palet, glyph putih.
    garis     : tanpa latar penuh, lingkaran garis tipis + glyph warna aksen2.
    lingkaran : latar lingkaran warna merek, glyph putih.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    S = 3
    n = s * S
    gaya = g["ikon"]
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    mask = Image.new("L", (n, n), 0)
    if gaya in ("lingkaran", "garis"):
        ImageDraw.Draw(mask).ellipse((0, 0, n - 1, n - 1), fill=255)
    else:
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, n - 1, n - 1), n * 0.22, fill=255)

    if gaya == "garis":
        # Tanpa latar penuh, glyph-nya harus selalu kontras dengan apa pun di
        # belakangnya - dulu memakai warna aksen, dan di atas dekor sewarna
        # (mis. busur ungu) ikonnya nyaris hilang. Warna teks palet aman.
        d.ellipse((n * 0.03, n * 0.03, n * 0.97, n * 0.97), outline=(*g["teks"], 200), width=max(2, n // 28))
        warna_glyph = g["teks"]
    elif gaya == "mono":
        d.rounded_rectangle((0, 0, n - 1, n - 1), n * 0.22, fill=(*g["aksen"], 255))
        terang = 0.299 * g["aksen"][0] + 0.587 * g["aksen"][1] + 0.114 * g["aksen"][2]
        warna_glyph = (20, 20, 24) if terang > 150 else (255, 255, 255)
    else:
        if platform == "instagram":
            ys = np.linspace(0, 1, n, dtype=np.float32)[:, None]
            xs = np.linspace(0, 1, n, dtype=np.float32)[None, :]
            u = np.clip(0.6 * ys + 0.4 * (1 - xs), 0, 1)[:, :, None]
            a, m, b = (np.array(v, np.float32) for v in ((255, 190, 60), (225, 45, 110), (110, 45, 200)))
            arr = np.where(u < 0.5, a + (m - a) * (u / 0.5), m + (b - m) * ((u - 0.5) / 0.5))
            bg = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
            bg.putalpha(mask)
            im = bg
            d = ImageDraw.Draw(im)
        elif gaya == "lingkaran":
            d.ellipse((0, 0, n - 1, n - 1), fill=(*WARNA_MEREK[platform], 255))
        else:
            d.rounded_rectangle((0, 0, n - 1, n - 1), n * 0.22, fill=(*WARNA_MEREK[platform], 255))
        warna_glyph = (255, 255, 255)

    # glyph: PNG putih-transparan diwarnai lalu ditempel di tengah
    ukur = int(n * 0.60)
    gl = _glyph(platform).resize((ukur, ukur), Image.LANCZOS)
    if platform == "tiktok" and gaya in ("warna", "lingkaran"):
        # ciri khas TikTok: bayangan sian & merah muda di kiri-atas dan kanan-bawah
        for warna, dx, dy in (((37, 244, 238), -n * 0.02, -n * 0.02), ((254, 44, 85), n * 0.02, n * 0.02)):
            lap = Image.new("RGBA", (ukur, ukur), (*warna, 255)); lap.putalpha(gl.getchannel("A"))
            im.alpha_composite(lap, (int((n - ukur) / 2 + dx), int((n - ukur) / 2 + dy)))
    lap = Image.new("RGBA", (ukur, ukur), (*warna_glyph[:3], 255))
    lap.putalpha(gl.getchannel("A"))
    im.alpha_composite(lap, ((n - ukur) // 2, (n - ukur) // 2))
    if gaya != "garis" and not (platform == "instagram" and gaya == "warna"):
        im.putalpha(Image.composite(im.getchannel("A"), Image.new("L", (n, n), 0), mask))
    return im.resize((s, s), Image.LANCZOS)


# ============================================================
#  TATA LETAK
# ============================================================

def _susun(w: int, h: int, g: dict[str, Any], jumlah: int, pakai_nama_teks: bool) -> dict[str, Any]:
    tata = g["tata"]
    n = jumlah
    L: dict[str, Any] = {"baris": [], "nama": None}

    def baris_kiri(x_ikon: int, y0: int, pitch: int, ikon: int, font: int, jarak: int) -> None:
        for i in range(n):
            L["baris"].append({"ikon": (x_ikon, y0 + i * pitch), "teks": (x_ikon + ikon + jarak, y0 + i * pitch), "ikon_s": ikon, "font": font})

    if tata == "dua-kolom":
        D = int(w * 0.40)
        L["badge"] = (int(w * 0.06), int(h * 0.30), D)
        ikon = int(w * 0.075); pitch = int(h * 0.062)
        baris_kiri(int(w * 0.52), int(h * 0.30) + (D - n * pitch) // 2, pitch, ikon, int(w * 0.040), int(w * 0.02))
        L["nama"] = (w // 2, int(h * 0.30) + D + int(h * 0.05))
    elif tata == "grid":
        D = int(w * 0.44)
        L["badge"] = ((w - D) // 2, int(h * 0.14), D)
        ikon = int(w * 0.07); kol_w = int(w * 0.44); x_kol = [int(w * 0.05), int(w * 0.51)]
        y0 = int(h * 0.14) + D + int(h * 0.09); pitch = int(h * 0.075)
        for i in range(n):
            xk, yk = x_kol[i % 2], y0 + (i // 2) * pitch
            L["baris"].append({"pil": (xk, yk, kol_w, int(h * 0.058)), "ikon": (xk + 12, yk + 6), "teks": (xk + 12 + ikon + 14, yk + 6), "ikon_s": ikon, "font": int(w * 0.034)})
        L["nama"] = (w // 2, int(h * 0.14) + D + int(h * 0.03))
    elif tata == "dua-baris":
        D = int(w * 0.42)
        L["badge"] = ((w - D) // 2, int(h * 0.15), D)
        ikon = int(w * 0.062); kol_w = int(w * 0.30); x_kol = [int(w * 0.035), int(w * 0.35), int(w * 0.665)]
        y0 = int(h * 0.15) + D + int(h * 0.10); pitch = int(h * 0.085)
        for i in range(n):
            xk, yk = x_kol[i % 3], y0 + (i // 3) * pitch
            L["baris"].append({"pil": (xk, yk, kol_w, int(h * 0.062)), "ikon": (xk + 10, yk + 8), "teks": (xk + 10 + ikon + 10, yk + 8), "ikon_s": ikon, "font": int(w * 0.026)})
        L["nama"] = (w // 2, int(h * 0.15) + D + int(h * 0.03))
    elif tata == "baris-ikon":
        D = int(w * 0.48)
        L["badge"] = ((w - D) // 2, int(h * 0.16), D)
        ikon = int(w * 0.11); gap = int(w * 0.30); x0 = w // 2 - gap
        y0 = int(h * 0.16) + D + int(h * 0.10)
        for i in range(n):
            xk = x0 + (i % 3) * gap - ikon // 2; yk = y0 + (i // 3) * int(h * 0.15)
            L["baris"].append({"ikon": (xk, yk), "teks_tengah": (xk + ikon // 2, yk + ikon + 10), "ikon_s": ikon, "font": int(w * 0.030)})
        L["nama"] = (w // 2, int(h * 0.16) + D + int(h * 0.035))
    elif tata == "melingkar":
        D = int(w * 0.40)
        L["badge"] = ((w - D) // 2, int(h * 0.24), D)
        ikon = int(w * 0.10); cx, cy = w // 2, int(h * 0.24) + D // 2; R = D * 0.85
        for i in range(n):
            a = -math.pi / 2 + i * math.tau / max(1, n)
            L["baris"].append({"ikon": (int(cx + R * math.cos(a) - ikon / 2), int(cy + R * math.sin(a) - ikon / 2)), "teks_tengah": None, "ikon_s": ikon, "font": int(w * 0.030)})
        y0 = int(h * 0.24) + D + int(h * 0.20); pitch = int(h * 0.044)
        for i in range(n):
            L["baris"][i]["teks_tengah_y"] = y0 + i * pitch
        L["nama"] = (w // 2, int(h * 0.24) + D + int(h * 0.12))
    elif tata == "kiri-atas":
        D = int(w * 0.30)
        L["badge"] = (int(w * 0.06), int(h * 0.10), D)
        ikon = int(w * 0.085); pitch = int(h * 0.072)
        baris_kiri(int(w * 0.10), int(h * 0.10) + D + int(h * 0.09), pitch, ikon, int(w * 0.052), int(w * 0.03))
        L["nama"] = (int(w * 0.06) + D + int(w * 0.06) + int(w * 0.25), int(h * 0.10) + D // 2 - int(w * 0.03))
        L["nama_kiri"] = True
    elif tata == "bawah":
        D = int(w * 0.46)
        L["badge"] = ((w - D) // 2, int(h * 0.56), D)
        ikon = int(w * 0.078); pitch = int(h * 0.060)
        baris_kiri(int(w * 0.24), int(h * 0.12), pitch, ikon, int(w * 0.046), int(w * 0.03))
        L["nama"] = (w // 2, int(h * 0.56) + D + int(h * 0.03))
    elif tata == "kolom-kanan":
        D = int(w * 0.44)
        L["badge"] = (int(w * 0.05), int(h * 0.32), D)
        ikon = int(w * 0.07); pil_w = int(w * 0.40); x = int(w * 0.56)
        y0 = int(h * 0.32) + (D - n * int(h * 0.068)) // 2
        for i in range(n):
            yk = y0 + i * int(h * 0.068)
            L["baris"].append({"pil": (x, yk, pil_w, int(h * 0.056)), "ikon": (x + 10, yk + 6), "teks": (x + 10 + ikon + 10, yk + 6), "ikon_s": ikon, "font": int(w * 0.030)})
        L["nama"] = (int(w * 0.05) + D // 2, int(h * 0.32) + D + int(h * 0.03))
    elif tata == "tengah":
        D = int(w * 0.50)
        L["badge"] = ((w - D) // 2, int(h * 0.15), D)
        ikon = int(w * 0.075); pitch = int(h * 0.062); y0 = int(h * 0.15) + D + int(h * 0.09)
        for i in range(n):
            L["baris"].append({"tengah": True, "y": y0 + i * pitch, "ikon_s": ikon, "font": int(w * 0.044)})
        L["nama"] = (w // 2, int(h * 0.15) + D + int(h * 0.03))
    else:  # acuan
        D = int(w * 0.56)
        L["badge"] = ((w - D) // 2, int(h * 0.17), D)
        ikon = int(w * 0.082); pitch = int(h * 0.062) if n <= 4 else int(h * 0.055)
        baris_kiri(int(w * 0.22), int(h * 0.17) + D + int(h * 0.11), pitch, ikon, int(w * 0.050), int(w * 0.03))
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
    nama_teks_perlu = pakai_logo or g["tata"] in ("grid", "baris-ikon", "dua-kolom", "kiri-atas", "bawah", "melingkar", "dua-baris", "kolom-kanan")
    L = _susun(w, h, g, len(isi), nama_teks_perlu)
    bx, by, D = L["badge"]
    badge = _logo_unggahan(job["logo"], D) if pakai_logo else _badge(channel, D, g)
    glow = _glow_badge(D, g) if g["glow_badge"] else None
    pusat_badge = (bx + D // 2, by + D // 2)
    nama_im = _teks_gambar(huruf(channel), int(w * 0.058), (*g["teks"], 255), miring=g["miring"], nama_font=g["font_teks"]) if L["pakai_nama_teks"] else None

    warna_teks = (*g["teks"], 255)
    baris_ims = []
    for (k, nilai), b in zip(isi, L["baris"]):
        ik = _ikon(k, b["ikon_s"], g)
        tx = _teks_gambar(huruf(nilai) if g["huruf_besar"] and g["acak"] < 0.3 else nilai, b["font"], warna_teks, nama_font=g["font_teks"])
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
            # gerak latar: zoom pelan / geser pelan / diam
            if g["gerak_latar"] == "zoom":
                # 16 tingkat zoom dihitung sekali (langkah ~0,4%, tak terlihat
                # pada gradien), bukan resize latar penuh tiap frame - itu
                # yang membuat satu video memakan lebih dari semenit.
                k = min(15, int(16 * t / durasi))
                if ("zoom", k) not in cache:
                    f = 1.0 + 0.06 * (k / 15)
                    big = latar.resize((int(w * f), int(h * f)))
                    cache[("zoom", k)] = big.crop(((big.width - w) // 2, (big.height - h) // 2, (big.width - w) // 2 + w, (big.height - h) // 2 + h))
                frame = cache[("zoom", k)].copy()
            elif g["gerak_latar"] == "geser":
                if "latar_lebar" not in cache:
                    cache["latar_lebar"] = latar.resize((int(w * 1.08), int(h * 1.08)))
                big = cache["latar_lebar"]
                dx = int((big.width - w) * (t / durasi)); dy = int((big.height - h) * 0.5)
                frame = big.crop((dx, dy, dx + w, dy + h))
            else:
                frame = latar.copy()
            _gerak_latar(frame, w, h, t, g, cache)
            _dekor(frame, w, h, t, g, pusat_badge, D)

            # ----- badge -----
            if t >= t_badge:
                u = (t - t_badge) / 0.8
                a = _clamp(u / 0.35)
                apung = int(5 * math.sin(t * 1.8))
                jenis = g["masuk_badge"]
                dx = dy = 0
                if jenis == "lenting":
                    bimg = _skala(badge, max(0.02, _lenting(u)))
                elif jenis == "turun":
                    bimg = badge; dy = int(-(1 - _halus(u)) * h * 0.25)
                elif jenis == "putar":
                    bimg = _skala(badge, max(0.05, _halus(u))).rotate((1 - _halus(u)) * 180, resample=Image.BICUBIC, expand=False)
                elif jenis == "kiri":
                    bimg = badge; dx = int(-(1 - _halus(u)) * w * 0.6)
                elif jenis == "kedip":
                    bimg = _skala(badge, 1.0 + 0.06 * math.sin(min(u, 1.0) * math.pi * 2))
                else:
                    bimg = _skala(badge, 1.35 - 0.35 * _halus(u))
                if glow is not None:
                    gx = pusat_badge[0] - glow.width // 2 + dx
                    gy = pusat_badge[1] - glow.height // 2 + dy + apung
                    _tempel(frame, glow, gx, gy, a * (0.7 + 0.3 * math.sin(t * 2.0)))
                _tempel(frame, bimg, bx + (D - bimg.width) // 2 + dx, by + (D - bimg.height) // 2 + apung + dy, a)

            # ----- nama (teks) -----
            if nama_im is not None and t >= t_badge + 0.6:
                a = _halus((t - t_badge - 0.6) / 0.5)
                nx, ny = L["nama"]
                x = nx - nama_im.width // 2
                if L.get("nama_kiri"):
                    x = int(w * 0.06) + D + int(w * 0.06)
                _tempel(frame, nama_im, x, ny + int(24 * (1 - a)), a)

            # ----- handle -----
            for i, (ik, tx, b) in enumerate(baris_ims):
                mulai = t_handle + i * 0.13
                if t < mulai:
                    continue
                u = (t - mulai) / 0.45
                a = _halus(u)
                jenis = g["masuk_handle"]
                dx = int(-70 * (1 - a)) if jenis == "kiri" else int(70 * (1 - a)) if jenis == "kanan" else 0
                dy = int(40 * (1 - a)) if jenis == "atas" else 0
                sk = max(0.05, _lenting(u)) if jenis == "pop" else 1.0
                ikx = _skala(ik, sk) if sk != 1.0 else ik
                txx_im = tx
                if jenis == "ketik":
                    lebar = max(1, int(tx.width * _clamp(u)))
                    txx_im = tx.crop((0, 0, lebar, tx.height))
                if "pil" in b:
                    px, py, pw, ph = b["pil"]
                    lap = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
                    ImageDraw.Draw(lap).rounded_rectangle((0, 0, pw - 1, ph - 1), ph // 2, fill=(*g["aksen"][:3], 60), outline=(*g["aksen2"][:3], 140), width=2)
                    _tempel(frame, lap, px + dx, py + dy, a)
                if b.get("tengah"):
                    if "lebar_tengah" not in cache:
                        cache["lebar_tengah"] = max(i2.width + 18 + t2.width for i2, t2, _ in baris_ims)
                    x0 = w // 2 - cache["lebar_tengah"] // 2
                    _tempel(frame, ikx, x0 + dx + (ik.width - ikx.width) // 2, b["y"] + dy + (ik.height - ikx.height) // 2, a)
                    _tempel(frame, txx_im, x0 + ik.width + 18 + dx, b["y"] + dy + (ik.height - tx.height) // 2, a)
                elif "teks_tengah_y" in b:
                    ix, iy = b["ikon"]
                    _tempel(frame, ikx, ix + dx + (ik.width - ikx.width) // 2, iy + dy + (ik.height - ikx.height) // 2, a)
                    _tempel(frame, txx_im, w // 2 - tx.width // 2, b["teks_tengah_y"] + dy, a)
                elif "teks_tengah" in b and b["teks_tengah"] is not None:
                    ix, iy = b["ikon"]
                    _tempel(frame, ikx, ix + dx + (ik.width - ikx.width) // 2, iy + dy + (ik.height - ikx.height) // 2, a)
                    cx, cy = b["teks_tengah"]
                    _tempel(frame, txx_im, cx - tx.width // 2, cy + dy, a)
                else:
                    ix, iy = b["ikon"]
                    txx, txy = b["teks"]
                    _tempel(frame, ikx, ix + dx + (ik.width - ikx.width) // 2, iy + dy + (ik.height - ikx.height) // 2, a)
                    _tempel(frame, txx_im, txx + dx, txy + dy + (ik.height - tx.height) // 2, a)

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


# ============================================================
#  MODE DPP - meniru outro TV Rakyat, nama channel di logo
# ============================================================
#
# Berbeda dari mode biasa yang tiap kali berganti gaya, mode DPP tampilannya
# TETAP: latar gelap bersemburat merah, gelombang merah di dasar, logo
# TV Rakyat asli masuk dengan putaran-balik, tagline, pil emas berpendar,
# lalu daftar sosmed - persis outro.mp4 pada set layer TV Rakyat. Yang
# berganti hanya dua hal: nama channel yang ditulis melengkung di bawah
# tulisan RAKYAT pada logonya (mis. "Aceh Barat"), dan handle sosmednya.
# Tagline dan ajakannya sengaja dikunci sama dengan acuan.

DPP = ASET / "dpp"
DPP_LOGO = DPP / "logo.png"       # logo bersih, tanpa nama daerah
DPP_AUDIO = DPP / "audio.m4a"     # jalur suara outro acuan, dipakai apa adanya
DPP_TAGLINE = "TV Rakyat, Medianya Rakyat"
DPP_AJAKAN = "Follow untuk Update Terkini"
DPP_DETIK = 5.5
# Kapan tiap unsur mulai tampil, diukur dari frame-frame acuan.
DPP_T_LOGO, DPP_T_TAGLINE, DPP_T_HANDLE, DPP_T_PIL = 0.95, 2.1, 2.5, 2.85


def _latar_dpp(w: int, h: int) -> Any:
    """Latar gelap dengan semburat merah-cokelat hangat dari kanan-bawah."""
    import numpy as np
    from PIL import Image

    ys = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    xs = np.linspace(0, 1, w, dtype=np.float32)[None, :]
    dasar = np.zeros((h, w, 3), np.float32)
    dasar[:, :, 0] = 7 + 9 * ys + 12 * ys ** 2
    dasar[:, :, 1] = 6 + 3 * ys
    dasar[:, :, 2] = 6 + 2 * ys
    d2 = (xs - 0.92) ** 2 * 1.3 + (ys - 0.98) ** 2 * 2.2
    dasar += np.exp(-d2 / 0.16)[:, :, None] * np.array([40, 9, 4], np.float32)
    vx, vy = (xs - 0.5) * 2, (ys - 0.5) * 2
    dasar *= (1.0 - 0.18 * np.clip(vx ** 2 + vy ** 2, 0, 1))[:, :, None]
    return Image.fromarray(np.clip(dasar, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _gelombang_dpp(draw: Any, w: int, h: int, t: float) -> None:
    """Tiga lapis gelombang merah di dasar, menjulang di kanan, bergerak pelan."""
    lapis = (  # isi, tepi, fase, turun, naik_kanan, kecepatan
        ((108, 8, 16, 255), (150, 26, 34, 255), 0.0, 0, 300, 0.7),
        ((158, 12, 22, 255), (206, 42, 50, 255), 1.7, 70, 225, 0.9),
        ((214, 22, 30, 255), (240, 84, 88, 255), 3.3, 150, 150, 1.15),
    )
    dasar_y = h - 240
    for isi, tepi, fase, turun, naik_kanan, kec in lapis:
        titik = []
        for x in range(0, w + 40, 40):
            u = x / w
            y = (dasar_y + turun + 140 * (1 - u) ** 1.2 - naik_kanan * u ** 1.7
                 - 34 * math.sin(u * 3.6 + fase + t * kec)
                 - 18 * math.sin(u * 7.9 - fase * 0.6 + t * kec * 0.7))
            titik.append((x, int(y)))
        draw.polygon(titik + [(w, h), (0, h)], fill=isi)
        draw.line(titik, fill=tepi, width=3)


def _lebar_huruf(teks: str, ukuran: int, nama_font: str = "poppins") -> list[float]:
    """Lebar tiap huruf teks melengkung. Spasi dilebarkan: di font kecil
    (~16 px, nama dua baris) spasi aslinya ~4 px dan kata-katanya tampak
    menempel ("PegununganBintang")."""
    from PIL import Image, ImageDraw

    f = _font(ukuran, nama_font)
    ukur = ImageDraw.Draw(Image.new("L", (1, 1)))
    return [max(ukur.textlength(c, font=f), ukuran * 0.42) if c == " " else ukur.textlength(c, font=f)
            for c in teks]


def _teks_busur(kanvas: Any, teks: str, pusat: tuple[float, float], r: float,
                ukuran: int, warna: tuple, nama_font: str = "poppins") -> tuple[float, float]:
    """Tulis teks melengkung mengikuti busur BAWAH lingkaran (bentuk senyum).

    Tiap huruf digambar sendiri lalu diputar sesuai garis singgungnya, supaya
    bagian atas huruf selalu menghadap ke pusat - seperti "Aceh Barat" di
    logo acuan. Diletakkan simetris di titik terbawah lingkaran.
    """
    from PIL import Image, ImageDraw

    f = _font(ukuran, nama_font)
    ukur = ImageDraw.Draw(Image.new("L", (1, 1)))
    lebar = _lebar_huruf(teks, ukuran, nama_font)
    total = sum(lebar)
    # sudut diukur dari sumbu +x, searah jarum jam (y layar ke bawah);
    # dasar lingkaran = 90 derajat. Teks dibaca kiri ke kanan = sudut menurun.
    a = math.pi / 2 + (total / 2) / r
    a_awal = a
    # Semua huruf duduk di SATU garis dasar: pusat tinggi huruf kapital jatuh
    # di pusat kotak. Dulu tiap huruf dipusatkan pada kotaknya sendiri, jadi
    # huruf berekor bawah (p, g, j, y) terangkat dan "p" terbaca seperti "P".
    x0h, y0h, x1h, y1h = ukur.textbbox((0, 0), "H", font=f, anchor="ls")
    naik_kapital = -y0h
    for c, lc in zip(teks, lebar):
        a_tengah = a - (lc / 2) / r
        x = pusat[0] + r * math.cos(a_tengah)
        y = pusat[1] + r * math.sin(a_tengah)
        # kotak huruf dengan sedikit ruang, diputar mengelilingi pusatnya
        kw, kh = int(lc + ukuran), int(ukuran * 1.6)
        huruf = Image.new("RGBA", (kw, kh), (0, 0, 0, 0))
        d = ImageDraw.Draw(huruf)
        d.text((kw / 2, kh / 2 + naik_kapital / 2), c, font=f, fill=warna, anchor="ms")
        derajat = math.degrees(math.pi / 2 - a_tengah)
        huruf = huruf.rotate(derajat, resample=Image.BICUBIC, expand=True)
        kanvas.alpha_composite(huruf, (int(x - huruf.width / 2), int(y - huruf.height / 2)))
        a -= lc / r
    # rentang sudut yang ditempati teks (radian, dari kiri ke kanan = menurun)
    return a_awal, a


def _busur_garis(kanvas: Any, pusat: tuple[float, float], r: float,
                 a0: float, a1: float, tebal: int, warna: tuple) -> None:
    """Garis melengkung dari sudut a0 ke a1 (radian), dihaluskan 3x."""
    from PIL import Image, ImageDraw

    S = 3
    lap = Image.new("RGBA", (kanvas.width * S, kanvas.height * S), (0, 0, 0, 0))
    cx, cy = pusat[0] * S, pusat[1] * S
    rr = r * S
    lo, hi = sorted((math.degrees(a0), math.degrees(a1)))
    ImageDraw.Draw(lap).arc((cx - rr, cy - rr, cx + rr, cy + rr), lo, hi, fill=warna, width=max(1, tebal * S))
    kanvas.alpha_composite(lap.resize(kanvas.size, Image.LANCZOS))


# Aturan pecah dua baris nama channel di logo DPP. Angkanya ditulis dalam
# satuan acuan (area nama selebar 680 px, font mulai 92 px) lalu diskalakan
# ke area nama sungguhan di logo (~0,44 D, sekitar 265 px pada video 1080):
# - nama selalu dicoba SATU baris dulu, font dikecilkan sampai muat;
# - kalau font satu baris jatuh di bawah TWO_LINE_BELOW, dipecah dua baris,
#   dan di mode dua baris font maksimumnya TWO_LINE_BELOW;
# - minimal dua kata (satu kata panjang tidak dipecah, terus mengecil);
# - pecahan dibatalkan kalau dua baris tidak menghasilkan font lebih besar.
# Angka lebih besar (mis. 50) = lebih cepat terpecah; lebih kecil (34) = lebih lambat.
TWO_LINE_BELOW = 42
DPP_ACUAN_LEBAR = 680
# Busur nama paling lebar ~87 derajat (0,44 D pada jari-jari 0,29 D): lebih
# dari itu ujung-ujungnya naik menabrak huruf RAKYAT ("Kabupaten Aceh
# Tenggara" pada batas lama 0,62 D).
DPP_SUDUT_MAKS = 0.44 / 0.29
DPP_FONT_MIN = 8


def _lebar_teks(teks: str, ukuran: int) -> float:
    """Lebar teks melengkung PERSIS seperti yang digambar _teks_busur."""
    return sum(_lebar_huruf(teks, ukuran))


def _font_pas(teks: str, lebar_boleh: float) -> float:
    """Ukuran font (pecahan) yang membuat teks tepat selebar lebar_boleh.

    Lebar teks sebanding lurus dengan ukuran font, jadi cukup diukur sekali.
    Pecahan dipakai untuk MEMUTUSKAN; di ukuran logo (~16 px) selisih satu
    piksel terlalu kasar - "Kepulauan Bangka Belitung" butuh 16,0 px, di bawah
    ambang 16,4 px, tapi dengan bilangan bulat dua barisnya pun 16 px
    sehingga pecahannya keliru dibatalkan.
    """
    per_px = _lebar_teks(teks, 100) / 100
    return lebar_boleh / per_px if per_px > 0 else float(DPP_FONT_MIN)


def _susun_nama_dpp(nama: str, diameter: int, r: float, mulai: int) -> list[tuple[str, float, int]]:
    """[(teks, jari-jari, font)] - satu baris, atau dua baris bertumpuk."""
    skala = diameter * 0.44 / DPP_ACUAN_LEBAR
    ambang = TWO_LINE_BELOW * skala
    satu = min(float(mulai), _font_pas(nama, DPP_SUDUT_MAKS * r))
    kata = nama.split()

    def jadi(f: float) -> int:
        return max(DPP_FONT_MIN, int(f))

    if satu >= ambang or len(kata) < 2:
        return [(nama, r, jadi(satu))]
    terbaik: tuple[float, float, str, str] | None = None
    for i in range(1, len(kata)):
        atas, bawah = " ".join(kata[:i]), " ".join(kata[i:])
        # Baris atas lebih dekat ke pusat (busurnya lebih pendek untuk sudut
        # yang sama), baris bawah lebih jauh; jari-jarinya bergantung pada
        # font, jadi dihitung beberapa putaran sampai tenang.
        f = ambang
        for _ in range(6):
            f = min(
                ambang,
                _font_pas(atas, DPP_SUDUT_MAKS * (r - f * 0.58)),
                _font_pas(bawah, DPP_SUDUT_MAKS * (r + f * 0.58)),
            )
        # Font sama besar (sering: semuanya mentok di ambang) -> pilih yang
        # panjang kedua barisnya paling seimbang, bukan yang pertama ketemu.
        timpang = abs(_lebar_teks(atas, 100) - _lebar_teks(bawah, 100))
        if terbaik is None or (round(f, 2), -timpang) > (round(terbaik[0], 2), -terbaik[1]):
            terbaik = (f, timpang, atas, bawah)
    if terbaik is None or terbaik[0] <= satu:
        return [(nama, r, jadi(satu))]
    f, _, atas, bawah = terbaik
    u = jadi(f)
    return [(atas, r - u * 0.58, u), (bawah, r + u * 0.58, u)]


def _logo_dpp(nama_channel: str, diameter: int) -> Any:
    """Logo TV Rakyat asli dengan nama channel melengkung di bawah RAKYAT."""
    from PIL import Image

    if not DPP_LOGO.is_file():
        raise OutroError(f"Logo DPP tidak ditemukan: {DPP_LOGO}")
    im = Image.open(DPP_LOGO).convert("RGBA")
    im.thumbnail((diameter, diameter), Image.LANCZOS)
    kanvas = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    kanvas.alpha_composite(im, ((diameter - im.width) // 2, (diameter - im.height) // 2))
    nama = " ".join(nama_channel.split())
    if not nama:
        return kanvas
    pusat = (diameter / 2, diameter / 2)
    # Sedikit lebih tinggi dari posisi "Aceh Barat" pada logo acuan (0,315 D):
    # ada garis di bawah teksnya, dan pada 0,315 D garis itu jatuh tepat di
    # cincin perak - abu di atas abu, tidak terlihat.
    r = diameter * 0.29
    warna = (232, 232, 232, 255)
    # Ukuran awal diukur dari logo acuan "Aceh Barat" (tinggi huruf ~0,053 D).
    mulai = int(diameter * 0.066)
    susunan = _susun_nama_dpp(nama, diameter, r, mulai)
    rentang: list[tuple[float, float]] = []
    for teks, radius, ukuran in susunan:
        rentang.append(_teks_busur(kanvas, teks, pusat, radius, ukuran, warna))
    # Garis atas dan bawah yang mengapit nama, melengkung sejajar teksnya.
    # Rapat ke teks tapi tetap bercelah, dan panjangnya PERSIS selebar teks -
    # berhenti di huruf pertama dan terakhir (dua baris: selebar baris yang
    # paling lebar). Versi awal (celah 0,05 D dan ujung diperpanjang) dinilai
    # pemilik jelek: garis atasnya menabrak RAKYAT dan menjulur jauh.
    a0 = max(a for a, _ in rentang)
    a1 = min(b for _, b in rentang)
    ukuran = susunan[0][2]
    jarak = diameter * 0.038
    if len(susunan) == 2:
        # cukup untuk dua baris + separuh tinggi huruf + celah kecil
        jarak = max(jarak, abs(susunan[1][1] - r) + ukuran * 0.36 + diameter * 0.008)
    tebal = max(2, int(diameter * 0.006))
    for radius in (r - jarak, r + jarak):
        _busur_garis(kanvas, pusat, radius, a0, a1, tebal, warna)
    return kanvas


def _pil_dpp(teks: str, lebar: int, tinggi: int) -> tuple[Any, list[tuple[float, float]]]:
    """Pil emas berteks + titik-titik kelilingnya (jalur cahaya berputar)."""
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
    t, rr = tepi / S, r / S
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


def _cahaya_pil_dpp(lebar: int, tinggi: int, titik: list[tuple[float, float]], p: float) -> Any:
    """Seberkas cahaya emas yang berjalan di keliling pil (p: 0..1)."""
    from PIL import Image, ImageDraw, ImageFilter

    im = Image.new("RGBA", (lebar, tinggi), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    n = len(titik)
    awal = int(p * n) % n
    seg = [titik[(awal + i) % n] for i in range(int(n * 0.26))]
    d.line(seg, fill=(255, 205, 80, 235), width=max(4, tinggi // 5), joint="curve")
    im = im.filter(ImageFilter.GaussianBlur(tinggi * 0.11))
    d = ImageDraw.Draw(im)
    d.line(seg, fill=(255, 225, 120, 255), width=max(3, tinggi // 12), joint="curve")
    im = im.filter(ImageFilter.GaussianBlur(tinggi * 0.03))
    d = ImageDraw.Draw(im)
    d.line(seg, fill=(255, 246, 200, 255), width=max(2, tinggi // 26), joint="curve")
    return im


def _mulai_ffmpeg(folder: Path, w: int, h: int) -> tuple[Any, Path, Any]:
    """Pipa rawvideo -> x264, dengan -threads dibatasi (kontainer 1 GB)."""
    keluaran = folder / "video-tanpa-audio.mp4"
    utas = os.getenv("VIDEO_THREADS", "1") or "1"
    if utas == "0":
        utas = "1"
    galat = (folder / "ffmpeg.log").open("w", encoding="utf-8")
    proses = subprocess.Popen(
        [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-threads", utas,
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
         "-threads", utas, "-movflags", "+faststart", str(keluaran)],
        stdin=subprocess.PIPE, stderr=galat,
    )
    return proses, keluaran, galat


def render_dpp(job: dict[str, Any], folder: Path, ev: threading.Event, lapor: Callable[[int], None]) -> Path:
    from PIL import Image, ImageDraw

    w = VIDEO_W - VIDEO_W % 2
    h = VIDEO_H - VIDEO_H % 2
    isi = [(k, job["akun"][k]) for k in KUNCI_AKUN if job["akun"].get(k)]

    # --- unsur tetap, dirender sekali ---
    latar = _latar_dpp(w, h)
    D = int(w * 0.56)
    logo = _logo_dpp(job["channel"], D)
    bx, by = (w - D) // 2, int(h * 0.19)
    tag_im = _teks_gambar(DPP_TAGLINE, int(w * 0.062), (255, 255, 255, 255), miring=True)
    tag_y = by + D + int(h * 0.03)
    pil_w, pil_h = int(w * 0.60), int(h * 0.05)
    pil_im, pil_titik = _pil_dpp(DPP_AJAKAN, pil_w, pil_h)
    pil_x, pil_y = (w - pil_w) // 2, tag_y + tag_im.height + int(h * 0.02)
    ikon_s = int(w * 0.072)
    pitch = int(h * 0.052) if len(isi) <= 4 else int(h * 0.048)
    baris_y0 = pil_y + pil_h + int(h * 0.035)
    baris = [(_ikon(k, ikon_s, {"ikon": "warna"}),
              _teks_gambar(v, int(w * 0.041), (255, 255, 255, 255), nama_font="poppins-regular")) for k, v in isi]
    # 16 tingkat ukuran logo dicache: mengubah ukuran PNG besar tiap frame
    # itulah yang dulu membuat render lambat.
    cache_logo: dict[tuple[int, int], Any] = {}

    def logo_ukuran(lebar_l: int, tinggi_l: int) -> Any:
        kunci = (max(2, lebar_l), max(2, tinggi_l))
        if kunci not in cache_logo:
            cache_logo[kunci] = logo.resize(kunci, Image.LANCZOS)
        return cache_logo[kunci]

    total = max(1, int(FPS * DPP_DETIK))
    proses, keluaran, galat = _mulai_ffmpeg(folder, w, h)
    berkas_log = folder / "ffmpeg.log"

    def _sebab_mati() -> str:
        try:
            if not galat.closed:
                galat.flush()
            b = [x for x in berkas_log.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
        except (OSError, ValueError):
            b = []
        if proses.returncode == -9:
            return "ffmpeg dihentikan paksa oleh sistem (SIGKILL) - hampir pasti kehabisan memori."
        return f"ffmpeg keluar dengan kode {proses.returncode}" + (f": {b[-1][:200]}" if b else " tanpa pesan.")

    try:
        lapor_tiap = max(1, total // 12)
        for n in range(total):
            if ev.is_set():
                proses.kill()
                raise Dibatalkan("Render dihentikan.")
            t = n / FPS
            frame = latar.copy()
            _gelombang_dpp(ImageDraw.Draw(frame), w, h, t)

            # logo: putaran-balik (lebar mengikuti |cos|) + lentingan ukuran
            if t >= DPP_T_LOGO:
                u = t - DPP_T_LOGO
                p = _clamp(u / 1.0)
                # putaran-balik selesai dalam 0,3 detik; ukurannya melonjak ke
                # 1,35x, bertahan sebentar, lalu turun ke ukuran akhir - persis
                # urutan frame acuan pada 1,0 / 1,1 / 1,4 / 1,7 / 1,9 detik.
                sudut = (1 - _halus(_clamp(u / 0.3))) * math.pi / 2
                if u < 0.45:
                    sk = 0.2 + 1.15 * _halus(u / 0.45)
                elif u < 0.75:
                    sk = 1.35
                else:
                    sk = 1.35 - 0.35 * _halus((u - 0.75) / 0.25)
                sk = max(0.05, sk)
                tinggi_l = int(D * sk)
                lebar_l = int(D * sk * max(0.04, abs(math.cos(sudut))))
                im_l = logo_ukuran(lebar_l, tinggi_l)
                apung = int(4 * math.sin(t * 1.6)) if p >= 1 else 0
                # tingkat ukuran dibulatkan ke 16 langkah supaya cache-nya kena
                _tempel(frame, im_l, bx + (D - im_l.width) // 2, by + (D - im_l.height) // 2 + apung,
                        _clamp((t - DPP_T_LOGO) / 0.2))

            # tagline: naik dari bawah sambil memudar masuk
            if t >= DPP_T_TAGLINE:
                a = _halus((t - DPP_T_TAGLINE) / 0.35)
                _tempel(frame, tag_im, (w - tag_im.width) // 2, tag_y + int(180 * (1 - a)), a)

            # handle: masuk hampir bersamaan dari kiri
            for i, (ik, tx) in enumerate(baris):
                mulai = DPP_T_HANDLE + i * 0.06
                if t < mulai:
                    continue
                a = _halus((t - mulai) / 0.35)
                geser = int(-50 * (1 - a))
                y = baris_y0 + i * pitch
                _tempel(frame, ik, int(w * 0.225) + geser, y, a)
                _tempel(frame, tx, int(w * 0.325) + geser, y + (ikon_s - tx.height) // 2, a)

            # pil: cahayanya datang lebih dulu, garis pilnya menyusul
            if t >= DPP_T_PIL:
                a_cahaya = _halus((t - DPP_T_PIL) / 0.25)
                a_pil = _halus((t - DPP_T_PIL - 0.2) / 0.35)
                if a_pil > 0:
                    _tempel(frame, pil_im, pil_x, pil_y, a_pil)
                cahaya = _cahaya_pil_dpp(pil_w, pil_h, pil_titik, ((t - DPP_T_PIL) / 1.6) % 1.0)
                _tempel(frame, cahaya, pil_x, pil_y, a_cahaya)

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
        raise OutroError(f"Gagal merender outro DPP: {_sebab_mati()}")
    lapor(100)
    return keluaran


def tempel_audio_dpp(video: Path, folder: Path) -> Path:
    """Pasang jalur suara outro acuan apa adanya (sudah sejajar dari detik 0)."""
    keluaran = folder / "outro.mp4"
    if not DPP_AUDIO.is_file():
        shutil.copy2(video, keluaran)
        return keluaran
    utas = os.getenv("VIDEO_THREADS", "1") or "1"
    hasil = subprocess.run(
        [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", utas,
         "-i", str(video), "-i", str(DPP_AUDIO),
         "-map", "0:v", "-map", "1:a", "-shortest",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", str(keluaran)],
        capture_output=True, text=True, timeout=120,
    )
    if hasil.returncode != 0 or not keluaran.is_file():
        logger.warning("audio DPP gagal ditempel, video tanpa audio dipakai: %s", (hasil.stderr or "").strip()[-200:])
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
