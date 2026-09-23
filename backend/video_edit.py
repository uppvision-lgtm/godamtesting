"""Mesin auto edit video: unduh dari link lalu susun dengan template berlapis.

Alurnya:

1. ``download_source`` mengunduh video dari link apa pun yang dikenali yt-dlp
   (Instagram, TikTok, YouTube, atau URL file MP4 langsung).
2. ``render`` menyusun hasil akhir dalam SATU proses ffmpeg: video sumber
   dipasang ke kanvas template, ditumpuk overlay PNG dan teks, lalu disambung
   dengan intro/outro. Satu kali encode saja supaya hemat CPU dan tidak ada
   penurunan kualitas berlapis.

Template disimpan sebagai folder di ``MEDIA_DIR/templates/<id>`` berisi
``template.json`` dan berkas asetnya. Lihat ``TEMPLATE_CONTOH`` untuk bentuknya.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import storage

logger = logging.getLogger(__name__)

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", ".media"))

# Pemilik template = username akun. String kosong berarti template bawaan
# milik aplikasi: yang dilihat tamu, dan yang dipinjam tiap akun baru.
PEMILIK_BAWAAN = ""
ID_TEMPLATE_BAWAAN = os.getenv("TEMPLATE_BAWAAN_ID", "bawaan-tv-rakyat")
# Template dari masa sebelum ada akun tidak menyimpan pemilik. Semuanya
# dianggap milik akun ini.
PEMILIK_LAMA = os.getenv("TEMPLATE_PEMILIK_LAMA", "godam").strip().lower()

# Pemilik yang diawali ini adalah tamu: orang yang belum masuk. Mereka boleh
# memakai semua fitur, persis seperti profil tamu di peramban, tetapi
# templatenya hanya hidup di disk server ini — tidak pernah naik ke Supabase,
# dan dihapus otomatis setelah beberapa jam.
AWALAN_TAMU = "guest:"
TAMU_SIMPAN_JAM = float(os.getenv("VIDEO_GUEST_RETENTION_HOURS", "12"))
# Batas jumlah template per tamu, supaya satu peramban tidak bisa memenuhi disk.
MAKS_TEMPLATE_TAMU = int(os.getenv("VIDEO_GUEST_MAX_TEMPLATES", "20"))
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")

# Preset encode. "veryfast" dipilih karena render jalan di server kecil;
# naikkan ke "medium" kalau CPU-nya lega dan ingin berkas lebih kecil.
VIDEO_PRESET = os.getenv("VIDEO_PRESET", "veryfast")
VIDEO_CRF = os.getenv("VIDEO_CRF", "23")
VIDEO_THREADS = os.getenv("VIDEO_THREADS", "0")

# Batas aman supaya satu job tidak menyandera server selamanya.
# Durasi terpanjang video sumber yang mau diproses. LEBIH DARI INI DITOLAK,
# bukan dipotong: memotong diam-diam berarti orang menunggu render selesai
# lalu menemukan videonya terpenggal di menit kelima tanpa pernah diberi tahu.
# Sekaligus menutup jalur pemborosan - satu tautan ke video berjam-jam dulu
# tetap diunduh utuh sebelum dipotong.
MAX_SOURCE_SECONDS = float(os.getenv("VIDEO_MAX_SOURCE_SECONDS", "600"))
RENDER_TIMEOUT_SECONDS = float(os.getenv("VIDEO_RENDER_TIMEOUT", "1800"))
DOWNLOAD_TIMEOUT_SECONDS = float(os.getenv("VIDEO_DOWNLOAD_TIMEOUT", "600"))
PREVIEW_TIMEOUT_SECONDS = float(os.getenv("VIDEO_PREVIEW_TIMEOUT", "45"))
# Jeda minimal antar penyelarasan daftar set layer dengan Supabase.
SINKRON_JEDA_DETIK = float(os.getenv("VIDEO_TEMPLATE_SYNC_SECONDS", "20"))
# Video sumber disimpan sementara per link, supaya merender satu link dengan
# banyak template tidak mengunduhnya berulang kali (Instagram cepat membatasi
# pengunduh yang terlalu sering).
SOURCE_CACHE_SECONDS = float(os.getenv("VIDEO_SOURCE_CACHE_SECONDS", "21600"))
# Video sumber yang diunggah dari komputer pengguna. Batasnya sengaja lebih
# kecil dari aset layer: video sumber diproses utuh oleh ffmpeg, dan berkas
# besar membuat unggahannya sendiri lama dan rawan putus.
# Batasnya tidak boleh melewati apa yang mau diterima Supabase. Yang lebih
# besar TETAP terunggah ke disk backend dan dibalas 200, tapi diam-diam
# ditolak Supabase - dan karena worker hanya bisa mengambil dari sana,
# rendernya kemudian gagal dengan "video sumber sudah tidak ada di server",
# yang diulang berapa kali pun tidak akan pernah berhasil.
MAX_SOURCE_UPLOAD_MB = min(
    float(os.getenv("VIDEO_MAX_SOURCE_UPLOAD_MB", "100")),
    storage.batas_unggah_mb(),
)
AWALAN_UNGGAHAN = "upload://"
# Tempat video unggahan disimpan di Supabase, supaya worker yang berjalan
# sebagai layanan terpisah tetap bisa mengambilnya.
AWALAN_UNGGAHAN_STORAGE = "uploads/"

# yt-dlp dipasang sebagai paket Python. Di image Docker ia ada di PATH, tapi
# saat dijalankan dari virtualenv (mode lokal) sering tidak. Jadi dicari dulu,
# dan kalau tetap tidak ketemu dipanggil sebagai modul: "python -m yt_dlp".
YTDLP_BIN = os.getenv("YTDLP_BIN", "").strip()

# Instagram membalas 429 ("Too Many Requests") ke pengunduh yang tidak masuk,
# terutama dari alamat pusat data seperti Railway. Sekali kena, mencoba lagi
# langsung justru memperpanjang hukumannya - jadi tautan Instagram ditahan
# dulu selama jeda ini sebelum server berani menyentuh Instagram lagi.
JEDA_BATAS_DETIK = float(os.getenv("VIDEO_JEDA_BATAS", "180"))
# Berapa kali satu unduhan boleh diulang saat kena 429, dan berapa lama
# menunggu sebelum tiap ulangan. Angkanya sengaja jauh lebih panjang dari
# jeda bawaan yt-dlp: 429 dari Instagram tidak hilang dalam hitungan detik.
TUNGGU_ULANG_DETIK = (20.0, 60.0)
# Jalan keluar terakhir kalau alamat servernya memang sudah dicap Instagram:
# lewatkan unduhan melalui proxy. Kosong = langsung, seperti biasa. Dibuat
# sebagai setelan supaya nanti cukup diisi di Railway, tanpa ubah kode.
UNDUH_PROXY = os.getenv("VIDEO_UNDUH_PROXY", "").strip()

# Kandidat font untuk drawtext: Debian (image Docker) lalu macOS (mode via PC).
# Poppins-Bold ikut dibundel supaya teks di video sama persis dengan skrip
# main.py, di laptop maupun di container. Sisanya cuma jaring pengaman kalau
# berkas itu hilang: Debian (image Docker) lalu macOS.
FONT_BUNDLED = str(Path(__file__).resolve().parent / "assets" / "Poppins-Bold.ttf")

FONT_CANDIDATES = (
    os.getenv("VIDEO_FONT", ""),
    FONT_BUNDLED,
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)

TEMPLATE_CONTOH: dict[str, Any] = {
    "name": "Template Reels",
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "intro": None,
    "outro": None,
    "overlays": [],
    "texts": [],
    # Kotak tempat teks hook diletakkan, dalam piksel kanvas: {x, y, w, h}.
    # Diisi lewat deteksi otomatis atau digambar pengguna di halaman.
    "text_box": None,
    # Kotak badge kategori (NEWS / HIBURAN / SHOWBIZ) dan teksnya. Kalau
    # kotaknya kosong, diturunkan dari text_box: menempel di atas kotak teks,
    # rata kiri.
    "badge_box": None,
    "kategori": "",
    # Warna tulisan isi hook: "white" untuk kotak gelap, "black" untuk terang.
    "teks_warna": "white",
}


class VideoError(RuntimeError):
    """Kesalahan yang layak ditampilkan apa adanya ke pengguna."""


class Dibatalkan(VideoError):
    """Render dihentikan atas permintaan pengguna, bukan karena gagal.

    Dipisah dari VideoError biasa supaya halaman bisa membedakan "kamu yang
    menghentikan" dari "ada yang salah".
    """


# ============================================================
#  LOKASI BERKAS
# ============================================================


def templates_dir() -> Path:
    path = MEDIA_DIR / "templates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def jobs_dir() -> Path:
    path = MEDIA_DIR / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir() -> Path:
    path = MEDIA_DIR / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = MEDIA_DIR / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bersihkan_cache_unduhan(batas_detik: float | None = None) -> int:
    """Hapus unduhan sumber yang sudah melewati umur cache.

    SOURCE_CACHE_SECONDS selama ini hanya dipakai untuk menilai apakah sebuah
    unduhan masih layak dipakai ulang, tidak pernah untuk membuangnya. Tanpa
    ini foldernya tumbuh terus: satu video sumber puluhan MB per link yang
    pernah dirender.
    """
    batas = time.time() - (SOURCE_CACHE_SECONDS if batas_detik is None else batas_detik)
    dihapus = 0
    for isi in list(cache_dir().iterdir()):
        try:
            # Kunci unduhan (folder .lock) yang basi ikut dibuang; kalau tidak,
            # link yang gagal di tengah jalan bisa terkunci selamanya.
            if isi.stat().st_mtime >= batas:
                continue
        except OSError:
            continue
        if isi.is_dir():
            shutil.rmtree(isi, ignore_errors=True)
        else:
            isi.unlink(missing_ok=True)
        dihapus += 1
    if dihapus:
        logger.info("%d unduhan sumber kedaluwarsa dibuang dari cache", dihapus)
    return dihapus


def _tarik_unggahan_dari_storage(id_unggahan: str, folder: Path) -> list[Path]:
    """Ambil video sumber unggahan dari Supabase ke disk ini."""
    if not storage.enabled():
        return []
    awalan = f"{AWALAN_UNGGAHAN_STORAGE}{id_unggahan}"
    try:
        isi = storage.list_prefix(awalan)
    except storage.StorageError as error:
        logger.warning("Gagal melihat unggahan %s di Supabase: %s", id_unggahan, error)
        return []
    for item in isi:
        nama = str(item.get("name") or "")
        if not nama.startswith("source."):
            continue
        tujuan = folder / _aman(nama)
        if _tarik_dari_storage(f"{awalan}/{nama}", tujuan):
            return [tujuan]
    return []


def bersihkan_unggahan_lama(batas_detik: float, sentuh_storage: bool = True) -> int:
    """Hapus video sumber unggahan yang lebih tua dari batas.

    ``sentuh_storage`` dimatikan saat backend berjalan di laptop: bucketnya
    dipakai bersama server, dan laptop tidak berhak membuang unggahan milik
    pengguna situs. Yang lokal tetap dibersihkan.
    """
    dihapus = 0
    for folder in uploads_dir().iterdir():
        if folder.is_dir() and time.time() - folder.stat().st_mtime > batas_detik:
            if sentuh_storage:
                _hapus_unggahan_di_storage(folder.name)
            shutil.rmtree(folder, ignore_errors=True)
            dihapus += 1
    return dihapus


def _hapus_unggahan_di_storage(id_unggahan: str) -> None:
    """Buang salinan unggahan di Supabase; kalau tidak, ia menumpuk selamanya."""
    if not storage.enabled():
        return
    awalan = f"{AWALAN_UNGGAHAN_STORAGE}{_aman(id_unggahan)}"
    try:
        for item in storage.list_prefix(awalan):
            nama = str(item.get("name") or "")
            if nama:
                storage.delete(f"{awalan}/{nama}")
    except storage.StorageError as error:
        logger.warning("Unggahan %s gagal dibuang dari Supabase: %s", id_unggahan, error)


# Sisa ruang yang sengaja tidak dipakai unggahan, supaya render dan berkas
# sementara masih punya tempat walau volumenya hampir habis.
RUANG_CADANGAN_MB = float(os.getenv("VIDEO_RUANG_CADANGAN_MB", "80"))

# Umur simpan berkas di volume. Sama dengan milik worker, tapi dibaca di sini
# supaya video_edit tidak perlu mengimpor video_tasks (yang mengimpor modul ini).
UMUR_SIMPAN_JAM = float(os.getenv("VIDEO_JOB_RETENTION_HOURS", "24"))


def ruang_media() -> dict[str, float]:
    """Ruang disk tempat MEDIA_DIR berada, dalam MB."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    total, dipakai, sisa = shutil.disk_usage(MEDIA_DIR)
    return {
        "total_mb": round(total / 1_048_576, 1),
        "dipakai_mb": round(dipakai / 1_048_576, 1),
        "sisa_mb": round(sisa / 1_048_576, 1),
    }


def isi_media() -> dict[str, float]:
    """Pemakaian tiap folder di bawah MEDIA_DIR, dalam MB, untuk diagnosa."""
    hasil: dict[str, float] = {}
    if not MEDIA_DIR.is_dir():
        return hasil
    for anak in sorted(MEDIA_DIR.iterdir()):
        if not anak.is_dir():
            continue
        jumlah = 0
        for isi in anak.rglob("*"):
            try:
                if isi.is_file():
                    jumlah += isi.stat().st_size
            except OSError:
                continue
        hasil[anak.name] = round(jumlah / 1_048_576, 1)
    return hasil


# Berapa lama salinan lokal video unggahan ditahan setelah sampai di Supabase.
UNGGAHAN_LOKAL_MENIT = float(os.getenv("VIDEO_UNGGAHAN_LOKAL_MENIT", "45"))


def rampingkan_unggahan(umur_menit: float | None = None) -> int:
    """Buang salinan LOKAL unggahan yang sudah aman tersimpan di Supabase.

    Video sumber unggahan hanya perlu ada di disk backend sampai salinannya
    sampai di Supabase: yang merender adalah worker, container lain dengan
    disk lain, yang memang selalu mengambilnya dari sana. Menahan salinan itu
    24 jam di volume 500 MB berarti lima unggahan saja sudah memenuhinya -
    dan sejak penuh, SEMUA unggahan berikutnya gagal.

    Yang dibuang hanya yang benar-benar sudah ada di Supabase; kalau
    pengecekannya gagal, salinan lokalnya dibiarkan.
    """
    if not storage.enabled():
        return 0
    batas = time.time() - float(
        UNGGAHAN_LOKAL_MENIT if umur_menit is None else umur_menit
    ) * 60
    dibuang = 0
    try:
        daftar = list(uploads_dir().iterdir())
    except OSError:
        return 0
    for folder in daftar:
        if not folder.is_dir():
            continue
        berkas = [b for b in folder.glob("source.*") if b.is_file()]
        if not berkas or max(b.stat().st_mtime for b in berkas) > batas:
            continue
        try:
            di_storage = {
                str(item.get("name") or "")
                for item in storage.list_prefix(f"{AWALAN_UNGGAHAN_STORAGE}{folder.name}")
            }
        except storage.StorageError as error:
            logger.warning("Unggahan %s tak bisa dicek di Supabase: %s", folder.name, error)
            continue
        for b in berkas:
            if b.name in di_storage:
                b.unlink(missing_ok=True)
                dibuang += 1
    if dibuang:
        logger.info("%d salinan lokal unggahan dibuang (sudah ada di Supabase)", dibuang)
    return dibuang


def bersihkan_volume(
    umur_jam: float | None = None, sentuh_storage: bool = True
) -> dict[str, Any]:
    """Buang berkas kedaluwarsa di volume PROSES INI, lalu laporkan.

    Selama ini satu-satunya pembersihan hidup di dalam task Celery, jadi yang
    tersapu selalu disk WORKER - /tmp, yang lagipula kosong lagi tiap deploy.
    Volume backend, tempat video unggahan pengguna sebenarnya mendarat, tidak
    pernah disentuh siapa pun: seratus MB per unggahan menumpuk sampai
    "No space left on device", dan sejak itu tiap unggahan gagal dengan 500.
    Ini bagian backendnya, dipanggil saat start, berkala, dan sebelum menerima
    unggahan baru.
    """
    umur = float(UMUR_SIMPAN_JAM if umur_jam is None else umur_jam)
    sebelum = ruang_media()
    laporan: dict[str, Any] = {"sebelum": sebelum, "umur_jam": umur}
    try:
        laporan["unggahan"] = bersihkan_unggahan_lama(umur * 3600, sentuh_storage)
    except OSError as error:
        logger.warning("Pembersihan unggahan gagal: %s", error)
        laporan["unggahan"] = 0
    try:
        laporan["ramping"] = rampingkan_unggahan()
    except OSError as error:
        logger.warning("Perampingan unggahan gagal: %s", error)
        laporan["ramping"] = 0
    try:
        laporan["cache"] = bersihkan_cache_unduhan()
    except OSError as error:
        logger.warning("Pembersihan cache unduhan gagal: %s", error)
        laporan["cache"] = 0
    # Sisa folder job: di backend biasanya kosong, tapi kalau backend pernah
    # ikut merender (mode lokal) folder-folder itu tidak ada yang membuang.
    dibuang_job = 0
    try:
        batas = time.time() - umur * 3600
        for folder in jobs_dir().iterdir():
            if folder.is_dir() and folder.stat().st_mtime < batas:
                shutil.rmtree(folder, ignore_errors=True)
                dibuang_job += 1
    except OSError as error:
        logger.warning("Pembersihan folder job gagal: %s", error)
    laporan["job"] = dibuang_job
    # Video outro dirender oleh proses backend ini, jadi hasilnya mendarat di
    # volume yang sama. Pembersihnya sudah lama ada di outro.py tapi tidak
    # pernah ada yang memanggilnya - dengan 100 outro sehari, itu volume
    # penuh dalam hitungan hari.
    try:
        import outro  # lokal: outro.py mengimpor modul ini

        laporan["outro"] = outro.bersihkan_lama(umur)
    except Exception as error:  # noqa: BLE001
        logger.warning("Pembersihan outro gagal: %s", error)
        laporan["outro"] = 0
    laporan["sesudah"] = ruang_media()
    laporan["dibebaskan_mb"] = round(
        laporan["sesudah"]["sisa_mb"] - sebelum["sisa_mb"], 1
    )
    if laporan["dibebaskan_mb"] >= 1:
        logger.info(
            "Volume dibersihkan: %s MB dibebaskan, sisa %s MB",
            laporan["dibebaskan_mb"], laporan["sesudah"]["sisa_mb"],
        )
    return laporan


def sediakan_ruang(butuh_mb: float) -> float:
    """Usahakan ada ruang untuk berkas sebesar ini; kembalikan sisa ruang MB.

    Dicoba bertahap: kalau sisa disk sudah cukup tidak ada yang dihapus sama
    sekali; kalau kurang, berkas kedaluwarsa dibuang; kalau masih kurang juga,
    umur simpannya diperpendek sampai satu jam. Pemanggilnya yang memutuskan
    mau menolak atau jalan terus.
    """
    perlu = float(butuh_mb) + RUANG_CADANGAN_MB
    sisa = ruang_media()["sisa_mb"]
    if sisa >= perlu:
        return sisa
    sisa = bersihkan_volume(sentuh_storage=SENTUH_STORAGE)["sesudah"]["sisa_mb"]
    if sisa >= perlu:
        return sisa
    for umur in (6.0, 1.0):
        if umur >= UMUR_SIMPAN_JAM:
            continue
        sisa = bersihkan_volume(umur, SENTUH_STORAGE)["sesudah"]["sisa_mb"]
        if sisa >= perlu:
            break
    return sisa


def _aman(nama: str) -> str:
    """Buang karakter yang bisa dipakai keluar dari folder media."""
    bersih = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(nama or "").strip())
    bersih = bersih.lstrip(".") or "tanpa-nama"
    return bersih[:80]


def template_path(template_id: str) -> Path:
    return templates_dir() / _aman(template_id)


def _tarik_dari_storage(path_relatif: str, tujuan: Path) -> bool:
    """Ambil satu berkas dari Supabase ke disk lokal. True kalau berhasil."""
    if not storage.enabled():
        return False
    try:
        storage.download(path_relatif, tujuan)
    except storage.StorageError:
        pass
    if tujuan.is_file():
        return True
    # Folder yang dibuat untuk menampung unduhan yang ternyata tidak ada
    # dibuang lagi; kalau tidak, setiap id template yang salah ketik
    # meninggalkan folder kosong di disk.
    try:
        if tujuan.parent.is_dir() and not any(tujuan.parent.iterdir()):
            tujuan.parent.rmdir()
    except OSError:
        pass
    return False


# Worker render tidak pernah membuka daftar template; ia hanya menarik berkas
# yang benar-benar dipakai. Menyalakan sinkronisasi di sana cuma menambah
# permintaan ke Supabase tanpa guna. start.sh mematikannya untuk peran worker.
SINKRON_TEMPLATE_AKTIF = os.getenv("TEMPLATE_SYNC", "on").strip().lower() not in (
    "0",
    "off",
    "false",
    "no",
)

_sinkron_terakhir = 0.0
_sinkron_jalan = threading.Lock()


def sinkron_template_dari_storage(paksa: bool = False) -> int:
    """Tarik set layer yang ada di Supabase tapi belum ada di disk ini.

    Dijalankan tiap kali daftar set dibuka, bukan hanya saat disk kosong:
    set yang dibuat dari perangkat atau container lain tersimpan di Supabase,
    dan tanpa ini tidak pernah muncul di sini. Diberi jeda supaya membuka
    daftar berkali-kali tidak membanjiri Supabase dengan permintaan.
    """
    global _sinkron_terakhir
    if not storage.enabled() or not SINKRON_TEMPLATE_AKTIF:
        return 0
    if not paksa and time.time() - _sinkron_terakhir < SINKRON_JEDA_DETIK:
        return 0
    if not _sinkron_jalan.acquire(blocking=False):
        return 0  # sudah ada yang menyinkronkan; tidak perlu dua-duanya
    _sinkron_terakhir = time.time()
    ditarik = 0
    try:
        for item in storage.list_prefix("templates"):
            nama = str(item.get("name") or "")
            if not nama:
                continue
            tujuan = templates_dir() / nama / "template.json"
            if tujuan.is_file():
                continue
            if _tarik_dari_storage(f"templates/{nama}/template.json", tujuan):
                ditarik += 1
    except storage.StorageError as error:
        logger.warning("Gagal menyinkronkan template dari Supabase: %s", error)
    finally:
        _sinkron_jalan.release()
    return ditarik


def sinkron_di_latar(paksa: bool = False) -> None:
    """Jalankan sinkronisasi tanpa menahan permintaan yang sedang dilayani.

    Sinkronisasi memakan ratusan milidetik karena menunggu Supabase, sedangkan
    bagian lokalnya cuma sepersekian itu. Membuka halaman tidak seharusnya ikut
    menunggu; daftar terbaru cukup muncul pada pembukaan berikutnya.
    """
    if not storage.enabled() or not SINKRON_TEMPLATE_AKTIF:
        return
    if _sinkron_jalan.locked():
        return
    threading.Thread(
        target=sinkron_template_dari_storage,
        kwargs={"paksa": paksa},
        name="sinkron-template",
        daemon=True,
    ).start()


def perbaiki_storage(template_id: str | None = None) -> dict[str, int]:
    """Dorong template & aset yang ada di disk ini tapi belum ada di Supabase.

    Backend menulis ke volume miliknya sendiri, sedangkan worker berjalan di
    container lain tanpa volume itu - Railway tidak bisa memasang satu volume
    ke dua layanan. Jadi satu-satunya jalan berkas sampai ke worker adalah
    Supabase. Kalau ada yang tidak terkirim ke sana, template itu tampak utuh
    di halaman (backend membacanya dari disk) tetapi setiap rendernya gagal
    dengan "template tidak ditemukan" atau "aset tidak ditemukan" - persis
    yang terjadi pada 10 dari 36 set layer yang ada.

    Yang belum ada saja yang dikirim, jadi aman dijalankan berkali-kali.
    Template tamu dilewati: memang tidak untuk disimpan.
    """
    hasil = {"template": 0, "aset": 0, "pinjam": 0, "gagal": 0}
    if not storage.enabled():
        return hasil
    akar = templates_dir()
    if not akar.is_dir():
        return hasil
    # Isi tiap set layer dikumpulkan sambil jalan, supaya pencarian pemberi
    # pinjaman di bawah tidak perlu membaca ulang semuanya dari awal.
    milik: dict[str, dict[str, Any]] = {}
    if template_id:
        folder_template = [akar / _aman(template_id)]
    else:
        folder_template = sorted(p for p in akar.iterdir() if p.is_dir())
    for folder in folder_template:
        berkas_json = folder / "template.json"
        if not berkas_json.is_file():
            continue
        tid = folder.name
        try:
            isi = json.loads(berkas_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if is_tamu(str(isi.get("owner") or "")):
            continue
        try:
            punya = {i.get("name") for i in storage.list_prefix(f"templates/{tid}", limit=100)}
            punya_aset = {
                i.get("name")
                for i in storage.list_prefix(f"templates/{tid}/assets", limit=500)
            }
        except storage.StorageError as error:
            logger.warning("Tidak bisa membaca isi Supabase untuk %s: %s", tid, error)
            hasil["gagal"] += 1
            continue
        if "template.json" not in punya:
            try:
                storage.upload(
                    f"templates/{tid}/template.json", berkas_json, "application/json"
                )
                hasil["template"] += 1
                logger.info("Template %s didorong ke Supabase", tid)
            except storage.StorageError as error:
                logger.warning("Template %s gagal didorong: %s", tid, error)
                hasil["gagal"] += 1
        for aset in sorted((folder / "assets").glob("*")):
            if not aset.is_file() or aset.name in punya_aset:
                continue
            try:
                storage.upload(f"templates/{tid}/assets/{aset.name}", aset)
                hasil["aset"] += 1
                logger.info("Aset %s/%s didorong ke Supabase", tid, aset.name)
            except storage.StorageError as error:
                logger.warning("Aset %s/%s gagal didorong: %s", tid, aset.name, error)
                hasil["gagal"] += 1
        punya_aset |= {a.name for a in (folder / "assets").glob("*") if a.is_file()}
        milik[tid] = {"isi": isi, "aset": punya_aset}

    hasil["pinjam"] = _pinjamkan_aset_hilang(milik)
    return hasil


def _pinjamkan_aset_hilang(milik: dict[str, dict[str, Any]]) -> int:
    """Set layer yang berkasnya sudah lenyap dibuat meminjam dari set lain.

    Beberapa set layer merujuk berkas yang sudah tidak ada di mana pun -
    tidak di disk, tidak di Supabase. Hasilnya set itu tampak utuh di halaman
    (pratinjaunya cuma kanvas kosong) tetapi setiap rendernya gagal dengan
    "aset tidak ditemukan". Berkas itu sendiri biasanya masih ada di set lain
    milik orang yang sama: set-set ini lahir dari menyalin, jadi nama
    berkasnya sama persis.

    Menyalin berkasnya kembali akan menggandakan puluhan MB untuk isi yang
    identik, padahal volume backend sudah 404 dari 500 MB. Jadi yang dipakai
    adalah "aset_dari" - mekanisme yang memang sudah ada di kode ini supaya
    set salinan meminjam berkas set induknya alih-alih menyalinnya.

    Pemberi pinjaman harus punya SELURUH berkas yang hilang dan dimiliki
    orang yang sama; kalau tidak ada yang memenuhi, set itu dilewati.
    """
    dipinjamkan = 0
    for tid, data in milik.items():
        isi = data["isi"]
        if str(isi.get("aset_dari") or "").strip():
            continue  # sudah meminjam dari suatu tempat
        perlu = {Path(n).name for n in aset_terpakai(isi)}
        hilang = perlu - data["aset"]
        if not hilang:
            continue
        pemilik = str(isi.get("owner") or "").strip().lower()
        pemberi = next(
            (
                lain
                for lain, d in sorted(milik.items())
                if lain != tid
                and hilang <= d["aset"]
                and str(d["isi"].get("owner") or "").strip().lower() == pemilik
            ),
            "",
        )
        if not pemberi:
            logger.warning(
                "Set layer %s kehilangan %s dan tidak ada set lain yang punya",
                tid, ", ".join(sorted(hilang)),
            )
            continue
        isi["aset_dari"] = pemberi
        berkas = template_path(tid) / "template.json"
        try:
            berkas.write_text(json.dumps(isi, indent=2, ensure_ascii=False), encoding="utf-8")
            if storage.enabled():
                storage.upload(f"templates/{tid}/template.json", berkas, "application/json")
        except (OSError, storage.StorageError) as error:
            logger.warning("Set layer %s gagal dibuat meminjam: %s", tid, error)
            continue
        dipinjamkan += 1
        logger.info(
            "Set layer %s meminjam %s dari %s", tid, ", ".join(sorted(hilang)), pemberi
        )
    return dipinjamkan


def perbaiki_storage_di_latar() -> None:
    """Jalankan perbaikan tanpa menahan permintaan yang sedang dilayani."""
    if not storage.enabled():
        return
    threading.Thread(
        target=lambda: logger.info("Perbaikan storage selesai: %s", perbaiki_storage()),
        name="perbaiki-storage",
        daemon=True,
    ).start()


# Selang antar-sapuan penjaga volume, dalam menit.
SELANG_SAPUAN_MENIT = float(os.getenv("VIDEO_SAPUAN_MENIT", "30"))

# Apakah proses ini berhak membuang berkas dari bucket bersama. Dimatikan
# oleh main.py saat mode lokal; lihat bersihkan_unggahan_lama.
SENTUH_STORAGE = True


def jaga_volume_di_latar(sentuh_storage: bool = True) -> None:
    """Sapu volume proses ini saat start, lalu ulangi setiap setengah jam.

    Tanpa ini tidak ada satu pun yang membersihkan volume backend: satu-satunya
    pembersihan yang ada berjalan di dalam task Celery, di container worker,
    di disk yang berbeda.
    """

    global SENTUH_STORAGE
    SENTUH_STORAGE = bool(sentuh_storage)

    def putaran() -> None:
        while True:
            try:
                bersihkan_volume(sentuh_storage=SENTUH_STORAGE)
            except Exception:  # noqa: BLE001
                logger.exception("Sapuan volume gagal")
            time.sleep(max(60.0, SELANG_SAPUAN_MENIT * 60))

    threading.Thread(target=putaran, name="jaga-volume", daemon=True).start()


def load_template(template_id: str) -> dict[str, Any]:
    berkas = template_path(template_id) / "template.json"
    if not berkas.is_file():
        _tarik_dari_storage(f"templates/{_aman(template_id)}/template.json", berkas)
    try:
        data = json.loads(berkas.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise VideoError(f"Template '{template_id}' tidak ditemukan") from error
    except json.JSONDecodeError as error:
        raise VideoError(f"Template '{template_id}' rusak: {error}") from error
    data["id"] = _aman(template_id)
    if "owner" not in data:
        data["owner"] = PEMILIK_LAMA
    data["owner"] = str(data.get("owner") or "").strip().lower()
    return data


def pemilik_template(template: dict[str, Any]) -> str:
    return str(template.get("owner") or "").strip().lower()


def is_tamu(pemilik: str | None) -> bool:
    """True kalau pemilik ini tamu (belum masuk), bukan akun terdaftar."""
    return bool(pemilik) and str(pemilik).startswith(AWALAN_TAMU)


def boleh_lihat(template: dict[str, Any], username: str | None) -> bool:
    """Template bawaan terlihat oleh semua; selain itu hanya oleh pemiliknya."""
    pemilik = pemilik_template(template)
    return pemilik == PEMILIK_BAWAAN or (bool(username) and pemilik == username)


def boleh_ubah(template: dict[str, Any], username: str | None) -> bool:
    """Hanya pemiliknya. Template bawaan tidak bisa diubah lewat API."""
    return bool(username) and pemilik_template(template) == username


def _masuk_daftar(template: dict[str, Any], username: str | None) -> bool:
    """Isi daftar template menurut siapa yang membukanya.

    Akun terdaftar: miliknya sendiri. Tamu: miliknya sendiri ditambah template
    bawaan, karena template bawaan itulah titik awal untuk diduplikat.
    """
    pemilik = pemilik_template(template)
    if not username:
        return pemilik == PEMILIK_BAWAAN
    if is_tamu(username):
        return pemilik in (username, PEMILIK_BAWAAN)
    return pemilik == username


def list_templates(owner: str | None = None, semua: bool = False) -> list[dict[str, Any]]:
    """Template untuk satu akun (``owner``) atau untuk tamu (``None``).

    ``semua=True`` mengabaikan pemilik; dipakai skrip perawatan, bukan API.
    """
    # Hanya saat disk benar-benar kosong (container baru) daftar perlu
    # menunggu Supabase; selebihnya disegarkan di latar belakang.
    if not any(templates_dir().iterdir()):
        sinkron_template_dari_storage(paksa=True)
    else:
        sinkron_di_latar()
    bersihkan_template_tamu()
    hasil = []
    for folder in sorted(templates_dir().iterdir()):
        if not folder.is_dir():
            continue
        try:
            template = load_template(folder.name)
        except VideoError:
            continue
        if semua or _masuk_daftar(template, owner):
            hasil.append(template)
    return hasil


def _nama_set_terpakai(
    nama: str, kecuali_id: str = "", owner: str | None = None
) -> str | None:
    """Kembalikan id set lain milik ``owner`` yang sudah memakai nama ini, kalau ada.

    Nama hanya harus unik di dalam satu akun: dua orang boleh sama-sama punya
    "TV Rakyat". ``owner=None`` membandingkan lintas akun.

    Perbandingannya mengabaikan besar-kecil huruf dan spasi berlebih, supaya
    "TV Rakyat" dan "tv  rakyat" tidak dianggap dua nama berbeda. Membaca
    berkas lokal langsung, bukan lewat list_templates(), karena fungsi itu
    ikut menghubungi Supabase — terlalu berat untuk dijalankan tiap menyimpan.
    """
    target = " ".join(str(nama or "").split()).casefold()
    if not target:
        return None
    kecuali = _aman(kecuali_id) if kecuali_id else ""
    for folder in templates_dir().iterdir():
        if not folder.is_dir() or folder.name == kecuali:
            continue
        berkas = folder / "template.json"
        if not berkas.is_file():
            continue
        try:
            isi_lain = json.loads(berkas.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if owner is not None:
            pemilik_lain = str(isi_lain.get("owner", PEMILIK_LAMA) or "").strip().lower()
            if pemilik_lain != owner:
                continue
        lain = isi_lain.get("name")
        if " ".join(str(lain or "").split()).casefold() == target:
            return folder.name
    return None


def nama_set_unik(nama: str, kecuali_id: str = "", owner: str | None = None) -> str:
    """Tambahkan nomor di belakang nama sampai tidak ada yang memakainya."""
    dasar = " ".join(str(nama or "").split()) or "Set layer"
    calon = dasar
    urut = 2
    while _nama_set_terpakai(calon, kecuali_id, owner):
        calon = f"{dasar} {urut}"
        urut += 1
    return calon


def _baca_template_mentah(folder: Path) -> dict[str, Any] | None:
    berkas = folder / "template.json"
    if not folder.is_dir() or not berkas.is_file():
        return None
    try:
        return json.loads(berkas.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _jumlah_template(owner: str) -> int:
    jumlah = 0
    for folder in templates_dir().iterdir():
        isi = _baca_template_mentah(folder)
        if isi is not None and str(isi.get("owner") or "").strip().lower() == owner:
            jumlah += 1
    return jumlah


_bersih_tamu_terakhir = 0.0


def bersihkan_template_tamu(paksa: bool = False) -> int:
    """Buang template milik tamu yang sudah lewat batas simpan.

    Tamu tidak punya tempat penyimpanan permanen, jadi templatenya hanya
    menumpang di disk server. Tanpa ini, tiap peramban yang mampir
    meninggalkan folder yang tidak pernah dibuang.
    """
    global _bersih_tamu_terakhir
    sekarang = time.time()
    if not paksa and sekarang - _bersih_tamu_terakhir < 300:
        return 0
    _bersih_tamu_terakhir = sekarang
    batas = sekarang - TAMU_SIMPAN_JAM * 3600
    dihapus = 0
    for folder in list(templates_dir().iterdir()):
        isi = _baca_template_mentah(folder)
        if isi is None or not is_tamu(str(isi.get("owner") or "")):
            continue
        berkas = folder / "template.json"
        try:
            if berkas.stat().st_mtime >= batas:
                continue
        except OSError:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        dihapus += 1
    if dihapus:
        logger.info("%d template tamu kedaluwarsa dibuang", dihapus)
    return dihapus


def save_template(
    data: dict[str, Any], template_id: str | None = None, owner: str | None = None
) -> dict[str, Any]:
    """Simpan template.

    ``owner`` hanya dipakai saat membuat template baru. Template yang sudah
    ada tidak pernah berpindah pemilik lewat sini, dan pemiliknya tidak
    diambil dari data kiriman halaman.
    """
    template_id = _aman(template_id or data.get("id") or uuid.uuid4().hex[:12])
    folder = template_path(template_id)
    (folder / "assets").mkdir(parents=True, exist_ok=True)
    berkas = folder / "template.json"
    lama: dict[str, Any] = {}
    if berkas.is_file():
        try:
            lama = json.loads(berkas.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lama = {}
    if lama:
        pemilik = str(lama.get("owner", PEMILIK_LAMA) or "").strip().lower()
    else:
        pemilik = str((owner if owner is not None else data.get("owner")) or "").strip().lower()
    if not lama and is_tamu(pemilik) and _jumlah_template(pemilik) >= MAKS_TEMPLATE_TAMU:
        raise VideoError(
            f"Tamu hanya bisa punya {MAKS_TEMPLATE_TAMU} template sekaligus. "
            "Hapus salah satu, atau masuk supaya templatemu tersimpan."
        )
    nama = " ".join(str(data.get("name") or "").split())
    bentrok = _nama_set_terpakai(nama, template_id, pemilik)
    if bentrok:
        raise VideoError(
            f"Nama set layer \"{nama}\" sudah dipakai. Pilih nama lain supaya "
            "tidak tertukar di daftar."
        )
    isi = {**TEMPLATE_CONTOH, **data, "id": template_id, "name": nama, "owner": pemilik}
    # Berkas pinjaman dari template bawaan dicatat oleh server, bukan dikirim
    # halaman; kalau tidak ada di data kiriman, yang tersimpan dipertahankan.
    aset_dari = data.get("aset_dari") if "aset_dari" in data else lama.get("aset_dari")
    if aset_dari:
        isi["aset_dari"] = _aman(str(aset_dari))
    else:
        isi.pop("aset_dari", None)
    (folder / "template.json").write_text(
        json.dumps(isi, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # Template tamu sengaja tidak naik ke Supabase: itulah arti "tidak tersimpan".
    if storage.enabled() and not is_tamu(pemilik):
        try:
            storage.upload(
                f"templates/{template_id}/template.json",
                folder / "template.json",
                "application/json",
            )
        except storage.StorageError as error:
            logger.warning("Template tersimpan lokal tapi gagal ke Supabase: %s", error)
    return isi


def duplicate_template(
    sumber_id: str, nama_baru: str, id_baru: str | None = None, owner: str | None = None
) -> dict[str, Any]:
    """Salin satu set layer beserta seluruh berkas asetnya.

    Berkasnya benar-benar disalin, bukan dipakai bersama, supaya mengganti
    atau menghapus layer di salinan tidak ikut mengubah set aslinya. Itu juga
    berlaku untuk berkas pinjaman dari template bawaan: salinannya berdiri
    sendiri. Aset yang hanya ada di Supabase (mis. di container yang baru
    dijalankan) ditarik dulu, jadi hasil salinan tetap lengkap.

    ``owner`` = pemilik salinan; kalau None, mengikuti pemilik set asal.
    """
    asal = load_template(sumber_id)
    id_baru = _aman(id_baru or uuid.uuid4().hex[:12])
    if id_baru == _aman(sumber_id):
        raise VideoError("Set layer salinan harus memakai id yang berbeda")

    folder_asal = template_path(sumber_id)
    folder_baru = template_path(id_baru)
    (folder_baru / "assets").mkdir(parents=True, exist_ok=True)

    # Semua berkas yang dirujuk template, ditambah berkas lain di foldernya.
    nama_aset: list[str] = []
    dirujuk = [o.get("file") for o in (asal.get("overlays") or [])]
    dirujuk += [asal.get("intro"), asal.get("outro")]
    for nama in dirujuk:
        if nama and str(nama) not in nama_aset:
            nama_aset.append(str(nama))
    for berkas in sorted((folder_asal / "assets").glob("*")):
        if berkas.is_file() and f"assets/{berkas.name}" not in nama_aset:
            nama_aset.append(f"assets/{berkas.name}")

    disalin = 0
    for nama in nama_aset:
        try:
            sumber = _aset(asal, nama)
        except VideoError as error:
            logger.warning("Aset %s dilewati saat menyalin: %s", nama, error)
            continue
        if sumber is None:
            continue
        tujuan = folder_baru / nama
        if folder_baru.resolve() not in tujuan.resolve().parents:
            continue
        tujuan.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sumber, tujuan)
        disalin += 1
        if storage.enabled() and not is_tamu(owner if owner is not None else pemilik_template(asal)):
            try:
                storage.upload(f"templates/{id_baru}/{nama}", tujuan)
            except storage.StorageError as error:
                logger.warning("Aset %s gagal disalin ke Supabase: %s", nama, error)

    pemilik = owner if owner is not None else pemilik_template(asal)
    # Nama salinan diberi nomor otomatis kalau sudah ada yang memakainya,
    # jadi menyalin berkali-kali tidak menghasilkan nama kembar.
    diminta = nama_baru.strip() or f"{asal.get('name', 'Set')} (salinan)"
    isi = {**asal, "id": id_baru, "name": nama_set_unik(diminta, id_baru, pemilik)}
    # Sumber yang MEMINJAM asetnya (aset_dari) tidak punya berkas di foldernya
    # sendiri - berkasnya ada di set akar - jadi salinannya harus ikut
    # meminjam dari akar yang sama. Dulu pinjaman ini selalu dibuang, dan
    # salinan dari set pinjaman lahir tanpa satu aset pun: pratinjaunya
    # kosong dan setiap rendernya gagal "aset tidak ditemukan".
    if not str(asal.get("aset_dari") or "").strip():
        isi.pop("aset_dari", None)
    hasil = save_template(isi, id_baru, owner=pemilik)
    logger.info("Set %s disalin ke %s beserta %d berkas", sumber_id, id_baru, disalin)
    return hasil


def template_awal_untuk(username: str) -> dict[str, Any]:
    """Template pertama akun baru: TV Rakyat, meminjam berkas template bawaan.

    Berkasnya tidak disalin (lihat ``_aset``), jadi mendaftar tidak berarti
    menggandakan puluhan MB aset. Begitu pengguna mengunggah berkasnya
    sendiri, berkas itu masuk ke folder templatenya dan yang dipinjam tidak
    dipakai lagi.
    """
    username = str(username or "").strip().lower()
    if not username:
        raise VideoError("Username kosong")
    bawaan = load_template(ID_TEMPLATE_BAWAAN)
    id_baru = uuid.uuid4().hex[:12]
    nama = str(bawaan.get("name") or "TV Rakyat")
    isi = {
        **bawaan,
        "id": id_baru,
        "name": nama_set_unik(nama, id_baru, username),
        "aset_dari": ID_TEMPLATE_BAWAAN,
    }
    return save_template(isi, id_baru, owner=username)


def hapus_hasil_job(job_id: str) -> None:
    """Buang video hasil render milik satu job dari Supabase.

    Hasil render menumpuk cepat — satu link yang dirender ke banyak template
    menghasilkan sebanyak itu pula berkas. Yang disimpan permanen cukup aset
    layernya; videonya dibuang setelah tidak ditampilkan lagi.
    """
    if not storage.enabled():
        return
    awalan = f"jobs/{_aman(job_id)}"
    try:
        for item in storage.list_prefix(awalan, limit=50):
            nama = str(item.get("name") or "")
            if nama:
                storage.delete(f"{awalan}/{nama}")
    except storage.StorageError as error:
        logger.warning("Hasil job %s gagal dihapus dari Supabase: %s", job_id, error)


def delete_template(template_id: str) -> None:
    folder = template_path(template_id)
    isi = _baca_template_mentah(folder)
    tamu = isi is not None and is_tamu(str(isi.get("owner") or ""))
    shutil.rmtree(folder, ignore_errors=True)
    # Salinan di Supabase ikut dihapus; kalau tidak, set ini muncul lagi
    # begitu container baru menyinkronkan template dari penyimpanan. Template
    # tamu tidak pernah ada di sana, jadi tidak perlu dicari.
    if not storage.enabled() or tamu:
        return
    awalan = f"templates/{_aman(template_id)}"
    try:
        for item in storage.list_prefix(f"{awalan}/assets", limit=500):
            storage.delete(f"{awalan}/assets/{item['name']}")
        storage.delete(f"{awalan}/template.json")
    except storage.StorageError as error:
        logger.warning("Set %s terhapus lokal tapi gagal dihapus dari Supabase: %s", template_id, error)


def _aset(template: dict[str, Any], nama: str | None) -> Path | None:
    """Ubah nama aset di template jadi path nyata, tetap di dalam foldernya."""
    if not nama:
        return None
    folder = template_path(template["id"]).resolve()
    path = (folder / str(nama)).resolve()
    if folder not in path.parents and path != folder:
        raise VideoError(f"Aset '{nama}' berada di luar folder template")
    if not path.is_file():
        # Cache lokal hilang (mis. container baru) — ambil lagi dari Supabase.
        _tarik_dari_storage(f"templates/{template['id']}/{nama}", path)
    if path.is_file():
        return path
    # Template akun baru meminjam berkas template bawaan alih-alih menyalinnya.
    dari = str(template.get("aset_dari") or "").strip()
    induk = _aman(dari) if dari else ""
    if induk and induk != template["id"]:
        folder_induk = template_path(induk).resolve()
        path_induk = (folder_induk / str(nama)).resolve()
        if folder_induk in path_induk.parents:
            if not path_induk.is_file():
                _tarik_dari_storage(f"templates/{induk}/{nama}", path_induk)
            if path_induk.is_file():
                return path_induk
    raise VideoError(f"Aset '{nama}' tidak ditemukan di template")


# ============================================================
#  PERIKSA BERKAS VIDEO
# ============================================================


def probe(path: Path) -> dict[str, Any]:
    """Baca durasi, ukuran, dan ada-tidaknya audio dari sebuah berkas."""
    hasil = subprocess.run(
        [
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-show_entries", "stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if hasil.returncode != 0:
        raise VideoError(f"Tidak bisa membaca video: {hasil.stderr.strip()[:200]}")
    data = json.loads(hasil.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    try:
        durasi = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        durasi = 0.0
    return {
        "duration": durasi,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def _font() -> str:
    for kandidat in FONT_CANDIDATES:
        if kandidat and Path(kandidat).is_file():
            return kandidat
    raise VideoError(
        "Tidak ada font untuk teks di video. Pasang fonts-dejavu-core "
        "atau isi environment VIDEO_FONT dengan path file .ttf"
    )


def _warna(nilai: Any, bawaan: str = "white") -> tuple[int, int, int, int]:
    """Terima "white", "#ffcc00", atau gaya ffmpeg "black@0.5" -> RGBA."""
    from PIL import ImageColor

    teks = str(nilai or bawaan).strip()
    alpha = 1.0
    if "@" in teks:
        teks, _, bagian = teks.partition("@")
        try:
            alpha = max(0.0, min(1.0, float(bagian)))
        except ValueError:
            alpha = 1.0
    try:
        r, g, b = ImageColor.getrgb(teks.strip() or bawaan)[:3]
    except ValueError:
        r, g, b = ImageColor.getrgb(bawaan)[:3]
    return r, g, b, int(alpha * 255)


def _bungkus_kata(gambar: Any, kata: list[str], font: Any, batas: int) -> list[list[str]]:
    """Bagi daftar kata jadi baris-baris yang lebarnya tidak melewati ``batas``."""
    baris: list[list[str]] = []
    sekarang: list[str] = []
    for k in kata:
        calon = " ".join(sekarang + [k])
        if not sekarang or gambar.textlength(calon, font=font) <= batas:
            sekarang.append(k)
        else:
            baris.append(sekarang)
            sekarang = [k]
    if sekarang:
        baris.append(sekarang)
    return baris


def _gambar_teks_berita(
    teks: dict[str, Any], isi: str, lebar: int, tinggi: int, tujuan: Path
) -> Path:
    """Paragraf gaya lower-third berita: rata kiri-kanan, kata pertama berwarna.

    Bentuk yang ditiru: "VIRAL! KONTROVERSI KARNAVAL DI PEKALONGAN ..." —
    kata pembuka merah, sisanya hitam, mengalir di baris yang sama, setiap
    baris (kecuali yang terakhir) diratakan ke kiri dan kanan.

    Kalau paragrafnya tidak muat dalam ``max_lines`` baris, ukuran font
    diturunkan setahap demi setahap sampai ``min_size``. Dengan begitu teks
    yang agak panjang tetap masuk kotak, bukan terpotong.

    Kunci yang dipakai dari ``teks``: size, min_size, max_lines, x, y, width,
    line_height (kelipatan ukuran font), color, kicker_color.
    """
    from PIL import Image, ImageDraw, ImageFont

    kanvas = Image.new("RGBA", (lebar, tinggi), (0, 0, 0, 0))
    gambar = ImageDraw.Draw(kanvas)
    jalur_font = _font()

    kata = isi.split()
    if not kata:
        kanvas.save(tujuan)
        return tujuan

    ukuran_awal = int(teks.get("size") or 38)
    ukuran_min = max(8, int(teks.get("min_size") or 22))
    maks_baris = max(1, int(teks.get("max_lines") or 4))
    try:
        kelipatan = float(teks.get("line_height") or 1.15)
    except (TypeError, ValueError):
        kelipatan = 1.15

    # Kalau template punya kotak teks, geometri diambil dari kotak itu: teks
    # mengisi lebar kotak dikurangi tepi, dan tingginya dibatasi tinggi kotak.
    # Dengan begitu satu layer teks yang sama cocok untuk template apa pun,
    # cukup dengan menentukan kotaknya.
    kotak = teks.get("box") if isinstance(teks.get("box"), dict) else None
    tinggi_maks: int | None = None
    if kotak:
        try:
            kx, ky = int(kotak["x"]), int(kotak["y"])
            kw, kh = int(kotak["w"]), int(kotak["h"])
        except (KeyError, TypeError, ValueError):
            kotak = None
    if kotak:
        tepi = max(12, int(kw * 0.05))
        kiri = kx + tepi
        batas = max(50, kw - 2 * tepi)
        tinggi_maks = max(20, kh - 2 * tepi)
    else:
        kiri = int(teks.get("x") or 0)
        batas = int(teks.get("width") or (lebar - 2 * kiri))
        batas = max(50, min(batas, lebar - kiri))

    def tinggi_blok(uk: int, jumlah_baris: int) -> int:
        return int(round(uk * kelipatan)) * jumlah_baris

    def batas_baris(uk: int) -> int:
        """Berapa baris yang boleh dipakai pada ukuran font ini.

        ``max_lines`` adalah pagar desain untuk teks pendek pada ukuran
        aslinya. Untuk teks panjang yang sudah disusutkan, yang sebenarnya
        membatasi adalah tinggi kotaknya - dan selama paragrafnya masih muat
        di dalam kotak, menambah baris jauh lebih baik daripada memenggal
        kalimat beritanya di tengah jalan.
        """
        if tinggi_maks is None:
            return maks_baris
        return max(maks_baris, tinggi_maks // max(1, int(round(uk * kelipatan))))

    # Susutkan font sampai paragrafnya muat, baik jumlah barisnya maupun —
    # kalau ada kotak — tinggi keseluruhannya.
    ukuran = ukuran_awal
    font = ImageFont.truetype(jalur_font, ukuran)
    baris = _bungkus_kata(gambar, kata, font, batas)

    def belum_muat() -> bool:
        if len(baris) > batas_baris(ukuran):
            return True
        return tinggi_maks is not None and tinggi_blok(ukuran, len(baris)) > tinggi_maks

    # Batas bawah kedua: kalau pada ``min_size`` pun belum muat, font
    # diturunkan lebih jauh lagi. ``min_size`` itu selera keterbacaan, dan
    # tulisan yang agak kecil masih jauh lebih baik daripada isi berita yang
    # hilang separuh - dulu sisanya langsung dibuang di sini.
    lantai = min(ukuran_min, max(8, int(ukuran_awal * 0.4)))
    while belum_muat() and ukuran > lantai:
        ukuran -= 1
        font = ImageFont.truetype(jalur_font, ukuran)
        baris = _bungkus_kata(gambar, kata, font, batas)
    if len(baris) > batas_baris(ukuran):
        logger.warning(
            "Teks '%s' tetap %d baris pada ukuran %d; sisanya dibuang",
            teks.get("name"), len(baris), ukuran,
        )
        baris = baris[: batas_baris(ukuran)]

    tinggi_baris = int(round(ukuran * kelipatan))
    if kotak:
        # Blok teks diletakkan di tengah kotak secara vertikal.
        atas = ky + max(0, (kh - tinggi_baris * len(baris)) // 2)
    elif teks.get("y") is not None:
        atas = int(teks["y"])
    else:
        atas = (tinggi - tinggi_baris * len(baris)) // 2

    warna_isi = _warna(teks.get("color"), "black")
    warna_pembuka = _warna(teks.get("kicker_color"), "#d32d27")
    lebar_spasi = gambar.textlength(" ", font=font)
    # Perataan paragraf. "justify" (bawaan) meregangkan sela kata sampai rata
    # kiri-kanan; tiga lainnya memakai sela biasa dan hanya menggeser titik
    # mulai tiap baris.
    rata = str(teks.get("align") or "justify").strip().lower()
    if rata not in ("justify", "left", "center", "right"):
        rata = "justify"

    for nomor, kata_baris in enumerate(baris):
        y = atas + nomor * tinggi_baris
        lebar_kata = [gambar.textlength(k, font=font) for k in kata_baris]
        terakhir = nomor == len(baris) - 1
        # Baris terakhir dan baris satu kata tidak pernah diregangkan (aturan
        # justify pada umumnya), selebihnya sisa lebar dibagi rata ke sela.
        regang = rata == "justify" and not terakhir and len(kata_baris) >= 2
        sela = (
            (batas - sum(lebar_kata)) / (len(kata_baris) - 1) if regang else lebar_spasi
        )
        lebar_baris = sum(lebar_kata) + sela * max(0, len(kata_baris) - 1)
        if rata == "center":
            x = float(kiri) + max(0.0, (batas - lebar_baris) / 2)
        elif rata == "right":
            x = float(kiri) + max(0.0, batas - lebar_baris)
        else:
            x = float(kiri)
        for urutan, (k, w) in enumerate(zip(kata_baris, lebar_kata)):
            warna = warna_pembuka if (nomor == 0 and urutan == 0) else warna_isi
            gambar.text((x, y), k, font=font, fill=warna)
            x += w + sela

    kanvas.save(tujuan)
    return tujuan


def kotak_kategori_bawaan(text_box: Any) -> dict[str, int] | None:
    """Tempat badge kategori kalau tidak ditentukan: menempel di atas kotak
    teks, rata kiri, seperti badge NEWS pada lower-third berita."""
    if not isinstance(text_box, dict):
        return None
    try:
        tx, ty, tw, th = (int(text_box[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    h = max(36, min(80, int(th * 0.26)))
    w = max(120, int(tw * 0.41))
    return {"x": tx + 6, "y": max(0, ty - h), "w": w, "h": h}


def _gambar_teks_kategori(
    teks: dict[str, Any], isi: str, lebar: int, tinggi: int, tujuan: Path
) -> Path:
    """Satu kata kategori (NEWS, HIBURAN, SHOWBIZ) di tengah kotak badge.

    Hurufnya kapital dan dibesarkan sampai mentok tepi kotak, lalu dipusatkan
    berdasarkan tinta sebenarnya supaya duduk rapi di tengah badge.
    """
    from PIL import Image, ImageDraw, ImageFont

    kanvas = Image.new("RGBA", (lebar, tinggi), (0, 0, 0, 0))
    gambar = ImageDraw.Draw(kanvas)
    kotak = teks.get("box") if isinstance(teks.get("box"), dict) else None
    isi = " ".join(isi.split()).upper()
    if not kotak or not isi:
        kanvas.save(tujuan)
        return tujuan
    kx, ky, kw, kh = (int(kotak[k]) for k in ("x", "y", "w", "h"))
    tepi = max(6, int(kh * 0.2))
    jalur = _font()
    ukuran = max(10, int(teks.get("size") or kh))
    font = ImageFont.truetype(jalur, ukuran)

    def muat(f: Any) -> bool:
        bb = gambar.textbbox((0, 0), isi, font=f)
        return (bb[2] - bb[0]) <= kw - 2 * tepi and (bb[3] - bb[1]) <= kh - 2 * tepi

    while not muat(font) and ukuran > 10:
        ukuran -= 1
        font = ImageFont.truetype(jalur, ukuran)
    bb = gambar.textbbox((0, 0), isi, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    x = kx + (kw - tw) // 2 - bb[0]
    y = ky + (kh - th) // 2 - bb[1]
    gambar.text((x, y), isi, font=font, fill=_warna(teks.get("color"), "white"))
    kanvas.save(tujuan)
    return tujuan


def _gambar_teks(
    teks: dict[str, Any], isi: str, lebar: int, tinggi: int, tujuan: Path
) -> Path:
    """Render satu lapisan teks jadi PNG transparan seukuran kanvas.

    Teks digambar sendiri (bukan filter ``drawtext``) karena banyak build
    ffmpeg dikompilasi tanpa libfreetype sehingga drawtext tidak tersedia.
    Cara ini juga memberi pemenggalan baris otomatis dan garis tepi.
    """
    from PIL import Image, ImageDraw, ImageFont

    gaya = str(teks.get("style") or "").lower()
    if gaya == "berita":
        return _gambar_teks_berita(teks, isi, lebar, tinggi, tujuan)
    if gaya == "kategori":
        return _gambar_teks_kategori(teks, isi, lebar, tinggi, tujuan)

    ukuran = int(teks.get("size") or 56)
    font = ImageFont.truetype(_font(), ukuran)
    kanvas = Image.new("RGBA", (lebar, tinggi), (0, 0, 0, 0))
    gambar = ImageDraw.Draw(kanvas)

    # Penggal baris supaya muat di lebar maksimum.
    try:
        rasio = float(teks.get("max_width") or 0.9)
    except (TypeError, ValueError):
        rasio = 0.9
    batas = max(50, int(lebar * max(0.1, min(1.0, rasio))))
    baris: list[str] = []
    for paragraf in isi.splitlines() or [""]:
        sekarang = ""
        for kata in paragraf.split():
            calon = f"{sekarang} {kata}".strip()
            if gambar.textlength(calon, font=font) <= batas or not sekarang:
                sekarang = calon
            else:
                baris.append(sekarang)
                sekarang = kata
        baris.append(sekarang)

    jarak = int(teks.get("line_spacing") or ukuran * 0.3)
    tinggi_baris = ukuran + jarak
    tinggi_total = tinggi_baris * len(baris) - jarak
    atas = int(teks["y"]) if teks.get("y") is not None else (tinggi - tinggi_total) // 2
    rata = str(teks.get("align") or "center").lower()
    tebal_garis = int(teks.get("stroke") or 0)

    if teks.get("box"):
        isi_kotak = _warna(teks.get("box_color"), "black@0.5")
        pad = int(teks.get("box_padding") or 18)
        lebar_kotak = max((gambar.textlength(b, font=font) for b in baris), default=0)
        kiri_kotak = (
            int(teks["x"]) if teks.get("x") is not None else (lebar - lebar_kotak) // 2
        )
        gambar.rectangle(
            [kiri_kotak - pad, atas - pad, kiri_kotak + lebar_kotak + pad, atas + tinggi_total + pad],
            fill=isi_kotak,
        )

    warna = _warna(teks.get("color"), "white")
    for nomor, teks_baris in enumerate(baris):
        panjang = gambar.textlength(teks_baris, font=font)
        if teks.get("x") is not None:
            kiri = int(teks["x"])
        elif rata == "left":
            kiri = int(lebar * 0.05)
        elif rata == "right":
            kiri = int(lebar * 0.95 - panjang)
        else:
            kiri = int((lebar - panjang) // 2)
        gambar.text(
            (kiri, atas + nomor * tinggi_baris),
            teks_baris,
            font=font,
            fill=warna,
            stroke_width=tebal_garis,
            stroke_fill=_warna(teks.get("stroke_color"), "black") if tebal_garis else None,
        )

    kanvas.save(tujuan)
    return tujuan


def _escape_filter(nilai: str) -> str:
    """Amankan path/nilai yang dipakai di dalam filtergraph ffmpeg."""
    return str(nilai).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


# ============================================================
#  UNDUH SUMBER
# ============================================================


def _perintah_ytdlp() -> list[str]:
    """Kembalikan cara memanggil yt-dlp yang pasti jalan di lingkungan ini."""
    if YTDLP_BIN:
        return [YTDLP_BIN]
    ditemukan = shutil.which("yt-dlp")
    if ditemukan:
        return [ditemukan]
    # Cadangan terakhir: paket ada di virtualenv yang sedang jalan.
    return [sys.executable, "-m", "yt_dlp"]


# Kalimat yang berarti "kamu terlalu sering meminta". Dicocokkan pada
# keluaran yt-dlp, sebab kode keluarnya sama saja dengan galat lain.
_KENA_BATAS = re.compile(r"(HTTP Error 429|Too Many Requests|rate.?limit)", re.I)

# Penahan kalau Redis tidak ada (mode lokal tanpa broker). Cukup untuk satu
# proses, dan worker memang hanya menjalankan satu render pada satu waktu.
_BATAS_LOKAL: dict[str, float] = {}


def _situs(url: str) -> str:
    """Nama situs untuk penahan: "instagram.com" dari alamat apa pun."""
    try:
        inang = urlparse(url).netloc.lower().split(":")[0]
    except ValueError:
        return ""
    bagian = [b for b in inang.split(".") if b not in ("www", "m")]
    return ".".join(bagian[-2:]) if len(bagian) >= 2 else inang


def _redis_batas():
    """Klien Redis untuk penahan, atau None kalau memang tidak ada."""
    alamat = os.getenv("REDIS_URL", "").strip()
    if not alamat:
        return None
    try:
        import redis  # impor di dalam: mode lokal boleh jalan tanpa paket ini

        return redis.Redis.from_url(alamat, decode_responses=True)
    except Exception:  # noqa: BLE001 - penahan tidak boleh menggagalkan unduhan
        return None


def sisa_tahanan(url: str) -> float:
    """Berapa detik lagi situs ini ditahan setelah kena 429. 0 = bebas."""
    situs = _situs(url)
    if not situs:
        return 0.0
    r = _redis_batas()
    if r is not None:
        try:
            sisa = r.ttl("videojob:batas:" + situs)
            return float(sisa) if sisa and sisa > 0 else 0.0
        except Exception:  # noqa: BLE001 - jatuh ke penahan dalam proses
            pass
    return max(0.0, _BATAS_LOKAL.get(situs, 0.0) - time.time())


# Galat yt-dlp yang sering muncul, diterjemahkan jadi kalimat yang bisa
# dikerjakan pengguna. Tanpa ini yang tampil di halaman adalah teks Inggris
# panjang berisi flag baris perintah ("--cookies-from-browser") dan tautan
# wiki GitHub - benar secara teknis, tapi tidak memberi tahu apa pun tentang
# apa yang harus dilakukan berikutnya.
_TERJEMAHAN: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"confirm you.{0,3}re not a bot|Sign in to confirm", re.I),
        "YouTube menolak permintaan dari server ini karena menganggapnya robot. "
        "Link YouTube memang belum bisa dipakai di sini. Pakai link Instagram "
        "atau TikTok, atau unduh videonya dulu lalu kirim lewat pilihan "
        "\u201cFile\u201d di atas.",
    ),
    (
        re.compile(r"empty media response|login required|requires? (a )?login|"
                   r"only available (for|to) registered", re.I),
        "Postingan ini tidak bisa dibaca tanpa akun. Pilih akun Instagram di "
        "Kelola Akun supaya dibaca memakai sesi akun itu, atau unggah berkas "
        "videonya lewat pilihan \u201cFile\u201d.",
    ),
    (
        re.compile(r"private (video|account)|This account is private", re.I),
        "Akun atau videonya privat, jadi tidak bisa diambil. Pakai video yang "
        "bisa dibuka umum, atau unggah berkasnya langsung.",
    ),
    (
        re.compile(r"video unavailable|has been removed|no longer available|"
                   r"content isn.{0,3}t available|not available on this app", re.I),
        "Videonya sudah tidak ada atau dihapus di sumbernya.",
    ),
    (
        re.compile(r"age.?restricted|inappropriate for some users", re.I),
        "Videonya dibatasi umur, jadi tidak bisa diambil tanpa akun.",
    ),
    (
        re.compile(r"Unsupported URL|is not a valid URL", re.I),
        "Link ini tidak dikenali. Pakai link Instagram, TikTok, atau link "
        "langsung ke berkas MP4.",
    ),
    (
        re.compile(r"Unable to download webpage|Failed to resolve|"
                   r"Connection (reset|refused)|Temporary failure", re.I),
        "Situs sumbernya tidak bisa dihubungi dari server ini. Coba lagi "
        "sebentar lagi.",
    ),
)


def _pesan_ramah(keluaran: str) -> str:
    """Terjemahkan galat yt-dlp jadi kalimat yang bisa ditindaklanjuti.

    Kembaliannya kosong kalau galatnya belum dikenal - dan di situ pesan
    aslinya tetap dipakai, karena galat yang tidak dikenal lebih berguna
    ditampilkan apa adanya daripada disamarkan jadi "terjadi kesalahan".
    """
    for pola, pesan in _TERJEMAHAN:
        if pola.search(keluaran):
            return pesan
    return ""


def _tahan_situs(url: str) -> None:
    """Catat bahwa situs ini baru saja menolak karena terlalu sering diminta."""
    situs = _situs(url)
    if not situs:
        return
    _BATAS_LOKAL[situs] = time.time() + JEDA_BATAS_DETIK
    r = _redis_batas()
    if r is None:
        return
    try:
        r.set("videojob:batas:" + situs, "1", ex=int(JEDA_BATAS_DETIK))
    except Exception:  # noqa: BLE001 - catatan lokal di atas sudah cukup
        pass


def _bebaskan_situs(url: str) -> None:
    """Unduhan berhasil: tahanannya dicabut supaya job berikutnya tidak menunggu."""
    situs = _situs(url)
    if not situs:
        return
    _BATAS_LOKAL.pop(situs, None)
    r = _redis_batas()
    if r is None:
        return
    try:
        r.delete("videojob:batas:" + situs)
    except Exception:  # noqa: BLE001
        pass


def _tanpa_cookie(perintah: list[str]) -> list[str]:
    """Salinan perintah yt-dlp tanpa pasangan "--cookies <berkas>"."""
    bersih: list[str] = []
    lewati = False
    for bagian in perintah:
        if lewati:
            lewati = False
            continue
        if bagian == "--cookies":
            lewati = True
            continue
        bersih.append(bagian)
    return bersih


def _ytdlp_dengan_mundur(
    perintah: list[str],
    url: str,
    pakai_cookie: bool,
    batas: int,
    log: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess:
    """Jalankan yt-dlp; lepas cookie yang ditolak, dan tunggu kalau kena 429.

    Dua kegagalan yang berbeda sifatnya ditangani di sini.

    Session ID yang sudah mati bukan sekadar tidak membantu - ia merusak.
    Instagram membalas permintaan bercookie-mati dengan badan kosong, dan
    yt-dlp berhenti dengan "Failed to parse JSON", padahal postingan publik
    yang sama terbaca dengan mudah tanpa cookie sama sekali. Jadi cookie yang
    ditolak dilepas dulu sebelum menyerah.

    HTTP 429 lain lagi: itu bukan galat tetap, melainkan "tunggu sebentar".
    yt-dlp tidak mengulang galat ini sendiri - ``--retries`` hanya berlaku
    untuk pengunduhan berkas, bukan untuk halaman yang dibaca extractor -
    jadi ia langsung menyerah dalam hitungan detik. Di sinilah penantiannya
    dilakukan, dengan jeda yang memanjang, dan hanya selama masih ada sisa
    waktu unduhan.
    """
    tenggat = time.monotonic() + batas

    def jalankan(pakai: list[str]) -> subprocess.CompletedProcess:
        sisa = max(30.0, tenggat - time.monotonic())
        return subprocess.run(
            pakai + [url], capture_output=True, text=True, timeout=sisa
        )

    def keluaran(hasil: subprocess.CompletedProcess) -> str:
        return f"{hasil.stdout or ''}\n{hasil.stderr or ''}"

    dipakai = perintah
    hasil = jalankan(dipakai)
    if hasil.returncode != 0 and pakai_cookie and not _KENA_BATAS.search(keluaran(hasil)):
        # Cookie ditolak, dan bukan karena kena batas: coba lagi sebagai tamu.
        # Kalau yang kedua ini pun kena 429, itulah kabar yang lebih berguna
        # untuk dibawa ke penantian di bawah - bukan galat cookie tadi.
        if log:
            log("Session ID-nya ditolak; mencoba lagi tanpa cookie.")
        logger.info("yt-dlp gagal dengan cookie, diulang tanpa cookie: %s", url)
        tanpa = _tanpa_cookie(perintah)
        kedua = jalankan(tanpa)
        if kedua.returncode == 0:
            return kedua
        if _KENA_BATAS.search(keluaran(kedua)):
            dipakai, hasil = tanpa, kedua

    for tunggu in TUNGGU_ULANG_DETIK:
        if hasil.returncode == 0 or not _KENA_BATAS.search(keluaran(hasil)):
            break
        if time.monotonic() + tunggu + 30 > tenggat:
            # Menunggu lalu kehabisan waktu sama saja dengan gagal, hanya
            # lebih lama. Lebih baik berhenti sekarang dengan pesan yang jelas.
            break
        if log:
            log(f"Situsnya sedang membatasi unduhan; menunggu {int(tunggu)} detik lalu mencoba lagi.")
        logger.info("yt-dlp kena 429, menunggu %.0f detik: %s", tunggu, url)
        time.sleep(tunggu)
        hasil = jalankan(dipakai)

    return hasil


def download_source(
    url: str,
    tujuan: Path,
    log: Callable[[str], None] | None = None,
    session_id: str | None = None,
) -> Path:
    """Unduh video dari link mana pun yang dikenali yt-dlp."""
    url = str(url or "").strip()
    tujuan.mkdir(parents=True, exist_ok=True)

    # Sumber dari komputer pengguna: berkasnya sudah ada di server, tinggal
    # disalin ke folder job. Disalin, bukan dipindah, karena satu unggahan bisa
    # dipakai banyak job (satu per template) yang berjalan terpisah.
    if url.startswith(AWALAN_UNGGAHAN):
        id_unggahan = _aman(url[len(AWALAN_UNGGAHAN):])
        folder = uploads_dir() / id_unggahan
        berkas = sorted(p for p in folder.glob("source.*") if p.is_file()) if folder.is_dir() else []
        if not berkas:
            # Tidak ada di disk ini: worker bisa jadi layanan lain, atau
            # containernya baru dijalankan ulang. Ambil dari Supabase.
            berkas = _tarik_unggahan_dari_storage(id_unggahan, folder)
        if not berkas:
            raise VideoError(
                "Video sumber unggahan sudah tidak ada di server (mungkin sudah "
                "dibersihkan). Unggah lagi berkasnya."
            )
        salinan = tujuan / berkas[0].name
        shutil.copy2(berkas[0], salinan)
        if log:
            log(f"Memakai video sumber dari unggahan ({salinan.stat().st_size / 1_048_576:.1f} MB)")
        return salinan

    if not url.lower().startswith(("http://", "https://")):
        raise VideoError("Link video harus diawali http:// atau https://")
    keluaran = tujuan / "source.%(ext)s"

    cache = cache_dir() / hashlib.sha1(url.encode("utf-8")).hexdigest()

    def dari_cache() -> Path | None:
        for tersimpan in sorted(cache.glob("source.*")):
            if time.time() - tersimpan.stat().st_mtime < SOURCE_CACHE_SECONDS:
                salinan = tujuan / tersimpan.name
                shutil.copy2(tersimpan, salinan)
                return salinan
        return None

    siap = dari_cache()
    if siap is not None:
        if log:
            log("Memakai video sumber yang baru saja diunduh (cache).")
        return siap

    # Beberapa render dari link yang sama bisa berjalan bersamaan. Tanpa kunci
    # ini semuanya mengunduh sendiri-sendiri, dan Instagram cepat membatasi
    # pengunduh yang beruntun. Yang lebih dulu memegang kunci yang mengunduh;
    # sisanya menunggu lalu memakai hasilnya.
    kunci = cache.with_name(cache.name + ".lock")
    kunci.parent.mkdir(parents=True, exist_ok=True)
    pemegang = False
    try:
        kunci.mkdir()
        pemegang = True
    except FileExistsError:
        # Kunci yang ditinggalkan proses mati tidak boleh menahan selamanya.
        try:
            if time.time() - kunci.stat().st_mtime > DOWNLOAD_TIMEOUT_SECONDS:
                shutil.rmtree(kunci, ignore_errors=True)
        except OSError:
            pass
    if not pemegang:
        if log:
            log("Menunggu unduhan video sumber yang sedang berjalan ...")
        batas = time.time() + DOWNLOAD_TIMEOUT_SECONDS
        while time.time() < batas and kunci.exists():
            time.sleep(1.0)
            siap = dari_cache()
            if siap is not None:
                if log:
                    log("Memakai video sumber yang baru saja diunduh (cache).")
                return siap
        siap = dari_cache()
        if siap is not None:
            if log:
                log("Memakai video sumber yang baru saja diunduh (cache).")
            return siap

    # Situsnya baru saja menolak karena terlalu sering diminta. Mencoba lagi
    # sekarang bukan cuma sia-sia - tiap percobaan memperpanjang penolakannya.
    # Link yang sudah pernah berhasil tidak kena: ia sudah dilayani cache di
    # atas. Yang bercookie juga lolos, sebab jalur masuknya memang lain.
    tahanan = sisa_tahanan(url)
    if tahanan > 0 and not (session_id and "instagram.com" in url.lower()):
        if pemegang:
            shutil.rmtree(kunci, ignore_errors=True)
        raise VideoError(
            f"{_situs(url) or 'Situs ini'} sedang membatasi unduhan dari server "
            f"ini (429). Tunggu {int(tahanan) + 1} detik lagi, atau pilih akun "
            f"Instagram di Kelola Akun supaya unduhannya memakai sesi akun itu."
        )

    perintah = _perintah_ytdlp() + (["--proxy", UNDUH_PROXY] if UNDUH_PROXY else []) + [
        "--no-playlist",
        "--no-progress",
        "--retries", "3",
        # Galat extractor (termasuk 429 saat membaca halaman postingan) tidak
        # ikut diatur "--retries"; ini yang mengaturnya, dengan jeda memanjang.
        "--extractor-retries", "3",
        "--retry-sleep", "extractor:exp=5:60",
        "--retry-sleep", "http:exp=5:60",
        # Satu postingan dibaca lewat beberapa permintaan beruntun. Diberi
        # jeda supaya tidak terlihat sebagai serbuan oleh situsnya.
        "--sleep-requests", "1",
        # Ditolak SEBELUM diunduh. Tanpa ini, video berjam-jam tetap masuk
        # disk lebih dulu baru ketahuan kepanjangan - dan itu jalur termurah
        # untuk memenuhi penyimpanan server. Dua penyaring terpisah berarti
        # ATAU: durasinya di bawah batas, ATAU durasinya memang tidak
        # diketahui (mis. tautan mp4 langsung). Menulisnya sebagai satu
        # penyaring ber-"|" ditolak yt-dlp dengan galat yang menyesatkan.
        "--match-filter", f"duration <= {int(MAX_SOURCE_SECONDS)}",
        "--match-filter", "!duration",
        "--max-filesize", os.getenv("VIDEO_MAX_UNDUH_MB", "600") + "M",
        # Utamakan mp4 h264+aac supaya ffmpeg tidak perlu kerja ekstra.
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(keluaran),
    ]

    # Link Instagram sering menolak pengunduh anonim; pakai cookie sessionid
    # milik pengguna kalau ada.
    berkas_cookie: Path | None = None
    if session_id and "instagram.com" in url.lower():
        berkas_cookie = tujuan / "cookies.txt"
        kedaluwarsa = int(time.time()) + 86400 * 30
        berkas_cookie.write_text(
            "# Netscape HTTP Cookie File\n"
            f".instagram.com\tTRUE\t/\tTRUE\t{kedaluwarsa}\tsessionid\t{session_id.strip()}\n",
            encoding="utf-8",
        )
        perintah += ["--cookies", str(berkas_cookie)]
        if log:
            log("Memakai Session ID untuk mengunduh dari Instagram.")

    if log:
        log(f"Mengunduh video dari {url}")
    try:
        hasil = _ytdlp_dengan_mundur(
            perintah, url, berkas_cookie is not None, DOWNLOAD_TIMEOUT_SECONDS, log
        )
    except subprocess.TimeoutExpired as error:
        raise VideoError("Unduhan video melewati batas waktu") from error
    finally:
        if berkas_cookie is not None:
            berkas_cookie.unlink(missing_ok=True)

    if hasil.returncode != 0:
        if pemegang:
            shutil.rmtree(kunci, ignore_errors=True)
        pesan = (hasil.stderr or hasil.stdout or "").strip().splitlines()
        detail = pesan[-1] if pesan else "penyebab tidak diketahui"
        if _KENA_BATAS.search(f"{hasil.stdout or ''}\n{hasil.stderr or ''}"):
            # Sudah ditunggu dan diulang di _ytdlp_dengan_mundur, tetap ditolak.
            # Situsnya ditahan supaya job berikutnya tidak memperparah keadaan,
            # dan orangnya diberi tahu jalan keluar yang benar-benar menolong.
            _tahan_situs(url)
            saran = (
                "Sesi akun yang dipakai pun ikut ditolak, jadi tunggu beberapa menit."
                if berkas_cookie is not None
                else (
                    "Pilih akun Instagram di Kelola Akun supaya unduhannya memakai "
                    "sesi akun itu - permintaan yang sudah masuk jarang kena batas "
                    "ini. Bisa juga unggah berkas videonya langsung."
                )
            )
            raise VideoError(
                f"{_situs(url) or 'Situs sumber'} menolak karena terlalu banyak "
                f"permintaan dari server ini (429). {saran}"
            )
        tambahan = (
            " Tanpa cookie pun gagal, jadi videonya memang tidak bisa diambil "
            "(mungkin akunnya privat)."
            if berkas_cookie is not None
            else ""
        )
        raise VideoError(f"Gagal mengunduh video: {detail[:300]}{tambahan}")

    berkas = sorted(tujuan.glob("source.*"))
    if not berkas:
        keluaran_ytdlp = f"{hasil.stdout}\n{hasil.stderr}"
        if "does not pass filter" in keluaran_ytdlp:
            # Ditolak penyaring durasi di atas: yt-dlp berhenti dengan kode 0
            # dan tidak membuat berkas apa pun, jadi tanpa pemeriksaan ini
            # pesannya jadi "berkasnya tidak ditemukan" yang membingungkan.
            raise VideoError(
                f"Videonya lebih panjang dari batas {MAX_SOURCE_SECONDS / 60:.0f} menit, "
                "jadi tidak diunduh. Pakai video yang lebih pendek."
            )
        if "File is larger than max-filesize" in keluaran_ytdlp:
            raise VideoError(
                f"Berkas videonya melebihi batas unduhan "
                f"{os.getenv('VIDEO_MAX_UNDUH_MB', '600')} MB."
            )
        raise VideoError("Video terunduh tetapi berkasnya tidak ditemukan")
    # Berhasil: tahanan situsnya dicabut, supaya job berikutnya tidak menunggu
    # sisa jeda yang sudah tidak berlaku lagi.
    _bebaskan_situs(url)
    if log:
        ukuran = berkas[0].stat().st_size / 1_048_576
        log(f"Video terunduh ({ukuran:.1f} MB)")
    try:
        cache.mkdir(parents=True, exist_ok=True)
        shutil.copy2(berkas[0], cache / berkas[0].name)
    except OSError as error:  # cache hanya pelengkap, jangan gagalkan job
        logger.warning("Gagal menyimpan cache sumber: %s", error)
    finally:
        if pemegang:
            shutil.rmtree(kunci, ignore_errors=True)
    return berkas[0]


def preview_source(url: str, session_id: str | None = None) -> dict[str, Any]:
    """Ambil keterangan video dari sebuah link TANPA mengunduh berkasnya.

    Dipakai halaman Auto Edit Video untuk menampilkan isi link begitu
    ditempel: judul, pembuat, durasi, dan gambar sampul. Hanya metadata yang
    diambil, jadi jauh lebih ringan daripada mengunduh videonya.
    """
    url = str(url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise VideoError("Link video harus diawali http:// atau https://")

    # Pratinjau adalah permintaan PERTAMA yang menyentuh Instagram setiap kali
    # link ditempel. Kalau situsnya sedang menahan, menembaknya lagi hanya
    # memperpanjang tahanan itu - dan pratinjaunya toh pasti gagal.
    tahanan = sisa_tahanan(url)
    if tahanan > 0 and not (session_id and "instagram.com" in url.lower()):
        raise VideoError(
            f"{_situs(url) or 'Situs ini'} sedang membatasi permintaan dari server "
            f"ini (429). Tunggu {int(tahanan) + 1} detik lagi, atau pilih akun "
            f"Instagram di Kelola Akun supaya dibaca memakai sesi akun itu."
        )

    perintah = _perintah_ytdlp() + (["--proxy", UNDUH_PROXY] if UNDUH_PROXY else []) + [
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--skip-download",
        "--dump-single-json",
        "--retries", "2",
        # Pratinjau harus cepat (batasnya 45 detik), jadi ulangannya sedikit
        # dan jedanya pendek - beda dengan unduhan yang boleh menunggu lama.
        "--extractor-retries", "1",
        "--retry-sleep", "extractor:3",
        "--sleep-requests", "0.5",
    ]

    berkas_cookie: Path | None = None
    if session_id and "instagram.com" in url.lower():
        # Sama seperti download_source: Instagram menolak pembaca anonim.
        berkas_cookie = Path(tempfile.mkdtemp(prefix="godam-cookie-")) / "cookies.txt"
        kedaluwarsa = int(time.time()) + 86400 * 30
        berkas_cookie.write_text(
            "# Netscape HTTP Cookie File\n"
            f".instagram.com\tTRUE\t/\tTRUE\t{kedaluwarsa}\tsessionid\t{session_id.strip()}\n",
            encoding="utf-8",
        )
        perintah += ["--cookies", str(berkas_cookie)]

    try:
        hasil = _ytdlp_dengan_mundur(
            perintah, url, berkas_cookie is not None, PREVIEW_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as error:
        raise VideoError("Pratinjau link melewati batas waktu") from error
    finally:
        if berkas_cookie is not None:
            shutil.rmtree(berkas_cookie.parent, ignore_errors=True)

    if hasil.returncode != 0:
        pesan = (hasil.stderr or hasil.stdout or "").strip().splitlines()
        detail = pesan[-1] if pesan else "penyebab tidak diketahui"
        if _KENA_BATAS.search(f"{hasil.stdout or ''}\n{hasil.stderr or ''}"):
            _tahan_situs(url)
            raise VideoError(
                f"{_situs(url) or 'Situs sumber'} menolak karena terlalu banyak "
                "permintaan dari server ini (429). "
                + (
                    "Sesi akun yang dipakai pun ikut ditolak, jadi tunggu beberapa menit."
                    if berkas_cookie is not None
                    else "Pilih akun Instagram di Kelola Akun supaya dibaca memakai "
                    "sesi akun itu, atau unggah berkas videonya langsung."
                )
            )
        tambahan = (
            " Tanpa cookie pun gagal, jadi postingannya memang tidak bisa dibaca "
            "(mungkin akunnya privat)."
            if berkas_cookie is not None
            else ""
        )
        ramah = _pesan_ramah(f"{hasil.stdout or ''}\n{hasil.stderr or ''}")
        if ramah:
            raise VideoError(ramah)
        raise VideoError(f"Tidak bisa membaca link: {detail[:300]}{tambahan}")

    _bebaskan_situs(url)

    try:
        data = json.loads(hasil.stdout or "{}")
    except json.JSONDecodeError as error:
        raise VideoError("Balasan yt-dlp tidak bisa dibaca") from error

    durasi = data.get("duration")
    try:
        durasi = float(durasi) if durasi is not None else None
    except (TypeError, ValueError):
        durasi = None

    # Caption/keterangan unggahan. Di Instagram & TikTok isinya caption asli,
    # di YouTube deskripsi video. Dipakai untuk membuat hook otomatis, jadi
    # dipotong supaya prompt tidak membengkak.
    caption = str(data.get("description") or "").strip()

    # Pemilik video untuk kredit "SUMBER: ...".
    #
    # "channel" adalah nama akunnya, sedangkan "uploader" adalah nama tampilan
    # yang boleh diisi bebas oleh pemiliknya. Di Instagram keduanya sering
    # berbeda: akun "tehojol" yang nama tampilannya "tehojol/selvi" membuat
    # kreditnya salah kalau memakai "uploader". Jadi nama akun didahulukan.
    pemilik = (
        str(data.get("channel") or "").strip()
        or str(data.get("uploader") or "").strip()
        or str(data.get("uploader_id") or "").strip().lstrip("@")
    )

    return {
        "title": data.get("title") or "(tanpa judul)",
        "description": caption[:2000],
        "view_count": data.get("view_count") or 0,
        "like_count": data.get("like_count") or 0,
        "upload_date": data.get("upload_date") or "",
        "uploader": pemilik,
        "duration": durasi,
        "thumbnail": data.get("thumbnail") or "",
        "extractor": data.get("extractor_key") or data.get("extractor") or "",
        "width": data.get("width") or 0,
        "height": data.get("height") or 0,
        "webpage_url": data.get("webpage_url") or url,
        # Videonya terlalu panjang untuk diproses - ini penolakan, bukan
        # sekadar peringatan bahwa bagian awalnya saja yang dipakai.
        "too_long": bool(durasi and durasi > MAX_SOURCE_SECONDS),
        "max_seconds": MAX_SOURCE_SECONDS,
    }


# ============================================================
#  PRATINJAU STATIS & DETEKSI KOTAK TEKS
# ============================================================


def _angka_posisi(nilai: Any, ukuran_kanvas: int, ukuran_layer: int) -> int:
    """Versi PIL dari rumus posisi overlay ffmpeg (untuk pratinjau)."""
    if nilai is None:
        return (ukuran_kanvas - ukuran_layer) // 2
    if isinstance(nilai, str):
        t = nilai.strip()
        if t in ("main_w-w", "main_h-h", "W-w", "H-h"):
            return ukuran_kanvas - ukuran_layer
        if t in ("(main_w-w)/2", "(main_h-h)/2", "(W-w)/2", "(H-h)/2"):
            return (ukuran_kanvas - ukuran_layer) // 2
        try:
            return int(float(t))
        except ValueError:
            return 0
    return int(nilai)


def komposit_statis(template: dict[str, Any], contoh: dict[str, str] | None = None) -> Any:
    """Susun layer gambar template di atas kanvas gelap, tanpa video.

    Dipakai halaman untuk menggambar kotak teks di atas tampilan sebenarnya,
    dan oleh deteksi otomatis. Layer video dilewati.

    ``contoh`` berisi teks contoh per nama layer (mis. {"hook": "VIRAL! ..."}).
    Kalau diisi, teksnya ikut digambar memakai perender yang SAMA dengan yang
    dipakai saat membuat video - jadi yang terlihat di halaman benar-benar
    bentuk akhirnya: perataan, warna, penyusutan font, dan pemenggalan baris.
    Deteksi otomatis memanggilnya tanpa ``contoh`` supaya yang dianalisis
    tetap gambar layer polos.
    """
    from PIL import Image

    lebar = int(template.get("width") or 1080)
    tinggi = int(template.get("height") or 1920)
    kanvas = Image.new("RGBA", (lebar, tinggi), (24, 24, 24, 255))
    for overlay in template.get("overlays") or []:
        nama = str(overlay.get("file") or "")
        if Path(nama).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        try:
            berkas = _aset(template, nama)
        except VideoError:
            continue
        if berkas is None:
            continue
        with Image.open(berkas) as sumber:
            im = sumber.convert("RGBA")
        w, h = overlay.get("w"), overlay.get("h")
        if w or h:
            tw = int(w) if w else max(1, round(im.width * int(h) / im.height))
            th = int(h) if h else max(1, round(im.height * int(w) / im.width))
            im = im.resize((tw, th), Image.LANCZOS)
        x = _angka_posisi(overlay.get("x"), lebar, im.width)
        y = _angka_posisi(overlay.get("y"), tinggi, im.height)
        # Potong bagian yang keluar kanvas; alpha_composite menolak itu.
        kiri, atas = max(0, -x), max(0, -y)
        kanan = min(im.width, lebar - x)
        bawah = min(im.height, tinggi - y)
        if kanan <= kiri or bawah <= atas:
            continue
        potongan = im.crop((kiri, atas, kanan, bawah))
        kanvas.alpha_composite(potongan, (x + kiri, y + atas))

    if contoh:
        kanvas = _tempel_contoh_teks(template, kanvas, contoh, lebar, tinggi)
    return kanvas


def _tempel_contoh_teks(
    template: dict[str, Any], kanvas: Any, contoh: dict[str, str], lebar: int, tinggi: int
) -> Any:
    """Gambar teks contoh di atas komposit, lewat jalur render yang sama."""
    import tempfile

    from PIL import Image

    kotak_teks = template.get("text_box") if isinstance(template.get("text_box"), dict) else None
    kotak_badge = template.get("badge_box") if isinstance(template.get("badge_box"), dict) else None
    if not kotak_badge:
        kotak_badge = kotak_kategori_bawaan(kotak_teks)

    with tempfile.TemporaryDirectory(prefix="pratinjau-teks-") as kerja:
        for nomor, teks in enumerate(template.get("texts") or []):
            nama = str(teks.get("name") or f"teks{nomor}")
            isi = contoh.get(nama)
            if not isi and teks.get("source"):
                isi = template.get(str(teks["source"]))
            isi = str(isi or "").strip()
            if not isi:
                continue
            gaya = str(teks.get("style") or "").lower()
            if gaya == "berita":
                warna = str(template.get("teks_warna") or "white").strip().lower()
                teks = {**teks, "color": warna if warna in ("black", "white") else "white"}
                if kotak_teks and not teks.get("box"):
                    teks = {**teks, "box": kotak_teks}
            if gaya == "kategori":
                if not teks.get("box"):
                    if not kotak_badge:
                        continue
                    teks = {**teks, "box": kotak_badge}
            try:
                png = _gambar_teks(teks, isi, lebar, tinggi, Path(kerja) / f"t{nomor}.png")
                with Image.open(png) as lapis:
                    kanvas.alpha_composite(lapis.convert("RGBA"))
            except Exception:  # noqa: BLE001 - pratinjau tidak boleh menggagalkan halaman
                logger.exception("Teks contoh '%s' gagal digambar di pratinjau", nama)
    return kanvas


def aset_terpakai(template: dict[str, Any]) -> set[str]:
    """Nama berkas aset yang masih dirujuk template ini."""
    dipakai = {str(o.get("file") or "") for o in (template.get("overlays") or [])}
    for kunci in ("intro", "outro"):
        nilai = template.get(kunci)
        if nilai:
            dipakai.add(str(nilai))
    return {d for d in dipakai if d}


def buang_aset_tak_terpakai(template_id: str, template: dict[str, Any]) -> int:
    """Hapus berkas di folder assets yang sudah tidak dirujuk template.

    Mengosongkan satu layer hanya melepas rujukannya; tanpa ini berkasnya
    tetap tinggal selamanya dan ikut memakan jatah penyimpanan pemiliknya.
    """
    folder = template_path(template_id) / "assets"
    if not folder.is_dir():
        return 0
    dipakai = {Path(n).name for n in aset_terpakai(template)}
    dibuang = 0
    for berkas in folder.iterdir():
        if berkas.is_file() and berkas.name not in dipakai:
            try:
                berkas.unlink()
                dibuang += 1
            except OSError as galat:
                logger.warning("Aset %s gagal dihapus: %s", berkas, galat)
    if dibuang:
        logger.info("%s: %d aset tak terpakai dibuang", template_id, dibuang)
    return dibuang


def deteksi_kotak_teks(kanvas: Any) -> dict[str, int] | None:
    """Tebak kotak teks: daerah terang yang rata dan paling luas di kanvas.

    Kotak lower-third pada umumnya berupa bidang polos berwarna terang (putih,
    biru muda) yang jauh lebih luas dari elemen lain. Gambar diperkecil dulu
    supaya pencarian komponen terhubung cepat, lalu hasilnya dikembalikan ke
    ukuran kanvas dan disisipkan sedikit ke dalam.
    """
    from PIL import Image, ImageFilter

    faktor = 4
    kecil = kanvas.convert("RGB").resize(
        (max(1, kanvas.width // faktor), max(1, kanvas.height // faktor)), Image.BOX
    )
    terang = kecil.convert("L")
    tepi = terang.filter(ImageFilter.FIND_EDGES)
    w, h = kecil.size
    pt, pe = terang.load(), tepi.load()
    warna = kecil.load()

    def kandidat(x: int, y: int) -> bool:
        r, g, b = warna[x, y]
        # Terang dan cukup polos (bukan tepian). Kejenuhan warna diberi
        # kelonggaran agar kotak berwarna muda dengan gradasi (mis. biru muda)
        # tetap terhitung utuh, sementara bidang latar yang pekat tersaring
        # oleh ambang terangnya.
        return pt[x, y] > 140 and pe[x, y] < 16 and (max(r, g, b) - min(r, g, b)) < 160

    dikunjungi = bytearray(w * h)
    terbaik: tuple[int, tuple[int, int, int, int]] | None = None
    for y0 in range(h):
        for x0 in range(w):
            if dikunjungi[y0 * w + x0] or not kandidat(x0, y0):
                continue
            # BFS komponen terhubung
            antrean = [(x0, y0)]
            dikunjungi[y0 * w + x0] = 1
            jumlah = 0
            minx = maxx = x0
            miny = maxy = y0
            while antrean:
                x, y = antrean.pop()
                jumlah += 1
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and not dikunjungi[ny * w + nx] and kandidat(nx, ny):
                        dikunjungi[ny * w + nx] = 1
                        antrean.append((nx, ny))
            luas_kotak = (maxx - minx + 1) * (maxy - miny + 1)
            # Harus cukup besar dan cukup "persegi" (bukan garis tipis)
            if jumlah < (w * h) * 0.01 or jumlah / luas_kotak < 0.55:
                continue
            if (maxy - miny + 1) < 6 or (maxx - minx + 1) < 12:
                continue
            if terbaik is None or jumlah > terbaik[0]:
                terbaik = (jumlah, (minx, miny, maxx, maxy))
    if terbaik is None:
        return None
    minx, miny, maxx, maxy = terbaik[1]
    x, y = minx * faktor, miny * faktor
    bw, bh = (maxx - minx + 1) * faktor, (maxy - miny + 1) * faktor
    # sisipkan sedikit ke dalam supaya tidak menempel tepi kotak
    sisip = max(4, int(min(bw, bh) * 0.03))
    return {"x": x + sisip, "y": y + sisip, "w": max(20, bw - 2 * sisip), "h": max(20, bh - 2 * sisip)}


# ============================================================
#  SUSUN PERINTAH FFMPEG
# ============================================================


# Rumus posisi yang boleh dipakai template. Sengaja dibatasi: nilai ini masuk
# ke perintah ffmpeg, jadi hanya bentuk yang dikenali yang diteruskan.
POSISI_RUMUS = {
    "main_w-w", "main_h-h", "(main_w-w)/2", "(main_h-h)/2",
    "W-w", "H-h", "(W-w)/2", "(H-h)/2", "0",
}


def _posisi(nilai: Any, bawaan: str) -> str:
    """Ubah x/y template jadi nilai posisi untuk filter overlay."""
    if nilai is None:
        return bawaan
    if isinstance(nilai, str):
        teks = nilai.strip()
        if teks in POSISI_RUMUS:
            return teks
        try:
            return str(int(float(teks)))
        except ValueError:
            logger.warning("Posisi '%s' tidak dikenali, dipakai %s", teks, bawaan)
            return bawaan
    return str(int(nilai))


def _klip(path: Path, lebar: int, tinggi: int, fps: int, idx: int, nama: str) -> tuple[list[str], str]:
    """Filter untuk menyeragamkan satu klip (intro/outro) ke ukuran kanvas."""
    return (
        [
            f"[{idx}:v]scale={lebar}:{tinggi}:force_original_aspect_ratio=increase,"
            f"crop={lebar}:{tinggi},fps={fps},setsar=1,format=yuv420p[{nama}]"
        ],
        nama,
    )


# Aset video yang jauh lebih besar dari tempat ia digambar. Satu frame
# ProRes 1920x1080 12-bit berisi 16 MB, dan ffmpeg menahan belasan frame
# sekaligus di rantai filternya - padahal stikernya cuma digambar 280x158.
# Di atas batas ini, asetnya dikecilkan dulu sekali, lalu dipakai berulang.
BATAS_FRAME_MB = float(os.getenv("VIDEO_ASET_FRAME_MB", "2"))
_VIDEO_EXT = {".mov", ".mp4", ".m4v", ".webm", ".mkv", ".avi", ".mpg", ".mpeg"}


def _punya_alpha(berkas: Path) -> bool:
    """True kalau berkas videonya menyimpan lapisan transparan."""
    hasil = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=pix_fmt", "-of", "default=nw=1:nk=1", str(berkas)],
        capture_output=True, text=True,
    )
    px = (hasil.stdout or "").strip().lower()
    return px.startswith(("yuva", "rgba", "argb", "bgra", "abgr")) or px.endswith("a")


def _aset_ringan(berkas: Path, lebar_pakai: int, tinggi_pakai: int) -> Path:
    """Kecilkan aset video yang kelewat besar untuk tempat ia digambar.

    Ini bukan penghematan disk melainkan penghematan MEMORI. Yang menentukan
    berapa RAM yang dipakai ffmpeg bukan besar berkasnya, melainkan besar satu
    frame setelah dibuka: ProRes 1920x1080 12-bit = 16 MB per frame, dan
    belasan frame hidup bersamaan di rantai filter. Itulah yang membuat render
    720x1280 pun menyentuh batas 1 GB lalu ditembak kernel.

    Mengecilkannya lebih dulu tidak mengubah hasil: asetnya toh diskala ke
    ukuran yang sama di dalam rantai filter. Hasilnya disimpan di sebelah
    aset aslinya, jadi cukup sekali untuk semua render berikutnya.
    """
    if berkas.suffix.lower() not in _VIDEO_EXT:
        return berkas
    try:
        info = probe(berkas)
    except VideoError:
        return berkas
    asal_w, asal_h = int(info.get("width") or 0), int(info.get("height") or 0)
    if asal_w <= 0 or asal_h <= 0:
        return berkas
    if asal_w * asal_h * 4 / 1_048_576 <= BATAS_FRAME_MB:
        return berkas  # framenya memang kecil, tidak ada yang perlu dihemat

    # Ukuran tujuan: yang tidak disebut dihitung dari rasi aslinya.
    if lebar_pakai and not tinggi_pakai:
        tinggi_pakai = max(2, round(lebar_pakai * asal_h / asal_w))
    elif tinggi_pakai and not lebar_pakai:
        lebar_pakai = max(2, round(tinggi_pakai * asal_w / asal_h))
    if not lebar_pakai or not tinggi_pakai:
        return berkas
    lebar_pakai -= lebar_pakai % 2
    tinggi_pakai -= tinggi_pakai % 2
    if lebar_pakai >= asal_w or tinggi_pakai >= asal_h:
        return berkas  # tidak ada yang bisa dikecilkan

    folder = berkas.parent / ".ringan"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return berkas
    alpha = _punya_alpha(berkas)
    # Nama ikut ukuran berkas asli, supaya aset yang diganti tidak memakai
    # hasil kecil yang lama.
    try:
        tanda = berkas.stat().st_size
    except OSError:
        return berkas
    tujuan = folder / f"{berkas.stem}-{lebar_pakai}x{tinggi_pakai}-{tanda}{'.mov' if alpha else '.mp4'}"
    if tujuan.is_file():
        return tujuan

    if alpha:
        # qtrle satu-satunya penyandi beralpha yang pasti ada di ffmpeg mana
        # pun; berkasnya besar tapi ini hanya singgahan, dan yang dihemat
        # memori saat render, bukan disk.
        sandi = ["-c:v", "qtrle", "-pix_fmt", "argb"]
    else:
        sandi = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    perintah = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-threads", "1", "-i", str(berkas),
        "-vf", f"scale={lebar_pakai}:{tinggi_pakai}", *sandi,
        "-c:a", "copy", str(tujuan),
    ]
    try:
        hasil = subprocess.run(perintah, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("Aset %s gagal dikecilkan: %s", berkas.name, error)
        return berkas
    if hasil.returncode != 0 or not tujuan.is_file():
        logger.warning(
            "Aset %s gagal dikecilkan: %s",
            berkas.name, (hasil.stderr or "").strip().splitlines()[-1:] or "tanpa pesan",
        )
        tujuan.unlink(missing_ok=True)
        return berkas
    logger.info(
        "Aset %s dikecilkan %dx%d -> %dx%d supaya hemat memori",
        berkas.name, asal_w, asal_h, lebar_pakai, tinggi_pakai,
    )
    return tujuan


def _bangun_perintah(
    template: dict[str, Any],
    source: Path,
    keluaran: Path,
    texts: dict[str, str],
    kerja: Path,
) -> tuple[list[str], float]:
    """Rakit satu perintah ffmpeg yang mengerjakan seluruh template sekaligus.

    Sengaja satu proses saja: video sumber, overlay, teks, lalu sambungan
    intro/outro dikerjakan dalam satu kali encode.
    """
    lebar = int(template.get("width") or 1080)
    tinggi = int(template.get("height") or 1920)
    fps = int(template.get("fps") or 30)

    info_sumber = probe(source)
    if info_sumber["duration"] and info_sumber["duration"] > MAX_SOURCE_SECONDS + 1:
        raise VideoError(
            f"Videonya {info_sumber['duration'] / 60:.0f} menit, lebih panjang dari "
            f"batas {MAX_SOURCE_SECONDS / 60:.0f} menit. Potong dulu videonya, "
            "atau pakai sumber yang lebih pendek."
        )
    batas = MAX_SOURCE_SECONDS
    if template.get("max_duration"):
        batas = min(batas, float(template["max_duration"]))
    durasi_badan = min(info_sumber["duration"], batas) if info_sumber["duration"] else batas
    if durasi_badan <= 0:
        raise VideoError("Durasi video sumber terbaca 0 detik")

    inputs: list[str] = ["-t", f"{durasi_badan:.3f}", "-i", str(source)]
    filters: list[str] = []
    idx = 1  # indeks input berikutnya

    # ---------- BADAN: video sumber dipasang ke kanvas ----------
    filters.append(
        f"[0:v]scale={lebar}:{tinggi}:force_original_aspect_ratio=increase,"
        f"crop={lebar}:{tinggi},fps={fps},setsar=1,format=yuv420p[b0]"
    )
    sekarang = "b0"

    # ---------- LAPISAN: overlay PNG template + teks yang digambar sendiri ----------
    lapisan: list[dict[str, Any]] = []
    for overlay in template.get("overlays") or []:
        berkas = _aset(template, overlay.get("file"))
        if berkas is None:
            continue
        lapisan.append({**overlay, "path": berkas})

    kotak_teks = template.get("text_box") if isinstance(template.get("text_box"), dict) else None
    kotak_badge = template.get("badge_box") if isinstance(template.get("badge_box"), dict) else None
    if kotak_badge is None:
        kotak_badge = kotak_kategori_bawaan(kotak_teks)
    for nomor, teks in enumerate(template.get("texts") or []):
        nama = str(teks.get("name") or f"teks{nomor}")
        isi = texts.get(nama)
        # Layer dengan "source" mengambil isinya dari field template, mis.
        # kategori (NEWS/HIBURAN) yang memang milik set layer, bukan per video.
        if not isi and teks.get("source"):
            isi = template.get(str(teks["source"]))
        isi = str(isi or teks.get("default") or "").strip()
        if not isi:
            continue
        gaya = str(teks.get("style") or "").lower()
        if gaya == "berita":
            # Warna isi mengikuti set layer supaya cocok dengan kotaknya:
            # hitam di kotak terang, putih di kotak gelap. Kata pembuka tetap
            # merah karena kontras di keduanya.
            # Bawaannya PUTIH kalau template belum pernah menyimpan pilihan
            # warna. Dulu bawaannya "tidak ada pendapat", sehingga warna jatuh
            # ke milik layer - dan layer bawaan semua template berwarna hitam.
            # Akibatnya halaman menampilkan "Putih" (bawaannya di sana putih)
            # sementara videonya keluar hitam, dan menekan tombol yang sudah
            # tersorot itu tidak mengubah apa pun. Satu-satunya sumber
            # kebenaran warna tulisan berita sekarang adalah setelan ini.
            warna = str(template.get("teks_warna") or "white").strip().lower()
            if warna not in ("black", "white"):
                warna = "white"
            teks = {**teks, "color": warna}
            if kotak_teks and not teks.get("box"):
                teks = {**teks, "box": kotak_teks}
        if gaya == "kategori" and not teks.get("box"):
            if not kotak_badge:
                continue  # belum ada tempatnya
            teks = {**teks, "box": kotak_badge}
        png = _gambar_teks(teks, isi, lebar, tinggi, kerja / f"text_{nomor}.png")
        # PNG teks sudah seukuran kanvas, jadi cukup ditempel di 0,0.
        lapisan.append(
            {"path": png, "x": 0, "y": 0, "start": teks.get("start"), "end": teks.get("end")}
        )

    for nomor, item in enumerate(lapisan):
        # Overlay berupa video biasanya lebih pendek dari videonya sendiri.
        # Tanpa diulang, ffmpeg menahan frame terakhirnya sehingga animasinya
        # terlihat membeku sampai video habis. "loop": true membuatnya diputar
        # berulang. Hanya berlaku untuk badan video; bagian intro/outro
        # disusun terpisah jadi overlay tidak pernah ikut ke sana.
        if item.get("loop"):
            # "-t" wajib menyertai pengulangan tak terbatas. Tanpa batas
            # durasi, masukannya tidak pernah habis dan ffmpeg menggantung
            # saat menutup berkas. Dibatasi sepanjang badan video saja.
            inputs += ["-stream_loop", "-1", "-t", f"{durasi_badan:.3f}"]
        w, h = item.get("w"), item.get("h")
        # Aset yang jauh lebih besar dari tempatnya digambar dikecilkan dulu:
        # yang memakan RAM adalah frame setelah dibuka, bukan berkasnya.
        jalur_ov = _aset_ringan(item["path"], int(w or 0), int(h or 0))
        inputs += ["-i", str(jalur_ov)]
        sumber_ov = f"{idx}:v"
        # "crop" opsional, formatnya sama seperti ffmpeg: "lebar:tinggi:x:y".
        # Dipakai kalau aset perlu dipotong dulu sebelum diskala, misalnya
        # panel lebar yang hanya sebagian isinya dipakai.
        potong = str(item.get("crop") or "").strip()
        rantai: list[str] = []
        if potong:
            rantai.append(f"crop={potong}")
        if w or h:
            # -2 dan bukan -1: sisi yang dihitung otomatis dibulatkan ke angka
            # genap, karena encoder yuv420p menolak ukuran ganjil.
            rantai.append(f"scale={int(w) if w else -2}:{int(h) if h else -2}")
        if rantai:
            filters.append(f"[{sumber_ov}]{','.join(rantai)}[ov{nomor}]")
            sumber_ov = f"ov{nomor}"
        # x dan y boleh angka atau rumus ffmpeg seperti "main_h-h" (tempel ke
        # dasar) dan "main_w-w" (tempel ke kanan). Rumus dipakai kalau tinggi
        # layer dihitung otomatis dari rasio berkasnya, sehingga posisinya
        # ikut menyesuaikan berapa pun ukuran berkas yang dipasang pengguna.
        x, y = item.get("x"), item.get("y")
        posisi_x = _posisi(x, "(W-w)/2")
        posisi_y = _posisi(y, "(H-h)/2")
        opsi = f"overlay={posisi_x}:{posisi_y}"
        mulai, selesai = item.get("start"), item.get("end")
        if mulai is not None or selesai is not None:
            opsi += f":enable='between(t\\,{float(mulai or 0)}\\,{float(selesai or durasi_badan)})'"
        filters.append(f"[{sekarang}][{sumber_ov}]{opsi}[b{nomor + 1}]")
        sekarang = f"b{nomor + 1}"
        idx += 1

    potongan_v = [sekarang]
    potongan_a: list[str] = []

    # ---------- AUDIO BADAN ----------
    if info_sumber["has_audio"]:
        filters.append(
            "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[ba]"
        )
    else:
        # Klip tanpa audio tetap butuh jalur audio, kalau tidak concat gagal.
        inputs += [
            "-f", "lavfi", "-t", f"{durasi_badan:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        filters.append(f"[{idx}:a]anull[ba]")
        idx += 1
    potongan_a.append("ba")

    # ---------- INTRO & OUTRO ----------
    total = durasi_badan
    for posisi in ("intro", "outro"):
        berkas = _aset(template, template.get(posisi))
        if berkas is None:
            continue
        berkas = _aset_ringan(berkas, lebar, tinggi)
        info = probe(berkas)
        total += info["duration"]
        inputs += ["-i", str(berkas)]
        idx_klip = idx
        idx += 1
        potongan, nama_v = _klip(berkas, lebar, tinggi, fps, idx_klip, f"{posisi}v")
        filters += potongan
        if info["has_audio"]:
            filters.append(
                f"[{idx_klip}:a]aformat=sample_fmts=fltp:sample_rates=44100:"
                f"channel_layouts=stereo[{posisi}a]"
            )
        else:
            inputs += [
                "-f", "lavfi", "-t", f"{max(info['duration'], 0.1):.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
            filters.append(f"[{idx}:a]anull[{posisi}a]")
            idx += 1
        if posisi == "intro":
            potongan_v.insert(0, nama_v)
            potongan_a.insert(0, f"{posisi}a")
        else:
            potongan_v.append(nama_v)
            potongan_a.append(f"{posisi}a")

    # ---------- SAMBUNG ----------
    if len(potongan_v) > 1:
        pasangan = "".join(f"[{v}][{a}]" for v, a in zip(potongan_v, potongan_a))
        filters.append(f"{pasangan}concat=n={len(potongan_v)}:v=1:a=1[outv][outa]")
        peta_v, peta_a = "[outv]", "[outa]"
    else:
        peta_v, peta_a = f"[{potongan_v[0]}]", f"[{potongan_a[0]}]"

    perintah = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        # Ditulis SEBELUM masukan, jadi berlaku untuk semua pembaca video -
        # yang di bawah hanya mengatur penyandi. Tiap utas pembaca menyimpan
        # kumpulan framenya sendiri, dan itu berlipat jadi ratusan MB.
        "-threads", VIDEO_THREADS if VIDEO_THREADS != "0" else "1",
        "-filter_complex_threads", "1",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", peta_v, "-map", peta_a,
        "-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF,
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-r", str(fps),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", "-threads", VIDEO_THREADS,
        "-progress", "pipe:1", "-nostats",
        str(keluaran),
    ]
    return perintah, total


def render(
    template: dict[str, Any],
    source: Path,
    out_dir: Path,
    texts: dict[str, str] | None = None,
    log: Callable[[str], None] | None = None,
    progress: Callable[[int], None] | None = None,
    batal: Callable[[], bool] | None = None,
) -> Path:
    """Susun video akhir sesuai template. Mengembalikan path berkas hasil.

    ``batal`` ditanya berkala selama render berjalan. Begitu ia menjawab True,
    ffmpeg dihentikan dan Dibatalkan dilempar. Pembatalan harus bekerja seperti
    ini, bukan dengan mematikan tugas Celery: worker memakai kumpulan utas, dan
    utas tidak bisa ditembak dari luar seperti proses.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    keluaran = out_dir / "output.mp4"
    perintah, total = _bangun_perintah(template, source, keluaran, texts or {}, out_dir)
    (out_dir / "ffmpeg-command.txt").write_text(" ".join(perintah), encoding="utf-8")
    if log:
        log(f"Menyusun video: kanvas {template.get('width')}x{template.get('height')}, "
            f"perkiraan hasil {total:.1f} detik")

    berkas_log = out_dir / "ffmpeg.log"
    batas_waktu = time.monotonic() + RENDER_TIMEOUT_SECONDS
    with berkas_log.open("w", encoding="utf-8") as galat:
        proses = subprocess.Popen(
            perintah, stdout=subprocess.PIPE, stderr=galat, text=True, bufsize=1
        )
        persen_terakhir = -1
        # Kabar ffmpeg dibaca di utas terpisah, dan yang di bawah mengambilnya
        # dari antrean dengan batas tunggu satu detik.
        #
        # Dulu perulangan ini membaca langsung dari ffmpeg. Selama ffmpeg
        # mengabarkan kemajuan itu baik-baik saja, tetapi ffmpeg yang
        # TERSANGKUT tidak mengabarkan apa pun - dan pembacaan itu menunggu
        # selamanya. Akibatnya permintaan berhenti tidak pernah sampai
        # terbaca dan batas waktu tidak pernah sampai diperiksa: video
        # mandek di 2% dan tombol hentinya tidak berpengaruh, persis yang
        # dilaporkan. Sekarang penantiannya ada di sisi kita, jadi berhenti
        # dan batas waktu tetap bekerja walau ffmpeg membisu.
        antrean: queue.Queue[str | None] = queue.Queue(maxsize=2000)

        def _baca() -> None:
            try:
                for baris in proses.stdout or []:
                    try:
                        antrean.put_nowait(baris)
                    except queue.Full:  # kemajuan boleh terlewat, kabar lain tidak
                        pass
            finally:
                antrean.put(None)

        pembaca = threading.Thread(target=_baca, name="ffmpeg-kabar", daemon=True)
        pembaca.start()
        try:
            periksa_batal = 0.0
            while True:
                try:
                    baris = antrean.get(timeout=1.0)
                except queue.Empty:
                    baris = ""
                if baris is None:
                    break
                baris = baris.strip()
                # Ditanya paling sering sekali per detik; ffmpeg mengirim kabar
                # jauh lebih rapat dari itu dan tiap pertanyaan menyentuh Redis.
                if batal and time.monotonic() - periksa_batal > 1.0:
                    periksa_batal = time.monotonic()
                    if batal():
                        proses.kill()
                        raise Dibatalkan("Pembuatan video dihentikan.")
                if baris.startswith("out_time_ms=") and total > 0:
                    try:
                        detik = int(baris.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    persen = max(0, min(99, int(detik / total * 100)))
                    if progress and persen != persen_terakhir:
                        persen_terakhir = persen
                        progress(persen)
                if time.monotonic() > batas_waktu:
                    proses.kill()
                    raise VideoError(
                        f"Render melewati batas waktu {RENDER_TIMEOUT_SECONDS / 60:.0f} menit "
                        "dan dihentikan."
                    )
        finally:
            if proses.stdout:
                proses.stdout.close()
            proses.wait()

    if proses.returncode != 0 or not keluaran.is_file():
        # Kode -9 berarti prosesnya ditembak SIGKILL. ffmpeg tidak pernah
        # melakukan itu pada dirinya sendiri, jadi penyebabnya hampir selalu
        # kehabisan memori. Tanpa penjelasan ini yang terbaca cuma "keluar
        # dengan kode -9", dan orang tidak punya petunjuk apa pun.
        # Berkas log bisa saja tidak ada - ffmpeg yang gagal dijalankan sama
        # sekali tidak sempat menulis apa pun. Membacanya tanpa pengaman
        # membuat kegagalan apa pun berubah jadi FileNotFoundError, dan
        # penyebab aslinya hilang sebelum sempat terbaca siapa pun.
        try:
            isi_log = berkas_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            isi_log = ""
        baris_log = [b for b in isi_log.strip().splitlines() if b.strip()]
        if proses.returncode == -9:
            # Ditembak SIGKILL. ffmpeg tidak pernah melakukan itu pada dirinya
            # sendiri, jadi penembaknya dari luar - paling sering kernel yang
            # kehabisan memori. Tapi itu DUGAAN, jadi kalimat terakhir ffmpeg
            # ikut dibawa: tanpa itu, sebab yang sebenarnya tidak bisa
            # dilacak sesudahnya, karena log ini ikut terhapus bersama job.
            ekor = baris_log[-1][:200] if baris_log else "ffmpeg tidak sempat menulis log"
            raise VideoError(
                f"Render dihentikan paksa oleh sistem (SIGKILL) pada kanvas "
                f"{template.get('width')}x{template.get('height')}. Biasanya ini "
                f"berarti servernya kehabisan memori - pakai kanvas lebih kecil, "
                f"atau server dengan RAM lebih besar. Kata terakhir ffmpeg: {ekor}"
            )
        detail = baris_log[-1] if baris_log else f"ffmpeg keluar dengan kode {proses.returncode}"
        raise VideoError(f"Gagal menyusun video: {detail[:300]}")

    if progress:
        progress(100)
    if log:
        ukuran = keluaran.stat().st_size / 1_048_576
        hasil = probe(keluaran)
        log(f"Video jadi: {hasil['duration']:.1f} detik, "
            f"{hasil['width']}x{hasil['height']}, {ukuran:.1f} MB")
    return keluaran
