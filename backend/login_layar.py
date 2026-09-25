"""Login sosmed lewat LAYAR PERAMBAN SERVER, ditampilkan di tab situs.

Alurnya: pengguna menekan tombol di Kelola Akun -> server membuka halaman
login asli platform di perambannya (tanpa jendela fisik, karena server tak
punya layar) -> tab baru di situs menampilkan layar peramban itu sebagai
gambar yang diperbarui terus, dan klik/ketik pengguna di tab diteruskan ke
peramban server. Pengguna mengetik username, password, dan mengerjakan
captcha LANGSUNG di halaman Instagram itu - situs kita tidak pernah memegang
passwordnya. Begitu cookie sesi muncul, server merangkainya jadi Session ID
lalu tab menyimpannya ke akun pengguna seperti SSID biasa.

Peramban dijalankan di thread Playwright milik live_session (_RUNTIME), jadi
tidak ada loop kedua dan tidak ada Chromium tambahan yang menganggur.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

# Thread + loop Playwright bersama, dan konfigurasi peramban yang sama dengan
# fitur live lain di server ini.
from live_session import _RUNTIME
from browser_config import browser_args

# Paket autoupload dimuat MALAS: sys.path-nya disiapkan main.py (letaknya
# beda di laptop dan di image server), dan router ini hanya dipasang kalau
# paket itu memang ada.
def _au():
    from autoupload import masuk, verify

    return masuk, verify


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/login-layar", tags=["login-layar"])

# Platform yang formulir loginnya bisa ditampilkan. Sama dengan jalur sandi;
# YouTube tidak ada karena Google memblokir peramban otomatis.
PLATFORM_LAYAR = ("instagram", "threads", "twitter", "facebook", "tiktok")

# Batas hidup satu sesi login, supaya peramban yang ditinggalkan tidak
# menahan memori server selamanya.
BATAS_HIDUP_DETIK = float(os.getenv("LOGIN_LAYAR_BATAS_DETIK", "420"))
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_SESI: dict[str, "SesiLogin"] = {}


class SesiLogin:
    def __init__(self, platform: str, lebar: int, tinggi: int) -> None:
        self.token = uuid.uuid4().hex
        self.platform = platform
        self.lebar = lebar
        self.tinggi = tinggi
        self.status = "starting"  # starting|running|done|error
        self.pesan = "Menyiapkan halaman login..."
        self.session_id = ""
        self.nama_akun = ""
        self.jpeg: bytes | None = None
        self.dibuat = time.monotonic()
        self.terakhir_dilihat = time.monotonic()
        self._hidup = True
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._tugas: list[asyncio.Task] = []

    # -- dijalankan di thread Playwright --

    async def mulai(self) -> None:
        from playwright.async_api import async_playwright

        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True, args=browser_args())
            self._context = await self._browser.new_context(
                viewport={"width": self.lebar, "height": self.tinggi}, user_agent=UA
            )
            self._page = await self._context.new_page()
            self._page.set_default_timeout(15_000)
            await self._page.goto(
                _au()[0].URL_MASUK[self.platform], wait_until="domcontentloaded", timeout=60_000
            )
            self.status = "running"
            self.pesan = "Silakan login di jendela ini."
            self._tugas = [
                asyncio.create_task(self._loop_gambar()),
                asyncio.create_task(self._loop_pantau()),
            ]
        except Exception as exc:  # noqa: BLE001
            logger.exception("login-layar gagal disiapkan")
            self.status = "error"
            self.pesan = f"Gagal menyiapkan halaman: {type(exc).__name__}"

    async def _loop_gambar(self) -> None:
        # ~4 bidikan/detik selama tab masih menonton; berhenti kalau
        # ditinggalkan supaya tidak membakar CPU peramban server.
        while self._hidup:
            try:
                ditonton = (time.monotonic() - self.terakhir_dilihat) < 8
                if ditonton and self._page is not None and not self._page.is_closed():
                    self.jpeg = await self._page.screenshot(type="jpeg", quality=55, scale="css")
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.25)

    async def _loop_pantau(self) -> None:
        # Pantau: cookie sesi muncul (berhasil) atau umur sesi habis.
        masuk, verify = _au()
        while self._hidup:
            await asyncio.sleep(1.0)
            if time.monotonic() - self.dibuat > BATAS_HIDUP_DETIK:
                if self.status == "running":
                    self.status = "error"
                    self.pesan = "Waktu login habis. Tutup lalu coba lagi."
                await self._matikan()
                return
            try:
                if self._page is None or self._page.is_closed():
                    continue
                cookies = {c["name"]: c for c in await self._context.cookies()}
                if masuk._cookie_wajib_ada(cookies, self.platform):
                    alamat = (self._page.url or "").lower()
                    if any(t in alamat for t in verify.TANDA_DITANGGUHKAN):
                        self.status = "error"
                        self.pesan = "Akun ini ditangguhkan oleh platform."
                        await self._matikan()
                        return
                    try:
                        self.session_id = masuk._susun_session_id(self.platform, cookies)
                    except Exception:  # noqa: BLE001 - cookie belum lengkap, tunggu lagi
                        continue
                    js = verify.NAMA_JS.get(self.platform)
                    if js:
                        try:
                            self.nama_akun = (await self._page.evaluate(js) or "").strip()[:40]
                        except Exception:  # noqa: BLE001
                            self.nama_akun = ""
                    self.status = "done"
                    self.pesan = "Login berhasil."
                    await self._matikan()
                    return
            except Exception:  # noqa: BLE001
                continue

    async def _teruskan(self, peristiwa: list[dict]) -> None:
        if self._page is None or self._page.is_closed():
            return
        for e in peristiwa:
            j = e.get("t")
            try:
                if j == "move":
                    await self._page.mouse.move(float(e["x"]), float(e["y"]))
                elif j == "click":
                    await self._page.mouse.click(float(e["x"]), float(e["y"]))
                elif j == "wheel":
                    await self._page.mouse.wheel(float(e.get("dx", 0)), float(e.get("dy", 0)))
                elif j == "text":
                    await self._page.keyboard.insert_text(str(e.get("v", "")))
                elif j == "key":
                    await self._page.keyboard.press(str(e.get("k", "")))
            except Exception:  # noqa: BLE001 - satu peristiwa gagal tak boleh menjatuhkan sesi
                continue

    async def _matikan(self) -> None:
        self._hidup = False
        # Jangan batalkan tugas yang sedang menjalankan fungsi ini: pemantau
        # memanggil _matikan() sendiri saat login berhasil, dan membatalkan
        # dirinya membuat await berikutnya (menutup peramban) terputus -
        # Chromium tertinggal hidup memakan memori server.
        kini = asyncio.current_task()
        for t in self._tugas:
            if t is not kini:
                t.cancel()
        for obj, tutup in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    await getattr(obj, tutup)()
            except Exception:  # noqa: BLE001
                pass
        self._context = self._browser = self._pw = self._page = None


# ---------------------------------------------------------------- endpoint

class MulaiBody(BaseModel):
    platform: str
    lebar: int = Field(default=440, ge=320, le=2560)
    tinggi: int = Field(default=780, ge=480, le=2560)


class InputBody(BaseModel):
    events: list[dict] = Field(default_factory=list, max_length=100)


def _wajib_login():
    # Diisi main.py dengan pengguna_wajib; berdiri sendiri tanpa penjaga.
    return None


@router.post("/start")
async def mulai(body: MulaiBody, _=Depends(_wajib_login)) -> dict:
    if body.platform not in PLATFORM_LAYAR:
        raise HTTPException(400, "Platform ini tidak bisa login lewat layar.")
    # Satu sesi login pada satu waktu: peramban server memakan memori, dan
    # login yang ditinggalkan membersihkan dirinya lewat batas umur.
    aktif = [s for s in _SESI.values() if s.status in ("starting", "running")]
    if len(aktif) >= int(os.getenv("LOGIN_LAYAR_MAKS", "2")):
        raise HTTPException(429, "Sedang ada login lain berjalan. Coba lagi sebentar.")
    sesi = SesiLogin(body.platform, body.lebar, body.tinggi)
    _SESI[sesi.token] = sesi
    _bersihkan()
    await _RUNTIME.run(sesi.mulai())
    return {"token": sesi.token, "lebar": sesi.lebar, "tinggi": sesi.tinggi, "status": sesi.status}


def _ambil(token: str) -> SesiLogin:
    s = _SESI.get(token)
    if s is None:
        raise HTTPException(404, "Sesi login tidak ditemukan.")
    return s


@router.get("/{token}/frame")
async def frame(token: str) -> Response:
    s = _ambil(token)
    s.terakhir_dilihat = time.monotonic()
    if not s.jpeg:
        return Response(status_code=204)
    return Response(content=s.jpeg, media_type="image/jpeg")


@router.get("/{token}/status")
async def status(token: str, _=Depends(_wajib_login)) -> dict:
    s = _ambil(token)
    return {
        "status": s.status,
        "pesan": s.pesan,
        "lebar": s.lebar,
        "tinggi": s.tinggi,
        "session_id": s.session_id if s.status == "done" else "",
        "nama_akun": s.nama_akun if s.status == "done" else "",
    }


@router.post("/{token}/input")
async def input_peristiwa(token: str, body: InputBody, _=Depends(_wajib_login)) -> dict:
    s = _ambil(token)
    if s.status == "running":
        await _RUNTIME.run(s._teruskan(body.events))
    return {"ok": True, "status": s.status}


@router.post("/{token}/close")
async def tutup(token: str, _=Depends(_wajib_login)) -> dict:
    s = _SESI.pop(token, None)
    if s is not None:
        await _RUNTIME.run(s._matikan())
    return {"ok": True}


def _bersihkan() -> None:
    mati = [t for t, s in _SESI.items() if not s._hidup and time.monotonic() - s.dibuat > 60]
    for t in mati:
        _SESI.pop(t, None)
