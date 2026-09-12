"""Akses tabel Supabase lewat PostgREST memakai service key.

Dipakai modul login. Konfigurasinya menumpang ``storage.py`` supaya hanya ada
satu tempat kredensial Supabase.
"""

from __future__ import annotations

from typing import Any

import httpx

from storage import SUPABASE_SERVICE_KEY, SUPABASE_URL, enabled

TIMEOUT = httpx.Timeout(20.0, connect=10.0)

__all__ = ["enabled", "select", "insert", "update", "DbError"]


class DbError(RuntimeError):
    """Kegagalan saat berbicara dengan database Supabase."""


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    headers.update(extra or {})
    return headers


def _url(tabel: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{tabel}"


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
