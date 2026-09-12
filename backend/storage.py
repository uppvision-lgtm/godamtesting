"""Penyimpanan berkas di Supabase Storage.

Kenapa perlu: disk container Railway/VPS bersifat sementara — sekali deploy
ulang, template dan video hasil render ikut hilang. Supabase menyimpannya
secara permanen, sekaligus memberi URL yang bisa diputar langsung browser
(mendukung seek, tidak seperti berkas yang dialirkan lewat proxy).

Disk lokal tetap dipakai sebagai cache kerja karena ffmpeg membutuhkan berkas
nyata. Kalau kredensial Supabase tidak diisi, seluruh modul ini otomatis
nonaktif dan aplikasi berjalan dengan disk lokal saja (mode via PC).
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "media").strip() or "media"
# Bucket publik: URL bisa dibuka siapa saja yang punya tautannya. Kalau
# dimatikan, dipakai signed URL berumur pendek.
SUPABASE_PUBLIC = os.getenv("SUPABASE_PUBLIC_BUCKET", "true").lower() in ("1", "true", "yes")
SIGNED_URL_TTL = int(os.getenv("SUPABASE_SIGNED_URL_TTL", "86400"))
TIMEOUT = httpx.Timeout(120.0, connect=15.0)


class StorageError(RuntimeError):
    """Kegagalan saat berurusan dengan Supabase Storage."""


def enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey": SUPABASE_SERVICE_KEY,
    }
    headers.update(extra or {})
    return headers


def _api(path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1{path}"


def _periksa(respons: httpx.Response, aksi: str) -> None:
    if respons.status_code >= 400:
        raise StorageError(f"{aksi} gagal ({respons.status_code}): {respons.text[:200]}")


def ensure_bucket() -> bool:
    """Buat bucket kalau belum ada. Aman dipanggil berkali-kali."""
    if not enabled():
        return False
    with httpx.Client(timeout=TIMEOUT) as client:
        ada = client.get(_api(f"/bucket/{SUPABASE_BUCKET}"), headers=_headers())
        if ada.status_code == 200:
            return True
        buat = client.post(
            _api("/bucket"),
            headers=_headers({"Content-Type": "application/json"}),
            json={
                "id": SUPABASE_BUCKET,
                "name": SUPABASE_BUCKET,
                "public": SUPABASE_PUBLIC,
                "file_size_limit": int(os.getenv("SUPABASE_FILE_LIMIT_MB", "512")) * 1_048_576,
            },
        )
        # 409 = sudah dibuat proses lain; itu bukan kegagalan.
        if buat.status_code == 409:
            return True
        _periksa(buat, "Membuat bucket")
        logger.info("Bucket Supabase '%s' dibuat", SUPABASE_BUCKET)
        return True


def upload(path: str, sumber: Path | bytes, content_type: str | None = None) -> str:
    """Simpan berkas ke Supabase. ``path`` relatif terhadap bucket."""
    if not enabled():
        raise StorageError("Supabase belum dikonfigurasi")
    isi = sumber.read_bytes() if isinstance(sumber, Path) else sumber
    if content_type is None:
        nama = str(sumber) if isinstance(sumber, Path) else path
        content_type = mimetypes.guess_type(nama)[0] or "application/octet-stream"
    with httpx.Client(timeout=TIMEOUT) as client:
        respons = client.post(
            _api(f"/object/{SUPABASE_BUCKET}/{path.lstrip('/')}"),
            headers=_headers({"Content-Type": content_type, "x-upsert": "true"}),
            content=isi,
        )
    _periksa(respons, f"Mengunggah {path}")
    return path


def download(path: str, tujuan: Path) -> Path:
    """Ambil berkas dari Supabase ke disk lokal."""
    if not enabled():
        raise StorageError("Supabase belum dikonfigurasi")
    tujuan.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=TIMEOUT) as client:
        with client.stream(
            "GET", _api(f"/object/{SUPABASE_BUCKET}/{path.lstrip('/')}"), headers=_headers()
        ) as respons:
            if respons.status_code >= 400:
                respons.read()
                _periksa(respons, f"Mengunduh {path}")
            with tujuan.open("wb") as berkas:
                for potongan in respons.iter_bytes(1_048_576):
                    berkas.write(potongan)
    return tujuan


def exists(path: str) -> bool:
    if not enabled():
        return False
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            respons = client.request(
                "HEAD", _api(f"/object/{SUPABASE_BUCKET}/{path.lstrip('/')}"), headers=_headers()
            )
        return respons.status_code == 200
    except httpx.HTTPError:
        return False


def delete(path: str) -> None:
    if not enabled():
        return
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            client.delete(
                _api(f"/object/{SUPABASE_BUCKET}/{path.lstrip('/')}"), headers=_headers()
            )
    except httpx.HTTPError as error:
        logger.warning("Gagal menghapus %s dari Supabase: %s", path, error)


def list_prefix(prefix: str, limit: int = 100) -> list[dict[str, Any]]:
    """Daftar berkas di bawah satu prefix (folder)."""
    if not enabled():
        return []
    with httpx.Client(timeout=TIMEOUT) as client:
        respons = client.post(
            _api(f"/object/list/{SUPABASE_BUCKET}"),
            headers=_headers({"Content-Type": "application/json"}),
            json={"prefix": prefix.strip("/"), "limit": limit, "offset": 0},
        )
    _periksa(respons, f"Membaca daftar {prefix}")
    return [item for item in respons.json() if item.get("name")]


def url_for(path: str) -> str:
    """URL untuk dibuka browser: publik kalau bucket publik, kalau tidak signed."""
    if not enabled():
        return ""
    bersih = path.lstrip("/")
    if SUPABASE_PUBLIC:
        return _api(f"/object/public/{SUPABASE_BUCKET}/{bersih}")
    with httpx.Client(timeout=TIMEOUT) as client:
        respons = client.post(
            _api(f"/object/sign/{SUPABASE_BUCKET}/{bersih}"),
            headers=_headers({"Content-Type": "application/json"}),
            json={"expiresIn": SIGNED_URL_TTL},
        )
    _periksa(respons, f"Membuat signed URL {path}")
    return f"{SUPABASE_URL}/storage/v1{respons.json()['signedURL']}"


def signed_upload(path: str) -> dict[str, str]:
    """URL sekali pakai agar browser mengunggah LANGSUNG ke Supabase.

    Dipakai halaman auto upload: berkas video tidak perlu melewati fungsi
    serverless Vercel yang membatasi badan permintaan 4,5 MB.
    """
    if not enabled():
        raise StorageError("Supabase belum dikonfigurasi")
    bersih = path.lstrip("/")
    with httpx.Client(timeout=TIMEOUT) as client:
        respons = client.post(
            _api(f"/object/upload/sign/{SUPABASE_BUCKET}/{bersih}"),
            headers=_headers({"Content-Type": "application/json"}),
            json={},
        )
    _periksa(respons, f"Membuat URL unggah {path}")
    data = respons.json()
    return {
        "path": bersih,
        "url": f"{SUPABASE_URL}/storage/v1{data['url']}",
        "token": data.get("token", ""),
    }
