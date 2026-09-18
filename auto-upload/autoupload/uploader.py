"""Orchestrator: satu postingan disebar paralel ke banyak platform.

Ini inti "script"-nya. Bisa dipakai langsung dari kode Python apa pun
(Flask/Django/cron/CLI) tanpa menyentuh lapisan HTTP:

    from autoupload import upload_sync
    hasil = upload_sync("uploads/video.mp4", "Caption gue", ["tiktok", "twitter"])
"""

import asyncio
from pathlib import Path

from autoupload.browser import MediaKind, PlatformError, UploadResult, session_names
from autoupload.caption import adapt_caption
from autoupload.platforms import REGISTRY


async def upload_one(
    media_path: str | Path,
    caption: str,
    platform: str,
    media_kind: MediaKind = MediaKind.VIDEO,
    *,
    session_name: str = "default",
    session_id: str = "",
    headless: bool = True,
    adapt: bool = True,
    **options: object,
) -> UploadResult:
    """Upload ke satu platform. Tidak pernah raise - error dikembalikan di hasil."""
    cls = REGISTRY.get(platform)
    if cls is None:
        return UploadResult(platform=platform, success=False, error=f"platform tidak dikenal: {platform}")
    if not session_id:
        # Profil yang diminta tidak ada tapi platform punya sesi lain (mis.
        # hasil login lama bernama "twitter-1") -> pakai itu, jangan gagal.
        available = session_names(platform)
        if session_name not in available and available:
            session_name = available[0]
    try:
        adapter = cls(session_name=session_name, headless=headless)
        final_caption = adapt_caption(platform, caption) if adapt else caption
        return await adapter.upload(
            Path(media_path), final_caption, media_kind, session_id=session_id, **options
        )
    except PlatformError as exc:
        return UploadResult(platform=platform, success=False, error=str(exc))
    except Exception as exc:
        return UploadResult(platform=platform, success=False, error=f"{type(exc).__name__}: {exc}")


async def fan_out_upload(
    media_path: str | Path,
    caption: str,
    media_kind: MediaKind,
    targets: list[dict],
) -> list[UploadResult]:
    """Upload ke semua platform yang diminta secara paralel.

    Isi tiap item `targets`: {platform, session_name?, session_id?, headless?,
    adapt?, jadwal?, + opsi khusus platform (mis. visibility untuk YouTube)}.

    `jadwal` (datetime, boleh None) diteruskan apa adanya ke adapter, yang
    memakainya lewat fitur penjadwalan bawaan platform masing-masing.
    """
    async def _run(t: dict) -> UploadResult:
        t = dict(t)
        return await upload_one(
            media_path,
            caption,
            t.pop("platform"),
            media_kind,
            session_name=t.pop("session_name", "default") or "default",
            session_id=t.pop("session_id", "") or "",
            headless=bool(t.pop("headless", True)),
            adapt=bool(t.pop("adapt", True)),
            **t,
        )

    return list(await asyncio.gather(*[_run(t) for t in targets]))


def upload_sync(
    media_path: str | Path,
    caption: str,
    platforms: list[str],
    media_kind: str = "video",
    **common: object,
) -> list[UploadResult]:
    """Versi blocking untuk dipanggil dari kode sync (Flask/Django/cron).

    Jangan dipakai di dalam event loop yang sudah jalan - pakai
    `fan_out_upload` (async) di sana.
    """
    targets = [{"platform": p, **common} for p in platforms]
    return asyncio.run(fan_out_upload(media_path, caption, MediaKind(media_kind), targets))
