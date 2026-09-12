"""API auto edit video: kelola template dan jalankan job render."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

import storage
import video_edit as ve
import video_tasks as vt

router = APIRouter(prefix="/api/video", tags=["video"])

# Render memakan CPU penuh, jadi jumlah job yang boleh menunggu dibatasi.
MAX_ACTIVE_JOBS = max(1, int(os.getenv("VIDEO_MAX_ACTIVE_JOBS", "2")))
MAX_ASSET_MB = float(os.getenv("VIDEO_MAX_ASSET_MB", "200"))
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


def _tangani(error: ve.VideoError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


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
