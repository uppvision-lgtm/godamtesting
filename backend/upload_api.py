"""API auto upload Instagram: terima berkas dari laptop lalu unggah ke akun.

Berkasnya bisa sampai ke backend lewat dua jalan:

1. **Langsung ke Supabase** (dipakai kalau Supabase aktif). Browser meminta URL
   unggah sekali pakai, mengirim berkasnya sendiri ke Supabase, lalu backend
   mengunduhnya. Ini menghindari batas 4,5 MB badan permintaan Vercel.
2. **Multipart biasa** ke backend, untuk mode via PC atau berkas kecil.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

import storage
from live_session import (
    MAX_CONCURRENT_SESSIONS,
    _SESSIONS,
    LiveSession,
    active_session_count,
)
from video_edit import MEDIA_DIR

router = APIRouter(prefix="/api/upload", tags=["upload"])

JENIS_VIDEO = {".mp4", ".mov", ".m4v", ".webm"}
MAX_VIDEO_MB = float(os.getenv("UPLOAD_MAX_VIDEO_MB", "300"))
MAX_CAPTION = 2200  # batas caption Instagram


class StartBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    session_id: str = Field(min_length=1, max_length=2000)
    caption: str = Field(default="", max_length=MAX_CAPTION)
    # Salah satu dari dua ini wajib diisi.
    file_id: str = ""
    storage_path: str = ""


def uploads_dir() -> Path:
    path = MEDIA_DIR / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _aman(nama: str) -> str:
    bersih = "".join(c for c in str(nama) if c.isalnum() or c in "._- ")
    return (bersih.strip() or "video.mp4")[:100]


def _periksa_jenis(nama: str) -> None:
    if Path(nama).suffix.lower() not in JENIS_VIDEO:
        raise HTTPException(
            status_code=415,
            detail=f"Hanya video yang bisa diunggah: {', '.join(sorted(JENIS_VIDEO))}",
        )


@router.get("/config")
def konfigurasi() -> dict[str, object]:
    """Beri tahu frontend jalur unggah mana yang tersedia."""
    return {
        "supabase": storage.enabled(),
        "max_video_mb": MAX_VIDEO_MB,
        "jenis": sorted(JENIS_VIDEO),
    }


@router.post("/signed")
def url_unggah(nama: str) -> dict[str, str]:
    """URL sekali pakai supaya browser mengunggah langsung ke Supabase."""
    if not storage.enabled():
        raise HTTPException(status_code=409, detail="Supabase belum dikonfigurasi")
    _periksa_jenis(nama)
    jalur = f"uploads/{uuid.uuid4().hex[:12]}/{_aman(nama)}"
    try:
        storage.ensure_bucket()
        return storage.signed_upload(jalur)
    except storage.StorageError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/file")
async def terima_berkas(file: UploadFile = File(...)) -> dict[str, object]:
    """Terima berkas video langsung (mode via PC / berkas kecil)."""
    nama = _aman(Path(file.filename or "video.mp4").name)
    _periksa_jenis(nama)
    file_id = uuid.uuid4().hex[:12]
    folder = uploads_dir() / file_id
    folder.mkdir(parents=True, exist_ok=True)
    tujuan = folder / nama
    batas = int(MAX_VIDEO_MB * 1_048_576)
    ukuran = 0
    try:
        with tujuan.open("wb") as keluar:
            while potongan := await file.read(1_048_576):
                ukuran += len(potongan)
                if ukuran > batas:
                    raise HTTPException(
                        status_code=413, detail=f"Video melebihi {MAX_VIDEO_MB:.0f} MB"
                    )
                keluar.write(potongan)
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {"file_id": file_id, "name": nama, "size": ukuran}


def _siapkan_berkas(body: StartBody) -> Path:
    """Pastikan videonya ada sebagai berkas nyata di disk sebelum Playwright jalan."""
    if body.file_id:
        folder = uploads_dir() / _aman(body.file_id)
        berkas = next((p for p in folder.glob("*") if p.is_file()), None)
        if berkas is None:
            raise HTTPException(status_code=404, detail="Berkas unggahan tidak ditemukan")
        return berkas

    if body.storage_path:
        if not storage.enabled():
            raise HTTPException(status_code=409, detail="Supabase belum dikonfigurasi")
        nama = _aman(Path(body.storage_path).name)
        _periksa_jenis(nama)
        tujuan = uploads_dir() / uuid.uuid4().hex[:12] / nama
        try:
            storage.download(body.storage_path, tujuan)
        except storage.StorageError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return tujuan

    raise HTTPException(status_code=422, detail="Pilih berkas videonya dulu")


@router.post("/start")
async def mulai_unggah(body: StartBody) -> dict[str, object]:
    berkas = _siapkan_berkas(body)

    # MAX_CONCURRENT_SESSIONS bernilai 0 berarti tanpa batas.
    if MAX_CONCURRENT_SESSIONS and active_session_count() >= MAX_CONCURRENT_SESSIONS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Maksimal {MAX_CONCURRENT_SESSIONS} browser berjalan bersamaan. "
                "Tunggu salah satu selesai lalu coba lagi."
            ),
        )

    token = uuid.uuid4().hex
    sesi = LiveSession(token)
    _SESSIONS[token] = sesi
    await sesi.start_upload(
        body.username.strip(), body.session_id.strip(), str(berkas), body.caption
    )
    return {"token": token, "width": sesi.width, "height": sesi.height}
