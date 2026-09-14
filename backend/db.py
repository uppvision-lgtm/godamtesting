"""Akses tabel lewat PostgREST.

Ada dua kemungkinan sumber data, ditentukan dari environment:

- **Supabase** (dipakai sandbox Vercel + Railway): cukup isi ``SUPABASE_URL``
  dan ``SUPABASE_SERVICE_KEY``. Basis REST-nya otomatis ``<url>/rest/v1``.
- **PostgREST sendiri** (dipakai VPS yang berdiri sendiri): isi ``DB_REST_URL``
  menunjuk langsung ke PostgREST, misalnya ``http://postgrest:3000``. Di dalam
  jaringan Docker yang tertutup, ``DB_SERVICE_KEY`` boleh dikosongkan.

Sengaja dipisah dari ``storage.py``: VPS memakai database sendiri tetapi
menyimpan berkas di disknya sendiri, jadi ia butuh database tanpa ikut
menyalakan Supabase Storage.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from storage import SUPABASE_SERVICE_KEY, SUPABASE_URL

TIMEOUT = httpx.Timeout(20.0, connect=10.0)

__all__ = ["enabled", "select", "insert", "update", "DbError"]

REST_URL = os.getenv("DB_REST_URL", "").strip().rstrip("/")
SERVICE_KEY = os.getenv("DB_SERVICE_KEY", "").strip()

# Tanpa DB_REST_URL, ikut kredensial Supabase supaya pemasangan lama tetap
# jalan tanpa mengubah apa pun.
if not REST_URL and SUPABASE_URL and SUPABASE_SERVICE_KEY:
    REST_URL = f"{SUPABASE_URL}/rest/v1"
    SERVICE_KEY = SERVICE_KEY or SUPABASE_SERVICE_KEY


class DbError(RuntimeError):
    """Kegagalan saat berbicara dengan database."""


def enabled() -> bool:
    return bool(REST_URL)


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    # PostgREST milik sendiri di jaringan Docker tertutup tidak memakai kunci;
    # mengirim Authorization kosong justru ditolak sebagai JWT tidak sah.
    if SERVICE_KEY:
        headers["apikey"] = SERVICE_KEY
        headers["Authorization"] = f"Bearer {SERVICE_KEY}"
    headers.update(extra or {})
    return headers


def _url(tabel: str) -> str:
    return f"{REST_URL}/{tabel}"


def _periksa(respons: httpx.Response, aksi: str) -> Any:
    if respons.status_code >= 400:
        raise DbError(f"{aksi} gagal ({respons.status_code}): {respons.text[:200]}")
    if not respons.content:
        return None
    return respons.json()


def select(tabel: str, params: dict[str, str]) -> list[dict[str, Any]]:
    with httpx.Client(timeout=TIMEOUT) as client:
        respons = client.get(_url(tabel), headers=_headers(), params=params)
    return _periksa(respons, f"Membaca {tabel}") or []


def insert(tabel: str, data: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=TIMEOUT) as client:
        respons = client.post(
            _url(tabel), headers=_headers({"Prefer": "return=representation"}), json=data
        )
    hasil = _periksa(respons, f"Menyimpan ke {tabel}") or []
    return hasil[0] if hasil else {}


def update(tabel: str, params: dict[str, str], data: dict[str, Any]) -> None:
    with httpx.Client(timeout=TIMEOUT) as client:
        respons = client.patch(_url(tabel), headers=_headers(), params=params, json=data)
    _periksa(respons, f"Memperbarui {tabel}")
