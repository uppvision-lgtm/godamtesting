import os
"""Lapisan HTTP: satu APIRouter berisi semua endpoint.

Kalau website utamamu FastAPI:
    from autoupload import router
    app.include_router(router)

Kalau Flask/Django: jalankan `python main.py` sebagai service kecil di port
sendiri, atau panggil `upload_sync()` langsung dari view tanpa lewat HTTP.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from autoupload import login
from autoupload.browser import session_names
from autoupload.config import MAX_FILE_SIZE, PLATFORMS, UPLOAD_DIR
from autoupload.db import Post, SocialAccount, get_db
from autoupload.platforms import REGISTRY
from autoupload.service import create_post, run_post
from autoupload.verify import periksa_sesi

# Awalan alamat bisa digeser lewat environment. Dipakai saat paket ini
# dipasang di dalam website utama, yang sudah punya "/api/upload" sendiri untuk
# hal lain; di sana awalannya jadi "/api/sosmed". Dijalankan sendiri lewat
# main.py, awalannya tetap "/api" seperti semula.
API_PREFIX = os.getenv("AUTOUPLOAD_API_PREFIX", "/api")

router = APIRouter(prefix=API_PREFIX, tags=["autoupload"])

VIDEO_EXT = {".mp4", ".mov", ".avi", ".webm"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}


# ---------------------------------------------------------------- skema

class UploadRequest(BaseModel):
    media_path: str
    caption: str = ""
    platforms: list[str]
    media_kind: str = Field("video", pattern="^(video|photo)$")
    scheduled_at: str | None = None          # ISO datetime, kosong = sekarang
    account_ids: dict[str, int] = Field(default_factory=dict)  # platform -> id akun


class AccountRequest(BaseModel):
    platform: str
    display_name: str = ""
    session_name: str = "default"
    session_id: str = ""                     # opsional: jalan pintas cookie
    extra: dict = Field(default_factory=dict)


# ---------------------------------------------------------------- media

@router.post("/media")
async def upload_media(file: UploadFile = File(...)) -> dict:
    """Simpan file media, kembalikan `media_path` untuk dipakai POST /api/upload."""
    suffix = Path(file.filename or "media").suffix.lower()
    if suffix not in VIDEO_EXT | PHOTO_EXT:
        raise HTTPException(400, f"Tipe file '{suffix}' tidak didukung")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = UPLOAD_DIR / f"{stamp}_{uuid.uuid4().hex[:8]}{suffix}"

    size = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"File lewat batas {MAX_FILE_SIZE // 1024 // 1024} MB")
            out.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "File kosong")

    return {
        "media_path": str(dest),
        "size_bytes": size,
        "media_kind": "video" if suffix in VIDEO_EXT else "photo",
    }


# ---------------------------------------------------------------- upload

@router.post("/upload")
async def upload(req: UploadRequest, db: Session = Depends(get_db)) -> dict:
    """Upload sekarang (tunggu selesai) atau simpan sebagai jadwal."""
    unknown = [p for p in req.platforms if p not in PLATFORMS]
    if not req.platforms or unknown:
        raise HTTPException(400, f"Platform tidak valid: {unknown or 'kosong'}")
    if not req.caption.strip():
        raise HTTPException(400, "Caption wajib diisi")
    if not Path(req.media_path).exists():
        raise HTTPException(400, f"File tidak ditemukan: {req.media_path}")

    run_at = None
    if req.scheduled_at:
        try:
            run_at = datetime.fromisoformat(req.scheduled_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "scheduled_at harus ISO datetime")
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        # WAJIB dinormalkan ke UTC sebelum disimpan. SQLAlchemy menulis kolom
        # DateTime SQLite dari medan jam APA ADANYA dan MEMBUANG offsetnya,
        # jadi "19:20+07:00" akan tersimpan sebagai "19:20" lalu dibaca ulang
        # sebagai UTC - jadwalnya meleset tujuh jam tanpa error apa pun.
        run_at = run_at.astimezone(timezone.utc)
        if run_at <= datetime.now(timezone.utc):
            run_at = None                     # sudah lewat -> jalankan sekarang

    post = create_post(
        db,
        media_path=req.media_path,
        caption=req.caption.strip(),
        platforms=req.platforms,
        media_kind=req.media_kind,
        scheduled_at=run_at,
        account_ids=req.account_ids,
    )
    # Dijalankan SEKARANG walau dijadwalkan: yang menunda adalah fitur jadwal
    # milik platformnya, bukan proses di laptop ini. Jadi kalau laptop mati
    # nanti, postingan tetap terbit pada waktunya.
    results = await run_post(post.id)
    db.expire_all()
    status = db.query(Post).filter(Post.id == post.id).first().status
    return {"post_id": post.id, "status": status, "results": results}


@router.get("/posts")
def list_posts(limit: int = 20, db: Session = Depends(get_db)) -> list[dict]:
    """Riwayat + jadwal postingan terbaru."""
    posts = db.query(Post).order_by(Post.id.desc()).limit(min(limit, 100)).all()
    return [
        {
            "id": p.id,
            "status": p.status,
            "media_kind": p.media_kind,
            "caption": (p.caption or "")[:120],
            "scheduled_at": p.scheduled_at,
            "created_at": p.created_at,
            "targets": [
                {"platform": t.platform, "status": t.status, "url": t.url, "error": t.error}
                for t in p.targets
            ],
        }
        for p in posts
    ]


# ---------------------------------------------------------------- akun

def _pratinjau_cookie(platform: str, session_id: str) -> list[dict]:
    """Ringkasan Session ID per cookie, SUDAH DISAMARKAN.

    Halaman perlu memperlihatkan "sudah diisi apa" supaya orang tidak harus
    menekan Ganti hanya untuk memastikan. Tetapi Session ID setara password dan
    router ini tidak punya autentikasi, jadi nilai utuhnya tidak boleh
    dikirimkan: yang keluar hanya pangkal, ujung, dan panjangnya - cukup untuk
    mengenali nilai, tidak cukup untuk memakai akunnya.
    """
    kelas = REGISTRY.get(platform)
    if kelas is None or not session_id:
        return []
    nama = list(getattr(kelas, "session_cookie_map", {}) or {})
    opsional = set(getattr(kelas, "session_cookie_optional", ()) or ())
    inti = [n for n in nama if n not in opsional]

    bagian = session_id.split("|") if "|" in session_id else [session_id]
    if len(bagian) == len(nama):
        dipakai = nama
    elif len(bagian) == len(inti):
        dipakai = inti
    else:
        # Jumlahnya tidak cocok dengan pemetaan mana pun: tampilkan apa adanya
        # supaya tetap terlihat, tanpa menebak nama cookie-nya.
        dipakai = [f"nilai {i + 1}" for i in range(len(bagian))]

    hasil = []
    for label, nilai in zip(dipakai, bagian):
        nilai = nilai.strip()
        samar = f"{nilai[:6]}…{nilai[-4:]}" if len(nilai) > 14 else "…"
        hasil.append({"nama": label, "pratinjau": samar, "panjang": len(nilai)})
    return hasil


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)) -> list[dict]:
    # Diurutkan tegas menurut id. Tanpa ini urutannya tidak dijamin, dan
    # halaman yang mengambil "yang terakhir" bisa menampilkan akun lama.
    accounts = (
        db.query(SocialAccount)
        .filter(SocialAccount.is_active.is_(True))
        .order_by(SocialAccount.id)
        .all()
    )
    return [
        {
            "id": a.id,
            "platform": a.platform,
            "display_name": a.display_name or a.session_name,
            "session_name": a.session_name,
            "has_session_id": bool(a.session_id),
            "has_browser_session": a.session_name in session_names(a.platform),
            # Hasil pemeriksaan terakhir, supaya halaman bisa menandai hijau
            # atau merah tanpa memeriksa ulang tiap kali dibuka.
            "verified": (a.extra or {}).get("verified"),
            "verified_at": (a.extra or {}).get("verified_at"),
            "verify_message": (a.extra or {}).get("verify_message", ""),
            # Nama akun yang terbaca saat pemeriksaan terakhir.
            "nama_akun": (a.extra or {}).get("nama_akun", ""),
            # Sudah disamarkan; lihat _pratinjau_cookie.
            "cookie_terisi": _pratinjau_cookie(a.platform, a.session_id or ""),
        }
        for a in accounts
    ]


@router.post("/accounts")
def add_account(req: AccountRequest, db: Session = Depends(get_db)) -> dict:
    if req.platform not in PLATFORMS:
        raise HTTPException(400, f"Platform harus salah satu dari {PLATFORMS}")
    # Satu platform hanya boleh punya satu akun aktif. Tanpa ini, menekan
    # "Ganti" meninggalkan akun lama tetap hidup: halaman menampilkan salah
    # satunya dan pengunggah memakai yang lain, sehingga Session ID ngawur
    # bisa tampak hijau karena yang terbaca akun lama yang masih sah.
    db.query(SocialAccount).filter(
        SocialAccount.platform == req.platform,
        SocialAccount.is_active.is_(True),
    ).update({"is_active": False}, synchronize_session=False)

    account = SocialAccount(
        platform=req.platform,
        display_name=req.display_name,
        session_name=(req.session_name or "default").strip().lower().replace(" ", "_"),
        session_id=req.session_id.strip(),
        extra=req.extra,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return {"id": account.id, "platform": account.platform}


@router.post("/accounts/{account_id}/verify")
async def verify_account(account_id: int, db: Session = Depends(get_db)) -> dict:
    """Buka beranda platform memakai Session ID akun ini.

    Terbuka sebagai akun yang sudah masuk berarti sesinya sah; dilempar ke
    halaman login berarti sudah mati. Hasilnya disimpan supaya halaman bisa
    menandainya tanpa memeriksa ulang setiap kali dibuka.
    """
    account = db.query(SocialAccount).filter(SocialAccount.id == account_id).first()
    if not account:
        raise HTTPException(404, "Akun tidak ditemukan")
    if not account.session_id:
        raise HTTPException(400, "Akun ini tidak punya Session ID untuk diperiksa")

    hasil = await periksa_sesi(account.platform, account.session_id)
    extra = dict(account.extra or {})
    extra.update(
        {
            "verified": bool(hasil["ok"]),
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "verify_message": hasil["pesan"],
            "nama_akun": hasil.get("nama", ""),
        }
    )
    account.extra = extra
    db.commit()
    return {
        "ok": hasil["ok"],
        "message": hasil["pesan"],
        "url": hasil.get("url", ""),
        "nama_akun": hasil.get("nama", ""),
    }


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)) -> dict:
    account = db.query(SocialAccount).filter(SocialAccount.id == account_id).first()
    if not account:
        raise HTTPException(404, "Akun tidak ditemukan")
    account.is_active = False
    db.commit()
    return {"ok": True}


# ------------------------------------------------- login lewat browser

@router.get("/login")
def login_sessions() -> list[dict]:
    """Status sesi browser tiap platform."""
    return login.list_all()


@router.post("/login/{platform}/start")
def login_start(platform: str, session_name: str = "default") -> dict:
    """Buka jendela browser supaya user bisa login manual."""
    result = login.start(platform, session_name)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "gagal membuka browser"))
    return login.status(platform)


@router.post("/login/{platform}/finish")
def login_finish(platform: str) -> dict:
    """Dipanggil setelah user selesai login di jendela browser."""
    return login.finish(platform)


@router.post("/login/{platform}/cancel")
def login_cancel(platform: str) -> dict:
    return login.cancel(platform)


@router.get("/login/{platform}")
def login_status(platform: str) -> dict:
    return login.status(platform)


@router.delete("/login/{platform}/{session_name}")
def login_delete(platform: str, session_name: str) -> dict:
    return login.delete_session(platform, session_name)
