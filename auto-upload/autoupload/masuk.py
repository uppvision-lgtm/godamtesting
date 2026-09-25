"""Masuk ke platform memakai username & password, lalu ambil cookie sesinya.

Ini jalan kedua di Kelola Akun, di samping menempel Session ID. Server
membuka peramban tanpa jendela, mengisi formulir login platform, menunggu
hasilnya, lalu membaca cookie sesi yang muncul setelah login berhasil.
Cookie itulah yang disimpan - persis seperti jalur SSID - sehingga semua alat
lain (unggah, periksa, komentar) tidak perlu tahu dari mana sesinya datang.

Password TIDAK PERNAH disimpan dan tidak pernah dicatat ke log. Ia hanya
lewat sekali, di memori, untuk mengisi formulir.

Yang perlu diketahui pemakainya:
- Akun dengan verifikasi dua langkah (2FA) tidak bisa masuk lewat sini:
  platform meminta kode yang tidak kita punya. Jawabannya menyuruh memakai
  SSID, bukan berputar-putar.
- Platform sering mencurigai login otomatis dari server (captcha, "unusual
  activity"). Itu juga dilaporkan apa adanya, bukan disamarkan.
- YouTube/Google ditolak di depan: Google memblokir sign-in otomatis
  ("This browser or app may not be secure"), jadi menjanjikannya sia-sia.
"""

from __future__ import annotations

import logging
import time

from autoupload.browser import CHROME_UA, STEALTH_ARGS, STEALTH_INIT_SCRIPT, VIEWPORT
from autoupload.login import LOGIN_COOKIE
from autoupload.platforms import REGISTRY

log = logging.getLogger("autoupload")

# Halaman formulir login tiap platform. TikTok memakai halaman email/sandi,
# bukan QR yang dipakai jalur "Login Browser".
URL_MASUK = {
    "instagram": "https://www.instagram.com/accounts/login/",
    "threads": "https://www.threads.com/login",
    "twitter": "https://x.com/i/flow/login",
    "facebook": "https://www.facebook.com/login",
    "tiktok": "https://www.tiktok.com/login/phone-or-email/email",
}

# Platform yang ditolak di depan, beserta alasannya.
TIDAK_DIDUKUNG = {
    "youtube": (
        "YouTube tidak bisa dimasuki otomatis dengan username & password: Google "
        "memblokir sign-in dari peramban otomatis. Untuk YouTube pakai SSID."
    ),
}

# Potongan alamat / teks yang berarti platform meminta langkah tambahan yang
# tidak bisa kita jawab (kode 2FA, captcha, pemeriksaan keamanan).
TANDA_TANTANGAN = (
    "/challenge/", "/two_factor", "two-factor", "/checkpoint/", "twofactor",
    "verification", "verify", "captcha", "unusual", "suspicious",
    "login_challenge", "/auth_platform/",
)

BATAS_DETIK = 75.0
TUNGGU_HASIL_DETIK = 30.0


class MasukGagal(Exception):
    """Login tidak menghasilkan sesi; pesannya layak dibaca pengguna."""


def _rahasia_aman(teks: str, sandi: str) -> str:
    """Pastikan password tidak pernah bocor ke pesan atau log."""
    return teks.replace(sandi, "***") if sandi else teks


async def _isi(page, pemilih: str, nilai: str, timeout: int = 15_000) -> None:
    kotak = page.locator(pemilih).first
    try:
        await kotak.wait_for(state="visible", timeout=timeout)
    except Exception as error:  # noqa: BLE001 - diberi nama langkahnya
        raise LangkahHilang(pemilih.split(",")[0].strip()) from error
    # fill() sendiri sudah memfokuskan kotaknya. Klik terpisah dulu dipakai,
    # dan di X ia macet 30 detik: kotaknya terlihat tetapi tertutup lapisan
    # lain, sehingga seluruh login gagal sebagai "TimeoutError" tanpa sebab.
    await kotak.fill(nilai, timeout=timeout)


class LangkahHilang(Exception):
    """Kotak isian yang ditunggu tidak muncul; membawa nama selektornya."""


async def _simpan_tangkapan(page, platform: str) -> str:
    """Simpan tangkapan layar ke folder debug uploader; kembalikan namanya."""
    try:
        from autoupload.config import DEBUG_SHOTS_DIR, MAKS_DEBUG_SHOT

        DEBUG_SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        nama = f"masuk_{platform}_{time.strftime('%H%M%S')}.png"
        await page.screenshot(path=str(DEBUG_SHOTS_DIR / nama))
        lama = sorted(DEBUG_SHOTS_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime)
        for f in lama[:-MAKS_DEBUG_SHOT]:
            f.unlink(missing_ok=True)
        return nama
    except Exception:  # noqa: BLE001 - tangkapan hanya pelengkap
        return ""


async def _klik_bila_ada(page, pemilih: str, timeout: int = 4_000) -> bool:
    try:
        tombol = page.locator(pemilih).first
        await tombol.wait_for(state="visible", timeout=timeout)
        await tombol.click()
        return True
    except Exception:  # noqa: BLE001 - tombolnya memang boleh tidak ada
        return False


# ---------------------------------------------------------------- per platform

async def _masuk_instagram(page, nama: str, sandi: str) -> None:
    # Instagram mengganti nama kolomnya (diamati September 2026: "email" dan
    # "pass", bukan "username"/"password"). Keduanya diterima supaya tidak
    # patah lagi saat mereka menggantinya kembali.
    await _isi(page, 'input[name="username"], input[name="email"], input[autocomplete^="username"]', nama)
    await _isi(page, 'input[name="password"], input[name="pass"], input[type="password"]', sandi)
    await page.keyboard.press("Enter")


async def _masuk_threads(page, nama: str, sandi: str) -> None:
    # Akun Threads adalah akun Instagram. Formulir di threads.com sendiri
    # diam saja terhadap Enter maupun klik "Login" (diamati: tidak ada reaksi
    # apa pun dalam 9 detik), jadi jalannya: masuk di instagram.com dengan
    # formulir yang terbukti bekerja, lalu buka threads.com dan tekan
    # "Lanjutkan dengan Instagram" supaya sesinya diteruskan ke domain Threads.
    await page.goto(URL_MASUK["instagram"], wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(2_500)
    await _masuk_instagram(page, nama, sandi)
    # Tunggu Instagram-nya sendiri masuk (atau menolak) dulu.
    habis = time.monotonic() + 25
    while time.monotonic() < habis:
        await page.wait_for_timeout(1_000)
        cookies = {c["name"]: c for c in await page.context.cookies()}
        if _cookie_wajib_ada(cookies, "instagram"):
            break
        if await _pesan_galat_halaman(page) or await _tampak_tantangan(page):
            return  # putusan (sandi salah / tantangan) dibaca pemanggil
    await page.goto(URL_MASUK["threads"], wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(2_500)
    await _klik_bila_ada(
        page,
        '[role="button"]:has-text("Lanjutkan dengan Instagram"), '
        '[role="button"]:has-text("Continue with Instagram"), '
        '[role="button"]:has-text("Lanjutkan sebagai"), [role="button"]:has-text("Continue as")',
        8_000,
    )


async def _masuk_twitter(page, nama: str, sandi: str) -> None:
    # X punya dua alur. Yang baru (diamati September 2026 di Chrome asli,
    # /i/jf/onboarding/web?mode=login) menaruh username dan password di SATU
    # halaman: name="username_or_email" + name="password". Yang lama
    # bertahap: username -> Next -> (kadang konfirmasi) -> password.
    await _isi(page, 'input[name="username_or_email"], input[autocomplete="username"]', nama, 25_000)
    if await page.locator('input[name="password"]').count() and await page.locator('input[name="password"]').first.is_visible():
        await _isi(page, 'input[name="password"]', sandi)
        await page.keyboard.press("Enter")
        return
    # Layar pembuka X ("Lihat apa yang sedang terjadi"): tombolnya "Lanjutkan"
    # / "Next", dan Enter di dalam kotak username tidak memicunya.
    if not await _klik_bila_ada(
        page,
        '[role="button"]:has-text("Lanjutkan"), [role="button"]:has-text("Next"), '
        '[role="button"]:has-text("Berikutnya")',
        4_000,
    ):
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2_500)
    # Langkah "unusual activity": X meminta username sekali lagi di kotak
    # teks polos. Kalau muncul, isi lagi dengan nama yang sama.
    konfirmasi = page.locator('input[data-testid="ocfEnterTextTextInput"]')
    try:
        if await konfirmasi.count() and await konfirmasi.first.is_visible():
            await konfirmasi.first.fill(nama)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2_000)
    except Exception:  # noqa: BLE001
        pass
    await _isi(page, 'input[name="password"]', sandi, 25_000)
    await page.keyboard.press("Enter")


async def _masuk_facebook(page, nama: str, sandi: str) -> None:
    await _klik_bila_ada(page, 'button[data-cookiebanner="accept_button"]', 2_500)
    await _isi(page, '#email, input[name="email"]', nama)
    await _isi(page, '#pass, input[name="pass"]', sandi)
    await page.keyboard.press("Enter")


async def _masuk_tiktok(page, nama: str, sandi: str) -> None:
    await _isi(page, 'input[name="username"], input[placeholder*="mail" i]', nama, 25_000)
    await _isi(page, 'input[type="password"]', sandi)
    await page.keyboard.press("Enter")


PENGISI = {
    "instagram": _masuk_instagram,
    "threads": _masuk_threads,
    "twitter": _masuk_twitter,
    "facebook": _masuk_facebook,
    "tiktok": _masuk_tiktok,
}


# ---------------------------------------------------------------- inti

def _cookie_wajib_ada(cookies: dict[str, dict], platform: str) -> bool:
    return any(
        n in cookies and cookies[n].get("value") for n in LOGIN_COOKIE.get(platform, ())
    )


async def _tampak_tantangan(page) -> bool:
    alamat = (page.url or "").lower()
    if any(t in alamat for t in TANDA_TANTANGAN):
        return True
    try:
        # Kotak kode verifikasi 6 digit adalah tanda 2FA yang paling umum.
        if await page.locator(
            'input[name="verificationCode"], input[name="security_code"], '
            'input[autocomplete="one-time-code"], input[name="approvals_code"]'
        ).count():
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


async def _pesan_galat_halaman(page) -> str:
    """Teks kesalahan yang ditampilkan platform (sandi salah, akun tak ada)."""
    try:
        teks = (await page.locator("body").inner_text(timeout=2_000) or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    # Kalimat yang benar-benar ditampilkan, diamati langsung (September 2026):
    # Instagram "The login information you entered is incorrect", Facebook
    # (lokal Indonesia) "... tidak terhubung ke akun" / "Kata sandi yang Anda
    # masukkan salah". Sisanya ragam lama yang masih mungkin muncul.
    for kata in (
        "login information you entered is incorrect", "informasi login yang anda masukkan",
        "tidak terhubung ke akun", "tidak cocok dengan akun",
        "password was incorrect", "incorrect password", "kata sandi salah",
        "password you entered is incorrect", "wrong password",
        "doesn't belong to an account", "couldn't find your account",
        "we couldn't find", "username or password", "sandi yang anda masukkan",
        "maximum number of attempts", "too many attempts", "terlalu banyak percobaan",
        "batas percobaan maksimum",
    ):
        if kata in teks:
            return kata
    return ""


def _susun_session_id(platform: str, cookies: dict[str, dict]) -> str:
    """Rangkai cookie jadi untai '|' persis seperti yang ditempel di jalur SSID."""
    adapter = REGISTRY[platform]
    nama_cookie = list(adapter.session_cookie_map.keys())
    opsional = set(getattr(adapter, "session_cookie_optional", ()))
    inti = [n for n in nama_cookie if n not in opsional]
    if all(cookies.get(n, {}).get("value") for n in nama_cookie):
        dipakai = nama_cookie
    elif all(cookies.get(n, {}).get("value") for n in inti):
        dipakai = inti
    else:
        hilang = [n for n in inti if not cookies.get(n, {}).get("value")]
        raise MasukGagal(
            f"Login tampak berhasil, tetapi cookie {', '.join(hilang)} tidak muncul. "
            "Coba lagi, atau pakai SSID."
        )
    return "|".join(cookies[n]["value"] for n in dipakai)


async def masuk_dengan_sandi(platform: str, nama: str, sandi: str) -> dict:
    """Login lalu kembalikan {session_id, nama_akun}. Melempar MasukGagal."""
    if platform in TIDAK_DIDUKUNG:
        raise MasukGagal(TIDAK_DIDUKUNG[platform])
    if platform not in PENGISI:
        raise MasukGagal(f"Platform '{platform}' tidak dikenal.")
    nama = nama.strip()
    if not nama or not sandi:
        raise MasukGagal("Username dan password wajib diisi.")

    from playwright.async_api import async_playwright

    adapter = REGISTRY[platform]
    mulai = time.monotonic()
    chrome_asli = False
    try:
        async with async_playwright() as pw:
            # Chrome asli dulu: X menyajikan halaman PUTIH KOSONG ke Chromium
            # headless (diamati: nol kotak isian), tetapi merender alur login
            # penuh di Chrome asli. Di server Chrome asli tidak ada, jadi
            # jatuh ke Chromium - dan untuk X itu berarti gagal dengan pesan
            # yang menyebutnya. Mode headless lama (extra_args X) tidak
            # dipakai di sini: ia juga menghasilkan halaman kosong.
            luncur = {"headless": True, "args": STEALTH_ARGS}
            try:
                peramban = await pw.chromium.launch(channel="chrome", **luncur)
                chrome_asli = True
            except Exception:  # noqa: BLE001 - Chrome asli tidak terpasang
                peramban = await pw.chromium.launch(**luncur)
                chrome_asli = False
            # Lebar layar penuh: pada 1000 px + layar sentuh Instagram
            # menyajikan dinding "pasang aplikasi" alih-alih formulirnya.
            konteks = await peramban.new_context(
                user_agent=CHROME_UA, viewport={"width": 1440, "height": 900}
            )
            await konteks.add_init_script(STEALTH_INIT_SCRIPT)
            page = await konteks.new_page()
            tangkapan = ""
            try:
                await page.goto(URL_MASUK[platform], wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(2_500)
                await PENGISI[platform](page, nama, sandi)

                # Tunggu salah satu: cookie sesi muncul (berhasil), tantangan
                # (2FA/captcha), atau pesan salah sandi.
                habis = time.monotonic() + TUNGGU_HASIL_DETIK
                while True:
                    await page.wait_for_timeout(1_000)
                    cookies = {c["name"]: c for c in await konteks.cookies()}
                    if _cookie_wajib_ada(cookies, platform):
                        break
                    if await _tampak_tantangan(page):
                        raise MasukGagal(
                            f"{platform.title()} meminta langkah tambahan (kode verifikasi "
                            "dua langkah, captcha, atau pemeriksaan keamanan) yang tidak "
                            "bisa dijawab otomatis. Untuk akun ini pakai SSID."
                        )
                    salah = await _pesan_galat_halaman(page)
                    if salah:
                        if "attempt" in salah or "percobaan" in salah:
                            # TikTok menjawab ini pada percobaan PERTAMA dari
                            # peramban otomatis - itu deteksi bot, bukan
                            # hitungan percobaan sungguhan.
                            raise MasukGagal(
                                f"{platform.title()} menolak: terlalu banyak percobaan dari "
                                "server ini. Tunggu beberapa saat, atau pakai SSID."
                            )
                        raise MasukGagal("Username atau password salah menurut platformnya.")
                    if time.monotonic() >= habis:
                        raise MasukGagal(
                            f"{platform.title()} tidak memberi sesi dalam "
                            f"{TUNGGU_HASIL_DETIK:.0f} detik. Bisa jadi ada langkah tambahan "
                            "yang tidak terdeteksi. Coba lagi, atau pakai SSID."
                        )
                session_id = _susun_session_id(platform, cookies)
            except BaseException:
                # Bukti apa yang sebenarnya tampil, selagi halamannya masih ada.
                tangkapan = await _simpan_tangkapan(page, platform)
                raise
            finally:
                await konteks.close()
                await peramban.close()
    except MasukGagal as error:
        if tangkapan:
            raise MasukGagal(f"{error} [debug: {tangkapan}]") from None
        raise
    except LangkahHilang as error:
        log.warning("Login sandi %s: kotak %s tidak muncul", platform, error)
        if platform == "twitter" and not chrome_asli:
            raise MasukGagal(
                "X menyajikan halaman kosong ke peramban otomatis di server ini "
                "(butuh Chrome asli, yang hanya ada di komputer). Untuk X pakai SSID."
                + (f" [debug: {tangkapan}]" if tangkapan else "")
            ) from None
        raise MasukGagal(
            f"Kotak isian ({error}) tidak muncul di halaman login {platform.title()} - "
            "tampilannya mungkin berubah atau ada halaman antara."
            + (f" [debug: {tangkapan}]" if tangkapan else "")
        ) from None
    except Exception as error:  # noqa: BLE001
        teks = f"{type(error).__name__}: {error}".lower()
        log.warning("Login sandi %s gagal: %s", platform, _rahasia_aman(str(error)[:160], sandi))
        jejak = f" [debug: {tangkapan}]" if tangkapan else ""
        if any(t in teks for t in ("targetclosed", "target crashed", "page crashed")):
            raise MasukGagal(
                "Peramban di server kehabisan memori saat membuka halaman loginnya. "
                "Coba lagi nanti, atau pakai SSID." + jejak
            ) from None
        raise MasukGagal(f"Tidak bisa membuka halaman loginnya: {type(error).__name__}{jejak}") from None

    log.info("Login sandi %s berhasil dalam %.1f detik", platform, time.monotonic() - mulai)
    return {"session_id": session_id, "nama_akun": nama.lstrip("@")}
