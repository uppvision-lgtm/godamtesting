"""Login sosmed di PERANGKAT PENGGUNA, dipanggil dari situs yang tayang.

Situs di Vercel tidak bisa membuka jendela peramban di komputer siapa pun,
dan server Railway tidak punya layar - padahal Instagram dan kawan-kawan
sering memasang captcha pada login otomatis. Jalan keluarnya: kalau
pengguna menjalankan backend ini di komputernya sendiri (mode lokal,
DEBUG=true), situs memanggil ``http://localhost:8000/api/perangkat/masuk``.
Backend lokal itulah yang membuka jendela Chrome TERLIHAT di layar
pengguna; captcha dikerjakan pengguna sendiri di jendela itu, lalu cookie
sesinya dikembalikan ke situs, yang menyimpannya ke akun pengguna di server.

Kenapa tanpa login situs: token situs ditandatangani kunci server dan tidak
berlaku di sini. Penjaganya diganti tiga hal:
- hanya aktif di mode lokal (di server rute ini menjawab 404),
- hanya melayani asal yang dikenal (situs godamtesting dan localhost),
- tidak ada data tersimpan yang bisa dibaca: yang dikembalikan hanyalah
  sesi dari username & password yang dikirim pemanggilnya sendiri.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/perangkat", tags=["perangkat"])

ASAL_BAWAAN = (
    "https://godamtesting.vercel.app",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def aktif() -> bool:
    return os.getenv("DEBUG", "").strip().lower() in ("1", "true", "yes")


def _asal_sah() -> set[str]:
    tambahan = [a.strip().rstrip("/") for a in os.getenv("PERANGKAT_ASAL", "").split(",") if a.strip()]
    return {*ASAL_BAWAAN, *tambahan}


def _jaga(request: Request) -> None:
    if not aktif():
        raise HTTPException(404, "Tidak tersedia.")
    asal = (request.headers.get("origin") or "").rstrip("/")
    # Tanpa Origin = dipanggil langsung (curl, skrip di mesin ini sendiri).
    # Dengan Origin, hanya situs yang dikenal: situs lain yang kebetulan
    # dibuka pengguna tidak boleh menyuruh komputernya membuka jendela login.
    if asal and asal not in _asal_sah():
        raise HTTPException(403, "Asal permintaan tidak dikenal.")


class MasukPerangkat(BaseModel):
    platform: str
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@router.get("/status")
def status(request: Request) -> dict:
    _jaga(request)
    return {"ok": True, "versi": 1}


@router.post("/masuk")
async def masuk(req: MasukPerangkat, request: Request) -> dict:
    """Buka jendela login TERLIHAT di perangkat ini; kembalikan sesinya."""
    _jaga(request)
    from autoupload import masuk as modul_masuk  # paket dimuat main.py

    try:
        hasil = await modul_masuk.masuk_dengan_sandi(
            req.platform, req.username, req.password, jendela=True
        )
    except modul_masuk.MasukGagal as error:
        raise HTTPException(400, str(error)) from None
    return {"session_id": hasil["session_id"], "nama_akun": hasil.get("nama_akun", "")}
