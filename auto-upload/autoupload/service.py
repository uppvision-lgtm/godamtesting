"""Logika postingan: simpan ke DB, jalankan upload, dan dispatcher jadwal.

Dipakai bersama oleh lapisan HTTP (api.py) dan penjadwal, jadi alurnya sama
untuk upload sekarang maupun upload terjadwal.
"""

import asyncio
import logging
import threading
from datetime import datetime, timezone

from autoupload.browser import MediaKind
from autoupload.config import SCHEDULER_INTERVAL
from autoupload.db import Post, PostTarget, SessionLocal, SocialAccount
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
                "session_name": account.session_name if account else "default",
                "session_id": account.session_id if account else "",
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
) -> Post:
    """Simpan post + target per platform. Belum meng-upload apa pun."""
    post = Post(
        media_path=media_path,
        media_kind=media_kind,
        caption=caption,
        status="scheduled" if scheduled_at else "pending",
        scheduled_at=scheduled_at,
    )
    db.add(post)
    db.flush()
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
            row.error = result.error or ""
            row.attempts += 1
            ok += int(result.success)
        post.status = "done" if ok == len(results) else ("partial" if ok else "failed")
        db.commit()
    finally:
        db.close()

    return [
        {"platform": r.platform, "success": r.success, "url": r.url, "error": r.error}
        for r in results
    ]


async def dispatch_due_posts() -> int:
    """Jalankan semua post terjadwal yang waktunya sudah tiba. Kembalikan jumlahnya."""
    db = SessionLocal()
    try:
        due_ids = [
            p.id
            for p in db.query(Post)
            .filter(Post.status == "scheduled")
            .filter(Post.scheduled_at <= datetime.now(timezone.utc))
            .all()
        ]
    finally:
        db.close()
    for post_id in due_ids:
        log.info("Menjalankan post terjadwal #%s", post_id)
        try:
            await run_post(post_id)
        except Exception as exc:
            log.error("Post terjadwal #%s gagal: %s", post_id, exc)
    return len(due_ids)


def start_scheduler(interval: int = SCHEDULER_INTERVAL) -> threading.Thread:
    """Nyalakan dispatcher jadwal di thread latar (tanpa Redis/Celery).

    Framework-agnostic: panggil sekali saat aplikasi start, dari FastAPI,
    Flask, maupun Django.
    """

    def _loop() -> None:
        while True:
            try:
                asyncio.run(dispatch_due_posts())
            except Exception as exc:  # loop harus tetap hidup
                log.error("Error loop penjadwal: %s", exc)
            import time

            time.sleep(interval)

    thread = threading.Thread(target=_loop, daemon=True, name="autoupload-scheduler")
    thread.start()
    log.info("Dispatcher post terjadwal aktif (tiap %ss)", interval)
    return thread
