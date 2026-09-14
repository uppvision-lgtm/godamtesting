"""API auto edit video: kelola template dan jalankan job render."""

from __future__ import annotations

import logging
import os
import subprocess
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

import storage
import video_edit as ve
import video_hook as vh
import video_tasks as vt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/video", tags=["video"])

# Render memakan CPU penuh, jadi jumlah job yang boleh menunggu dibatasi.
MAX_ACTIVE_JOBS = max(1, int(os.getenv("VIDEO_MAX_ACTIVE_JOBS", "2")))
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


class JobBody(BaseModel):
    url: str = Field(min_length=5, max_length=2000)
    template_id: str = Field(min_length=1, max_length=80)
    texts: dict[str, str] = {}
    # Opsional: dipakai kalau link sumbernya dari Instagram (butuh cookie).
    session_id: str = ""


class PreviewBody(BaseModel):
    """Link video yang mau dilihat isinya sebelum diproses."""

    url: str = Field(min_length=5, max_length=2000)
    session_id: str = ""


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


# ============================================================
#  PRATINJAU LINK
# ============================================================


@router.post("/preview")
def pratinjau_link(body: PreviewBody) -> dict[str, Any]:
    """Tampilkan isi link (judul, durasi, sampul) tanpa mengunduh videonya."""
    try:
        return ve.preview_source(body.url, body.session_id or None)
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
def daftar_template() -> dict[str, Any]:
    return {"templates": ve.list_templates()}


@router.post("/templates")
def buat_template(body: TemplateBody, template_id: str | None = None) -> dict[str, Any]:
    try:
        return ve.save_template(body.model_dump(), template_id)
    except ve.VideoError as error:
        raise _tangani(error) from error


@router.get("/templates/{template_id}")
def ambil_template(template_id: str) -> dict[str, Any]:
    try:
        data = ve.load_template(template_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    aset = sorted(
        p.name for p in (ve.template_path(template_id) / "assets").glob("*") if p.is_file()
    )
    return {**data, "assets": aset}


@router.delete("/templates/{template_id}")
def hapus_template(template_id: str) -> dict[str, bool]:
    ve.delete_template(template_id)
    return {"ok": True}


@router.post("/templates/{template_id}/assets")
async def unggah_aset(template_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    """Terima satu berkas layer (PNG overlay, intro, atau outro)."""
    try:
        ve.load_template(template_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

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

    if storage.enabled():
        try:
            storage.upload(f"templates/{ve._aman(template_id)}/assets/{nama}", tujuan)
        except storage.StorageError as error:
            raise HTTPException(
                status_code=502, detail=f"Aset gagal disimpan ke Supabase: {error}"
            ) from error
    return {"file": f"assets/{nama}", "size": ukuran}


@router.delete("/templates/{template_id}/assets/{nama}")
def hapus_aset(template_id: str, nama: str) -> dict[str, bool]:
    berkas = ve.template_path(template_id) / "assets" / ve._aman(nama)
    berkas.unlink(missing_ok=True)
    return {"ok": True}


# ============================================================
#  JOB RENDER
# ============================================================


@router.post("/jobs")
def mulai_job(body: JobBody) -> dict[str, Any]:
    try:
        ve.load_template(body.template_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    aktif = [j for j in vt.daftar_job(50) if j.get("status") in STATUS_AKTIF]
    if len(aktif) >= MAX_ACTIVE_JOBS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Sedang ada {len(aktif)} job video berjalan (maksimal {MAX_ACTIVE_JOBS}). "
                "Tunggu sampai selesai lalu coba lagi."
            ),
        )

    job_id = vt.buat_job(body.url.strip(), body.template_id, body.texts)
    vt.render_video.delay(
        job_id, body.url.strip(), body.template_id, body.texts, body.session_id.strip()
    )
    return {"job_id": job_id, "status": "queued"}


@router.get("/jobs")
def daftar_job(limit: int = 20) -> dict[str, Any]:
    return {"jobs": vt.daftar_job(max(1, min(100, limit)))}


@router.get("/jobs/{job_id}")
def status_job(job_id: str) -> dict[str, Any]:
    try:
        return vt.baca_status(job_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/jobs/{job_id}/file")
def berkas_job(job_id: str) -> FileResponse:
    try:
        status = vt.baca_status(job_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if not status.get("output"):
        raise HTTPException(status_code=409, detail="Video belum selesai disusun")
    berkas = vt.job_path(job_id) / str(status["output"])
    if not berkas.is_file():
        # Cache lokal sudah dibersihkan; ambil lagi dari Supabase.
        if status.get("url"):
            return RedirectResponse(str(status["url"]))
        raise HTTPException(status_code=404, detail="Berkas hasil sudah tidak ada")
    return FileResponse(berkas, media_type="video/mp4", filename=f"{job_id}.mp4")


@router.delete("/jobs/{job_id}")
def hapus_job(job_id: str) -> dict[str, bool]:
    shutil.rmtree(vt.job_path(job_id), ignore_errors=True)
    return {"ok": True}
