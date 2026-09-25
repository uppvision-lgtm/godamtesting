import os
"""Lapisan HTTP: satu APIRouter berisi semua endpoint.

Kalau website utamamu FastAPI:
    from autoupload import router
    app.include_router(router)

Kalau Flask/Django: jalankan `python main.py` sebagai service kecil di port
sendiri, atau panggil `upload_sync()` langsung dari view tanpa lewat HTTP.
"""

import asyncio
import logging
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from autoupload import login, masuk
from autoupload.browser import session_names
import hashlib

from autoupload.config import (
    MAKS_AKUN_PER_PLATFORM,
    MAX_FILE_SIZE,
    PLATFORMS,
    UPLOAD_DIR,
)
from autoupload.db import Post, PostTarget, SessionLocal, SocialAccount, get_db
from autoupload.platforms import REGISTRY
from autoupload.rahasia import buka, sandikan
from autoupload.service import _buang_media, bersihkan_riwayat, create_post, run_post
from autoupload.verify import periksa_sesi

log = logging.getLogger("autoupload")

# Awalan alamat bisa digeser lewat environment. Dipakai saat paket ini
# dipasang di dalam website utama, yang sudah punya "/api/upload" sendiri untuk
# hal lain; di sana awalannya jadi "/api/sosmed". Dijalankan sendiri lewat
# main.py, awalannya tetap "/api" seperti semula.
API_PREFIX = os.getenv("AUTOUPLOAD_API_PREFIX", "/api")

router = APIRouter(prefix=API_PREFIX, tags=["autoupload"])

# Format video yang boleh diunggah. Daftar ini sengaja lebar: pemilih berkas
# memakai accept="video/*", jadi apa pun yang dianggap video oleh sistem bisa
# dipilih, dan menolaknya di server hanya membuat orang bingung kenapa
# videonya "tidak didukung".
VIDEO_EXT = {
    ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv",
    ".mpg", ".mpeg", ".3gp", ".flv", ".wmv", ".ts", ".m2ts",
}
# Yang boleh dikirim apa adanya ke platform. Sisanya diubah dulu: Instagram,
# Threads dan X hanya menerima MP4/MOV, jadi menerima .mkv tanpa mengubahnya
# hanya memindahkan kegagalan ke tengah proses upload.
VIDEO_LANGSUNG = {".mp4", ".mov"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}


# ---------------------------------------------------------------- pemilik


def pemilik_saat_ini() -> str:
    """Username pemilik isi yang sedang diakses.

    Bawaannya kosong: paket ini bisa dijalankan berdiri sendiri, tanpa sistem
    akun, dan di situ semua isi memang milik satu orang. Saat dipasang ke
    website, main.py MENIMPA dependensi ini lewat `dependency_overrides`
    dengan username yang sedang masuk - itulah yang memisahkan akun sosmed
    milik satu pengguna dari pengguna lain.
    """
    return ""


def _milik(kueri, model, pemilik: str):
    """Batasi kueri ke baris milik `pemilik` saja."""
    return kueri.filter(model.owner == pemilik)


def _jalur_aman(jalur: str) -> Path:
    """Pastikan berkas yang diminta benar-benar ada DI DALAM folder unggahan.

    `media_path` datang dari klien. Tanpa pemeriksaan ini, siapa pun yang
    sudah masuk bisa menyebut jalur apa pun di server - termasuk basis data
    berisi Session ID semua akun - dan menyuruh server mengunggahnya ke akun
    sosmed miliknya sendiri. Jalurnya diselesaikan dulu (symlink ikut
    ditelusuri) supaya "uploads/../../rahasia.db" tidak lolos.
    """
    try:
        nyata = Path(jalur).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(400, f"File tidak ditemukan: {jalur}") from None
    if not nyata.is_relative_to(UPLOAD_DIR.resolve()):
        raise HTTPException(400, "File harus berada di folder unggahan server.")
    return nyata


# ---------------------------------------------------------------- skema

class ItemUpload(BaseModel):
    """Satu akun tujuan, dengan video dan captionnya sendiri."""

    account_id: int
    media_path: str = ""     # kosong = pakai media utama
    caption: str = ""        # kosong = pakai caption utama


class UploadRequest(BaseModel):
    media_path: str
    caption: str = ""
    # Tidak wajib kalau `items` dipakai: di sana platform tiap baris
    # sudah ditentukan oleh akunnya sendiri.
    platforms: list[str] = Field(default_factory=list)
    media_kind: str = Field("video", pattern="^(video|photo)$")
    scheduled_at: str | None = None          # ISO datetime, kosong = sekarang
    account_ids: dict[str, int] = Field(default_factory=dict)  # platform -> id akun
    # Kalau True, permintaan ditahan sampai uploadnya selesai. Dipakai
    # skrip/CLI; halaman web memakai False lalu menanyakan statusnya.
    tunggu: bool = False
    # Kirim ke banyak akun sekaligus, satu baris per akun. Kalau diisi,
    # `platforms` dan `account_ids` diabaikan.
    items: list[ItemUpload] = Field(default_factory=list)


class AccountRequest(BaseModel):
    platform: str
    display_name: str = ""
    session_name: str = "default"
    session_id: str = ""                     # opsional: jalan pintas cookie
    extra: dict = Field(default_factory=dict)


class MasukRequest(BaseModel):
    """Login dengan username & password; password hanya lewat, tidak disimpan."""

    platform: str
    display_name: str = ""
    session_name: str = "default"
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------- info

@router.get("/info")
def info() -> dict:
    """Keterangan yang dibutuhkan halaman sebelum mengunggah.

    `unggah_langsung` adalah alamat backend ini dilihat dari luar. Halaman
    memakainya untuk mengirim berkas LANGSUNG ke sini, melewati proxy Next di
    Vercel yang membatasi body sekitar 4,5 MB - batas yang membuat video
    berukuran wajar ditolak dengan 413 tanpa keterangan apa pun.

    Dikirim saat berjalan, bukan ditanam saat build, supaya alamat backend
    yang berubah tidak menuntut frontend dibangun ulang.
    """
    alamat = os.getenv("AUTOUPLOAD_PUBLIC_URL", "").strip().rstrip("/")
    if not alamat:
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if domain:
            alamat = f"https://{domain}"
    return {
        "unggah_langsung": alamat,
        "maks_berkas_mb": MAX_FILE_SIZE // 1024 // 1024,
        "video_ext": sorted(VIDEO_EXT),
        "foto_ext": sorted(PHOTO_EXT),
    }


# ---------------------------------------------------------------- media

def _pastikan_kuota_media(sidik: str, pemilik: str) -> None:
    """Jatah folder unggahan sosmed per pemilik.

    Berkas di sini bertahan sampai riwayatnya dihapus, jadi tanpa batas satu
    orang bisa menghabiskan volume server untuk semua orang.
    """
    # 3072 MB dulu: enam kali lipat seluruh volume Railway (500 MB), jadi
    # jatah itu tidak pernah bisa ditepati. Folder ini juga TIDAK ikut disapu
    # penjaga volume karena isinya sengaja disimpan sampai riwayatnya dihapus.
    batas = int(os.getenv("KUOTA_MEDIA_MB", "250")) * 1_048_576
    dipakai = 0
    for berkas in UPLOAD_DIR.glob(f"{sidik}_*"):
        try:
            dipakai += berkas.stat().st_size
        except OSError:
            continue
    if dipakai >= batas:
        raise HTTPException(
            413,
            f"Jatah penyimpanan video sudah terpakai {dipakai / 1_048_576:.0f} MB "
            f"dari {batas / 1_048_576:.0f} MB. Hapus beberapa riwayat unggahan dulu.",
        )


@router.post("/media")
async def upload_media(
    file: UploadFile = File(...), pemilik: str = Depends(pemilik_saat_ini)
) -> dict:
    """Simpan file media, kembalikan `media_path` untuk dipakai POST /api/upload."""
    suffix = Path(file.filename or "media").suffix.lower()
    if suffix not in VIDEO_EXT | PHOTO_EXT:
        raise HTTPException(400, f"Tipe file '{suffix}' tidak didukung")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Nama berkas dibubuhi sidik pemiliknya supaya pemakaian per orang bisa
    # dihitung tanpa tabel tambahan - cukup menjumlah berkas berawalan sama.
    sidik = hashlib.sha256((pemilik or "tamu").encode()).hexdigest()[:8]
    _pastikan_kuota_media(sidik, pemilik)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = UPLOAD_DIR / f"{sidik}_{stamp}_{uuid.uuid4().hex[:8]}{suffix}"

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

    if suffix in VIDEO_EXT and suffix not in VIDEO_LANGSUNG:
        dest = await asyncio.to_thread(_jadikan_mp4, dest)
        size = dest.stat().st_size

    return {
        "media_path": str(dest),
        "size_bytes": size,
        "media_kind": "video" if suffix in VIDEO_EXT else "photo",
    }


def _codec_video(berkas: Path) -> str:
    """Nama codec video berkas, atau "" kalau tidak terbaca."""
    hasil = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(berkas)],
        capture_output=True, text=True,
    )
    return hasil.stdout.strip().lower() if hasil.returncode == 0 else ""


def _jadikan_mp4(asal: Path) -> Path:
    """Ubah video ke MP4 supaya platformnya mau menerimanya.

    Dicoba salin-aliran dulu (tanpa menyandi ulang): untuk .mkv atau .m4v yang
    isinya sudah H.264/AAC, ini hanya menukar wadahnya dan selesai dalam
    hitungan detik tanpa menurunkan mutu. Kalau codec-nya tidak cocok, barulah
    disandikan ulang.
    """
    tujuan = asal.with_suffix(".mp4")
    if tujuan.exists() and tujuan != asal:
        tujuan = asal.with_name(f"{asal.stem}-mp4.mp4")

    sandi_ulang = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    # Salin-aliran HANYA kalau videonya sudah H.264. Wadah MP4 menerima codec
    # lain (mpeg4 dari .avi, misalnya), tetapi Instagram dan TikTok tidak -
    # jadi menyalinnya begitu saja hanya memindahkan kegagalan ke tengah
    # proses upload, saat videonya sudah terlanjur terkirim.
    percobaan = (
        ([ "-c", "copy", "-movflags", "+faststart"], sandi_ulang)
        if _codec_video(asal) == "h264"
        else (sandi_ulang,)
    )
    for opsi in percobaan:
        hasil = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(asal),
             *opsi, str(tujuan)],
            capture_output=True, text=True,
        )
        if hasil.returncode == 0 and tujuan.exists() and tujuan.stat().st_size > 0:
            asal.unlink(missing_ok=True)
            log.info("%s diubah ke MP4 (%s)", asal.name, "salin" if "copy" in opsi else "sandi ulang")
            return tujuan
        tujuan.unlink(missing_ok=True)

    asal.unlink(missing_ok=True)
    raise HTTPException(
        400,
        f"Video '{asal.suffix}' tidak bisa diubah ke MP4. Coba ekspor ulang "
        "videonya sebagai MP4 (H.264) lalu unggah lagi.",
    )


# ---------------------------------------------------------------- upload

@router.post("/upload")
async def upload(
    req: UploadRequest,
    db: Session = Depends(get_db),
    pemilik: str = Depends(pemilik_saat_ini),
) -> dict:
    """Upload sekarang (tunggu selesai) atau simpan sebagai jadwal."""
    daftar: list[dict] = []
    if req.items:
        akun_map = {
            a.id: a
            for a in db.query(SocialAccount)
            .filter(
                SocialAccount.id.in_([i.account_id for i in req.items]),
                SocialAccount.owner == pemilik,
            )
            .all()
        }
        for it in req.items:
            akun = akun_map.get(it.account_id)
            if akun is None or not akun.is_active:
                raise HTTPException(400, f"Akun #{it.account_id} tidak ada atau tidak aktif")
            jalur = str(_jalur_aman(it.media_path or req.media_path))
            teks = (it.caption or req.caption).strip()
            if not teks:
                raise HTTPException(400, f"Caption untuk akun #{it.account_id} kosong")
            daftar.append({
                "platform": akun.platform,
                "account_id": akun.id,
                "media_path": jalur,
                "caption": teks,
            })
        if not daftar:
            raise HTTPException(400, "Belum ada akun tujuan")
    else:
        unknown = [p for p in req.platforms if p not in PLATFORMS]
        if not req.platforms or unknown:
            raise HTTPException(400, f"Platform tidak valid: {unknown or 'kosong'}")
        if not req.caption.strip():
            raise HTTPException(400, "Caption wajib diisi")
        _jalur_aman(req.media_path)

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
        owner=pemilik,
        media_path=req.media_path,
        caption=req.caption.strip(),
        platforms=req.platforms,
        media_kind=req.media_kind,
        scheduled_at=run_at,
        account_ids=req.account_ids,
        items=daftar or None,
    )
    # Dijalankan SEKARANG walau dijadwalkan: yang menunda adalah fitur jadwal
    # milik platformnya, bukan proses di laptop ini. Jadi kalau laptop mati
    # nanti, postingan tetap terbit pada waktunya.
    if req.tunggu:
        results = await run_post(post.id)
        db.expire_all()
        status = db.query(Post).filter(Post.id == post.id).first().status
        return {"post_id": post.id, "status": status, "results": results}

    # Upload berjalan MENITAN (Instagram pernah 131 detik). Menahan koneksi
    # selama itu membuatnya mustahil lewat proxy Vercel yang memutus di 60
    # detik, dan membuat menutup tab berarti kehilangan hasilnya. Jadi
    # jobnya dilepas ke latar belakang dan halaman menanyakan statusnya.
    asyncio.create_task(_jalankan_di_latar(post.id))
    return {"post_id": post.id, "status": "processing", "results": []}


async def _jalankan_di_latar(post_id: int) -> None:
    """Jalankan satu post, dan pastikan galatnya tidak hilang diam-diam.

    Task latar yang melempar hanya menghasilkan peringatan asyncio yang tidak
    terbaca siapa pun, dan barisnya akan menggantung di "processing".
    """
    try:
        await run_post(post_id)
    except Exception:  # noqa: BLE001
        log.exception("Post #%s gagal di latar belakang", post_id)
        db = SessionLocal()
        try:
            post = db.query(Post).filter(Post.id == post_id).first()
            if post is not None and post.status == "processing":
                post.status = "failed"
                for row in post.targets:
                    if row.status in ("pending", "running"):
                        row.status = "failed"
                        row.error = "Prosesnya berhenti karena galat tak terduga."
                db.commit()
        finally:
            db.close()


@router.get("/posts")
def list_posts(
    limit: int = 20,
    db: Session = Depends(get_db),
    pemilik: str = Depends(pemilik_saat_ini),
) -> list[dict]:
    """Riwayat + jadwal postingan terbaru milik pengguna ini."""
    posts = (
        _milik(db.query(Post), Post, pemilik)
        .order_by(Post.id.desc())
        .limit(min(limit, 100))
        .all()
    )
    return [_post_dict(p) for p in posts]


def _post_dict(p: Post) -> dict:
    return {
        "id": p.id,
        "status": p.status,
        "media_kind": p.media_kind,
        "caption": (p.caption or "")[:120],
        "scheduled_at": p.scheduled_at,
        "created_at": p.created_at,
        "targets": [
            {
                "id": t.id,
                "platform": t.platform,
                "account_id": t.account_id,
                "status": t.status,
                "progress": t.progress or 0,
                "tahap": t.tahap or "",
                "url": t.url,
                "error": t.error,
            }
            for t in p.targets
        ],
    }


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

    # Dipecah PERSIS seperti session_cookies() di browser.py: "|" utama,
    # ":" sebagai kebiasaan lama. Kalau di sini berbeda, pratinjaunya
    # memperlihatkan satu bongkahan sementara pengunggah memakainya sebagai
    # beberapa cookie - dan orang tidak akan tahu bagian mana yang salah.
    if "|" in session_id:
        bagian = session_id.split("|")
    elif len(nama) > 1 and ":" in session_id:
        bagian = session_id.split(":")
    else:
        bagian = [session_id]
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


@router.get("/posts/{post_id}")
def get_post(
    post_id: int,
    db: Session = Depends(get_db),
    pemilik: str = Depends(pemilik_saat_ini),
) -> dict:
    """Status satu postingan. Dipakai halaman untuk menanyakan kemajuan."""
    post = _milik(db.query(Post).filter(Post.id == post_id), Post, pemilik).first()
    if post is None:
        raise HTTPException(404, "Postingan tidak ditemukan")
    return _post_dict(post)


@router.delete("/posts/{post_id}")
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    pemilik: str = Depends(pemilik_saat_ini),
) -> dict:
    """Hapus satu riwayat berikut berkas medianya."""
    post = _milik(db.query(Post).filter(Post.id == post_id), Post, pemilik).first()
    if post is None:
        raise HTTPException(404, "Postingan tidak ditemukan")
    if post.status == "processing":
        raise HTTPException(409, "Postingan ini sedang dikirim; tunggu sampai selesai.")
    jalur = [post.media_path, *(r.media_path for r in post.targets if r.media_path)]
    db.query(PostTarget).filter(PostTarget.post_id == post.id).delete()
    db.delete(post)
    db.commit()
    for j in jalur:
        _buang_media(j)
    return {"deleted": post_id}


@router.delete("/posts")
def clear_posts(
    db: Session = Depends(get_db), pemilik: str = Depends(pemilik_saat_ini)
) -> dict:
    """Kosongkan riwayat milik pengguna ini, kecuali yang sedang berjalan."""
    n = 0
    jalur: list[str] = []
    for post in _milik(
        db.query(Post).filter(Post.status != "processing"), Post, pemilik
    ).all():
        jalur.append(post.media_path)
        jalur.extend(r.media_path for r in post.targets if r.media_path)
        db.query(PostTarget).filter(PostTarget.post_id == post.id).delete()
        db.delete(post)
        n += 1
    db.commit()
    for j in jalur:
        _buang_media(j)
    return {"deleted": n}


@router.get("/accounts")
def list_accounts(
    db: Session = Depends(get_db), pemilik: str = Depends(pemilik_saat_ini)
) -> list[dict]:
    # Diurutkan tegas menurut id. Tanpa ini urutannya tidak dijamin, dan
    # halaman yang mengambil "yang terakhir" bisa menampilkan akun lama.
    accounts = (
        db.query(SocialAccount)
        .filter(SocialAccount.is_active.is_(True), SocialAccount.owner == pemilik)
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
            "cookie_terisi": _pratinjau_cookie(a.platform, buka(a.session_id or "")),
        }
        for a in accounts
    ]


def _simpan_akun(
    db: Session, pemilik: str, platform: str, display_name: str,
    session_name: str, session_id: str, extra: dict,
) -> SocialAccount:
    """Simpan satu akun; dipakai jalur SSID maupun jalur username & password."""
    if platform not in PLATFORMS:
        raise HTTPException(400, f"Platform harus salah satu dari {PLATFORMS}")
    # Satu platform boleh punya BANYAK akun aktif sekaligus. Yang dinonaktifkan
    # hanya akun dengan nama sesi yang sama, supaya menekan "Ganti" pada satu
    # akun tetap mengganti akun itu - bukan menumpuk duplikat yang membuat
    # halaman dan pengunggah memakai baris berbeda.
    sesi = (session_name or "default").strip().lower().replace(" ", "_")
    db.query(SocialAccount).filter(
        SocialAccount.platform == platform,
        SocialAccount.session_name == sesi,
        SocialAccount.is_active.is_(True),
        SocialAccount.owner == pemilik,
    ).update({"is_active": False}, synchronize_session=False)

    terpakai = (
        db.query(SocialAccount)
        .filter(
            SocialAccount.platform == platform,
            SocialAccount.is_active.is_(True),
            SocialAccount.owner == pemilik,
        )
        .count()
    )
    if terpakai >= MAKS_AKUN_PER_PLATFORM:
        raise HTTPException(
            400,
            f"{platform} sudah punya {terpakai} akun tertaut "
            f"(batas {MAKS_AKUN_PER_PLATFORM}). Putuskan salah satunya dulu.",
        )

    account = SocialAccount(
        owner=pemilik,
        platform=platform,
        display_name=display_name,
        session_name=sesi,
        session_id=sandikan(session_id.strip()),
        extra=extra,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.post("/accounts")
def add_account(
    req: AccountRequest,
    db: Session = Depends(get_db),
    pemilik: str = Depends(pemilik_saat_ini),
) -> dict:
    account = _simpan_akun(
        db, pemilik, req.platform, req.display_name, req.session_name, req.session_id, req.extra
    )
    return {"id": account.id, "platform": account.platform}


@router.post("/accounts/masuk")
async def add_account_masuk(
    req: MasukRequest,
    db: Session = Depends(get_db),
    pemilik: str = Depends(pemilik_saat_ini),
) -> dict:
    """Tautkan akun dengan username & password.

    Server login sekali di peramban tanpa jendela, mengambil cookie sesinya,
    lalu menyimpannya persis seperti Session ID yang ditempel - password
    tidak disimpan. Kegagalan (2FA, captcha, sandi salah) dijawab 400 dengan
    kalimat yang menyebut sebabnya.
    """
    if req.platform not in PLATFORMS:
        raise HTTPException(400, f"Platform harus salah satu dari {PLATFORMS}")
    try:
        hasil = await masuk.masuk_dengan_sandi(
            req.platform, req.username, req.password, jendela=_di_lokal()
        )
    except masuk.MasukGagal as error:
        raise HTTPException(400, str(error)) from None
    account = _simpan_akun(
        db, pemilik, req.platform, req.display_name, req.session_name,
        hasil["session_id"], {"nama_akun": hasil.get("nama_akun", ""), "sumber": "sandi"},
    )
    return {"id": account.id, "platform": account.platform, "nama_akun": hasil.get("nama_akun", "")}


def _di_lokal() -> bool:
    """True saat backend berjalan di laptop (DEBUG), yang punya layar."""
    return os.getenv("DEBUG", "").strip().lower() in ("1", "true", "yes")


# Server tujuan tombol "Kirim ke server". Diambil dari setelan, TIDAK dari
# permintaan: kalau alamatnya bisa diisi dari luar, session ID bisa dikirim
# ke server mana pun.
SERVER_TUJUAN = os.getenv(
    "AUTOUPLOAD_SERVER_URL", "https://backend-production-bf483.up.railway.app"
).rstrip("/")


class KirimRequest(BaseModel):
    """Akun SITUS di server (bukan akun sosmed); dipakai sekali untuk login."""

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@router.get("/lokal")
def info_lokal() -> dict:
    """Halaman memakai ini untuk menampilkan fitur khusus localhost."""
    return {"lokal": _di_lokal(), "server": SERVER_TUJUAN if _di_lokal() else ""}


@router.post("/accounts/{account_id}/kirim-ke-server")
async def kirim_ke_server(
    account_id: int,
    req: KirimRequest,
    db: Session = Depends(get_db),
    pemilik: str = Depends(pemilik_saat_ini),
) -> dict:
    """Salin satu akun dari backend LOKAL ke server, lalu periksa di sana.

    Gunanya: login sosmed yang butuh captcha dikerjakan di laptop (jendela
    terlihat, IP rumah yang lebih dipercaya), hasilnya dipakai di server.
    Session ID dikirim langsung backend-ke-backend lewat HTTPS dan tidak
    pernah lewat peramban. Token lokal tidak berlaku di server (kunci
    tanda tangannya beda), jadi backend lokal login ke server dengan akun
    situs yang diberikan - password itu hanya dipakai sekali, tidak disimpan.
    """
    if not _di_lokal():
        raise HTTPException(404, "Hanya tersedia di backend lokal.")
    account = _milik(
        db.query(SocialAccount).filter(SocialAccount.id == account_id), SocialAccount, pemilik
    ).first()
    if not account or not account.session_id:
        raise HTTPException(404, "Akun tidak ditemukan atau tidak punya Session ID.")

    import httpx

    async with httpx.AsyncClient(base_url=SERVER_TUJUAN, timeout=httpx.Timeout(300.0, connect=20.0)) as c:
        r = await c.post("/api/auth/login", json={"username": req.username, "password": req.password})
        if r.status_code != 200:
            raise HTTPException(400, "Login ke server gagal: username atau password situs salah.")
        token = r.json().get("token", "")
        kepala = {"Authorization": f"Bearer {token}"}
        r = await c.post(
            "/api/sosmed/accounts",
            headers=kepala,
            json={
                "platform": account.platform,
                "display_name": account.display_name or account.platform,
                "session_name": account.session_name or "ssid",
                "session_id": buka(account.session_id),
                "extra": {k: v for k, v in (account.extra or {}).items() if k == "nama_akun"},
            },
        )
        if r.status_code != 200:
            detail = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("detail")
            raise HTTPException(400, f"Server menolak menyimpan akunnya: {detail or r.status_code}")
        id_server = r.json().get("id")
        periksa = {}
        try:
            v = await c.post(f"/api/sosmed/accounts/{id_server}/verify", headers=kepala)
            periksa = v.json() if v.status_code == 200 else {}
        except httpx.HTTPError:
            periksa = {}
    return {
        "ok": True,
        "id_server": id_server,
        "periksa_ok": periksa.get("ok"),
        "periksa_pesan": periksa.get("message", "Belum diperiksa di server."),
        "nama_akun": periksa.get("nama_akun", ""),
    }


@router.post("/accounts/{account_id}/verify")
async def verify_account(
    account_id: int,
    db: Session = Depends(get_db),
    pemilik: str = Depends(pemilik_saat_ini),
) -> dict:
    """Buka beranda platform memakai Session ID akun ini.

    Terbuka sebagai akun yang sudah masuk berarti sesinya sah; dilempar ke
    halaman login berarti sudah mati. Hasilnya disimpan supaya halaman bisa
    menandainya tanpa memeriksa ulang setiap kali dibuka.
    """
    account = _milik(
        db.query(SocialAccount).filter(SocialAccount.id == account_id), SocialAccount, pemilik
    ).first()
    if not account:
        raise HTTPException(404, "Akun tidak ditemukan")
    if not account.session_id:
        raise HTTPException(400, "Akun ini tidak punya Session ID untuk diperiksa")

    hasil = await periksa_sesi(account.platform, buka(account.session_id))
    extra = dict(account.extra or {})
    extra.update(
        {
            "verified": bool(hasil["ok"]),
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "verify_message": hasil["pesan"],
            # Nama yang tidak terbaca kali ini tidak menghapus nama yang sudah
            # diketahui (mis. dari login lewat perangkat).
            # ...kecuali akunnya ditangguhkan: nama yang tersimpan saat itu
            # berasal dari halaman penangguhan ("Instagram user"), bukan akun.
            "nama_akun": ""
            if "DITANGGUHKAN" in hasil["pesan"]
            else (hasil.get("nama", "") or extra.get("nama_akun", "")),
        }
    )
    account.extra = extra
    db.commit()
    return {
        "ok": hasil["ok"],
        "message": hasil["pesan"],
        "url": hasil.get("url", ""),
        "nama_akun": extra["nama_akun"],
    }


@router.delete("/accounts/{account_id}")
def delete_account(
    account_id: int,
    db: Session = Depends(get_db),
    pemilik: str = Depends(pemilik_saat_ini),
) -> dict:
    account = _milik(
        db.query(SocialAccount).filter(SocialAccount.id == account_id), SocialAccount, pemilik
    ).first()
    if not account:
        raise HTTPException(404, "Akun tidak ditemukan")
    account.is_active = False
    # Session ID ikut dibuang. Akun yang sudah diputuskan tidak punya alasan
    # menyimpan kredensial, dan menyimpannya hanya menambah yang bisa bocor.
    account.session_id = ""
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
