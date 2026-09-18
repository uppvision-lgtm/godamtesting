"""Logika postingan: simpan ke DB, jalankan upload, dan dispatcher jadwal.

Dipakai bersama oleh lapisan HTTP (api.py) dan penjadwal, jadi alurnya sama
untuk upload sekarang maupun upload terjadwal.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autoupload.browser import MediaKind
from autoupload.config import (
    JOB_TIMEOUT_MENIT,
    MAKS_PANJANG_GALAT,
    RIWAYAT_SIMPAN_HARI,
    UPLOAD_DIR,
)
from autoupload.db import Post, PostTarget, SessionLocal, SocialAccount
from autoupload.rahasia import buka
from autoupload.uploader import fan_out_upload

log = logging.getLogger("autoupload")


def _targets_payload(db, post: Post) -> list[dict]:
    """Susun opsi upload per platform dari akun yang tersimpan di DB."""
    payload = []
    for t in post.targets:
        account = (
            db.query(SocialAccount).filter(SocialAccount.id == t.account_id).first()
            if t.account_id
            else db.query(SocialAccount)
            .filter(
                SocialAccount.platform == t.platform,
                SocialAccount.is_active.is_(True),
            )
            .order_by(SocialAccount.id.desc())
            .first()
        )
        extra = dict(account.extra or {}) if account else {}
        payload.append(
            {
                "platform": t.platform,
                "media_path": t.media_path or "",
                "caption": t.caption or "",
                "session_name": account.session_name if account else "default",
                "session_id": buka(account.session_id) if account else "",
                # Diteruskan ke adapter supaya ia memakai fitur jadwal bawaan
                # platformnya. None = kirim sekarang.
                # Disadarkan zonanya: SQLite mengembalikannya polos,
                # dan adapter perlu tahu ini UTC untuk mengubahnya ke
                # waktu lokal peramban.
                "jadwal": (
                    post.scheduled_at.replace(tzinfo=timezone.utc)
                    if post.scheduled_at and post.scheduled_at.tzinfo is None
                    else post.scheduled_at
                ),
                **extra,
            }
        )
    return payload


def create_post(
    db,
    media_path: str,
    caption: str,
    platforms: list[str],
    media_kind: str = "video",
    scheduled_at: datetime | None = None,
    account_ids: dict[str, int] | None = None,
    items: list[dict] | None = None,
) -> Post:
    """Simpan post + targetnya. Belum meng-upload apa pun.

    Dua bentuk sasaran:

    - `platforms` + `account_ids`: satu video dan satu caption untuk semua.
    - `items`: satu baris per akun, masing-masing boleh membawa video dan
      caption sendiri. Dipakai saat menyebar ke banyak akun sekaligus.
    """
    post = Post(
        media_path=media_path,
        media_kind=media_kind,
        caption=caption,
        # Statusnya TIDAK pernah "scheduled" lagi: penjadwalan diserahkan ke
        # fitur jadwal milik tiap sosmed, jadi tidak ada yang perlu ditunggu
        # oleh dispatcher di laptop ini. Waktunya tetap disimpan sebagai
        # catatan dan diteruskan ke adapter.
        status="pending",
        scheduled_at=scheduled_at,
    )
    db.add(post)
    db.flush()
    if items:
        for it in items:
            db.add(
                PostTarget(
                    post_id=post.id,
                    platform=it["platform"],
                    account_id=it.get("account_id"),
                    media_path=(it.get("media_path") or "").strip(),
                    caption=(it.get("caption") or "").strip() or caption,
                )
            )
    else:
        for platform in platforms:
            db.add(
                PostTarget(
                    post_id=post.id,
                    platform=platform,
                    caption=caption,
                    account_id=(account_ids or {}).get(platform),
                )
            )
    db.commit()
    db.refresh(post)
    return post


def _ringkas_galat(pesan: str | None) -> str:
    """Pangkas pesan galat sebelum masuk basis data.

    Jejak Playwright bisa ribuan huruf dan tumpah apa adanya ke layar. Baris
    pertamanya sudah memuat inti masalahnya; sisanya hanya berguna di log.
    """
    if not pesan:
        return ""
    baris = pesan.strip().splitlines()[0].strip()
    if len(baris) <= MAKS_PANJANG_GALAT:
        return baris
    return baris[: MAKS_PANJANG_GALAT - 1].rstrip() + "…"


def _buang_media(jalur: str | None) -> None:
    """Hapus berkas media setelah tidak ada lagi post yang memakainya.

    Tanpa ini folder uploads/ hanya bertambah: satu video tetap tersimpan
    selamanya walau postingannya sudah lama terbit. Hanya berkas DI DALAM
    UPLOAD_DIR yang disentuh, supaya berkas milik pengguna di tempat lain
    tidak ikut terhapus.
    """
    if not jalur:
        return
    berkas = Path(jalur)
    try:
        berkas.relative_to(UPLOAD_DIR)
    except ValueError:
        return  # di luar folder unggahan -> bukan milik kita
    db = SessionLocal()
    try:
        # Dianggap masih dipakai kalau ada post ATAU target yang belum
        # selesai merujuknya. Tanpa memeriksa target, video milik akun lain
        # dalam kiriman yang sama bisa ikut terhapus saat gilirannya belum
        # datang.
        masih = (
            db.query(Post)
            .filter(Post.media_path == jalur)
            .filter(Post.status.in_(("pending", "processing")))
            .count()
        ) + (
            db.query(PostTarget)
            .join(Post, Post.id == PostTarget.post_id)
            .filter(PostTarget.media_path == jalur)
            .filter(Post.status.in_(("pending", "processing")))
            .count()
        )
    finally:
        db.close()
    if masih:
        return
    try:
        berkas.unlink(missing_ok=True)
        log.info("Media %s dihapus setelah postingannya selesai", berkas.name)
    except OSError as exc:
        log.warning("Media %s gagal dihapus: %s", berkas.name, exc)


def bereskan_job_nyangkut() -> int:
    """Tandai gagal semua post yang tertinggal di status "processing".

    Dipanggil saat aplikasi start. Kalau backend mati di tengah upload,
    barisnya tidak pernah dibereskan siapa pun dan halaman menampilkannya
    "sedang dikirim" selamanya.
    """
    batas = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=JOB_TIMEOUT_MENIT)
    db = SessionLocal()
    n = 0
    try:
        for post in db.query(Post).filter(Post.status == "processing").all():
            dibuat = post.created_at
            if dibuat is not None and dibuat.tzinfo is not None:
                dibuat = dibuat.replace(tzinfo=None)
            if dibuat is not None and dibuat > batas:
                continue  # masih mungkin sedang berjalan
            post.status = "failed"
            for row in post.targets:
                if row.status in ("pending", "running"):
                    row.status = "failed"
                    row.error = "Prosesnya berhenti sebelum selesai (backend mati di tengah jalan)."
            n += 1
        if n:
            db.commit()
            log.warning("%s post yang menggantung ditandai gagal", n)
    finally:
        db.close()
    return n


def bersihkan_riwayat(hari: int = RIWAYAT_SIMPAN_HARI) -> int:
    """Buang riwayat lama berikut berkas medianya."""
    batas = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=hari)
    db = SessionLocal()
    jalur: list[str] = []
    try:
        lama = [
            p for p in db.query(Post).all()
            if p.created_at
            and (p.created_at.replace(tzinfo=None) if p.created_at.tzinfo else p.created_at) < batas
        ]
        for post in lama:
            jalur.append(post.media_path)
            jalur.extend(r.media_path for r in post.targets if r.media_path)
            db.query(PostTarget).filter(PostTarget.post_id == post.id).delete()
            db.delete(post)
        if lama:
            db.commit()
    finally:
        db.close()
    for j in jalur:
        _buang_media(j)
    if jalur:
        log.info("%s riwayat lama dibuang", len(jalur))
    return len(jalur)


# Ekstensi yang boleh dibuang oleh pembersih. Berkas lain di folder unggahan
# (mis. .gitkeep) bukan milik kita.
EKST_MEDIA = {
    ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv",
    ".mpg", ".mpeg", ".3gp", ".flv", ".wmv", ".ts", ".m2ts",
    ".jpg", ".jpeg", ".png", ".webp",
}


def bersihkan_berkas_yatim(hari: int = 2) -> int:
    """Buang berkas di uploads/ yang tidak lagi dirujuk post mana pun.

    Pembersihan setelah selesai hanya mengurus berkas yang lewat sejak
    perubahan ini; berkas lama dan berkas dari upload yang gagal di tengah
    jalan tidak pernah tersentuh siapa pun. Hanya yang lebih tua dari `hari`
    yang dibuang, supaya berkas yang baru diunggah dan belum dipakai tidak
    ikut hilang.
    """
    from autoupload.config import DEBUG_SHOTS_DIR, MAKS_DEBUG_SHOT

    batas = datetime.now().timestamp() - hari * 86400
    db = SessionLocal()
    try:
        dipakai = {p.media_path for p in db.query(Post).all() if p.media_path}
        dipakai |= {r.media_path for r in db.query(PostTarget).all() if r.media_path}
    finally:
        db.close()

    n = 0
    if UPLOAD_DIR.is_dir():
        for berkas in UPLOAD_DIR.iterdir():
            if not berkas.is_file() or str(berkas) in dipakai:
                continue
            # Hanya berkas media. Folder ini juga memuat .gitkeep, dan
            # menghapusnya membuat folder kosong itu hilang dari repo.
            if berkas.suffix.lower() not in EKST_MEDIA:
                continue
            try:
                if berkas.stat().st_mtime < batas:
                    berkas.unlink(missing_ok=True)
                    n += 1
            except OSError:
                pass

    # Sekalian pangkas screenshot kegagalan: tanpa ini tumpukan lama baru
    # terpangkas saat ada kegagalan BARU, yang bisa lama sekali.
    if DEBUG_SHOTS_DIR.is_dir():
        berkas = sorted((f for f in DEBUG_SHOTS_DIR.glob("*") if f.is_file()),
                        key=lambda f: f.stat().st_mtime, reverse=True)
        for f in berkas[MAKS_DEBUG_SHOT * 2:]:
            f.unlink(missing_ok=True)
            n += 1

    if n:
        log.info("%s berkas yatim dibuang", n)
    return n


async def run_post(post_id: int) -> list[dict]:
    """Jalankan upload untuk satu post dan catat hasilnya per platform.

    Sesi DB dibuka/ditutup sendiri di sini: upload browser bisa makan waktu
    menit, dan menahan transaksi SQLite selama itu bikin request lain kena
    'database is locked'.
    """
    db = SessionLocal()
    try:
        post = db.query(Post).filter(Post.id == post_id).first()
        if post is None:
            return []
        targets = _targets_payload(db, post)
        media_path, caption, media_kind = post.media_path, post.caption, post.media_kind
        post.status = "processing"
        db.commit()
    finally:
        db.close()

    results = await fan_out_upload(media_path, caption, MediaKind(media_kind), targets)

    db = SessionLocal()
    try:
        post = db.query(Post).filter(Post.id == post_id).first()
        rows = db.query(PostTarget).filter(PostTarget.post_id == post_id).order_by(PostTarget.id).all()
        ok = 0
        for row, result in zip(rows, results):
            row.status = "success" if result.success else "failed"
            row.url = result.url or ""
            row.error = _ringkas_galat(result.error)
            row.attempts += 1
            ok += int(result.success)
        post.status = "done" if ok == len(results) else ("partial" if ok else "failed")
        db.commit()
        jalur = {post.media_path, *(r.media_path for r in rows if r.media_path)}
    finally:
        db.close()

    for j in jalur:
        _buang_media(j)

    return [
        {"platform": r.platform, "success": r.success, "url": r.url, "error": r.error}
        for r in results
    ]


