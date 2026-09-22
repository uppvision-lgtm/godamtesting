"""Live browser run (session-id based).

Opens a Playwright browser, authenticates using the Instagram ``sessionid``
cookie, then runs the whole comment job on the visible page while streaming
JPEG frames and a step log to the frontend. The user can therefore watch the
bot work from start to finish without needing a CAPTCHA.

Semua pekerjaan Playwright dijalankan di SATU thread khusus dengan event loop
sendiri (lihat ``_BrowserRuntime``). Dua alasan:

1. Windows. Loop default uvicorn (Selector) tidak bisa menjalankan subprocess,
   sehingga Chromium gagal dibuka dengan ``NotImplementedError`` tanpa pesan.
   Loop di thread ini selalu Proactor.
2. Hemat memori. Objek Playwright terikat pada loop tempat ia dibuat, jadi satu
   loop bersama adalah syarat agar 1 Chromium bisa dipakai banyak tab sekaligus.
"""

import asyncio
import logging
import os
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from playwright.async_api import async_playwright

from browser_config import (
    blocked_resource_types,
    browser_args,
    frame_idle_ms,
    frame_interval_ms,
    frame_quality,
    viewport,
)

logger = logging.getLogger("live")
from auth_api import identitas

router = APIRouter(prefix="/api/live", tags=["live"])
_SESSIONS: dict[str, "LiveSession"] = {}

# Platform yang bisa dijalankan bot komentar. Alurnya sama persis; yang beda
# hanya modul DOM-nya. Menambah platform = menambah satu baris di sini.
PLATFORM_KOMENTAR: dict[str, str] = {"instagram": "tasks", "threads": "tasks_threads"}


def _modul_bot(platform: str):
    """Modul bot untuk platform itu: punya _add_session_cookie dan _process_posts."""
    import importlib

    nama = PLATFORM_KOMENTAR.get(platform)
    if not nama:
        raise HTTPException(
            status_code=422,
            detail=f"platform harus salah satu dari: {', '.join(PLATFORM_KOMENTAR)}",
        )
    return importlib.import_module(nama)

# Batas browser yang boleh jalan bersamaan. Default 0 = tanpa batas: berapa pun
# bot yang diminta akan dijalankan. Setiap sesi memakan satu Chromium (±300MB
# di server), jadi kalau suatu saat server terasa berat, batas bisa dipasang
# lagi lewat env LIVE_MAX_SESSIONS (mis. 5) tanpa mengubah kode.
MAX_CONCURRENT_SESSIONS = max(0, int(os.getenv("LIVE_MAX_SESSIONS", "0")))

# 1 Chromium dipakai bersama semua bot (tiap bot = 1 tab/context). Hasil ukur
# lokal 5 bot: 5 browser terpisah 1392MB/21 proses vs 1 browser + 5 tab
# 741MB/9 proses (±47% lebih hemat). Set LIVE_SHARED_BROWSER=0 untuk kembali
# memakai satu browser per bot.
SHARED_BROWSER = os.getenv("LIVE_SHARED_BROWSER", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


class _BrowserRuntime:
    """Satu thread + event loop khusus untuk seluruh pekerjaan Playwright.

    Endpoint FastAPI tetap berjalan di loop-nya sendiri dan menitipkan
    coroutine ke sini lewat :meth:`run`. Thread dibuat saat pertama dibutuhkan
    dan dipakai ulang oleh semua sesi.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _loop_siap(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            loop = self._loop
            if loop is not None and loop.is_running():
                return loop
            siap = threading.Event()

            def jalankan() -> None:
                if sys.platform == "win32":
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                loop_baru = asyncio.new_event_loop()
                asyncio.set_event_loop(loop_baru)
                self._loop = loop_baru
                loop_baru.call_soon(siap.set)
                loop_baru.run_forever()

            self._thread = threading.Thread(target=jalankan, name="playwright", daemon=True)
            self._thread.start()
            if not siap.wait(20) or self._loop is None:
                raise RuntimeError("Thread browser gagal disiapkan")
            return self._loop

    async def run(self, coro):
        """Jalankan ``coro`` di thread browser, tunggu hasilnya dari loop FastAPI."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop_siap())
        return await asyncio.wrap_future(future)


_RUNTIME = _BrowserRuntime()


class _SharedBrowserPool:
    """Satu Chromium untuk banyak sesi, ditutup otomatis saat pemakai terakhir keluar."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._users = 0

    @property
    def users(self) -> int:
        return self._users

    async def acquire(self, headless: bool):
        """Pinjam browser bersama; dipakai sebagai context baru per sesi."""
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=headless,
                    args=browser_args(),
                )
            self._users += 1
            return self._browser

    async def release(self) -> None:
        """Lepas satu pemakai; browser ditutup kalau sudah tidak ada pemakai."""
        async with self._lock:
            self._users = max(0, self._users - 1)
            if self._users == 0:
                await self._stop_locked()

    async def _stop_locked(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        try:
            if playwright is not None:
                await playwright.stop()
        except Exception:
            pass

    async def stop_all(self) -> None:
        async with self._lock:
            self._users = 0
            await self._stop_locked()


_POOL = _SharedBrowserPool()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class StartRequest(BaseModel):
    # Keduanya boleh kosong kalau account_id diisi: nama dan Session ID-nya
    # diambil dari akun yang tertaut di Kelola Akun.
    username: str = ""
    session_id: str = ""
    # Akun tertaut yang dipakai bot ini. Satu bot = satu akun.
    account_id: int | None = None
    # instagram (bawaan) atau threads. Menentukan modul bot dan akun mana
    # yang boleh dipilih.
    platform: str = "instagram"
    target: str
    comment_count: int = 3
    max_posts: int = 3
    # Nada komentar yang dipilih user di form: positif (default) | netral | negatif
    tone: str = "positif"


def _akun_tertaut(
    account_id: int, pengguna: dict[str, Any] | None, platform: str = "instagram"
) -> tuple[str, str]:
    """Nama akun dan Session ID dari akun tertaut milik ``platform``.

    Dipakai supaya orang tidak perlu menempel Session ID lagi di halaman Auto
    Comment: cukup memilih akunnya. Nilainya diambil di server, jadi cookie-nya
    tidak pernah lewat peramban. Hanya untuk pengguna yang masuk - akun sosmed
    milik pengguna, bukan milik tamu.
    """
    if not pengguna or pengguna.get("tamu"):
        raise HTTPException(status_code=401, detail="Masuk dulu untuk memakai akun tertaut.")
    try:
        from autoupload.db import SessionLocal, SocialAccount
        from autoupload.rahasia import buka
    except Exception as error:  # noqa: BLE001 - paket tidak terpasang di server ini
        raise HTTPException(
            status_code=503, detail="Kelola Akun tidak aktif di server ini."
        ) from error
    db = SessionLocal()
    try:
        akun = (
            db.query(SocialAccount)
            .filter(
                SocialAccount.id == account_id,
                SocialAccount.is_active.is_(True),
                # Hanya akun milik pengguna ini. Tanpa saringan ini, siapa pun
                # yang punya akun website bisa menjalankan bot memakai Session
                # ID orang lain hanya dengan menebak nomor akunnya.
                SocialAccount.owner == str(pengguna.get("username") or ""),
            )
            .first()
        )
        if akun is None or akun.platform != platform:
            raise HTTPException(
                status_code=400, detail=f"Akun {platform.capitalize()} itu tidak ditemukan."
            )
        sesi = buka(akun.session_id or "")
        if not sesi:
            raise HTTPException(
                status_code=400,
                detail="Session ID akun itu tidak terbaca. Tempel ulang di Kelola Akun.",
            )
        nama = (akun.extra or {}).get("nama_akun") or akun.session_name
        return str(nama), sesi
    finally:
        db.close()


class LiveSession:
    def __init__(self, token: str) -> None:
        self.token = token
        self.status = "starting"  # starting|running|completed|error
        self.message = "Menyiapkan browser..."
        self.logs: list[str] = []
        self.result: dict | None = None
        ukuran = viewport()
        self.width = ukuran["width"]
        self.height = ukuran["height"]
        self.latest_jpeg: bytes | None = None
        self._running = True
        # Kapan terakhir ada yang meminta frame. Kalau sudah lama tidak ada,
        # loop screenshot berhenti supaya CPU/memori tidak terbuang.
        self._last_frame_request = 0.0
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._shared = False
        self._released = False
        self._closing: asyncio.Task | None = None
        self._capture_task: asyncio.Task | None = None
        self._job_task: asyncio.Task | None = None

    def is_browser_alive(self) -> bool:
        """True selama Chromium sesi ini benar-benar berjalan.

        Dipakai landing page untuk menghitung bot yang aktif. Sengaja memeriksa
        tab-nya (bukan sekadar ``status``) supaya angkanya turun tepat saat
        Chromium ditutup — termasuk saat job selesai sendiri atau tab-nya mati
        tanpa sempat mengubah status.
        """
        if self._page is not None:
            return not self._page.is_closed()
        # Belum punya tab tapi masih "starting" = Chromium sedang dibuka.
        return self.status == "starting" and self._running

    def add_log(self, text: str) -> None:
        self.logs.append(text)
        if len(self.logs) > 300:
            del self.logs[:-300]
        logger.info("[%s] %s", self.token[:6], text)

    async def start(
        self,
        username: str,
        session_id: str,
        target: str,
        comment_count: int,
        max_posts: int = 3,
        tone: str = "positif",
        platform: str = "instagram",
    ) -> None:
        """Dipanggil dari loop FastAPI; browsernya dibuka di thread khusus."""
        bot = _modul_bot(platform)
        await _RUNTIME.run(
            self._start_di_thread_browser(
                username,
                session_id,
                pesan=f"Session dipasang. Membuka @{target} ...",
                awal=[
                    f"Session diterima untuk akun {username} ({platform})",
                    f"Tone komentar: {tone}",
                    f"Menuju channel @{target}",
                ],
                buat_job=lambda: self._run_job(
                    username, target, comment_count, max_posts, tone, proses=bot._process_posts
                ),
                pasang_cookie=bot._add_session_cookie,
            )
        )

    async def start_upload(
        self, username: str, session_id: str, berkas: str, caption: str
    ) -> None:
        """Ragam kedua: mengunggah satu video ke Instagram, bukan berkomentar."""
        from pathlib import Path as _Path

        await _RUNTIME.run(
            self._start_di_thread_browser(
                username,
                session_id,
                pesan="Session dipasang. Menyiapkan unggahan ...",
                awal=[
                    f"Session diterima untuk akun {username}",
                    f"Berkas: {_Path(berkas).name}",
                ],
                buat_job=lambda: self._run_upload(username, berkas, caption),
            )
        )

    async def _start_di_thread_browser(
        self,
        username: str,
        session_id: str,
        pesan: str,
        awal: list[str],
        buat_job: Any,
        pasang_cookie: Any = None,
    ) -> None:
        headless = os.getenv("LIVE_HEADLESS", "false").lower() == "true"
        try:
            if SHARED_BROWSER:
                # 1 Chromium untuk semua bot: yang baru hanya membuka tab baru.
                self._browser = await _POOL.acquire(headless)
                self._shared = True
            else:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=headless,
                    args=browser_args(),
                )
            self._context = await self._browser.new_context(
                viewport={"width": self.width, "height": self.height},
                user_agent=USER_AGENT,
            )
            self._page = await self._context.new_page()
            self._page.set_default_timeout(int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "10000")))
            self._page.set_default_navigation_timeout(
                int(os.getenv("PLAYWRIGHT_NAVIGATION_TIMEOUT_MS", "30000"))
            )
            await self._blokir_resource_berat()
            if pasang_cookie is None:
                from tasks import _add_session_cookie as pasang_cookie

            await pasang_cookie(self._page, unquote(session_id).strip())
            self.status = "running"
            self.message = pesan
            for baris in awal:
                self.add_log(baris)
            if self._shared:
                self.add_log(
                    f"Mode hemat: 1 Chromium dipakai bersama "
                    f"({_POOL.users} tab terbuka)."
                )
            self._capture_task = asyncio.create_task(self._capture_loop())
            self._job_task = asyncio.create_task(buat_job())
        except Exception as exc:
            # NotImplementedError dari loop Windows datang tanpa pesan, jadi
            # nama kelasnya ikut ditampilkan supaya tidak membingungkan.
            detail = str(exc).strip() or repr(exc)
            logger.exception("Gagal menyiapkan browser")
            self.status = "error"
            self.message = f"Gagal menyiapkan browser: {type(exc).__name__}: {detail}"
            self.add_log(f"Error: {type(exc).__name__}: {detail}")

    async def _capture_loop(self) -> None:
        """Kirim frame ke frontend — hanya saat ada yang menonton.

        Kalau tidak ada permintaan frame selama LIVE_FRAME_IDLE_MS, loop ini
        berhenti mengambil screenshot sehingga 1 bot hampir tidak memakai CPU
        saat jendelanya tertutup/tidak dilihat.
        """
        interval = frame_interval_ms() / 1000
        if interval <= 0:
            # LIVE_FRAME_INTERVAL_MS=0 -> streaming gambar dimatikan total.
            self.add_log("Streaming gambar dimatikan (mode paling hemat).")
            return
        idle_after = frame_idle_ms() / 1000
        quality = frame_quality()
        while self._running:
            try:
                ditonton = (time.monotonic() - self._last_frame_request) <= idle_after
                if (
                    ditonton
                    and self._page is not None
                    and not self._page.is_closed()
                ):
                    self.latest_jpeg = await self._page.screenshot(
                        type="jpeg", quality=quality, scale="css"
                    )
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def _blokir_resource_berat(self) -> None:
        """Batalkan font & media (opsional gambar) supaya hemat bandwidth/CPU."""
        if self._page is None:
            return
        blocked = blocked_resource_types()
        if not blocked:
            return

        async def handler(route) -> None:
            try:
                if route.request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass

        try:
            await self._page.route("**/*", handler)
            self.add_log(f"Mode hemat: {', '.join(sorted(blocked))} dibatalkan.")
        except Exception:
            pass

    async def _run_job(
        self,
        username: str,
        target: str,
        comment_count: int,
        max_posts: int = 3,
        tone: str = "positif",
        proses: Any = None,
    ) -> None:
        try:
            if proses is None:
                from tasks import _process_posts as proses

            result = await proses(
                self._page,
                username,
                target,
                comment_count,
                log=self.add_log,
                max_posts=max_posts,
                tone=tone,
            )
            self.result = result
            self.status = "completed"
            self.message = (
                f"Job selesai: {result.get('comments_posted', 0)} komentar "
                f"pada {result.get('posts_processed', 0)} postingan."
            )
            self.add_log(self.message)
        except asyncio.CancelledError:
            # Dibatalkan tombol Stop: bukan kegagalan job.
            raise
        except Exception as exc:
            self.status = "error"
            self.message = f"Job gagal: {exc}"
            self.add_log(f"Error: {exc}")
        finally:
            await self._selesaikan()

    async def _run_upload(self, username: str, berkas: str, caption: str) -> None:
        """Unggah satu video, lalu cari tautan postingan yang baru dibuat."""
        from pathlib import Path as _Path

        try:
            from upload_ig import postingan_terbaru, upload_video

            hasil = await upload_video(
                self._page, _Path(berkas), caption, log=self.add_log
            )
            tautan = ""
            if username:
                self.add_log("Mencari tautan postingan yang baru diunggah ...")
                try:
                    tautan = await postingan_terbaru(self._page, username)
                except Exception as error:
                    self.add_log(f"Tautan postingan tidak terbaca: {error}")
            self.result = {**hasil, "post_url": tautan}
            self.status = "completed"
            self.message = "Video terunggah ke Instagram."
            self.add_log(self.message + (f" {tautan}" if tautan else ""))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status = "error"
            self.message = f"Unggah gagal: {exc}"
            self.add_log(f"Error: {exc}")
        finally:
            await self._selesaikan()

    async def _selesaikan(self) -> None:
        """Ambil frame terakhir lalu tutup Chromium supaya memori dibebaskan."""
        self._running = False
        try:
            if self._page is not None and not self._page.is_closed():
                self.latest_jpeg = await self._page.screenshot(type="jpeg", quality=70)
        except Exception:
            pass
        await self._shutdown_browser()

    async def _shutdown_browser(self) -> None:
        """Tutup tab milik sesi ini (browser ikut ditutup kalau tidak dipakai bersama).

        Aman dipanggil berkali-kali dan dari dalam task job sendiri. Penutupan
        dijalankan sebagai task terpisah + ``shield`` supaya tetap selesai walau
        pemanggilnya dibatalkan (mis. tombol Stop atau tab ditutup).
        """
        self._running = False
        if self._closing is None:
            self._closing = asyncio.create_task(self._close_now())
        await asyncio.shield(self._closing)

    async def _close_now(self) -> None:
        browser, playwright, context = self._browser, self._playwright, self._context
        self._browser = None
        self._playwright = None
        self._page = None
        self._context = None

        # Tutup tab (context) bot ini saja; sesi lain tidak ikut terganggu.
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass

        if self._shared:
            if not self._released:
                self._released = True
                # Chromium ditutup hanya saat tidak ada sesi lain yang memakainya.
                await _POOL.release()
            return

        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        try:
            if playwright is not None:
                await playwright.stop()
        except Exception:
            pass

    async def close(self) -> None:
        """Hentikan task & tutup browser segera (dipanggil tombol Stop)."""
        await _RUNTIME.run(self._close_di_thread_browser())

    async def _close_di_thread_browser(self) -> None:
        self._running = False
        for task in (self._capture_task, self._job_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._shutdown_browser()


def active_session_count() -> int:
    """Jumlah script bot (Chromium) yang sedang benar-benar berjalan."""
    return sum(1 for sesi in _SESSIONS.values() if sesi.is_browser_alive())


def _get_session(token: str) -> LiveSession:
    session = _SESSIONS.get(token)
    if session is None:
        raise HTTPException(status_code=404, detail="Live session tidak ditemukan")
    return session


@router.post("/start")
async def start_live(
    request: StartRequest, pengguna: dict[str, Any] | None = Depends(identitas)
) -> dict:
    platform = str(request.platform or "instagram").strip().lower()
    _modul_bot(platform)  # tolak platform yang tidak dikenal sebelum apa pun dibuka
    nama_akun, sesi = request.username.strip(), request.session_id.strip()
    if request.account_id:
        nama_akun, sesi = _akun_tertaut(request.account_id, pengguna, platform)
    if not nama_akun or not sesi or not request.target.strip():
        raise HTTPException(status_code=422, detail="Akun dan channel target wajib diisi")
    if not (1 <= request.comment_count <= 100):
        raise HTTPException(status_code=422, detail="comment_count harus 1-100")
    if not (1 <= request.max_posts <= 50):
        raise HTTPException(status_code=422, detail="max_posts harus 1-50")
    from comment_ai import TONES, normalize_tone

    tone_input = str(request.tone or "").strip().lower()
    if tone_input and tone_input not in TONES:
        raise HTTPException(
            status_code=422,
            detail=f"tone harus salah satu dari: {', '.join(TONES)}",
        )
    tone = normalize_tone(tone_input)

    if MAX_CONCURRENT_SESSIONS:
        aktif = active_session_count()
        if aktif >= MAX_CONCURRENT_SESSIONS:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Maksimal {MAX_CONCURRENT_SESSIONS} bot berjalan bersamaan. "
                    "Tunggu salah satu selesai lalu coba lagi."
                ),
            )
    token = uuid.uuid4().hex
    session = LiveSession(token)
    _SESSIONS[token] = session
    await session.start(
        nama_akun,
        sesi,
        request.target.strip(),
        request.comment_count,
        request.max_posts,
        tone,
        platform,
    )
    return {"token": token, "width": session.width, "height": session.height}


@router.get("/{token}/frame")
async def live_frame(token: str) -> Response:
    session = _get_session(token)
    # Tandai bahwa ada yang menonton, supaya loop screenshot tetap berjalan.
    session._last_frame_request = time.monotonic()
    if not session.latest_jpeg:
        return Response(status_code=204)
    return Response(content=session.latest_jpeg, media_type="image/jpeg")


@router.get("/{token}/status")
async def live_status(token: str) -> dict:
    session = _get_session(token)
    return {
        "token": token,
        "status": session.status,
        "message": session.message,
        "logs": session.logs,
        "result": session.result,
        # Info mode hemat: berapa tab yang sedang memakai Chromium bersama.
        "shared_browser": SHARED_BROWSER,
        "tabs": _POOL.users,
    }


@router.post("/{token}/close")
async def live_close(token: str) -> dict:
    session = _get_session(token)
    await session.close()
    _SESSIONS.pop(token, None)
    return {"ok": True}


@router.post("/{token}/stop")
async def live_stop(token: str) -> dict:
    session = _get_session(token)
    await session.close()
    _SESSIONS.pop(token, None)
    return {"ok": True, "status": "stopped"}
