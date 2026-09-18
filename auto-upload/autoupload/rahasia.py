"""Penyandian Session ID sebelum masuk basis data.

Session ID setara password akun sosmed: siapa pun yang memegangnya bisa
memakai akun itu tanpa password maupun OTP. Menyimpannya polos berarti satu
berkas basis data yang bocor sudah cukup untuk mengambil alih semua akun,
dan berkas itu ikut ke volume server serta cadangan.

Kuncinya memakai CREDENTIAL_ENCRYPTION_KEY yang sama dengan penyimpanan isian
form di website, jadi tidak ada kunci baru yang perlu diurus.

Nilai bersandi diberi awalan penanda supaya baris lama yang tersimpan polos
tetap terbaca: tanpa awalan berarti belum disandikan. Dengan begitu tidak ada
migrasi yang harus dijalankan, dan setiap kali akun disimpan ulang nilainya
ikut tersandikan.
"""

from __future__ import annotations

import logging
import os

AWALAN = "enc1:"

log = logging.getLogger(__name__)
_sudah_diperingatkan = False


def _kunci():
    global _sudah_diperingatkan
    nilai = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not nilai:
        if not _sudah_diperingatkan:
            log.warning(
                "CREDENTIAL_ENCRYPTION_KEY belum diisi; Session ID disimpan "
                "apa adanya. Isi kunci itu supaya kredensial tersandikan."
            )
            _sudah_diperingatkan = True
        return None
    try:
        from cryptography.fernet import Fernet

        return Fernet(nilai.encode())
    except Exception:  # noqa: BLE001 - kunci salah bentuk
        if not _sudah_diperingatkan:
            log.warning("CREDENTIAL_ENCRYPTION_KEY tidak sah; Session ID disimpan apa adanya.")
            _sudah_diperingatkan = True
        return None


def sandikan(nilai: str) -> str:
    """Sandikan untuk disimpan. Kembalikan apa adanya kalau kunci tidak ada."""
    if not nilai or nilai.startswith(AWALAN):
        return nilai
    kunci = _kunci()
    if kunci is None:
        return nilai
    return AWALAN + kunci.encrypt(nilai.encode()).decode()


def buka(nilai: str) -> str:
    """Kembalikan nilai asli. Baris lama yang polos dilewatkan begitu saja."""
    if not nilai or not nilai.startswith(AWALAN):
        return nilai
    kunci = _kunci()
    if kunci is None:
        # Datanya tersandikan tetapi kuncinya hilang. Mengembalikan teks
        # bersandi akan membuat upload gagal dengan pesan membingungkan,
        # jadi lebih baik dianggap kosong: "sesi belum diisi".
        log.error("Session ID tersandikan tetapi CREDENTIAL_ENCRYPTION_KEY hilang.")
        return ""
    try:
        from cryptography.fernet import InvalidToken

        try:
            return kunci.decrypt(nilai[len(AWALAN):].encode()).decode()
        except InvalidToken:
            log.error("Session ID tidak bisa dibuka: kuncinya berbeda dari saat disimpan.")
            return ""
    except Exception:  # noqa: BLE001
        return ""


def tersandikan(nilai: str) -> bool:
    return bool(nilai) and nilai.startswith(AWALAN)
