"""API Auto Outro - khusus developer, khusus backend lokal.

Dua kelompok endpoint:

- /api/outro/sesi/...  : status dan kendali login peramban Google Flow
  (jendela dibuka, developer login sendiri, lalu "Selesai"). Penulis prompt
  memakai API, jadi tidak ada sesi yang perlu disiapkan untuknya.
- /api/outro/jobs/...  : menjalankan pembuatan outro, memantau, menghentikan,
  dan mengambil videonya.

Semuanya di balik developer_wajib. Di server (bukan mode lokal) seluruhnya
ditolak: tidak ada layar untuk jendela login, memorinya tidak cukup, dan
login Google dari pusat data hampir pasti ditolak.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import outro
from auth_api import developer_wajib

router = APIRouter(prefix="/api/outro", tags=["outro"])

# Hanya Google Flow yang butuh sesi peramban. Penulis promptnya lewat API,
# jadi tidak ada yang perlu di-login untuk langkah pertama.
PLATFORM = ("flow",)
LOKAL = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")


def _hanya_lokal() -> None:
    if not LOKAL:
        raise HTTPException(
            status_code=400,
            detail=(
                "Auto Outro hanya berjalan di backend lokal (mode via PC): butuh "
                "jendela peramban di layar dan memori yang tidak dimiliki server."
            ),
        )


def _platform(nama: str) -> str:
    if nama not in PLATFORM:
        raise HTTPException(status_code=404, detail="Platform tidak dikenal.")
    return nama


class OutroBody(BaseModel):
    channel: str = Field(min_length=1, max_length=80)
    akun: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------- sesi login

@router.get("/sesi")
def status_sesi(_: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    from autoupload import login

    return {
        "lokal": LOKAL,
        "chrome": {
            "tersambung": outro.cdp_hidup(),
            "chrome_jalan": outro.chrome_pengguna_jalan(),
            "alamat": outro.CDP_URL,
        },
        "sesi": {p: login.status(p) for p in PLATFORM},
    }


@router.post("/sesi/{platform}/mulai")
def mulai_login(platform: str, _: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    _hanya_lokal()
    from autoupload import login

    hasil = login.start(_platform(platform))
    if not hasil.get("ok"):
        raise HTTPException(status_code=400, detail=hasil.get("error", "Gagal membuka peramban."))
    return login.status(platform)


@router.post("/sesi/{platform}/selesai")
def selesai_login(platform: str, _: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    _hanya_lokal()
    from autoupload import login

    hasil = login.finish(_platform(platform))
    return {**login.status(platform), "ok": hasil.get("ok", False)}


@router.post("/sesi/{platform}/batal")
def batal_login(platform: str, _: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    _hanya_lokal()
    from autoupload import login

    login.cancel(_platform(platform))
    return login.status(platform)


@router.delete("/sesi/{platform}")
def hapus_sesi(platform: str, _: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    _hanya_lokal()
    from autoupload import login

    return login.delete_session(_platform(platform), "default")


@router.post("/chrome")
def nyalakan_chrome(
    tutup_dulu: bool = False, _: dict[str, Any] = Depends(developer_wajib)
) -> dict[str, Any]:
    """Jalankan Chrome pengguna dengan port debug supaya Flow bisa dipakai.

    Google menolak login dari peramban yang dikendalikan otomasi, jadi tidak
    ada yang login lewat alat ini. Yang dilakukan justru sebaliknya: menumpang
    sesi yang sudah ada di Chrome milik orangnya sendiri.

    ``tutup_dulu`` menutup Chrome yang sedang terbuka. Itu menutup tab-tab
    orang, jadi tidak pernah dilakukan diam-diam - halaman harus memintanya
    secara tegas setelah memberi tahu.
    """
    _hanya_lokal()
    hasil = outro.nyalakan_chrome_debug(tutup_dulu=tutup_dulu)
    return {**hasil, "tersambung": outro.cdp_hidup()}


# ---------------------------------------------------------------- pekerjaan

@router.get("/jobs")
def daftar(_: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    return {"jobs": [_ringkas(j) for j in outro.daftar_job()]}


@router.post("/jobs")
def buat(body: OutroBody, _: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    _hanya_lokal()
    if not outro.punya_sesi("flow"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Google Flow belum siap. Tekan \"Siapkan Chrome\" di atas supaya "
                "Chrome-mu yang sudah masuk bisa dipakai."
            ),
        )
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
    nama = "outro-" + "".join(c for c in job.get("channel", "outro") if c.isalnum() or c in "-_ ").strip().replace(" ", "-")
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
        "prompt": job.get("prompt", ""),
        "punya_video": bool(job.get("video")),
        "message": job.get("message", ""),
        "logs": job.get("logs", []),
        "created": job.get("created"),
        "updated": job.get("updated"),
    }
