"""Isian formulir tersimpan di server, supaya tidak perlu diketik ulang.

Dipakai halaman Auto Comment, Auto Upload, dan Edit Video. Hanya untuk akun
yang sudah masuk: tamu menyimpan isiannya di peramban saja, dan itu memang
hilang begitu tab ditutup.

Isinya DIENKRIPSI sebelum masuk database. Alasannya Session ID Instagram ikut
di dalam formulir, dan itu kredensial penuh atas akun orang. Yang tersimpan di
tabel hanyalah satu untai Fernet; tanpa CREDENTIAL_ENCRYPTION_KEY, isinya tidak
bisa dibaca siapa pun yang melihat database.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import db
from auth_api import pengguna_opsional

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/forms", tags=["forms"])

TABEL = "app_forms"

# Halaman yang boleh menyimpan. Daftar tertutup supaya nama halaman dari
# permintaan tidak bisa dipakai membuat baris sembarangan.
#
# "auto-comment-threads" sempat tertinggal: halaman Threads dibuat belakangan
# dengan kunci simpannya sendiri, tapi tidak pernah didaftarkan di sini.
# Akibatnya tiap membuka halaman itu server menjawab 404, dan pengaturan bot
# Threads hanya hidup di peramban yang sedang dipakai - hilang begitu ganti
# perangkat atau data situs dibersihkan, padahal versi Instagram tersimpan.
HALAMAN_SAH = {"auto-comment", "auto-comment-threads", "auto-upload", "edit-video"}

# Batas ukuran isian setelah dijadikan JSON. Halaman Auto Comment bisa memuat
# puluhan bot, tetapi tidak ada alasan sah untuk melewati angka ini.
MAKS_BYTE = int(os.getenv("FORM_MAX_BYTES", str(256 * 1024)))


class IsianBody(BaseModel):
    isi: Any = Field(default=None)


def _kunci() -> Fernet:
    kunci = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not kunci:
        # 503, bukan 500: ini soal pemasangan server, dan halaman menanganinya
        # dengan diam-diam kembali menyimpan di peramban saja.
        raise HTTPException(
            status_code=503,
            detail="Penyimpanan isian belum aktif: CREDENTIAL_ENCRYPTION_KEY belum diisi.",
        )
    try:
        return Fernet(kunci.encode())
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=503, detail="CREDENTIAL_ENCRYPTION_KEY tidak sah."
        ) from error


def _akun(pengguna: dict[str, Any] | None) -> str:
    """Nama akun yang boleh menyimpan. Tamu dan yang belum masuk ditolak."""
    if not pengguna:
        raise HTTPException(status_code=401, detail="Masuk dulu untuk menyimpan isian.")
    return str(pengguna["username"])


def _halaman(nama: str) -> str:
    if nama not in HALAMAN_SAH:
        raise HTTPException(status_code=404, detail=f"Halaman '{nama}' tidak dikenal.")
    return nama


def _pastikan_db() -> None:
    if not db.enabled():
        raise HTTPException(
            status_code=503, detail="Penyimpanan isian belum aktif: database belum dikonfigurasi."
        )


@router.get("/{halaman}")
def baca(
    halaman: str, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, Any]:
    """Isian tersimpan milik akun ini, atau null kalau belum pernah menyimpan."""
    akun = _akun(pengguna)
    halaman = _halaman(halaman)
    _pastikan_db()
    try:
        baris = db.select(
            TABEL,
            {
                "username": f"eq.{akun}",
                "halaman": f"eq.{halaman}",
                "select": "isi",
                "limit": "1",
            },
        )
    except db.DbError as error:
        logger.warning("Gagal membaca isian %s/%s: %s", akun, halaman, error)
        raise HTTPException(status_code=502, detail=f"Gagal membaca isian: {error}") from error
    if not baris:
        return {"isi": None}
    try:
        mentah = _kunci().decrypt(str(baris[0].get("isi") or "").encode())
        return {"isi": json.loads(mentah)}
    except (InvalidToken, ValueError, json.JSONDecodeError):
        # Kunci enkripsi berganti, atau barisnya rusak. Diperlakukan seperti
        # belum pernah menyimpan; halaman akan menimpanya saat diketik lagi.
        logger.warning("Isian %s/%s tidak bisa dibaca, dianggap kosong", akun, halaman)
        return {"isi": None}


@router.put("/{halaman}")
def simpan(
    halaman: str,
    body: IsianBody,
    pengguna: dict[str, Any] | None = Depends(pengguna_opsional),
) -> dict[str, bool]:
    akun = _akun(pengguna)
    halaman = _halaman(halaman)
    _pastikan_db()
    try:
        mentah = json.dumps(body.isi, separators=(",", ":"), ensure_ascii=False).encode()
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="Isian tidak bisa disimpan.") from error
    if len(mentah) > MAKS_BYTE:
        raise HTTPException(
            status_code=413, detail=f"Isian terlalu besar (batas {MAKS_BYTE // 1024} KB)."
        )
    sandi = _kunci().encrypt(mentah).decode()
    try:
        db.upsert(
            TABEL,
            {
                "username": akun,
                "halaman": halaman,
                "isi": sandi,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="username,halaman",
        )
    except db.DbError as error:
        logger.warning("Gagal menyimpan isian %s/%s: %s", akun, halaman, error)
        raise HTTPException(status_code=502, detail=f"Gagal menyimpan isian: {error}") from error
    return {"ok": True}


@router.delete("/{halaman}")
def hapus(
    halaman: str, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, bool]:
    akun = _akun(pengguna)
    halaman = _halaman(halaman)
    _pastikan_db()
    try:
        db.delete(TABEL, {"username": f"eq.{akun}", "halaman": f"eq.{halaman}"})
    except db.DbError as error:
        raise HTTPException(status_code=502, detail=f"Gagal menghapus isian: {error}") from error
    return {"ok": True}
