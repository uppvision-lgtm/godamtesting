"""Login & daftar akun. Data pengguna disimpan di tabel Supabase ``app_users``.

Password TIDAK pernah disimpan apa adanya: yang masuk database hanya hash
scrypt beserta garamnya. Sesi dipegang token bertanda tangan HMAC, jadi server
tidak perlu menyimpan daftar sesi.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

import db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

TABEL = "app_users"

# Pendaftaran umum ditutup: aplikasi ini dipakai sendiri dulu. Nyalakan dengan
# AUTH_ALLOW_REGISTER=true kalau suatu saat mau dibuka untuk orang lain.
IZINKAN_DAFTAR = os.getenv("AUTH_ALLOW_REGISTER", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Akun pemilik disimpan di environment server, jadi login sudah bisa dipakai
# tanpa database. Hash-nya dibuat dengan ``python buat_akun.py``.
AKUN_USERNAME = os.getenv("AUTH_USERNAME", "").strip().lower()
AKUN_HASH = os.getenv("AUTH_PASSWORD_HASH", "").strip()
POLA_USERNAME = re.compile(r"^[a-zA-Z0-9._-]{3,30}$")
MIN_PASSWORD = 8
TOKEN_UMUR_DETIK = int(os.getenv("AUTH_TOKEN_TTL", str(7 * 24 * 3600)))

# Parameter scrypt: cukup berat untuk menghambat penebakan, masih ringan untuk
# satu permintaan login (±100 ms di server kecil).
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2**14, 8, 1, 32


def _kunci_tanda_tangan() -> bytes:
    kunci = os.getenv("SECRET_KEY", "").strip()
    if not kunci or kunci == "change-me-in-development":
        # Tanpa SECRET_KEY tetap, token hangus setiap server dimulai ulang.
        global _KUNCI_SEMENTARA
        if not _KUNCI_SEMENTARA:
            _KUNCI_SEMENTARA = secrets.token_hex(32)
            logger.warning(
                "SECRET_KEY belum diisi — token login akan hangus tiap server restart."
            )
        return _KUNCI_SEMENTARA.encode()
    return kunci.encode()


_KUNCI_SEMENTARA = ""


class AkunBody(BaseModel):
    username: str = Field(min_length=3, max_length=30)
    password: str = Field(min_length=MIN_PASSWORD, max_length=200)


# ============================================================
#  PASSWORD
# ============================================================


def hash_password(password: str) -> str:
    garam = secrets.token_bytes(16)
    turunan = hashlib.scrypt(
        password.encode(), salt=garam, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P, garam.hex(), turunan.hex()
    )


def cek_password(password: str, tersimpan: str) -> bool:
    try:
        jenis, n, r, p, garam, turunan = str(tersimpan).split("$")
        if jenis != "scrypt":
            return False
        hitung = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(garam),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(turunan)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(hitung, bytes.fromhex(turunan))


# ============================================================
#  TOKEN SESI
# ============================================================


def buat_token(user_id: str, username: str) -> str:
    isi = json.dumps(
        {"sub": user_id, "username": username, "exp": int(time.time()) + TOKEN_UMUR_DETIK},
        separators=(",", ":"),
    ).encode()
    badan = base64.urlsafe_b64encode(isi).rstrip(b"=")
    tanda = hmac.new(_kunci_tanda_tangan(), badan, hashlib.sha256).digest()
    return f"{badan.decode()}.{base64.urlsafe_b64encode(tanda).rstrip(b'=').decode()}"


def baca_token(token: str) -> dict[str, Any] | None:
    try:
        badan, tanda = str(token).split(".")
        harusnya = hmac.new(_kunci_tanda_tangan(), badan.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(
            base64.urlsafe_b64decode(tanda + "=" * (-len(tanda) % 4)), harusnya
        ):
            return None
        isi = json.loads(base64.urlsafe_b64decode(badan + "=" * (-len(badan) % 4)))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if int(isi.get("exp", 0)) < time.time():
        return None
    return isi


# ============================================================
#  ENDPOINT
# ============================================================


def _pastikan_siap() -> None:
    """Pendaftaran butuh tabel Supabase; akun pemilik tidak bisa dibuat dari web."""
    if not db.enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "Pendaftaran belum bisa dipakai: kredensial Supabase belum diisi "
                "di server (SUPABASE_URL & SUPABASE_SERVICE_KEY)."
            ),
        )


def _bersih(username: str) -> str:
    username = username.strip()
    if not POLA_USERNAME.match(username):
        raise HTTPException(
            status_code=422,
            detail="Username 3-30 karakter, hanya huruf, angka, titik, garis bawah, atau strip.",
        )
    return username.lower()


def _login_siap() -> bool:
    """Login bisa dipakai kalau ada akun pemilik ATAU tabel Supabase siap."""
    return bool(AKUN_USERNAME and AKUN_HASH) or db.enabled()


@router.get("/config")
def konfigurasi() -> dict[str, bool]:
    """Dipakai frontend untuk tahu keadaan login dan pendaftaran."""
    return {"enabled": _login_siap(), "register": IZINKAN_DAFTAR}


@router.post("/register")
def daftar(body: AkunBody) -> dict[str, Any]:
    if not IZINKAN_DAFTAR:
        raise HTTPException(
            status_code=403,
            detail="Pendaftaran sedang ditutup. Aplikasi ini baru dipakai oleh pemiliknya.",
        )
    _pastikan_siap()
    username = _bersih(body.username)
    try:
        if db.select(TABEL, {"username": f"eq.{username}", "select": "id", "limit": "1"}):
            raise HTTPException(status_code=409, detail="Username ini sudah dipakai.")
        akun = db.insert(
            TABEL, {"username": username, "password_hash": hash_password(body.password)}
        )
    except db.DbError as error:
        logger.exception("Gagal mendaftarkan akun")
        raise HTTPException(status_code=502, detail=f"Gagal menyimpan akun: {error}") from error
    return {
        "token": buat_token(str(akun.get("id")), username),
        "username": username,
    }


@router.post("/login")
def masuk(body: AkunBody) -> dict[str, Any]:
    username = body.username.strip().lower()

    # Akun pemilik dari environment: tidak perlu database sama sekali.
    if AKUN_USERNAME and AKUN_HASH and username == AKUN_USERNAME:
        if not cek_password(body.password, AKUN_HASH):
            raise HTTPException(status_code=401, detail="Username atau password salah.")
        return {"token": buat_token("pemilik", AKUN_USERNAME), "username": AKUN_USERNAME}

    if not db.enabled():
        # Tanpa database, satu-satunya akun yang ada adalah akun pemilik.
        raise HTTPException(status_code=401, detail="Username atau password salah.")

    try:
        baris = db.select(
            TABEL,
            {"username": f"eq.{username}", "select": "id,username,password_hash", "limit": "1"},
        )
    except db.DbError as error:
        logger.exception("Gagal membaca akun")
        raise HTTPException(status_code=502, detail=f"Gagal membaca akun: {error}") from error

    akun = baris[0] if baris else None
    # Pesan sengaja sama untuk username salah maupun password salah, supaya
    # tidak bisa dipakai menebak username mana yang terdaftar.
    if not akun or not cek_password(body.password, str(akun.get("password_hash") or "")):
        raise HTTPException(status_code=401, detail="Username atau password salah.")

    try:
        db.update(TABEL, {"id": f"eq.{akun['id']}"}, {"last_login_at": "now()"})
    except db.DbError:
        pass  # catatan waktu login bukan hal kritis

    return {"token": buat_token(str(akun["id"]), str(akun["username"])), "username": akun["username"]}


@router.get("/me")
def siapa(authorization: str = Header(default="")) -> dict[str, Any]:
    isi = baca_token(authorization.removeprefix("Bearer ").strip())
    if not isi:
        raise HTTPException(status_code=401, detail="Sesi tidak berlaku, silakan masuk lagi.")
    return {"username": isi.get("username"), "user_id": isi.get("sub")}
