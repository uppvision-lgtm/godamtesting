"""Login, daftar akun, dan ganti password. Akun disimpan di tabel Supabase ``app_users``.

Password TIDAK pernah disimpan apa adanya: yang masuk database hanya hash
scrypt beserta garamnya. Sesi dipegang token bertanda tangan HMAC, jadi server
tidak perlu menyimpan daftar sesi. Supaya ganti password bisa mengeluarkan
perangkat lain, token membawa nomor ``sesi`` yang dicocokkan dengan nomor di
tabel; nomor itu naik satu tiap kali password diganti.
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
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

import db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

TABEL = "app_users"

# Pendaftaran dibuka untuk umum. Matikan dengan AUTH_ALLOW_REGISTER=false kalau
# suatu saat mau ditutup lagi.
IZINKAN_DAFTAR = os.getenv("AUTH_ALLOW_REGISTER", "true").strip().lower() in (
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
# Hanya panjang yang dipaksa. Memaksa simbol/angka/kapital justru mendorong
# orang memakai pola yang mudah ditebak ("Password1!").
MIN_PASSWORD = 8
TOKEN_UMUR_DETIK = int(os.getenv("AUTH_TOKEN_TTL", str(7 * 24 * 3600)))

# Parameter scrypt: cukup berat untuk menghambat penebakan, masih ringan untuk
# satu permintaan login (±100 ms di server kecil).
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2**14, 8, 1, 32

# Pengecekan password bocor lewat Have I Been Pwned. Yang dikirim ke sana hanya
# lima karakter pertama dari SHA-1 password (k-anonymity), bukan passwordnya.
CEK_PASSWORD_BOCOR = os.getenv("AUTH_CHECK_PWNED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
PWNED_URL = "https://api.pwnedpasswords.com/range/"
PWNED_TIMEOUT = float(os.getenv("AUTH_PWNED_TIMEOUT", "4"))

# Nomor sesi tiap akun diingat sebentar supaya polling halaman (tiap 2 detik)
# tidak menghubungi Supabase pada setiap permintaan.
SESI_CACHE_DETIK = 60.0
_sesi_cache: dict[str, tuple[int | None, float]] = {}


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


class GantiPasswordBody(BaseModel):
    password_lama: str = Field(min_length=1, max_length=200)
    password_baru: str = Field(min_length=MIN_PASSWORD, max_length=200)


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


def password_bocor(password: str) -> bool | None:
    """True kalau password ini pernah muncul di kebocoran data (Have I Been Pwned).

    None berarti layanannya tidak bisa dihubungi. Pemanggil memperlakukan None
    sebagai "lanjutkan": orang tidak boleh gagal mendaftar hanya karena server
    pihak ketiga sedang bermasalah.
    """
    if not CEK_PASSWORD_BOCOR:
        return False
    sha1 = hashlib.sha1(password.encode()).hexdigest().upper()  # noqa: S324 - memang protokol HIBP
    awalan, sisa = sha1[:5], sha1[5:]
    try:
        respons = httpx.get(
            PWNED_URL + awalan,
            headers={"Add-Padding": "true", "User-Agent": "ig-tools-auth"},
            timeout=PWNED_TIMEOUT,
        )
    except httpx.HTTPError as error:
        logger.warning("Pengecekan password bocor dilewati: %s", error)
        return None
    if respons.status_code != 200:
        logger.warning("Pengecekan password bocor dilewati: HTTP %s", respons.status_code)
        return None
    for baris in respons.text.splitlines():
        akhiran, _, jumlah = baris.strip().partition(":")
        # Dengan Add-Padding, balasan diisi baris palsu berjumlah 0 supaya
        # ukurannya tidak membocorkan apa pun; baris itu bukan kebocoran.
        if akhiran == sisa:
            try:
                return int(jumlah or 0) > 0
            except ValueError:
                return True
    return False


def _tolak_kalau_bocor(password: str) -> None:
    if password_bocor(password):
        raise HTTPException(
            status_code=422,
            detail=(
                "Password ini pernah bocor di kebocoran data situs lain, jadi mudah "
                "ditebak. Pilih password yang lain."
            ),
        )


# ============================================================
#  TOKEN SESI
# ============================================================


def buat_token(user_id: str, username: str, sesi: int = 0) -> str:
    isi = json.dumps(
        {
            "sub": user_id,
            "username": username,
            "sesi": int(sesi),
            "exp": int(time.time()) + TOKEN_UMUR_DETIK,
        },
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


def _sesi_akun(username: str) -> int | None:
    """Nomor sesi akun di database, atau None kalau akunnya tidak ada."""
    sekarang = time.time()
    tersimpan = _sesi_cache.get(username)
    if tersimpan and tersimpan[1] > sekarang:
        return tersimpan[0]
    try:
        baris = db.select(
            TABEL, {"username": f"eq.{username}", "select": "sesi", "limit": "1"}
        )
    except db.DbError as error:
        logger.warning("Gagal membaca sesi akun %s: %s", username, error)
        # Database sedang bermasalah: pakai nilai lama kalau pernah ada, dan
        # kalau belum pernah, percayai tokennya supaya orang tidak terlempar
        # keluar hanya karena Supabase sempat tidak menjawab.
        return tersimpan[0] if tersimpan else -1
    sesi = int(baris[0].get("sesi") or 0) if baris else None
    _sesi_cache[username] = (sesi, sekarang + SESI_CACHE_DETIK)
    return sesi


def _ingat_sesi(username: str, sesi: int) -> None:
    _sesi_cache[username] = (sesi, time.time() + SESI_CACHE_DETIK)


# ============================================================
#  DEPENDENCY: SIAPA YANG SEDANG MASUK
# ============================================================


def pengguna_opsional(authorization: str = Header(default="")) -> dict[str, Any] | None:
    """Akun yang sedang masuk, atau None untuk tamu. Tidak pernah menolak permintaan."""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    isi = baca_token(token)
    if not isi:
        return None
    username = str(isi.get("username") or "").strip().lower()
    user_id = str(isi.get("sub") or "")
    if not username or not user_id:
        return None

    if user_id == "pemilik":
        # Akun dari environment tidak punya baris di tabel, jadi tidak ada
        # nomor sesi yang bisa dicocokkan.
        if AKUN_USERNAME and username == AKUN_USERNAME:
            return {"user_id": user_id, "username": username}
        return None

    if not db.enabled():
        return None
    sesi = _sesi_akun(username)
    if sesi is None:
        return None
    if sesi != -1 and int(isi.get("sesi") or 0) != sesi:
        # Password sudah diganti setelah token ini dibuat.
        return None
    return {"user_id": user_id, "username": username}


def pengguna_wajib(
    pengguna: dict[str, Any] | None = Depends(pengguna_opsional),
) -> dict[str, Any]:
    if not pengguna:
        raise HTTPException(status_code=401, detail="Masuk dulu untuk melakukan ini.")
    return pengguna


# Id tamu dibuat peramban dan dikirim lewat header. Bentuknya dibatasi ketat
# karena nilainya dipakai sebagai nama pemilik template di disk.
AWALAN_TAMU = "guest:"
POLA_ID_TAMU = re.compile(r"^[a-z0-9]{8,40}$")


def bersihkan_id_tamu(nilai: str) -> str:
    """Ubah header tamu jadi nama pemilik yang aman, atau string kosong."""
    nilai = str(nilai or "").strip().lower()
    nilai = nilai.removeprefix(AWALAN_TAMU)
    if not POLA_ID_TAMU.match(nilai):
        return ""
    return AWALAN_TAMU + nilai


def identitas(
    pengguna: dict[str, Any] | None = Depends(pengguna_opsional),
    x_guest_id: str = Header(default=""),
) -> dict[str, Any] | None:
    """Siapa pemilik isi ini: akun yang masuk, atau tamu dari header.

    Tamu diperlakukan seperti profil tamu di peramban — semua fitur jalan,
    tetapi apa pun yang dibuat tidak ikut tersimpan permanen.
    """
    if pengguna:
        return {**pengguna, "tamu": False}
    tamu = bersihkan_id_tamu(x_guest_id)
    if tamu:
        return {"user_id": tamu, "username": tamu, "tamu": True}
    return None


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


def _siapkan_template_awal(username: str) -> None:
    """Akun baru langsung punya satu template, TV Rakyat, yang siap dipakai."""
    import video_edit as ve

    try:
        ve.template_awal_untuk(username)
    except ve.VideoError as error:
        # Pendaftarannya tetap sah; halaman akan membuatkan template kosong
        # begitu dibuka kalau daftarnya kosong.
        logger.warning("Template awal untuk %s tidak dibuat: %s", username, error)
    except Exception:  # noqa: BLE001
        logger.exception("Template awal untuk %s gagal dibuat", username)


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
    if AKUN_USERNAME and username == AKUN_USERNAME:
        raise HTTPException(status_code=409, detail="Username ini sudah dipakai.")
    _tolak_kalau_bocor(body.password)
    try:
        if db.select(TABEL, {"username": f"eq.{username}", "select": "id", "limit": "1"}):
            raise HTTPException(status_code=409, detail="Username ini sudah dipakai.")
        akun = db.insert(
            TABEL,
            {"username": username, "password_hash": hash_password(body.password), "sesi": 0},
        )
    except db.DbError as error:
        # Dua pendaftaran nama yang sama pada saat bersamaan: yang kalah ditolak
        # oleh batasan unik di database, bukan oleh pengecekan di atas.
        if "23505" in str(error) or "duplicate" in str(error).lower():
            raise HTTPException(status_code=409, detail="Username ini sudah dipakai.") from error
        logger.exception("Gagal mendaftarkan akun")
        raise HTTPException(status_code=502, detail=f"Gagal menyimpan akun: {error}") from error
    _ingat_sesi(username, 0)
    _siapkan_template_awal(username)
    return {
        "token": buat_token(str(akun.get("id")), username, 0),
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
            {
                "username": f"eq.{username}",
                "select": "id,username,password_hash,sesi",
                "limit": "1",
            },
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
        db.update(
            TABEL,
            {"id": f"eq.{akun['id']}"},
            {"last_login_at": datetime.now(timezone.utc).isoformat()},
        )
    except db.DbError:
        pass  # catatan waktu login bukan hal kritis

    sesi = int(akun.get("sesi") or 0)
    _ingat_sesi(username, sesi)
    return {
        "token": buat_token(str(akun["id"]), str(akun["username"]), sesi),
        "username": akun["username"],
    }


@router.post("/change-password")
def ganti_password(
    body: GantiPasswordBody, pengguna: dict[str, Any] = Depends(pengguna_wajib)
) -> dict[str, Any]:
    """Ganti password. Semua sesi lama, di perangkat mana pun, langsung keluar."""
    if pengguna["user_id"] == "pemilik":
        raise HTTPException(
            status_code=400,
            detail="Akun pemilik diatur lewat environment server, bukan dari halaman ini.",
        )
    username = pengguna["username"]
    try:
        baris = db.select(
            TABEL,
            {"id": f"eq.{pengguna['user_id']}", "select": "id,password_hash,sesi", "limit": "1"},
        )
    except db.DbError as error:
        logger.exception("Gagal membaca akun")
        raise HTTPException(status_code=502, detail=f"Gagal membaca akun: {error}") from error
    akun = baris[0] if baris else None
    if not akun:
        raise HTTPException(status_code=401, detail="Sesi tidak berlaku, silakan masuk lagi.")
    if not cek_password(body.password_lama, str(akun.get("password_hash") or "")):
        raise HTTPException(status_code=401, detail="Password lama salah.")
    if body.password_baru == body.password_lama:
        raise HTTPException(
            status_code=422, detail="Password baru harus berbeda dari password lama."
        )
    _tolak_kalau_bocor(body.password_baru)

    sesi_baru = int(akun.get("sesi") or 0) + 1
    try:
        db.update(
            TABEL,
            {"id": f"eq.{akun['id']}"},
            {"password_hash": hash_password(body.password_baru), "sesi": sesi_baru},
        )
    except db.DbError as error:
        logger.exception("Gagal mengganti password")
        raise HTTPException(status_code=502, detail=f"Gagal menyimpan password: {error}") from error
    _ingat_sesi(username, sesi_baru)
    # Token baru untuk perangkat ini; token lama di mana pun sudah tidak cocok
    # dengan nomor sesi yang baru.
    return {"token": buat_token(str(akun["id"]), username, sesi_baru), "username": username}


@router.get("/me")
def siapa(pengguna: dict[str, Any] | None = Depends(pengguna_opsional)) -> dict[str, Any]:
    if not pengguna:
        raise HTTPException(status_code=401, detail="Sesi tidak berlaku, silakan masuk lagi.")
    return {"username": pengguna["username"], "user_id": pengguna["user_id"]}
