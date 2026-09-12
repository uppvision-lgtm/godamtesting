"""Uji cepat sambungan Supabase Storage: unggah, baca, buka URL, lalu hapus."""

import sys
import time
from pathlib import Path

import httpx

import storage

if not storage.enabled():
    print("BELUM NYALA: SUPABASE_URL / SUPABASE_SERVICE_KEY belum terisi.")
    sys.exit(1)

print(f"URL    : {storage.SUPABASE_URL}")
print(f"Bucket : {storage.SUPABASE_BUCKET} (publik: {storage.SUPABASE_PUBLIC})")

jalur = f"cek/{int(time.time())}.txt"
isi = b"halo dari instagram-bot-app"

try:
    storage.ensure_bucket()
    print("bucket   : siap")

    storage.upload(jalur, isi, "text/plain")
    print(f"unggah   : OK -> {jalur}")

    tujuan = Path("/tmp/cek_supabase.txt")
    storage.download(jalur, tujuan)
    assert tujuan.read_bytes() == isi, "isi berkas berubah setelah diunduh"
    print("unduh    : OK, isinya sama persis")

    url = storage.url_for(jalur)
    respons = httpx.get(url, timeout=30)
    assert respons.status_code == 200, f"URL publik membalas {respons.status_code}"
    print(f"URL      : OK ({url[:70]}...)")

    tanda = storage.signed_upload(f"cek/signed-{int(time.time())}.mp4")
    print(f"URL unggah sekali pakai: OK (token {len(tanda['token'])} karakter)")

    storage.delete(jalur)
    print("hapus    : OK")
    print("\nSUPABASE NYALA ✓")
except Exception as error:  # noqa: BLE001
    print(f"\nGAGAL: {type(error).__name__}: {error}")
    sys.exit(1)
