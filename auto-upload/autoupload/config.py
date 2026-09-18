"""Konfigurasi minimal - semua lewat environment variable, tanpa dependency."""

import os
from pathlib import Path

# Folder basis untuk data runtime (profil browser, file upload, database).
# Set AUTOUPLOAD_DATA_DIR kalau paket ini dipakai dari project lain dengan
# working directory berbeda.
DATA_DIR = Path(os.getenv("AUTOUPLOAD_DATA_DIR", ".")).resolve()

DATABASE_URL = os.getenv("AUTOUPLOAD_DATABASE_URL", f"sqlite:///{DATA_DIR / 'autoupload.db'}")
UPLOAD_DIR = DATA_DIR / os.getenv("AUTOUPLOAD_UPLOAD_DIR", "uploads")
PROFILES_DIR = DATA_DIR / "browser_profiles"   # profil Chrome per akun (sesi login)
DEBUG_SHOTS_DIR = DATA_DIR / "debug_shots"     # screenshot saat upload gagal

MAX_FILE_SIZE = int(os.getenv("AUTOUPLOAD_MAX_FILE_MB", "500")) * 1024 * 1024
# Upload yang lebih lama dari ini dianggap tidak akan selesai: prosesnya
# mati di tengah jalan, dan barisnya harus dibereskan supaya tidak
# menggantung di status "processing" selamanya.
JOB_TIMEOUT_MENIT = int(os.getenv("AUTOUPLOAD_JOB_TIMEOUT_MENIT", "30"))
# Riwayat lebih tua dari ini dibuang berikut berkas medianya.
RIWAYAT_SIMPAN_HARI = int(os.getenv("AUTOUPLOAD_RIWAYAT_HARI", "30"))
# Screenshot kegagalan yang disimpan; sisanya dibuang dari yang terlama.
MAKS_DEBUG_SHOT = int(os.getenv("AUTOUPLOAD_MAKS_DEBUG_SHOT", "20"))
# Panjang maksimum pesan galat yang disimpan ke basis data.
MAKS_PANJANG_GALAT = int(os.getenv("AUTOUPLOAD_MAKS_GALAT", "240"))

PLATFORMS = ("tiktok", "instagram", "facebook", "youtube", "threads", "twitter")
