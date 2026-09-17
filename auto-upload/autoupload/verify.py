"""Periksa apakah sebuah Session ID benar-benar masih bisa membuka akunnya.

Dipakai halaman Auto Upload: begitu Session ID ditempel, beranda platform itu
dibuka sekali dengan cookie tersebut. Kalau halamannya terbuka sebagai akun
yang sudah masuk, sesinya sah (hijau). Kalau justru dilempar ke halaman login,
sesinya sudah mati (merah).

Pemeriksaannya sengaja memakai konteks browser sekali pakai, bukan profil
tersimpan: tujuannya menguji cookie yang ditempel, bukan sesi yang kebetulan
sudah tersimpan di profil. Tidak ada yang diunggah dan tidak ada yang diubah
di akunnya.
"""

from __future__ import annotations

import logging

from autoupload.browser import CHROME_UA, STEALTH_ARGS, STEALTH_INIT_SCRIPT, VIEWPORT
from autoupload.platforms import REGISTRY

log = logging.getLogger(__name__)

# Halaman yang dibuka untuk menguji. Dipilih yang PASTI meminta login: kalau
# sesinya mati, platform melemparnya ke halaman masuk, dan itulah sinyalnya.
URL_CEK: dict[str, str] = {
    "instagram": "https://www.instagram.com/",
    # Halaman ringkasan TikTok Studio, bukan halaman unggahnya: keduanya
    # sama-sama butuh login, tetapi yang ini juga menyebut nama akunnya.
    "tiktok": "https://www.tiktok.com/tiktokstudio",
    "youtube": "https://studio.youtube.com/",
    "facebook": "https://www.facebook.com/me",
    # Threads menampilkan isi publik walau belum masuk, jadi berandanya tidak
    # bisa dipakai menguji. Halaman pengaturan hanya bertahan kalau sesinya
    # sah; kalau tidak, ia dilempar balik ke beranda.
    "threads": "https://www.threads.com/settings/account",
    "twitter": "https://x.com/home",
}

# Potongan alamat yang berarti "kamu belum masuk". Diperiksa pada alamat AKHIR
# setelah semua pengalihan selesai.
PENANDA_LOGIN = (
    "/login",
    "/accounts/login",
    "signin",
    "sign_in",
    "/i/flow/login",
    "accounts.google.com",
    "/auth/",
    "checkpoint",
)

# Halaman yang HANYA bertahan kalau sudah masuk. Kalau alamat akhirnya tidak
# lagi memuat potongan ini, berarti kita dilempar keluar. Dipakai untuk
# platform yang tidak menampilkan formulir login maupun mengalihkan ke /login.
HARUS_BERTAHAN: dict[str, str] = {
    "threads": "/settings",
}

# Judul halaman yang berarti belum masuk. Diperiksa huruf kecil semua.
JUDUL_LOGIN = ("log in", "login", "sign in", "masuk", "sign up")

# Cara membaca nama akun dari tiap platform. Tidak ada yang seragam: sebagian
# menaruhnya di data halaman, sebagian di tautan profil, sebagian di tombol
# pengalih akun. Dibaca setelah sesinya dinyatakan sah, dan gagal membacanya
# tidak membatalkan hasil — statusnya tetap hijau, hanya tanpa nama.
NAMA_JS: dict[str, str] = {
    "instagram": """() => {
        const m = document.documentElement.innerHTML.match(/"username":"([^"]{2,30})"/);
        if (m) return m[1];
        const img = document.querySelector('img[alt$="profile picture"]');
        return img ? (img.alt || "").replace(/'s profile picture$/, "") : "";
    }""",
    "tiktok": """() => {
        const a = document.querySelector('a[href^="/@"]');
        return a ? a.getAttribute('href').slice(2) : "";
    }""",
    "twitter": """() => {
        const e = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
        const m = e && (e.innerText || "").match(/@([A-Za-z0-9_]{1,20})/);
        return m ? m[1] : "";
    }""",
    "facebook": """() => {
        const m = document.documentElement.innerHTML.match(/"NAME":"([^"]{2,40})"/);
        return m ? m[1] : "";
    }""",
    "threads": """() => {
        const a = document.querySelector('a[href^="/@"]');
        if (a) return a.getAttribute('href').slice(2);
        const m = document.documentElement.innerHTML.match(/"username":"([^"]{2,30})"/);
        return m ? m[1] : "";
    }""",
    "youtube": """() => {
        const e = document.querySelector('#account-name, ytcp-account-section #name, #channel-title');
        return e ? e.innerText.trim() : "";
    }""",
}

TIMEOUT_MS = 25_000


async def periksa_sesi(platform: str, session_id: str) -> dict:
    """Buka beranda platform dengan cookie ini. Kembalikan {ok, pesan, url}."""
    kelas = REGISTRY.get(platform)
    if kelas is None:
        return {"ok": False, "pesan": f"Platform '{platform}' tidak dikenal.", "url": ""}
    if not session_id.strip():
        return {"ok": False, "pesan": "Session ID kosong.", "url": "", "nama": ""}

    adapter = kelas(session_name="periksa", headless=True)
    try:
        cookies = adapter.session_cookies(session_id)
    except Exception as error:  # noqa: BLE001 - pesan aslinya sudah jelas
        return {"ok": False, "pesan": str(error), "url": "", "nama": ""}

    url_awal = URL_CEK.get(platform) or getattr(adapter, "start_url", "")
    if not url_awal:
        return {"ok": False, "pesan": "Tidak ada halaman untuk diperiksa.", "url": "", "nama": ""}

    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as pw:
            # Konteks sekali pakai, BUKAN profil tersimpan: yang diuji adalah
            # cookie yang baru ditempel, bukan sesi lama yang kebetulan ada.
            peramban = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
            konteks = await peramban.new_context(user_agent=CHROME_UA, viewport=VIEWPORT)
            await konteks.add_init_script(STEALTH_INIT_SCRIPT)
            await konteks.add_cookies(cookies)
            halaman = await konteks.new_page()
            try:
                await halaman.goto(url_awal, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                # Beri waktu pengalihan sisi-klien selesai; banyak platform
                # baru melempar ke halaman login setelah JavaScript berjalan.
                await halaman.wait_for_timeout(3500)
                akhir = (halaman.url or "").lower()
                # Alamat saja tidak cukup. Instagram dan X menampilkan formulir
                # login di alamat yang sama persis, tanpa mengalihkan ke mana
                # pun; yang membedakan hanya isi halamannya.
                kotak_sandi = await halaman.locator('input[type="password"]').count()
                judul = (await halaman.title() or "").lower()
                nama = ""
                js = NAMA_JS.get(platform)
                if js:
                    try:
                        nama = (await halaman.evaluate(js) or "").strip()[:40]
                    except Exception:  # noqa: BLE001 - nama hanyalah pelengkap
                        nama = ""
            finally:
                await konteks.close()
                await peramban.close()
    except Exception as error:  # noqa: BLE001
        log.warning("Pemeriksaan sesi %s gagal: %s", platform, error)
        return {
            "ok": False,
            "pesan": f"Tidak bisa membuka halamannya: {type(error).__name__}",
            "url": "",
            "nama": "",
        }

    if next((p for p in PENANDA_LOGIN if p in akhir), ""):
        return {
            "ok": False,
            "pesan": "Sesinya sudah tidak berlaku: halaman melempar ke login.",
            "url": akhir,
            "nama": "",
        }
    # Kalau platform punya halaman yang hanya bertahan saat sudah masuk,
    # itulah putusannya — dan tidak perlu menebak dari isi halaman lagi.
    # Halaman pengaturan wajar saja memuat kotak sandi, dan tanpa urutan ini
    # sesi yang sah bisa salah dinyatakan mati.
    wajib = HARUS_BERTAHAN.get(platform)
    if wajib:
        if wajib in akhir:
            return {
                "ok": True,
                "pesan": "Halaman yang butuh login terbuka; sesinya sah.",
                "url": akhir,
                "nama": nama,
            }
        return {
            "ok": False,
            "pesan": "Sesinya sudah tidak berlaku: halamannya dilempar balik ke beranda.",
            "url": akhir,
            "nama": "",
        }
    if kotak_sandi > 0:
        return {
            "ok": False,
            "pesan": "Sesinya sudah tidak berlaku: yang muncul formulir login.",
            "url": akhir,
            "nama": "",
        }
    if any(j in judul for j in JUDUL_LOGIN):
        return {
            "ok": False,
            "pesan": f"Sesinya sudah tidak berlaku: judul halamannya \"{judul[:40]}\".",
            "url": akhir,
            "nama": "",
        }
    return {
        "ok": True,
        "pesan": "Beranda terbuka sebagai akun yang sudah masuk.",
        "url": akhir,
        "nama": nama,
    }
