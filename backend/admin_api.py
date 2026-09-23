"""Halaman pemantauan untuk akun developer.

Hanya bisa dibuka akun berperan ``developer``. Isinya ringkasan tiap akun:
kapan dibuat, kapan terakhir masuk, berapa template, dan berapa job render.

Yang TIDAK pernah ikut: hash password, isian formulir, dan Session ID
Instagram. Memantau berarti melihat pemakaian, bukan membuka isi akun orang.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

import db
import video_edit as ve
import video_tasks as vt
from auth_api import PERAN_DEVELOPER, TABEL, developer_wajib

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

PERAN_SAH = ("pengguna", PERAN_DEVELOPER)


def _hitung_template() -> tuple[Counter[str], int]:
    """Jumlah template per pemilik, plus jumlah template milik tamu."""
    per_akun: Counter[str] = Counter()
    tamu = 0
    for folder in ve.templates_dir().iterdir():
        isi = ve._baca_template_mentah(folder)
        if isi is None:
            continue
        pemilik = str(isi.get("owner", ve.PEMILIK_LAMA) or "").strip().lower()
        if ve.is_tamu(pemilik):
            tamu += 1
        elif pemilik:
            per_akun[pemilik] += 1
    return per_akun, tamu


def _hitung_job() -> tuple[Counter[str], Counter[str], int]:
    """(job per pemilik, status per pemilik-status, job tamu)."""
    per_akun: Counter[str] = Counter()
    per_status: Counter[str] = Counter()
    tamu = 0
    for status in vt.daftar_job(batas=1000):
        pemilik = str(status.get("owner") or "")
        per_status[str(status.get("status") or "?")] += 1
        if ve.is_tamu(pemilik):
            tamu += 1
        elif pemilik:
            per_akun[pemilik] += 1
    return per_akun, per_status, tamu


@router.get("/users")
def daftar_akun(pengguna: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    """Ringkasan semua akun beserta pemakaiannya."""
    if not db.enabled():
        raise HTTPException(status_code=503, detail="Database belum dikonfigurasi.")
    try:
        baris = db.select(
            TABEL,
            {
                "select": "id,username,peran,created_at,last_login_at",
                "order": "created_at.desc",
                "limit": "500",
            },
        )
    except db.DbError as error:
        raise HTTPException(status_code=502, detail=f"Gagal membaca akun: {error}") from error

    template_per_akun, template_tamu = _hitung_template()
    job_per_akun, job_per_status, job_tamu = _hitung_job()

    akun = [
        {
            "username": str(b.get("username") or ""),
            "peran": str(b.get("peran") or "pengguna"),
            "created_at": b.get("created_at"),
            "last_login_at": b.get("last_login_at"),
            "jumlah_template": template_per_akun.get(str(b.get("username") or ""), 0),
            "jumlah_job": job_per_akun.get(str(b.get("username") or ""), 0),
            "diri_sendiri": str(b.get("username") or "") == pengguna["username"],
        }
        for b in baris
    ]
    return {
        "akun": akun,
        "ringkasan": {
            "jumlah_akun": len(akun),
            "template_akun": sum(template_per_akun.values()),
            "template_tamu": template_tamu,
            "job_akun": sum(job_per_akun.values()),
            "job_tamu": job_tamu,
            "job_per_status": dict(job_per_status),
        },
    }


@router.delete("/users/{username}")
def hapus_akun(
    username: str, pengguna: dict[str, Any] = Depends(developer_wajib)
) -> dict[str, Any]:
    """Hapus akun orang lain beserta isinya. Hanya developer."""
    username = str(username or "").strip().lower()
    if username == pengguna["username"]:
        # Menghapus diri sendiri lewat jalur ini bisa menyisakan sistem tanpa
        # satu pun developer; untuk itu ada DELETE /api/auth/me.
        raise HTTPException(
            status_code=400,
            detail="Akun sendiri dihapus lewat halaman akun, bukan dari sini.",
        )
    if not db.enabled():
        raise HTTPException(status_code=503, detail="Database belum dikonfigurasi.")
    try:
        ada = db.select(TABEL, {"username": f"eq.{username}", "select": "id", "limit": "1"})
    except db.DbError as error:
        raise HTTPException(status_code=502, detail=f"Gagal membaca akun: {error}") from error
    if not ada:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan.")
    from auth_api import buang_semua_milik

    jumlah = buang_semua_milik(username)
    logger.info("Akun %s dihapus oleh %s", username, pengguna["username"])
    return {"ok": True, "username": username, "dihapus": jumlah}


@router.post("/perbaiki-storage")
def perbaiki_storage(pengguna: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    """Dorong set layer & aset yang belum ada di Supabase, lalu laporkan.

    Worker berjalan di container tanpa volume backend, jadi berkas yang tidak
    sampai ke Supabase membuat set layer itu tampak normal di halaman tapi
    selalu gagal saat dirender. Ini menjalankan perbaikannya kapan saja,
    tanpa perlu menunggu backend dijalankan ulang.
    """
    import video_edit as ve

    return ve.perbaiki_storage()


@router.get("/users/{username}/jobs")
def job_pengguna(
    username: str, batas: int = 50, _: dict[str, Any] = Depends(developer_wajib)
) -> dict[str, Any]:
    """Riwayat render satu akun, lengkap dengan lognya - untuk menelusuri keluhan.

    Status job dibatasi pemiliknya di API biasa, jadi keluhan "video macet"
    atau "teksnya tidak muncul" dari akun lain tidak bisa diperiksa dengan
    fakta tanpa jalur ini. Sebelum dikembalikan, job yang terlantar (aktif
    tapi lama tak berkabar) ditandai gagal supaya yang terlihat keadaan
    sebenarnya, bukan "menyusun 2%" yang beku.
    """
    vt.tandai_terlantar()
    nama = username.strip().lower()
    return {"username": nama, "jobs": vt.daftar_job(max(1, min(200, batas)), owner=nama)}


@router.get("/jobs/{job_id}")
def job_satu(job_id: str, _: dict[str, Any] = Depends(developer_wajib)) -> dict[str, Any]:
    """Satu job milik siapa pun, apa adanya."""
    try:
        return vt.segarkan_kalau_terlantar(vt.baca_status(job_id))
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/users/{username}/peran")
def ubah_peran(
    username: str, peran: str, pengguna: dict[str, Any] = Depends(developer_wajib)
) -> dict[str, Any]:
    """Jadikan akun lain developer, atau kembalikan jadi pengguna biasa."""
    peran = str(peran or "").strip().lower()
    if peran not in PERAN_SAH:
        raise HTTPException(status_code=422, detail=f"Peran harus salah satu dari {PERAN_SAH}.")
    username = str(username or "").strip().lower()
    if username == pengguna["username"]:
        # Menurunkan diri sendiri bisa membuat tidak ada developer tersisa.
        raise HTTPException(status_code=400, detail="Peran akun sendiri tidak bisa diubah di sini.")
    if not db.enabled():
        raise HTTPException(status_code=503, detail="Database belum dikonfigurasi.")
    try:
        ada = db.select(TABEL, {"username": f"eq.{username}", "select": "id", "limit": "1"})
        if not ada:
            raise HTTPException(status_code=404, detail="Akun tidak ditemukan.")
        db.update(TABEL, {"username": f"eq.{username}"}, {"peran": peran})
    except db.DbError as error:
        raise HTTPException(status_code=502, detail=f"Gagal mengubah peran: {error}") from error
    # Cache sesi memegang peran lama; dibuang supaya perubahan langsung berlaku.
    import auth_api

    auth_api._sesi_cache.pop(username, None)
    logger.info("Peran %s diubah jadi %s oleh %s", username, peran, pengguna["username"])
    return {"ok": True, "username": username, "peran": peran}
