"""Sekali jalan, setelah fitur akun dipasang. Aman diulang.

1. Template yang dibuat sebelum ada akun diberi pemilik (TEMPLATE_PEMILIK_LAMA,
   bawaan "godam") dan template.json-nya didorong ke Supabase.
2. Template bawaan untuk tamu dan akun baru dibuat dari "utama" (TV Rakyat),
   lengkap dengan salinan asetnya, dengan id TEMPLATE_BAWAAN_ID.
3. Akun pemilik lama dibuat di tabel app_users kalau belum ada. Passwordnya
   dibaca dari env MIGRASI_PASSWORD; tidak ditulis di berkas ini.

Jalankan dari folder backend:
    MIGRASI_PASSWORD='...' .venv/bin/python migrasi_pemilik.py
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import auth_api  # noqa: E402
import db  # noqa: E402
import storage  # noqa: E402
import video_edit as ve  # noqa: E402

SUMBER_BAWAAN = os.getenv("MIGRASI_SUMBER_BAWAAN", "utama")


def beri_pemilik_template_lama() -> int:
    ve.sinkron_template_dari_storage(paksa=True)
    diberi = 0
    for folder in sorted(ve.templates_dir().iterdir()):
        berkas = folder / "template.json"
        if not folder.is_dir() or not berkas.is_file():
            continue
        try:
            raw = json.loads(berkas.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"  lewati {folder.name}: {error}")
            continue
        if "owner" in raw:
            continue
        raw["owner"] = ve.PEMILIK_LAMA
        berkas.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        if storage.enabled():
            storage.upload(f"templates/{folder.name}/template.json", berkas, "application/json")
        print(f"  {folder.name} ({raw.get('name')}) -> milik {ve.PEMILIK_LAMA}")
        diberi += 1
    return diberi


def buat_template_bawaan() -> bool:
    if (ve.template_path(ve.ID_TEMPLATE_BAWAAN) / "template.json").is_file():
        print(f"  template bawaan {ve.ID_TEMPLATE_BAWAAN} sudah ada")
        return False
    hasil = ve.duplicate_template(
        SUMBER_BAWAAN, "TV Rakyat", id_baru=ve.ID_TEMPLATE_BAWAAN, owner=ve.PEMILIK_BAWAAN
    )
    print(f"  template bawaan dibuat: {hasil['id']} ({hasil['name']}), pemilik='{hasil['owner']}'")
    return True


def buat_akun_pemilik_lama() -> bool:
    if not db.enabled():
        print("  database belum dikonfigurasi; akun dilewati")
        return False
    username = ve.PEMILIK_LAMA
    if db.select(auth_api.TABEL, {"username": f"eq.{username}", "select": "id", "limit": "1"}):
        print(f"  akun {username} sudah ada")
        return False
    password = os.getenv("MIGRASI_PASSWORD", "")
    if len(password) < auth_api.MIN_PASSWORD:
        print(f"  MIGRASI_PASSWORD kosong atau kurang dari {auth_api.MIN_PASSWORD} karakter; akun dilewati")
        return False
    # Sengaja tidak lewat pengecekan password bocor: ini akun yang passwordnya
    # ditentukan pemilik sendiri saat migrasi. Ganti lewat halaman setelah itu.
    db.insert(
        auth_api.TABEL,
        {"username": username, "password_hash": auth_api.hash_password(password), "sesi": 0},
    )
    print(f"  akun {username} dibuat")
    return True


def main() -> int:
    print("1. Memberi pemilik pada template lama")
    print(f"   -> {beri_pemilik_template_lama()} template diberi pemilik")
    print("2. Template bawaan")
    buat_template_bawaan()
    print("3. Akun pemilik lama")
    buat_akun_pemilik_lama()
    return 0


if __name__ == "__main__":
    sys.exit(main())
