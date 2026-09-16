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
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

import storage

logger = logging.getLogger(__name__)

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", ".media"))
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")

# Preset encode. "veryfast" dipilih karena render jalan di server kecil;
# naikkan ke "medium" kalau CPU-nya lega dan ingin berkas lebih kecil.
VIDEO_PRESET = os.getenv("VIDEO_PRESET", "veryfast")
VIDEO_CRF = os.getenv("VIDEO_CRF", "23")
VIDEO_THREADS = os.getenv("VIDEO_THREADS", "0")

# Batas aman supaya satu job tidak menyandera server selamanya.
MAX_SOURCE_SECONDS = float(os.getenv("VIDEO_MAX_SOURCE_SECONDS", "300"))
RENDER_TIMEOUT_SECONDS = float(os.getenv("VIDEO_RENDER_TIMEOUT", "1800"))
DOWNLOAD_TIMEOUT_SECONDS = float(os.getenv("VIDEO_DOWNLOAD_TIMEOUT", "600"))
PREVIEW_TIMEOUT_SECONDS = float(os.getenv("VIDEO_PREVIEW_TIMEOUT", "45"))
# Jeda minimal antar penyelarasan daftar set layer dengan Supabase.
SINKRON_JEDA_DETIK = float(os.getenv("VIDEO_TEMPLATE_SYNC_SECONDS", "20"))
# Video sumber disimpan sementara per link, supaya merender satu link dengan
# banyak template tidak mengunduhnya berulang kali (Instagram cepat membatasi
# pengunduh yang terlalu sering).
SOURCE_CACHE_SECONDS = float(os.getenv("VIDEO_SOURCE_CACHE_SECONDS", "21600"))

# yt-dlp dipasang sebagai paket Python. Di image Docker ia ada di PATH, tapi
# saat dijalankan dari virtualenv (mode lokal) sering tidak. Jadi dicari dulu,
# dan kalau tetap tidak ketemu dipanggil sebagai modul: "python -m yt_dlp".
YTDLP_BIN = os.getenv("YTDLP_BIN", "").strip()

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
        return tujuan.is_file()
    except storage.StorageError:
        return False


_sinkron_terakhir = 0.0


def sinkron_template_dari_storage(paksa: bool = False) -> int:
    """Tarik set layer yang ada di Supabase tapi belum ada di disk ini.

    Dijalankan tiap kali daftar set dibuka, bukan hanya saat disk kosong:
    set yang dibuat dari perangkat atau container lain tersimpan di Supabase,
    dan tanpa ini tidak pernah muncul di sini. Diberi jeda supaya membuka
    daftar berkali-kali tidak membanjiri Supabase dengan permintaan.
    """
    global _sinkron_terakhir
    if not storage.enabled():
        return 0
    if not paksa and time.time() - _sinkron_terakhir < SINKRON_JEDA_DETIK:
        return 0
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
    return ditarik


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
    return data


def list_templates() -> list[dict[str, Any]]:
    sinkron_template_dari_storage(paksa=not any(templates_dir().iterdir()))
    hasil = []
    for folder in sorted(templates_dir().iterdir()):
        if not folder.is_dir():
            continue
        try:
            hasil.append(load_template(folder.name))
        except VideoError:
            continue
    return hasil


def _nama_set_terpakai(nama: str, kecuali_id: str = "") -> str | None:
    """Kembalikan id set lain yang sudah memakai nama ini, kalau ada.

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
            lain = json.loads(berkas.read_text(encoding="utf-8")).get("name")
        except (OSError, json.JSONDecodeError):
            continue
        if " ".join(str(lain or "").split()).casefold() == target:
            return folder.name
    return None


def nama_set_unik(nama: str, kecuali_id: str = "") -> str:
    """Tambahkan nomor di belakang nama sampai tidak ada yang memakainya."""
    dasar = " ".join(str(nama or "").split()) or "Set layer"
    calon = dasar
    urut = 2
    while _nama_set_terpakai(calon, kecuali_id):
        calon = f"{dasar} {urut}"
        urut += 1
    return calon


def save_template(data: dict[str, Any], template_id: str | None = None) -> dict[str, Any]:
    template_id = _aman(template_id or data.get("id") or uuid.uuid4().hex[:12])
    folder = template_path(template_id)
    (folder / "assets").mkdir(parents=True, exist_ok=True)
    nama = " ".join(str(data.get("name") or "").split())
    bentrok = _nama_set_terpakai(nama, template_id)
    if bentrok:
        raise VideoError(
            f"Nama set layer \"{nama}\" sudah dipakai. Pilih nama lain supaya "
            "tidak tertukar di daftar."
        )
    isi = {**TEMPLATE_CONTOH, **data, "id": template_id, "name": nama}
    (folder / "template.json").write_text(
        json.dumps(isi, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if storage.enabled():
        try:
            storage.upload(
                f"templates/{template_id}/template.json",
                folder / "template.json",
                "application/json",
            )
        except storage.StorageError as error:
            logger.warning("Template tersimpan lokal tapi gagal ke Supabase: %s", error)
    return isi


def duplicate_template(sumber_id: str, nama_baru: str, id_baru: str | None = None) -> dict[str, Any]:
    """Salin satu set layer beserta seluruh berkas asetnya.

    Berkasnya benar-benar disalin, bukan dipakai bersama, supaya mengganti
    atau menghapus layer di salinan tidak ikut mengubah set aslinya.

    Aset yang hanya ada di Supabase (mis. di container yang baru dijalankan)
    ditarik dulu, jadi hasil salinan tetap lengkap.
    """
    asal = load_template(sumber_id)
    id_baru = _aman(id_baru or uuid.uuid4().hex[:12])
    if id_baru == _aman(sumber_id):
        raise VideoError("Set layer salinan harus memakai id yang berbeda")

    folder_asal = template_path(sumber_id)
    folder_baru = template_path(id_baru)
    (folder_baru / "assets").mkdir(parents=True, exist_ok=True)

    # Pastikan aset yang dipakai set asal ada di disk sebelum disalin.
    dipakai = [o.get("file") for o in (asal.get("overlays") or [])]
    dipakai += [asal.get("intro"), asal.get("outro")]
    for nama in dipakai:
        if nama:
            try:
                _aset(asal, nama)
            except VideoError as error:
                logger.warning("Aset %s dilewati saat menyalin: %s", nama, error)

    disalin = 0
    for berkas in sorted((folder_asal / "assets").glob("*")):
        if not berkas.is_file():
            continue
        tujuan = folder_baru / "assets" / berkas.name
        shutil.copy2(berkas, tujuan)
        disalin += 1
        if storage.enabled():
            try:
                storage.upload(f"templates/{id_baru}/assets/{berkas.name}", tujuan)
            except storage.StorageError as error:
                logger.warning("Aset %s gagal disalin ke Supabase: %s", berkas.name, error)

    # Nama salinan diberi nomor otomatis kalau sudah ada yang memakainya,
    # jadi menyalin berkali-kali tidak menghasilkan nama kembar.
    diminta = nama_baru.strip() or f"{asal.get('name', 'Set')} (salinan)"
    isi = {**asal, "id": id_baru, "name": nama_set_unik(diminta, id_baru)}
    hasil = save_template(isi, id_baru)
    logger.info("Set %s disalin ke %s beserta %d berkas", sumber_id, id_baru, disalin)
    return hasil


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
    shutil.rmtree(template_path(template_id), ignore_errors=True)
    # Salinan di Supabase ikut dihapus; kalau tidak, set ini muncul lagi
    # begitu container baru menyinkronkan template dari penyimpanan.
    if not storage.enabled():
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
    if not path.is_file():
        raise VideoError(f"Aset '{nama}' tidak ditemukan di template")
    return path


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

    # Susutkan font sampai paragrafnya muat, baik jumlah barisnya maupun —
    # kalau ada kotak — tinggi keseluruhannya.
    ukuran = ukuran_awal
    font = ImageFont.truetype(jalur_font, ukuran)
    baris = _bungkus_kata(gambar, kata, font, batas)
    def belum_muat() -> bool:
        if len(baris) > maks_baris:
            return True
        return tinggi_maks is not None and tinggi_blok(ukuran, len(baris)) > tinggi_maks
    while belum_muat() and ukuran > ukuran_min:
        ukuran -= 1
        font = ImageFont.truetype(jalur_font, ukuran)
        baris = _bungkus_kata(gambar, kata, font, batas)
    if len(baris) > maks_baris:
        logger.warning(
            "Teks '%s' tetap %d baris pada ukuran %d; sisanya dibuang",
            teks.get("name"), len(baris), ukuran,
        )
        baris = baris[:maks_baris]

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

    for nomor, kata_baris in enumerate(baris):
        y = atas + nomor * tinggi_baris
        lebar_kata = [gambar.textlength(k, font=font) for k in kata_baris]
        terakhir = nomor == len(baris) - 1
        # Baris terakhir dan baris satu kata tidak diregangkan (gaya justify
        # pada umumnya), selebihnya sisa lebar dibagi rata ke sela kata.
        if terakhir or len(kata_baris) < 2:
            sela = lebar_spasi
        else:
            sela = (batas - sum(lebar_kata)) / (len(kata_baris) - 1)
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


def download_source(
    url: str,
    tujuan: Path,
    log: Callable[[str], None] | None = None,
    session_id: str | None = None,
) -> Path:
    """Unduh video dari link mana pun yang dikenali yt-dlp."""
    url = str(url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise VideoError("Link video harus diawali http:// atau https://")
    tujuan.mkdir(parents=True, exist_ok=True)
    keluaran = tujuan / "source.%(ext)s"

    cache = MEDIA_DIR / "cache" / hashlib.sha1(url.encode("utf-8")).hexdigest()

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

    perintah = _perintah_ytdlp() + [
        "--no-playlist",
        "--no-progress",
        "--retries", "3",
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
        hasil = subprocess.run(
            perintah + [url],
            capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT_SECONDS,
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
        raise VideoError(f"Gagal mengunduh video: {detail[:300]}")

    berkas = sorted(tujuan.glob("source.*"))
    if not berkas:
        raise VideoError("Video terunduh tetapi berkasnya tidak ditemukan")
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

    perintah = _perintah_ytdlp() + [
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--skip-download",
        "--dump-single-json",
        "--retries", "2",
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
        hasil = subprocess.run(
            perintah + [url], capture_output=True, text=True, timeout=PREVIEW_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as error:
        raise VideoError("Pratinjau link melewati batas waktu") from error
    finally:
        if berkas_cookie is not None:
            shutil.rmtree(berkas_cookie.parent, ignore_errors=True)

    if hasil.returncode != 0:
        pesan = (hasil.stderr or hasil.stdout or "").strip().splitlines()
        detail = pesan[-1] if pesan else "penyebab tidak diketahui"
        raise VideoError(f"Tidak bisa membaca link: {detail[:300]}")

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
        # Peringatan kalau videonya lebih panjang dari batas render.
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


def komposit_statis(template: dict[str, Any]) -> Any:
    """Susun layer gambar template di atas kanvas gelap, tanpa video.

    Dipakai halaman untuk menggambar kotak teks di atas tampilan sebenarnya,
    dan oleh deteksi otomatis. Layer video dilewati.
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
    return kanvas


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
            warna = str(template.get("teks_warna") or "").strip().lower()
            if warna in ("black", "white"):
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
        inputs += ["-i", str(item["path"])]
        sumber_ov = f"{idx}:v"
        w, h = item.get("w"), item.get("h")
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
) -> Path:
    """Susun video akhir sesuai template. Mengembalikan path berkas hasil."""
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
        try:
            for baris in proses.stdout or []:
                baris = baris.strip()
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
                    raise VideoError("Render video melewati batas waktu")
        finally:
            if proses.stdout:
                proses.stdout.close()
            proses.wait()

    if proses.returncode != 0 or not keluaran.is_file():
        pesan = berkas_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        detail = pesan[-1] if pesan else f"ffmpeg keluar dengan kode {proses.returncode}"
        raise VideoError(f"Gagal menyusun video: {detail[:300]}")

    if progress:
        progress(100)
    if log:
        ukuran = keluaran.stat().st_size / 1_048_576
        hasil = probe(keluaran)
        log(f"Video jadi: {hasil['duration']:.1f} detik, "
            f"{hasil['width']}x{hasil['height']}, {ukuran:.1f} MB")
    return keluaran
