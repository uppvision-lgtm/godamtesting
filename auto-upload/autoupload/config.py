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
SCHEDULER_INTERVAL = int(os.getenv("AUTOUPLOAD_SCHEDULER_INTERVAL", "30"))

PLATFORMS = ("tiktok", "instagram", "facebook", "youtube", "threads", "twitter")
