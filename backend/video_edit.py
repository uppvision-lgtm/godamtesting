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

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

import storage

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

# Kandidat font untuk drawtext: Debian (image Docker) lalu macOS (mode via PC).
FONT_CANDIDATES = (
    os.getenv("VIDEO_FONT", ""),
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


def sinkron_template_dari_storage() -> int:
    """Tarik template dari Supabase kalau disk lokal kosong.

    Disk container bersifat sementara: setelah deploy ulang, folder template
    lenyap sementara isinya masih aman di Supabase.
    """
    if not storage.enabled():
        return 0
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
    if not any(templates_dir().iterdir()):
        sinkron_template_dari_storage()
    hasil = []
    for folder in sorted(templates_dir().iterdir()):
        if not folder.is_dir():
            continue
        try:
            hasil.append(load_template(folder.name))
        except VideoError:
            continue
    return hasil


def save_template(data: dict[str, Any], template_id: str | None = None) -> dict[str, Any]:
    template_id = _aman(template_id or data.get("id") or uuid.uuid4().hex[:12])
    folder = template_path(template_id)
    (folder / "assets").mkdir(parents=True, exist_ok=True)
    isi = {**TEMPLATE_CONTOH, **data, "id": template_id}
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


def delete_template(template_id: str) -> None:
    shutil.rmtree(template_path(template_id), ignore_errors=True)


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


def _gambar_teks(
    teks: dict[str, Any], isi: str, lebar: int, tinggi: int, tujuan: Path
) -> Path:
    """Render satu lapisan teks jadi PNG transparan seukuran kanvas.

    Teks digambar sendiri (bukan filter ``drawtext``) karena banyak build
    ffmpeg dikompilasi tanpa libfreetype sehingga drawtext tidak tersedia.
    Cara ini juga memberi pemenggalan baris otomatis dan garis tepi.
    """
    from PIL import Image, ImageDraw, ImageFont

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

    perintah = [
        "yt-dlp",
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
        pesan = (hasil.stderr or hasil.stdout or "").strip().splitlines()
        detail = pesan[-1] if pesan else "penyebab tidak diketahui"
        raise VideoError(f"Gagal mengunduh video: {detail[:300]}")

    berkas = sorted(tujuan.glob("source.*"))
    if not berkas:
        raise VideoError("Video terunduh tetapi berkasnya tidak ditemukan")
    if log:
        ukuran = berkas[0].stat().st_size / 1_048_576
        log(f"Video terunduh ({ukuran:.1f} MB)")
    return berkas[0]


# ============================================================
#  SUSUN PERINTAH FFMPEG
# ============================================================


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

    for nomor, teks in enumerate(template.get("texts") or []):
        nama = str(teks.get("name") or f"teks{nomor}")
        isi = str(texts.get(nama, teks.get("default") or "")).strip()
        if not isi:
            continue
        png = _gambar_teks(teks, isi, lebar, tinggi, kerja / f"text_{nomor}.png")
        # PNG teks sudah seukuran kanvas, jadi cukup ditempel di 0,0.
        lapisan.append(
            {"path": png, "x": 0, "y": 0, "start": teks.get("start"), "end": teks.get("end")}
        )

    for nomor, item in enumerate(lapisan):
        inputs += ["-i", str(item["path"])]
        sumber_ov = f"{idx}:v"
        w, h = item.get("w"), item.get("h")
        if w or h:
            filters.append(
                f"[{sumber_ov}]scale={int(w) if w else -1}:{int(h) if h else -1}[ov{nomor}]"
            )
            sumber_ov = f"ov{nomor}"
        x, y = item.get("x"), item.get("y")
        posisi_x = str(int(x)) if x is not None else "(W-w)/2"
        posisi_y = str(int(y)) if y is not None else "(H-h)/2"
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
