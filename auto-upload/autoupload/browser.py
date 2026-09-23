"""Mesin upload berbasis browser nyata (Playwright) - tanpa API resmi/berbayar.

Tiap akun punya profil Chrome sendiri di browser_profiles/<platform>/<akun>/.
Cookie tersimpan di disk, jadi cukup login sekali dan tetap login berminggu-
minggu seperti browser biasa. Beberapa akun per platform = folder profil beda.
"""

import asyncio
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from autoupload.config import DEBUG_SHOTS_DIR, MAKS_DEBUG_SHOT, PROFILES_DIR

log = logging.getLogger("autoupload")


class MediaKind(str, Enum):
    VIDEO = "video"
    PHOTO = "photo"


@dataclass
class UploadResult:
    platform: str
    success: bool
    url: str | None = None
    error: str | None = None


class PlatformError(Exception):
    """Dilempar saat operasi platform gagal."""


# Upload pada profil yang sama diserialisasi: dua upload sekaligus akan
# berebut lock profil Chrome (satu instance per folder profil).
_profile_locks: dict[str, asyncio.Lock] = {}

# Flag anti-deteksi-otomasi (TikTok memblokir Chromium headless polos dengan
# "maximum attempts reached" saat login). Utamakan Chrome asli via channel.
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--start-maximized",
    # Penghemat memori. Kontainer server hanya punya ~1 GB, dan Chromium yang
    # kehabisan memori tidak melapor dengan sopan: renderernya dibunuh, lalu
    # perintah Playwright berikutnya gagal dengan "Page crashed" atau
    # "net::ERR_ABORTED; maybe frame was detached?" - pesan yang tidak ada
    # hubungannya dengan penyebab aslinya. Flag yang sama sudah lama dipakai
    # bot komentar di berkas browser_config.py.
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-translate",
    "--mute-audio",
    "--renderer-process-limit=1",
    # Satu renderer untuk seluruh halaman (iframe tidak dapat proses sendiri),
    # dan batas tumpukan JS. "--single-process" SENGAJA tidak dipakai: diuji
    # dan peramban langsung mati sebelum halaman pertama selesai.
    "--disable-features=site-per-process,Translate,BackForwardCache,MediaRouter,OptimizationHints",
    "--js-flags=--max-old-space-size=256",
]

# Chrome headless mengiklankan 'HeadlessChrome' di navigator.userAgent dan
# navigator.userAgentData; YouTube Studio menganggapnya browser tak didukung
# (interstitial + RPC internal rusak -> tombol Publish tak pernah muncul).
# Tampil sebagai Chrome asli. Samakan versi mayor dengan Chrome terpasang.
_UA_OS = {
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "win32": "Windows NT 10.0; Win64; x64",
}.get(sys.platform, "X11; Linux x86_64")
CHROME_UA = (
    f"Mozilla/5.0 ({_UA_OS}) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['id-ID', 'id', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
(() => {
  try {
    let ua = navigator.userAgent;
    if (ua.includes('HeadlessChrome')) {
      ua = ua.replace('HeadlessChrome', 'Chrome');
      Object.defineProperty(navigator, 'userAgent', { get: () => ua, configurable: true });
      Object.defineProperty(navigator, 'appVersion', {
        get: () => ua.replace('Mozilla/', ''), configurable: true });
    }
    const seg = ua.split('Chrome/')[1];
    const ver = seg ? seg.split('.')[0] : '153';
    const brands = [
      { brand: 'Chromium', version: ver },
      { brand: 'Google Chrome', version: ver },
      { brand: 'Not:A-Brand', version: '24' },
    ];
    const p = navigator.platform || '';
    const platform = p.includes('Mac') ? 'macOS' : (p.includes('Win') ? 'Windows' : 'Linux');
    const uad = {
      brands,
      mobile: false,
      platform,
      getHighEntropyValues: async () => ({
        brands, mobile: false, platform, platformVersion: '10.15.7',
        architecture: 'x86', bitness: '64', model: '', uaFullVersion: '',
        fullVersionList: brands,
      }),
    };
    Object.defineProperty(navigator, 'userAgentData', { get: () => uad, configurable: true });
  } catch (e) {}
})();
"""

# Layar 1366x900 berarti Chromium menyiapkan petak gambar seluas itu untuk
# tiap halaman. Di kontainer 1 GB itu pemborosan yang tidak dibutuhkan bot:
# semua tombol yang dipakai tetap terlihat pada 1000x700, ukuran yang sama
# dengan yang dipakai bot komentar.
VIEWPORT = {"width": 1000, "height": 700}


async def _tautan_di_halaman(page, pemilih: str) -> list[str]:
    """Alamat postingan di halaman: dinormalkan, unik, urutan dipertahankan.

    X menempelkan sub-alamat pada postingan yang sama (/analytics untuk
    pemiliknya, /photo/1 untuk tiap lampiran), dan itu harus dipangkas supaya
    satu postingan tidak terhitung beberapa kali. Pemangkasannya menargetkan
    bagian SESUDAH id postingan - bukan menyaring alamat yang sekadar
    mengandung kata "video" atau "photo", sebab alamat postingan TikTok
    justru berbentuk /@akun/video/<id> dan akan ikut terbuang.
    """
    return await page.evaluate(
        """(sel) => [...new Set([...document.querySelectorAll(sel)]
              .map(a => a.href)
              .filter(Boolean)
              .map(u => u.split('?')[0])
              .map(u => u.replace(/(\\/status\\/\\d+)\\/.*$/, '$1'))
          )]""",
        pemilih,
    )


async def waktu_lokal_halaman(page, kapan):
    """Ubah waktu UTC menjadi waktu LOKAL peramban yang sedang terbuka.

    Pemilih jadwal X dan TikTok memakai zona waktu peramban, sedangkan
    `scheduled_at` disimpan dalam UTC. Tanpa konversi, jadwal yang diminta
    pukul 18.00 WIB akan terpasang pukul 18.00 UTC - meleset tujuh jam.
    Offsetnya dibaca dari halaman itu sendiri supaya benar baik di laptop
    maupun di server yang zonanya berbeda.
    """
    from datetime import timedelta, timezone

    if kapan.tzinfo is None:
        kapan = kapan.replace(tzinfo=timezone.utc)
    offset = await page.evaluate("() => -new Date().getTimezoneOffset()")
    return kapan.astimezone(timezone.utc) + timedelta(minutes=offset)


async def tautan_profil(page, url_profil: str, pemilih: str, jeda_ms: int = 5_000) -> set[str]:
    """Daftar tautan postingan yang SUDAH ada di profil, sebelum upload.

    `jeda_ms` bisa dinaikkan untuk halaman yang lambat merender daftarnya.
    """
    try:
        await page.goto(url_profil, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(jeda_ms)
        return set(await _tautan_di_halaman(page, pemilih))
    except Exception:  # noqa: BLE001 - tanpa daftar awal pun upload tetap jalan
        return set()


async def tautan_baru(
    page,
    url_profil: str,
    pemilih: str,
    sebelum: set[str],
    percobaan: int = 8,
    jeda_ms: int = 5_000,
) -> str | None:
    """Tautan postingan yang baru muncul di profil setelah upload.

    Dibandingkan dengan daftar sebelum upload, bukan sekadar mengambil yang
    paling atas: profil bisa punya postingan tersemat yang selalu di urutan
    pertama, dan itu akan salah dilaporkan sebagai hasil upload. Diulang
    beberapa kali karena postingan baru tidak selalu langsung tampil.
    """
    for _ in range(percobaan):
        try:
            await page.goto(url_profil, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(jeda_ms)
            baru = [t for t in await _tautan_di_halaman(page, pemilih) if t not in sebelum]
            if baru:
                return baru[0]
        except Exception:  # noqa: BLE001 - coba lagi
            pass
        await page.wait_for_timeout(4_000)
    return None


def _pangkas_debug_shots() -> None:
    """Sisakan screenshot kegagalan terbaru saja.

    Folder ini tidak pernah dibersihkan siapa pun dan hanya bertambah setiap
    kali ada upload gagal.
    """
    try:
        berkas = sorted(
            (f for f in DEBUG_SHOTS_DIR.glob("*") if f.is_file()),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for f in berkas[MAKS_DEBUG_SHOT * 2:]:  # *2: tiap kegagalan = .png + .txt
            f.unlink(missing_ok=True)
    except OSError:
        pass  # kebersihan tidak boleh menggagalkan upload


def profile_dir(platform: str, session_name: str = "default") -> Path:
    return PROFILES_DIR / platform / session_name


def session_names(platform: str) -> list[str]:
    """Semua profil akun yang tersimpan untuk satu platform."""
    pdir = PROFILES_DIR / platform
    if not pdir.exists():
        return []
    return sorted(d.name for d in pdir.iterdir() if d.is_dir() and any(d.iterdir()))


def platform_has_session(platform: str) -> bool:
    return bool(session_names(platform))


def _clear_stale_profile_locks(profile: Path) -> None:
    """Hapus file Singleton* sisa Chrome yang crash/di-kill.

    Chrome menulis SingletonLock/Socket/Cookie sebagai symlink ke
    '<hostname>-<pid>'. Kalau pid-nya sudah mati, lock itu basi dan bikin
    semua launch berikutnya gagal dengan error 'profile in use'.
    """
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = profile / name
        try:
            if not lock.is_symlink() and not lock.exists():
                continue
            target = os.readlink(lock) if lock.is_symlink() else ""
            pid = int(target.rsplit("-", 1)[-1]) if "-" in target else -1
            try:
                os.kill(pid, 0)  # cuma cek hidup/tidak, tidak mematikan
                continue         # masih dipakai proses hidup -> jangan hapus
            except (ProcessLookupError, ValueError, OverflowError):
                pass             # prosesnya sudah tidak ada -> lock basi
            except PermissionError:
                continue         # hidup tapi milik user lain
            lock.unlink(missing_ok=True)
        except OSError:          # best-effort, jangan sampai bikin gagal
            pass


class PlaywrightPlatform(ABC):
    """Basis adapter upload lewat sesi browser. Satu profil per platform+akun."""

    platform_key = "abstract"

    # Jalan pintas "Session ID": map cookie_name -> domain, di-inject ke sesi
    # upload. Satu entri = tempel satu nilai cookie. Beberapa entri = nilai
    # digabung pakai "|" dengan urutan yang sama.
    session_cookie_map: dict[str, str | list[str]] = {}
    # Cookie yang BOLEH dilewat (tempel lebih sedikit nilai): biasanya
    # timestamp pendek yang di-refresh sendiri oleh platform.
    session_cookie_optional: tuple[str, ...] = ()

    # Sebagian platform menyimpan sesi PER WILAYAH DATA dan memilih wilayahnya
    # dari IP pemanggil. Sesi yang dibuat di Indonesia karena itu ditolak saat
    # dipakai dari server di benua lain: cookie-nya sah, tapi dicari di pusat
    # data yang salah, dan hasilnya tampak persis seperti sesi mati. Kalau
    # kedua nilai ini diisi, wilayahnya dicoba satu per satu.
    wilayah_cookie: str = ""
    wilayah_kandidat: tuple[str, ...] = ()

    # Jenis permintaan yang dibatalkan demi hemat memori (resource_type
    # Playwright: "image", "media", "font", ...). Kosong = muat semuanya.
    # Diumumkan di sini, bukan di dalam adapter, supaya pemeriksaan sesi di
    # verify.py memakai penawar yang sama dengan upload: dulu hanya upload
    # yang hemat, dan pemeriksaannyalah yang mati kehabisan memori.
    jenis_diblokir: tuple[str, ...] = ()

    async def blokir_berat(self, page) -> None:
        """Batalkan gambar, video, dan font kalau platform ini memintanya."""
        if not self.jenis_diblokir:
            return
        berat = set(self.jenis_diblokir)

        async def penangan(route) -> None:
            try:
                if route.request.resource_type in berat:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:  # noqa: BLE001 - permintaan sudah selesai duluan
                pass

        try:
            await page.route("**/*", penangan)
        except Exception:  # noqa: BLE001 - hemat memori tidak boleh menggagalkan
            pass

    def cookie_wilayah(self, nilai: str) -> list[dict]:
        """Cookie penunjuk wilayah data, untuk semua domain cookie sesi."""
        domains: list[str] = []
        for nilai_domain in self.session_cookie_map.values():
            for domain in [nilai_domain] if isinstance(nilai_domain, str) else nilai_domain:
                if domain not in domains:
                    domains.append(domain)
        return [
            {
                "name": self.wilayah_cookie,
                "value": nilai,
                "domain": domain,
                "path": "/",
                "secure": True,
                "httpOnly": False,
            }
            for domain in domains
        ]

    async def buka_masuk(
        self, page, url: str, ditolak, tunggu_ms: int = 4_000, timeout_ms: int = 60_000
    ) -> str | None:
        """Buka `url` sebagai akun yang sudah masuk.

        `ditolak(page)` mengembalikan True kalau yang muncul halaman login.
        Kalau ditolak dan platform ini punya wilayah kandidat, tiap wilayah
        dicoba sampai ada yang diterima. Kembaliannya wilayah yang berhasil
        ("" = tidak perlu cookie wilayah), atau None kalau semua ditolak.
        """
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if not await self._tunggu_putusan(page, ditolak, tunggu_ms):
            return ""
        if not (self.wilayah_cookie and self.wilayah_kandidat):
            return None
        for wilayah in self.wilayah_kandidat:
            await page.context.add_cookies(self.cookie_wilayah(wilayah))
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if not await self._tunggu_putusan(page, ditolak, tunggu_ms):
                print(f"[{self.platform_key}] wilayah data: {wilayah}", flush=True)
                return wilayah
        return None

    @staticmethod
    async def _tunggu_putusan(page, ditolak, tunggu_ms: int) -> bool:
        """True kalau halamannya PERNAH tampak login selama jendela tunggu.

        Dulu `ditolak()` dipanggil sekali saja, di ujung jeda tetap. Itu
        lomba, dan lombanya bisa kalah: X memuat berandanya dulu lalu baru
        mengalihkan ke halaman masuk, jadi sesi yang sudah mati bisa terbaca
        sah hanya karena pengalihannya datang sedetik setelah jeda habis.
        Diuji dengan cookie palsu: 2 dari 3 kali divonis sah.

        Mengintip berkali-kali sepanjang jendela yang sama hanya bisa
        menemukan LEBIH banyak penolakan, tidak pernah lebih sedikit -
        jendelanya tetap ditunggu penuh sebelum halaman dinyatakan diterima,
        supaya isinya sempat termuat untuk dibaca.
        """
        habis = time.monotonic() + max(tunggu_ms, 0) / 1000
        while True:
            await page.wait_for_timeout(500)
            if await ditolak(page):
                # Sekali lagi sesudah jeda. Sebagian platform MELINTASI
                # halaman masuk walau sesinya sah (YouTube Studio sesekali
                # singgah di accounts.google.com sebelum mendarat). Tanpa
                # konfirmasi ini, mengintip berkali-kali justru menukar
                # kesalahan lama dengan kesalahan baru: sesi sah divonis
                # mati. Halaman masuk yang sungguhan tidak pergi ke mana-mana.
                await page.wait_for_timeout(1_500)
                if await ditolak(page):
                    return True
            if time.monotonic() >= habis:
                return False

    def __init__(self, session_name: str = "default", headless: bool = True, **_: object) -> None:
        self.session_name = session_name
        self.headless = headless

    @property
    def profile(self) -> Path:
        return profile_dir(self.platform_key, self.session_name)

    def has_session(self) -> bool:
        return self.profile.exists() and any(self.profile.iterdir())

    def session_cookies(self, raw_session_id: str) -> list[dict]:
        """Susun cookie Playwright dari string Session ID mentah.

        Satu cookie boleh punya beberapa domain (mis. cookie auth YouTube ada
        di .google.com dan .youtube.com sekaligus).
        """
        names = list(self.session_cookie_map.keys())
        # Terima pemisah "|" (utama) maupun ":" (kebiasaan lama).
        parts = raw_session_id.split("|") if "|" in raw_session_id else raw_session_id.split(":")
        core_names = [n for n in names if n not in self.session_cookie_optional]
        if len(parts) == len(names):
            used = names
        elif len(parts) == len(core_names):
            used = core_names
        else:
            expected = (
                f"{len(names)} (lengkap) atau {len(core_names)} (inti, tanpa "
                f"{'/'.join(self.session_cookie_optional)})"
                if self.session_cookie_optional
                else str(len(names))
            )
            raise PlatformError(
                f"Session ID untuk {self.platform_key} harus {expected} nilai "
                f"dipisah '|': {'|'.join(names)}"
            )
        cookies: list[dict] = []
        for name, value in zip(used, parts):
            domains = self.session_cookie_map[name]
            if isinstance(domains, str):
                domains = [domains]
            for domain in domains:
                cookies.append(
                    {
                        "name": name,
                        "value": value.strip(),
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        # WAJIB tetap bisa dibaca JS: YouTube Studio menghitung
                        # header Authorization: SAPISIDHASH tiap RPC youtubei/v1
                        # dari document.cookie (SAPISID). Dengan httpOnly=True
                        # semua RPC balas 401 dan dialog upload mentok.
                        "httpOnly": False,
                    }
                )
        return cookies

    async def upload(
        self, media_path: Path, caption: str, media_kind: MediaKind, **options: object
    ) -> UploadResult:
        opts = dict(options)
        session_id = str(opts.pop("session_id", "") or "")
        if not session_id and not self.has_session():
            raise PlatformError(
                f"Sesi browser {self.platform_key} belum ada. Login dulu lewat "
                "menu Akun (Login Browser) atau tambahkan akun dengan Session ID."
            )
        lock = _profile_locks.setdefault(str(self.profile), asyncio.Lock())
        async with lock:  # serialisasi upload pada profil akun yang sama
            return await self._upload_locked(
                media_path, caption, media_kind, session_id=session_id, **opts
            )

    async def _upload_locked(
        self, media_path: Path, caption: str, media_kind: MediaKind, **options: object
    ) -> UploadResult:
        from playwright.async_api import async_playwright

        _clear_stale_profile_locks(self.profile)

        async with async_playwright() as pw:
            # Platform boleh menonaktifkan emulasi viewport dengan None.
            viewport = getattr(self, "viewport_override", VIEWPORT)
            launch_kwargs: dict = dict(
                user_data_dir=str(self.profile),
                headless=self.headless,
                args=STEALTH_ARGS + list(getattr(self, "extra_args", [])),
            )
            # Konteks berlayar sentuh: Instagram mengabaikan event mouse
            # sintetis pada tombol entry-nya, tetapi menerima event sentuh.
            # Tanpa ini, Playwright tidak mengizinkan tap() sama sekali.
            if getattr(self, "has_touch", False):
                launch_kwargs["has_touch"] = True
            if self.headless:
                launch_kwargs["user_agent"] = CHROME_UA  # lihat CHROME_UA
            if viewport is None:
                launch_kwargs["no_viewport"] = True
            else:
                launch_kwargs["viewport"] = viewport
            session_id = str(options.get("session_id") or "")
            peramban = None

            if session_id and self.session_cookie_map:
                # Konteks SEKALI PAKAI, bukan profil tersimpan. Dua alasannya:
                #
                # 1. Pemeriksaan Session ID ("Periksa") sudah memakai konteks
                #    sekali pakai. Kalau upload memakai profil tersimpan, yang
                #    diuji dan yang dipakai adalah keadaan cookie yang berbeda,
                #    sehingga sesi bisa hijau saat diperiksa tapi gagal saat
                #    dipakai.
                # 2. Profil tersimpan menumpuk sampai ratusan megabyte padahal
                #    tidak lagi berguna begitu semua platform memakai
                #    Session ID.
                luncur = {"headless": self.headless,
                          "args": STEALTH_ARGS + list(getattr(self, "extra_args", []))}
                try:
                    peramban = await pw.chromium.launch(channel="chrome", **luncur)
                except Exception:  # noqa: BLE001 - Chrome asli tidak terpasang
                    peramban = await pw.chromium.launch(**luncur)
                ctx_kwargs: dict = {"user_agent": CHROME_UA}
                ctx_kwargs["viewport"] = viewport
                if viewport is None:
                    ctx_kwargs["no_viewport"] = True
                if getattr(self, "has_touch", False):
                    ctx_kwargs["has_touch"] = True
                context = await peramban.new_context(**ctx_kwargs)
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                kuki = self.session_cookies(session_id)
                await context.add_cookies(kuki)
                # Dicatat sebagai peringatan (server memakai LOG_LEVEL=WARNING)
                # supaya kalau unggahan mendarat di halaman login, bisa langsung
                # dibedakan: cookie tidak terpasang, atau terpasang tapi ditolak.
                log.warning(
                    "[%s] konteks unggah: %d cookie dipasang (session_id %d huruf)",
                    self.platform_key, len(kuki), len(session_id),
                )
            else:
                log.warning(
                    "[%s] unggah TANPA Session ID - memakai profil tersimpan (%s)",
                    self.platform_key, self.profile,
                )
                # Tanpa Session ID, satu-satunya sesi yang ada justru di profil
                # tersimpan hasil login browser - jadi profilnya tetap dipakai.
                try:
                    context = await pw.chromium.launch_persistent_context(
                        channel="chrome", **launch_kwargs
                    )
                except Exception:  # Chrome asli tidak terpasang -> pakai Chromium
                    context = await pw.chromium.launch_persistent_context(**launch_kwargs)
                await context.add_init_script(STEALTH_INIT_SCRIPT)

            page = context.pages[0] if context.pages else await context.new_page()
            try:
                url = await self._do_upload(page, media_path, caption, media_kind, **options)
                return UploadResult(platform=self.platform_key, success=True, url=url or "")
            except Exception as exc:  # laporkan kegagalan browser apa pun
                detail = f"{type(exc).__name__}: {exc}"
                shot = await self._save_debug_shot(page)
                if shot:
                    detail += f" [debug: {shot.name}]"
                return UploadResult(platform=self.platform_key, success=False, error=detail)
            finally:
                await context.close()  # profil (termasuk cookie baru) disimpan saat close
                if peramban is not None:
                    await peramban.close()

    async def _save_debug_shot(self, page) -> Path | None:
        """Simpan screenshot + konteks halaman saat upload gagal, untuk melacak
        selector yang berubah."""
        try:
            DEBUG_SHOTS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%H%M%S")
            _pangkas_debug_shots()
            path = DEBUG_SHOTS_DIR / f"{self.platform_key}_{stamp}.png"
            await page.screenshot(path=str(path), full_page=False)
            buttons = await page.evaluate(
                """() => [...document.querySelectorAll(
                       'button, [role="button"], a[href], input[type="submit"]')]
                     .slice(0, 80)
                     .map(el => (el.getAttribute('aria-label') || el.innerText || '')
                          .trim().slice(0, 60))
                     .filter(t => t)"""
            )
            body_text = await page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 1500) : ''"
            )
            (DEBUG_SHOTS_DIR / f"{self.platform_key}_{stamp}.txt").write_text(
                f"url: {page.url}\ntitle: {await page.title()}\n"
                f"tabs: {[p.url for p in page.context.pages]}\n"
                f"buttons/links: {buttons}\nbody_text:\n{body_text}\n"
            )
            return path
        except Exception:  # debugging tidak boleh merusak jalur error
            return None

    @abstractmethod
    async def _do_upload(
        self, page, media_path: Path, caption: str, media_kind: MediaKind, **options: object
    ) -> str | None:
        """Jalankan alur posting sesungguhnya. Kembalikan URL post kalau ada."""
