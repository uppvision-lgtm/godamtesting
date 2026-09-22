"""API auto edit video: kelola template dan jalankan job render."""

from __future__ import annotations

import logging
import os
import time
import subprocess
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator

import storage
import video_edit as ve
import video_hook as vh
import video_tasks as vt
from auth_api import bersihkan_id_tamu, identitas, pengguna_wajib

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/video", tags=["video"])

# Berapa render yang benar-benar berjalan BERSAMAAN ditentukan sepenuhnya oleh
# CELERY_CONCURRENCY pada worker, bukan oleh berkas ini. Di sini hanya ada dua
# pembatas, dan keduanya BAWAANNYA TANPA BATAS:
#
#   VIDEO_MAX_QUEUED_JOBS  panjang antrean yang boleh menunggu
#   VIDEO_MAX_BATCH        berapa template boleh dikirim dalam satu permintaan
#
# Isi dengan angka di atas nol kalau suatu saat perlu direm, misalnya di server
# kecil. Kosong atau nol berarti tidak dibatasi sama sekali — itulah keadaan
# yang diinginkan di server sendiri yang kuat.
MAX_QUEUED_JOBS = max(0, int(os.getenv("VIDEO_MAX_QUEUED_JOBS", "0")))
MAX_BATCH = max(0, int(os.getenv("VIDEO_MAX_BATCH", "0")))
MAX_ASSET_MB = float(os.getenv("VIDEO_MAX_ASSET_MB", "200"))
# Sisi terpanjang gambar layer setelah dirapikan. Berkas desain kerap jauh
# lebih besar dari kanvas 720x1280 — satu berkas 3375x5706 memakan 77 MB
# begitu ffmpeg membukanya, dan beberapa layer sekaligus membuat proses render
# dimatikan kehabisan memori di server kecil. Batas ini masih di atas ukuran
# tampil terbesar (936 px), jadi ketajamannya tidak berkurang.
MAX_SISI_GAMBAR = int(os.getenv("VIDEO_MAX_IMAGE_SIDE", "1600"))
JENIS_GAMBAR = {".png", ".jpg", ".jpeg", ".webp"}
# Berlaku sama untuk video layer, intro, dan outro. Berkas 4K pada kanvas
# 720x1280 memaksa ffmpeg membongkar frame raksasa yang langsung dibuang
# setelah diperkecil; di container kecil itu membuat render dimatikan sistem.
MAX_SISI_VIDEO = int(os.getenv("VIDEO_MAX_VIDEO_SIDE", "1920"))
JENIS_VIDEO = {".mp4", ".mov", ".m4v", ".webm"}
JENIS_ASET = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".m4v", ".webm"}
STATUS_AKTIF = ("queued", "downloading", "rendering")


class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    width: int = Field(default=1080, ge=120, le=4096)
    height: int = Field(default=1920, ge=120, le=4096)
    fps: int = Field(default=30, ge=1, le=60)
    intro: str | None = None
    outro: str | None = None
    max_duration: float | None = Field(default=None, ge=1, le=600)
    overlays: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    # Kotak teks hook dalam piksel kanvas: {x, y, w, h}. None = pakai posisi
    # bawaan layer teks.
    text_box: dict[str, int] | None = None
    badge_box: dict[str, int] | None = None
    # Keduanya menerima null: set yang dibuat sebelum field ini ada menyimpan
    # nilai kosong, dan membacanya lalu menyimpannya kembali harus tetap bisa.
    kategori: str | None = Field(default="", max_length=30)
    teks_warna: str | None = "white"

    @field_validator("kategori")
    @classmethod
    def _kategori_bersih(cls, v: str | None) -> str:
        return " ".join(str(v or "").split())

    @field_validator("teks_warna")
    @classmethod
    def _warna_sah(cls, v: str | None) -> str:
        return v if v in ("black", "white") else "white"


class DuplikatBody(BaseModel):
    """Nama untuk salinan set layer."""

    name: str = Field(min_length=1, max_length=100)


class BatchBody(BaseModel):
    """Satu link dirender dengan banyak set layer sekaligus."""

    url: str = Field(min_length=5, max_length=2000)
    # Tanpa batas atas kecuali VIDEO_MAX_BATCH diisi: satu kali kirim boleh
    # berisi berapa pun template.
    template_ids: list[str] = Field(min_length=1, max_length=MAX_BATCH or None)
    texts: dict[str, str] = {}
    session_id: str = ""
    # Alternatif session_id: id akun Instagram yang tertaut di Kelola Akun.
    # Cookie-nya diambil di server, jadi tidak pernah lewat peramban.
    account_id: int | None = None
    # Warna tulisan berita khusus render ini. Kosong = ikut setelan tiap
    # template, yang memang berbeda-beda antar template.
    teks_warna: str | None = None

    @field_validator("teks_warna")
    @classmethod
    def _warna_render(cls, v: str | None) -> str:
        return v if v in ("black", "white") else ""


class JobBody(BaseModel):
    url: str = Field(min_length=5, max_length=2000)
    template_id: str = Field(min_length=1, max_length=80)
    texts: dict[str, str] = {}
    # Opsional: dipakai kalau link sumbernya dari Instagram (butuh cookie).
    session_id: str = ""
    account_id: int | None = None


class PreviewBody(BaseModel):
    """Link video yang mau dilihat isinya sebelum diproses."""

    url: str = Field(min_length=5, max_length=2000)
    session_id: str = ""
    account_id: int | None = None


class HookBody(BaseModel):
    """Naskah mentah dari pengguna yang mau dirapikan jadi 3 baris hook."""

    naskah: str = Field(min_length=3, max_length=5000)


def _rapikan_gambar(path: Path) -> int | None:
    """Pangkas pinggiran transparan lalu perkecil gambar layer bila kebesaran.

    Dua-duanya menghemat memori saat render. Pemangkasan juga membuat template
    tidak perlu memakai "crop" untuk membuang pinggiran kosong, sehingga
    ukuran berkas asli tidak lagi ikut menentukan hasil.

    Mengembalikan ukuran berkas baru, atau None kalau tidak ada yang diubah.
    """
    if path.suffix.lower() not in JENIS_GAMBAR:
        return None
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGBA")
            kotak = im.getbbox()
            berubah = False
            if kotak and kotak != (0, 0, im.width, im.height):
                im = im.crop(kotak)
                berubah = True
            sisi = max(im.width, im.height)
            if sisi > MAX_SISI_GAMBAR:
                skala = MAX_SISI_GAMBAR / sisi
                im = im.resize(
                    (max(1, round(im.width * skala)), max(1, round(im.height * skala))),
                    Image.LANCZOS,
                )
                berubah = True
            if not berubah:
                return None
            im.save(path, format="PNG")
    except Exception as error:  # berkas rusak atau format tak terduga
        logger.warning("Gambar %s tidak bisa dirapikan: %s", path.name, error)
        return None
    return path.stat().st_size


def _rapikan_video(path: Path) -> int | None:
    """Perkecil video layer/outro yang resolusinya jauh di atas kanvas.

    Hanya dijalankan sekali saat berkas diunggah, jadi setiap render sesudahnya
    ikut lebih ringan. Mengembalikan ukuran berkas baru, atau None kalau tidak
    ada yang diubah.
    """
    if path.suffix.lower() not in JENIS_VIDEO:
        return None
    try:
        info = ve.probe(path)
    except ve.VideoError as error:
        logger.warning("Video %s tidak bisa dibaca: %s", path.name, error)
        return None

    lebar, tinggi = int(info.get("width") or 0), int(info.get("height") or 0)
    if not lebar or not tinggi or max(lebar, tinggi) <= MAX_SISI_VIDEO:
        return None

    skala = MAX_SISI_VIDEO / max(lebar, tinggi)
    # Lebar dan tinggi dibulatkan ke angka genap; encoder yuv420p menolak
    # ukuran ganjil.
    baru_l = max(2, int(round(lebar * skala / 2)) * 2)
    baru_t = max(2, int(round(tinggi * skala / 2)) * 2)
    sementara = path.with_name(path.stem + "-kecil" + path.suffix)
    perintah = [
        ve.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(path),
        "-vf", f"scale={baru_l}:{baru_t}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        # Audio disalin apa adanya kalau ada; kalau tidak, dilewati.
        "-c:a", "copy",
        str(sementara),
    ]
    try:
        hasil = subprocess.run(perintah, capture_output=True, text=True, timeout=600)
        if hasil.returncode != 0 or not sementara.is_file():
            # Sebagian berkas audionya tidak bisa disalin; ulangi dengan aac.
            perintah[perintah.index("copy")] = "aac"
            hasil = subprocess.run(perintah, capture_output=True, text=True, timeout=600)
        if hasil.returncode != 0 or not sementara.is_file():
            logger.warning(
                "Video %s gagal diperkecil: %s", path.name, (hasil.stderr or "")[:200]
            )
            sementara.unlink(missing_ok=True)
            return None
        sementara.replace(path)
    except (subprocess.TimeoutExpired, OSError) as error:
        logger.warning("Video %s gagal diperkecil: %s", path.name, error)
        sementara.unlink(missing_ok=True)
        return None

    logger.info("Video %s diperkecil %dx%d -> %dx%d", path.name, lebar, tinggi, baru_l, baru_t)
    return path.stat().st_size


def _tangani(error: ve.VideoError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


# Berapa kali dorongan ke Supabase diulang kalau gagal. Dijalankan setelah
# jawaban dikirim, jadi pengguna tidak menunggu.
DORONG_ULANG = 3


def _dorong_ke_storage(jalur: str, berkas: Path) -> None:
    """Simpan aset ke Supabase di latar belakang, dengan pengulangan.

    Aset layer berukuran puluhan MB dan mengunggahnya ke Supabase bisa makan
    belasan detik. Dulu itu terjadi SEBELUM jawaban dikirim, jadi pengguna
    menunggu dua kali: sekali mengirim berkas ke server, sekali lagi menunggu
    server mengirimkannya ke Supabase. Berkasnya sudah ada di disk begitu
    jawaban dikirim, jadi render dan pratinjau langsung bisa memakainya;
    salinan di Supabase hanya perlu untuk bertahan melewati deploy ulang.
    """
    for percobaan in range(1, DORONG_ULANG + 1):
        try:
            storage.upload(jalur, berkas)
            if percobaan > 1:
                logger.info("Aset %s tersimpan ke Supabase pada percobaan ke-%d", jalur, percobaan)
            return
        except storage.StorageError as error:
            if percobaan == DORONG_ULANG:
                logger.error(
                    "Aset %s GAGAL disimpan ke Supabase setelah %d percobaan: %s. "
                    "Berkasnya masih ada di disk server, tetapi akan hilang saat "
                    "container dijalankan ulang.",
                    jalur, DORONG_ULANG, error,
                )
                return
            logger.warning("Aset %s gagal ke Supabase (percobaan %d): %s", jalur, percobaan, error)
            time.sleep(2 * percobaan)


# ============================================================
#  SIAPA BOLEH APA
# ============================================================
#
# Template punya pemilik: username akun, "guest:xxx" untuk tamu, atau kosong
# untuk template bawaan. Tamu bekerja seperti profil tamu di peramban — semua
# fitur jalan, termasuk membuat dan menyunting template — tetapi miliknya
# tidak pernah naik ke Supabase dan dibuang otomatis setelah beberapa jam.
# Job pun dicatat pemiliknya, jadi milik satu orang tidak terlihat orang lain.


def _sesi_dari_akun(account_id: int | None, pengguna: dict[str, Any] | None) -> str:
    """Session ID Instagram dari akun yang tertaut di Kelola Akun.

    Supaya orang tidak perlu mengetik cookie lagi di Auto Edit Video: cukup
    memilih akunnya, dan nilainya diambil di sini. Hanya untuk pengguna yang
    masuk - akun sosmed milik pengguna, bukan milik tamu - dan hanya akun
    Instagram, sebab itulah satu-satunya sumber link yang butuh cookie.
    Kalau paket autoupload tidak terpasang, diam-diam kembali kosong: alur
    lama dengan session_id yang diketik tetap jalan.
    """
    if not account_id or not pengguna or pengguna.get("tamu"):
        return ""
    try:
        from autoupload.db import SessionLocal, SocialAccount
        from autoupload.rahasia import buka
    except Exception:  # noqa: BLE001 - autoupload tidak ada di server ini
        return ""
    db = SessionLocal()
    try:
        akun = (
            db.query(SocialAccount)
            .filter(
                SocialAccount.id == account_id,
                SocialAccount.is_active.is_(True),
                SocialAccount.owner == str(pengguna.get("username") or ""),
            )
            .first()
        )
        if akun is None or akun.platform != "instagram":
            return ""
        return buka(akun.session_id or "")
    finally:
        db.close()


def _nama_akun(pengguna: dict[str, Any] | None) -> str | None:
    return str(pengguna["username"]) if pengguna else None


def _template_terlihat(template_id: str, pengguna: dict[str, Any] | None) -> dict[str, Any]:
    """Template yang boleh dibaca akun ini: miliknya sendiri atau template bawaan."""
    try:
        data = ve.load_template(template_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if not ve.boleh_lihat(data, _nama_akun(pengguna)):
        # 404, bukan 403: keberadaan template orang lain tidak perlu diumumkan.
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' tidak ditemukan")
    return data


def _periksa_ubah(data: dict[str, Any], pengguna: dict[str, Any] | None) -> None:
    nama = _nama_akun(pengguna)
    if not ve.boleh_lihat(data, nama):
        raise HTTPException(status_code=404, detail=f"Template '{data.get('id')}' tidak ditemukan")
    if not pengguna:
        raise HTTPException(
            status_code=401, detail="Muat ulang halaman, lalu coba lagi."
        )
    if not ve.boleh_ubah(data, nama):
        raise HTTPException(
            status_code=403,
            detail="Template bawaan tidak bisa diubah. Duplikat dulu untuk menyuntingnya.",
        )


def _template_milik(template_id: str, pengguna: dict[str, Any] | None) -> dict[str, Any]:
    """Template yang boleh diubah akun ini. Tamu selalu ditolak."""
    try:
        data = ve.load_template(template_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    _periksa_ubah(data, pengguna)
    return data


def _job_terjangkau(job_id: str, pengguna: dict[str, Any] | None) -> dict[str, Any]:
    """Job milik akun ini; untuk tamu, job tamu (tanpa pemilik)."""
    try:
        data = vt.baca_status(job_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if str(data.get("owner") or "") != (_nama_akun(pengguna) or ""):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' tidak ditemukan")
    return data


# ============================================================
#  PRATINJAU LINK
# ============================================================


@router.post("/preview")
def pratinjau_link(
    body: PreviewBody, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, Any]:
    """Tampilkan isi link (judul, durasi, sampul) tanpa mengunduh videonya."""
    sesi = body.session_id.strip() or _sesi_dari_akun(body.account_id, pengguna)
    try:
        return ve.preview_source(body.url, sesi or None)
    except ve.VideoError as error:
        raise _tangani(error)


# ============================================================
#  HOOK OTOMATIS
# ============================================================


@router.post("/hook")
async def buat_hook(body: HookBody) -> dict[str, Any]:
    """Ubah naskah mentah jadi 3 baris lower-third siap tempel ke template.

    Tidak pernah melempar galat selama naskahnya terisi: kalau DeepSeek tidak
    tersedia, dipakai ringkasan sederhana dari naskah itu sendiri.
    """
    hasil = await vh.buat_hook(body.naskah)
    if not hasil["hook"]:
        raise HTTPException(
            status_code=400,
            detail="Naskah terlalu pendek untuk dijadikan hook.",
        )
    return hasil


# ============================================================
#  TEMPLATE
# ============================================================


@router.get("/templates")
def daftar_template(
    pengguna: dict[str, Any] | None = Depends(identitas),
) -> dict[str, Any]:
    """Template milik akun yang masuk; untuk tamu, template bawaan saja."""
    return {"templates": ve.list_templates(owner=_nama_akun(pengguna))}


@router.post("/templates")
def buat_template(
    body: TemplateBody,
    template_id: str | None = None,
    pengguna: dict[str, Any] | None = Depends(identitas),
) -> dict[str, Any]:
    """Buat atau perbarui template. Tamu tidak bisa menyimpan apa pun."""
    ada: dict[str, Any] | None = None
    if template_id:
        try:
            ada = ve.load_template(template_id)
        except ve.VideoError:
            ada = None
    if ada is not None:
        _periksa_ubah(ada, pengguna)
    elif not pengguna:
        raise HTTPException(status_code=401, detail="Muat ulang halaman, lalu coba lagi.")
    try:
        return ve.save_template(body.model_dump(), template_id, owner=_nama_akun(pengguna))
    except ve.VideoError as error:
        raise _tangani(error) from error


@router.get("/templates/{template_id}")
def ambil_template(
    template_id: str, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, Any]:
    data = _template_terlihat(template_id, pengguna)
    aset = sorted(
        p.name for p in (ve.template_path(template_id) / "assets").glob("*") if p.is_file()
    )
    # Kotak badge yang akan dipakai kalau pengguna belum menggambarnya, supaya
    # halaman bisa menampilkannya sebagai tebakan. can_edit memberi tahu
    # halaman apakah kendali penyuntingan perlu dikunci.
    return {
        **data,
        "assets": aset,
        "badge_box_default": ve.kotak_kategori_bawaan(data.get("text_box")),
        "can_edit": ve.boleh_ubah(data, _nama_akun(pengguna)),
    }


@router.get("/templates/{template_id}/preview.png")
def pratinjau_template(
    template_id: str, pengguna: dict[str, Any] | None = Depends(identitas)
) -> Response:
    """Gambar komposit layer statis template (tanpa video), untuk halaman."""
    import io

    template = _template_terlihat(template_id, pengguna)
    try:
        kanvas = ve.komposit_statis(template)
    except ve.VideoError as error:
        raise _tangani(error)
    buf = io.BytesIO()
    kanvas.convert("RGB").save(buf, format="PNG", optimize=True)
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/templates/{template_id}/detect-box")
def deteksi_kotak(
    template_id: str, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, Any]:
    """Tebak posisi kotak teks dari layer gambar template."""
    template = _template_terlihat(template_id, pengguna)
    try:
        kotak = ve.deteksi_kotak_teks(ve.komposit_statis(template))
    except ve.VideoError as error:
        raise _tangani(error)
    if kotak is None:
        raise HTTPException(status_code=404, detail="Tidak menemukan bidang polos yang cukup luas untuk kotak teks.")
    return {"text_box": kotak}


@router.post("/templates/{template_id}/duplicate")
def duplikat_template(
    template_id: str, body: DuplikatBody, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, Any]:
    """Buat set layer baru dengan isi yang sama, hanya namanya berbeda.

    Salinannya menjadi milik yang menyalin, juga saat sumbernya template
    bawaan. Itulah cara menyunting template bawaan, baik sebagai akun maupun
    sebagai tamu.
    """
    if not pengguna:
        raise HTTPException(status_code=401, detail="Muat ulang halaman, lalu coba lagi.")
    _template_terlihat(template_id, pengguna)
    try:
        hasil = ve.duplicate_template(template_id, body.name, owner=pengguna["username"])
    except ve.VideoError as error:
        raise _tangani(error)
    aset = sorted(
        p.name for p in (ve.template_path(hasil["id"]) / "assets").glob("*") if p.is_file()
    )
    return {**hasil, "assets": aset, "badge_box_default": ve.kotak_kategori_bawaan(hasil.get("text_box"))}


@router.delete("/templates/{template_id}")
def hapus_template(
    template_id: str, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, bool]:
    _template_milik(template_id, pengguna)
    ve.delete_template(template_id)
    return {"ok": True}


@router.post("/templates/{template_id}/assets")
async def unggah_aset(
    template_id: str,
    latar: BackgroundTasks,
    file: UploadFile = File(...),
    pengguna: dict[str, Any] | None = Depends(identitas),
) -> dict[str, Any]:
    """Terima satu berkas layer (PNG overlay, intro, atau outro)."""
    _template_milik(template_id, pengguna)

    nama = ve._aman(Path(file.filename or "aset").name)
    if Path(nama).suffix.lower() not in JENIS_ASET:
        raise HTTPException(
            status_code=415,
            detail=f"Jenis berkas tidak didukung. Pakai: {', '.join(sorted(JENIS_ASET))}",
        )
    folder = ve.template_path(template_id) / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    tujuan = folder / nama
    batas = int(MAX_ASSET_MB * 1_048_576)
    ukuran = 0
    try:
        with tujuan.open("wb") as keluar:
            while potongan := await file.read(1_048_576):
                ukuran += len(potongan)
                if ukuran > batas:
                    raise HTTPException(
                        status_code=413, detail=f"Berkas melebihi {MAX_ASSET_MB:.0f} MB"
                    )
                keluar.write(potongan)
    except HTTPException:
        tujuan.unlink(missing_ok=True)
        raise

    ukuran = _rapikan_gambar(tujuan) or _rapikan_video(tujuan) or ukuran

    # Template tamu memang tidak disimpan permanen, jadi tidak perlu didorong.
    if storage.enabled() and not ve.is_tamu(_nama_akun(pengguna)):
        latar.add_task(
            _dorong_ke_storage, f"templates/{ve._aman(template_id)}/assets/{nama}", tujuan
        )
    return {"file": f"assets/{nama}", "size": ukuran}


@router.delete("/templates/{template_id}/assets/{nama}")
def hapus_aset(
    template_id: str, nama: str, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, bool]:
    _template_milik(template_id, pengguna)
    berkas = ve.template_path(template_id) / "assets" / ve._aman(nama)
    berkas.unlink(missing_ok=True)
    return {"ok": True}


# ============================================================
#  JOB RENDER
# ============================================================


@router.post("/jobs")
def mulai_job(
    body: JobBody, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, Any]:
    _template_terlihat(body.template_id, pengguna)

    _pastikan_antrean_muat(1)
    job_id = vt.buat_job(
        body.url.strip(), body.template_id, body.texts, owner=_nama_akun(pengguna) or ""
    )
    sesi = body.session_id.strip() or _sesi_dari_akun(body.account_id, pengguna)
    vt.render_video.delay(job_id, body.url.strip(), body.template_id, body.texts, sesi)
    return {"job_id": job_id, "status": "queued"}


def _pastikan_antrean_muat(tambahan: int) -> None:
    """Tolak kalau antrean sudah penuh. Tanpa MAX_QUEUED_JOBS, tidak pernah menolak.

    Saat tidak dibatasi, pemeriksaannya dilewati sama sekali — jadi mengirim
    banyak job tidak perlu membaca seluruh antrean lebih dulu.
    """
    if MAX_QUEUED_JOBS <= 0:
        return
    semua = vt.daftar_job(MAX_QUEUED_JOBS + tambahan + 1)
    antre = [j for j in semua if j.get("status") == "queued"]
    if len(antre) + tambahan > MAX_QUEUED_JOBS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Antrean penuh: {len(antre)} job menunggu (maksimal {MAX_QUEUED_JOBS}). "
                "Tunggu sebagian selesai lalu coba lagi."
            ),
        )


@router.post("/sources")
async def unggah_sumber(latar: BackgroundTasks, file: UploadFile = File(...)) -> dict[str, Any]:
    """Terima video sumber dari komputer pengguna.

    Mengembalikan alamat "upload://<id>" yang dipakai persis seperti link:
    dikirim ke jobs/batch, lalu tiap job menyalinnya sebagai video sumber.
    """
    nama = ve._aman(Path(file.filename or "video").name)
    if Path(nama).suffix.lower() not in JENIS_VIDEO:
        raise HTTPException(
            status_code=415,
            detail=f"Jenis berkas tidak didukung. Pakai: {', '.join(sorted(JENIS_VIDEO))}",
        )
    id_unggahan = uuid.uuid4().hex[:12]
    folder = ve.uploads_dir() / id_unggahan
    folder.mkdir(parents=True, exist_ok=True)
    tujuan = folder / f"source{Path(nama).suffix.lower()}"
    batas = int(ve.MAX_SOURCE_UPLOAD_MB * 1_048_576)
    ukuran = 0
    try:
        with tujuan.open("wb") as keluar:
            while potongan := await file.read(1_048_576):
                ukuran += len(potongan)
                if ukuran > batas:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Video sumber melebihi {ve.MAX_SOURCE_UPLOAD_MB:.0f} MB.",
                    )
                keluar.write(potongan)
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    try:
        info = ve.probe(tujuan)
    except ve.VideoError as error:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=415, detail=f"Berkas bukan video yang bisa dibaca: {error}")
    # Didorong ke Supabase supaya worker render bisa mengambilnya walaupun
    # berjalan sebagai layanan terpisah yang tidak berbagi disk dengan API.
    # Seperti aset layer, dorongannya menyusul setelah jawaban dikirim.
    if storage.enabled():
        latar.add_task(
            _dorong_ke_storage, f"{ve.AWALAN_UNGGAHAN_STORAGE}{id_unggahan}/{tujuan.name}", tujuan
        )
    return {
        "url": f"{ve.AWALAN_UNGGAHAN}{id_unggahan}",
        "name": nama,
        "size": ukuran,
        "duration": info.get("duration"),
        "width": info.get("width"),
        "height": info.get("height"),
    }


@router.post("/jobs/batch")
def mulai_banyak_job(
    body: BatchBody, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, Any]:
    """Buat satu job per set layer untuk link yang sama.

    Jumlah template tidak dibatasi. Berapa yang dikerjakan bersamaan mengikuti
    CELERY_CONCURRENCY di worker; sisanya menunggu giliran di antrean. Video
    sumbernya diunduh sekali lalu dipakai ulang dari cache, jadi 100 set tidak
    berarti 100 unduhan.
    """
    ids: list[str] = []
    for tid in body.template_ids:
        tid = tid.strip()
        if tid and tid not in ids:
            ids.append(tid)
    if not ids:
        raise HTTPException(status_code=400, detail="Pilih minimal satu set layer.")
    for tid in ids:
        _template_terlihat(tid, pengguna)
    _pastikan_antrean_muat(len(ids))

    url = body.url.strip()
    pemilik = _nama_akun(pengguna) or ""
    sesi = body.session_id.strip() or _sesi_dari_akun(body.account_id, pengguna)
    jobs = []
    # Dikirim hanya kalau memang diminta, supaya worker versi lama pun tetap
    # menerima panggilan yang bentuknya persis seperti sebelumnya.
    tambahan = {"teks_warna": body.teks_warna} if body.teks_warna else {}
    for tid in ids:
        job_id = vt.buat_job(url, tid, body.texts, owner=pemilik)
        vt.render_video.delay(job_id, url, tid, body.texts, sesi, **tambahan)
        jobs.append({"job_id": job_id, "template_id": tid, "status": "queued"})
    return {"jobs": jobs}


@router.get("/jobs")
def daftar_job(
    limit: int = 20, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, Any]:
    """Riwayat render akun yang masuk. Tamu tidak punya riwayat tersimpan."""
    if not pengguna or pengguna.get("tamu"):
        return {"jobs": []}
    return {"jobs": vt.daftar_job(max(1, min(100, limit)), owner=pengguna["username"])}


@router.get("/jobs/{job_id}")
def status_job(
    job_id: str, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, Any]:
    return _job_terjangkau(job_id, pengguna)


@router.get("/jobs/{job_id}/file")
def berkas_job(
    job_id: str, pengguna: dict[str, Any] | None = Depends(identitas)
) -> FileResponse:
    status = _job_terjangkau(job_id, pengguna)
    if not status.get("output"):
        raise HTTPException(status_code=409, detail="Video belum selesai disusun")
    # Supabase didahulukan. Mengalirkan video lewat backend berarti tiap
    # pemutaran dan tiap unduhan memakai bandwidth serta memori container,
    # padahal Supabase sudah menyajikannya langsung ke peramban. Berkas lokal
    # hanya dipakai kalau salinan di Supabase memang tidak ada.
    if status.get("url"):
        return RedirectResponse(str(status["url"]))
    berkas = vt.job_path(job_id) / str(status["output"])
    if not berkas.is_file():
        raise HTTPException(status_code=404, detail="Berkas hasil sudah tidak ada")
    return FileResponse(berkas, media_type="video/mp4", filename=f"{job_id}.mp4")


@router.post("/jobs/{job_id}/ke-sosmed")
def salin_ke_sosmed(
    job_id: str, pengguna: dict[str, Any] = Depends(pengguna_wajib)
) -> dict[str, Any]:
    """Jadikan hasil render sebagai berkas unggahan Auto Upload.

    Inilah sambungan antara dua alat: Auto Edit Video menyusun videonya, Auto
    Upload mengirimnya, dan keduanya tidak saling kenal. Berkasnya DISALIN,
    bukan dirujuk: folder job punya pembersihan sendiri, dan tautan yang
    menunjuk ke berkas yang keburu dibersihkan akan gagal saat giliran unggah
    tiba. Kalau salinan lokalnya sudah tidak ada (worker di mesin lain),
    diambil dari alamat publiknya.
    """
    status = _job_terjangkau(job_id, {**pengguna, "tamu": False})
    if status.get("status") != "done" or not status.get("output"):
        raise HTTPException(status_code=409, detail="Video belum selesai disusun")
    try:
        from autoupload.config import UPLOAD_DIR
    except Exception as error:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail="Auto Upload tidak aktif di server ini"
        ) from error

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tujuan = UPLOAD_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.mp4"
    sumber = vt.job_path(job_id) / str(status["output"])
    if sumber.is_file():
        shutil.copyfile(sumber, tujuan)
    elif status.get("storage_path"):
        # Diambil memakai kunci server, bukan lewat URL. Tautan bertanda
        # tangan punya umur, sedangkan jalur di bucket tidak - jadi cara ini
        # tetap jalan untuk job yang sudah agak lama.
        import storage as st

        try:
            st.download(str(status["storage_path"]), tujuan)
        except Exception as error:  # noqa: BLE001
            tujuan.unlink(missing_ok=True)
            raise HTTPException(
                status_code=502, detail=f"Hasil render tidak bisa diambil: {error}"
            ) from error
    elif status.get("url"):
        import httpx

        try:
            with httpx.stream(
                "GET", str(status["url"]), timeout=300, follow_redirects=True
            ) as balasan:
                balasan.raise_for_status()
                with open(tujuan, "wb") as keluar:
                    for potongan in balasan.iter_bytes(1024 * 1024):
                        keluar.write(potongan)
        except Exception as error:  # noqa: BLE001
            tujuan.unlink(missing_ok=True)
            raise HTTPException(
                status_code=502, detail=f"Hasil render tidak bisa diambil: {error}"
            ) from error
    else:
        raise HTTPException(status_code=404, detail="Berkas hasil sudah tidak ada")

    return {
        "media_path": str(tujuan),
        "size_bytes": tujuan.stat().st_size,
        "media_kind": "video",
    }


@router.post("/jobs/stop")
def hentikan_job(
    pengguna: dict[str, Any] | None = Depends(identitas),
) -> dict[str, Any]:
    """Hentikan semua pembuatan video milik pemanggil yang belum selesai.

    Satu tombol untuk semuanya: yang masih menunggu giliran tidak akan pernah
    mulai, dan yang sedang berjalan dihentikan di tengah jalan. Job milik
    orang lain tidak tersentuh.
    """
    pemilik = _nama_akun(pengguna) or ""
    belum = [
        j
        for j in vt.daftar_job(500, owner=pemilik)
        if j.get("status") in ("queued", "downloading", "rendering")
    ]
    for j in belum:
        vt.minta_batal(str(j["job_id"]))
        # Yang masih mengantre dicegat lebih awal supaya tidak membuang waktu
        # worker sama sekali. terminate sengaja tidak dipakai: worker memakai
        # kumpulan utas, dan utas tidak bisa ditembak dari luar.
        tugas = j.get("task_id")
        if tugas:
            try:
                vt.celery_app.control.revoke(str(tugas))
            except Exception:  # noqa: BLE001
                logger.warning("Gagal mencabut tugas %s", tugas)
        # Yang belum mulai bisa langsung ditandai; yang sedang jalan akan
        # menandai dirinya sendiri begitu ffmpeg berhenti.
        if j.get("status") == "queued":
            vt.tulis_status(
                str(j["job_id"]), status="dibatalkan", log="Dihentikan sebelum mulai."
            )
    return {"dihentikan": len(belum)}


class BersihkanBody(BaseModel):
    """Daftar job yang hasil videonya sudah tidak dipakai."""

    job_ids: list[str] = Field(default_factory=list, max_length=500)
    # sendBeacon tidak bisa mengirim header, jadi tamu menitipkan idnya di sini.
    guest_id: str = ""


@router.delete("/jobs/{job_id}")
def hapus_job(
    job_id: str, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, bool]:
    _job_terjangkau(job_id, pengguna)
    vt.buang_job(job_id)
    shutil.rmtree(vt.job_path(job_id), ignore_errors=True)
    return {"ok": True}


@router.post("/jobs/cleanup")
def bersihkan_job(
    body: BersihkanBody, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict[str, int]:
    """Buang hasil render yang sudah tidak ditampilkan lagi.

    Dipakai halaman saat memulai render baru atau saat ditinggalkan. Memakai
    POST, bukan DELETE, supaya bisa dikirim lewat sendBeacon ketika tab
    ditutup — permintaan biasa sering keburu dibatalkan peramban.
    """
    if not pengguna and body.guest_id:
        tamu = bersihkan_id_tamu(body.guest_id)
        if tamu:
            pengguna = {"user_id": tamu, "username": tamu, "tamu": True}
    dihapus = 0
    for job_id in body.job_ids[:500]:
        job_id = str(job_id).strip()
        if not job_id:
            continue
        try:
            _job_terjangkau(job_id, pengguna)
        except HTTPException:
            continue  # bukan miliknya, atau sudah tidak ada
        vt.buang_job(job_id)
        shutil.rmtree(vt.job_path(job_id), ignore_errors=True)
        dihapus += 1
    return {"dihapus": dihapus}
