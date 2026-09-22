"""API Auto Outro - khusus developer.

Alurnya lewat API sepenuhnya (AI penulis prompt + Hugging Face ZeroGPU +
renderer teks sendiri), jadi tidak ada login peramban dan tidak ada jendela
yang perlu dibuka. Endpoint-nya:

- /api/outro/jobs         : mulai, daftar
- /api/outro/jobs/{id}    : pantau
- /api/outro/jobs/{id}/stop  : hentikan
- /api/outro/jobs/{id}/video : unduh hasilnya

Semuanya di balik developer_wajib. ZeroGPU dipanggil dari mana pun backend
berjalan, jadi tidak lagi dibatasi mode lokal - yang membedakan hanya siapa
yang boleh membukanya.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import outro
from auth_api import developer_wajib

router = APIRouter(prefix="/api/outro", tags=["outro"])


class OutroBody(BaseModel):
    channel: str = Field(min_length=1, max_length=80)
    akun: dict[str, str] = Field(default_factory=dict)


@router.get("/jobs")
def daftar(_: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    return {"jobs": [_ringkas(j) for j in outro.daftar_job()]}


@router.post("/jobs")
def buat(body: OutroBody, _: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    try:
        job_id = outro.mulai_job(body.channel, body.akun)
    except outro.OutroError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"job_id": job_id, "status": "queued"}


@router.get("/jobs/{job_id}")
def status(job_id: str, _: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    job = outro.baca_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Pekerjaan tidak ditemukan.")
    return _ringkas(job)


@router.post("/jobs/{job_id}/stop")
def hentikan(job_id: str, _: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    if not outro.minta_batal(job_id):
        raise HTTPException(status_code=409, detail="Pekerjaan sudah selesai atau tidak ada.")
    return {"ok": True}


@router.get("/jobs/{job_id}/video")
def video(job_id: str, _: dict[str, Any] = Depends(developer_wajib)) -> FileResponse:
    job = outro.baca_job(job_id)
    if job is None or not job.get("video"):
        raise HTTPException(status_code=404, detail="Videonya belum ada.")
    berkas = Path(job["video"])
    if not berkas.is_file():
        raise HTTPException(status_code=404, detail="Berkas videonya sudah tidak ada.")
    nama = "outro-" + "".join(
        c for c in job.get("channel", "outro") if c.isalnum() or c in "-_ "
    ).strip().replace(" ", "-")
    return FileResponse(str(berkas), media_type="video/mp4", filename=f"{nama or 'outro'}.mp4")


def _ringkas(job: dict[str, Any]) -> dict[str, Any]:
    """Yang perlu halaman: jalur berkas di disk tidak ikut dikirim."""
    return {
        "job_id": job["job_id"],
        "status": job.get("status"),
        "langkah": job.get("langkah"),
        "progress": job.get("progress", 0),
        "channel": job.get("channel"),
        "akun": job.get("akun", {}),
        "punya_video": bool(job.get("video")),
        "message": job.get("message", ""),
        "logs": job.get("logs", []),
        "created": job.get("created"),
        "updated": job.get("updated"),
    }
