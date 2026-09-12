"""Buat akun pemilik untuk login (dipakai sendiri, tanpa pendaftaran terbuka).

Jalankan:  python buat_akun.py

Password tidak pernah disimpan apa adanya — yang ditulis ke backend/.env hanya
hash scrypt beserta garamnya. Kalau kredensial Supabase sudah terisi, akunnya
bisa sekalian dimasukkan ke tabel app_users.
"""

from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

from auth_api import POLA_USERNAME, hash_password

ENV = Path(__file__).with_name(".env")


def tulis_env(username: str, sandi_hash: str) -> None:
    baris = ENV.read_text(encoding="utf-8").splitlines() if ENV.is_file() else []
    baris = [b for b in baris if not re.match(r"^AUTH_(USERNAME|PASSWORD_HASH)=", b)]
    baris += [f"AUTH_USERNAME={username}", f"AUTH_PASSWORD_HASH={sandi_hash}"]
    ENV.write_text("\n".join(baris) + "\n", encoding="utf-8")
    ENV.chmod(0o600)


def main() -> int:
    print("=== Buat akun pemilik ===")
    username = input("Username: ").strip()
    if not POLA_USERNAME.match(username):
        print("Username 3-30 karakter: huruf, angka, titik, garis bawah, atau strip.")
        return 1

    sandi = getpass.getpass("Password (minimal 8 karakter, tidak tampil): ")
    if len(sandi) < 8:
        print("Password terlalu pendek.")
        return 1
    if sandi != getpass.getpass("Ulangi password: "):
        print("Password tidak sama.")
        return 1

    sandi_hash = hash_password(sandi)
    tulis_env(username.lower(), sandi_hash)
    print(f"\nAkun '{username.lower()}' tersimpan di backend/.env")

    try:
        import db

        if db.enabled():
            jawab = input("Simpan juga ke tabel app_users di Supabase? [y/N] ").strip().lower()
            if jawab == "y":
                if db.select("app_users", {"username": f"eq.{username.lower()}", "select": "id"}):
                    print("  (sudah ada di tabel, dilewati)")
                else:
                    db.insert(
                        "app_users",
                        {"username": username.lower(), "password_hash": sandi_hash},
                    )
                    print("  tersimpan di Supabase.")
    except Exception as error:  # noqa: BLE001
        print(f"  Lewati Supabase: {error}")

    print("\nJalankan ulang backend supaya akunnya terbaca, lalu login lewat tombol Masuk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
