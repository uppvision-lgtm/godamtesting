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
import json
import logging
import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
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
BATAS_HIDUP_DETIK = float(os.getenv("LOGIN_LAYAR_BATAS_DETIK", "600"))
KUALITAS = int(os.getenv("LOGIN_LAYAR_KUALITAS", "82"))
# Selang minimum antar-bingkai yang dikirim WS, detik. 1/60 = 60 fps.
# Screencast tetap berbasis-perubahan: ini hanya melepas pembatas, bukan
# memaksa bingkai saat layar diam.
SELANG_KIRIM = 1.0 / int(os.getenv("LOGIN_LAYAR_FPS", "60"))
# Batas piksel kanvas (lebar*tinggi*kepadatan^2). Kontainer 954 MB: layar
# retina penuh terlalu mahal untuk dikodekan berulang, jadi kepadatan
# diturunkan sampai muat.
BATAS_PIKSEL = int(os.getenv("LOGIN_LAYAR_BATAS_PIKSEL", str(2400 * 1500)))
UA_DESKTOP = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
UA_SELULER = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"
)


def _ukuran_aman(lebar, tinggi, dpr):
    """Batasi ukuran & kepadatan supaya muat di memori kontainer."""
    lebar = max(320, min(int(lebar or 0), 1600))
    tinggi = max(480, min(int(tinggi or 0), 1400))
    # Kepadatan (supersample internal) dibatasi 1.5: screencast Chrome selalu
    # mengeluarkan bingkai seukuran CSS apa pun kepadatannya, jadi 2x tidak
    # menambah piksel bingkai - hanya menghaluskan teks - dan mahal di memori.
    dpr = max(1.0, min(float(dpr or 1), 1.5))
    while dpr > 1.0 and lebar * tinggi * dpr * dpr > BATAS_PIKSEL:
        dpr = round(dpr - 0.25, 2)
    return lebar, tinggi, max(1.0, dpr)

_SESI: dict[str, "SesiLogin"] = {}


class SesiLogin:
    def __init__(self, pemilik, platform, lebar, tinggi, dpr, seluler):
        self.token = uuid.uuid4().hex
        self.pemilik = pemilik
        self.platform = platform
        self.lebar, self.tinggi, self.dpr = _ukuran_aman(lebar, tinggi, dpr)
        self.seluler = bool(seluler)
        self.status = "starting"  # starting|running|done|error
        self.pesan = "Menyiapkan halaman login..."
        self.session_id = ""
        self.nama_akun = ""
        self.dibuat = time.monotonic()
        self._hidup = True
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._cdp = None
        self._tugas: list[asyncio.Task] = []
        # Bingkai JPEG (base64) terbaru dari screencast, plus penghitung versi.
        # WebSocket (loop FastAPI) membandingkan versi; screencast (loop
        # peramban) menaikkannya. Sengaja bukan asyncio.Event: primitif itu
        # terikat satu loop, sedangkan dua loop berbeda menyentuhnya di sini.
        self._jpeg_b64 = ""
        self._versi = 0
        # Kapan WS terakhir aktif; sesi yang WS-nya tak pernah datang atau
        # sudah lama putus ditutup cepat supaya slot tidak tersandera login
        # yang ditinggalkan (tab ditutup sebelum selesai).
        self._ws_ada = False
        self._ws_terakhir = time.monotonic()

    # -- dijalankan di thread Playwright --

    async def mulai(self) -> None:
        from playwright.async_api import async_playwright

        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True, args=browser_args())
            self._context = await self._browser.new_context(
                viewport={"width": self.lebar, "height": self.tinggi},
                device_scale_factor=self.dpr,     # retina: bingkai 2x tajam
                is_mobile=self.seluler,
                has_touch=self.seluler,
                user_agent=UA_SELULER if self.seluler else UA_DESKTOP,
            )
            self._page = await self._context.new_page()
            self._page.set_default_timeout(15_000)
            await self._page.goto(
                _au()[0].URL_MASUK[self.platform], wait_until="domcontentloaded", timeout=60_000
            )
            # Screencast: Chrome mengirim bingkai HANYA saat layar berubah.
            self._cdp = await self._context.new_cdp_session(self._page)
            self._cdp.on("Page.screencastFrame", self._terima_bingkai)
            await self._mulai_screencast()
            self.status = "running"
            self.pesan = "Silakan login di jendela ini."
            self._tugas = [asyncio.create_task(self._loop_pantau())]
        except Exception as exc:  # noqa: BLE001
            logger.exception("login-layar gagal disiapkan")
            self.status = "error"
            self.pesan = f"Gagal menyiapkan halaman: {type(exc).__name__}"

    async def _mulai_screencast(self) -> None:
        await self._cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": KUALITAS,
            "maxWidth": int(self.lebar),
            "maxHeight": int(self.tinggi),
            "everyNthFrame": 1,
        })

    def _terima_bingkai(self, params: dict) -> None:
        # Dipanggil di loop peramban. Simpan bingkai lalu akui, supaya Chrome
        # mengirim yang berikutnya; tanpa ack, screencast berhenti.
        self._jpeg_b64 = params.get("data", "")
        self._versi += 1
        sid = params.get("sessionId")
        if sid is not None and self._cdp is not None:
            asyncio.create_task(self._ack(sid))

    async def _ack(self, sid: int) -> None:
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": sid})
        except Exception:  # noqa: BLE001
            pass

    async def ubah_ukuran(self, lebar, tinggi) -> None:
        """Samakan lebar/tinggi peramban server dengan tab (saat resize).

        Kepadatan (dpr) TIDAK diubah di sini: mengubahnya butuh konteks baru,
        sedangkan resize jendela hampir selalu hanya soal lebar/tinggi.
        """
        lebar, tinggi, _ = _ukuran_aman(lebar, tinggi, self.dpr)
        if (lebar, tinggi) == (self.lebar, self.tinggi) or self._page is None:
            return
        self.lebar, self.tinggi = lebar, tinggi
        try:
            await self._page.set_viewport_size({"width": lebar, "height": tinggi})
            await self._cdp.send("Page.stopScreencast")
            await self._mulai_screencast()
        except Exception:  # noqa: BLE001
            pass

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
            # WS belum pernah tersambung 40 dtk, atau sudah putus 40 dtk:
            # tab-nya kemungkinan ditutup. Lepaskan slotnya.
            if not self._ws_ada and time.monotonic() - self.dibuat > 40:
                await self._matikan()
                return
            if self._ws_ada and time.monotonic() - self._ws_terakhir > 40 and self.status == "running":
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
        m = self._page.mouse
        for e in peristiwa:
            j = e.get("t")
            try:
                if j == "move":
                    await m.move(float(e["x"]), float(e["y"]))
                elif j == "down":
                    await m.move(float(e["x"]), float(e["y"]))
                    await m.down()
                elif j == "up":
                    await m.move(float(e["x"]), float(e["y"]))
                    await m.up()
                elif j == "click":
                    await m.click(float(e["x"]), float(e["y"]), click_count=int(e.get("n", 1)))
                elif j == "wheel":
                    await m.wheel(float(e.get("dx", 0)), float(e.get("dy", 0)))
                elif j == "text":
                    await self._page.keyboard.insert_text(str(e.get("v", "")))
                elif j == "key":
                    await self._page.keyboard.press(str(e.get("k", ""))[:40])
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
    lebar: int = Field(default=1024, ge=320, le=1600)
    tinggi: int = Field(default=760, ge=480, le=1400)
    dpr: float = Field(default=1.5, ge=1.0, le=2.0)
    seluler: bool = False


def _pemilik(pengguna) -> str:
    return str((pengguna or {}).get("username") or "").strip().lower()


def _baca_token(token: str):
    try:
        from auth_api import baca_token
    except Exception:  # noqa: BLE001
        return None
    return baca_token(token) if token else None


def _wajib_login():
    # Diisi main.py dengan pengguna_wajib; berdiri sendiri tanpa penjaga.
    return None


@router.post("/start")
async def mulai(body: MulaiBody, pengguna=Depends(_wajib_login)) -> dict:
    if body.platform not in PLATFORM_LAYAR:
        raise HTTPException(400, "Platform ini tidak bisa login lewat layar.")
    # Satu sesi login pada satu waktu: peramban server memakan memori, dan
    # login yang ditinggalkan membersihkan dirinya lewat batas umur.
    aktif = [s for s in _SESI.values() if s.status in ("starting", "running")]
    if len(aktif) >= int(os.getenv("LOGIN_LAYAR_MAKS", "2")):
        raise HTTPException(429, "Sedang ada login lain berjalan. Coba lagi sebentar.")
    sesi = SesiLogin(_pemilik(pengguna), body.platform,
                     body.lebar, body.tinggi, body.dpr, body.seluler)
    _SESI[sesi.token] = sesi
    _bersihkan()
    await _RUNTIME.run(sesi.mulai())
    return {"token": sesi.token, "lebar": sesi.lebar, "tinggi": sesi.tinggi,
            "dpr": sesi.dpr, "status": sesi.status}


def _ambil(token: str) -> SesiLogin:
    s = _SESI.get(token)
    if s is None:
        raise HTTPException(404, "Sesi login tidak ditemukan.")
    return s


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


@router.websocket("/{token}/ws")
async def ws(websocket: WebSocket, token: str) -> None:
    """Alirkan bingkai layar ke tab dan terima klik/ketik dari tab.

    Autentikasi lewat query ?akses=<token situs> (header sulit dari
    WebSocket peramban): harus token yang sah, dan pemiliknya harus sama
    dengan yang memulai sesi login ini.
    """
    s = _SESI.get(token)
    akses = websocket.query_params.get("akses", "")
    isi = _baca_token(akses)
    pemilik = str((isi or {}).get("username") or "").strip().lower()
    if s is None or not isi or (s.pemilik and pemilik != s.pemilik):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    s._ws_ada = True
    s._ws_terakhir = time.monotonic()

    async def terima():
        # Peristiwa masuk (klik/ketik/gulir/resize) diteruskan ke peramban.
        try:
            while s._hidup:
                pesan = await websocket.receive_text()
                s._ws_terakhir = time.monotonic()
                data = json.loads(pesan)
                if data.get("t") == "resize":
                    await _RUNTIME.run(s.ubah_ukuran(data.get("lebar"), data.get("tinggi")))
                elif s.status == "running":
                    await _RUNTIME.run(s._teruskan([data]))
        except (WebSocketDisconnect, Exception):  # noqa: BLE001
            pass

    tugas_terima = asyncio.create_task(terima())
    versi_terkirim = -1
    try:
        while s._hidup:
            if s._versi != versi_terkirim and s._jpeg_b64:
                versi_terkirim = s._versi
                await websocket.send_text(json.dumps({"t": "frame", "b64": s._jpeg_b64,
                                                       "w": s.lebar, "h": s.tinggi}))
            s._ws_terakhir = time.monotonic()
            if s.status in ("done", "error"):
                await websocket.send_text(json.dumps({
                    "t": "selesai", "status": s.status, "pesan": s.pesan,
                    "session_id": s.session_id, "nama_akun": s.nama_akun}))
                break
            await asyncio.sleep(SELANG_KIRIM)
    except (WebSocketDisconnect, Exception):  # noqa: BLE001
        pass
    finally:
        tugas_terima.cancel()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


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
