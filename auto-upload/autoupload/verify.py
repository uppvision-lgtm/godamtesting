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

import asyncio
import logging
import os
import time

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
    # Tetap linimasa, walau ini halaman terberat di X. Halaman pengaturan
    # sempat dicoba karena jauh lebih ringan, tapi DIBATALKAN: diuji dengan
    # cookie palsu, X menahan alamatnya di /settings/account beberapa detik
    # sebelum mengalihkan ke login - cukup lama untuk dibaca sebagai sesi
    # sah. Sesi mati yang divonis hijau jauh lebih berbahaya daripada
    # pemeriksaan yang berat, dan beratnya sudah ditangani dengan
    # memblokir gambar/video (jenis_diblokir) serta "--headless=old".
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
    # Alur masuk X yang baru. Diamati langsung (September 2026): keluar dari
    # /settings/account melempar ke "/i/jf/onboarding/web?redirect_after_
    # login=...&mode=login". Tidak ada satu pun penanda lama yang cocok, jadi
    # sesi mati di X hanya ketahuan lewat kotak sandinya - itu pun kebetulan.
    "/i/jf/",
    "mode=login",
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
    # Hanya huruf, angka, titik, garis bawah - aturan username Instagram. Dulu
    # "[^"]" menerima teks apa pun, dan halaman beranda juga memuat
    # "username":"Instagram user" (label, bukan akun) sehingga akun yang baru
    # ditautkan tampil "Aktif - Instagram user" alih-alih nama aslinya.
    "instagram": """() => {
        const m = document.documentElement.innerHTML.match(/"username":"([A-Za-z0-9._]{2,30})"/);
        if (m) return m[1];
        const img = document.querySelector('img[alt$="profile picture"]');
        return img ? (img.alt || "").replace(/'s profile picture$/, "") : "";
    }""",
    "tiktok": """() => {
        const a = document.querySelector('a[href^="/@"]');
        return a ? a.getAttribute('href').slice(2) : "";
    }""",
    # Tombol pengalih akun di sidebar HANYA memuat "@handle" kalau sidebarnya
    # sedang lebar. Jendela pemeriksaan cuma 1000 px, dan pada lebar itu X
    # menciutkan sidebar jadi ikon tanpa tulisan - sehingga nama akun X selalu
    # kosong dan di halaman hanya tampak "ssid - aktif". Tautan profil di
    # navigasi tetap ada pada lebar berapa pun, dan href-nya adalah handle-nya.
    "twitter": """() => {
        const a = document.querySelector('[data-testid="AppTabBar_Profile_Link"]');
        const h = a && a.getAttribute('href');
        if (h && /^[/][A-Za-z0-9_]{1,20}$/.test(h)) return h.slice(1);
        const e = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
        const m = e && (e.innerText || "").match(/@([A-Za-z0-9_]{1,20})/);
        if (m) return m[1];
        const j = document.documentElement.innerHTML.match(/"screen_name":"([A-Za-z0-9_]{1,20})"/);
        return j ? j[1] : "";
    }""",
    "facebook": """() => {
        const m = document.documentElement.innerHTML.match(/"NAME":"([^"]{2,40})"/);
        return m ? m[1] : "";
    }""",
    "threads": """() => {
        const a = document.querySelector('a[href^="/@"]');
        if (a) return a.getAttribute('href').slice(2);
        const m = document.documentElement.innerHTML.match(/"username":"([A-Za-z0-9._]{2,30})"/);
        return m ? m[1] : "";
    }""",
    "youtube": """() => {
        const e = document.querySelector('#account-name, ytcp-account-section #name, #channel-title');
        return e ? e.innerText.trim() : "";
    }""",
}

TIMEOUT_MS = 25_000

# Lama halaman diintip sesudah terbuka, sebelum putusannya diambil.
JENDELA_PUTUSAN_MS = int(os.getenv("VERIFY_JENDELA_MS", "9000"))

# Batas keras satu pemeriksaan. Proxy Railway memutus permintaan pada 300
# detik dan membalas 502 "upstream error" - halaman lalu hanya bisa berputar
# lalu gagal tanpa sebab. Lebih baik berhenti sendiri lebih awal dengan alasan
# yang bisa dibaca. TikTok yang mencoba lima wilayah adalah kasus terpanjang:
# 6 x (TIMEOUT_MS + jeda) kira-kira 3 menit.
BATAS_DETIK = float(os.getenv("VERIFY_TIMEOUT_DETIK", "240"))

# Di atas ini pemeriksaan dianggap lambat dan dicatat sebagai peringatan.
LAPOR_LAMBAT_DETIK = float(os.getenv("VERIFY_LAMBAT_DETIK", "20"))

# Satu pemeriksaan pada satu waktu. Tiap pemeriksaan menyalakan Chromium
# sendiri, dan dua sekaligus tidak muat di kontainer ~1 GB: keduanya lalu
# dibunuh kernel di tengah jalan dan gagal dengan "TargetClosedError".
# Mengantre membuat yang kedua lambat, bukan gagal.
_ANTRE = asyncio.Semaphore(int(os.getenv("VERIFY_SERENTAK", "1")))

PLATFORM_NAMA = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "twitter": "X / Twitter",
    "threads": "Threads",
    "facebook": "Facebook",
    "youtube": "YouTube",
}


# Galat yang berarti "proses peramban mati", bukan "halaman menolak".
_TANDA_MATI = ("targetclosed", "target crashed", "page crashed", "browser has been closed")


def _memori() -> str:
    """Keadaan memori kontainer, untuk ditempel di catatan kegagalan."""
    try:
        from autoupload.uploader import _keadaan_memori

        return _keadaan_memori() or "memori kontainer: tidak terbaca"
    except Exception:  # noqa: BLE001 - keterangan tambahan, bukan syarat
        return "memori kontainer: tidak terbaca"


def _pesan_gagal(platform: str, error: Exception) -> str:
    """Terjemahkan kegagalan pemeriksaan jadi kalimat yang jujur.

    "TargetClosedError" tidak berarti apa-apa bagi pengguna, dan yang lebih
    buruk: ia terbaca seperti masalah pada Session ID yang baru saja ditempel,
    padahal akunnya tersimpan baik-baik saja dan sesinya belum tentu salah.
    Yang sebenarnya terjadi adalah proses peramban di server dibunuh kernel
    karena kehabisan memori - paling sering di X, yang aplikasi webnya
    memuat 143 berkas skrip dan merupakan halaman terberat yang dibuka alat
    ini.
    """
    teks = f"{type(error).__name__}: {error}".lower()
    if any(t in teks for t in _TANDA_MATI):
        nama = PLATFORM_NAMA.get(platform, platform.title())
        return (
            f"Peramban di server kehabisan memori saat membuka {nama}, jadi "
            "sesinya belum sempat diperiksa. Akunnya tetap tersimpan - coba "
            "tekan Periksa lagi beberapa saat lagi. Kalau terus berulang, "
            "memori server memang perlu dinaikkan."
        )
    return f"Tidak bisa membuka halamannya: {type(error).__name__}"


async def _tampak_login(halaman, platform: str) -> bool:
    """True kalau halamannya meminta login, bukan menampilkan isi akun."""
    akhir = (halaman.url or "").lower()
    if any(p in akhir for p in PENANDA_LOGIN):
        return True
    wajib = HARUS_BERTAHAN.get(platform)
    if wajib:
        # Halaman pengaturan wajar memuat kotak sandi, jadi untuk platform ini
        # bertahannya alamat yang menentukan - bukan isi halaman.
        return wajib not in akhir
    if await halaman.locator('input[type="password"]').count():
        return True
    judul = (await halaman.title() or "").lower()
    return any(j in judul for j in JUDUL_LOGIN)


async def _antre_lalu_periksa(platform: str, session_id: str) -> dict:
    """Tunggu giliran, baru periksa."""
    async with _ANTRE:
        return await _periksa_inti(platform, session_id)


async def periksa_sesi(platform: str, session_id: str) -> dict:
    """Periksa sesi, tapi jangan pernah melebihi batas waktu proxy.

    Pembungkus tipis di atas :func:`_periksa_inti`. Tanpa ini pemeriksaan yang
    macet menggantung sampai proxy memutusnya pada 300 detik, dan yang sampai
    ke pengguna cuma 502 tanpa penjelasan.
    """
    mulai = time.monotonic()
    try:
        # Batas waktunya menghitung antrean SEKALIGUS pemeriksaannya. Kalau
        # hanya pemeriksaannya yang dibatasi, yang mengantre lama tetap bisa
        # menembus batas 300 detik proxy Railway dan berakhir sebagai 502
        # tanpa penjelasan - persis yang hendak dihindari di sini.
        hasil = await asyncio.wait_for(_antre_lalu_periksa(platform, session_id), BATAS_DETIK)
    except asyncio.TimeoutError:
        log.warning("Pemeriksaan sesi %s melewati %.0f detik", platform, BATAS_DETIK)
        return {
            "ok": False,
            "pesan": (
                f"Pemeriksaan tidak selesai dalam {BATAS_DETIK:.0f} detik. "
                f"{PLATFORM_NAMA.get(platform, platform.title())} mungkin sedang "
                "lambat atau menahan permintaan dari server ini. Coba lagi "
                "beberapa saat lagi."
            ),
            "url": "",
            "nama": "",
        }
    lama = time.monotonic() - mulai
    # Server ini berjalan dengan LOG_LEVEL=WARNING, jadi catatan INFO tidak
    # pernah sampai ke log Railway. Yang cepat cukup dicatat INFO (berguna saat
    # menyelidiki di lokal); yang lambat dinaikkan ke WARNING supaya kalau
    # pemeriksaan melambat lagi, buktinya ada tanpa perlu mengubah setelan.
    if lama > LAPOR_LAMBAT_DETIK:
        log.warning("Pemeriksaan sesi %s LAMBAT: %.1f detik", platform, lama)
    else:
        log.info("Pemeriksaan sesi %s selesai dalam %.1f detik", platform, lama)
    return hasil


async def _periksa_inti(platform: str, session_id: str) -> dict:
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
            t0 = time.monotonic()
            # extra_args milik adapter IKUT dipakai. Tanpa ini X diperiksa
            # dengan headless baru, yang menjalankan tumpukan peramban penuh
            # dan kehabisan memori di server - padahal jalur upload sudah
            # lama memakai "--headless=old" persis untuk menghindarinya.
            peramban = await pw.chromium.launch(
                headless=True,
                args=STEALTH_ARGS + list(getattr(adapter, "extra_args", [])),
            )
            lama_luncur = time.monotonic() - t0
            (log.warning if lama_luncur > LAPOR_LAMBAT_DETIK else log.info)(
                "[%s] chromium hidup dalam %.1f detik", platform, lama_luncur
            )
            konteks = await peramban.new_context(user_agent=CHROME_UA, viewport=VIEWPORT)
            await konteks.add_init_script(STEALTH_INIT_SCRIPT)
            await konteks.add_cookies(cookies)
            halaman = await konteks.new_page()
            # Penawar memori yang sama dengan jalur upload. Yang dibaca di
            # bawah cuma alamat, judul, kotak sandi, dan nama akun - tidak
            # ada satu pun yang butuh gambar atau video termuat.
            await adapter.blokir_berat(halaman)
            try:
                # Beri waktu pengalihan sisi-klien selesai; banyak platform
                # baru melempar ke halaman login setelah JavaScript berjalan.
                # buka_masuk juga mencoba wilayah data lain untuk platform yang
                # sesinya terikat wilayah (TikTok), supaya sesi yang sah tidak
                # divonis mati hanya karena servernya ada di benua lain.
                async def ditolak(p) -> bool:
                    return await _tampak_login(p, platform)

                t1 = time.monotonic()
                # 3500 ms dulu terlalu pendek: pengalihan X ke halaman
                # masuk kadang baru datang sesudahnya. Jendela ini sekarang
                # dipakai untuk mengintip berulang, bukan tidur, jadi
                # memperpanjangnya menambah ketelitian - bukan kelambatan:
                # halaman yang sudah jelas ditolak berhenti lebih awal.
                wilayah = await adapter.buka_masuk(
                    halaman, url_awal, ditolak, JENDELA_PUTUSAN_MS, timeout_ms=TIMEOUT_MS
                )
                lama_buka = time.monotonic() - t1
                (log.warning if lama_buka > LAPOR_LAMBAT_DETIK else log.info)(
                    "[%s] halaman terbuka dalam %.1f detik", platform, lama_buka
                )
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
        log.warning("Pemeriksaan sesi %s gagal: %s | %s", platform, error, _memori())
        return {
            "ok": False,
            "pesan": _pesan_gagal(platform, error),
            "url": "",
            "nama": "",
        }

    if wilayah:
        # Sesinya sah, tapi hanya lewat wilayah data itu. Disebut supaya kalau
        # suatu saat gagal lagi, penyebabnya tidak perlu dicari dari nol.
        return {
            "ok": True,
            "pesan": f"Halaman akun terbuka; sesinya sah (wilayah data: {wilayah}).",
            "url": akhir,
            "nama": nama,
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
