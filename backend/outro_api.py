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
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

import outro
from auth_api import developer_wajib

router = APIRouter(prefix="/api/outro", tags=["outro"])


class OutroBody(BaseModel):
    channel: str = Field(min_length=1, max_length=80)
    akun: dict[str, str] = Field(default_factory=dict)
    # Seed gaya. Kosong = acak (tiap outro beda); diisi = mengulang tampilan
    # yang sama persis, mis. untuk membuat ulang yang disukai.
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    # "biasa": gaya acak bersumbu (bawaan). "dpp": meniru outro TV Rakyat
    # apa adanya - nama channel ditulis melengkung di logo, sosmed dari isian.
    mode: Literal["biasa", "dpp"] = "biasa"


@router.get("/jobs")
def daftar(_: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    return {"jobs": [_ringkas(j) for j in outro.daftar_job()]}


@router.post("/jobs")
def buat(body: OutroBody, _: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    try:
        job_id = outro.mulai_job(body.channel, body.akun, body.seed, mode=body.mode)
    except outro.OutroError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    job = outro.baca_job(job_id) or {}
    return {"job_id": job_id, "status": "queued", "mode": body.mode,
            "seed": job.get("seed"), "gaya": job.get("gaya", "")}


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
def video(job_id: str, _: dict[str, Any] = Depends(developer_wajib)) -> Response:
    job = outro.baca_job(job_id)
    if job is None or not (job.get("video") or job.get("storage_path")):
        raise HTTPException(status_code=404, detail="Videonya belum ada.")
    # Hasil kini tinggal di Supabase; salinan di disk hanya ada kalau
    # pengunggahannya gagal.
    ambil = outro.ambil_video(job_id)
    if ambil is None:
        raise HTTPException(status_code=404, detail="Berkas videonya sudah tidak ada.")
    isi, channel = ambil
    nama = "outro-" + "".join(
        c for c in channel if c.isalnum() or c in "-_ "
    ).strip().replace(" ", "-")
    # Header HTTP hanya aman untuk ASCII; nama channel bisa berisi huruf lain.
    nama = nama.encode("ascii", "ignore").decode() or "outro"
    nama_berkas = f"{nama}.mp4"
    if isinstance(isi, Path):
        return FileResponse(str(isi), media_type="video/mp4", filename=nama_berkas)
    return Response(
        content=isi,
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{nama_berkas}"'},
    )


def _ringkas(job: dict[str, Any]) -> dict[str, Any]:
    """Yang perlu halaman: jalur berkas di disk tidak ikut dikirim."""
    return {
        "job_id": job["job_id"],
        "status": job.get("status"),
        "langkah": job.get("langkah"),
        "progress": job.get("progress", 0),
        "channel": job.get("channel"),
        "mode": job.get("mode", "biasa"),
        "seed": job.get("seed"),
        "gaya": job.get("gaya", ""),
        "akun": job.get("akun", {}),
        "punya_video": bool(job.get("video") or job.get("storage_path")),
        "message": job.get("message", ""),
        "logs": job.get("logs", []),
        "created": job.get("created"),
        "updated": job.get("updated"),
    }
