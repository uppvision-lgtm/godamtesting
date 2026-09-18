"""Orchestrator: satu postingan disebar paralel ke banyak platform.

Ini inti "script"-nya. Bisa dipakai langsung dari kode Python apa pun
(Flask/Django/cron/CLI) tanpa menyentuh lapisan HTTP:

    from autoupload import upload_sync
    hasil = upload_sync("uploads/video.mp4", "Caption gue", ["tiktok", "twitter"])
"""

import asyncio
import logging
import re
from pathlib import Path

from autoupload.browser import MediaKind, PlatformError, UploadResult, session_names
from autoupload.caption import adapt_caption
from autoupload.config import MAKS_PARALEL
from autoupload.platforms import REGISTRY

log = logging.getLogger("autoupload")



# Galat yang wajar terjadi sesaat: jaringan putus, satu dialog telat muncul,
# browser tertutup di tengah jalan. Diulang SEKALI. Kegagalan lain (sesi mati,
# platform menolak jadwal, media ditolak) tidak diulang karena percobaan kedua
# pasti gagal dengan cara yang sama - dan mengulangnya berarti mengunggah
# videonya dua kali.
_SESAAT = re.compile(
    r"(timeout|timed out|net::|econnreset|econnrefused|target closed|"
    r"target crashed|browser has been closed|connection closed|temporarily)",
    re.IGNORECASE,
)

# Peramban mati di tengah jalan. Di kontainer kecil ini hampir selalu berarti
# kehabisan memori, dan "Target crashed" tidak memberi tahu apa pun soal itu.
_PERAMBAN_MATI = re.compile(r"(target crashed|browser has been closed)", re.IGNORECASE)


def _layak_diulang(hasil) -> bool:
    return not hasil.success and bool(_SESAAT.search(hasil.error or ""))


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
        hasil = await _sekali(t)
        if not hasil.success and _PERAMBAN_MATI.search(hasil.error or ""):
            hasil.error = (
                "Peramban mati di tengah upload - biasanya karena servernya "
                "kehabisan memori saat beberapa akun dikerjakan bersamaan. "
                "Kurangi AUTOUPLOAD_MAKS_PARALEL, atau kirim ke lebih sedikit "
                f"akun sekaligus. ({(hasil.error or '')[:80]})"
            )
        if _layak_diulang(hasil):
            log.warning("%s gagal sesaat (%s); diulang sekali",
                        t.get("platform"), (hasil.error or "")[:80])
            await asyncio.sleep(5)
            hasil = await _sekali(t)
        return hasil

    def _pelapor(target_id):
        """Callback yang dipakai adapter untuk melaporkan kemajuannya.

        Ditulis langsung ke baris targetnya supaya halaman bisa menanyakannya
        tanpa perlu saluran terpisah. Kegagalan menulis SENGAJA diabaikan:
        laporan kemajuan tidak boleh sampai menggagalkan uploadnya.
        """
        def lapor(persen: int, teks: str) -> None:
            if target_id is None:
                return
            try:
                from autoupload.db import PostTarget, SessionLocal

                db = SessionLocal()
                try:
                    baris = db.query(PostTarget).filter(PostTarget.id == target_id).first()
                    # Tidak pernah mundur: dua langkah bisa dilaporkan tidak
                    # berurutan saat ada percobaan ulang, dan angka yang turun
                    # membuat orang mengira prosesnya mengulang dari awal.
                    if baris is not None and persen > (baris.progress or 0):
                        baris.progress = min(persen, 99)
                        baris.tahap = teks[:80]
                        db.commit()
                        # Dicatat juga di log: sebagian tahap berlalu dalam
                        # sepersekian detik dan tidak akan pernah tertangkap
                        # oleh halaman yang menanyakan status tiap 3 detik.
                        log.info("[%s] %s%% - %s", baris.platform, persen, teks)
                finally:
                    db.close()
            except Exception:  # noqa: BLE001 - kemajuan hanyalah hiasan
                pass

        return lapor

    async def _sekali(t: dict) -> UploadResult:
        t = dict(t)
        # Tiap target boleh membawa video dan captionnya sendiri; yang tidak
        # membawanya memakai milik Post. Inilah yang membuat satu kiriman bisa
        # menyebar ke banyak akun dengan video berbeda-beda.
        media_t = t.pop("media_path", "") or media_path
        caption_t = t.pop("caption", "") or caption
        t["lapor"] = _pelapor(t.pop("target_id", None))
        return await upload_one(
            media_t,
            caption_t,
            t.pop("platform"),
            media_kind,
            session_name=t.pop("session_name", "default") or "default",
            session_id=t.pop("session_id", "") or "",
            headless=bool(t.pop("headless", True)),
            adapt=bool(t.pop("adapt", True)),
            **t,
        )

    # Dibatasi: seratus Chromium sekaligus akan mematikan mesinnya. Yang lain
    # mengantre dan tetap dikerjakan, hanya bergiliran.
    gerbang = asyncio.Semaphore(MAKS_PARALEL)

    async def _antre(t: dict) -> UploadResult:
        async with gerbang:
            return await _run(t)

    return list(await asyncio.gather(*[_antre(t) for t in targets]))


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
