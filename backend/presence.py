"""Angka realtime untuk landing page: pengunjung online & bot yang berjalan.

Cara kerjanya **push, bukan polling**: tiap tab membuka satu koneksi
Server-Sent Events (``GET /api/presence/stream``) dan server mengirim angka
baru begitu ada perubahan (dicek tiap detik). Jadi:

- ``online``       = jumlah tab yang koneksinya masih hidup. Satu orang buka
  2 tab dihitung 2; begitu tab ditutup koneksinya putus dan angkanya turun
  sendiri tanpa menunggu timeout panjang.
- ``running_bots`` = jumlah Chromium bot yang benar-benar sedang berjalan
  (lihat ``LiveSession.is_browser_alive``), bukan sekadar job yang tercatat.

Endpoint ``/ping`` + ``/leave`` + ``/stats`` tetap ada sebagai cadangan untuk
browser/jaringan yang memblokir SSE.

Semua disimpan di memori proses (bukan Redis) supaya mode "via PC" — yang
tidak menjalankan Redis — tetap bisa memakainya.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from live_session import active_session_count

router = APIRouter(prefix="/api/presence", tags=["presence"])

# Seberapa sering server memeriksa perubahan angka (detik). Ini murah karena
# hanya membaca dua dict di memori, tidak menyentuh jaringan/Redis.
TICK_SECONDS = 1.0

# Komentar ": ping" dikirim berkala supaya koneksi tidak dianggap mati oleh
# proxy walau angkanya tidak berubah-ubah.
HEARTBEAT_SECONDS = 15.0

# Stream ditutup rapi sebelum batas durasi fungsi serverless Vercel (60 detik),
# lalu browser menyambung ulang sendiri. Di server sendiri boleh dibuat besar.
STREAM_MAX_SECONDS = max(5.0, float(os.getenv("PRESENCE_STREAM_MAX_SECONDS", "55")))

# Tenggang setelah koneksi putus. Gunanya supaya penyambungan ulang tiap
# STREAM_MAX_SECONDS tidak membuat angkanya berkedip turun-naik. Orang yang
# benar-benar menutup tab hilang dari hitungan setelah tenggang sependek ini.
GRACE_SECONDS = max(0.0, float(os.getenv("PRESENCE_GRACE_SECONDS", "5")))

# Umur satu ping pada jalur cadangan (tanpa SSE).
PING_TTL_SECONDS = max(10.0, float(os.getenv("PRESENCE_TTL_SECONDS", "45")))

# connection_id -> visitor_id (satu stream SSE yang sedang terbuka)
_streams: dict[str, str] = {}
# visitor_id -> batas waktu (monotonic) pengunjung masih dianggap online
_recent: dict[str, float] = {}


class PingRequest(BaseModel):
    visitor_id: str = Field(min_length=1, max_length=100)


async def _ping_longgar(request: Request) -> PingRequest:
    """Badan JSON apa pun tipe kontennya.

    Frontend kini statis dan beda asal dengan backend. sendBeacon lintas-asal
    hanya boleh tanpa Content-Type atau text/plain (application/json butuh
    preflight dan ditolak peramban), sedangkan FastAPI hanya membaca JSON dari
    permintaan bertipe application/json.
    """
    try:
        return PingRequest.model_validate(json.loads(await request.body() or b"{}"))
    except Exception as error:  # noqa: BLE001 - badan rusak = permintaan tidak sah
        raise HTTPException(status_code=422, detail="visitor_id wajib ada.") from error


def _purge_expired(now: float) -> None:
    for visitor_id, deadline in list(_recent.items()):
        if deadline <= now:
            _recent.pop(visitor_id, None)


def _stats() -> dict[str, int]:
    """Angka terbaru. ``online`` unik per tab, jadi stream + ping tidak dobel."""
    _purge_expired(time.monotonic())
    return {
        "online": len(set(_streams.values()) | set(_recent)),
        "running_bots": active_session_count(),
    }


async def _event_stream(request: Request, visitor_id: str) -> AsyncIterator[str]:
    connection_id = uuid.uuid4().hex
    _streams[connection_id] = visitor_id
    # Tab ini sekarang punya koneksi hidup, jadi catatan tenggangnya tidak perlu.
    _recent.pop(visitor_id, None)
    try:
        # Sambung ulang cepat (1 detik) kalau koneksinya putus.
        yield "retry: 1000\n\n"
        terakhir: dict[str, int] | None = None
        mulai = time.monotonic()
        heartbeat = mulai
        while True:
            if await request.is_disconnected():
                break
            sekarang = time.monotonic()
            data = _stats()
            if data != terakhir:
                terakhir = data
                heartbeat = sekarang
                yield f"data: {json.dumps(data)}\n\n"
            elif sekarang - heartbeat >= HEARTBEAT_SECONDS:
                heartbeat = sekarang
                yield ": ping\n\n"
            if sekarang - mulai >= STREAM_MAX_SECONDS:
                break
            await asyncio.sleep(TICK_SECONDS)
    finally:
        _streams.pop(connection_id, None)
        if GRACE_SECONDS > 0:
            _recent[visitor_id] = time.monotonic() + GRACE_SECONDS


@router.get("/stream")
async def stream(
    request: Request,
    v: str = Query(min_length=1, max_length=100, description="ID unik per tab"),
) -> StreamingResponse:
    """Kirim angka terbaru terus-menerus selama tab masih terbuka."""
    return StreamingResponse(
        _event_stream(request, v.strip()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Matikan buffering proxy (nginx dsb) supaya tiap baris langsung sampai.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/ping")
async def ping(request: PingRequest) -> dict[str, int]:
    """Jalur cadangan tanpa SSE: tandai tab masih terbuka, balas angka terbaru."""
    _recent[request.visitor_id.strip()] = time.monotonic() + PING_TTL_SECONDS
    return _stats()


@router.post("/leave")
async def leave(request: PingRequest = Depends(_ping_longgar)) -> dict[str, int]:
    """Dipanggil saat tab ditutup supaya angkanya langsung turun."""
    _recent.pop(request.visitor_id.strip(), None)
    return _stats()


@router.get("/stats")
async def stats() -> dict[str, int]:
    """Angka terbaru sekali ambil, tanpa ikut terhitung sebagai pengunjung."""
    return _stats()
