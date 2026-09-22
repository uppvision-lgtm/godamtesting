"""Jatah pemakaian untuk tamu: dua kali jalan per fitur.

Tamu boleh memakai SELURUH fitur dari awal sampai jadi - yang dibatasi hanya
berapa kali, bukan fiturnya. Sesudah jatahnya habis, satu-satunya yang
diminta adalah masuk atau mendaftar; akun yang sudah masuk tidak dihitung
sama sekali.

Dihitung dua-duanya: per id tamu DAN per alamat IP. Id tamu dibuat sendiri
oleh peramban, jadi kalau hanya itu yang dihitung, jatahnya cukup dilewati
dengan membuka tab baru. IP jauh lebih mahal untuk diganti-ganti, dan
gabungan keduanya membuat batas ini benar-benar berarti tanpa menyusahkan
orang yang memakainya dengan wajar.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# Berapa kali satu fitur boleh dijalankan tamu. Dihitung sejak pemakaian
# pertama dan disetel ulang setelah UMUR_JAM.
JATAH_TAMU = int(os.getenv("JATAH_TAMU", "2"))
UMUR_JAM = float(os.getenv("JATAH_TAMU_UMUR_JAM", "24"))

# Nama fitur yang dihitung, beserta kalimat ajakannya.
AJAKAN = {
    "render": "membuat video",
    "unggah-sumber": "mengunggah video sumber",
    "bot-komentar": "menjalankan bot komentar",
    "unggah-sosmed": "mengunggah ke sosial media",
}


def _redis():
    from video_tasks import _r

    return _r()


def _kunci(fitur: str, penanda: str) -> str:
    return f"jatah-tamu:{fitur}:{hashlib.sha256(penanda.encode()).hexdigest()[:16]}"


def ip_pemanggil(request: Request | None) -> str:
    """Alamat IP pemanggil, diambil dari ujung rantai proxy.

    Nilai TERAKHIR yang dipakai, bukan yang pertama: yang pertama dikirim
    klien dan bisa dikarang sesukanya, sedangkan proxy menambahkan alamat
    aslinya di belakang.
    """
    if request is None:
        return "tanpa-ip"
    rantai = [b.strip() for b in request.headers.get("x-forwarded-for", "").split(",") if b.strip()]
    if rantai:
        return rantai[-1]
    return request.client.host if request.client else "tanpa-ip"


def sisa(fitur: str, pengguna: dict[str, Any] | None, request: Request | None) -> int | None:
    """Sisa jatah tamu untuk fitur ini. None berarti tanpa batas (sudah masuk)."""
    if pengguna and not pengguna.get("tamu"):
        return None
    penanda = [str((pengguna or {}).get("username") or "tanpa-id"), ip_pemanggil(request)]
    try:
        r = _redis()
        terpakai = max(int(r.get(_kunci(fitur, p)) or 0) for p in penanda)
    except Exception:  # noqa: BLE001 - Redis mati: jangan menghalangi pemakaian
        logger.warning("Jatah tamu tidak terbaca untuk %s", fitur)
        return JATAH_TAMU
    return max(0, JATAH_TAMU - terpakai)


def pakai(fitur: str, pengguna: dict[str, Any] | None, request: Request | None, jumlah: int = 1) -> None:
    """Catat satu pemakaian; tolak kalau jatahnya sudah habis.

    Akun yang sudah masuk lewat begitu saja - tidak ada yang dihitung dan
    tidak ada yang ditulis ke Redis.
    """
    if pengguna and not pengguna.get("tamu"):
        return
    penanda = [str((pengguna or {}).get("username") or "tanpa-id"), ip_pemanggil(request)]
    try:
        r = _redis()
        terpakai = max(int(r.get(_kunci(fitur, p)) or 0) for p in penanda)
    except Exception:  # noqa: BLE001 - Redis mati tidak boleh mematikan fitur
        return
    if terpakai + jumlah > JATAH_TAMU:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Jatah tamu untuk {AJAKAN.get(fitur, fitur)} sudah habis "
                f"({JATAH_TAMU} kali). Masuk atau daftar - gratis, dan setelah itu "
                "tidak ada batasnya. Pekerjaanmu juga ikut tersimpan."
            ),
        )
    pipa = r.pipeline()
    for p in penanda:
        kunci = _kunci(fitur, p)
        pipa.incrby(kunci, jumlah)
        pipa.expire(kunci, int(UMUR_JAM * 3600))
    try:
        pipa.execute()
    except Exception:  # noqa: BLE001
        logger.warning("Jatah tamu gagal dicatat untuk %s", fitur)
